#!/usr/bin/env python3
"""Dashy dashboard: host vitals plus every tailnet-published app, no dependencies."""
import json
import os
import re
import socket
import threading
from collections import deque
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_VERSION = "1.1.3"
APP_NAME = "Dashy"
PORT = int(os.environ.get("DASH_PORT", "8080"))
DATA = os.environ.get("DASH_DATA", "/data")
# Loopback is right for the host-networked case and harmless otherwise; a
# different value only matters if the container cannot reach the host directly.
HOST_IP = os.environ.get("HOST_IP", "127.0.0.1")
# HOST_IP is where this process probes from, so 127.0.0.1 is right. LINK_HOST is
# what goes in a link the browser follows, where 127.0.0.1 would be the viewer's
# own machine. They only differ on a LAN install, which has no tailnet name to use.
LINK_HOST = os.environ.get("LINK_HOST", "").strip() or HOST_IP
# where the worker should put serve.json if it has to create one. The
# container cannot see the host filesystem, so this is a hint, not a lookup.
SERVE_HOST_HINT = os.environ.get("SERVE_HOST_PATH", "").strip()

SERVE = os.environ.get("SERVE_JSON", "/serve/serve.json")


def serve_tailnet():
    """Read a literal node name from Serve. Tailscale's ${TS_CERT_DOMAIN}
    template is intentionally skipped; the root worker resolves that template
    through the Tailscale status command after enabling Serve."""
    try:
        with open(SERVE) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return ""
    for host in (cfg.get("Web") or {}):
        h = host.rsplit(":", 1)[0].strip()
        if h and "${" not in h and "$TS_CERT_DOMAIN" not in h:
            return h
    return ""


def tailnet_host():
    """Resolve this node's FQDN dynamically, including after first-time setup."""
    env = os.environ.get("TAILNET_HOST", "").strip().rstrip(".")
    if env and "${" not in env and "$TS_CERT_DOMAIN" not in env:
        return env
    try:
        with open(os.path.join(DATA, "tailnet-host")) as fh:
            saved = fh.read().strip().rstrip(".")
        if saved:
            return saved
    except OSError:
        pass
    return serve_tailnet()


QUEUE = os.environ.get("DASH_QUEUE", "/queue")
# Layout has to be shared, not per-browser: the same arrangement should greet
# you on the phone and the desktop. /data is read-only (the collector owns it),
# so the shared state gets its own writable mount.
CONFIG = os.environ.get("DASH_CONFIG", "/config")
HEXOS_URL = os.environ.get("HEXOS_URL", "https://deck.hexos.com/dash")

# Where this install gets its own updates. The check is done by the cron job,
# not by the container, and it is a plain file read here.
UPDATE_REPO = os.environ.get("UPDATE_REPO", "Boldestfutura/dashy")
UPDATE_BRANCH = os.environ.get("UPDATE_BRANCH", "main")

def system_info():
    """This NAS as TrueNAS describes it - name, version, hardware, and which
    installed app is this dashboard - written by the collector from
    system.info. Nothing about any particular machine is known here."""
    try:
        with open(os.path.join(DATA, "system.json")) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def is_self(app_name):
    """Is this installed app the dashboard itself? The collector names it from
    the dataset it runs in; the fallbacks cover a first minute with no file."""
    me = str(system_info().get("app") or "").lower()
    name = str(app_name or "").lower()
    return name == me if me else name in ("dashy", "dashboard", "overlook")


def _hostname():
    """The NAS's name for the header. A container's /proc/sys/kernel/hostname is
    its own UTS namespace - host networking shares the network namespace, not
    that one - so it reports a container id. TrueNAS's own hostname comes
    first; an explicit HOST_NAME and the tailnet name are only fallbacks."""
    v = str(system_info().get("hostname") or "").strip()
    if v:
        return v.split(".")[0]
    v = os.environ.get("HOST_NAME", "").strip()
    if v:
        return v.split(".")[0]
    # The discovered tailnet FQDN also identifies the machine's first label.
    tn = tailnet_host()
    if tn:
        first = tn.split(":")[0].split(".")[0].strip()
        if first:
            return first
    return "nas"


_lock = threading.Lock()
HIST = 150          # samples kept per interface, 2s apart -> about 5 minutes
APP_HIST = 90       # latency samples per app, 20s apart -> about half an hour
_hist = {}
_app_hist = {}
_io_hist = deque(maxlen=HIST)
_cpu_hist = deque(maxlen=HIST)
_ctemp_hist = deque(maxlen=HIST)
_store_hist = deque(maxlen=HIST)
_mem_hist = deque(maxlen=HIST)
_state = {
    "cpu": None,          # percent, rolling
    "cpu_cores": [],
    "core_temps": [],
    "boots": [],
    "availability": None,
    "uptime_info": None,
    "net": [],
    "net_hist": {},
    "io": [],
    "io_hist": [],
    "power": None,
    "power_hist": [],
    "mem_hist": [],
    "cpu_hist": [],
    "ctemp_hist": [],
    "store_hist": [],
    "cores": os.cpu_count() or 1,
    "mem": None,
    "load": None,
    "uptime": None,
    "pools": [],
    "pools_age": None,
    "services": [],
    "cpu_temp": None,
    "drives": [],
    "host": _hostname(),
}


# ---------------------------------------------------------------- host vitals

def _read(path):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


def cpu_snapshot():
    """Total and idle jiffies for the whole chip and for each logical CPU."""
    agg, cores = None, {}
    for line in _read("/proc/stat").split("\n"):
        if not line.startswith("cpu"):
            continue
        bits = line.split()
        try:
            nums = [int(x) for x in bits[1:]]
        except ValueError:
            continue
        if len(nums) < 5:
            continue
        pair = (sum(nums), nums[3] + nums[4])
        if bits[0] == "cpu":
            agg = pair
        else:
            cores[bits[0]] = pair
    return agg, cores


def _pct(prev, cur):
    if not prev or not cur:
        return None
    dt, di = cur[0] - prev[0], cur[1] - prev[1]
    if dt <= 0:
        return None
    return round(max(0.0, min(100.0, (1 - di / dt) * 100)), 1)


def arc_size():
    """ZFS cache. It counts as 'used' but is handed back the moment anything needs it."""
    for line in _read("/proc/spl/kstat/zfs/arcstats").split("\n"):
        bits = line.split()
        if len(bits) == 3 and bits[0] == "size":
            try:
                return int(bits[2])
            except ValueError:
                return None
    return None


def mem_now():
    info = {}
    for line in _read("/proc/meminfo").split("\n"):
        bits = line.split(":")
        if len(bits) == 2:
            try:
                info[bits[0]] = int(bits[1].strip().split()[0]) * 1024
            except (ValueError, IndexError):
                pass
    total = info.get("MemTotal")
    avail = info.get("MemAvailable")
    if not total:
        return None
    used = total - (avail if avail is not None else total)
    arc = arc_size() or 0
    apps = max(0, used - arc)
    return {"total": total, "used": used, "free": total - used,
            "arc": arc, "apps": apps,
            "pct": round(used / total * 100, 1),
            "apps_pct": round(apps / total * 100, 1)}


def load_now():
    bits = _read("/proc/loadavg").split()
    if len(bits) < 3:
        return None
    try:
        return [float(bits[0]), float(bits[1]), float(bits[2])]
    except ValueError:
        return None


def uptime_now():
    bits = _read("/proc/uptime").split()
    try:
        return float(bits[0])
    except (ValueError, IndexError):
        return None


SKIP_IF = ("lo", "docker", "br-", "veth", "virbr", "dummy", "kube", "cni")


def net_snapshot():
    """Per-interface byte counters. Needs host networking, or we only see our own veth."""
    out = {}
    for line in _read("/proc/net/dev").split("\n")[2:]:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        name = name.strip()
        if name.startswith(SKIP_IF) or name == "lo":
            continue
        bits = rest.split()
        if len(bits) < 10:
            continue
        try:
            out[name] = {"rx": int(bits[0]), "tx": int(bits[8]),
                         "rx_err": int(bits[2]), "tx_err": int(bits[10]) if len(bits) > 10 else 0}
        except ValueError:
            continue
    return out


DISK_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+)$")


def disk_snapshot():
    """Sectors read and written per device. 512-byte sectors is the kernel's unit here."""
    out = {}
    for line in _read("/proc/diskstats").split("\n"):
        bits = line.split()
        if len(bits) < 10:
            continue
        name = bits[2]
        if not DISK_RE.match(name):
            continue
        try:
            out[name] = (int(bits[5]) * 512, int(bits[9]) * 512)
        except ValueError:
            continue
    return out


def power_now():
    """Watts, from the trail the root cron job leaves (RAPL is root-only)."""
    rows = []
    for line in _read(os.path.join(DATA, "power.tsv")).strip().split("\n"):
        bits = line.split("\t")
        if len(bits) != 2:
            continue
        try:
            rows.append([int(bits[0]), int(bits[1])])
        except ValueError:
            continue
    return rows[-HIST:]


def link_speed(name):
    raw = _read("/sys/class/net/%s/speed" % name).strip()
    try:
        v = int(raw)
        return v if v > 0 else None
    except ValueError:
        return None


def _vtuple(v):
    """0.8.10 sorts after 0.8.9, which a string compare would get wrong."""
    out = []
    for part in str(v or "").split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out + [0] * (4 - len(out)))[:4]


# A check the user asked for, held in memory because /data is the collector's
# and mounted read-only here. It wins over the file while it is the newer of
# the two; the cron job's own check overtakes it half an hour later.
_checked_now = {"latest": "", "checked": 0, "repo": ""}


def _fetch_latest(repo):
    """Ask whichever source this install actually updates from. An image
    install can only run what the registry holds; a file install reads main."""
    if repo.startswith("ghcr.io/"):
        name = repo[len("ghcr.io/"):]
        try:
            tok = _getjson("https://ghcr.io/token?service=ghcr.io"
                           "&scope=repository:%s:pull" % name)["token"]
            tags = _getjson("https://ghcr.io/v2/%s/tags/list" % name,
                            {"Authorization": "Bearer " + tok}).get("tags") or []
            tags = [t for t in tags if t and t[0].isdigit()]
            return max(tags, key=_vtuple) if tags else ""
        except Exception:
            return ""
    try:
        req = urllib.request.Request(
            "https://raw.githubusercontent.com/%s/%s/app/server.py"
            % (repo, UPDATE_BRANCH), headers={"User-Agent": "dashy"})
        with urllib.request.urlopen(req, timeout=20) as r:
            for line in r.read(200000).decode("utf-8", "replace").splitlines():
                if line.startswith("APP_VERSION"):
                    return line.split('"')[1]
    except Exception:
        pass
    return ""


def _getjson(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "dashy"})
    if headers:
        req.add_header("User-Agent", "dashy")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read(1 << 20))


def check_now():
    """Look right now, rather than waiting out the collector's half hour."""
    d = {}
    try:
        with open(os.path.join(DATA, "selfupdate.json")) as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        pass
    repo = str(d.get("repo") or UPDATE_REPO).strip()
    latest = _fetch_latest(repo)
    if not latest:
        return False, "could not reach %s" % (repo.split("/")[0] or "the repository")
    _checked_now.update({"latest": latest, "checked": int(time.time()), "repo": repo})
    if _vtuple(latest) > _vtuple(APP_VERSION):
        return True, "%s is available" % latest
    return True, "up to date"


def self_update():
    """What was last seen upstream, by the cron job or by the user asking."""
    d = {}
    try:
        with open(os.path.join(DATA, "selfupdate.json")) as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        pass
    if _checked_now["latest"] and _checked_now["checked"] >= (d.get("checked") or 0):
        d = _checked_now
    latest = str(d.get("latest") or "").strip()
    if not latest:
        return None
    return {"latest": latest,
            "current": APP_VERSION,
            "available": _vtuple(latest) > _vtuple(APP_VERSION),
            "checked": d.get("checked"),
            "repo": d.get("repo") or UPDATE_REPO}


def update_status():
    try:
        with open(os.path.join(DATA, "updates.json")) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def queued():
    """Requests waiting for the root worker, which picks them up within a
    minute. Without this an update just tapped showed nothing at all until the
    worker reached it, which looked like the tap had not worked."""
    try:
        names = os.listdir(QUEUE)
    except OSError:
        return []
    return sorted(n[:-4] for n in names if n.endswith(".req"))


def request_upgrade(app):
    """Drop a request for the root worker. The container cannot call midclt itself."""
    # this app updates itself from the repo rather than through midclt, so it
    # is checked before the app-name rule that it deliberately does not match
    if app == "_self":
        try:
            os.makedirs(QUEUE, exist_ok=True)
            with open(os.path.join(QUEUE, "_self.req"), "w") as fh:
                fh.write(str(int(time.time())))
        except OSError as e:
            return False, str(e)
        return True, "queued"
    if not app or not re.match(r"^[a-z0-9][a-z0-9._-]{0,48}$", app):
        return False, "bad app name"
    known = {s.get("app") for s in discover() if s.get("app")}
    if app not in known:
        return False, "unknown app"
    try:
        os.makedirs(QUEUE, exist_ok=True)
        with open(os.path.join(QUEUE, app + ".req"), "w") as fh:
            fh.write(str(int(time.time())))
    except OSError as e:
        return False, str(e)
    return True, "queued"


def core_temps():
    out = []
    for line in _read(os.path.join(DATA, "cores.tsv")).strip().split("\n"):
        bits = line.split("\t")
        if len(bits) != 2:
            continue
        try:
            out.append({"name": bits[0], "temp": round(int(bits[1]) / 1000.0, 1)})
        except ValueError:
            continue
    return out


def boot_history():
    """Parse `last -x reboot shutdown` into sessions, so uptime can be shown against downtime."""
    sessions, downs = [], []
    for line in _read(os.path.join(DATA, "boots.txt")).split("\n"):
        if not line.strip():
            continue
        kind = "boot" if line.startswith("reboot") else ("down" if line.startswith("shutdown") else None)
        if not kind:
            continue
        stamps = re.findall(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}", line)
        if not stamps:
            continue
        try:
            start = _epoch(stamps[0])
            end = _epoch(stamps[1]) if len(stamps) > 1 else None
        except ValueError:
            continue
        if "still running" in line:
            end = None
        (sessions if kind == "boot" else downs).append({"start": start, "end": end})
    sessions.sort(key=lambda x: x["start"], reverse=True)
    # wtmp marks several old boots "still running"; only the newest really is.
    # Close each older one at the next boot that followed it.
    for i, s in enumerate(sessions):
        if i > 0 and not s["end"]:
            s["end"] = sessions[i - 1]["start"]
    # all of them: the availability figure needs every boot in its window, and
    # trimming here once counted a thirteenth reboot's uptime as downtime
    return sessions


def _epoch(iso):
    from datetime import datetime
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S%z").timestamp()


def uptime_report(sessions, days=90):
    """Availability plus anything actually worth reporting: downtime and restarts."""
    if not sessions:
        return {"pct": None, "down": None, "restarts": 0, "last_boot": None}
    now = time.time()
    window = now - days * 86400
    span = now - window
    up = 0.0
    restarts = 0
    for s in sessions:
        if s["start"] >= window:
            restarts += 1
        a = max(s["start"], window)
        b = s["end"] if s["end"] else now
        if b > a:
            up += b - a
    up = min(up, span)
    return {
        "pct": round(min(100.0, up / span * 100), 2) if span > 0 else None,
        "down": max(0.0, span - up),
        "restarts": restarts,
        "last_boot": max(s["start"] for s in sessions),
    }


def pools_now():
    """Written every minute by a host cron job; the container cannot run zpool itself."""
    out = []
    raw = _read(os.path.join(DATA, "pools.tsv"))
    for line in raw.strip().split("\n"):
        bits = line.split("\t")
        if len(bits) < 5:
            continue
        name, size, alloc, free, health = bits[0], bits[1], bits[2], bits[3], bits[4]
        if name == "boot-pool":
            continue
        try:
            size, alloc, free = int(size), int(alloc), int(free)
        except ValueError:
            continue
        # Columns 6 and 7 are the root dataset's used and avail: usable space,
        # parity excluded. zpool's figures are raw, which on a 4-wide RAIDZ1
        # showed 12 TB free when 7.8 TB could actually be written. Fall back to
        # raw only for a collector too old to write them.
        try:
            zused, zavail = int(bits[5]), int(bits[6])
            if zused + zavail > 0:
                alloc, free, size = zused, zavail, zused + zavail
        except (IndexError, ValueError):
            pass
        # live health straight from the kernel, in case the file is stale
        live = _read("/proc/spl/kstat/zfs/%s/state" % name).strip()
        out.append({"name": name, "size": size, "used": alloc, "free": free,
                    "pct": round(alloc / size * 100, 1) if size else 0,
                    "health": live or health})
    age = None
    try:
        age = time.time() - float(_read(os.path.join(DATA, "pools.stamp")).strip())
    except ValueError:
        pass
    return out, age


def cpu_temp_now():
    raw = _read(os.path.join(DATA, "cputemp")).strip()
    try:
        return round(int(raw) / 1000.0, 1)
    except ValueError:
        return None


def _read_json(name, default):
    """A file the root cron job writes. Missing or half-written is normal:
    it is replaced by rename, but it does not exist at all until the first run."""
    try:
        return json.loads(_read(os.path.join(DATA, name)) or "null") or default
    except ValueError:
        return default


def procs_now():
    """Top processes by CPU and by memory, sampled on the host - the container
    has its own pid namespace and can see only itself."""
    return _read_json("procs.json", {})


def vms_now():
    """Virtual machines, written by the root cron job - the container has no
    midclt of its own."""
    return _read_json("vms.json", [])


def request_vm(vm_id, action):
    """Ask the root worker to start, stop or restart a VM.

    Validated here as well as in the worker. The worker checks the id against
    the installed list because it is the thing holding root; this check is
    only so an obvious mistake fails fast with a sensible message."""
    if action not in ("start", "stop", "restart"):
        return False, "unknown action"
    try:
        vm_id = int(vm_id)
    except (TypeError, ValueError):
        return False, "bad VM id"
    known = {v.get("id") for v in vms_now()}
    if vm_id not in known:
        return False, "no such VM"
    try:
        os.makedirs(QUEUE, exist_ok=True)
        tmp = os.path.join(QUEUE, "_vm.req.tmp")
        with open(tmp, "w") as fh:
            json.dump({"id": vm_id, "action": action}, fh)
        os.replace(tmp, os.path.join(QUEUE, "_vm.req"))
    except OSError as exc:
        return False, "could not queue it: %s" % exc
    return True, "queued"


def pool_status():
    """Scrub state and error counters, which zpool list does not carry."""
    return _read_json("poolstatus.json", {})


def drives_now():
    """Written by the root cron job: the container has no access to smartctl or raw devices."""
    out = []
    for line in _read(os.path.join(DATA, "drives.jsonl")).strip().split("\n"):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        spinning = bool(d.get("rpm"))
        t = d.get("temp")
        # spinning disks run cooler than flash and should be judged more strictly
        warn, bad = (50, 55) if spinning else (60, 70)
        level = None
        if isinstance(t, (int, float)):
            level = "bad" if t >= bad else ("warn" if t >= warn else "ok")
        d["level"] = level
        d["kind"] = "HDD" if spinning else ("NVMe" if str(d.get("dev", "")).startswith("nvme") else "SSD")
        out.append(d)
    out.sort(key=lambda x: (x["kind"] != "HDD", x.get("dev", "")))
    return out


# ---------------------------------------------------------------- background work

def vitals_loop():
    prev_agg, prev_cores = cpu_snapshot()
    prev_net, prev_t = net_snapshot(), time.time()
    prev_io = disk_snapshot()
    while True:
        time.sleep(2.0)
        # One bad reading must not end the loop. A thread that dies here is
        # silent: the page keeps serving the last figures as if they were live.
        try:
            cur_agg, cur_cores = cpu_snapshot()
            pct = _pct(prev_agg, cur_agg)
            per = []
            for name in sorted(cur_cores, key=lambda n: int(n[3:]) if n[3:].isdigit() else 0):
                v = _pct(prev_cores.get(name), cur_cores[name])
                if v is not None:
                    per.append({"name": name.replace("cpu", "CPU "), "pct": v})
            prev_agg, prev_cores = cur_agg, cur_cores

            cur_net, now_t = net_snapshot(), time.time()
            gap = now_t - prev_t
            nets = []
            for name, cur in sorted(cur_net.items()):
                old = prev_net.get(name)
                down = up = None
                if old and gap > 0:
                    down = max(0, (cur["rx"] - old["rx"]) / gap)
                    up = max(0, (cur["tx"] - old["tx"]) / gap)
                nets.append({"name": name, "rx": cur["rx"], "tx": cur["tx"],
                             "down": down, "up": up,
                             "errors": cur["rx_err"] + cur["tx_err"],
                             "speed": link_speed(name)})
            prev_net, prev_t = cur_net, now_t

            cur_io = disk_snapshot()
            ios, rd_tot, wr_tot = [], 0, 0
            for name, cur in sorted(cur_io.items()):
                old = prev_io.get(name)
                rd = wr = None
                if old and gap > 0:
                    rd = max(0, (cur[0] - old[0]) / gap)
                    wr = max(0, (cur[1] - old[1]) / gap)
                    rd_tot += rd
                    wr_tot += wr
                ios.append({"name": name, "read": rd, "write": wr})
            prev_io = cur_io

            stamp = int(now_t)
            for n in nets:
                if n["down"] is None:
                    continue
                _hist.setdefault(n["name"], deque(maxlen=HIST)).append(
                    [stamp, round(n["down"]), round(n["up"])])
            pools, age = pools_now()
            with _lock:
                if pct is not None:
                    _state["cpu"] = pct
                _state["mem"] = mem_now()
                _state["load"] = load_now()
                _state["uptime"] = uptime_now()
                _state["pools"] = pools
                _state["pools_age"] = age
                _state["cpu_temp"] = cpu_temp_now()
                _state["drives"] = drives_now()
                if per:
                    _state["cpu_cores"] = per
                _state["core_temps"] = core_temps()
                boots = boot_history()
                _state["boots"] = boots[:12]
                rep = uptime_report(boots)
                _state["uptime_info"] = rep
                _state["availability"] = rep["pct"]
                if nets:
                    _state["net"] = nets
                    _state["net_hist"] = {k: list(v) for k, v in _hist.items()}
                if ios:
                    _state["io"] = ios
                    _io_hist.append([stamp, round(rd_tot), round(wr_tot)])
                    _state["io_hist"] = list(_io_hist)
                m = _state["mem"]
                if m:
                    _mem_hist.append([stamp, m["apps"], m["arc"]])
                    _state["mem_hist"] = list(_mem_hist)

                # the wide tiles want a sparkline each, so keep a short trail for
                # the figures that previously had none
                c = _state["cpu"]
                if c is not None:
                    _cpu_hist.append([stamp, round(c, 1)])
                    _state["cpu_hist"] = list(_cpu_hist)
                ct = _state["cpu_temp"]
                if ct is not None:
                    _ctemp_hist.append([stamp, round(ct)])
                    _state["ctemp_hist"] = list(_ctemp_hist)
                pools = _state["pools"] or []
                if pools:
                    worst = max((p.get("pct") or 0) for p in pools)
                    _store_hist.append([stamp, round(worst, 1)])
                    _state["store_hist"] = list(_store_hist)

                pw = power_now()
                if pw:
                    _state["power_hist"] = pw
                    _state["power"] = pw[-1][1]
        except Exception:
            traceback.print_exc()


def installed_apps():
    try:
        with open(os.path.join(DATA, "apps.json")) as fh:
            return {a.get("name") for a in json.load(fh) if a.get("name")}
    except (OSError, ValueError, TypeError):
        return set()


def request_serve(host=""):
    """Ask the root worker to publish everything that is not published yet.

    The container cannot do it: no midclt, and on a first run the Tailscale app
    itself needs a mount and an env var. Choosing "use my tailnet" is one
    decision, so this is one request covering every unpublished app rather than
    a queue entry per tile.

    `host` is optional and normally empty. Tailscale substitutes
    ${TS_CERT_DOMAIN} in its Serve config; the root worker also reads the
    resulting node DNS name from Tailscale status and shares it with Dashy."""
    host = (host or "").strip().lower()
    if host and (not re.match(r"^[a-z0-9][a-z0-9.-]{0,252}$", host)
                 or "." not in host):
        return False, "that does not look like a tailnet name"

    # Only ports Dashy has actually discovered, so this cannot be talked into
    # publishing something arbitrary that happens to be listening.
    # Read the NAS app inventory directly: an enable click can arrive before
    # the first 20s service probe has filled the in-memory list.
    svcs = discover()
    want = sorted({int(s["lport"]) for s in svcs
                   if s.get("lport") and not s.get("port")})
    if PORT not in {int(s["lport"]) for s in svcs if s.get("lport")}:
        want.append(PORT)              # Dashy publishes no ports of its own
    want = sorted(set(want))
    try:
        os.makedirs(QUEUE, exist_ok=True)
        with open(os.path.join(QUEUE, "_serve.req"), "w") as fh:
            json.dump({"host": host, "lports": want[:60], "host_lookup": True,
                       "serve": SERVE_HOST_HINT}, fh)
    except OSError as e:
        return False, str(e)
    if not want:
        return True, "checking the Tailscale hostname"
    return True, "publishing %d app%s" % (len(want), "" if len(want) == 1 else "s")


def detect():
    """What this NAS appears to be running, so first-run setup can be a
    confirmation rather than an interrogation."""
    apps = installed_apps()
    tn = tailnet_host()
    return {"tailscale": bool(tn) or "tailscale" in apps,
            # Tailscale is here but nothing is published, which is the normal
            # state of a new install and the one worth offering to fix
            "serve_offer": (not tn) and ("tailscale" in apps),
            "hexos": "hexos" in apps}


def header_links(services, setup=None):
    """Resolve admin links separately from the discovered app tiles."""
    setup = setup or {}
    pub = serve_map()
    tn = tailnet_host()
    out = {"truenas": "https://%s/" % LINK_HOST, "hexos": None}
    for lport in (443, 80):
        tport = pub.get(lport)
        if not tport:
            continue
        if tn:
            out["truenas"] = "https://%s:%d/" % (tn, tport)
        elif lport == 80:
            out["truenas"] = "http://%s/" % LINK_HOST
        break

    # an explicit address always wins: not everyone reaches the NAS over a tailnet
    if setup.get("truenas"):
        out["truenas"] = setup["truenas"]
    # hexos has no app to discover, it is just a known address
    out["hexos"] = HEXOS_URL
    return out


def layout_path():
    return os.path.join(CONFIG, "layout.json")


_LISTS = ("order", "hidden", "wide")
_THEMES = ("system", "light", "dark")
# Must match ACCENTS in the page. When it did not, every accent added to the
# page but not to this tuple was accepted by the UI and dropped on save.
# The five current names, plus the older ones they replaced: a client that has
# not reloaded yet still sends an old one, and silently discarding it on save is
# exactly how tile layouts were lost before.
_ACCENTS = ("blue", "dusk", "harbour", "heather", "granite",
            "teal", "violet", "amber", "yellow", "rose", "green")
_prefs_cache = {"mtime": None, "data": None}


def _clean_prefs(d):
    out = {}
    for key in _LISTS:
        v = d.get(key)
        if isinstance(v, list):
            out[key] = [x for x in v[:64]
                        if isinstance(x, str) and re.match(r"^[a-z0-9_-]{1,32}$", x)]
    if d.get("theme") in _THEMES:
        out["theme"] = d["theme"]
    if d.get("accent") in _ACCENTS:
        out["accent"] = d["accent"]

    # Per-card size. 1x is the default and is stored by omission, so a card
    # only appears here when it is bigger than normal.
    sz = d.get("size")
    if isinstance(sz, dict):
        clean = {}
        for k, v in list(sz.items())[:64]:
            if not (isinstance(k, str) and re.match(r"^[a-z0-9_-]{1,32}$", k)):
                continue
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            if n in (2, 4):
                clean[k] = 2         # 4x is retired; keep the card big, not normal
        out["size"] = clean

    # Per-app launch URL overrides, kept separately for LAN and tailnet.
    # Migrate the previous single URL to both fields so existing pins retain
    # their old mode-independent behavior until edited by the owner.
    urls = d.get("urls")
    if isinstance(urls, dict):
        clean = {}
        for k, v in list(urls.items())[:128]:
            if not (isinstance(k, str)
                    and re.match(r"^[a-z0-9][a-z0-9_-]{0,31}$", k)):
                continue
            fields = {"lan": v, "ts": v} if isinstance(v, str) else v
            if not isinstance(fields, dict):
                continue
            entry = {}
            for mode in ("lan", "ts"):
                value = fields.get(mode)
                if not isinstance(value, str):
                    continue
                value = value.strip()
                if len(value) <= 300 and re.match(r"^https?://[^\s<>\"']+$", value):
                    entry[mode] = value
            if entry:
                clean[k] = entry
        out["urls"] = clean

    su = d.get("setup")
    if isinstance(su, dict):
        clean = {}
        for flag in ("done", "tailscale", "hexos"):
            if isinstance(su.get(flag), bool):
                clean[flag] = su[flag]
        # whether a tile opens its tailnet address or its address on the LAN
        if su.get("links") in ("ts", "lan"):
            clean["links"] = su["links"]
        url = su.get("truenas")
        if isinstance(url, str) and len(url) <= 200:
            url = url.strip()
            clean["truenas"] = url if re.match(r"^https?://[^\s<>\"']+$", url) else ""
        if clean:
            out["setup"] = clean
    return out


def load_prefs():
    """Shared settings: tile layout plus theme and accent. Cached on mtime so
    the 3s poll does not re-read the file for every connected device."""
    try:
        st = os.stat(layout_path())
    except OSError:
        return None
    if _prefs_cache["mtime"] == st.st_mtime_ns:
        return _prefs_cache["data"]
    try:
        with open(layout_path()) as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    out = _clean_prefs(d)
    if not out:
        return None
    # the revision lets a device notice someone else's change without
    # diffing the whole object, and tells it apart from its own echo
    out["rev"] = int(d.get("rev") or 0)
    _prefs_cache["mtime"] = st.st_mtime_ns
    _prefs_cache["data"] = out
    return out


def save_prefs(d):
    if not isinstance(d, dict):
        return False, "bad payload", 0
    clean = _clean_prefs(d)
    if not clean:
        return False, "nothing to store", 0
    prev = load_prefs() or {}
    clean["rev"] = int(prev.get("rev") or 0) + 1
    try:
        os.makedirs(CONFIG, exist_ok=True)
        tmp = layout_path() + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(clean, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, layout_path())
    except OSError as e:
        return False, "not writable: %s" % e.__class__.__name__, 0
    _prefs_cache["mtime"] = None            # force a re-read next time
    return True, "saved", clean["rev"]


# Lowest-numbered port is a poor guess at "the web interface". Pi-hole publishes
# 53 and 20720, and 53 is DNS. RustDesk publishes three relay ports and has no
# web interface at all. So rule out what is definitely not HTTP, and if an app
# has nothing left, it has no tile - which is the right answer for RustDesk.
NOT_WEB = (
    {22, 23, 25, 53, 111, 123, 135, 137, 138, 139, 161, 389, 445, 465, 514,
     587, 636, 993, 995, 1900, 2049, 3306, 3389, 5353, 5432, 6379, 11211,
     27017, 50300}
    | set(range(21115, 21120))            # RustDesk relay
)


def portal_of(app):
    """TrueNAS's own answer to "what URL opens this app", as (port, path).

    Worth more than any guess made here: it names the right port where an app
    publishes several - Scrutiny's "Web UI" against its InfluxDB - and it
    carries the path, so Pi-hole lands on /admin/ and Plex on /web instead of
    a bare port that redirects at best. Empty for custom apps, which declare
    no portals at all, and for apps with no web interface."""
    portals = app.get("portals") or {}
    if not portals:
        return None, ""
    key = next((k for k in portals
                if k.strip().lower() in ("web ui", "webui", "web", "gui")), None)
    url = portals[key] if key else list(portals.values())[0]
    try:
        u = urllib.parse.urlsplit(url)
        port = u.port
    except ValueError:
        return None, ""
    if not port:
        return None, ""
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    return int(port), path


def serve_map():
    """Local port -> tailnet port, from whatever Tailscale Serve publishes.
    An address book, not a census."""
    try:
        with open(SERVE) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return {}
    pub = {}
    for host, entry in (cfg.get("Web") or {}).items():
        try:
            tport = int(host.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            continue
        handlers = entry.get("Handlers") or {}
        target = (list(handlers.values())[0] or {}).get("Proxy", "") if handlers else ""
        try:
            pub[int(target.rsplit(":", 1)[1])] = tport
        except (IndexError, ValueError):
            continue
    return pub


def discover():
    """Every installed app, plus whatever Serve says about how to reach it.

    Serve used to BE the list, which meant publishing one app hid all the
    others - install Dashy on a fresh Tailscale setup and the dashboard showed
    exactly one tile, itself. Serve answers "what address", not "what exists".
    The app list comes from the NAS; anything you do not want, hide."""
    pub = serve_map()
    try:
        with open(os.path.join(DATA, "apps.json")) as fh:
            apps = json.load(fh)
    except (OSError, ValueError):
        apps = []

    out = []
    for a in apps:
        app_name = a.get("name") if isinstance(a, dict) else None
        if not isinstance(app_name, str) or not app_name.strip():
            continue
        ports = sorted(p for p in (a.get("ports") or [])
                       if isinstance(p, int) and 0 < p < 65536)
        try:
            pport, ppath = portal_of(a)
        except (AttributeError, TypeError):
            pport, ppath = None, ""

        # In order of how much the answer is worth: the app's own portal when
        # Tailscale also publishes it, then anything Tailscale publishes (the
        # owner saying which port matters), then the portal, then a guess.
        order = []
        if pport and pport in pub:
            order.append(pport)
        order += [p for p in ports if p in pub]
        if pport:
            order.append(pport)
        order += [p for p in ports if p not in NOT_WEB]
        chosen = next((p for p in order if p), None)
        # Dashy is a TrueNAS-installed app too, but host networking means
        # TrueNAS reports no published container ports for it. Its listener is
        # this process's configured PORT, so discover that one app from the
        # installed-app inventory without resurrecting arbitrary port tiles.
        me = is_self(app_name)
        if chosen is None and me:
            chosen, ppath = PORT, "/"
        if chosen is None:
            continue                 # host networking, or nothing web-facing
        # The catalogue's own title and one-line description. A custom app
        # has neither, so it is titled from its name on the NAS.
        if me:
            label, desc = APP_NAME, "This page"
        else:
            label = str(a.get("title") or "").strip() or app_name.replace("-", " ").title()
            desc = str(a.get("desc") or "").strip()
        out.append({"port": pub.get(chosen), "lport": chosen,
                    "path": ppath if chosen == pport else "/",
                    "name": label, "desc": desc,
                    "state": a.get("state"), "app": app_name,
                    "id": app_name,
                    "version": APP_VERSION if me else a.get("version"),
                    "update": bool(a.get("update")),
                    # Dashy is a custom app too, but it knows its own version
                    "custom": bool(a.get("custom")) and not me,
                    "iconsrc": ("/appicon/" + a["name"]) if a.get("icon") else None})

    # Only installed, web-facing TrueNAS apps appear here. The NAS admin UI and
    # Dashy itself have separate header/browser links, never pseudo-app tiles.
    out.sort(key=lambda x: (x["port"] is None, x["port"] or 0,
                            (x["name"] or "").lower()))
    return out


def probe(port):
    """Is something answering on the host at this port? Any HTTP reply counts as up.

    The time it takes to answer is returned alongside the code: it is the one
    live figure that is genuinely per-app, and a service creeping from 20ms to
    two seconds is worth seeing before it stops answering at all."""
    url = "http://%s:%d/" % (HOST_IP, port)
    req = urllib.request.Request(url, method="GET")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            return r.status, round((time.monotonic() - t0) * 1000)
    except urllib.error.HTTPError as e:
        return e.code, round((time.monotonic() - t0) * 1000)  # 401/403 is still alive
    except (urllib.error.URLError, socket.timeout, OSError, ValueError):
        return 0, None


def services_loop():
    while True:
        try:
            found = []
            over = (load_prefs() or {}).get("urls") or {}
            tn = tailnet_host()
            for svc in discover():
                svc = dict(svc)
                code, ms = probe(svc["lport"])
                svc["code"] = code
                svc["rt_ms"] = ms
                path = svc.get("path") or "/"
                # Keep discovered LAN/tailnet URLs and the corresponding overrides
                # separate. The owner can correct either mode without affecting the
                # other mode or relying on a guessed published port.
                auto_lan = "http://%s:%d%s" % (LINK_HOST, svc["lport"], path)
                auto_ts = ("https://%s:%d%s" % (tn, svc["port"], path)
                           if (tn and svc.get("port")) else None)
                entry = over.get(svc.get("id") or "") or {}
                if isinstance(entry, str):       # defensive migration for old prefs
                    entry = {"lan": entry, "ts": entry}
                svc["url_lan_auto"] = auto_lan
                svc["url_ts_auto"] = auto_ts
                svc["url_lan"] = entry.get("lan") or auto_lan
                svc["url_ts"] = entry.get("ts") or auto_ts
                svc["url"] = svc["url_ts"] or svc["url_lan"]
                # Latency trail, one sample per pass. Only a reachable reply is a
                # figure worth plotting; a down app must not add a zero that would
                # read as "fast".
                stamp = int(time.time())
                if ms is not None:
                    d = _app_hist.setdefault(svc["id"], deque(maxlen=APP_HIST))
                    d.append([stamp, ms])
                svc["rt_hist"] = list(_app_hist.get(svc["id"]) or [])
                found.append(svc)
            with _lock:
                _state["services"] = found
        except Exception:
            traceback.print_exc()
        time.sleep(20)


def snapshot():
    with _lock:
        svc = list(_state["services"])
        out = {k: _state[k] for k in
               ("cpu", "cores", "mem", "load", "uptime", "pools", "pools_age",
                "cpu_temp", "drives", "cpu_cores", "core_temps",
                "boots", "availability", "uptime_info", "net", "net_hist",
                "io", "io_hist", "power", "power_hist", "mem_hist",
                "cpu_hist", "ctemp_hist", "store_hist", "host")}
    out["version"] = APP_VERSION
    out["app"] = APP_NAME
    tn = tailnet_host()
    out["tailnet"] = tn
    out["host"] = _hostname()
    out["system"] = system_info()
    out["services"] = svc
    out["queued"] = queued()
    out["updates"] = update_status()
    out["prefs"] = load_prefs()
    out["detected"] = detect()
    out["selfupdate"] = self_update()
    out["procs"] = procs_now()
    out["poolstatus"] = pool_status()
    out["vms"] = vms_now()
    out["links"] = header_links(svc, (out["prefs"] or {}).get("setup"))
    return out


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#f5f6f8" id="tc">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Dashy">
<link rel="icon" href="/favicon.png?v=ICON_VERSION" sizes="32x32" type="image/png">
<link rel="apple-touch-icon" href="/icon-180.png?v=ICON_VERSION">
<link rel="manifest" href="/manifest.webmanifest?v=ICON_VERSION">
<title>Dashy</title>
<style>
  :root{
    color-scheme:light dark;
    --bg:#eaeeff; --panel:#ffffff; --surf:#fbfcff; --brd:rgba(16,20,28,.09);
    --ink:#0f1218; --muted:#6a7280; --faint:rgba(16,20,28,.06);
    --ok:#1fa971; --warn:#e0a32e; --bad:#e0443a;
    --accent:#4a57c8; --brand:#4a57c8;
    --shadow:0 1px 2px rgba(16,20,28,.04), 0 12px 30px -14px rgba(16,20,28,.20);
    /* M3's shape scale rather than one radius everywhere. Mixing round and
       square is the point: uniform corners read as a template, varied ones
       read as a designed thing. */
    --r-xs:8px; --r-sm:12px; --r-md:16px; --r-lg:22px; --r-xl:28px;
    --r-full:999px;
    --r:var(--r-md);
    /* every tile, home and settings: a hairline outline and a soft drop */
    --tile-shadow:0 8px 24px -21px rgba(0,0,0,.45);
  }
  /* dark palette twice: once for "follow the system" (suppressed when the user
     has explicitly chosen light), once for an explicit dark choice */
  @media (prefers-color-scheme:dark){
    :root:not([data-theme="light"]){
      --bg:#0f111b; --panel:#171b22; --surf:#1b1e2b; --brd:rgba(255,255,255,.09);
      --ink:#eef1f5; --muted:#949cab; --faint:rgba(255,255,255,.06);
      --ok:#34d399; --warn:#fbbf24; --bad:#f87171;
      --accent:#7d87e6; --brand:#7d87e6;
      --shadow:0 1px 2px rgba(0,0,0,.4), 0 18px 42px -18px rgba(0,0,0,.7);
      color-scheme:dark;
    }
  }
  :root[data-theme="dark"]{
      --bg:#0f111b; --panel:#171b22; --surf:#1b1e2b; --brd:rgba(255,255,255,.09);
      --ink:#eef1f5; --muted:#949cab; --faint:rgba(255,255,255,.06);
      --ok:#34d399; --warn:#fbbf24; --bad:#f87171;
      --accent:#7d87e6; --brand:#7d87e6;
      --shadow:0 1px 2px rgba(0,0,0,.4), 0 18px 42px -18px rgba(0,0,0,.7);
      color-scheme:dark;
  }
  :root[data-theme="light"]{color-scheme:light}
  /* kills the double-tap-to-zoom gesture without touching pinch-zoom, which
     user-scalable=no would have taken away too */
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent;
    touch-action:manipulation}
  /* A soft wash of the chosen accent. Fixed and behind everything, so it never
     repaints on scroll, and it follows --brand automatically when the accent
     changes. Lighter in light mode, deeper and cooler in dark. */
  html{background:var(--bg);min-height:100%}
  body{
    margin:0;background:var(--bg);color:var(--ink);
    padding:calc(env(safe-area-inset-top,0px) + 18px) 0 calc(env(safe-area-inset-bottom,0px) + 28px);
    font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,system-ui,sans-serif;
    -webkit-font-smoothing:antialiased;-webkit-text-size-adjust:100%;
  }
  .wrap{width:100%;max-width:1180px;margin:0 auto;padding:0 clamp(14px,3vw,28px)}

  /* a single row at every width; the title column is what gives if it gets tight */
  header{display:grid;align-items:center;gap:14px;margin-bottom:20px;
         grid-template-columns:minmax(0,1fr) auto}
  .id{display:flex;align-items:center;gap:11px;min-width:0}
  .id h1{font-size:clamp(20px,4.4vw,26px);margin:0;letter-spacing:-.02em;font-weight:700;
         display:flex;align-items:baseline;gap:8px;min-width:0}
  .id h1 .host{font-size:12.5px;font-weight:600;color:var(--muted);letter-spacing:0}
  .id p{margin:1px 0 0;font-size:12.5px;color:var(--muted);
        overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .mark{width:34px;height:34px;flex:0 0 auto;border-radius:9px;
        box-shadow:0 4px 12px -5px var(--brand)}

  .hdr-r{display:flex;align-items:center;gap:8px;justify-content:flex-end}
  .ibtn{width:34px;height:34px;flex:0 0 auto;display:inline-flex;align-items:center;
        justify-content:center;border-radius:var(--r-full);background:var(--faint);
        border:1px solid var(--brd);color:var(--muted);text-decoration:none;
        transition:color .15s ease,border-color .15s ease,transform .15s ease}
  .ibtn:hover{color:var(--ink);border-color:var(--muted);transform:translateY(-1px)}
  .ibtn svg{width:17px;height:17px;display:block}


  /* ---- full-page settings --------------------------------------------- */
  .settings-page{padding:5px 0 32px;animation:settingsin .22s ease both}
  @keyframes settingsin{from{opacity:.35;transform:translateY(5px)}to{opacity:1;transform:none}}
  .settings-head{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;
                 padding:4px 0 22px;border-bottom:1px solid var(--brd)}
  .settings-kicker{margin:0 0 5px;color:var(--accent);font-size:10px;font-weight:750;
                   letter-spacing:.13em;text-transform:uppercase}
  .settings-head h2{margin:0;font-size:clamp(23px,4vw,30px);letter-spacing:-.025em}
  .settings-head p:last-child{margin:4px 0 0;color:var(--muted);font-size:13px}
  .settings-back{appearance:none;border:1px solid var(--brd);border-radius:var(--r-full);
                 background:var(--surf);color:var(--ink);padding:9px 14px;font:inherit;
                 font-size:12.5px;font-weight:650;cursor:pointer;white-space:nowrap}
  .settings-back:hover{border-color:var(--accent);color:var(--accent)}
  .settings-layout{display:grid;grid-template-columns:174px minmax(0,1fr);gap:24px;
                   align-items:start;padding-top:20px}
  .settings-nav{position:sticky;top:14px;display:flex;flex-direction:column;gap:4px}
  .settings-nav button{appearance:none;border:0;border-radius:10px;background:transparent;
                       color:var(--muted);text-align:left;padding:10px 12px;font:inherit;
                       font-size:13px;font-weight:650;cursor:pointer}
  .settings-nav button:hover{background:var(--faint);color:var(--ink)}
  .settings-nav button.active{background:color-mix(in srgb,var(--accent) 14%,var(--panel));
                              color:var(--accent)}
  .settings-content{min-width:0}
  .settings-section{scroll-margin-top:18px;margin:0 0 28px}
  .settings-section>h3{margin:0 0 12px;font-size:17px;letter-spacing:-.01em}
  .settings-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,250px),1fr));
                 gap:12px}
  .settings-card{min-width:0;padding:17px;border:1px solid var(--brd);border-radius:16px;
                  background:var(--panel);box-shadow:var(--tile-shadow)}
  .settings-glyph{width:32px;height:32px;display:grid;place-items:center;border-radius:50%;
                  margin-bottom:12px;background:color-mix(in srgb,var(--accent) 18%,var(--panel));
                  color:var(--accent);font-size:18px;font-weight:750}
  .settings-card h4{margin:0;font-size:14px;font-weight:700}
  .settings-card>p{min-height:34px;margin:4px 0 12px;color:var(--muted);font-size:12px;line-height:1.4}
  .settings-card .mi{margin-top:7px;padding:9px 10px;border:1px solid var(--brd);
                     border-radius:10px;background:var(--faint);justify-content:space-between}
  .settings-card .mi:hover{background:color-mix(in srgb,var(--accent) 8%,var(--faint))}
  .settings-card .seg{margin:5px 0 0}
  .settings-card .sw{padding:8px 0 0;grid-template-columns:repeat(4,minmax(46px,1fr))}
  .settings-card .mrow{margin-top:12px;padding:9px 0 0;border-top:1px solid var(--faint)}
  .settings-card .pubmsg{margin-top:6px}
  .settings-card .fld.sm{margin-top:4px}
  .settings-version{display:flex;align-items:center;justify-content:space-between;gap:8px;
                    margin-top:10px;color:var(--muted);font-size:11px}
  .settings-links{display:flex;gap:8px;margin-top:8px}
  .settings-links .mi{flex:1;justify-content:center}
  .settings-note{margin-top:12px;padding:12px 14px;border-radius:11px;background:var(--faint);
                 color:var(--muted);font-size:12px}
  @media(max-width:680px){
    .settings-head{align-items:flex-start;padding-bottom:16px}
    .settings-back{padding:8px 10px;font-size:11px}
    .settings-layout{grid-template-columns:minmax(0,1fr);gap:14px;padding-top:14px}
    .settings-nav{position:sticky;top:0;z-index:5;flex-direction:row;overflow-x:auto;
                  padding:5px 0;background:var(--bg);scrollbar-width:none}
    .settings-nav::-webkit-scrollbar{display:none}
    .settings-nav button{flex:0 0 auto;padding:8px 11px;font-size:12px}
  }

  /* shared settings controls */
  .mh{font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);
      font-weight:700;padding:9px 10px 5px}
  .mi{display:flex;align-items:center;gap:10px;width:100%;padding:9px 10px;border-radius:10px;
      background:none;border:none;color:var(--ink);font:inherit;font-size:13.5px;
      font-weight:600;cursor:pointer;text-align:left;text-decoration:none;
      transition:background .13s ease}
  .mi:hover{background:var(--faint)}
  /* only an update actually waiting earns the warning colour; the rest of
     the time the row is present but plainly doing nothing */
  #mi-self[data-state="ready"]{color:var(--warn)}
  #mi-self[data-state="checking"]{opacity:.6;pointer-events:none}
  .opt.col{display:block}
  .opt.col .seg{margin-top:10px}
  .seg.sm{margin-top:8px}
  .seg.sm button{padding:6px 10px;font-size:12.5px}
  .pubmsg{display:block;margin-top:7px;font-size:12px;color:var(--muted)}
  .pubmsg.bad{color:var(--warn)}
  /* update state */
  @keyframes spin{to{transform:rotate(360deg)}}
  /* a spinner says "busy"; three dots in sequence say "working through it",
     which is what an update actually is */
  .dots{display:inline-flex;align-items:center;justify-content:center;
        gap:3px;width:16px;height:16px;flex:0 0 auto;align-self:center}
  .dots i{width:4px;height:4px;border-radius:50%;background:currentColor;
          opacity:.22;animation:blink 1.2s ease-in-out infinite}
  .dots i:nth-child(2){animation-delay:.2s}
  .dots i:nth-child(3){animation-delay:.4s}
  @keyframes blink{0%,66%,100%{opacity:.22}33%{opacity:1}}
  @media (prefers-reduced-motion:reduce){ .dots i{animation:none;opacity:.6} }
  #settings.dot{position:relative}
  #settings.dot::after{content:'';position:absolute;top:5px;right:5px;width:7px;height:7px;
      border-radius:50%;background:var(--warn);box-shadow:0 0 0 2px var(--card)}
  #mi-self[data-state="ready"] svg{color:var(--warn)}
  #mi-self[data-state="running"]{opacity:.6;pointer-events:none}
  .mi svg{width:16px;height:16px;flex:0 0 auto;color:var(--muted)}
  .mi .rt{margin-left:auto;font-size:12px;color:var(--muted);font-weight:600;
          display:inline-flex;align-items:center;gap:6px}
  .mi .rt .pdot{width:7px;height:7px;border-radius:50%;display:block}
  .msep{height:1px;background:var(--brd);margin:6px 8px}
  .mrow{display:flex;align-items:center;gap:10px;padding:7px 10px}
  .mrow .ml{font-size:13.5px;font-weight:600}
  .mrow.nas{display:block;padding-bottom:10px}
  .mrow .mhint{display:block;font-size:11px;color:var(--muted);margin-top:2px}
  .opt .txt code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;
                 background:var(--faint);padding:1px 4px;border-radius:4px}
  .sw2.sm{width:38px;height:22px;margin-left:auto}
  .sw2.sm i{width:16px;height:16px}
  .sw2.sm[aria-checked="true"] i{transform:translateX(16px)}
  .fld.sm{width:100%;margin-top:6px;padding:7px 9px;border-radius:9px;
          border:1px solid var(--brd);background:var(--faint);color:var(--ink);
          font:inherit;font-size:12.5px}

  /* three-way theme switch */
  .seg{display:flex;gap:4px;padding:4px;margin:2px 8px 6px;background:var(--faint);
       border-radius:11px}
  .seg button{flex:1;padding:7px 4px;border:none;border-radius:8px;background:none;
              color:var(--muted);font:inherit;font-size:12px;font-weight:650;cursor:pointer;
              transition:background .15s ease,color .15s ease}
  .seg button[aria-pressed="true"]{background:var(--panel);color:var(--ink);
                                   box-shadow:0 1px 3px rgba(0,0,0,.12)}

  /* Four named colours rather than seven anonymous dots: the name is what
     makes a choice repeatable when you come back to it a month later. */
  .sw{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;
      justify-items:center;padding:4px 10px 12px}
  .swatch{display:flex;flex-direction:column;align-items:center;gap:6px}
  .sw button{width:26px;height:26px;border-radius:50%;border:2px solid transparent;
             cursor:pointer;padding:0;transition:transform .14s ease}
  .sw button:hover{transform:scale(1.12)}
  .sw button[aria-pressed="true"]{border-color:var(--ink);
                                  box-shadow:0 0 0 2px var(--panel) inset}
  .swname{font-size:10.5px;font-weight:600;color:var(--muted);letter-spacing:.01em}
  .swatch:has(button[aria-pressed="true"]) .swname{color:var(--ink)}

  /* ---- rearrange mode -------------------------------------------------- */
  /* iOS-style wobble. Two keyframe sets and a per-tile delay so the tiles are
     out of phase - a grid of identically-timed wobbles reads as one object. */
  @keyframes jigA{0%,100%{transform:rotate(-.7deg)}50%{transform:rotate(.7deg)}}
  @keyframes jigB{0%,100%{transform:rotate(.7deg)}50%{transform:rotate(-.7deg)}}
  body.editing .cardwrap{touch-action:none}
  /* The wobble lives on .jig, which holds the card AND its corner badge, so the
     badge travels with the tile. The wrapper above it stays untouched for the
     drag animation - a running animation beats an inline transform, so those
     two can never share an element. */
  .jig{position:relative;display:flex;width:100%}
  body.editing .cardwrap > .jig{animation:jigA .28s ease-in-out infinite;
                                transform-origin:50% 50%}
  body.editing .cardwrap:nth-child(2n) > .jig{animation-name:jigB}
  body.editing .cardwrap:nth-child(3n) > .jig{animation-delay:-.09s}
  body.editing .cardwrap:nth-child(5n) > .jig{animation-delay:-.17s}
  @media (prefers-reduced-motion:reduce){
    body.editing .cardwrap > .jig{animation:none}
    .cardwrap{transition:none !important}
  }
  .card.ghost{opacity:.45}
  body.editing .card{cursor:default}
  /* a <button> may not contain a <button>; the parser would hoist the tools out
     of the card, so each card sits in a positioned wrapper instead */
  /* A tile is a control, not prose. Dragging one used to paint a selection
     across whatever text sat behind it, and a long press raised the system
     callout menu on iOS. */
  .cardwrap{position:relative;display:flex;
            -webkit-user-select:none;user-select:none;
            -webkit-touch-callout:none}
  .cardwrap .card{width:100%}
  /* the overlay covers the card, so it carries the card's name: without it a
     page of identical size buttons left you guessing which tile was which */
  .cardtools{position:absolute;inset:0;display:none;flex-direction:column;
             align-items:center;justify-content:center;
             gap:8px;background:color-mix(in srgb,var(--panel) 97%,transparent);
             border-radius:var(--r-md);z-index:3}
  body.editing .cardtools{display:flex}
  .cardtools button{width:30px;height:30px;border-radius:9px;border:1px solid var(--brd);
                    background:var(--bg);color:var(--ink);font-size:14px;line-height:1;
                    cursor:pointer;display:flex;align-items:center;justify-content:center;
                    padding:0}
  .cardtools button:disabled{opacity:.3;cursor:default}
  .cardtools .wbtn{font-size:11px;font-weight:700;letter-spacing:-.02em}
  .ctname{font-size:13px;font-weight:650;color:var(--ink);max-width:88%;text-align:center;
          overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  /* The corner badge you get on a phone home screen: a white disc with a dark
     glyph, hanging off the top-left corner. x removes a tile, + puts a removed
     one back. It sits above the tools overlay, which covers the whole card. */
  .xbtn{position:absolute;top:-7px;left:-7px;width:23px;height:23px;border-radius:50%;
        border:none;padding:0;display:none;align-items:center;justify-content:center;
        background:#fff;color:#1c1f24;font-size:15px;line-height:1;font-weight:500;
        cursor:pointer;z-index:6;
        box-shadow:0 1px 3px rgba(0,0,0,.34),0 0 0 .5px rgba(0,0,0,.12)}
  body.editing .xbtn{display:flex}
  .cardwrap[data-off="1"] .xbtn{color:var(--ok);font-weight:700}
  .cardwrap[data-off="1"] .card,
  .cardwrap[data-off="1"] .cardtools{opacity:.34}
  body:not(.editing) .cardwrap[data-off="1"]{display:none}
  .editbar{display:none;align-items:center;gap:10px;margin:0 0 12px;padding:11px 14px;
           background:var(--panel);border:1px solid var(--brd);border-radius:14px;
           font-size:13px;font-weight:600;box-shadow:var(--shadow)}
  body.editing .editbar{display:flex}
  .editbar .done{margin-left:auto;padding:9px 18px;border-radius:var(--r-full);border:none;
                 background:var(--brand);color:#fff;font:inherit;font-weight:700;cursor:pointer}
  .editbar .rst{padding:9px 16px;border-radius:var(--r-full);border:1px solid var(--brd);
                background:none;color:var(--muted);font:inherit;font-weight:600;cursor:pointer}


  /* ---- press feedback -------------------------------------------------
     One rule for every touch target. transform only, so it composites and
     cannot trigger layout; .09s in, a slower ease back out.              */
  .card,.apptile,.ibtn,.mi,.x,.seg button,.sw button,
  .cardtools button,.editbar button,.ubtn{
    transition:transform .16s cubic-bezier(.22,1,.36,1),
               background .13s ease, border-color .15s ease, color .15s ease, opacity .15s ease}
  .card:active,.apptile:active,.mi:active,.x:active,.seg button:active,
  .cardtools button:active,.editbar button:active,.ubtn:active{
    transform:scale(.967);transition-duration:.09s}
  .ibtn:active{transform:scale(.94);transition-duration:.09s}
  .sw button:active{transform:scale(.88);transition-duration:.09s}
  .apptile .upd:active{transform:scale(.88);transition-duration:.09s}
  @media (prefers-reduced-motion:reduce){
    .card:active,.apptile:active,.mi:active,.x:active,.seg button:active,
    .cardtools button:active,.editbar button:active,.ubtn:active,
    .ibtn:active,.sw button:active{transform:none}
  }

  /* a wide tile spans two columns; at phone width that is the full row */
  .cardwrap[data-w="2"]{grid-column:span 2}
  .spark{margin-top:0;height:20px;width:100%;display:block;overflow:visible}
  .cardwrap[data-w="2"] .spark{height:40px}
  /* Inset enough to clear the card's corner radius: drawn flush, the curve
     gets clipped at both ends and reads as a rendering fault. */
  .card .sparkwrap{margin-top:auto;padding-top:10px;padding-bottom:3px;
                   margin-left:-2px;margin-right:-2px}
  .cardwrap[data-w="2"] .card{min-height:168px}


  /* ---- first-run setup ------------------------------------------------- */
  .setup{position:fixed;inset:0;z-index:90;display:flex;align-items:center;
         justify-content:center;padding:20px;background:var(--bg);
         overflow-y:auto}
  .setup[hidden]{display:none}
  .setupcard{width:100%;max-width:430px;background:var(--panel);
             border:1px solid var(--brd);border-radius:20px;box-shadow:var(--shadow);
             padding:24px 22px 18px;animation:pop .34s cubic-bezier(.22,1,.36,1) both}
  .setupcard h2{margin:14px 0 6px;font-size:21px;letter-spacing:-.02em}
  .setupcard .lede{margin:0 0 18px;font-size:13.5px;color:var(--muted);line-height:1.5}
  .setupcard .mark{width:46px;height:46px;border-radius:12px}
  .feat{display:flex;gap:11px;align-items:flex-start;margin:0 0 11px;font-size:13px;
        line-height:1.45}
  .feat b{font-weight:650}
  .feat .ic{flex:0 0 auto;width:17px;height:17px;color:var(--brand);margin-top:1px}
  .feat span{color:var(--muted)}
  .opt{display:flex;align-items:center;gap:12px;padding:11px 2px;
       border-top:1px solid var(--brd)}
  .opt .txt{min-width:0}
  .opt .txt b{display:flex;align-items:baseline;font-size:13.5px;font-weight:650}
  /* direct child only: the detected badge is a span too, and must stay inline */
  .opt .txt > span{display:block;font-size:11.5px;color:var(--muted);margin-top:1px}
  .opt .auto{font-size:10px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;
             color:var(--ok);margin-left:6px}
  .sw2{margin-left:auto;flex:0 0 auto;width:44px;height:26px;border-radius:999px;
       background:var(--faint);border:1px solid var(--brd);position:relative;cursor:pointer;
       transition:background .18s ease,border-color .18s ease}
  .sw2:disabled{opacity:.45;cursor:not-allowed}
  .sw2 i{position:absolute;top:2px;left:2px;width:20px;height:20px;border-radius:50%;
         background:var(--panel);box-shadow:0 1px 3px rgba(0,0,0,.25);
         transition:transform .18s cubic-bezier(.22,1,.36,1)}
  .sw2[aria-checked="true"]{background:var(--brand);border-color:var(--brand)}
  .sw2[aria-checked="true"] i{transform:translateX(18px);background:#fff}
  /* per-app launch URL editor, opened from a tile's detail sheet */
  .urlrow{margin-top:4px;display:flex;flex-direction:column;gap:6px}
  .urlrow .fld2{width:100%;padding:9px 11px;border-radius:10px;border:1px solid var(--brd);
                background:var(--faint);color:var(--ink);font:inherit;font-size:13px}
  .urlfield{display:flex;flex-direction:column;gap:4px;font-size:11.5px;font-weight:650;color:var(--muted)}
  .urlrow .fld2:focus{outline:2px solid color-mix(in srgb,var(--accent) 45%,transparent);
                      outline-offset:1px;border-color:var(--accent)}
  .urlrow .uhint{font-size:11.5px;color:var(--muted)}
  .urlrow .ubtns{display:flex;gap:8px;margin-top:2px}
  .urlrow .ubtns button{flex:1 1 auto}
  .urlrow .ubtns .clr{background:transparent;border:1px solid var(--brd);color:var(--ink)}
  .setup .fld{width:100%;margin-top:8px;padding:9px 11px;border-radius:10px;
              border:1px solid var(--brd);background:var(--faint);color:var(--ink);
              font:inherit;font-size:13px}
  .setup .go{width:100%;margin-top:16px;padding:12px;border-radius:12px;border:none;
             background:var(--brand);color:#fff;font:inherit;font-size:14.5px;
             font-weight:700;cursor:pointer}
  .setup .hint2{margin:8px 2px 2px;font-size:11.5px;color:var(--muted)}
  .dots{display:flex;gap:6px;justify-content:center;margin:18px 0 0}
  .dots i{width:6px;height:6px;border-radius:50%;background:var(--brd);display:block;
          transition:background .2s ease,transform .2s ease}
  .dots i.on{background:var(--brand);transform:scale(1.25)}
  /* top-right: the logo owns the left corner */
  .setup .back{position:absolute;top:18px;right:16px;background:none;border:none;
               color:var(--muted);font:inherit;font-size:13px;font-weight:600;
               cursor:pointer;padding:6px 8px;border-radius:8px}
  .setup .back:hover{background:var(--faint);color:var(--ink)}
  .setupcard{position:relative}
  .setup .skip{display:block;width:100%;margin-top:8px;padding:8px;background:none;
               border:none;color:var(--muted);font:inherit;font-size:12.5px;cursor:pointer}

  /* Structure comes from grouping, not from size. A section heading costs
     eleven pixels and does more than any amount of typographic shouting. */
  .sec{margin-bottom:22px}
  .sec h2{font-size:11px;font-weight:700;letter-spacing:.085em;text-transform:uppercase;
          color:var(--muted);margin:0 0 9px 2px}
  .grid{display:grid;gap:9px;grid-template-columns:repeat(auto-fill,minmax(168px,1fr));
        grid-auto-rows:minmax(84px,auto)}

  /* Outlined, with a soft shadow - the same tile as the settings page, so
     the two screens read as one app. The outline is also what the hover and
     warn/bad states recolour. */
  .card{position:relative;background:var(--panel);border:1px solid var(--brd);
        box-shadow:var(--tile-shadow);
        border-radius:var(--r-md);padding:14px 15px;
        min-height:92px;display:flex;flex-direction:column;align-items:stretch}
  button.card{width:100%;text-align:left;font:inherit;color:inherit;
              cursor:pointer;appearance:none;
              transition:transform .15s ease, border-color .15s ease}
  /* The alarm is the card, not the digit. A red number inside an identical
     white box does not read as "this one needs you". */
  .cardwrap[data-state="warn"] .card{
    background:color-mix(in srgb,var(--warn) 6%,var(--panel))}
  .cardwrap[data-state="bad"] .card{border-color:color-mix(in srgb,var(--bad) 50%,var(--brd));
    background:color-mix(in srgb,var(--bad) 7%,var(--panel))}
  button.card:hover{border-color:color-mix(in srgb,var(--accent) 45%,var(--brd))}
  button.card::after{content:"";}
  button.card:hover::after{opacity:1;transform:translateX(2px)}

  /* Sentence case, not shouting caps, and no giant number. A card that is
     three times the size of its neighbour claims to be three times as
     important, which was never true of CPU against a pool at 90%. */
  .hcard .hv{font-size:14px;font-weight:620;letter-spacing:-.01em;margin:4px 0 0;
             line-height:1.3;text-align:left}
  .hcard .hfoot{font-size:11px;color:var(--muted);margin:auto 0 0;padding-top:6px;text-align:left}
  /* The checklist: one line per part of the machine, so "all normal" shows
     its working. Status colour always comes with a glyph (tick, !, x), never
     colour alone. Too cramped at 1x, so it starts at 2x, where it replaces
     "everything else normal". */
  .hlist{display:none}
  .cardwrap[data-w="2"] .hlist{
    display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px 14px;margin-top:12px}
  .cardwrap[data-w="2"] #s-health{display:none}
  .hrow{display:flex;align-items:center;gap:7px;min-width:0;font-size:11.5px;text-align:left}
  .hrow b{font-weight:650;color:var(--ink);flex:0 0 auto}
  .hrow span{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .hrow i{width:15px;height:15px;flex:0 0 auto;border-radius:50%;display:grid;place-items:center;
          font-style:normal;font-size:9.5px;font-weight:800;line-height:1;color:#fff;background:var(--ok)}
  .hrow[data-level="warn"] i{background:var(--warn)}
  .hrow[data-level="bad"] i{background:var(--bad)}
  .hrow[data-level="info"] i{background:var(--faint);color:var(--muted)}
  .hrow[data-level="warn"] span{color:var(--warn)}
  .hrow[data-level="bad"] span{color:var(--bad)}
  .cardwrap[data-state="warn"] .hcard .hv{color:var(--warn)}
  .cardwrap[data-state="bad"] .hcard .hv{color:var(--bad)}
  /* lost contact: every reading is frozen, so stop them looking live */
  body.stale #vitals .cardwrap:not([data-k="health"]),body.stale .hlist,
  body.stale .appgrid{opacity:.45;filter:saturate(.25)}
  body.stale .cardwrap[data-w] #s-health{display:block}
  .lbl{font-size:11px;letter-spacing:.02em;color:var(--muted);
       font-weight:600;margin:0;text-align:left;align-self:flex-start}
  .vmico{width:22px;height:22px;border-radius:6px;flex:0 0 auto;color:#fff;
         display:inline-flex;align-items:center;justify-content:center}
  .vmico svg{width:13px;height:13px}
  .vmico.lt{color:#16181d}
  .vmlbl{display:flex;align-items:center;gap:7px}
  .vmlbl span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .big{font-size:19px;font-weight:620;letter-spacing:-.015em;margin:3px 0 0;
       font-variant-numeric:tabular-nums;line-height:1.2}
  .big small{font-size:12px;font-weight:500;color:var(--muted);margin-left:2px;letter-spacing:0}
  .sub{margin:1px 0 0;font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums;text-align:left}

  /* margin-top:auto pins the bar to the bottom but guarantees nothing above
     it, so the wrapper keeps the pin and sets a floor on the gap */
  .meter.sm{height:3px}
  .meter i{display:block;height:100%;border-radius:2px;background:var(--accent);width:0;
           transition:width .7s cubic-bezier(.4,0,.2,1), background .4s ease}
  .meter i[data-level="warn"]{background:var(--warn)}
  .meter i[data-level="bad"]{background:var(--bad)}
  .meter i[data-level="good"]{background:var(--ok)}
  #m-net{transition:width .45s linear, background .45s linear}
  .dir{font-weight:700;margin-right:3px}
  .dir.dn{color:var(--accent)}
  .dir.up{color:var(--brand)}
  #s-net .dir{font-size:13px}

  .prow{margin-top:9px;overflow:hidden}
  .prow:first-child{margin-top:2px}
  .prow .pn{font-size:12.5px;font-weight:650}
  .prow .pf{font-size:12px;color:var(--muted);float:right;font-variant-numeric:tabular-nums}

  .bigrow{display:flex;align-items:center;gap:9px}
  .pdot{width:10px;height:10px;border-radius:50%;flex:0 0 auto;display:block;
        animation:breathe 2s ease-in-out infinite}
  .pdot.ok{background:var(--ok);box-shadow:0 0 0 4px color-mix(in srgb,var(--ok) 20%,transparent)}
  .pdot.warn{background:var(--warn);box-shadow:0 0 0 4px color-mix(in srgb,var(--warn) 22%,transparent)}

  [hidden]{display:none !important}
  .scrim{position:fixed;inset:0;z-index:60;display:flex;align-items:center;justify-content:center;
         padding:20px;background:rgba(8,10,14,.5);
         -webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px)}
  .sheet{width:100%;max-width:470px;max-height:86vh;display:flex;flex-direction:column;
         background:var(--panel);border:1px solid var(--brd);border-radius:20px;
         box-shadow:0 26px 64px -18px rgba(0,0,0,.55);overflow:hidden;
         -webkit-backdrop-filter:blur(26px) saturate(180%);backdrop-filter:blur(26px) saturate(180%);
         animation:pop .34s cubic-bezier(.22,1,.36,1) both}
  /* flow in: the scrim fades, the sheet rises, then the rows follow in sequence.
     opacity and transform only, so it stays on the compositor */
  @keyframes pop{from{opacity:0;transform:translateY(14px) scale(.975)}
                 to{opacity:1;transform:none}}
  .scrim{animation:scrimin .22s ease both}
  @keyframes scrimin{from{opacity:0}to{opacity:1}}
  .scrim.out{animation:scrimout .16s ease both}
  .scrim.out .sheet{animation:popout .16s ease both}
  @keyframes scrimout{to{opacity:0}}
  @keyframes popout{to{opacity:0;transform:translateY(6px) scale(.985)}}

  /* backwards, not both: once the run finishes the element falls back to its
     normal style, so content is never left stranded at opacity 0 */
  .sheet-b > *{animation:rowin .3s cubic-bezier(.22,1,.36,1) backwards;
               animation-delay:calc(var(--i,0) * 38ms + 60ms)}
  /* belt and braces: if animations never run at all, this reveals everything */
  .sheet-b.shown > *{animation:none}
  @keyframes rowin{from{opacity:0;transform:translateY(9px)}to{opacity:1;transform:none}}

  @media (prefers-reduced-motion:reduce){
    .sheet,.scrim,.scrim.out,.scrim.out .sheet,.sheet-b > *,.settings-page{animation:none}
  }
  .sheet-h{display:flex;align-items:center;justify-content:space-between;gap:10px;
           padding:15px 16px 13px;border-bottom:1px solid var(--brd);flex:0 0 auto}
  .sheet-h p{margin:0;font-weight:700;font-size:15px;flex:0 0 auto}
  .sheet-h #sh-ico{display:flex;align-items:center;flex:0 0 auto}
  .sheet-h #sh-ico[hidden]{display:none}
  .sheet-h #sh-ico .aico{width:32px;height:32px;border-radius:8px;object-fit:cover}
  .sheet-h #sh-ico .aico.mono{display:flex;align-items:center;justify-content:center;
                              color:#fff;font-size:13px;font-weight:700}
  .sh-sub{font-size:12.5px;font-weight:600;color:var(--muted);margin-right:auto;
          overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .x{appearance:none;border:none;background:var(--faint);color:var(--muted);cursor:pointer;
     width:28px;height:28px;border-radius:50%;font-size:19px;line-height:1}
  .sheet-b{overflow-y:auto;padding:6px 0 8px;-webkit-overflow-scrolling:touch;
           overscroll-behavior:contain}

  /* a heading inside a sheet, so a long sheet reads as sections */
  .shead{margin:14px 16px 4px;font-size:10.5px;font-weight:700;letter-spacing:.085em;
         text-transform:uppercase;color:var(--muted)}
  .shead:first-child{margin-top:4px}
  .shead .t2{display:inline;text-transform:none;letter-spacing:0;font-weight:600}
  /* one finding: a coloured dot, a sentence, and the detail under it */
  .srow{display:flex;align-items:flex-start;gap:10px;padding:9px 16px;
        border-bottom:1px solid var(--faint)}
  .srow:last-child{border-bottom:none}
  .srow .l{min-width:0;flex:1 1 auto}
  .srow .t2{white-space:normal;overflow:visible}
  .sdotL{width:7px;height:7px;border-radius:50%;flex:0 0 auto;margin-top:5px;
         background:var(--ok)}
  .srow[data-level="warn"] .sdotL{background:var(--warn)}
  .srow[data-level="bad"] .sdotL{background:var(--bad)}
  .srow[data-level="warn"] .t1{color:var(--warn)}
  .srow[data-level="bad"] .t1{color:var(--bad)}
  button.srow{width:100%;background:none;font:inherit;color:inherit;text-align:left;
              cursor:pointer;border-left:none;border-right:none;border-top:none}
  button.srow .t1::after{content:" \u203a";color:var(--muted);font-weight:700}
  .allok{padding:22px 16px 18px;text-align:center}
  .allok p{margin:0;font-weight:650;font-size:14px}
  .allok .t2{margin-top:5px;white-space:normal;font-weight:500}
  .pnote{margin:-2px 16px 11px;font-size:11.5px;color:var(--muted);line-height:1.45}
  .pnote.bad{color:var(--bad);font-weight:600}
  .row{display:flex;align-items:center;gap:10px;padding:10px 16px;
       border-bottom:1px solid var(--faint)}
  .row:last-child{border-bottom:none}
  .row .l,.applink .l{min-width:0;flex:1 1 auto}
  .t1{display:block;font-weight:650;font-size:13.5px}
  .t2{display:block;font-size:11.5px;color:var(--muted);margin-top:2px;
      overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .t2.wrap{white-space:normal;overflow:visible;line-height:1.45}
  .row .val{font-size:18px;font-weight:700;font-variant-numeric:tabular-nums;
            flex:0 0 auto;min-width:52px;text-align:right}
  .row .val[data-level="warn"]{color:var(--warn)}
  .row .val[data-level="bad"]{color:var(--bad)}
  .row .tag{font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;font-weight:700;
            color:var(--muted);background:var(--faint);padding:3px 7px;border-radius:999px;
            flex:0 0 auto;min-width:54px;text-align:center}
  .row .bar{flex:1 1 auto;height:6px;border-radius:999px;background:var(--faint);
            overflow:hidden;min-width:60px}
  .row .bar i{display:block;height:100%;background:var(--accent)}
  .row .bar i[data-level="warn"]{background:var(--warn)}
  .row .bar i[data-level="bad"]{background:var(--bad)}

  .smart{display:inline-flex;align-items:center;font-size:10px;font-weight:700;white-space:nowrap;
         padding:3px 8px;border-radius:999px;letter-spacing:.04em;flex:0 0 auto}
  .smart.ok{color:var(--ok);background:color-mix(in srgb,var(--ok) 15%,transparent)}
  .smart.bad{color:var(--bad);background:color-mix(in srgb,var(--bad) 18%,transparent)}
  .smart.unk{color:var(--muted);background:var(--faint)}
  .ubtn{flex:1;appearance:none;border:1px solid var(--brd);border-radius:var(--r-full);padding:13px 0;
        font:inherit;font-weight:650;font-size:14px;cursor:pointer}
  .ubtn.no{background:var(--faint);color:var(--ink)}
  .ubtn.yes{background:var(--warn);color:#2a1c00;border-color:transparent}
  .ubtn:disabled{opacity:.6;cursor:default}

  /* every row the same height, so one long name or version can never stretch
     its neighbours and drop their text out of line */
  .appgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(134px,1fr));
           grid-auto-rows:1fr;gap:9px}
  .sheet .appgrid{padding:12px 16px 14px}
  /* Deliberately a smaller corner than the metric cards above them: the
     shape is what separates the two groups now that nothing is outlined. */
  .apptile{position:relative;text-decoration:none;color:inherit;display:flex;
           flex-direction:column;gap:6px;background:var(--panel);
           border:1px solid var(--brd);box-shadow:var(--tile-shadow);
           border-radius:var(--r-sm);
           padding:11px 12px;min-height:84px;transition:border-color .15s ease}
  .apptile:hover{border-color:color-mix(in srgb,var(--accent) 40%,var(--brd))}
  .apptile .sdot{position:absolute;top:13px;right:12px;width:6px;height:6px;
                 border-radius:50%;background:var(--muted);opacity:.5}
  .apptile[data-up="1"] .sdot{background:var(--ok);opacity:1}
  .apptile[data-up="0"]{opacity:.62}
  .apptile .aico{width:22px;height:22px;border-radius:6px;object-fit:cover;flex:0 0 auto}
  .apptile .aico.mono{display:flex;align-items:center;justify-content:center;color:#fff;
                      font-size:10px;font-weight:700}
  .apptile .an{font-size:12.5px;font-weight:600;line-height:1.2;margin-top:auto;
               overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .apptile .ap{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums;
               overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .apptile .upd{position:absolute;top:24px;right:5px;cursor:pointer;appearance:none;
                width:21px;height:21px;display:flex;align-items:center;justify-content:center;
                padding:0;line-height:1;color:var(--warn);border-radius:50%;
                border:1px solid color-mix(in srgb,var(--warn) 55%,transparent);
                background:color-mix(in srgb,var(--warn) 9%,transparent)}
  .apptile .upd:hover{border-color:var(--warn);
                      background:color-mix(in srgb,var(--warn) 17%,transparent)}
  .apptile .upd::before{content:"\\203a";display:block;font-size:15px;line-height:1;
                        transform:rotate(-90deg) translateY(-1px);
                        transition:transform .15s ease, opacity .15s ease}
  .apptile .upd:hover::before{transform:rotate(-90deg) translateY(1px)}
  /* update progress. Queued and running are not warnings, so they wear the
     accent; only a finished or failed update takes a status colour. */
  .apptile .ust{position:absolute;top:24px;right:6px;font-size:9px;font-weight:700;
                letter-spacing:.04em;text-transform:uppercase;border-radius:999px;padding:2px 6px;
                display:none;font-variant-numeric:tabular-nums}
  .apptile[data-ust] .ust{display:block}
  .apptile[data-ust] .upd{display:none}
  .apptile[data-ust="queued"] .ust,.apptile[data-ust="running"] .ust{
    color:var(--accent);background:color-mix(in srgb,var(--accent) 16%,transparent)}
  .apptile[data-ust="done"] .ust{color:var(--ok);background:color-mix(in srgb,var(--ok) 18%,transparent)}
  .apptile[data-ust="failed"] .ust{color:var(--bad);background:color-mix(in srgb,var(--bad) 18%,transparent)}
  /* an app being upgraded is down on purpose; do not fade it like a fault */
  .apptile[data-ust="running"],.apptile[data-ust="queued"]{opacity:1}
  .uprog{display:none;height:3px;border-radius:2px;overflow:hidden;
         background:color-mix(in srgb,var(--accent) 18%,transparent)}
  .uprog i{display:block;height:100%;width:0;border-radius:2px;background:var(--accent);
           transition:width .6s ease}
  .apptile .uprog{position:absolute;left:12px;right:12px;bottom:6px}
  .apptile[data-ust="queued"] .uprog,.apptile[data-ust="running"] .uprog{display:block}
  /* queued has no percentage yet, so it moves without claiming one */
  [data-ust="queued"] .uprog i{width:35%;animation:indet 1.3s ease-in-out infinite}
  @keyframes indet{0%{transform:translateX(-100%)}100%{transform:translateX(290%)}}
  .upbanner{display:flex;flex-wrap:wrap;align-items:center;gap:4px 10px;margin:0 0 14px;
            padding:11px 14px;border-radius:var(--r-sm);background:var(--panel);
            border:1px solid color-mix(in srgb,var(--accent) 45%,var(--brd));box-shadow:var(--tile-shadow)}
  .upbanner[hidden]{display:none}
  .upbanner b{font-size:13px;font-weight:650}
  .upbanner span{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
  .upbanner .uprog{display:block;flex:1 0 100%}
  .upbanner[data-ust="done"]{border-color:color-mix(in srgb,var(--ok) 50%,var(--brd))}
  .upbanner[data-ust="done"] .uprog,.upbanner[data-ust="failed"] .uprog{display:none}
  .upbanner[data-ust="failed"]{border-color:color-mix(in srgb,var(--bad) 50%,var(--brd))}
  @media (prefers-reduced-motion:reduce){ [data-ust="queued"] .uprog i{animation:none;width:100%;opacity:.5} }
  /* a custom address is a deliberate choice, so its tile carries a small pin
     marker rather than looking identical to the auto-resolved ones. The edit
     itself lives in the app sheet now, not on the tile. */
  .apptile .pin{position:absolute;bottom:11px;right:11px;width:7px;height:7px;
                border-radius:50% 50% 50% 0;transform:rotate(-45deg);
                background:var(--accent);box-shadow:0 0 0 2px var(--surf)}
  .apptile{cursor:pointer}

  /* ---- app detail sheet ------------------------------------------------
     Tapping a tile used to launch the app. It now opens this instead: the
     same readings HEXOS shows, and the one place the launch address is set. */
  .appsheet{padding:2px 0 10px}
  .appdesc{margin:12px 16px 10px;font-size:12.5px;color:var(--muted);line-height:1.45}
  .appstat{display:flex;align-items:center;gap:8px;margin:0 16px 12px}
  .pill{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:700;
        letter-spacing:.03em;padding:4px 10px;border-radius:999px}
  .pill.up{color:var(--ok);background:color-mix(in srgb,var(--ok) 15%,transparent)}
  .pill.down{color:var(--bad);background:color-mix(in srgb,var(--bad) 16%,transparent)}
  .pill.warn{color:var(--warn);background:color-mix(in srgb,var(--warn) 16%,transparent)}
  .pill .dot{width:6px;height:6px;border-radius:50%;background:currentColor}
  .appver{margin-left:auto;font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums}
  .mcards{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin:0 16px 12px}
  .mcard{background:var(--surf);border-radius:var(--r-sm);padding:11px 12px;min-height:70px;
         display:flex;flex-direction:column;gap:5px;overflow:hidden}
  .mcard .ml{font-size:10.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
             color:var(--muted)}
  .mcard .mv{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.1}
  .mcard .mu{font-size:11px;font-weight:600;color:var(--muted);margin-left:3px}
  .mcard .mspark{height:26px;margin-top:auto}
  .mcard .mspark svg{width:100%;height:26px;display:block}
  .appresponse{display:flex;align-items:center;gap:10px;margin:0 16px 11px;padding:8px 10px;
               border-radius:var(--r-sm);background:var(--surf)}
  .appresponse .rl{font-size:10px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;
                   color:var(--muted);flex:0 0 auto}
  .appresponse .rv{font-size:14px;font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap}
  .appresponse .mspark{height:22px;flex:1;min-width:20px}
  .appresponse .mspark svg{height:22px}
  .appurls{margin:2px 16px 12px;border-top:1px solid var(--faint);padding-top:12px}
  .appurls .lbl{font-size:10.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
                color:var(--muted);margin-bottom:7px}
  .urow{display:flex;align-items:center;gap:8px;padding:7px 0;font-size:12px;
        font-variant-numeric:tabular-nums}
  .urow .uk{color:var(--muted);flex:0 0 62px}
  .urow .uv{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
            text-decoration:none;color:var(--ink)}
  .urow .uv.on{color:var(--accent);font-weight:650}
  .appacts{position:sticky;bottom:-8px;z-index:3;display:flex;gap:9px;
           margin-top:8px;padding:12px 16px max(12px,env(safe-area-inset-bottom));
           background:var(--panel);border-top:1px solid var(--brd)}
  .appacts .ubtn{flex:1}
  .appacts .launch{background:var(--accent);color:#fff;border-color:transparent;flex:2}
  .appacts .launch:disabled{opacity:.5}
  .optbox{margin:0 16px 4px;padding:0 0 6px}
  .optbox .none{font-size:12px;color:var(--muted);padding:2px 0 6px}


  /* Memory donut: apps / ZFS cache / free, the split TrueNAS's own gauge
     uses. Cache is the accent mixed 60% into the card - checked against both
     themes, 40% read as grey. Every segment eases to its new size each poll. */
  :root{--mem-cache:color-mix(in srgb,var(--accent) 60%,var(--panel))}
  .memrow{display:flex;align-items:center;gap:12px;flex:1}
  .memtxt{flex:1;min-width:0}
  /* 1x reads like CPU temp beside it: the figures at the top, a trend line
     along the bottom. The donut needs the room a 2x card gives it. */
  .cardwrap[data-w="1"] .donut{display:none}
  .cardwrap[data-w="1"] .memrow{align-items:flex-start;flex:0 0 auto}
  .cardwrap[data-w="2"] .memspark{display:none}
  .donut{flex:0 0 auto;width:50px;height:50px;transform:rotate(-90deg);overflow:visible}
  .cardwrap[data-w="2"] .donut{width:104px;height:104px}
  .donut circle{fill:none;stroke-width:6;stroke-dasharray:0 100;
    transition:stroke-dasharray .7s cubic-bezier(.4,0,.2,1),
               stroke-dashoffset .7s cubic-bezier(.4,0,.2,1)}
  .donut .apps{stroke:var(--accent)}
  .donut .cache{stroke:var(--mem-cache)}
  .donut .free{stroke:var(--faint)}
  .mleg{display:none}
  .cardwrap[data-w="2"] .mleg{display:grid;gap:4px;margin-top:12px;
    font-size:11.5px;color:var(--muted);max-width:190px}
  .mleg span{display:flex;align-items:center;gap:7px}
  .mleg u{width:8px;height:8px;border-radius:3px;flex:0 0 auto}
  .mleg b{margin-left:auto;font-weight:600;color:var(--ink);font-variant-numeric:tabular-nums}
  @media (prefers-reduced-motion:reduce){ .donut circle{transition:none} }
  .stackbar{display:flex;height:16px;border-radius:999px;overflow:hidden;margin:10px 16px 10px;
            background:var(--faint)}
  .stackbar i{display:block;height:100%}
  .legend{display:flex;flex-wrap:wrap;gap:9px 16px;padding:0 16px 12px;font-size:12px;
          color:var(--muted)}
  .legend span{display:inline-flex;align-items:center;gap:6px;font-variant-numeric:tabular-nums}
  .legend u{width:9px;height:9px;border-radius:3px;display:block}

  .chart{padding:14px 16px 2px}
  .chart svg{width:100%;height:112px;display:block;overflow:visible}
  .chart .cap{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);
              margin-top:6px}

  .tl{padding:12px 16px 4px}
  .tl .seg{display:flex;height:22px;border-radius:7px;overflow:hidden;background:var(--faint)}
  .tl .seg i{display:block;height:100%}
  .tl .cap{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:6px}

          font-weight:600;color:var(--muted);background:none;border:none;cursor:pointer;
          padding:6px 10px;border-radius:999px}
  @media (prefers-reduced-motion:reduce){*{animation-duration:.001ms !important;transition-duration:.001ms !important}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="id">
      <!-- Three ascending bars. A dashboard is a board of readings, and bars
           are the only shape here that still reads as itself at 16px. Equal
           gaps, rounded caps, the tallest bar twice the shortest. -->
      <svg class="mark" viewBox="0 0 48 48" role="img" aria-label="Dashy">
        <rect width="48" height="48" rx="12" fill="var(--brand)"/>
        <g fill="#fbfcff">
          <rect x="11" y="26" width="6.5" height="12" rx="3.25"/>
          <rect x="20.75" y="19" width="6.5" height="19" rx="3.25"/>
          <rect x="30.5" y="11" width="6.5" height="27" rx="3.25"/>
        </g>
      </svg>
      <div style="min-width:0"><h1>Dashy<span class="host" id="host"></span></h1></div>
    </div>
    <div class="hdr-r">

      <button class="ibtn" id="settings" type="button" aria-controls="settings-page" aria-expanded="false"
                title="Settings" aria-label="Settings"><svg viewBox="0 0 24 24" fill="none"
         stroke="currentColor" stroke-width="1.9" stroke-linecap="round"
         stroke-linejoin="round"><circle cx="12" cy="12" r="3.1"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></button>

    </div>
  </header>

  <main class="settings-page" id="settings-page" hidden aria-label="Settings">
    <div class="settings-head">
      <div><p class="settings-kicker">DASHY / SETTINGS</p><h2>Settings</h2>
        <p>Appearance, layout, integrations and app links.</p></div>
      <button class="settings-back" id="settings-back" type="button">Back to dashboard</button>
    </div>
    <div class="settings-layout">
      <nav class="settings-nav" aria-label="Settings sections">
        <button class="active" type="button" data-settings-target="settings-system">System</button>
        <button type="button" data-settings-target="settings-appearance">Appearance</button>
        <button type="button" data-settings-target="settings-dashboard">Cards</button>
        <button type="button" data-settings-target="settings-integrations">Integrations</button>
        <button type="button" data-settings-target="settings-links">App links</button>
      </nav>
      <div class="settings-content">
        <section class="settings-section" id="settings-system">
          <h3>System</h3><div class="settings-grid">
            <article class="settings-card"><div class="settings-glyph">↻</div><h4>App updates</h4>
              <p>Check for a new version of Dashy, then install it. A snapshot is taken first.</p>
              <button class="mi" id="mi-self" type="button">
                <span class="dots" id="mi-selfdots" hidden><i></i><i></i><i></i></span>
                <span id="mi-selftxt">Check for updates</span><span class="rt" id="mi-selfver"></span></button>
            </article>
            <article class="settings-card"><div class="settings-glyph">i</div><h4>About</h4>
              <p>Dashboard version and setup.</p>
              <button class="mi" id="mi-setup" type="button">Show the welcome screen</button>
               <div class="settings-version" id="settings-version"></div>
            </article>
          </div>
        </section>
        <section class="settings-section" id="settings-appearance">
          <h3>Appearance</h3><div class="settings-grid">
            <article class="settings-card"><div class="settings-glyph">◐</div><h4>Theme</h4>
              <p>Choose a theme or follow your device preference.</p>
              <div class="seg" id="seg-theme" role="group" aria-label="Theme">
                <button type="button" data-theme="system" aria-pressed="true">System</button>
                <button type="button" data-theme="light" aria-pressed="false">Light</button>
                <button type="button" data-theme="dark" aria-pressed="false">Dark</button>
              </div>
            </article>
            <article class="settings-card"><div class="settings-glyph">●</div><h4>Accent colour</h4>
              <p>Choose the colour used for highlights and charts.</p>
              <div class="sw" id="sw-accent" role="group" aria-label="Accent colour"></div>
            </article>
          </div>
        </section>
        <section class="settings-section" id="settings-dashboard">
          <h3>Dashboard</h3><div class="settings-grid">
            <article class="settings-card"><div class="settings-glyph">▦</div><h4>Card layout</h4>
              <p>Resize cards, hide the ones you do not need, or restore the default layout.</p>
              <button class="mi" id="mi-edit" type="button">Resize or remove cards</button>
              <button class="mi" id="mi-reset" type="button">Reset dashboard layout</button>
            </article>
          </div>
        </section>
        <section class="settings-section" id="settings-integrations">
          <h3>Integrations</h3><div class="settings-grid">
            <article class="settings-card"><div class="settings-glyph">↗</div><h4>Tailscale</h4>
              <p>Automatically publish discovered apps and find their tailnet addresses.</p>
              <div class="mrow"><span class="ml">Enable Tailscale</span>
                <button class="sw2 sm" id="m-ts" role="switch" aria-checked="false"><i></i></button></div>
              <span class="pubmsg" id="m-tsmsg"></span>
            </article>
            <article class="settings-card"><div class="settings-glyph">⬡</div><h4>HexOS</h4>
              <p>Show the HexOS link in the dashboard header.</p>
              <div class="mrow"><span class="ml">Enable HexOS</span>
                <button class="sw2 sm" id="m-hx" role="switch" aria-checked="false"><i></i></button></div>
            </article>
          </div>
        </section>
        <section class="settings-section" id="settings-links">
          <h3>App links</h3><div class="settings-grid">
            <article class="settings-card"><div class="settings-glyph">⌁</div><h4>Default address</h4>
              <p>Choose which discovered address app tiles use when launched.</p>
              <div class="seg" id="m-links" role="group" aria-label="Default app address">
                <button type="button" data-links="ts" aria-pressed="true">Tailnet</button>
                <button type="button" data-links="lan" aria-pressed="false">LAN</button>
              </div>
            </article>
            <article class="settings-card"><div class="settings-glyph">⌂</div><h4>TrueNAS address</h4>
              <p>Address used by the TrueNAS header link.</p>
              <input class="fld sm" id="m-nas" type="url" inputmode="url"
                     placeholder="https://nas.example.ts.net/">
              <div class="settings-links">
                <a class="mi" id="mi-truenas" target="_blank" rel="noopener">Open TrueNAS</a>
                <a class="mi" id="mi-hexos" target="_blank" rel="noopener">Open HexOS</a>
              </div>
            </article>
          </div>
          <div class="settings-note">Each app can also have separate LAN and tailnet addresses in its Options.</div>
        </section>
      </div>
    </div>
  </main>

  <div class="editbar" id="editbar">
    <span><b>2\u00d7</b> resizes, <b>\u00d7</b> removes. Removed cards stay here, faded, with a <b>+</b>.</span>
    <button class="rst" id="eb-reset" type="button">Reset</button>
    <button class="done" id="eb-done" type="button">Done</button>
  </div>

  <div class="upbanner" id="upbanner" role="status" aria-live="polite" hidden>
    <b id="ub-t"></b><span id="ub-s"></span><span class="uprog"><i id="ub-bar"></i></span>
  </div>

  <div id="vitals"></div>

</div>

<div class="setup" id="setup" hidden>
  <div class="setupcard">
    <button class="back" id="su-back" hidden type="button">&lsaquo; Back</button>
    <svg class="mark" viewBox="0 0 48 48" role="img" aria-label="Dashy">
      <rect width="48" height="48" rx="12" fill="var(--brand)"/>
      <g fill="#fbfcff">
        <rect x="11" y="26" width="6.5" height="12" rx="3.25"/>
        <rect x="20.75" y="19" width="6.5" height="19" rx="3.25"/>
        <rect x="30.5" y="11" width="6.5" height="27" rx="3.25"/>
      </g>
    </svg>
    <h2 id="su-h">Welcome to Dashy</h2>
    <p class="lede" id="su-lede">A dashboard for your NAS that finds your apps
      on its own.</p>

    <div id="su-feats">
      <p class="feat"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round"><path d="M3 12h4l3-8 4 16 3-8h4"/></svg>
        <span><b>Live vitals.</b> CPU, memory, temperatures, disks, network.
        Tap any tile for detail.</span></p>
      <p class="feat"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.6"/><rect x="14" y="3" width="7" height="7" rx="1.6"/><rect x="3" y="14" width="7" height="7" rx="1.6"/><rect x="14" y="14" width="7" height="7" rx="1.6"/></svg>
        <span><b>Your apps, automatically.</b> Versions, status, and one-tap
        updates with a snapshot taken first.</span></p>
      <p class="feat"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round"><path d="M12 3v4M12 17v4M3 12h4M17 12h4"/><circle cx="12" cy="12" r="3.4"/></svg>
        <span><b>Drive health.</b> Per-drive temperatures and SMART, with
        warnings up top.</span></p>
      <p class="feat"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 17h16M4 7h16M4 12h10"/></svg>
        <span><b>Make it yours.</b> Resize and hide tiles. Your layout
        follows you to every device.</span></p>
    </div>

    <div id="su-look" hidden>
      <div class="seg" id="su-theme" role="group" aria-label="Theme">
        <button type="button" data-theme="system" aria-pressed="true">System</button>
        <button type="button" data-theme="light"  aria-pressed="false">Light</button>
        <button type="button" data-theme="dark"   aria-pressed="false">Dark</button>
      </div>
      <p class="hint2">System follows your phone or computer.</p>
      <p class="mh" style="padding-left:2px">Accent</p>
      <div class="sw" id="su-accent" role="group" aria-label="Accent colour"></div>
    </div>

    <div id="su-opts" hidden>
      <div class="opt">
        <span class="txt"><b>Tailscale<span class="auto" id="a-ts"></span></b>
          <span>Open apps by their tailnet address.</span></span>
        <button class="sw2" id="o-ts" role="switch" aria-checked="false"><i></i></button>
      </div>
      <div class="opt col" id="su-serve" hidden>
        <span class="txt"><b>App links</b>
          <span>Which address a tile opens. Tailnet works from anywhere you are
          signed in; your local network only works at home.</span></span>
        <div class="seg" id="su-links" role="group" aria-label="App links">
          <button type="button" data-links="ts" aria-pressed="true">Tailnet</button>
          <button type="button" data-links="lan" aria-pressed="false">Local network</button>
        </div>
        <span class="pubmsg" id="su-tsmsg"></span>
      </div>
      <div class="opt">
        <span class="txt"><b>HexOS<span class="auto" id="a-hx"></span></b>
          <span>Adds a link to your HexOS deck.</span></span>
        <button class="sw2" id="o-hx" role="switch" aria-checked="false"><i></i></button>
      </div>
      <div class="opt" style="display:block">
        <span class="txt"><b>TrueNAS address</b>
          <span>The address you normally type to open TrueNAS itself.
          Usually its IP, like <code>http://192.168.1.10</code>. We have had a
          guess below &mdash; if it opens TrueNAS, it is right.</span></span>
        <input class="fld" id="o-nas" type="url" inputmode="url"
               placeholder="http://192.168.1.10">
      </div>
    </div>

    <div class="dots" id="su-dots"><i></i><i></i><i></i></div>
    <button class="go" id="su-go">Get started</button>
    <button class="skip" id="su-skip">Skip for now</button>
  </div>
</div>

<div class="scrim" id="scrim" hidden>
  <div class="sheet" role="dialog" aria-modal="true" aria-labelledby="sh-t">
    <div class="sheet-h"><span id="sh-ico" hidden></span><p id="sh-t"></p><span class="sh-sub" id="sh-s"></span>
      <button class="x" id="sh-x" type="button" aria-label="Close">&times;</button></div>
    <div class="sheet-b" id="sh-b"></div>
  </div>
</div>

<script>
const $=id=>document.getElementById(id);
let built=false, data=null, openKey=null;

function fmt(n, step, units){
  if(n===null||n===undefined) return '\\u2013';
  let i=0; n=Number(n);
  while(n>=step && i<units.length-1){ n/=step; i++; }
  return (n>=100?n.toFixed(0):n>=10?n.toFixed(1):n.toFixed(2))+' '+units[i];
}
const ram  = n=>fmt(n,1024,['B','KiB','MiB','GiB','TiB']);
const disk = n=>fmt(n,1000,['B','KB','MB','GB','TB','PB']);
function diskParts(n){
  if(n===null||n===undefined) return ['\\u2013',''];
  const u=['B','KB','MB','GB','TB','PB']; let i=0; n=Number(n);
  while(n>=1000 && i<u.length-1){ n/=1000; i++; }
  return [(n>=100?n.toFixed(0):n>=10?n.toFixed(1):n.toFixed(2)), u[i]];
}
function durParts(s){
  if(!s) return ['\\u2013',''];
  const d=Math.floor(s/86400), h=Math.floor(s%86400/3600), m=Math.floor(s%3600/60);
  if(d) return [String(d), d===1?'day':'days'];
  if(h) return [String(h), h===1?'hour':'hours'];
  if(m) return [String(m), m===1?'minute':'minutes'];
  return [String(Math.round(s)), 'seconds'];
}
function dur(s){
  if(!s) return '\\u2013';
  const d=Math.floor(s/86400), h=Math.floor(s%86400/3600), m=Math.floor(s%3600/60);
  if(d) return d+'d';
  if(h) return h+'h';
  if(m) return m+'m';
  return Math.round(s)+'s';
}
// the popup still wants the fuller form
function durLong(s){
  if(!s) return '\\u2013';
  const d=Math.floor(s/86400), h=Math.floor(s%86400/3600), m=Math.floor(s%3600/60);
  if(d) return d+'d '+h+'h';
  if(h) return h+'h '+m+'m';
  return m+'m';
}
function level(p){ return p>=90?'bad':p>=75?'warn':'ok'; }
function rateParts(b){
  if(b===null||b===undefined) return ['\\u2013',''];
  if(b<1000) return [b.toFixed(0),'B/s'];
  if(b<1e6)  return [(b/1e3).toFixed(0),'KB/s'];
  if(b<1e9)  return [(b/1e6).toFixed(b<1e7?1:0),'MB/s'];
  return [(b/1e9).toFixed(2),'GB/s'];
}
function rate(b){ const p=rateParts(b); return p[1]? p[0]+' '+p[1] : p[0]; }
function when(ts){ return new Date(ts*1000).toLocaleDateString([], {day:'numeric',month:'short',year:'2-digit'}); }


// Every card declared once. Order and visibility come from saved prefs, so
// rearranging is a re-render rather than a special case per card.
const CARDS=[
  {k:'health',label:'Health', sec:'system', kind:'health'},
  {k:'cpu',   label:'Processor', sec:'system', trend:1},
  {k:'mem',   label:'Memory',    sec:'system'},
  {k:'ctemp', label:'CPU temp',  sec:'system', trend:1},
  {k:'net',   label:'Network',   sec:'system', trend:1},
  {k:'dtemp', label:'Drive temps', sec:'storage'}
];
const SECTIONS=[['system','System'],['storage','Storage'],['apps','Apps'],['vms','Virtual machines']];
const CARDK=CARDS.map(c=>c.k);
/* Pool cards are made from the data, so their keys are not in CARDK. They are
   lower-cased to the same alphabet the server accepts for a prefs key: a pool
   called HDDs used to be resized as pool-HDDs, which both ends then threw away
   as unknown, and the card went back to 1x on the next reload. */
function poolKey(name){
  return 'pool-'+String(name).toLowerCase().replace(/[^a-z0-9_-]/g,'_').slice(0,27);
}
function knownKey(k){ return CARDK.indexOf(k)>=0 || /^pool-[a-z0-9_-]{1,27}$/.test(k); }

/* ---------- preferences ------------------------------------------------
   Per device, in localStorage. Dashy has no login, so there is nowhere else
   sensible to put them, and every read is guarded: private windows and
   blocked site data both throw rather than return null.                   */
const PKEY='overlook.prefs.v2';
/* Dusk is the default. Amber, red and green stay out: they already mean warn,
   bad and ok, so an accent that strays into them reads as an alert that is not
   there. Everything on offer sits in the blue-to-stone range. */
const ACCENTS={
  dusk:    {light:'#4a57c8', dark:'#7d87e6', name:'Dusk'},
  harbour: {light:'#1d7391', dark:'#49a7c6', name:'Harbour'},
  heather: {light:'#7b57ad', dark:'#a583d6', name:'Heather'},
  granite: {light:'#51606e', dark:'#8c9bab', name:'Granite'}
};

/* A saved choice may no longer exist. Map it to the nearest survivor rather
   than silently dropping someone back to default. */
const ACCENT_WAS={teal:'harbour', blue:'dusk', violet:'heather',
                  amber:'granite', yellow:'granite', rose:'heather', green:'harbour'};
function migrateAccent(a){ return ACCENTS[a] ? a : (ACCENT_WAS[a] || 'dusk'); }
const DEFAULTS={theme:'system', accent:'dusk', order:CARDK.slice(), hidden:[], wide:[], size:{},
                setup:{}, urls:{}};
let prefs=Object.assign({}, DEFAULTS);
let lastTick=0;
let bootVersion=null;
let builtSig='';
let footUp=['\u2013',''], footNote='', footIssue=false;

function loadPrefs(){
  let raw=null;
  try{ raw=localStorage.getItem(PKEY); }catch(e){}
  if(raw){
    try{
      const d=JSON.parse(raw)||{};
      if(['system','light','dark'].indexOf(d.theme)>=0) prefs.theme=d.theme;
      if(d.accent) prefs.accent=migrateAccent(d.accent);
      if(Array.isArray(d.hidden)) prefs.hidden=d.hidden.filter(knownKey);
      if(Array.isArray(d.wide)) prefs.wide=d.wide.filter(knownKey);
      if(d.size && typeof d.size==='object') prefs.size=cleanSize(d.size);
      migrateSizes();
      if(d.setup && typeof d.setup==='object') prefs.setup=d.setup;
      if(d.urls && typeof d.urls==='object') prefs.urls=cleanUrls(d.urls);
      if(Array.isArray(d.order)){
        // keep only known cards, then append any card added since this was saved
        const keep=d.order.filter(k=>CARDK.indexOf(k)>=0);
        prefs.order=keep.concat(CARDK.filter(k=>keep.indexOf(k)<0));
      }
    }catch(e){}
  }
}
function savePrefs(){
  try{ localStorage.setItem(PKEY, JSON.stringify(prefs)); }catch(e){}
  pushPrefs();
}

let setupShown=false;
let prefsRev=-1;          // last revision this device has seen or written
let pushTimer=null, pushing=false;

function pushPrefs(){
  clearTimeout(pushTimer);
  pushTimer=setTimeout(async()=>{
    pushTimer=null;      // a fired timer id stays truthy; clearing it matters
    pushing=true;
    try{
      const r=await fetch('/api/layout',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({order:prefs.order, hidden:prefs.hidden, wide:prefs.wide,
                             size:prefs.size,
                             theme:prefs.theme, accent:prefs.accent,
                             setup:prefs.setup, urls:prefs.urls})});
      const j=await r.json();
      if(j && typeof j.rev==='number') prefsRev=j.rev;   // do not re-apply our own echo
    }catch(e){}                 // offline or no writable mount: the local copy still holds
    pushing=false;
  }, 350);                      // a drag fires constantly; only the last state matters
}

// Apply settings that arrived from another device. Returns true if the tile
// layout changed, since that needs a rebuild rather than just a repaint.
function adoptPrefs(P){
  if(!P || typeof P.rev!=='number') return false;
  if(pushing || pushTimer) return false;      // our own change is still in flight
  if(P.rev===prefsRev) return false;
  prefsRev=P.rev;

  let layoutChanged=false;
  const same=(a,b)=>a.length===b.length && a.every((v,i)=>v===b[i]);

  if(Array.isArray(P.order)){
    const keep=P.order.filter(k=>CARDK.indexOf(k)>=0);
    const next=keep.concat(CARDK.filter(k=>keep.indexOf(k)<0));
    if(!same(next, prefs.order)){ prefs.order=next; layoutChanged=true; }
  }
  if(Array.isArray(P.hidden)){
    const next=P.hidden.filter(knownKey);
    if(!same(next, prefs.hidden)){ prefs.hidden=next; layoutChanged=true; }
  }
  if(P.size && typeof P.size==='object'){
    const ns=cleanSize(P.size);
    if(JSON.stringify(ns)!==JSON.stringify(prefs.size)){ prefs.size=ns; layoutChanged=true; }
  }
  if(Array.isArray(P.wide) && !(P.size && Object.keys(P.size).length)){
    const had=JSON.stringify(prefs.size);
    prefs.wide=P.wide.slice(); prefs.size={}; migrateSizes();
    if(JSON.stringify(prefs.size)!==had) layoutChanged=true;
  }
  if(Array.isArray(P.wide)){
    const next=P.wide.filter(knownKey);
    if(!same(next, prefs.wide)){ prefs.wide=next; layoutChanged=true; }
  }
  if(P.setup && typeof P.setup==='object') prefs.setup=P.setup;
  if(P.urls && typeof P.urls==='object') prefs.urls=cleanUrls(P.urls);
  if(P.theme && P.theme!==prefs.theme){ prefs.theme=P.theme; applyTheme(); }
  const inc=P.accent && migrateAccent(P.accent);
  if(inc && inc!==prefs.accent){
    prefs.accent=inc; applyAccent();
  }
  try{ localStorage.setItem(PKEY, JSON.stringify(prefs)); }catch(e){}
  return layoutChanged;
}

const darkMQ = window.matchMedia ? matchMedia('(prefers-color-scheme:dark)') : null;
function isDark(){
  return prefs.theme==='dark' || (prefs.theme==='system' && !!(darkMQ && darkMQ.matches));
}
function applyTheme(){
  const r=document.documentElement;
  if(prefs.theme==='system') r.removeAttribute('data-theme');
  else r.setAttribute('data-theme', prefs.theme);
  applyAccent();
  document.querySelectorAll('#seg-theme button').forEach(b=>
    b.setAttribute('aria-pressed', String(b.dataset.theme===prefs.theme)));
}
/* ---------- tonal surfaces -------------------------------------------------
   Material 3's idea, done with arithmetic rather than a library: every
   surface is the chosen colour at a different tone, with most of its chroma
   taken out. The background, the cards, the borders and the muted text all
   carry a trace of the accent, so picking Heather does not just recolour the
   icon - it tilts the whole page.

   OKLCH rather than HSL. HSL's lightness is not perceptual, so the same L
   across two hues gives two visibly different greys, and the neutrals come
   out blotchy. The conversion below is the standard sRGB <-> Oklab matrices.
   Kept short on purpose: this runs on every theme change.                */

function srgbToLinear(c){ return c<=0.04045 ? c/12.92 : Math.pow((c+0.055)/1.055, 2.4); }
function linearToSrgb(c){ return c<=0.0031308 ? c*12.92 : 1.055*Math.pow(c, 1/2.4)-0.055; }

function hexToOklch(hex){
  const h=hex.replace('#','');
  const r=srgbToLinear(parseInt(h.slice(0,2),16)/255),
        g=srgbToLinear(parseInt(h.slice(2,4),16)/255),
        b=srgbToLinear(parseInt(h.slice(4,6),16)/255);
  const l=Math.cbrt(0.4122214708*r+0.5363325363*g+0.0514459929*b),
        m=Math.cbrt(0.2119034982*r+0.6806995451*g+0.1073969566*b),
        s2=Math.cbrt(0.0883024619*r+0.2817188376*g+0.6299787005*b);
  const L=0.2104542553*l+0.7936177850*m-0.0040720468*s2,
        A=1.9779984951*l-2.4285922050*m+0.4505937099*s2,
        B=0.0259040371*l+0.7827717662*m-0.8086757660*s2;
  return {L, C:Math.sqrt(A*A+B*B), h:Math.atan2(B,A)};
}

function oklchToHex(L,C,h){
  const A=Math.cos(h)*C, B=Math.sin(h)*C;
  const l=Math.pow(L+0.3963377774*A+0.2158037573*B,3),
        m=Math.pow(L-0.1055613458*A-0.0638541728*B,3),
        s2=Math.pow(L-0.0894841775*A-1.2914855480*B,3);
  const to=v=>{
    const n=Math.round(Math.max(0,Math.min(1,linearToSrgb(v)))*255);
    return n.toString(16).padStart(2,'0');
  };
  return '#'+to( 4.0767416621*l-3.3077115913*m+0.2309699292*s2)
            +to(-1.2684380046*l+2.6097574011*m-0.3413193965*s2)
            +to(-0.0041960863*l-0.7034186147*m+1.7076147010*s2);
}

/* tone: 0 = black, 1 = white. c: absolute chroma, NOT a fraction of the
   source. A fraction looks right in theory and vanishes in practice - the
   accents sit around C 0.15, so even a tenth of that is invisible across a
   large surface. These are the values that actually read as tinted. */
function tone(src, t, c){
  return oklchToHex(t, c, src.h);
}

function applySurfaces(brandHex, dark){
  const src=hexToOklch(brandHex);
  const r=document.documentElement.style;
  /* Scale by how saturated the accent itself is, so a deliberately muted
     colour produces muted surfaces. Without this Granite - the near-neutral
     option - came out as blue as Dusk, which defeats the point of offering
     it. 0.13 is roughly where the vivid accents sit. */
  const k=Math.max(0.3, Math.min(1, src.C/0.13));
  const C=(x)=>x*k;
  if(dark){
    // Chroma is far less visible at low lightness than at high, so the dark
    // scheme needs roughly double the number to read as the same colour.
    // Matching the light values left dark mode looking like plain charcoal.
    r.setProperty('--bg',    tone(src, 0.182, C(0.045)));
    r.setProperty('--panel', tone(src, 0.258, C(0.052)));
    r.setProperty('--faint', tone(src, 0.318, C(0.058)));
    r.setProperty('--brd',   tone(src, 0.352, C(0.066)));
    r.setProperty('--muted', tone(src, 0.690, C(0.050)));
    r.setProperty('--ink',   tone(src, 0.950, C(0.022)));
    // the card surface: one tonal step up from the page, which is what makes
    // a borderless card read as raised
    r.setProperty('--surf',  tone(src, 0.262, C(0.055)));
  }else{
    r.setProperty('--bg',    tone(src, 0.952, C(0.024)));
    r.setProperty('--panel', tone(src, 0.994, C(0.008)));
    r.setProperty('--faint', tone(src, 0.928, C(0.032)));
    r.setProperty('--brd',   tone(src, 0.872, C(0.038)));
    r.setProperty('--muted', tone(src, 0.522, C(0.040)));
    r.setProperty('--ink',   tone(src, 0.192, C(0.038)));
    r.setProperty('--surf',  tone(src, 0.988, C(0.014)));
  }
  // the accent itself keeps all of its chroma
  r.setProperty('--accent', brandHex);
}

function applyAccent(){
  // an inline custom property beats both palette blocks, so one value wins
  // in either theme; the light/dark pair keeps it legible in both
  const a=ACCENTS[prefs.accent]||ACCENTS.dusk;
  const dark=isDark(), brand=dark?a.dark:a.light;
  document.documentElement.style.setProperty('--brand', brand);
  applySurfaces(brand, dark);
  document.querySelectorAll('#sw-accent button, #su-accent button').forEach(b=>
    b.setAttribute('aria-pressed', String(b.dataset.accent===prefs.accent)));
  paintBrowserChrome();
}

/* On a phone the browser paints the strip behind the status bar, and the one
   behind its own toolbar, from the theme-color meta. That was a fixed grey, so
   the accent wash stopped dead at the top and bottom of the page and left a
   band of the wrong colour at each end. Work out what the page actually
   renders up there and hand the browser the same value.

   The mix is done here rather than with color-mix because the computed value
   comes back as color(srgb ...) on some engines, and theme-color wants
   something every browser will parse. Plain hex is that. */
function cssRGB(expr){
  const probe=document.createElement('span');
  probe.style.cssText='position:absolute;left:-9999px;top:0;color:'+expr;
  document.body.appendChild(probe);
  const v=getComputedStyle(probe).color;
  probe.remove();
  let m=v.match(/rgba?\\(\\s*([0-9.]+)[,\\s]+([0-9.]+)[,\\s]+([0-9.]+)/);
  if(m) return [+m[1], +m[2], +m[3]];
  m=v.match(/color\\(srgb\\s+([0-9.]+)\\s+([0-9.]+)\\s+([0-9.]+)/);
  if(m) return [m[1]*255, m[2]*255, m[3]*255];
  return null;
}

function paintBrowserChrome(){
  const m=$('tc');
  if(!m) return;
  const brand=cssRGB('var(--brand)'), bg=cssRGB('var(--bg)');
  if(!brand || !bg) return;
  const k=0.11;                       // about what the wash lands on up there
  const hex=(n)=>('0'+Math.round(n).toString(16)).slice(-2);
  m.setAttribute('content', '#'+[0,1,2].map(i=>hex(bg[i]+(brand[i]-bg[i])*k)).join(''));
}

function buildSwatches(){
  const html=Object.keys(ACCENTS).map(k=>
    '<span class="swatch"><button type="button" data-accent="'+k+'" '+
    'aria-label="'+ACCENTS[k].name+'" aria-pressed="false" '+
    'style="background:'+ACCENTS[k].light+'"></button>'+
    '<span class="swname">'+ACCENTS[k].name+'</span></span>').join('');
  ['sw-accent','su-accent'].forEach(id=>{ const el=$(id); if(el) el.innerHTML=html; });
}



/* ---------- first run ---------------------------------------------------
   Two panes in one card: what the thing does, then what you run. Every answer
   is pre-filled from what the NAS actually has installed, so this is a
   confirmation rather than an interrogation.                              */
let setupStep=0;

function setupNeeded(){
  const su=(prefs.setup||{});
  return su.done!==true;
}

function openSetup(fromMenu){
  $('su-skip').hidden=!!fromMenu;
  $('setup').hidden=false;
  setupGo(0);
}

const SU_PANES=['su-feats','su-look','su-opts'];

function setupGo(step){
  setupStep=step;
  SU_PANES.forEach((id,i)=>{ $(id).hidden = i!==step; });
  [].slice.call($('su-dots').children).forEach((d,i)=>
    d.classList.toggle('on', i===step));
  $('su-back').hidden = step===0;
  $('su-skip').hidden = step!==0 || $('su-skip').dataset.never==='1';

  if(step===0){
    $('su-h').textContent='Welcome to Dashy';
    $('su-lede').textContent='A dashboard for your NAS that finds your apps on its own.';
    $('su-go').textContent='Get started';
  }
  if(step===1){
    $('su-h').textContent='Pick a look';
    $('su-lede').textContent='You can change this any time.';
    $('su-go').textContent='Continue';
    buildSuAccent();
    document.querySelectorAll('#su-theme button').forEach(b=>
      b.setAttribute('aria-pressed', String(b.dataset.theme===prefs.theme)));
  }
  if(step===2){
    const det=(data&&data.detected)||{};
    const su=prefs.setup||{};
    const pick=k=> su[k]!==undefined ? su[k] : !!det[k];
    setSwitch('o-ts', pick('tailscale'));
    setSwitch('o-hx', pick('hexos'));
    $('a-ts').textContent=det.tailscale?'found':'';
    $('a-hx').textContent=det.hexos?'found':'';
    const guess=(data&&data.links&&data.links.truenas) ||
                (location.protocol+'//'+location.hostname+'/');
    $('o-nas').value=su.truenas || guess;
    $('su-h').textContent='What do you use?';
    $('su-lede').textContent='Already filled in from what is installed.';
    $('su-go').textContent='Finish';
  }
}

function buildSuAccent(){
  const el=$('su-accent');
  if(el.children.length) {
    el.querySelectorAll('button').forEach(b=>
      b.setAttribute('aria-pressed', String(b.dataset.accent===prefs.accent)));
    return;
  }
  el.innerHTML=Object.keys(ACCENTS).map(k=>
    '<button type="button" data-accent="'+k+'" aria-label="'+k+'" '+
    'aria-pressed="'+(k===prefs.accent)+'" style="background:'+ACCENTS[k].light+'"></button>'
  ).join('');
}

function setSwitch(id, on){
  const b=$(id); if(b) b.setAttribute('aria-checked', on?'true':'false');
}
function getSwitch(id){
  const b=$(id); return !!b && b.getAttribute('aria-checked')==='true';
}

function finishSetup(skipped){
  const nas=($('o-nas').value||'').trim();
  const tailscale=getSwitch('o-ts');
  const selectedLink=$('su-links') && $('su-links').querySelector('button[aria-pressed="true"]');
  prefs.setup={
    done:true,
    tailscale:tailscale,
    links:tailscale?(selectedLink?selectedLink.dataset.links:'ts'):'lan',
    hexos:getSwitch('o-hx'),
    truenas:/^https?:\\/\\//.test(nas) ? nas : ''
  };
  if(skipped){
    const det=(data&&data.detected)||{};
    prefs.setup.tailscale=!!det.tailscale;
    prefs.setup.links=det.tailscale?'ts':'lan';
    prefs.setup.hexos=!!det.hexos;
    prefs.setup.truenas='';
  }
  savePrefs();
  $('setup').hidden=true;
  if(data) paint(data);
  if(prefs.setup.tailscale) publishAll();
}

/* ---------- settings page ---------------------------------------------- */
function settingsOpen(){ return !$('settings-page').hidden; }
function openSettings(){
  const m=$('settings-page');
  window._settingsScroll=window.scrollY;
  m.hidden=false;
  $('vitals').hidden=true;
  $('editbar').hidden=true;
  window.scrollTo(0,0);
  $('settings').setAttribute('aria-expanded','true');
}
function closeSettings(){
  const m=$('settings-page');
  m.hidden=true;
  $('vitals').hidden=false;
  $('editbar').hidden=false;
  $('settings').setAttribute('aria-expanded','false');
  if(window._settingsScroll!==undefined) window.scrollTo(0,window._settingsScroll);
}

/* ---------- rearrange -------------------------------------------------- */
function setEditing(on){
  document.body.classList.toggle('editing', !!on);
  if(!on) savePrefs();
}
// Sizes a card can be, in the order the button walks through them.
// 4x (two by two) was removed in 1.1.2: nobody used it. A card saved at 4x
// comes back as 2x rather than dropping to normal.
const SIZES=[1,2];
function sizeOf(k){ const n=prefs.size[k]; return SIZES.indexOf(n)>=0 ? n : 1; }
function nextSize(k){ return SIZES[(SIZES.indexOf(sizeOf(k))+1) % SIZES.length]; }

function cleanSize(o){
  const out={};
  for(const k in o){
    if(!knownKey(k)) continue;
    const n=Number(o[k])===4 ? 2 : Number(o[k]);
    if(SIZES.indexOf(n)>=0 && n!==1) out[k]=n;
  }
  return out;
}

// Per-app LAN and tailnet overrides. Older string values are copied to both
// modes on load; blank fields stay absent so each can auto-resolve separately.
function cleanUrls(o){
  const out={};
  for(const k in o){
    if(!/^[a-z0-9][a-z0-9_-]{0,31}$/.test(k)) continue;
    const raw=o[k];
    const fields=typeof raw==='string'?{lan:raw,ts:raw}:raw;
    if(!fields || typeof fields!=='object') continue;
    const entry={};
    for(const mode of ['lan','ts']){
      const v=String(fields[mode]||'').trim();
      if(v && /^https?:\\/\\//i.test(v) && v.length<=300) entry[mode]=v;
    }
    if(Object.keys(entry).length) out[k]=entry;
  }
  return out;
}

// 0.11 moved from a flat "these are wide" list to a size per card. Anyone
// upgrading had the old list and no sizes, and watched their double-width
// cards quietly go back to normal - which is exactly what happened to the
// first person to install it. Read the old shape when the new one is empty.
function migrateSizes(){
  if(prefs.size && Object.keys(prefs.size).length) return;
  if(!Array.isArray(prefs.wide) || !prefs.wide.length) return;
  const out={};
  prefs.wide.forEach(k=>{ if(CARDK.indexOf(k)>=0) out[k]=2; });
  prefs.size=out;
}

function toggleWide(k){
  const n=nextSize(k);
  if(n===1) delete prefs.size[k]; else prefs.size[k]=n;
  // keep the old flat list in step, so a device still on an older Dashy shows
  // a resized card as wide rather than losing the setting entirely
  prefs.wide=CARDK.filter(c=>sizeOf(c)>=2);
  savePrefs(); applyWidths(); applyHidden();
  if(data) drawSparks(data);
}
function toggleCard(k){
  const i=prefs.hidden.indexOf(k);
  if(i>=0) prefs.hidden.splice(i,1); else prefs.hidden.push(k);
  savePrefs(); applyHidden();
}
function resetLayout(){
  prefs.order=CARDK.slice(); prefs.hidden=[]; prefs.wide=[]; prefs.size={};
  savePrefs(); build(); if(data) paint(data);
}

function cardTools(k, label){
  return '<div class="cardtools">'+
      '<span class="ctname">'+(label||'')+'</span>'+
      '<button type="button" class="wbtn" data-wide="'+k+'" aria-label="Change size"></button>'+
    '</div>'+
    '<button type="button" class="xbtn" data-eye="'+k+'" aria-label="Remove"></button>';
}

function cardHTML(c){
  // the one card that answers a question rather than reporting a number
  if(c.kind==='health'){
    return '<div class="cardwrap" data-k="'+c.k+'"><div class="jig">'+
             '<button class="card hcard" data-pop="system">'+
               '<p class="lbl">Health</p>'+
               '<p class="hv" id="v-health">\u2013</p>'+
               '<p class="sub" id="s-health"></p>'+
               '<div class="hlist" id="l-health"></div>'+
               '<p class="hfoot" id="f-health"></p>'+
             '</button>'+cardTools(c.k, c.label)+
           '</div></div>';
  }
  const head = c.dot
    ? '<div class="bigrow"><i class="pdot ok" id="'+c.dot+'"></i>'+
        '<p class="big" id="v-'+c.k+'">\u2013</p></div>'+
      '<p class="sub" id="s-'+c.k+'"></p>'
    : '<p class="big" id="v-'+c.k+'">\u2013</p><p class="sub" id="s-'+c.k+'"></p>';
  const body = c.k==='mem' ? memBody(head) : head;
  const spark = (c.trend || c.k==='mem')
    ? '<div class="sparkwrap'+(c.k==='mem'?' memspark':'')+'"><svg class="spark" id="k-'+c.k+
      '" preserveAspectRatio="none" viewBox="0 0 160 20"></svg></div>' : '';
  const tools=cardTools(c.k, c.label);
  return '<div class="cardwrap" data-k="'+c.k+'">'+
           '<div class="jig">'+
             '<button class="card" data-pop="'+(c.pop||c.k)+'">'+
               '<p class="lbl">'+c.label+'</p>'+body+spark+
             '</button>'+tools+
           '</div>'+
         '</div>';
}

function memBody(head){
  const seg=k=>'<circle class="'+k+'" id="d-'+k+'" cx="18" cy="18" r="15.915"><title></title></circle>';
  const leg=(k,name)=>'<span><u style="background:var(--'+
    (k==='apps'?'accent':k==='cache'?'mem-cache':'faint')+')"></u>'+name+'<b id="lm-'+k+'"></b></span>';
  return '<div class="memrow"><div class="memtxt">'+head+
           '<div class="mleg">'+leg('apps','Apps')+leg('cache','ZFS cache')+leg('free','Free')+'</div>'+
         '</div><svg class="donut" viewBox="0 0 36 36" aria-hidden="true">'+
           seg('apps')+seg('cache')+seg('free')+'</svg></div>';
}

/* Circumference is 100, so a share of memory is a dash length directly. Each
   segment is shortened by G to leave a gap of card surface between them. */
function paintMemDonut(m){
  const apps=(m.apps!==undefined)?m.apps:m.used, arc=m.arc||0;
  const free=Math.max(0, m.total-apps-arc), G=1.6;
  let at=0;
  [['apps',apps,'Apps'],['cache',arc,'ZFS cache'],['free',free,'Free']].forEach(([k,v,name])=>{
    const len=v/m.total*100, c=$('d-'+k), b=$('lm-'+k);
    if(c){
      const dash=Math.max(0, len-G);
      c.style.strokeDasharray=dash.toFixed(2)+' '+(100-dash).toFixed(2);
      c.style.strokeDashoffset=(-(at+G/2)).toFixed(2);
      c.firstChild.textContent=name+' '+ram(v);
    }
    if(b) b.textContent=ram(v);
    at+=len;
  });
}

/* The pools are not known until the first poll, so the Storage section is
   built from the data rather than declared. One card per pool: "2 pools, 60%
   full" hid which pool was the full one, which is the only thing you wanted
   to know. */
function poolCards(){
  return ((data&&data.pools)||[]).map(p=>({
    k:poolKey(p.name), label:esc(p.name), sec:'storage', pop:'store', pool:p.name}));
}
function layoutSig(){
  const vms=((data&&data.vms)||[]).map(v=>v.id+':'+v.state).join(',');
  const pools=((data&&data.pools)||[]).map(p=>p.name).join(',');
  const apps=((data&&data.services)||[]).map(s=>s.name).join(',');
  return pools+'|'+apps+'|'+vms;
}

function build(){
  const by={}; CARDS.forEach(c=>by[c.k]=c);
  // hidden cards stay in the DOM so paint() can keep writing to their ids;
  // CSS hides them. Dropping them would mean guarding every $() in paint().
  // pools first, then the thermometer: which pool is full is the question,
  // how warm the drives are is the footnote
  // A saved order from an older version has no 'health' in it, and anything
  // unknown gets appended - which would put the one card that says whether
  // anything is wrong at the bottom. It leads, always.
  let base=prefs.order.map(k=>by[k]).filter(Boolean);
  CARDS.forEach(c=>{ if(base.indexOf(c)<0) base.push(c); });
  base=base.filter(c=>c.k!=='health');
  if(by.health) base.unshift(by.health);
  const order=base.filter(c=>c.sec!=='storage')
                  .concat(poolCards())
                  .concat(base.filter(c=>c.sec==='storage'));
  $('vitals').innerHTML = SECTIONS.map(([id,title])=>{
    if(id==='apps') return appsSection();
    if(id==='vms') return vmsSection();
    const cards=order.filter(c=>c.sec===id);
    if(!cards.length) return '';
    return '<div class="sec" data-sec="'+id+'"><h2>'+title+'</h2>'+
           '<div class="grid">'+cards.map(cardHTML).join('')+'</div></div>';
  }).join('');
  applyHidden();
  applyWidths();
  builtSig=layoutSig();
  built=true;
}

// One card per VM. A VM is either on or it is not, so the card leads with
// that rather than with a number.
function vmName(v){
  return String((v && v.name) || ('VM ' + (v && v.id))).replace(/_/g, ' ');
}

// The guest OS, for the logo on a VM card. TrueNAS does not record it, so it is
// read from what the box does know: the VM's name, its description, and the
// file names of its install ISO and disks. First match wins, so the specific
// names come before 'linux'. Logos are Simple Icons (CC0) except Windows,
// which Simple Icons does not carry; drawn inline so nothing loads from the web.
// [name, pattern, brand colour, colour is light, 24x24 path]
const VM_OS=[
  ['Home Assistant',/home ?assistant|\\bhaos\\b|\\bhassos\\b/,'#18BCF2',false,'M22.939 10.627 13.061.749a1.505 1.505 0 0 0-2.121 0l-9.879 9.878C.478 11.21 0 12.363 0 13.187v9c0 .826.675 1.5 1.5 1.5h9.227l-4.063-4.062a2.034 2.034 0 0 1-.664.113c-1.13 0-2.05-.92-2.05-2.05s.92-2.05 2.05-2.05 2.05.92 2.05 2.05c0 .233-.041.456-.113.665l3.163 3.163V9.928a2.05 2.05 0 0 1-1.15-1.84c0-1.13.92-2.05 2.05-2.05s2.05.92 2.05 2.05a2.05 2.05 0 0 1-1.15 1.84v8.127l3.146-3.146A2.051 2.051 0 0 1 18 12.239c1.13 0 2.05.92 2.05 2.05s-.92 2.05-2.05 2.05c-.25 0-.488-.047-.709-.13L12.9 20.602v3.088h9.6c.825 0 1.5-.675 1.5-1.5v-9c0-.825-.477-1.977-1.061-2.561z'],
  ['TrueNAS',/truenas|freenas/,'#0095D5',false,'M24 10.049v5.114l-10.949 6.324v-5.114L24 10.049zm-24 0v5.114l10.956 6.324v-5.114L0 10.049zm12.004-.605l-4.433 2.559 4.433 2.559 4.429-2.559-4.429-2.559zm10.952-1.207l-9.905-5.723v5.118l5.473 3.164 4.432-2.559zm-12-.605V2.513L1.044 8.236l4.432 2.555 5.48-3.159z'],
  ['Unraid',/unraid/,'#F15A2C',false,'M11.406 8.528h1.17v6.926h-1.17zM1.17 15.454H0V8.528h1.17zm4.534.828h1.17v2.645h-1.17zm-2.86-2.969h1.169v4.282h-1.17zm5.703 0h1.17v4.282h-1.17zM22.83 8.528H24v6.926h-1.17zm-4.534-.81h-1.17V5.073h1.17zm2.86 2.95h-1.169V6.406h1.17zm-5.72 0h-1.17V6.406h1.17z'],
  ['Proxmox',/proxmox|\\bpve\\b/,'#E57000',false,'M4.928 1.825c-1.09.553-1.09.64-.07 1.78 5.655 6.295 7.004 7.782 7.107 7.782.139.017 7.971-8.542 8.058-8.801.034-.07-.208-.312-.519-.536-.415-.312-.864-.433-1.712-.467-1.59-.104-2.144.242-4.115 2.455-.899 1.003-1.66 1.833-1.66 1.833-.017 0-.76-.813-1.642-1.798S8.473 2.1 8.127 1.91c-.796-.45-2.421-.484-3.2-.086zM1.297 4.367C.45 4.695 0 5.007 0 5.248c0 .121 1.331 1.678 2.94 3.459 1.625 1.78 2.939 3.268 2.939 3.302 0 .035-1.331 1.522-2.94 3.303C1.314 17.11.017 18.683.035 18.822c.086.467 1.504 1.055 2.541 1.055 1.678-.018 2.058-.312 5.603-4.202 1.78-1.954 3.233-3.614 3.233-3.666 0-.069-1.435-1.694-3.199-3.63-2.3-2.508-3.423-3.632-3.96-3.874-.812-.398-2.126-.467-2.956-.138zm18.467.12c-.502.26-1.764 1.505-3.943 3.891-1.763 1.937-3.199 3.562-3.199 3.631 0 .07 1.453 1.712 3.234 3.666 3.544 3.89 3.925 4.184 5.602 4.202 1.038 0 2.455-.588 2.542-1.055.017-.156-1.28-1.712-2.905-3.493-1.608-1.78-2.94-3.285-2.94-3.32 0-.034 1.332-1.539 2.94-3.32C22.72 6.91 24.017 5.352 24 5.214c-.087-.45-1.366-.968-2.473-1.038-.795-.034-1.21.035-1.763.312zM7.954 16.973c-2.144 2.369-3.908 4.374-3.943 4.46-.034.07.208.312.52.537.414.311.864.432 1.711.467 1.574.103 2.161-.26 4.15-2.508.864-.968 1.608-1.78 1.625-1.78s.761.812 1.643 1.798c2.023 2.248 2.559 2.576 4.132 2.49.848-.035 1.297-.156 1.712-.467.311-.225.553-.467.519-.536-.087-.26-7.92-8.819-8.058-8.801-.069 0-1.867 1.954-4.011 4.34z'],
  ['pfSense',/pfsense/,'#212121',false,'M2.013 0C.898 0 0 .929 0 2.044v17.775L3.252 8.27h3.282L6.1 9.785h.063c.186-.217.433-.403.742-.62.31-.216.62-.402.96-.588.342-.186.713-.31 1.116-.433.402-.124.805-.155 1.208-.155.867 0 1.579.154 2.198.433.62.279 1.084.712 1.455 1.239.31.464.5 1.019.593 1.669.006.06.027.135.027.189.062.712-.031 1.518-.28 2.385a8.571 8.571 0 0 1-1.02 2.322 9.885 9.885 0 0 1-1.58 1.95 8.125 8.125 0 0 1-2.044 1.364 5.536 5.536 0 0 1-2.354.495 5.655 5.655 0 0 1-1.982-.34c-.588-.217-.99-.62-1.238-1.177h-.062L2.353 24h19.603A2.042 2.042 0 0 0 24 21.956V4.706c-.093-.03-.186-.06-.248-.092a2.771 2.771 0 0 0-.557-.062c-.557 0-1.022.124-1.394.372-.34.248-.65.743-.867 1.518l-.526 1.826h2.013l.495 1.58-1.3 1.27h-2.014l-2.446 8.67h-3.53l2.446-8.67h-1.455l.805-2.85h1.425l.557-2.044c.185-.619.403-1.238.681-1.795a4.996 4.996 0 0 1 1.053-1.487c.433-.434.99-.775 1.641-1.022.65-.248 1.487-.372 2.447-.372.248 0 .464 0 .712.031A2.082 2.082 0 0 0 21.988 0zm6.565 11.118c-.898 0-1.672.278-2.323.805-.65.526-1.083 1.239-1.331 2.106-.248.867-.217 1.579.155 2.105.31.557.929.805 1.858.805.898 0 1.672-.278 2.322-.805.65-.526 1.115-1.238 1.363-2.105.247-.867.185-1.58-.155-2.106-.34-.527-.991-.805-1.89-.805Z'],
  ['OPNsense',/opnsense/,'#E44A20',false,'M5.25 0v5.25h13.5v13.5H24V7.5L16.5 0Zm13.5 18.75H5.25V5.25H0V16.5L7.5 24h11.25Z'],
  ['OpenWrt',/openwrt/,'#00B5E2',false,'m12 0c-3.906 0-7.4465 1.5949-10.006 4.1543l1.6953 1.6953c2.1278-2.1278 5.069-3.4395 8.3105-3.4395 3.2416 0 6.1833 1.3122 8.3105 3.4395l1.6953-1.6953c-2.56-2.5594-6.0999-4.1543-10.006-4.1543zm0 4.3203c-2.7091 0-5.1685 1.1138-6.9473 2.8926l1.6953 1.6953c1.346-1.346 3.2079-2.1777 5.252-2.1777 2.0435 0 3.9053 0.83172 5.252 2.1777l1.6953-1.6953c-1.7782-1.7782-4.2381-2.8926-6.9473-2.8926zm-7.7285 3.7559c-1.3295 1.6792-2.1113 3.7906-2.1113 6.084 0 5.4189 4.4216 9.8398 9.8398 9.8398 5.4183 0 9.8398-4.4209 9.8398-9.8398 0-2.2934-0.79833-4.4048-2.1113-6.084l-1.7129 1.7129c0.8974 1.2298 1.4297 2.7427 1.4297 4.3711 0 4.1053-3.34 7.4473-7.4453 7.4473-4.1052 0-7.4473-3.342-7.4473-7.4473 0-1.6284 0.53174-3.1413 1.4297-4.3711l-1.7109-1.7129zm7.7285 0.56641c-1.5128 0-2.8928 0.61633-3.9062 1.6133l1.6953 1.6953c1.2219-1.2195 3.2006-1.2195 4.4219 0l1.6953-1.6953c-1.0354-1.0361-2.4416-1.6176-3.9062-1.6133zm0 3.8887c-0.8997 0-1.6289 0.72911-1.6289 1.6289 0 0.8992 0.72921 1.6289 1.6289 1.6289 0.8998 0 1.6289-0.72971 1.6289-1.6289-0.0019-0.8992-0.72971-1.6277-1.6289-1.6289z'],
  ['Pop!_OS',/\\bpop ?!? ?os\\b/,'#48B9C7',true,'M12 0C5.372 0 0 5.373 0 12c0 6.628 5.372 12 12 12 6.627 0 12-5.372 12-12 0-6.627-5.373-12-12-12ZM9.64 2.918c1.091-.026 1.548.229 2.182.635a4.459 4.459 0 0 1 1.902 2.764c.254 1.141.178 2.029-.127 2.664v.05c-.609 1.294-1.622 2.335-3.043 2.842l1.217 3.172c.228.583.432 1.192.254 1.75-.177.558-.989.736-1.572.127-1.116-1.192-4.871-8.702-5.15-9.26-.279-.558-.584-1.016-.584-1.574.026-.837 1.318-1.7 1.953-2.131.634-.431 1.877-1.014 2.968-1.039Zm-.996 2.311c-.789.022-.358 1.669-.197 2.129.178.507.661 1.572 1.193 2.105.127.127.254.229.407.254.152.027.457-.127.584-.33a.932.932 0 0 0 .15-.559 3.232 3.232 0 0 0-.049-1.216c-.228-.787-.711-1.548-1.346-2.055-.127-.102-.279-.229-.457-.279a.901.901 0 0 0-.285-.049Zm8.414 2.027a2.283 2.283 0 0 1 1.588.636c.305.279.33.582.229.963-.102.38-.457 1.194-.736 1.777l-.709 1.344c-1.37 2.435-1.649 2.689-2.03 2.537-.456-.178-.304-2.614.127-5.582.127-.812.329-1.217.557-1.42.171-.152.6-.248.975-.254l-.001-.001Zm-1.859 8.332c.554.011.789.7.656 1.232a.861.861 0 0 1-.379.559c-.203.127-.685.127-.965-.102-.278-.228-.33-.609-.254-.914.076-.304.331-.635.686-.736a.757.757 0 0 1 .256-.039Zm-8.604 2.805h10.809c.52 0 .938.419.938.939v.074c0 .52-.418.94-.938.94H6.595a.936.936 0 0 1-.937-.94v-.074c0-.52.417-.939.937-.939Z'],
  ['Linux Mint',/linux ?mint|\\bmint\\b/,'#86BE43',true,'M5.438 5.906v8.438c0 2.06 1.69 3.75 3.75 3.75h5.625c2.06 0 3.75-1.69 3.75-3.75V9.656a2.827 2.827 0 0 0-2.813-2.812 2.8 2.8 0 0 0-1.875.737A2.8 2.8 0 0 0 12 6.844a2.827 2.827 0 0 0-2.812 2.812v4.688h1.875V9.656c0-.529.408-.937.937-.937s.938.408.938.937v4.688h1.875V9.656c0-.529.408-.937.937-.937s.938.408.938.937v4.688a1.86 1.86 0 0 1-1.875 1.875H9.188a1.86 1.86 0 0 1-1.875-1.875V5.906ZM12 0C5.384 0 0 5.384 0 12s5.384 12 12 12 12-5.384 12-12S18.616 0 12 0m0 1.875A10.11 10.11 0 0 1 22.125 12 10.11 10.11 0 0 1 12 22.125 10.11 10.11 0 0 1 1.875 12 10.11 10.11 0 0 1 12 1.875'],
  ['Kali Linux',/\\bkali\\b/,'#557C94',false,'M12.778 5.943s-1.97-.13-5.327.92c-3.42 1.07-5.36 2.587-5.36 2.587s5.098-2.847 10.852-3.008zm7.351 3.095l.257-.017s-1.468-1.78-4.278-2.648c1.58.642 2.954 1.493 4.021 2.665zm.42.74c.039-.068.166.217.263.337.004.024.01.039-.045.027-.005-.025-.013-.032-.013-.032s-.135-.08-.177-.137c-.041-.057-.049-.157-.028-.195zm3.448 8.479s.312-3.578-5.31-4.403a18.277 18.277 0 0 0-2.524-.187c-4.506.06-4.67-5.197-1.275-5.462 1.407-.116 3.087.643 4.73 1.408-.007.204.002.385.136.552.134.168.648.35.813.445.164.094.691.43 1.014.85.07-.131.654-.512.654-.512s-.14.003-.465-.119c-.326-.122-.713-.49-.722-.511-.01-.022-.015-.055.06-.07.059-.049-.072-.207-.13-.265-.058-.058-.445-.716-.454-.73-.009-.016-.012-.031-.04-.05-.085-.027-.46.04-.46.04s-.575-.283-.774-.893c.003.107-.099.224 0 .469-.3-.127-.558-.344-.762-.88-.12.305 0 .499 0 .499s-.707-.198-.82-.85c-.124.293 0 .469 0 .469s-1.153-.602-3.069-.61c-1.283-.118-1.55-2.374-1.43-2.754 0 0-1.85-.975-5.493-1.406-3.642-.43-6.628-.065-6.628-.065s6.45-.31 11.617 1.783c.176.785.704 2.094.989 2.723-.815.563-1.733 1.092-1.876 2.97-.143 1.878 1.472 3.53 3.474 3.58 1.9.102 3.214.116 4.806.942 1.52.84 2.766 3.4 2.89 5.703.132-1.709-.509-5.383-3.5-6.498 4.181.732 4.549 3.832 4.549 3.832zM12.68 5.663l-.15-.485s-2.484-.441-5.822-.204C3.37 5.211 0 6.38 0 6.38s6.896-1.735 12.68-.717Z'],
  ['Ubuntu',/ubuntu|kubuntu|xubuntu|lubuntu/,'#E95420',false,'M17.61.455a3.41 3.41 0 0 0-3.41 3.41 3.41 3.41 0 0 0 3.41 3.41 3.41 3.41 0 0 0 3.41-3.41 3.41 3.41 0 0 0-3.41-3.41zM12.92.8C8.923.777 5.137 2.941 3.148 6.451a4.5 4.5 0 0 1 .26-.007 4.92 4.92 0 0 1 2.585.737A8.316 8.316 0 0 1 12.688 3.6 4.944 4.944 0 0 1 13.723.834 11.008 11.008 0 0 0 12.92.8zm9.226 4.994a4.915 4.915 0 0 1-1.918 2.246 8.36 8.36 0 0 1-.273 8.303 4.89 4.89 0 0 1 1.632 2.54 11.156 11.156 0 0 0 .559-13.089zM3.41 7.932A3.41 3.41 0 0 0 0 11.342a3.41 3.41 0 0 0 3.41 3.409 3.41 3.41 0 0 0 3.41-3.41 3.41 3.41 0 0 0-3.41-3.41zm2.027 7.866a4.908 4.908 0 0 1-2.915.358 11.1 11.1 0 0 0 7.991 6.698 11.234 11.234 0 0 0 2.422.249 4.879 4.879 0 0 1-.999-2.85 8.484 8.484 0 0 1-.836-.136 8.304 8.304 0 0 1-5.663-4.32zm11.405.928a3.41 3.41 0 0 0-3.41 3.41 3.41 3.41 0 0 0 3.41 3.41 3.41 3.41 0 0 0 3.41-3.41 3.41 3.41 0 0 0-3.41-3.41z'],
  ['Debian',/debian/,'#A81D33',false,'M13.88 12.685c-.4 0 .08.2.601.28.14-.1.27-.22.39-.33a3.001 3.001 0 01-.99.05m2.14-.53c.23-.33.4-.69.47-1.06-.06.27-.2.5-.33.73-.75.47-.07-.27 0-.56-.8 1.01-.11.6-.14.89m.781-2.05c.05-.721-.14-.501-.2-.221.07.04.13.5.2.22M12.38.31c.2.04.45.07.42.12.23-.05.28-.1-.43-.12m.43.12l-.15.03.14-.01V.43m6.633 9.944c.02.64-.2.95-.38 1.5l-.35.181c-.28.54.03.35-.17.78-.44.39-1.34 1.22-1.62 1.301-.201 0 .14-.25.19-.34-.591.4-.481.6-1.371.85l-.03-.06c-2.221 1.04-5.303-1.02-5.253-3.842-.03.17-.07.13-.12.2a3.551 3.552 0 012.001-3.501 3.361 3.362 0 013.732.48 3.341 3.342 0 00-2.721-1.3c-1.18.01-2.281.76-2.651 1.57-.6.38-.67 1.47-.93 1.661-.361 2.601.66 3.722 2.38 5.042.27.19.08.21.12.35a4.702 4.702 0 01-1.53-1.16c.23.33.47.66.8.91-.55-.18-1.27-1.3-1.48-1.35.93 1.66 3.78 2.921 5.261 2.3a6.203 6.203 0 01-2.33-.28c-.33-.16-.77-.51-.7-.57a5.802 5.803 0 005.902-.84c.44-.35.93-.94 1.07-.95-.2.32.04.16-.12.44.44-.72-.2-.3.46-1.24l.24.33c-.09-.6.74-1.321.66-2.262.19-.3.2.3 0 .97.29-.74.08-.85.15-1.46.08.2.18.42.23.63-.18-.7.2-1.2.28-1.6-.09-.05-.28.3-.32-.53 0-.37.1-.2.14-.28-.08-.05-.26-.32-.38-.861.08-.13.22.33.34.34-.08-.42-.2-.75-.2-1.08-.34-.68-.12.1-.4-.3-.34-1.091.3-.25.34-.74.54.77.84 1.96.981 2.46-.1-.6-.28-1.2-.49-1.76.16.07-.26-1.241.21-.37A7.823 7.824 0 0017.702 1.6c.18.17.42.39.33.42-.75-.45-.62-.48-.73-.67-.61-.25-.65.02-1.06 0C15.082.73 14.862.8 13.8.4l.05.23c-.77-.25-.9.1-1.73 0-.05-.04.27-.14.53-.18-.741.1-.701-.14-1.431.03.17-.13.36-.21.55-.32-.6.04-1.44.35-1.18.07C9.6.68 7.847 1.3 6.867 2.22L6.838 2c-.45.54-1.96 1.611-2.08 2.311l-.131.03c-.23.4-.38.85-.57 1.261-.3.52-.45.2-.4.28-.6 1.22-.9 2.251-1.16 3.102.18.27 0 1.65.07 2.76-.3 5.463 3.84 10.776 8.363 12.006.67.23 1.65.23 2.49.25-.99-.28-1.12-.15-2.08-.49-.7-.32-.85-.7-1.34-1.13l.2.35c-.971-.34-.57-.42-1.361-.67l.21-.27c-.31-.03-.83-.53-.97-.81l-.34.01c-.41-.501-.63-.871-.61-1.161l-.111.2c-.13-.21-1.52-1.901-.8-1.511-.13-.12-.31-.2-.5-.55l.14-.17c-.35-.44-.64-1.02-.62-1.2.2.24.32.3.45.33-.88-2.172-.93-.12-1.601-2.202l.15-.02c-.1-.16-.18-.34-.26-.51l.06-.6c-.63-.74-.18-3.102-.09-4.402.07-.54.53-1.1.88-1.981l-.21-.04c.4-.71 2.341-2.872 3.241-2.761.43-.55-.09 0-.18-.14.96-.991 1.26-.7 1.901-.88.7-.401-.6.16-.27-.151 1.2-.3.85-.7 2.421-.85.16.1-.39.14-.52.26 1-.49 3.151-.37 4.562.27 1.63.77 3.461 3.011 3.531 5.132l.08.02c-.04.85.13 1.821-.17 2.711l.2-.42M9.54 13.236l-.05.28c.26.35.47.73.8 1.01-.24-.47-.42-.66-.75-1.3m.62-.02c-.14-.15-.22-.34-.31-.52.08.32.26.6.43.88l-.12-.36m10.945-2.382l-.07.15c-.1.76-.34 1.511-.69 2.212.4-.73.65-1.541.75-2.362M12.45.12c.27-.1.66-.05.95-.12-.37.03-.74.05-1.1.1l.15.02M3.006 5.142c.07.57-.43.8.11.42.3-.66-.11-.18-.1-.42m-.64 2.661c.12-.39.15-.62.2-.84-.35.44-.17.53-.2.83'],
  ['Fedora',/fedora/,'#51A2DA',false,'M12.001 0C5.376 0 .008 5.369.004 11.992H.002v9.287h.002A2.726 2.726 0 0 0 2.73 24h9.275c6.626-.004 11.993-5.372 11.993-11.997C23.998 5.375 18.628 0 12 0zm2.431 4.94c2.015 0 3.917 1.543 3.917 3.671 0 .197.001.395-.03.619a1.002 1.002 0 0 1-1.137.893 1.002 1.002 0 0 1-.842-1.175 2.61 2.61 0 0 0 .013-.337c0-1.207-.987-1.672-1.92-1.672-.934 0-1.775.784-1.777 1.672.016 1.027 0 2.046 0 3.07l1.732-.012c1.352-.028 1.368 2.009.016 1.998l-1.748.013c-.004.826.006.677.002 1.093 0 0 .015 1.01-.016 1.776-.209 2.25-2.124 4.046-4.424 4.046-2.438 0-4.448-1.993-4.448-4.437.073-2.515 2.078-4.492 4.603-4.469l1.409-.01v1.996l-1.409.013h-.007c-1.388.04-2.577.984-2.6 2.47a2.438 2.438 0 0 0 2.452 2.439c1.356 0 2.441-.987 2.441-2.437l-.001-7.557c0-.14.005-.252.02-.407.23-1.848 1.883-3.256 3.754-3.256z'],
  ['Red Hat',/red ?hat|\\brhel\\b/,'#EE0000',false,'M16.009 13.386c1.577 0 3.86-.326 3.86-2.202a1.765 1.765 0 0 0-.04-.431l-.94-4.08c-.216-.898-.406-1.305-1.982-2.093-1.223-.625-3.888-1.658-4.676-1.658-.733 0-.947.946-1.822.946-.842 0-1.467-.706-2.255-.706-.757 0-1.25.515-1.63 1.576 0 0-1.06 2.99-1.197 3.424a.81.81 0 0 0-.028.245c0 1.162 4.577 4.974 10.71 4.974m4.101-1.435c.218 1.032.218 1.14.218 1.277 0 1.765-1.984 2.745-4.593 2.745-5.895.004-11.06-3.451-11.06-5.734a2.326 2.326 0 0 1 .19-.925C2.746 9.415 0 9.794 0 12.217c0 3.969 9.405 8.861 16.851 8.861 5.71 0 7.149-2.582 7.149-4.62 0-1.605-1.387-3.425-3.887-4.512'],
  ['CentOS',/centos/,'#262577',false,'M12.076.066L8.883 3.28H3.348v5.434L0 12.01l3.349 3.298v5.39h5.374l3.285 3.236 3.285-3.236h5.43v-5.374L24 12.026l-3.232-3.252V3.321H15.31zm0 .749l2.49 2.506h-1.69v6.441l-.8.805-.81-.815V3.28H9.627zm-8.2 2.991h4.483L6.485 5.692l4.253 4.279v.654H9.94L5.674 6.423l-1.798 1.77zm5.227 0h1.635v5.415l-3.509-3.53zm4.302.043h1.687l1.83 1.842-3.517 3.539zm2.431 0h4.404v4.394l-1.83-1.842-4.241 4.267h-.764v-.69l4.261-4.287zm2.574 3.3l1.83 1.843v1.676h-5.327zm-12.735.013l3.515 3.462H3.876v-1.69zM3.348 9.454v1.697h6.377l.871.858-.782.77H3.35v1.786L.753 12.01zm17.42.068l2.488 2.503-2.533 2.55v-1.796h-6.41l-.75-.754.825-.83h6.38zm-9.502.978l.81.815.186-.188.614-.618v.686h.768l-.825.83.75.754h-.719v.808l-.842-.83-.741.73v-.707h-.7l.781-.77-.188-.186-.682-.672h.788zm-7.39 2.807h5.402l-3.603 3.55-1.798-1.772zm6.154 0h.708v.7l-4.404 4.338 1.852 1.824h-4.31v-4.342l1.798 1.77zm3.348 0h.715l4.317 4.343.186-.187 1.599-1.61v4.316h-4.366l1.853-1.825-.188-.185-4.116-4.054zm1.46 0h5.357v1.798l-1.785 1.796zm-2.83.191l.842.829v6.37h1.691l-2.532 2.495-2.533-2.495h1.79V14.23zm-1.27 1.251v5.42H8.939l-1.852-1.823zm2.64.097l3.552 3.499-1.853 1.825h-1.7z'],
  ['Rocky Linux',/\\brocky\\b/,'#10B981',false,'M23.332 15.957c.433-1.239.668-2.57.668-3.957 0-6.627-5.373-12-12-12S0 5.373 0 12c0 3.28 1.315 6.251 3.447 8.417L15.62 8.245l3.005 3.005zm-2.192 3.819l-5.52-5.52L6.975 22.9c1.528.706 3.23 1.1 5.025 1.1 3.661 0 6.94-1.64 9.14-4.224z'],
  ['AlmaLinux',/\\balma ?linux\\b|\\balma\\b/,'#000000',false,'M23.994 15.133c.079 1.061-.668 1.927-1.69 2.005a1.8 1.8 0 0 1-1.928-1.651c-.078-1.062.63-1.849 1.691-1.967 1.023-.078 1.849.59 1.927 1.613zm-12.623 4.955c-.944 0-1.73.786-1.73 1.809 0 1.14.747 1.848 1.887 1.848.904-.04 1.691-.865 1.691-1.809 0-.983-.904-1.848-1.848-1.848zm1.061-9.675c-.039-.865-.078-1.73.08-2.556.156-.944.314-1.887.904-2.674.707-.983 1.809-.944 2.399.118.314.511.432 1.062.471 1.652 0 .354.158.432.472.393.944-.157 1.888-.157 2.792.197.118.039.236.118.394 0 .314-.276.393-1.652.196-2.006-.354-.63-.904-.55-1.455-.55-.629.039-1.18-.158-1.612-.67-.393-.471-.511-1.06-.59-1.65-.04-.276-.079-.512-.315-.709-.55-.55-1.809-.432-2.477.118-2.556 2.045-2.989 5.467-1.534 8.18.04.118.118.236.275.157zm7.984 3.658c.354-.511.865-.747 1.415-.983a.973.973 0 0 0 .59-.472c.354-.669-.078-1.81-.747-2.36-2.595-2.006-5.938-1.612-8.18.433-.118.078-.157.196-.078.314.786-.236 1.612-.472 2.477-.51.905-.08 1.848-.158 2.753.235 1.14.472 1.337 1.534.472 2.36-.393.393-.905.668-1.455.825-.315.08-.354.236-.236.551.354.865.59 1.77.472 2.753-.04.157-.079.275.078.393.354.236 1.691 0 1.967-.275.511-.472.314-1.023.196-1.534-.157-.63-.078-1.219.276-1.73zm-7.197-2.045c-.118-.079-.197-.118-.315 0 .472.708.905 1.455 1.259 2.241.314.866.668 1.73.55 2.714-.118 1.18-1.1 1.69-2.123 1.101-.511-.275-.905-.669-1.22-1.14-.196-.276-.393-.276-.629-.08-.747.63-1.533 1.102-2.516 1.26-.158 0-.315 0-.394.157-.118.393.472 1.612.826 1.809.59.354 1.062 0 1.534-.276.55-.314 1.101-.432 1.73-.236.59.197.983.63 1.337 1.102.158.196.315.353.63.432.747.197 1.77-.59 2.084-1.376 1.18-3.028-.157-6.135-2.753-7.708zm-2.556 2.438c.472-.669.826-1.416.983-2.202-.157-.04-.197.04-.315.078-.904.944-1.848 1.849-3.067 2.478-.472.236-.983.433-1.534.433-.865 0-1.376-.551-1.298-1.416a2.92 2.92 0 0 1 .787-1.849c.236-.275.236-.432-.04-.668-.786-.55-1.494-1.22-1.848-2.124-.078-.275-.275-.275-.51-.157a4.293 4.293 0 0 0-.434.236c-1.022.63-1.14 1.416-.275 2.28.63.63.944 1.338.708 2.203-.118.433-.354.747-.63 1.101a.95.95 0 0 0-.235.787c.079.747.826 1.494 1.73 1.573 2.517.236 4.562-.63 5.978-2.753zm-4.68-5.152c1.376 1.18 3.067 1.455 4.837 1.377.157 0 .315 0 .354-.118.04-.197-.157-.197-.275-.236-.826-.354-1.691-.63-2.438-1.14S6.848 8.25 6.534 7.266c-.236-.747.078-1.415.825-1.651.669-.236 1.337-.236 1.967 0 .393.157.55.078.629-.354.118-.747.354-1.455.826-2.085.55-.786.55-.865-.354-1.376-.04 0-.04-.04-.079-.04-.865-.471-1.534-.196-1.848.709-.472 1.376-1.377 1.887-2.832 1.612-.196-.04-.393-.079-.472-.079-.747.118-1.18.55-1.297 1.14-.158 1.81.786 3.107 2.084 4.17zm-2.32 3.658c-.079-.944-1.023-1.652-2.045-1.534-.905.079-1.691 1.022-1.613 1.966.08.983 1.023 1.77 1.967 1.652 1.14-.079 1.73-1.18 1.69-2.084zm15.18-8.298c.943-.079 1.73-.983 1.651-1.927-.078-.983-1.022-1.77-2.005-1.691-1.023.079-1.73.983-1.652 1.966s.983 1.73 2.006 1.652zm-12.27-.826c1.062-.157 1.77-1.023 1.652-2.045C8.107.897 7.163.149 6.18.267c-1.062.118-1.691.944-1.573 2.085.118.865 1.061 1.612 1.966 1.494z'],
  ['Arch Linux',/arch ?linux|\\barch\\b/,'#1793D1',false,'M11.39.605C10.376 3.092 9.764 4.72 8.635 7.132c.693.734 1.543 1.589 2.923 2.554-1.484-.61-2.496-1.224-3.252-1.86C6.86 10.842 4.596 15.138 0 23.395c3.612-2.085 6.412-3.37 9.021-3.862a6.61 6.61 0 01-.171-1.547l.003-.115c.058-2.315 1.261-4.095 2.687-3.973 1.426.12 2.534 2.096 2.478 4.409a6.52 6.52 0 01-.146 1.243c2.58.505 5.352 1.787 8.914 3.844-.702-1.293-1.33-2.459-1.929-3.57-.943-.73-1.926-1.682-3.933-2.713 1.38.359 2.367.772 3.137 1.234-6.09-11.334-6.582-12.84-8.67-17.74zM22.898 21.36v-.623h-.234v-.084h.562v.084h-.234v.623h.331v-.707h.142l.167.5.034.107a2.26 2.26 0 01.038-.114l.17-.493H24v.707h-.091v-.593l-.206.593h-.084l-.205-.602v.602h-.091'],
  ['Manjaro',/manjaro/,'#35BFA4',true,'M2.182 0A2.177 2.177 0 0 0 0 2.182v19.636C0 23.027.973 24 2.182 24h4.363V6.545h8.728V0Zm15.273 0v24h4.363A2.177 2.177 0 0 0 24 21.818V2.182A2.177 2.177 0 0 0 21.818 0ZM8.727 8.727V24h6.546V8.727Z'],
  ['Alpine Linux',/\\balpine\\b/,'#0D597F',false,'M5.998 1.607L0 12l5.998 10.393h12.004L24 12 18.002 1.607H5.998zM9.965 7.12L12.66 9.9l1.598 1.595.002-.002 2.41 2.363c-.2.14-.386.252-.563.344a3.756 3.756 0 01-.496.217 2.702 2.702 0 01-.425.111c-.131.023-.25.034-.358.034-.13 0-.242-.014-.338-.034a1.317 1.317 0 01-.24-.072.95.95 0 01-.2-.113l-1.062-1.092-3.039-3.041-1.1 1.053-3.07 3.072a.974.974 0 01-.2.111 1.274 1.274 0 01-.237.073c-.096.02-.209.033-.338.033-.108 0-.227-.009-.358-.031a2.7 2.7 0 01-.425-.114 3.748 3.748 0 01-.496-.217 5.228 5.228 0 01-.563-.343l6.803-6.727zm4.72.785l4.579 4.598 1.382 1.353a5.24 5.24 0 01-.564.344 3.73 3.73 0 01-.494.217 2.697 2.697 0 01-.426.111c-.13.023-.251.034-.36.034-.129 0-.241-.014-.337-.034a1.285 1.285 0 01-.385-.146c-.033-.02-.05-.036-.053-.04l-1.232-1.218-2.111-2.111-.334.334L12.79 9.8l1.896-1.897zm-5.966 4.12v2.529a2.128 2.128 0 01-.356-.035 2.765 2.765 0 01-.422-.116 3.708 3.708 0 01-.488-.214 5.217 5.217 0 01-.555-.34l1.82-1.825Z'],
  ['openSUSE',/suse/,'#73BA25',true,'M10.724 0a12 12 0 0 0-9.448 4.623c1.464.391 2.5.727 2.81.832.005-.19.037-1.893.037-1.893s.004-.04.025-.06c.026-.026.065-.018.065-.018.385.056 8.602 1.274 12.066 3.292.427.25.638.517.902.786.958.99 2.223 5.108 2.359 5.957.005.033-.036.07-.054.083a5.177 5.177 0 0 1-.313.228c-.82.55-2.708 1.872-5.13 1.656-2.176-.193-5.018-1.44-8.445-3.699.336.79.668 1.58 1 2.371.497.258 5.287 2.7 7.651 2.651 1.904-.04 3.941-.968 4.756-1.458 0 0 .179-.108.257-.048.085.066.061.167.041.27-.05.234-.164.66-.242.863l-.065.165c-.093.25-.183.482-.356.625-.48.436-1.246.784-2.446 1.305-1.855.812-4.865 1.328-7.66 1.31-1.001-.022-1.968-.133-2.817-.232-1.743-.197-3.161-.357-4.026.269A12 12 0 0 0 10.724 24a12 12 0 0 0 12-12 12 12 0 0 0-12-12zM13.4 6.963a3.503 3.503 0 0 0-2.521.942 3.498 3.498 0 0 0-1.114 2.449 3.528 3.528 0 0 0 3.39 3.64 3.48 3.48 0 0 0 2.524-.946 3.504 3.504 0 0 0 1.114-2.446 3.527 3.527 0 0 0-3.393-3.64zm-.03 1.035a2.458 2.458 0 0 1 2.368 2.539 2.43 2.43 0 0 1-.774 1.706 2.456 2.456 0 0 1-1.762.659 2.461 2.461 0 0 1-2.364-2.542c.02-.655.3-1.26.777-1.707a2.419 2.419 0 0 1 1.756-.655zm.402 1.23c-.602 0-1.087.325-1.087.727 0 .4.485.725 1.087.725.6 0 1.088-.326 1.088-.725 0-.402-.487-.726-1.088-.726Z'],
  ['NixOS',/\\bnixos\\b/,'#5277C3',false,'M7.352 1.592l-1.364.002L5.32 2.75l1.557 2.713-3.137-.008-1.32 2.34H14.11l-1.353-2.332-3.192-.006-2.214-3.865zm6.175 0l-2.687.025 5.846 10.127 1.341-2.34-1.59-2.765 2.24-3.85-.683-1.182h-1.336l-1.57 2.705-1.56-2.72zm6.887 4.195l-5.846 10.125 2.696-.008 1.601-2.76 4.453.016.682-1.183-.666-1.157-3.13-.008L21.778 8.1l-1.365-2.313zM9.432 8.086l-2.696.008-1.601 2.76-4.453-.016L0 12.02l.666 1.157 3.13.008-1.575 2.71 1.365 2.315L9.432 8.086zM7.33 12.25l-.006.01-.002-.004-1.342 2.34 1.59 2.765-2.24 3.85.684 1.182H7.35l.004-.006h.001l1.567-2.698 1.558 2.72 2.688-.026-.004-.006h.01L7.33 12.25zm2.55 3.93l1.354 2.332 3.192.006 2.215 3.865 1.363-.002.668-1.156-1.557-2.713 3.137.008 1.32-2.34H9.881Z'],
  ['Gentoo',/gentoo/,'#54487A',false,'M9.94 0a7.31 7.31 0 00-1.26.116c-4.344.795-7.4 4.555-7.661 7.031-.126 1.215.53 2.125.89 2.526.977 1.085 2.924 1.914 4.175 2.601-1.81 1.543-2.64 2.296-3.457 3.154C1.403 16.712.543 18.125.54 19.138c0 .325-.053 1.365.371 2.187.16.309.613 1.338 1.98 2.109.874.494 2.119.675 3.337.501 3.772-.538 8.823-3.737 12.427-6.716 2.297-1.9 3.977-3.739 4.462-4.644.39-.731.434-2.043.207-2.866-.645-2.337-5.887-7.125-10.172-9.051A7.824 7.824 0 009.94 0zm-.008.068a7.4 7.4 0 013.344.755c3.46 1.7 9.308 6.482 9.739 8.886.534 2.972-9.931 11.017-16.297 12.272-2.47.485-4.576.618-5.537-1.99-.832-2.262.783-3.916 3.16-6.09a92.546 92.546 0 012.96-2.576c.065-.069-5.706-2.059-5.89-4.343C1.221 4.634 4.938.3 9.697.076c.08-.004.157-.007.235-.008zm-.112.52a5.647 5.647 0 00-.506.032c-2.337.245-2.785.547-4.903 2.149-.71.537-2.016 1.844-2.35 3.393-.128.59.024 1.1.448 1.458 1.36 1.144 3.639 2.072 5.509 2.97.547.263.185.74-.698 1.505-2.227 1.928-5.24 4.276-5.45 6.066-.099.842.19 1.988 1.213 2.574 1.195.685 3.676.238 5.333-.379 2.422-.902 5.602-2.892 8.127-4.848 2.625-2.034 5.067-4.617 5.188-5.038.148-.517.133-.996-.154-1.546-.448-.862-1.049-1.503-1.694-2.22-1.732-1.825-3.563-3.43-5.754-4.658C12.694 1.242 11.417.564 9.82.588zm1.075 3.623c.546 0 1.176.173 1.853.5 1.688.817 3.422 2.961-.015 4.195-.935.336-3.9-.824-3.81-2.407.09-1.57.854-2.289 1.972-2.288zm.285 1.367c-.317-.002-.575.079-.694.263-.557.861-.303 1.472.212 1.862.192-.457 2.156.043 2.148.472a.32.32 0 00.055-.032c1.704-1.282-.472-2.557-1.72-2.565z'],
  ['FreeBSD',/freebsd/,'#AB2B28',false,'M23.682 2.406c-.001-.149-.097-.187-.24-.189h-.25v.659h.108v-.282h.102l.17.282h.122l-.184-.29c.102-.012.175-.065.172-.18zm-.382.096v-.193h.13c.06-.002.145.011.143.089.005.09-.08.107-.153.103h-.12zM21.851 1.49c1.172 1.171-2.077 6.319-2.626 6.869-.549.548-1.944.044-3.115-1.128-1.172-1.171-1.676-2.566-1.127-3.115.549-.55 5.697-3.798 6.868-2.626zM1.652 6.61C.626 4.818-.544 2.215.276 1.395c.81-.81 3.355.319 5.144 1.334A11.003 11.003 0 0 0 1.652 6.61zm18.95.418a10.584 10.584 0 0 1 1.368 5.218c0 5.874-4.762 10.636-10.637 10.636C5.459 22.882.697 18.12.697 12.246.697 6.371 5.459 1.61 11.333 1.61c1.771 0 3.441.433 4.909 1.199-.361.201-.69.398-.969.574-.428-.077-.778-.017-.998.202-.402.402-.269 1.245.263 2.2.273.539.701 1.124 1.25 1.674.103.104.208.202.315.297 1.519 1.446 3.205 2.111 3.829 1.486.267-.267.297-.728.132-1.287.167-.27.35-.584.538-.927zm2.814-5.088c-.322 0-.584.266-.584.595s.261.595.584.595c.323 0 .584-.266.584-.595s-.261-.595-.584-.595zm0 1.087c-.252 0-.457-.22-.457-.492s.204-.492.457-.492c.252 0 .457.22.457.492s-.204.492-.457.492z'],
  ['Android',/android/,'#3DDC84',true,'M18.4395 5.5586c-.675 1.1664-1.352 2.3318-2.0274 3.498-.0366-.0155-.0742-.0286-.1113-.043-1.8249-.6957-3.484-.8-4.42-.787-1.8551.0185-3.3544.4643-4.2597.8203-.084-.1494-1.7526-3.021-2.0215-3.4864a1.1451 1.1451 0 0 0-.1406-.1914c-.3312-.364-.9054-.4859-1.379-.203-.475.282-.7136.9361-.3886 1.5019 1.9466 3.3696-.0966-.2158 1.9473 3.3593.0172.031-.4946.2642-1.3926 1.0177C2.8987 12.176.452 14.772 0 18.9902h24c-.119-1.1108-.3686-2.099-.7461-3.0683-.7438-1.9118-1.8435-3.2928-2.7402-4.1836a12.1048 12.1048 0 0 0-2.1309-1.6875c.6594-1.122 1.312-2.2559 1.9649-3.3848.2077-.3615.1886-.7956-.0079-1.1191a1.1001 1.1001 0 0 0-.8515-.5332c-.5225-.0536-.9392.3128-1.0488.5449zm-.0391 8.461c.3944.5926.324 1.3306-.1563 1.6503-.4799.3197-1.188.0985-1.582-.4941-.3944-.5927-.324-1.3307.1563-1.6504.4727-.315 1.1812-.1086 1.582.4941zM7.207 13.5273c.4803.3197.5506 1.0577.1563 1.6504-.394.5926-1.1038.8138-1.584.4941-.48-.3197-.5503-1.0577-.1563-1.6504.4008-.6021 1.1087-.8106 1.584-.4941z'],
  ['Apple',/mac ?os|\\bosx\\b|sonoma|sequoia|ventura|monterey|tahoe/,'#000000',false,'M12.152 6.896c-.948 0-2.415-1.078-3.96-1.04-2.04.027-3.91 1.183-4.961 3.014-2.117 3.675-.546 9.103 1.519 12.09 1.013 1.454 2.208 3.09 3.792 3.039 1.52-.065 2.09-.987 3.935-.987 1.831 0 2.35.987 3.96.948 1.637-.026 2.676-1.48 3.676-2.948 1.156-1.688 1.636-3.325 1.662-3.415-.039-.013-3.182-1.221-3.22-4.857-.026-3.04 2.48-4.494 2.597-4.559-1.429-2.09-3.623-2.324-4.39-2.376-2-.156-3.675 1.09-4.61 1.09zM15.53 3.83c.843-1.012 1.4-2.427 1.245-3.83-1.207.052-2.662.805-3.532 1.818-.78.896-1.454 2.338-1.273 3.714 1.338.104 2.715-.688 3.559-1.701'],
  ['Windows',/windows|\\bwin ?(7|8|10|11|20\\d\\d)\\b/,'#0078D4',false,'M3 3h8.5v8.5H3zM12.5 3H21v8.5h-8.5zM3 12.5h8.5V21H3zM12.5 12.5H21V21h-8.5z'],
  ['Linux',/linux/,'#FCC624',true,'M12.504 0c-.155 0-.315.008-.48.021-4.226.333-3.105 4.807-3.17 6.298-.076 1.092-.3 1.953-1.05 3.02-.885 1.051-2.127 2.75-2.716 4.521-.278.832-.41 1.684-.287 2.489a.424.424 0 00-.11.135c-.26.268-.45.6-.663.839-.199.199-.485.267-.797.4-.313.136-.658.269-.864.68-.09.189-.136.394-.132.602 0 .199.027.4.055.536.058.399.116.728.04.97-.249.68-.28 1.145-.106 1.484.174.334.535.47.94.601.81.2 1.91.135 2.774.6.926.466 1.866.67 2.616.47.526-.116.97-.464 1.208-.946.587-.003 1.23-.269 2.26-.334.699-.058 1.574.267 2.577.2.025.134.063.198.114.333l.003.003c.391.778 1.113 1.132 1.884 1.071.771-.06 1.592-.536 2.257-1.306.631-.765 1.683-1.084 2.378-1.503.348-.199.629-.469.649-.853.023-.4-.2-.811-.714-1.376v-.097l-.003-.003c-.17-.2-.25-.535-.338-.926-.085-.401-.182-.786-.492-1.046h-.003c-.059-.054-.123-.067-.188-.135a.357.357 0 00-.19-.064c.431-1.278.264-2.55-.173-3.694-.533-1.41-1.465-2.638-2.175-3.483-.796-1.005-1.576-1.957-1.56-3.368.026-2.152.236-6.133-3.544-6.139zm.529 3.405h.013c.213 0 .396.062.584.198.19.135.33.332.438.533.105.259.158.459.166.724 0-.02.006-.04.006-.06v.105a.086.086 0 01-.004-.021l-.004-.024a1.807 1.807 0 01-.15.706.953.953 0 01-.213.335.71.71 0 00-.088-.042c-.104-.045-.198-.064-.284-.133a1.312 1.312 0 00-.22-.066c.05-.06.146-.133.183-.198.053-.128.082-.264.088-.402v-.02a1.21 1.21 0 00-.061-.4c-.045-.134-.101-.2-.183-.333-.084-.066-.167-.132-.267-.132h-.016c-.093 0-.176.03-.262.132a.8.8 0 00-.205.334 1.18 1.18 0 00-.09.4v.019c.002.089.008.179.02.267-.193-.067-.438-.135-.607-.202a1.635 1.635 0 01-.018-.2v-.02a1.772 1.772 0 01.15-.768c.082-.22.232-.406.43-.533a.985.985 0 01.594-.2zm-2.962.059h.036c.142 0 .27.048.399.135.146.129.264.288.344.465.09.199.14.4.153.667v.004c.007.134.006.2-.002.266v.08c-.03.007-.056.018-.083.024-.152.055-.274.135-.393.2.012-.09.013-.18.003-.267v-.015c-.012-.133-.04-.2-.082-.333a.613.613 0 00-.166-.267.248.248 0 00-.183-.064h-.021c-.071.006-.13.04-.186.132a.552.552 0 00-.12.27.944.944 0 00-.023.33v.015c.012.135.037.2.08.334.046.134.098.2.166.268.01.009.02.018.034.024-.07.057-.117.07-.176.136a.304.304 0 01-.131.068 2.62 2.62 0 01-.275-.402 1.772 1.772 0 01-.155-.667 1.759 1.759 0 01.08-.668 1.43 1.43 0 01.283-.535c.128-.133.26-.2.418-.2zm1.37 1.706c.332 0 .733.065 1.216.399.293.2.523.269 1.052.468h.003c.255.136.405.266.478.399v-.131a.571.571 0 01.016.47c-.123.31-.516.643-1.063.842v.002c-.268.135-.501.333-.775.465-.276.135-.588.292-1.012.267a1.139 1.139 0 01-.448-.067 3.566 3.566 0 01-.322-.198c-.195-.135-.363-.332-.612-.465v-.005h-.005c-.4-.246-.616-.512-.686-.71-.07-.268-.005-.47.193-.6.224-.135.38-.271.483-.336.104-.074.143-.102.176-.131h.002v-.003c.169-.202.436-.47.839-.601.139-.036.294-.065.466-.065zm2.8 2.142c.358 1.417 1.196 3.475 1.735 4.473.286.534.855 1.659 1.102 3.024.156-.005.33.018.513.064.646-1.671-.546-3.467-1.089-3.966-.22-.2-.232-.335-.123-.335.59.534 1.365 1.572 1.646 2.757.13.535.16 1.104.021 1.67.067.028.135.06.205.067 1.032.534 1.413.938 1.23 1.537v-.043c-.06-.003-.12 0-.18 0h-.016c.151-.467-.182-.825-1.065-1.224-.915-.4-1.646-.336-1.77.465-.008.043-.013.066-.018.135-.068.023-.139.053-.209.064-.43.268-.662.669-.793 1.187-.13.533-.17 1.156-.205 1.869v.003c-.02.334-.17.838-.319 1.35-1.5 1.072-3.58 1.538-5.348.334a2.645 2.645 0 00-.402-.533 1.45 1.45 0 00-.275-.333c.182 0 .338-.03.465-.067a.615.615 0 00.314-.334c.108-.267 0-.697-.345-1.163-.345-.467-.931-.995-1.788-1.521-.63-.4-.986-.87-1.15-1.396-.165-.534-.143-1.085-.015-1.645.245-1.07.873-2.11 1.274-2.763.107-.065.037.135-.408.974-.396.751-1.14 2.497-.122 3.854a8.123 8.123 0 01.647-2.876c.564-1.278 1.743-3.504 1.836-5.268.048.036.217.135.289.202.218.133.38.333.59.465.21.201.477.335.876.335.039.003.075.006.11.006.412 0 .73-.134.997-.268.29-.134.52-.334.74-.4h.005c.467-.135.835-.402 1.044-.7zm2.185 8.958c.037.6.343 1.245.882 1.377.588.134 1.434-.333 1.791-.765l.211-.01c.315-.007.577.01.847.268l.003.003c.208.199.305.53.391.876.085.4.154.78.409 1.066.486.527.645.906.636 1.14l.003-.007v.018l-.003-.012c-.015.262-.185.396-.498.595-.63.401-1.746.712-2.457 1.57-.618.737-1.37 1.14-2.036 1.191-.664.053-1.237-.2-1.574-.898l-.005-.003c-.21-.4-.12-1.025.056-1.69.176-.668.428-1.344.463-1.897.037-.714.076-1.335.195-1.814.12-.465.308-.797.641-.984l.045-.022zm-10.814.049h.01c.053 0 .105.005.157.014.376.055.706.333 1.023.752l.91 1.664.003.003c.243.533.754 1.064 1.189 1.637.434.598.77 1.131.729 1.57v.006c-.057.744-.48 1.148-1.125 1.294-.645.135-1.52.002-2.395-.464-.968-.536-2.118-.469-2.857-.602-.369-.066-.61-.2-.723-.4-.11-.2-.113-.602.123-1.23v-.004l.002-.003c.117-.334.03-.752-.027-1.118-.055-.401-.083-.71.043-.94.16-.334.396-.4.69-.533.294-.135.64-.202.915-.47h.002v-.002c.256-.268.445-.601.668-.838.19-.201.38-.336.663-.336zm7.159-9.074c-.435.201-.945.535-1.488.535-.542 0-.97-.267-1.28-.466-.154-.134-.28-.268-.373-.335-.164-.134-.144-.333-.074-.333.109.016.129.134.199.2.096.066.215.2.36.333.292.2.68.467 1.167.467.485 0 1.053-.267 1.398-.466.195-.135.445-.334.648-.467.156-.136.149-.267.279-.267.128.016.034.134-.147.332a8.097 8.097 0 01-.69.468zm-1.082-1.583V5.64c-.006-.02.013-.042.029-.05.074-.043.18-.027.26.004.063 0 .16.067.15.135-.006.049-.085.066-.135.066-.055 0-.092-.043-.141-.068-.052-.018-.146-.008-.163-.065zm-.551 0c-.02.058-.113.049-.166.066-.047.025-.086.068-.14.068-.05 0-.13-.02-.136-.068-.01-.066.088-.133.15-.133.08-.031.184-.047.259-.005.019.009.036.03.03.05v.02h.003z']
];
function vmOS(v){
  const t=[v.name,v.description].concat(v.media||[]).join(' ').toLowerCase().replace(/[_.-]+/g,' ');
  return VM_OS.find(o=>o[1].test(t))||null;
}

function vmIcon(v){
  const os=vmOS(v);
  if(os) return '<span class="vmico'+(os[3]?' lt':'')+'" style="background:'+os[2]+'" '+
      'title="'+esc(os[0])+'" aria-hidden="true">'+
      '<svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="'+os[4]+'"/></svg></span>';
  const h=hueOf(vmName(v)||'vm');
  return '<span class="vmico" style="background:hsl('+h+' 42% 46%)" aria-hidden="true">'+
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '+
         'stroke-linecap="round" stroke-linejoin="round">'+
      '<rect x="2.5" y="4" width="19" height="12" rx="2"/>'+
      '<path d="M8 20h8M12 16v4"/>'+
    '</svg></span>';
}

function vmsSection(){
  const list=(data&&data.vms)||[];
  if(!list.length) return '';
  return '<div class="sec" data-sec="vms"><h2>Virtual machines</h2><div class="grid">'+
    list.map(v=>{
      const run=(v.state||'').toUpperCase()==='RUNNING';
      const sub=[v.cpus?v.cpus+(v.cpus===1?' cpu':' cpus'):'',
                 v.memory?ram(v.memory*1024*1024):''].filter(Boolean).join(' \u00b7 ');
      return '<div class="cardwrap" data-k="vm-'+v.id+'"'+(run?'':' data-state="warn"')+'>'+
        '<div class="jig"><button class="card vmcard" data-pop="vm-'+v.id+'">'+
          '<p class="lbl vmlbl">'+vmIcon(v)+
            '<span>'+esc(vmName(v))+'</span></p>'+
          '<p class="big">'+(run?'Running':esc(title1(v.state||'Stopped')))+'</p>'+
          '<p class="sub">'+esc(sub)+'</p>'+
        '</button></div></div>';
    }).join('')+'</div></div>';
}

function title1(s){ s=String(s||'').toLowerCase(); return s.charAt(0).toUpperCase()+s.slice(1); }

// The sheet is where the power controls live, so they cannot be hit by a
// stray tap on the dashboard.
function vmSheet(id){
  const v=((data&&data.vms)||[]).find(x=>String(x.id)===String(id));
  if(!v) return ['Virtual machine','<div class="row"><span class="t2">Gone.</span></div>'];
  const run=(v.state||'').toUpperCase()==='RUNNING';
  const st=(data&&data.updates&&data.updates._vm)||null;
  const busy = st && st.state==='running' && (Date.now()/1000 - (st.t||0)) < 300;

  const rows=
    srow(run?'ok':'warn', run?'Running':title1(v.state||'Stopped'),
         (v.autostart?'Starts with the server':'Does not start automatically'))+
    '<div class="row"><span class="l"><span class="t1">Allocated</span>'+
      '<span class="t2">'+(v.cpus||'?')+' cpu'+(v.cpus===1?'':'s')+
      (v.memory?' \u00b7 '+ram(v.memory*1024*1024)+' memory':'')+'</span></span></div>'+
    (v.usb&&v.usb.length
      ? '<div class="row"><span class="l"><span class="t1">USB passed through</span>'+
        '<span class="t2">'+v.usb.map(esc).join(', ')+'</span></span></div>' : '')+
    (v.description
      ? '<div class="row"><span class="t2 wrap">'+esc(v.description)+'</span></div>' : '')+
    (st && (Date.now()/1000-(st.t||0) < 600)
      ? '<div class="row"><span class="t2">'+esc(st.msg||'')+'</span></div>' : '');

  const btn=(act,label,cls)=>
    '<button class="ubtn '+cls+'" data-vm="'+v.id+'" data-vmact="'+act+'"'+
    (busy?' disabled':'')+'>'+label+'</button>';
  const controls='<div class="m-row" style="padding:12px 16px 14px;display:flex;gap:10px">'+
    // Neither action on a running VM gets the emphasised style: highlighting
    // one would nudge a choice that should be deliberate. Start is safe, so
    // it keeps it.
    (run ? btn('stop','Stop','no')+btn('restart','Restart','no')
         : btn('start','Start','yes'))+'</div>'+
    '<div class="row"><span class="t2 wrap">The container cannot reach the '+
    'hypervisor, so this is queued for the root job and runs within a '+
    'minute.</span></div>';

  return [vmName(v), rows+controls];
}

function appsSection(){
  const list=(data&&data.services)||[];
  if(!list.length) return '';
  return '<div class="sec" data-sec="apps"><h2>Apps</h2>'+rowsApps()+'</div>';
}

function applyWidths(){
  document.querySelectorAll('#vitals .cardwrap').forEach(el=>{
    el.dataset.w = String(sizeOf(el.dataset.k));
  });
}

// Which series each tile draws when it is double width. Only tiles with a
// real history appear here; the rest simply have no sparkline.
const SPARKS={
  cpu:   d=>[(d.cpu_hist||[]).map(r=>[r[0],r[1]]), 'var(--accent)', 100],
  ctemp: d=>[(d.ctemp_hist||[]).map(r=>[r[0],r[1]]), 'var(--warn)', null],
  // apps only, the same figure as the big number, in the Apps legend colour
  mem:   d=>[(d.mem_hist||[]).map(r=>[r[0],r[1]]), 'var(--accent)', null],
  store: d=>[(d.store_hist||[]).map(r=>[r[0],r[1]]), 'var(--warn)', 100],
  net:   d=>{const h=d.net_hist||{}; const k=Object.keys(h)[0];
             return [(h[k]||[]).map(r=>[r[0],(r[1]||0)+(r[2]||0)]), 'var(--accent)', null];}
};

function drawSparks(d){
  for(const k in SPARKS){
    const el=$('k-'+k);
    if(!el) continue;
    const wrap=el.closest('.cardwrap');
    const w=wrap ? wrap.dataset.w : '1';
    const [rows, colour, cap]=SPARKS[k](d);
    // every card that has history shows it; the bigger sizes just get more room
    const h = w==='2' ? 40 : 20;
    // the viewBox is written into the markup, so it has to follow the height
    // or the path simply draws out through the bottom of the card
    el.setAttribute('viewBox', '0 0 300 '+h);
    el.innerHTML=sparkPath(rows, colour, cap, h);
  }
}

function sparkPath(rows, colour, cap, H){
  if(!rows || rows.length<2) return '';
  const W=300, n=rows.length;
  H = H || 46;
  let lo=Infinity, hi=-Infinity;
  rows.forEach(r=>{ const v=r[1]; if(v<lo)lo=v; if(v>hi)hi=v; });
  // Scale to the data, not to zero. A sparkline is about shape; the big number
  // above it already states the absolute value, and anchoring at zero turned a
  // steady reading into a solid block of fill.
  if(hi-lo<1e-6){ hi=lo+1; lo-=1; }
  const pad=(hi-lo)*0.22; lo-=pad; hi+=pad;
  const x=i=>(i/(n-1))*W;
  const y=v=>H-((v-lo)/(hi-lo))*(H-6)-3;
  const line=rows.map((r,i)=>(i?'L':'M')+x(i).toFixed(1)+' '+y(r[1]).toFixed(1)).join(' ');
  const area='M0 '+H+' '+rows.map((r,i)=>'L'+x(i).toFixed(1)+' '+y(r[1]).toFixed(1)).join(' ')+
             ' L'+W+' '+H+' Z';
  return '<path d="'+area+'" fill="'+colour+'" opacity=".09"/>'+
         '<path d="'+line+'" fill="none" stroke="'+colour+'" stroke-width="2" '+
         'stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>';
}

function applyHidden(){
  const wraps=[].slice.call(document.querySelectorAll('#vitals .cardwrap'));
  wraps.forEach((el,i)=>{
    const off = prefs.hidden.indexOf(el.dataset.k)>=0;
    el.dataset.off = off ? '1' : '0';
    // an arrow with nowhere to go should look like it
    const w=el.querySelector('[data-wide]');
    if(w) w.textContent = nextSize(el.dataset.k)+'\u00d7';
    const eye=el.querySelector('[data-eye]');
    if(eye){
      eye.textContent = off ? '\u002b' : '\u00d7';
      eye.setAttribute('aria-label', off ? 'Put this card back' : 'Remove this card');
    }
  });
}

function paint(d){
  // settings may have been changed on another device; adopt before drawing
  const relayout=adoptPrefs(d.prefs);
  if(!setupShown && setupNeeded()){ setupShown=true; openSetup(false); }
  // Adopt the new payload BEFORE asking whether the layout changed:
  // layoutSig() reads `data`, so testing it first compared the signature of
  // the previous poll and every new section arrived one tick late.
  // A new Dashy is running behind this page. Load it, or this tab keeps
  // running the old code against the new server until someone refreshes.
  if(bootVersion===null) bootVersion=d.version;
  else if(d.version && d.version!==bootVersion){
    try{ sessionStorage.setItem('dashy.updated', JSON.stringify({from:bootVersion, at:Date.now()})); }catch(e){}
    location.reload();
    return;
  }
  data=d;
  if(!built) build();
  else if(relayout || layoutSig()!==builtSig){ build(); }

  if(d.cpu!==null && d.cpu!==undefined){
    $('v-cpu').innerHTML=d.cpu.toFixed(0)+'<small>%</small>';
  }
  $('s-cpu').textContent=d.cores+' threads'+(d.power?' \\u00b7 '+d.power+' W':'');

  if(d.mem){
    const p=(d.mem.apps_pct!==undefined)?d.mem.apps_pct:d.mem.pct;
    $('v-mem').innerHTML=p.toFixed(0)+'<small>%</small>';
    // the cache is in the donut (and its legend at the bigger sizes), so this
    // line stays short enough to sit beside the donut on the smallest card
    $('s-mem').textContent=ram(d.mem.apps!==undefined?d.mem.apps:d.mem.used)+' of '+ram(d.mem.total);
    paintMemDonut(d.mem);
  }

  if(d.cpu_temp!==null && d.cpu_temp!==undefined){
    $('v-ctemp').innerHTML=d.cpu_temp.toFixed(0)+'<small>\\u00b0C</small>';
    $('v-ctemp').style.color=d.cpu_temp>=85?'var(--bad)':d.cpu_temp>=75?'var(--warn)':'';
  }
  const ct=d.core_temps||[];
  $('s-ctemp').textContent=ct.length? ct.length+' cores' : 'package sensor';

  const dr=(d.drives||[]).filter(x=>typeof x.temp==='number');
  if(dr.length){
    const lo=Math.min(...dr.map(x=>x.temp)), hi=Math.max(...dr.map(x=>x.temp));
    const worst=dr.some(x=>x.level==='bad')?'bad':dr.some(x=>x.level==='warn')?'warn':'ok';
    $('v-dtemp').innerHTML=(lo===hi?lo:lo+'\\u2013'+hi)+'<small>\\u00b0C</small>';
    $('v-dtemp').style.color=worst==='bad'?'var(--bad)':worst==='warn'?'var(--warn)':'';
    $('s-dtemp').textContent=dr.length+' drives';
  }

  (d.pools||[]).forEach(p=>{
    const k=poolKey(p.name);
    const v=$('v-'+k); if(!v) return;
    const fp=diskParts(p.free);
    v.innerHTML=fp[0]+'<small>'+fp[1]+' free</small>';
    const used=diskParts(p.size-p.free), tot=diskParts(p.size);
    const health=(p.health||'').toUpperCase();
    $('s-'+k).textContent = (health && health!=='ONLINE')
      ? health
      : used[0]+' of '+tot[0]+tot[1]+' \u00b7 '+p.pct.toFixed(0)+'% full';
    const w=document.querySelector('.cardwrap[data-k="'+k+'"]');
    if(w) w.dataset.state = (health && health!=='ONLINE') ? 'bad'
                          : p.pct>=90 ? 'warn' : '';
  })

  const nets=(d.net||[]).filter(n=>n.down!==null && n.down!==undefined);
  const primary=nets.slice().sort((a,b)=>(b.rx+b.tx)-(a.rx+a.tx))[0];
  if(primary){
    // same shape as the CPU card: big number, small grey unit. Arrows carry the
    // direction, colour-matched to the bar below, so no words are needed.
    const dn=rateParts(primary.down), up=rateParts(primary.up);
    $('v-net').innerHTML='<span class="dir dn">\\u2193</span>'+dn[0]+'<small>'+dn[1]+'</small>';
    $('s-net').innerHTML='<span class="dir up">\\u2191</span>'+up[0]+' '+up[1];
    // one bar: its length is total throughput against the recent peak, and it
    // blends from the download colour into the upload colour at their ratio
    const hist=(d.net_hist||{})[primary.name]||[];
    let peak=1;
    hist.forEach(h=>{ peak=Math.max(peak,h[1]+h[2]); });
    const total=primary.down+primary.up;
    peak=Math.max(peak,total);
    // throughput has no ceiling, so Network shows its trend line instead of a
    // bar scaled against an invented peak
    const el=$('m-net');
    if(el){
      el.style.width=Math.max(3,Math.min(100, Math.sqrt(total/peak)*100)).toFixed(1)+'%';
      const split=total? (primary.down/total*100) : 50;
      const soft=18;
      el.style.background='linear-gradient(90deg, var(--accent) 0%, var(--accent) '+
        Math.max(0,split-soft).toFixed(1)+'%, var(--brand) '+
        Math.min(100,split+soft).toFixed(1)+'%, var(--brand) 100%)';
    }
  }


  const upp=durParts(d.uptime);
  footUp=upp;
  // only speak up when there is something to say
  const ui=d.uptime_info||{};
  const recent = ui.last_boot ? (Date.now()/1000 - ui.last_boot) : null;
  let note='', issue=false;
  if(ui.pct!==null && ui.pct!==undefined && ui.pct < 99.9){
    note = durLong(ui.down)+' down in 90 days'
         + (ui.restarts>1 ? ' \\u00b7 '+ui.restarts+' restarts' : '');
    issue=true;
  }else if(recent!==null && recent < 86400){
    note = 'restarted '+durLong(recent)+' ago';
    issue=true;
  }
  footNote=note; footIssue=issue;


  $('host').textContent=d.host||'';
  paintHealth(d);
  paintAppStates();
  paintBanner();
  drawSparks(d);
  paintLinks(d.links||{});
  // uptime lives in the settings pane now; the home screen stays clean
  const mv=$('settings-version');
  mv.innerHTML='<span>'+(d.app||'Dashy')+' v'+d.version+'</span>'+
    '<span'+(footIssue?' style="color:var(--warn)"':'')+'>up '+footUp[0]+' '+footUp[1]+'</span>';
  mv.title = footIssue && footNote ? footNote : '';
  lastTick=Date.now();
}

/* ---------- update progress ---------------------------------------------
   An update goes: tapped -> queued (a file the root worker picks up within a
   minute) -> running, with a percentage from the midclt job -> done or failed.
   Every step shows, on the tile and in a banner on the home screen, because
   a tap that seemingly did nothing for a minute reads as broken. */
const localQueued={};             // app -> when it was tapped, until the server confirms
function updState(app){
  const st=((data&&data.updates)||{})[app];
  const now=Date.now()/1000, age=(st&&st.t) ? now-st.t : 1e9;
  if(st && st.state==='running' && age<1800)
    return {k:'running', pct:Math.max(0,Math.min(100,st.pct||0)), msg:st.msg||''};
  const tapped=localQueued[app];
  const pending=((data&&data.queued)||[]).indexOf(app)>=0 ||
    (tapped && Date.now()-tapped<120000 && !(st && st.t*1000>=tapped-2000));
  if(pending) return {k:'queued', pct:0};
  if(st && st.state==='done' && age<600) return {k:'done'};
  if(st && st.state==='failed' && age<3600) return {k:'failed', msg:st.msg||''};
  return null;
}
function updLabel(u){
  return u.k==='queued' ? 'Queued' : u.k==='running' ? u.pct+'%'
       : u.k==='done' ? 'Updated' : 'Failed';
}
function paintAppStates(){
  document.querySelectorAll('.apptile[data-app]').forEach(t=>{
    const u=updState(t.dataset.app);
    if(!u){ delete t.dataset.ust; return; }
    t.dataset.ust=u.k;
    const l=t.querySelector('.ust'); if(l) l.textContent=updLabel(u);
    const bar=t.querySelector('.uprog i');
    if(bar) bar.style.width = u.k==='running' ? Math.max(4,u.pct)+'%' : '';
  });
}
// Dashy's own update ends with its container restarting, so for a few
// minutes after it a lost connection is expected, not a fault.
function selfBusy(){
  const u=updState('_self');
  if(u && (u.k==='queued' || u.k==='running')) return true;
  const st=((data&&data.updates)||{})._self;
  return !!(st && st.state==='done' && Date.now()/1000-(st.t||0) < 180);
}
let justUpdated=null;
try{
  const j=JSON.parse(sessionStorage.getItem('dashy.updated')||'null');
  if(j && Date.now()-j.at<60000) justUpdated=j;
  sessionStorage.removeItem('dashy.updated');
}catch(e){}
function paintBanner(){
  const el=$('upbanner'); if(!el) return;
  const show=(state,title,sub,pct)=>{
    el.hidden=false; el.dataset.ust=state;
    $('ub-t').textContent=title; $('ub-s').textContent=sub||'';
    $('ub-bar').style.width = state==='running' ? Math.max(4,pct||0)+'%' : '';
  };
  const me=updState('_self');
  if(me && (me.k==='queued'||me.k==='running'))
    return show(me.k, 'Updating Dashy',
      me.k==='queued' ? 'starts within a minute' : me.pct+'% \u00b7 the page reloads by itself when it is done', me.pct);
  if(selfBusy() && document.body.classList.contains('stale'))
    return show('queued','Dashy is restarting','finishing the update');
  const busy=((data&&data.services)||[]).map(s=>[s,updState(s.app)])
    .filter(([s,u])=>u && (u.k==='queued'||u.k==='running'));
  if(busy.length===1){
    const [s,u]=busy[0];
    return show(u.k, 'Updating '+s.name,
      u.k==='queued' ? 'starts within a minute' : u.pct+'% \u00b7 a snapshot was taken first', u.pct);
  }
  if(busy.length>1){
    const run=busy.filter(([s,u])=>u.k==='running');
    const pct=run.length ? Math.round(run.reduce((n,[s,u])=>n+u.pct,0)/run.length) : 0;
    return show(run.length?'running':'queued', 'Updating '+busy.length+' apps',
      busy.map(([s])=>s.name).join(', '), pct);
  }
  if(justUpdated && Date.now()-justUpdated.at<60000)
    return show('done','Dashy updated', 'now on '+((data&&data.version)||''));
  const failed=((data&&data.services)||[]).map(s=>[s,updState(s.app)])
    .filter(([s,u])=>u && u.k==='failed');
  if(me && me.k==='failed') return show('failed','Dashy did not update', me.msg);
  if(failed.length) return show('failed', failed[0][0].name+' did not update',
                                failed[0][1].msg || 'see TrueNAS for the error');
  el.hidden=true; delete el.dataset.ust;
}

/* The collector looks every half hour, which is the right cadence for a
   background check and the wrong one for somebody standing there waiting.
   This asks now, and shows that it is asking. */
let checking=false, checkMsg='', checkAt=0;
function doCheck(){
  if(checking) return;
  checking=true;
  paintSelfUpdate();
  // the answer shows on the same button: 'Update to x' if there is one,
  // otherwise what the check said, for a minute
  const done=(msg)=>{
    checking=false;
    checkMsg=msg||''; checkAt=Date.now();
    paintSelfUpdate();
  };
  // a registry that never answers would otherwise spin for ever
  const ac=('AbortController' in window) ? new AbortController() : null;
  const bail=setTimeout(()=>{ if(ac) ac.abort(); }, 20000);
  fetch('/api/check',{method:'POST', headers:{'Content-Type':'application/json'},
                      body:'{}', signal: ac ? ac.signal : undefined})
    .then(r=>r.json())
    .then(r=>{ clearTimeout(bail); done(r && r.msg); tick(); })
    .catch(e=>{ clearTimeout(bail);
                done(e && e.name==='AbortError' ? 'timed out' : 'no answer'); });
}

function paintSelfUpdate(){
  const b=$('mi-self');
  if(!b) return;
  const u=data && data.selfupdate;
  const st=(data && data.updates && data.updates._self) || null;
  const txt=$('mi-selftxt'), ver=$('mi-selfver');
  const gear=$('settings');
  const set=(state, label, right)=>{
    b.dataset.state=state;
    // swap the arrow for the dots while it is actually doing something
    const dots=$('mi-selfdots');
    if(dots) dots.hidden = state!=='running' && state!=='checking';
    if(txt) txt.textContent=label;
    if(ver) ver.textContent=right||'';
    if(gear) gear.classList.toggle('dot', state==='ready');
  };

  // Both of these are written to a file that nothing ever clears, so they age
  // out here or they pin the row: 'Updated' would sit in the menu for weeks,
  // and a worker that died mid-update would leave 'Updating...' forever.
  const age=(st && st.t) ? (Date.now()/1000 - st.t) : 1e9;
  if(checking) return set('checking','Checking\u2026','');
  const me=updState('_self');
  if(me && me.k==='queued') return set('running','Update queued\u2026','starts within a minute');
  if(st && st.state==='running' && age < 900)
    return set('running','Updating\u2026', st.pct ? st.pct+'%' : (st.msg||''));
  // An available update outranks a stale failure. The other order looks
  // reasonable - show the most recent thing that happened - but it means one
  // failed attempt hides a working update for an hour, with no way to retry.
  // That happened for real: the worker refused an install it did not
  // recognise, and the row then suppressed the fixed version behind it.
  if(u && u.available)
    return set('ready','Update to '+u.latest,
               (st && st.state==='failed' && age < 3600)
                 ? 'last attempt failed' : u.current+' \u2192 '+u.latest);
  // nothing waiting: the same button checks
  if(checkMsg && Date.now()-checkAt < 60000)
    return set('idle','Check for updates', checkMsg);
  if(st && st.state==='failed' && age < 3600)
    return set('idle','Check for updates', st.msg||'last update failed');
  if(st && st.state==='done' && age < 3600) return set('idle','Check for updates','just updated');
  set('idle','Check for updates', u ? 'up to date' : '');
}

// Each app can override its LAN and tailnet address independently. The global
// switch picks a mode; only that mode's override is applied.
function urlOverrides(id){
  const value=(prefs.urls||{})[id];
  return typeof value==='string'?{lan:value,ts:value}:(value||{});
}
function autoAppURL(s,mode){
  const setup=prefs.setup||{};
  const want=mode||setup.links||(setup.tailscale?'ts':'lan');
  if(want==='lan') return s.url_lan_auto || s.url_lan || s.url;
  return s.url_ts_auto || s.url_ts || s.url_lan_auto || s.url_lan || s.url;
}
function appURL(s){
  const setup=prefs.setup||{};
  const mode=(setup.links||(setup.tailscale?'ts':'lan'))==='lan'?'lan':'ts';
  const pin=urlOverrides(s.id)[mode];
  return pin || autoAppURL(s,mode);
}
// A marker means at least one of this app's addresses has been customized.
function isPinned(s){
  const fields=urlOverrides(s.id);
  return !!(fields.lan || fields.ts);
}

function setLinks(mode){
  prefs.setup=Object.assign({}, prefs.setup, {links:mode});
  if(mode==='ts' && data && data.detected && data.detected.tailscale){
    prefs.setup.tailscale=true;
    setSwitch('o-ts',true); setSwitch('m-ts',true);
  }
  savePrefs();
  paintServeOffer();
  if(openKey==='apps') renderPop(false);
  build();
  // choosing the tailnet is also a request to make those addresses exist
  if(mode==='ts' && data && data.detected && data.detected.tailscale) publishAll();
}

let publishing=false;
function publishAll(){
  if(publishing) return;
  publishing=true;
  const msg=(t)=>['su-tsmsg','m-tsmsg'].forEach(id=>{
    const el=$(id); if(el){ el.textContent=t; el.classList.remove('bad'); } });
  fetch('/api/serve',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({})})
    .then(r=>r.json())
    .then(r=>{ msg(r.ok ? (r.msg||'') : (r.msg||'that did not work'));
               if(!r.ok) ['su-tsmsg','m-tsmsg'].forEach(id=>{
                 const el=$(id); if(el) el.classList.add('bad'); }); })
    .catch(()=>msg('could not reach the NAS'))
    .finally(()=>{ publishing=false; });
}

// Offered only when Tailscale is actually installed.
// The container cannot do any of this itself, so the button drops a request in
// the queue and the root worker runs the same script install.sh does.
function paintServeOffer(){
  const det=(data&&data.detected)||{};
  const st=(data&&data.updates&&data.updates._serve)||null;
  // the choice is worth offering whenever Tailscale is here at all, published
  // or not - it is how you switch back to local addresses too
  const on=!!det.tailscale;
  const su=$('su-serve'); if(su) su.hidden=!on;
  const setupToggle=$('o-ts'); if(setupToggle) setupToggle.disabled=!on;
  const toggle=$('m-ts'); if(toggle) toggle.disabled=!on;
  if(!on){ const msg=$('m-tsmsg'); if(msg) msg.textContent='Install Tailscale on TrueNAS to discover tailnet addresses.'; }
  const setup=prefs.setup||{};
  const mode=(setup.links||(setup.tailscale?'ts':'lan'))==='lan'?'lan':'ts';
  ['su-links','m-links'].forEach(id=>{
    const g=$(id); if(!g) return;
    g.querySelectorAll('button').forEach(b=>
      b.setAttribute('aria-pressed', String(b.dataset.links===mode)));
  });
  if(!on) return;
  let msg='', bad=false;
  if(st && st.state==='running') msg='Publishing\u2026';
  else if(st && st.state==='failed'){ msg=st.msg||'That did not work.'; bad=true; }
  else if(st && st.state==='done' && mode==='ts') msg=st.msg||'';
  ['su-tsmsg','m-tsmsg'].forEach(id=>{
    const el=$(id); if(!el) return;
    el.textContent=msg; el.classList.toggle('bad',bad); });
}

function paintLinks(L){
  const set=(id,url)=>{const a=$(id); if(!a) return;
    if(url){a.href=url;a.hidden=false;}else{a.removeAttribute('href');a.hidden=true;}};
  // filter here rather than on the server: a toggle then takes effect on the
  // spot instead of waiting for the next poll to recompute the links
  const su=prefs.setup||{};
  const hexos = su.hexos ? L.hexos : null;
  set('mi-truenas',L.truenas);
  set('mi-hexos',hexos);
  paintSelfUpdate();
  paintServeOffer();
  setSwitch('m-ts', !!su.tailscale);
  setSwitch('m-hx', !!su.hexos);
  const nas=$('m-nas');
  // never fight the user mid-type
  if(nas && document.activeElement!==nas) nas.value = su.truenas || L.truenas || '';
}

/* How bad is it, in one word, and what to say about it. Both come from
   healthRows, so the card's colour, its headline and its checklist can never
   disagree. They did: a pool logging disk errors put a red row in the list
   under a headline that said all systems normal. Updates are not counted - an
   update waiting is housekeeping, not a fault. */
function issues(d){
  return healthRows(d).filter(r=>(r[0]==='bad'||r[0]==='warn') && r[1]!=='Updates');
}
function severity(d){
  const list=issues(d);
  return list.some(r=>r[0]==='bad') ? 'bad' : list.length ? 'warn' : 'ok';
}
function headline(d){
  const list=issues(d);
  const worst=list.find(r=>r[0]==='bad') || list[0];
  return worst ? (worst[3] || worst[2]) : 'all systems normal';
}

function paintHealth(d){
  const sev=severity(d), msg=headline(d), n=issues(d).length;
  const wrap=document.querySelector('.cardwrap[data-k="health"]');
  if(wrap) wrap.dataset.state = sev==='ok' ? '' : sev;
  const v=$('v-health'); if(v) v.textContent = sev==='ok' ? 'All systems normal' : msg;
  const sb=$('s-health');
  if(sb) sb.textContent = sev==='ok' ? ''
                        : n>1 ? (n-1)+' more '+(n===2?'issue':'issues')
                        : 'everything else normal';
  const f=$('f-health');
  if(f) f.textContent = (d.uptime||d.uptime===0) ? ('up '+upWord(d.uptime)) : '';
  const l=$('l-health');
  if(l) l.innerHTML = healthRows(d).map(([lv,name,txt])=>
    '<div class="hrow" data-level="'+lv+'"><i>'+
      ({ok:'\u2713',warn:'!',bad:'\u00d7',info:'\u2013'}[lv])+
    '</i><b>'+esc(name)+'</b><span title="'+esc(txt)+'">'+esc(txt)+'</span></div>').join('');
}

/* Every health judgement Dashy makes, one row per subsystem. Each row is
   [level, name, text, headline]; the headline is how the row reads on its own
   when it is the worst thing on the card, and defaults to the text. 'info' is
   neither good nor bad, like having no VMs at all. */
function healthRows(d){
  const rows=[], pools=d.pools||[], drives=d.drives||[], svc=d.services||[],
        vms=d.vms||[], ps=d.poolstatus||{};

  const sick=pools.find(p=>String(p.health||'').toUpperCase()!=='ONLINE');
  const errs=pools.find(p=>{const st=ps[p.name];
    return st && ((st.read||0)+(st.write||0)+(st.cksum||0) > 0 ||
                  (st.errors && st.errors!=='No known data errors'));});
  const full=pools.slice().sort((a,b)=>b.pct-a.pct)[0];
  const age=d.pools_age;
  // the collector writes pool data every minute; three missed runs means it
  // has stopped, and everything it reports - pools, drives, VMs - is old news
  const stale=(typeof age==='number' && age>180);
  if(!pools.length) rows.push(['info','Pools','no data']);
  else if(sick){ const h=String(sick.health||'unknown').toLowerCase();
    rows.push(['bad','Pools',sick.name+' '+h, sick.name+' is '+h]); }
  else if(errs) rows.push(['bad','Pools',errs.name+' has disk errors']);
  else if(stale) rows.push(['warn','Pools','data '+agoWord(Math.round(age)),
                            'NAS readings stopped '+agoWord(Math.round(age))]);
  else if(full && full.pct>=85) rows.push([full.pct>=90?'bad':'warn','Pools',
    full.name+' '+full.pct.toFixed(0)+'% full']);
  else rows.push(['ok','Pools', pools.length===1 ? 'online' : pools.length+' online']);

  const failed=drives.find(x=>x.ok===false), hot=drives.find(x=>x.level==='bad'),
        warm=drives.find(x=>x.level==='warn');
  const temps=drives.map(x=>x.temp).filter(t=>typeof t==='number');
  const range=temps.length ? Math.min(...temps)+'\u2013'+Math.max(...temps)+'\u00b0C' : '';
  if(!drives.length) rows.push(['info','Drives','no data']);
  else if(failed) rows.push(['bad','Drives',failed.dev+' SMART failure']);
  else if(hot) rows.push(['bad','Drives',hot.dev+' at '+hot.temp+'\u00b0C']);
  else if(warm) rows.push(['warn','Drives',warm.dev+' at '+warm.temp+'\u00b0C']);
  else rows.push(['ok','Drives',drives.length+' healthy'+(range?' \u00b7 '+range:'')]);

  const down=svc.filter(s=>!s.code);
  if(!svc.length) rows.push(['info','Apps','none found']);
  else if(down.length) rows.push(['warn','Apps',
    down.length===1 ? down[0].name+' down' : down.length+' down',
    down.length===1 ? down[0].name+' not responding' : down.length+' apps not responding']);
  else rows.push(['ok','Apps','all '+svc.length+' responding']);

  const upd=svc.filter(s=>s.update).length;
  const going=svc.filter(s=>{const u=updState(s.app); return u && (u.k==='queued'||u.k==='running');});
  if(going.length) rows.push(['info','Updates', going.length===1 ? 'updating '+going[0].name
                                                                 : 'updating '+going.length+' apps']);
  else rows.push(upd ? ['warn','Updates',upd===1?'1 app update':upd+' app updates']
                     : ['ok','Updates','all apps current']);

  const t=d.cpu_temp;
  if(t===null||t===undefined) rows.push(['info','CPU','no sensor']);
  else rows.push([t>=85?'bad':t>=75?'warn':'ok','CPU',t.toFixed(0)+'\u00b0C'+
    (d.cpu!==null&&d.cpu!==undefined ? ' \u00b7 '+d.cpu.toFixed(0)+'% busy' : ''),
    'CPU at '+t.toFixed(0)+'\u00b0C']);

  // a VM that should start on boot but is not running is the one worth flagging
  const stopped=vms.filter(v=>v.autostart && String(v.state||'').toUpperCase()!=='RUNNING');
  if(!vms.length) rows.push(['info','VMs','none']);
  else if(stopped.length) rows.push(['warn','VMs',stopped.length+' stopped',
    stopped.length===1 ? vmName(stopped[0])+' is not running' : stopped.length+' VMs not running']);
  else rows.push(['ok','VMs',vms.filter(v=>String(v.state||'').toUpperCase()==='RUNNING').length+' running']);
  return rows;
}

function upWord(sec){
  const d=Math.floor(sec/86400);
  if(d>=2) return d+' days';
  const h=Math.floor(sec/3600);
  if(h>=2) return h+' hours';
  return Math.max(1,Math.floor(sec/60))+' min';
}

// "4 cores · 8 threads · 32 GiB memory, ECC · Supermicro X11SCL-F", from system.info
function hardwareLine(sys){
  sys=sys||{};
  const bits=[];
  if(sys.cores) bits.push(sys.cores+(sys.cores===1?' core':' cores'));
  if(sys.threads && sys.threads!==sys.cores) bits.push(sys.threads+' threads');
  if(sys.memory) bits.push(ram(sys.memory)+' memory'+(sys.ecc===true?', ECC':''));
  const board=[sys.maker,sys.product].filter(Boolean).join(' ');
  if(board) bits.push(board);
  return bits.join(' \u00b7 ');
}
function cpuModelRow(){
  const sys=(data&&data.system)||{};
  if(!sys.cpu) return '';
  return '<div class="row"><span class="l"><span class="t1">'+esc(sys.cpu)+'</span>'+
    '<span class="t2">'+esc(hardwareLine(sys))+'</span></span></div>';
}

function rowsCores(){
  const list=data.cpu_cores||[];
  if(!list.length) return '<div class="row"><span class="t2">No per-thread data yet.</span></div>';
  return list.map(c=>
    '<div class="row"><span class="tag">'+esc(String(c.name).trim())+'</span>'+
      '<div class="bar"><i style="width:'+c.pct+'%" data-level="'+level(c.pct)+'"></i></div>'+
      '<span class="val" data-level="'+level(c.pct)+'">'+c.pct.toFixed(0)+'%</span></div>').join('');
}

function rowsCoreTemps(){
  const list=data.core_temps||[];
  if(!list.length) return '<div class="row"><span class="t2">No per-core sensors.</span></div>';
  return list.map(c=>{
    const lv=c.temp>=85?'bad':c.temp>=75?'warn':'ok';
    const w=Math.max(4,Math.min(100,(c.temp-25)/70*100));
    return '<div class="row"><span class="tag">'+esc(c.name)+'</span>'+
      '<div class="bar"><i style="width:'+w+'%" data-level="'+lv+'"></i></div>'+
      '<span class="val" data-level="'+lv+'">'+c.temp.toFixed(0)+'\\u00b0</span></div>';
  }).join('');
}

function memBreakdown(){
  const m=data.mem; if(!m) return '<div class="row"><span class="t2">No memory data.</span></div>';
  const apps=(m.apps!==undefined)?m.apps:m.used, arc=m.arc||0;
  const free=Math.max(0, m.total-apps-arc), pc=n=>(n/m.total*100);
  return '<div class="stackbar">'+
      '<i style="width:'+pc(apps)+'%;background:var(--accent)"></i>'+
      '<i style="width:'+pc(arc)+'%;background:var(--mem-cache)"></i>'+
      '<i style="width:'+pc(free)+'%;background:transparent"></i></div>'+
    '<div class="legend">'+
      '<span><u style="background:var(--accent)"></u>Apps '+ram(apps)+'</span>'+
      '<span><u style="background:var(--mem-cache)"></u>ZFS cache '+ram(arc)+'</span>'+
      '<span><u style="background:var(--faint)"></u>Free '+ram(free)+'</span></div>'+
    '<div class="row"><span class="l"><span class="t1">Installed</span></span>'+
      '<span class="val">'+ram(m.total)+'</span></div>'+
    '<div class="row"><span class="l"><span class="t1">Reclaimable</span>'+
      '<span class="t2">cache ZFS gives back on demand</span></span>'+
      '<span class="val">'+ram(arc)+'</span></div>'+
    '<div class="row"><span class="l"><span class="t1">Available to apps</span>'+
      '<span class="t2">free plus reclaimable cache</span></span>'+
      '<span class="val">'+ram(m.total-apps)+'</span></div>';
}

// zpool list says ONLINE and nothing else. The counters below it are where
// a disk on its way out shows up first.
function poolNote(name){
  const st = (data.poolstatus || {})[name];
  if (!st) return '';
  const bits = [];
  const sw = scrubWord(st);
  if (sw) bits.push(sw);
  if (st.frag !== null && st.frag !== undefined) bits.push(st.frag + '% fragmented');
  const errs = (st.read||0) + (st.write||0) + (st.cksum||0);
  const bad = errs > 0 || (st.errors && st.errors !== 'No known data errors');
  if (errs) bits.push(st.read + ' read / ' + st.write + ' write / ' + st.cksum + ' checksum errors');
  else if (st.errors) bits.push(String(st.errors).toLowerCase());
  if (!bits.length) return '';
  return '<p class="pnote'+(bad ? ' bad' : '')+'">'+esc(bits.join('  \u00b7  '))+'</p>';
}

function rowsPools(){
  return (data.pools||[]).map(p=>{
    const ok=(p.health||'').toUpperCase()==='ONLINE', lv=level(p.pct);
    const col = lv==='ok' ? 'var(--accent)' : lv==='warn' ? 'var(--warn)' : 'var(--bad)';
    return '<div class="row"><span class="l"><span class="t1">'+esc(p.name)+'</span>'+
      '<span class="t2">'+disk(p.used)+' used \\u00b7 '+disk(p.free)+' free of '+disk(p.size)+'</span></span>'+
      '<span class="smart '+(ok?'ok':'bad')+'">'+esc(p.health||'?')+'</span>'+
      '<span class="val" data-level="'+lv+'">'+p.pct.toFixed(0)+'%</span></div>'+
      '<div class="stackbar" style="height:10px;margin:0 16px 8px">'+
        '<i style="width:'+p.pct+'%;background:'+col+'"></i></div>'+
      poolNote(p.name);
  }).join('') || '<div class="row"><span class="t2">No pool data.</span></div>';
}

function rowsDrives(){
  return (data.drives||[]).map(x=>{
    const sm=x.ok===true?'ok':x.ok===false?'bad':'unk';
    const smt=x.ok===true?'SMART OK':x.ok===false?'SMART FAIL':'SMART ?';
    const yrs=x.hours?' \\u00b7 '+(x.hours/8766).toFixed(1)+' yrs on':'';
    return '<div class="row"><span class="tag">'+x.kind+'</span>'+
      '<span class="l"><span class="t1">'+esc(x.dev)+'</span>'+
      '<span class="t2">'+(x.size?disk(x.size):'')+(x.model?' \\u00b7 '+esc(x.model):'')+yrs+'</span></span>'+
      '<span class="smart '+sm+'">'+smt+'</span>'+
      '<span class="val"'+(x.level?' data-level="'+x.level+'"':'')+'>'+
        (typeof x.temp==='number'?x.temp+'\\u00b0':'\\u2013')+'</span></div>';
  }).join('') || '<div class="row"><span class="t2">No drive data.</span></div>';
}

// Sampled on the host by the cron job: the container has its own pid
// namespace and can see nothing but itself.
// Load is the one number that says whether the machine is keeping up: it
// counts what is waiting, not what is busy. Against the core count it reads
// as a ratio rather than a bare figure.
function loadRow(){
  const l = data.load || [];
  if (!l.length) return '';
  const n = (data.procs && data.procs.ncpu) || data.cores || 0;
  const lv = n ? (l[0] >= n ? 'bad' : l[0] >= n * .7 ? 'warn' : 'ok') : '';
  return '<div class="row"><span class="l"><span class="t1">Load average</span>'+
    '<span class="t2">'+l.map(x=>x.toFixed(2)).join('  \u00b7  ')+
    '  over 1, 5 and 15 min'+(n ? ' \u00b7 '+n+' cores' : '')+'</span></span>'+
    '<span class="val"'+(lv ? ' data-level="'+lv+'"' : '')+'>'+l[0].toFixed(2)+'</span></div>';
}

function rowsProcs(kind){
  const pr = data.procs || {};
  const list = (kind === 'cpu' ? pr.cpu : pr.mem) || [];
  if (!list.length)
    return '<div class="row"><span class="t2">No process data yet — '+
           'the collector writes it once a minute.</span></div>';
  const age = pr.at ? Math.max(0, Math.round(Date.now()/1000 - pr.at)) : null;
  const head = '<p class="shead">Using the most '+
    (kind === 'cpu' ? 'CPU <span class="t2">% of one core</span>' : 'memory')+
    (age !== null ? ' <span class="t2">'+(age < 90 ? 'just now' : agoWord(age))+'</span>' : '')+'</p>';
  return head + list.map(r => {
    const val = kind === 'cpu' ? r.cpu.toFixed(0)+'%' : ram(r.rss);
    const side = kind === 'cpu' ? ram(r.rss)+' resident' : r.cpu.toFixed(0)+'% CPU';
    return '<div class="row">'+
      (r.app ? '<span class="tag">'+esc(r.app)+'</span>' : '')+
      '<span class="l"><span class="t1">'+esc(r.comm)+'</span>'+
      '<span class="t2">pid '+r.pid+' · '+side+'</span></span>'+
      '<span class="val">'+val+'</span></div>';
  }).join('');
}

function agoWord(sec){
  if (sec < 90) return 'just now';
  if (sec < 5400) return Math.round(sec/60)+' min ago';
  if (sec < 172800) return Math.round(sec/3600)+' h ago';
  const d = Math.round(sec/86400);
  if (d < 14) return d+' days ago';
  if (d < 60) return Math.round(d/7)+' weeks ago';
  return Math.round(d/30.4)+' months ago';
}

// Everything the dashboard knows that wants a human decision, in one place.
// The Health tile used to open the uptime record, which answered a question
// nobody asked of it.
function rowsSystem(){
  const out = [];
  const svc = data.services || [];
  const down = svc.filter(s => !s.code);
  const upd = svc.filter(s => s.update);
  const su = data.selfupdate || {};
  const pools = data.pools || [];
  const drives = data.drives || [];
  const ps = data.poolstatus || {};

  if (su.available)
    out.push(srow('warn', 'Dashy ' + su.latest + ' is available',
                  'You are on ' + (su.current || data.version) +
                  '. Install it from the settings pane.'));

  if (upd.length)
    out.push(srow('warn', upd.length === 1 ? upd[0].name + ' has an update'
                                           : upd.length + ' apps have updates',
                  upd.map(s => s.name).join(', ')));

  if (down.length)
    out.push(srow('bad', down.length === 1 ? down[0].name + ' is not responding'
                                           : down.length + ' apps are not responding',
                  down.map(s => s.name).join(', ')));

  pools.forEach(p => {
    const st = ps[p.name] || {};
    const health = (p.health || '').toUpperCase();
    if (health && health !== 'ONLINE')
      out.push(srow('bad', p.name + ' is ' + health, 'Check the pool in TrueNAS.'));
    else if (p.pct >= 90)
      out.push(srow('bad', p.name + ' is ' + p.pct.toFixed(0) + '% full',
                    disk(p.free) + ' left of ' + disk(p.size)));
    else if (p.pct >= 85)
      out.push(srow('warn', p.name + ' is ' + p.pct.toFixed(0) + '% full',
                    disk(p.free) + ' left of ' + disk(p.size), 'store'));
    const errs = (st.read || 0) + (st.write || 0) + (st.cksum || 0);
    if (errs)
      out.push(srow('bad', p.name + ' has logged errors',
                    st.read + ' read, ' + st.write + ' write, ' + st.cksum + ' checksum'));
    if (st.errors && st.errors !== 'No known data errors')
      out.push(srow('bad', p.name + ': ' + st.errors, ''));
  });

  drives.filter(d => d.ok === false).forEach(d =>
    out.push(srow('bad', d.dev + ' failed its SMART check',
                  (d.model || '') + (d.hours ? ' · ' + (d.hours/8766).toFixed(1) + ' yrs on' : ''),
                  )));
  drives.filter(d => d.level === 'bad').forEach(d =>
    out.push(srow('bad', d.dev + ' is running hot', d.temp + '°C')));
  drives.filter(d => d.level === 'warn').forEach(d =>
    out.push(srow('warn', d.dev + ' is warm', d.temp + '°C')));

  // the rest of what the Health card judges, so the card and this sheet agree
  const age = data.pools_age;
  if (typeof age === 'number' && age > 180)
    out.push(srow('warn', 'NAS readings stopped ' + agoWord(Math.round(age)),
                  'The collector cron job has not run since. Pools, drives and VMs below are from then.'));
  const t = data.cpu_temp;
  if (typeof t === 'number' && t >= 75)
    out.push(srow(t >= 85 ? 'bad' : 'warn', 'CPU at ' + t.toFixed(0) + '°C', ''));
  (data.vms || []).filter(v => v.autostart && String(v.state || '').toUpperCase() !== 'RUNNING')
    .forEach(v => out.push(srow('warn', vmName(v) + ' is not running',
                                'It is set to start with the server.', 'vm-' + v.id)));

  const head = out.length
    ? '<p class="shead">' + out.length + (out.length === 1 ? ' thing wants' : ' things want') +
      ' your attention</p>'
    : '<div class="allok"><p>Nothing needs you.</p>' +
      '<p class="t2">Every app is answering, every pool is online, no drive is warm '+
      'and every VM that should be running is.</p></div>';

  // what is normal, so the sheet says something either way
  const normal = [];
  // the machine itself, as TrueNAS describes it
  const sys = data.system || {};
  if (sys.version || sys.hostname)
    normal.push(srow('ok', 'TrueNAS ' + String(sys.version || '').replace(/^TrueNAS-?/i, ''),
                     [sys.hostname, sys.timezone].filter(Boolean).join(' \u00b7 ')));
  const hw = hardwareLine(sys);
  if (sys.cpu || hw)
    normal.push(srow('ok', sys.cpu || 'Hardware', hw));
  normal.push(srow('ok', 'Dashy ' + data.version,
                   su.checked ? 'Checked for updates ' + agoWord(Math.max(0, Math.round(Date.now()/1000 - su.checked))) : ''));
  normal.push(srow('ok', svc.length + ' apps installed',
                   (svc.length - down.length) + ' responding'));
  pools.forEach(p => {
    const st = ps[p.name] || {};
    normal.push(srow('ok', p.name + ' · ' + p.pct.toFixed(0) + '% full',
                     scrubWord(st)));
  });
  if (data.uptime || data.uptime === 0)
    normal.push(srow('ok', 'Up ' + upWord(data.uptime),
                     'See the uptime record', 'up'));

  return head + out.join('') +
         '<p class="shead">Everything else</p>' + normal.join('');
}

// "scrub repaired 0B in 00:14:39 with 0 errors on Sun Oct 11 00:14:40 2026"
// is accurate and unreadable. Keep the part that answers "is it healthy and
// when was it last checked".
function scrubWord(st){
  const s = st.scan || '';
  if (!s) return '';
  let m = s.match(/scrub in progress.*?(\\d+(?:\\.\\d+)?)% done/);
  if (m) return 'Scrub running · ' + m[1] + '% done';
  m = s.match(/scrub repaired (\\S+) in \\S+ with (\\d+) errors on (.+)$/);
  if (m) {
    const when = new Date(m[3]);
    const ago = isNaN(when) ? m[3] : agoWord(Math.round((Date.now() - when) / 1000));
    return (m[2] === '0' ? 'Last scrub clean' : 'Last scrub found ' + m[2] + ' errors') +
           ' · ' + ago;
  }
  if (/none requested/.test(s)) return 'Never scrubbed';
  return s.length > 90 ? s.slice(0, 90) + '…' : s;
}

function srow(level, title, sub, pop){
  const tag = pop ? 'button type="button" data-pop="' + pop + '"' : 'div';
  return '<' + (pop ? tag : 'div') + ' class="srow' + (pop ? ' go' : '') +
    '" data-level="' + level + '">' +
    '<i class="sdotL"></i>' +
    '<span class="l"><span class="t1">' + esc(title) + '</span>' +
    (sub ? '<span class="t2">' + esc(sub) + '</span>' : '') + '</span>' +
    '</' + (pop ? 'button' : 'div') + '>';
}

function rowsApps(){
  const list=data.services||[];
  if(!list.length) return '<div class="row"><span class="t2">Nothing published yet.</span></div>';
  return '<div class="appgrid">'+list.map(s=>
    '<a class="apptile" data-up="'+(s.code?'1':'0')+'"'+
      ' data-uid="'+esc(s.id)+'" data-name="'+esc(s.name)+'" data-app="'+esc(s.app)+'"'+
      ' href="'+esc(appURL(s)||'#')+'"'+
      ' role="button" aria-label="'+esc(s.name)+' details">'+
      '<span class="sdot"></span>'+
      (s.update?'<button class="upd" data-app="'+esc(s.app)+'" data-name="'+esc(s.name)+'" '+
                 'data-ver="'+esc(niceVer(s))+'" aria-label="Update '+esc(s.name)+'"></button>':'')+
      '<span class="ust"></span><span class="uprog"><i></i></span>'+
      (isPinned(s)?'<span class="pin" aria-hidden="true"></span>':'')+
      appIcon(s)+
      '<span class="an">'+esc(s.name)+'</span>'+
      '<span class="ap" title="'+esc(s.version||'')+'">'+esc(niceVer(s))+'</span></a>').join('')+'</div>';
}

// TrueNAS reports '1.43.5.11079-b3b13e506_1.3.21': the app's own version, a
// build, then the catalogue's chart revision after the last underscore. The
// tile shows the part a person means by 'version'; the sheet keeps it all. A
// custom app's '1.0.0_custom' is a placeholder TrueNAS makes up, not a version.
function niceVer(s){
  const raw=String((s&&s.version)||'').trim();
  if((s&&s.custom) || /_custom$/i.test(raw)) return 'Custom app';
  let v=raw.indexOf('_')>0 ? raw.slice(0,raw.lastIndexOf('_')) : raw;
  v=v.replace(/^v(?=\\d)/i,'').split(/[-+ ]/)[0];
  const m=v.match(/^\\d+(\\.\\d+){0,2}/);
  return m ? 'Version '+m[0] : '';
}

// Catalogue apps have an icon; custom ones do not, so those fall back to a
// monogram tinted from the name - stable, rather than random per reload.
function appIcon(s){
  if(s.iconsrc) return '<img class="aico" data-mono="'+esc(s.name||'?')+'" '+
                       'src="'+esc(s.iconsrc)+'" alt="" loading="lazy">';
  return monogramHTML(s.name||'?');
}
// no inline onerror: the quoting is fragile and a stray apostrophe in an app
// name would break the whole grid. Wired up after render instead.
function wireIcons(root){
  root.querySelectorAll('img.aico').forEach(img=>{
    img.onerror=()=>{
      const d=document.createElement('div');
      d.innerHTML=monogramHTML(img.dataset.mono||'?');
      img.replaceWith(d.firstChild);
    };
  });
}
function esc(t){
  return String(t).replace(/[&<>"']/g, c=>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function hueOf(name){
  let h=0; for(let i=0;i<name.length;i++) h=(h*31+name.charCodeAt(i))%360;
  return h;
}
function monogramHTML(name){
  const h=hueOf(name);
  return '<span class="aico mono" style="background:hsl('+h+' 42% 46%)">'+
         (name.trim()[0]||'?').toUpperCase()+'</span>';
}

// A small filled line, for the metric cards. Same square-root idea as the big
// chart: these figures spend most of their life near the floor, and a linear
// axis would draw that as a dead flat line.
function mspark(rows, colour){
  const vals=(rows||[]).map(r=>r[1]).filter(v=>typeof v==='number');
  if(vals.length<2) return '<div class="mspark"></div>';
  const W=120, H=26, n=vals.length;
  let peak=Math.max.apply(null, vals)||1, low=Math.min.apply(null, vals);
  const span=Math.max(peak-low, peak*0.12, 1);
  const x=i=>(i/(n-1))*W;
  const y=v=>H-2-((v-low)/span)*(H-6);
  const line=vals.map((v,i)=>(i?'L':'M')+x(i).toFixed(1)+' '+y(v).toFixed(1)).join(' ');
  const area='M0 '+H+' '+vals.map((v,i)=>'L'+x(i).toFixed(1)+' '+y(v).toFixed(1)).join(' ')+' L'+W+' '+H+' Z';
  return '<div class="mspark"><svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none">'+
    '<path d="'+area+'" fill="'+colour+'" opacity=".12"/>'+
    '<path d="'+line+'" fill="none" stroke="'+colour+'" stroke-width="1.8" '+
      'stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>'+
    '</svg></div>';
}

// The app detail sheet. Every field here is something Dashy already knows: an
// app's own latency trail, the versions the NAS reports, and the auto-resolved
// address. The launch address is set here rather than on the tile, so the whole
// decision - where it opens, and everything known about it - is in one place.
function appSheet(id){
  const s=((data&&data.services)||[]).filter(x=>x.id===id)[0];
  if(!s) return null;
  const up=!!s.code;
  const ver=String(s.version||'').trim();
  const newer=String(s.update||'').trim();
  const overrides=urlOverrides(id);
  const lanAuto=s.url_lan_auto||s.url_lan||'';
  const tsAuto=s.url_ts_auto||s.url_ts||'';
  const lanURL=overrides.lan||lanAuto;
  const tsURL=overrides.ts||tsAuto;
  const target=appURL(s);

  let head;
  if(up) head='<span class="pill up"><span class="dot"></span>Running</span>';
  else if(s.code===0) head='<span class="pill down"><span class="dot"></span>Not answering</span>';
  else head='<span class="pill warn"><span class="dot"></span>Unknown</span>';
  const codeTxt=up?('HTTP '+s.code):(s.code===0?'no reply':'');
  // Response latency is per-app. CPU, RAM, Network and Disk I/O are the live
  // host readings currently available to Dashy, so each is explicitly marked
  // NAS rather than being presented as container-level usage.
  const rt=(typeof s.rt_ms==='number')?s.rt_ms:null;
  const rtRows=s.rt_hist||[];
  const netDev=(data.net||[]).filter(n=>n.down!==null&&n.down!==undefined)
                 .sort((a,b)=>(b.down+b.up)-(a.down+a.up))[0];
  const netHist=netDev?(data.net_hist||{})[netDev.name]||[]:[];
  const netNow=netDev?(netDev.down||0)+(netDev.up||0):0;
  const ioNow=(data.io||[]).reduce((n,x)=>n+(x.read||0)+(x.write||0),0);
  const ioHist=(data.io_hist||[]).map(r=>[r[0],(r[1]||0)+(r[2]||0)]);
  // apps only, like the Memory tile: the ZFS cache is handed back on demand,
  // and counting it made this read 87% while the tile above said 50%
  const ramHist=(data.mem_hist||[]).map(r=>[
    r[0], data.mem&&data.mem.total ? 100*(r[1]||0)/data.mem.total : 0]);
  const memPct=data.mem?Math.round(data.mem.apps_pct!==undefined?data.mem.apps_pct:data.mem.pct):null;
  const cards=
    '<div class="mcards">'+
      '<div class="mcard"><span class="ml">CPU \u00b7 NAS</span>'+
        '<span class="mv">'+(data.cpu!==null&&data.cpu!==undefined?Math.round(data.cpu):'\u2014')+
          (data.cpu!==null&&data.cpu!==undefined?'<span class="mu">%</span>':'')+'</span>'+
        mspark(data.cpu_hist,'var(--accent)')+'</div>'+
      '<div class="mcard"><span class="ml">RAM \u00b7 NAS</span>'+
        '<span class="mv">'+(memPct!==null?memPct:'\u2014')+
          (memPct!==null?'<span class="mu">%</span>':'')+'</span>'+mspark(ramHist,'var(--brand)')+'</div>'+
      '<div class="mcard"><span class="ml">Network \u00b7 NAS</span>'+
        '<span class="mv" style="font-size:16px">'+rate(netNow)+'</span>'+mspark(netHist,'var(--accent)')+'</div>'+
      '<div class="mcard"><span class="ml">Disk I/O \u00b7 NAS</span>'+
        '<span class="mv" style="font-size:16px">'+rate(ioNow)+'</span>'+mspark(ioHist,'var(--brand)')+'</div>'+
    '</div>';
  const response='<div class="appresponse"><span class="rl">App response</span>'+
    '<span class="rv">'+(rt!==null?rt+' ms':'\u2014')+'</span>'+mspark(rtRows,'var(--accent)')+'</div>';

  const urls=
    '<div class="appurls"><div class="lbl">Launch address</div>'+
      '<div class="urow"><span class="uk">LAN</span><span class="uv '+(overrides.lan?'on':'')+'">'+
        esc(lanURL||'Not discovered')+'</span></div>'+
      '<div class="urow"><span class="uk">Tailnet</span><span class="uv '+(overrides.ts?'on':'')+'">'+
        esc(tsURL||'Not configured')+'</span></div>'+
    '</div>';

  const acts=
    '<div class="appacts">'+
      '<button class="ubtn no" id="ap-opt" type="button">Options</button>'+
      '<button class="ubtn launch" id="ap-launch" type="button"'+
        (target?'':' disabled')+'>Launch</button>'+
    '</div>';

  const sub = up?('running'+(codeTxt?(' \u00b7 '+codeTxt):'')+
                  (rt!==null?' \u00b7 '+rt+' ms':''))
            : (s.code===0?'not answering':'unknown');
  return [s.name, '<div class="appsheet">'+
      (s.desc?'<div class="appdesc">'+esc(s.desc)+'</div>':'')+
      '<div class="appstat">'+head+
        '<span class="appver">'+(ver?esc(ver):'')+(newer?' \u2192 '+esc(newer):'')+'</span></div>'+
      response+cards+urls+'<div id="ap-optbox"></div>'+acts+
    '</div>', sub];
}

// Rendered on demand by the Options button: the paste field is only in the way
// when you actually want it, so it stays behind one tap.
function appOptions(){
  const id=openKey.slice(4);
  const fields=urlOverrides(id);
  const s=((data&&data.services)||[]).find(x=>x.id===id)||{};
  const lanAuto=s.url_lan_auto||s.url_lan||'';
  const tsAuto=s.url_ts_auto||s.url_ts||'';
  appUrlId=id;
  const box=$('ap-optbox'); if(!box) return;
  if(box._open){ box._open=false; box.innerHTML=''; return; }
  box._open=true;
  box.innerHTML=
    '<div class="optbox"><div class="urlrow">'+
      '<label class="urlfield"><span>LAN address</span><input class="fld2" id="au-lan" type="url" inputmode="url" autocomplete="off" '+
        'spellcheck="false" placeholder="'+esc(lanAuto||'http://nas-ip:port')+'" value="'+esc(fields.lan||'')+'"></label>'+
      '<label class="urlfield"><span>Tailnet address</span><input class="fld2" id="au-ts" type="url" inputmode="url" autocomplete="off" '+
        'spellcheck="false" placeholder="'+esc(tsAuto||'https://nas.tailnet.ts.net')+'" value="'+esc(fields.ts||'')+'"></label>'+
      '<span class="uhint">Leave either field blank to use its discovered address.</span>'+
      '<div class="ubtns">'+
        '<button class="ubtn no clr" id="au-clear">Use automatic</button>'+
        '<button class="ubtn yes" id="au-save">Save</button>'+
      '</div></div></div>';
  const i=$('au-lan'); if(i) i.focus();
}


// series: [{key, colour, label}] over rows of [stamp, v1, v2...]
function chart(rows, series, fmtv){
  if(!rows || rows.length<2) return '';
  const W=430, H=112, n=rows.length;
  let peak=1;
  rows.forEach(p=>series.forEach(s=>{ peak=Math.max(peak, p[s.key]); }));
  const x=i=>(i/(n-1))*W;
  // square-root scale: a quiet line would be a flat smear on a linear axis
  const y=v=>H-Math.sqrt(Math.max(0,v)/peak)*(H-8)-4;
  const path=k=>rows.map((p,i)=>(i?'L':'M')+x(i).toFixed(1)+' '+y(p[k]).toFixed(1)).join(' ');
  const area=k=>'M0 '+H+' '+rows.map((p,i)=>'L'+x(i).toFixed(1)+' '+y(p[k]).toFixed(1)).join(' ')+' L'+W+' '+H+' Z';
  const mins=Math.max(1,Math.round((rows[n-1][0]-rows[0][0])/60));
  return '<div class="chart"><svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none">'+
      series.map(s=>'<path d="'+area(s.key)+'" fill="'+s.colour+'" opacity=".10"/>').join('')+
      series.map(s=>'<path d="'+path(s.key)+'" fill="none" stroke="'+s.colour+'" stroke-width="2.8" '+
        'stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>').join('')+
    '</svg><div class="cap"><span>'+mins+' min ago</span><span>peak '+fmtv(peak)+'</span>'+
      '<span>now</span></div></div>'+
    '<div class="legend">'+series.map(s=>
      '<span><u style="background:'+s.colour+'"></u>'+s.label+'</span>').join('')+'</div>';
}

function netChart(name){
  return chart((data.net_hist||{})[name]||[],
    [{key:1,colour:'var(--accent)',label:'down'},{key:2,colour:'var(--brand)',label:'up'}], rate);
}
function ioChart(){
  return chart(data.io_hist||[],
    [{key:1,colour:'var(--accent)',label:'read'},{key:2,colour:'var(--brand)',label:'write'}], rate);
}
function memChart(){
  return chart(data.mem_hist||[],
    [{key:1,colour:'var(--accent)',label:'apps'},{key:2,colour:'var(--mem-cache)',label:'ZFS cache'}], ram);
}
function powerChart(){
  return chart(data.power_hist||[],
    [{key:1,colour:'var(--warn)',label:'package watts'}], v=>v.toFixed(0)+' W');
}

function rowsIo(){
  const list=(data.io||[]).filter(x=>x.read!==null&&x.read!==undefined&&(x.read||x.write));
  if(!list.length) return '';
  return list.map(x=>
    '<div class="row"><span class="l"><span class="t1">'+esc(x.name)+'</span>'+
      '<span class="t2">live disk activity</span></span>'+
      '<span class="val" style="min-width:92px">'+rate(x.read)+'<br>'+
      '<span style="font-size:11px;color:var(--muted);font-weight:600">'+rate(x.write)+' write</span>'+
      '</span></div>').join('');
}

function rowsNet(){
  const list=data.net||[];
  if(!list.length) return '<div class="row"><span class="t2">No interface data.</span></div>';
  return list.map(n=>
    '<div class="row"><span class="l"><span class="t1">'+esc(n.name)+'</span>'+
      '<span class="t2">'+(n.speed? n.speed+' Mb/s link \\u00b7 ':'')+
        disk(n.rx)+' in \\u00b7 '+disk(n.tx)+' out'+
        (n.errors? ' \\u00b7 '+n.errors+' errors':'')+'</span></span>'+
      '<span class="val" style="min-width:86px">'+rate(n.down)+'<br>'+
        '<span style="font-size:11px;color:var(--muted);font-weight:600">'+rate(n.up)+' up</span>'+
      '</span></div>').join('');
}

function uptimeChart(){
  const b=(data.boots||[]).slice().sort((a,c)=>a.start-c.start);
  if(!b.length) return '<div class="row"><span class="t2">No boot history yet.</span></div>';
  const now=Date.now()/1000;
  const from=Math.min(b[0].start, now-90*86400);
  const span=Math.max(1, now-from);
  let segs='', cursor=from;
  b.forEach(s=>{
    const a=Math.max(s.start,from), e=s.end?s.end:now;
    if(a>cursor) segs+='<i style="width:'+((a-cursor)/span*100)+'%;background:var(--bad)"></i>';
    if(e>a)      segs+='<i style="width:'+((e-a)/span*100)+'%;background:var(--ok)"></i>';
    cursor=Math.max(cursor,e);
  });
  const rows=b.slice().reverse().map(s=>
    '<div class="row"><span class="l"><span class="t1">booted '+when(s.start)+'</span>'+
      '<span class="t2">'+(s.end?('until '+when(s.end)):'still running')+'</span></span>'+
      '<span class="val">'+durLong((s.end||now)-s.start)+'</span></div>').join('');
  return '<div class="tl"><div class="seg">'+segs+'</div>'+
      '<div class="cap"><span>'+when(from)+'</span><span>now</span></div></div>'+
    '<div class="legend"><span><u style="background:var(--ok)"></u>up</span>'+
      '<span><u style="background:var(--bad)"></u>down</span>'+
      ((data.availability!==null&&data.availability!==undefined)
        ? '<span>'+data.availability.toFixed(2)+'% available over 90 days</span>' : '')+'</div>'+
    rows;
}

const POPS={
  system:()=>['System', rowsSystem()],
  cpu:   ()=>['CPU', cpuModelRow()+loadRow()+powerChart()+
            (data.power!==null&&data.power!==undefined
              ? '<div class="row"><span class="l"><span class="t1">Package power</span>'+
                '<span class="t2">whole chip \\u2014 this CPU reports no per-core figure</span></span>'+
                '<span class="val">'+data.power+' W</span></div>' : '')+
            rowsProcs('cpu')+rowsCores()],
  mem:   ()=>['Memory', memChart()+memBreakdown()+rowsProcs('mem')],
  ctemp: ()=>['Temperature by core',  rowsCoreTemps()],
  dtemp: ()=>['Drive temperatures',   rowsDrives()],
  store: ()=>['Storage', ioChart()+rowsIo()+rowsPools()],
  net:   ()=>['Network', (function(){
            const l=data.net||[];
            const p=l.slice().sort((a,b)=>(b.rx+b.tx)-(a.rx+a.tx))[0];
            return (p?netChart(p.name):'')+rowsNet();
          })()],
  apps:  ()=>['Apps',                 rowsApps()],
  up:    ()=>['Uptime record',        uptimeChart()],
};

function renderPop(opening){
  if(!openKey || !data) return;
  // VM and app sheets are keyed by id rather than being listed in POPS
  const r = openKey.indexOf('vm-')===0 ? vmSheet(openKey.slice(3))
          : openKey.indexOf('app-')===0 ? appSheet(openKey.slice(4))
          : (POPS[openKey] ? POPS[openKey]() : null);
  if(!r) return;
  const b=$('sh-b');

  if($('sh-t').textContent!==r[0]) $('sh-t').textContent=r[0];
  const sub=r[2]||'';
  if($('sh-s').textContent!==sub) $('sh-s').textContent=sub;
  const icon=$('sh-ico');
  if(openKey.indexOf('app-')===0){
    const svc=((data.services)||[]).find(s=>s.id===openKey.slice(4));
    const html=svc?appIcon(svc):'';
    if(icon.innerHTML!==html){ icon.innerHTML=html; wireIcons(icon); }
    icon.hidden=!html;
  }else{
    icon.innerHTML=''; icon.hidden=true;
  }

  // The poll runs every 3s. Rewriting innerHTML each time rebuilt the whole
  // subtree - images reloaded, charts redrew, scroll position jumped and the
  // entrance animation replayed. That was the flashing. Only touch the DOM
  // when the markup has actually changed.
  if(b._html===r[1] && !opening) return;
  // The app sheet's Options box is live user input. A 3s poll must not wipe
  // what is being typed into it, so its contents are lifted out and put back.
  let opt=null, optValues=null, optFocus='';
  const prev=b.querySelector('#ap-optbox');
  if(prev && prev._open && prev.innerHTML){
    opt=prev.innerHTML;
    optValues={};
    prev.querySelectorAll('#au-lan,#au-ts').forEach(i=>optValues[i.id]=i.value);
    if(document.activeElement && ['au-lan','au-ts'].includes(document.activeElement.id))
      optFocus=document.activeElement.id;
  }
  b._html=r[1];
  b.innerHTML=r[1];
  wireIcons(b);
  if(opt){
    const nb=b.querySelector('#ap-optbox');
    if(nb){ nb.innerHTML=opt; nb._open=true;
            nb.querySelectorAll('#au-lan,#au-ts').forEach(i=>{
              if(optValues && Object.prototype.hasOwnProperty.call(optValues,i.id))
                i.value=optValues[i.id];
            });
            if(optFocus){ const i=$(optFocus); if(i) i.focus(); } }
  }

  if(!opening){
    // a refresh is not an entrance: show it immediately, no animation
    b.classList.add('shown');
    return;
  }
  b.classList.remove('shown');
  let i=0;
  for(const el of b.children) el.style.setProperty('--i', String(i++));
  // whatever happens to the animation, the rows are visible shortly after
  clearTimeout(renderPop._t);
  renderPop._t=setTimeout(()=>b.classList.add('shown'), 90 + i*38 + 320);
}

function openPop(k){
  if(!data) return;
  if(k.indexOf('vm-')!==0 && k.indexOf('app-')!==0 && !POPS[k]) return;
  openKey=k;
  $('sh-b')._html=null;      // force a rebuild so the entrance plays
  renderPop(true);
  $('scrim').hidden=false;
}
function closePop(){
  const sc=$('scrim');
  if(sc.hidden || sc.classList.contains('out')) return;
  openKey=null;
  sc.classList.add('out');
  // let the exit animation finish, but never leave it stuck open
  setTimeout(()=>{ sc.classList.remove('out'); sc.hidden=true; }, 170);
}

async function askSelfUpdate(){
  const u=(data && data.selfupdate) || {};
  $('sh-t').textContent='Update Dashy';
  $('sh-s').textContent='';
  $('sh-b').innerHTML=
    '<div class="row"><span class="l"><span class="t1">'+esc(u.current||'')+
      ' \u2192 '+esc(u.latest||'')+'</span>'+
      '<span class="t2">from '+esc(u.repo||'the repository')+'</span></span></div>'+
    '<div class="row"><span class="t2 wrap">The new files are downloaded and '+
      'checked before anything is replaced, and a ZFS snapshot is taken first. '+
      'Dashy restarts, so the page will go quiet for a few seconds.</span></div>'+
    '<div class="m-row" style="padding:12px 16px 14px;display:flex;gap:10px">'+
      '<button class="ubtn no" id="u-no">Cancel</button>'+
      '<button class="ubtn yes" id="su-yes">Update</button></div>';
  $('sh-b')._html=null;
  $('scrim').hidden=false;
}

async function askUpdate(app,name,ver){
  const body=
    '<div class="row"><span class="l"><span class="t1">'+esc(name)+'</span>'+
      '<span class="t2">'+(ver?'currently '+esc(ver.replace(/^Version /,'on version ')):'version unknown')+'</span></span></div>'+
    '<div class="row"><span class="t2 wrap">The app will restart while it upgrades, so it will be '+
      'briefly unavailable. A ZFS snapshot of its data is taken first, so there is a rollback '+
      'point. The upgrade starts within a minute.</span></div>'+
    '<div class="m-row" style="padding:12px 16px 14px;display:flex;gap:10px">'+
      '<button class="ubtn no" id="u-no">Cancel</button>'+
      '<button class="ubtn yes" id="u-yes" data-app="'+esc(app)+'">Update '+esc(name)+'</button></div>';
  $('sh-t').textContent='Update available';
  $('sh-s').textContent='';
  $('sh-b').innerHTML=body;
  $('scrim').hidden=false; openKey=null;
}

// The launch address, per app, for this install. Auto-resolution already picks
// LAN or tailnet from the global switch; this pins one tile so an app that
// lives somewhere else (a different host, a reverse proxy, a cockpit path)
// still opens where it should. Blank hands it back to the auto-resolution.
// Set from the app sheet's Options button, via appOptions/saveAppURL.
let appUrlId='';

function saveAppURL(id){
  const lan=($('au-lan').value||'').trim();
  const ts=($('au-ts').value||'').trim();
  for(const [field,value] of [['au-lan',lan],['au-ts',ts]]){
    if(value && !/^https?:\\/\\//i.test(value)){ $(field).focus(); return; }
  }
  prefs.urls=Object.assign({}, prefs.urls);
  if(!lan && !ts) delete prefs.urls[id];
  else prefs.urls[id]={...(lan?{lan}:{}),...(ts?{ts}:{})};
  savePrefs();
  if(data) paint(data);            // refresh the tiles behind the sheet
  build();                         // prefs.urls is not in layoutSig, so force it
  $('sh-b')._html=null;            // force the open sheet to re-read prefs
  renderPop(false);
}
/* ---------- long press to arrange --------------------------------------
   A hold puts the page into arrange mode, where each card carries its size
   button and its remove badge. Dragging to reorder was removed: once the
   cards are grouped into System / Storage / Apps, a tile dragged out of its
   section would have to stop being what the section says it is.          */
const HOLD_MS=450, HOLD_SLOP=10;
let hold=null;

function onDown(ev){
  if(ev.button!==undefined && ev.button!==0) return;
  const wrap=ev.target.closest('#vitals .cardwrap');
  if(!wrap) return;
  if(ev.target.closest('.cardtools button, .xbtn')) return;   // controls are not handles
  if(document.body.classList.contains('editing')) return;
  hold={wrap, start:{x:ev.clientX, y:ev.clientY}, id:ev.pointerId, t:setTimeout(()=>{
    hold=null;
    setEditing(true);
    if(navigator.vibrate) navigator.vibrate(12);
  }, HOLD_MS)};
}

function cancelHold(){ if(hold){ clearTimeout(hold.t); hold=null; } }

function onMove(ev){
  if(!hold) return;
  const dx=Math.abs(ev.clientX-hold.start.x), dy=Math.abs(ev.clientY-hold.start.y);
  if(dx>HOLD_SLOP || dy>HOLD_SLOP) cancelHold();      // a scroll, not a press
}


document.addEventListener('pointerdown', onDown);
document.addEventListener('pointermove', onMove, {passive:true});
document.addEventListener('pointerup', cancelHold);
document.addEventListener('pointercancel', cancelHold);
// a press that turns into a scroll must not arm the hold
document.addEventListener('scroll', cancelHold, {passive:true});

document.addEventListener('click',ev=>{
  const u=ev.target.closest('.apptile .upd');
  if(u){ ev.preventDefault(); ev.stopPropagation();
         askUpdate(u.dataset.app,u.dataset.name,u.dataset.ver); return; }
  // the tile itself now opens the sheet; Launch inside it is what opens the app
  const tile=ev.target.closest('.apptile');
  if(tile){ ev.preventDefault();
            openPop('app-'+tile.dataset.uid); return; }
  if(ev.target.id==='ap-opt'){ appOptions(); return; }
  if(ev.target.id==='ap-launch'){
    const id=openKey.slice(4);
    const s=((data&&data.services)||[]).filter(x=>x.id===id)[0]||{};
    const u=appURL(s);
    if(u) window.open(u,'_blank','noopener');
    return;
  }
  if(ev.target.id==='au-save'){ saveAppURL(appUrlId); return; }
  if(ev.target.id==='au-clear'){
    prefs.urls=Object.assign({}, prefs.urls);
    delete prefs.urls[appUrlId];
    savePrefs(); if(data) paint(data);
    build();
    // the sheet is still open - rebuild it so the pin marker and the listed
    // address go back to auto straight away, not on the next poll
    const box=$('ap-optbox'); if(box){ box._open=false; box.innerHTML=''; }
    $('sh-b')._html=null; renderPop(false);
    return; }
  if(ev.target.id==='u-no'){ closePop(); return; }
  const vb=ev.target.closest('button[data-vmact]');
  if(vb){
    const id=vb.dataset.vm, act=vb.dataset.vmact;
    // Stopping a VM can take down whatever runs in it, a router or a home hub.
    // A single stray tap must not do that, so stop and restart ask twice.
    if(act!=='start' && vb.dataset.armed!=='1'){
      const label=vb.textContent;
      vb.dataset.armed='1';
      vb.textContent = act==='stop' ? 'Tap again to stop' : 'Tap again to restart';
      setTimeout(()=>{ if(vb.isConnected && vb.dataset.armed==='1'){
        vb.dataset.armed=''; vb.textContent=label; } }, 4000);
      return;
    }
    vb.dataset.armed='';
    [].forEach.call(document.querySelectorAll('button[data-vmact]'),
                    b=>{ b.disabled=true; });
    vb.textContent = act==='start' ? 'Starting\u2026'
                   : act==='stop'  ? 'Stopping\u2026' : 'Restarting\u2026';
    fetch('/api/vm',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id:Number(id), action:act})})
      .then(r=>r.json())
      .then(r=>{ if(!r.ok) vb.textContent = r.msg || 'that did not work'; })
      .catch(()=>{ vb.textContent='could not reach the NAS'; });
    return;
  }
  if(ev.target.id==='su-yes' || ev.target.id==='u-yes'){
    const btn=ev.target, self=btn.id==='su-yes';
    btn.disabled=true; btn.textContent='Queued\u2026';
    // say so when it was refused; a sheet that closes either way looks like
    // success, and the update then simply never happens
    const fail=(msg)=>{ btn.textContent=msg; btn.disabled=false; };
    fetch('/api/update',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({app: self ? '_self' : btn.dataset.app})})
      .then(r=>r.json())
      .then(r=>{
        if(!r || !r.ok) return fail((r && r.msg) || 'that did not work');
        // back to the home screen; the tile and the banner carry it from here
        localQueued[self ? '_self' : btn.dataset.app]=Date.now();
        closePop();
        if(settingsOpen()) closeSettings();
        if(data) paintHealth(data);
        paintAppStates(); paintBanner(); paintSelfUpdate();
        window.scrollTo({top:0, behavior:'smooth'});
      })
      .catch(()=>fail('could not reach the NAS'));
    return;
  }
  // --- settings menu -----------------------------------------------
  if(ev.target.closest('#settings')){
    ev.preventDefault(); settingsOpen()?closeSettings():openSettings(); return;
  }
  if(ev.target.closest('#settings-back')){ closeSettings(); return; }
  const settingsTarget=ev.target.closest('[data-settings-target]');
  if(settingsTarget){
    document.querySelectorAll('.settings-nav button').forEach(b=>b.classList.remove('active'));
    settingsTarget.classList.add('active');
    const section=$(settingsTarget.dataset.settingsTarget);
    if(section) section.scrollIntoView({behavior:'smooth',block:'start'});
    return;
  }
  const seg=ev.target.closest('#seg-theme button');
  if(seg){ prefs.theme=seg.dataset.theme; savePrefs(); applyTheme(); return; }
  const sw=ev.target.closest('#sw-accent button');
  if(sw){ prefs.accent=sw.dataset.accent; savePrefs(); applyAccent(); return; }
  if(ev.target.closest('#mi-edit')){ closeSettings(); setEditing(true); return; }
  if(ev.target.closest('#mi-reset')){ resetLayout(); return; }
  if(ev.target.closest('#mi-setup')){ closeSettings(); openSetup(true); return; }
  const lk=ev.target.closest('#su-links button, #m-links button');
  if(lk){ setLinks(lk.dataset.links); return; }
  const selfRow=ev.target.closest('#mi-self');
  if(selfRow){
    if(selfRow.dataset.state==='ready'){ closeSettings(); askSelfUpdate(); }
    else if(selfRow.dataset.state==='idle') doCheck();
    return;
  }
  const sw2=ev.target.closest('.sw2');
  if(sw2){
    const on = sw2.getAttribute('aria-checked')!=='true';
    setSwitch(sw2.id, on);
    const menuKey={'m-ts':'tailscale','m-hx':'hexos'}[sw2.id];
    if(menuKey){                       // in Settings these apply immediately
      prefs.setup=Object.assign({}, prefs.setup, {done:true});
      prefs.setup[menuKey]=on;
      if(menuKey==='tailscale'){
        if(!on) prefs.setup.links='lan';
        else if(!prefs.setup.links) prefs.setup.links='lan';
      }
      savePrefs();
      if(data) paint(data);
      if(menuKey==='tailscale' && on) publishAll();
    }
    return;
  }
  const sth=ev.target.closest('#su-theme button');
  if(sth){ prefs.theme=sth.dataset.theme; savePrefs(); applyTheme();
           document.querySelectorAll('#su-theme button').forEach(b=>
             b.setAttribute('aria-pressed', String(b.dataset.theme===prefs.theme)));
           return; }
  const sac=ev.target.closest('#su-accent button');
  if(sac){ prefs.accent=sac.dataset.accent; savePrefs(); applyAccent(); buildSuAccent(); return; }
  if(ev.target.closest('#su-back')){ setupGo(Math.max(0,setupStep-1)); return; }
  if(ev.target.closest('#su-go')){
    if(setupStep<2) setupGo(setupStep+1); else finishSetup(false);
    return;
  }
  if(ev.target.closest('#su-skip')){ finishSetup(true); return; }
  if(ev.target.closest('#eb-done')){ setEditing(false); return; }
  if(ev.target.closest('#eb-reset')){ resetLayout(); return; }
  // --- rearrange mode ----------------------------------------------
  const wb=ev.target.closest('.cardtools button[data-wide]');
  if(wb){ ev.preventDefault(); ev.stopPropagation();
          toggleWide(wb.dataset.wide); return; }
  const eye=ev.target.closest('.cardtools button[data-eye]');
  if(eye){ ev.preventDefault(); ev.stopPropagation();
           toggleCard(eye.dataset.eye); return; }
  if(document.body.classList.contains('editing')) return;   // cards are inert while editing

  const b=ev.target.closest('button[data-pop]');
  if(b){ openPop(b.dataset.pop); return; }
  if(ev.target.id==='sh-x' || ev.target.id==='scrim') closePop();
});
document.addEventListener('keydown',ev=>{ if(ev.key==='Escape') closePop(); });

/* Lost contact. Every figure on the page is the last one that arrived, so
   without this a NAS that had gone down kept saying "All systems normal" for
   as long as the tab stayed open. Four missed polls and the page says so. */
const pageStart=Date.now();
function paintStale(){
  const since=lastTick||pageStart, gap=Date.now()-since;
  const old=gap>12000;
  document.body.classList.toggle('stale', old);
  paintBanner();
  if(!old) return;
  const wrap=document.querySelector('.cardwrap[data-k="health"]');
  if(selfBusy()){
    // expected: the update restarts the container. Say so calmly.
    if(wrap) wrap.dataset.state='';
    const v=$('v-health'); if(v) v.textContent='Dashy is restarting';
    const sb=$('s-health'); if(sb) sb.textContent='finishing its update \u00b7 back in a moment';
    return;
  }
  if(wrap) wrap.dataset.state='bad';
  const v=$('v-health'); if(v) v.textContent='No contact with Dashy';
  const sec=Math.round(gap/1000);
  const sb=$('s-health');
  if(sb) sb.textContent = lastTick
    ? 'last reading '+(sec<90 ? sec+'s ago' : agoWord(sec))+' \u00b7 figures are frozen'
    : 'waiting for the first reading';
}

let ticking=false;
async function tick(){
  if(ticking) return;           // a slow answer must not stack polls behind it
  ticking=true;
  const ac=('AbortController' in window) ? new AbortController() : null;
  const bail=setTimeout(()=>{ if(ac) ac.abort(); }, 10000);
  try{
    const r=await fetch('/api/stats',{cache:'no-store', signal: ac ? ac.signal : undefined});
    if(!r.ok) throw new Error(r.status);
    paint(await r.json());
    renderPop();
  }catch(e){
    // Recorded, because an exception thrown inside paint() is invisible
    // otherwise and has cost real debugging time more than once.
    window.__lastTickError = (e && e.stack) ? String(e.stack) : String(e);
  }finally{
    clearTimeout(bail);
    ticking=false;
    paintStale();
  }
}
// chrome first, so the theme and accent are right before any data lands
loadPrefs();
buildSwatches();
applyTheme();
// the shared layout is authoritative; the local copy is only a fallback for
// first paint and for when the server has no writable config mount

$('settings-version').textContent='Dashy';
// a system theme change has to re-pick the accent shade too
if(darkMQ){
  const onScheme=()=>{ if(prefs.theme==='system') applyAccent(); };
  darkMQ.addEventListener ? darkMQ.addEventListener('change',onScheme)
                          : darkMQ.addListener(onScheme);
}
document.addEventListener('keydown',ev=>{ if(ev.key==='Escape' && settingsOpen()) closeSettings(); });
function commitNas(){
  const v=($('m-nas').value||'').trim();
  const next=/^https?:\\/\\//.test(v) ? v : '';
  if((prefs.setup||{}).truenas===next) return;
  prefs.setup=Object.assign({}, prefs.setup, {done:true, truenas:next});
  savePrefs();
  if(data) paint(data);
}
document.addEventListener('change', ev=>{ if(ev.target.id==='m-nas') commitNas(); });
document.addEventListener('keydown', ev=>{
  if(ev.target.id==='m-nas' && ev.key==='Enter'){ ev.preventDefault(); commitNas(); $('m-nas').blur(); }
  if(['au-lan','au-ts'].includes(ev.target.id) && ev.key==='Enter'){
    ev.preventDefault(); saveAppURL(appUrlId);
  }
});

tick();
setInterval(tick, 3000);
document.addEventListener('visibilitychange',()=>{ if(!document.hidden) tick(); });
</script>
</body>
</html>
"""


APPDIR = os.path.dirname(os.path.abspath(__file__))

ICONS = {
    "/favicon.png": "favicon.png",
    "/icon-180.png": "icon-180.png",
    "/icon-192.png": "icon-192.png",
    "/icon-512.png": "icon-512.png",
    "/icon-512-maskable.png": "icon-512-maskable.png",
}

MANIFEST = {
    "name": APP_NAME,
    "short_name": APP_NAME,
    "description": "NAS vitals and apps",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#f5f6f8",
    "theme_color": "#f5f6f8",
    "icons": [
        {"src": "/icon-192.png?v=" + APP_VERSION, "sizes": "192x192", "type": "image/png"},
        {"src": "/icon-512.png?v=" + APP_VERSION, "sizes": "512x512", "type": "image/png"},
        {"src": "/icon-512-maskable.png?v=" + APP_VERSION, "sizes": "512x512", "type": "image/png",
         "purpose": "maskable"},
    ],
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "dashy"
    # a client that opens a connection and goes quiet must not hold a thread
    # for ever; every legitimate request here completes in well under this
    timeout = 30

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype, cache="no-store"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_icon(self, name):
        try:
            with open(os.path.join(APPDIR, name), "rb") as fh:
                blob = fh.read()
        except OSError:
            return self._send(404, b"", "image/png")
        # icons only change with a rename, so let devices keep them
        self._send(200, blob, "image/png", cache="public, max-age=604800")

    def _send_appicon(self, name):
        """App icons are cached to disk by the collector, so the browser never
        reaches out to the internet and it works with the tailnet offline."""
        from urllib.parse import unquote
        name = unquote(name)
        if not re.match(r"^[a-z0-9][a-z0-9._-]{0,48}$", name):
            return self._send(404, b"", "image/png")
        d = os.path.join(DATA, "icons")
        for ext, ctype in (("svg", "image/svg+xml"), ("png", "image/png"),
                           ("webp", "image/webp"), ("jpg", "image/jpeg")):
            fp = os.path.join(d, name + "." + ext)
            if os.path.exists(fp):
                try:
                    with open(fp, "rb") as fh:
                        return self._send(200, fh.read(), ctype,
                                          cache="public, max-age=86400")
                except OSError:
                    break
        self._send(404, b"", "image/png")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE.replace("ICON_VERSION", APP_VERSION), "text/html; charset=utf-8")
        elif path.startswith("/appicon/"):
            self._send_appicon(path[len("/appicon/"):])
        elif path in ICONS:
            self._send_icon(ICONS[path])
        elif path == "/manifest.webmanifest":
            self._send(200, json.dumps(MANIFEST), "application/manifest+json",
                       cache="public, max-age=86400")
        elif path == "/api/stats":
            self._send(200, json.dumps(snapshot()), "application/json; charset=utf-8")
        elif path == "/api/layout":
            self._send(200, json.dumps({"layout": load_prefs()}),
                       "application/json; charset=utf-8")
        elif path == "/version":
            self._send(200, json.dumps({"version": APP_VERSION}),
                       "application/json; charset=utf-8")
        elif path == "/healthz":
            self._send(200, "ok", "text/plain; charset=utf-8")
        else:
            self._send(404, json.dumps({"error": "not found"}),
                       "application/json; charset=utf-8")

    do_HEAD = do_GET

    def _json_body(self, limit):
        """The request body as a dict, or None once an error has been sent.

        Every POST here acts on the NAS: it stops VMs, queues upgrades and
        rewrites Tailscale Serve. So two things are refused outright.

        A cross-site request. A page on any other site can make a browser POST
        here; the browser says so in Sec-Fetch-Site. Requiring a JSON content
        type closes the same door for older browsers, because a cross-origin
        JSON POST needs a CORS preflight and this server never grants one.

        A body over the limit. Reading only part of it would leave the rest in
        the stream, where keep-alive parses it as the next request."""
        if self.headers.get("Sec-Fetch-Site", "") == "cross-site":
            self._fail(403, "cross-site request refused")
            return None
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._fail(415, "send application/json")
            return None
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > limit:
            self._fail(413, "request too large")
            return None
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            self._fail(400, "expected a JSON object")
            return None
        return body

    def _fail(self, code, msg):
        self.close_connection = True
        self._send(code, json.dumps({"ok": False, "msg": msg}),
                   "application/json; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        limits = {"/api/layout": 65536, "/api/check": 1024, "/api/vm": 4096,
                  "/api/serve": 8192, "/api/update": 4096}
        if path not in limits:
            return self._fail(404, "not found")
        body = self._json_body(limits[path])
        if body is None:
            return
        if path == "/api/layout":
            ok, msg, rev = save_prefs(body)
            return self._send(200 if ok else 400,
                              json.dumps({"ok": ok, "msg": msg, "rev": rev}),
                              "application/json; charset=utf-8")
        if path == "/api/check":
            ok, msg = check_now()
        elif path == "/api/vm":
            ok, msg = request_vm(body.get("id"), str(body.get("action", "")))
        elif path == "/api/serve":
            ok, msg = request_serve(str(body.get("host") or ""))
        else:
            ok, msg = request_upgrade(str(body.get("app", "")))
        # /api/check reports a failed lookup in its message, not its status
        code = 200 if (ok or path == "/api/check") else 400
        self._send(code, json.dumps({"ok": ok, "msg": msg}),
                   "application/json; charset=utf-8")


if __name__ == "__main__":
    threading.Thread(target=vitals_loop, daemon=True).start()
    threading.Thread(target=services_loop, daemon=True).start()
    time.sleep(2.2)                      # let the first CPU sample land before serving
    print("%s on :%s for %s (tailnet %s), data %s"
          % (APP_NAME, PORT, _state["host"], tailnet_host() or "not published", DATA), flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
