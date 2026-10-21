#!/usr/bin/env python3
"""Dashy dashboard: host vitals plus every tailnet-published app, no dependencies."""
import json
import os
import re
import socket
import threading
from collections import deque
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_VERSION = "0.20.4"
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

# Only used to pretty up names and add a one-liner. Anything not listed still
# appears, titled from whatever the app is actually called on the NAS.
KNOWN = {
    "plex":           ("Plex",           "Films, shows and music"),
    "jellyfin":       ("Jellyfin",       "Second media server"),
    "immich":         ("Immich",         "Photos"),
    "audiobookshelf": ("Audiobookshelf", "Audiobooks and podcasts"),
    "seerr":          ("Seerr",          "Media requests"),
    "requestrr":      ("Requestrr",      "Request bot"),
    "pihole":         ("Pi-hole",        "DNS and ad blocking"),
    "scrutiny":       ("Scrutiny",       "Drive health"),
    "sparkyfitness":  ("SparkyFitness",  "Workouts and food"),
    "slskd":          ("Soulseek",       "slskd"),
    "tally":          ("Tally",          "Scoreboard"),
    # Three keys for one app: it was "dashboard", then "dashy", and is now
    # "dashy" once more. An install that predates a rename still carries the
    # old name, and should still recognise itself.
    "dashboard":      ("Dashy",          "This page"),
    "dashy":          ("Dashy",          "This page"),
    "overlook":       ("Dashy",          "This page"),
    "rust-desk":      ("RustDesk",       "Remote desktop"),
}
def _hostname():
    """The NAS's name for the header. A container's /proc/sys/kernel/hostname is
    its own UTS namespace - host networking shares the network namespace, not
    that one - so it reports a container id unless we are told explicitly."""
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
    return sessions[:12]


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
            _state["boots"] = boots
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
                clean[k] = n
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
        ports = sorted(int(p) for p in (a.get("ports") or []) if p)
        pport, ppath = portal_of(a)

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
        if chosen is None and app_name.lower() == APP_NAME.lower():
            chosen, ppath = PORT, "/"
        if chosen is None:
            continue                 # host networking, or nothing web-facing
        label, desc = KNOWN.get(app_name,
                                (app_name.replace("-", " ").title(), ""))
        out.append({"port": pub.get(chosen), "lport": chosen,
                    "path": ppath if chosen == pport else "/",
                    "name": label, "desc": desc,
                    "state": a.get("state"), "app": app_name,
                    "id": app_name,
                    "version": (APP_VERSION if app_name.lower() == APP_NAME.lower()
                                else a.get("version")),
                    "update": bool(a.get("update")),
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
    if not os.environ.get("HOST_NAME") and tn:
        out["host"] = tn.split(":", 1)[0].split(".", 1)[0]
    out["services"] = svc
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
    --accent:#2563eb; --brand:#2563eb;
    --shadow:0 1px 2px rgba(16,20,28,.04), 0 12px 30px -14px rgba(16,20,28,.20);
    /* M3's shape scale rather than one radius everywhere. Mixing round and
       square is the point: uniform corners read as a template, varied ones
       read as a designed thing. */
    --r-xs:8px; --r-sm:12px; --r-md:16px; --r-lg:22px; --r-xl:28px;
    --r-full:999px;
    --r:var(--r-md);
  }
  /* dark palette twice: once for "follow the system" (suppressed when the user
     has explicitly chosen light), once for an explicit dark choice */
  @media (prefers-color-scheme:dark){
    :root:not([data-theme="light"]){
      --bg:#0f111b; --panel:#171b22; --surf:#1b1e2b; --brd:rgba(255,255,255,.09);
      --ink:#eef1f5; --muted:#949cab; --faint:rgba(255,255,255,.06);
      --ok:#34d399; --warn:#fbbf24; --bad:#f87171;
      --accent:#5b8cff; --brand:#5b8cff;
      --shadow:0 1px 2px rgba(0,0,0,.4), 0 18px 42px -18px rgba(0,0,0,.7);
      color-scheme:dark;
    }
  }
  :root[data-theme="dark"]{
      --bg:#0f111b; --panel:#171b22; --surf:#1b1e2b; --brd:rgba(255,255,255,.09);
      --ink:#eef1f5; --muted:#949cab; --faint:rgba(255,255,255,.06);
      --ok:#34d399; --warn:#fbbf24; --bad:#f87171;
      --accent:#5b8cff; --brand:#5b8cff;
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
  .beta{font-size:9.5px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;
        color:var(--muted);margin:1px 0 0}
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
                  background:var(--panel);box-shadow:0 8px 24px -21px rgba(0,0,0,.45)}
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
  #mi-self[data-state="idle"]{color:var(--muted);cursor:default}
  #mi-self[data-state="idle"]:hover{background:none}
  .opt.col{display:block}
  .opt.col .seg{margin-top:10px}
  .seg.sm{margin-top:8px}
  .seg.sm button{padding:6px 10px;font-size:12.5px}
  .pubmsg{display:block;margin-top:7px;font-size:12px;color:var(--muted)}
  .pubmsg.bad{color:var(--warn)}
  /* update state */
  @keyframes spin{to{transform:rotate(360deg)}}
  /* a check that takes two seconds and says nothing looks like a dead button */
  #mi-check[data-state="busy"] svg{animation:spin .85s linear infinite;
                                   transform-origin:50% 50%}
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
  .cardtools{position:absolute;inset:0;display:none;align-items:center;justify-content:center;
             gap:7px;background:color-mix(in srgb,var(--panel) 97%,transparent);
             border-radius:var(--r-md);z-index:3}
  body.editing .cardtools{display:flex}
  .cardtools button{width:30px;height:30px;border-radius:9px;border:1px solid var(--brd);
                    background:var(--bg);color:var(--ink);font-size:14px;line-height:1;
                    cursor:pointer;display:flex;align-items:center;justify-content:center;
                    padding:0}
  .cardtools button:disabled{opacity:.3;cursor:default}
  .cardtools .wbtn{font-size:11px;font-weight:700;letter-spacing:-.02em}
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
  /* and a big one spans two of each - four cells, hence 4x */
  .cardwrap[data-w="4"]{grid-column:span 2;grid-row:span 2}
  .cardwrap[data-w="4"] .jig,
  .cardwrap[data-w="4"] .card{min-height:100%}
  .spark{margin-top:0;height:20px;width:100%;display:block;overflow:visible}
  .cardwrap[data-w="2"] .spark{height:40px}
  /* the container has a fixed height, so a taller chart drew straight out
     through the bottom of the card. Give it the room it is asking for. */
  .cardwrap[data-w="4"] .spark{margin-top:14px;height:132px}
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

  /* Filled, not outlined. In M3 a surface is defined by its tone; a border
     and a drop shadow are both ways of faking separation that colour does
     better. --surf is a tonal step up from the page, so the card reads as
     raised without anything being drawn around it. */
  .card{position:relative;background:var(--surf);border:none;
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
  .cardwrap[data-state="warn"] .hcard .hv{color:var(--warn)}
  .cardwrap[data-state="bad"] .hcard .hv{color:var(--bad)}
  .lbl{font-size:11px;letter-spacing:.02em;color:var(--muted);
       font-weight:600;margin:0;text-align:left;align-self:flex-start}
  .vmico{width:22px;height:22px;border-radius:6px;flex:0 0 auto;color:#fff;
         display:inline-flex;align-items:center;justify-content:center}
  .vmico svg{width:13px;height:13px}
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

  .appgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(134px,1fr));gap:9px}
  .sheet .appgrid{padding:12px 16px 14px}
  /* Deliberately a smaller corner than the metric cards above them: the
     shape is what separates the two groups now that nothing is outlined. */
  .apptile{position:relative;text-decoration:none;color:inherit;display:flex;
           flex-direction:column;gap:6px;background:var(--surf);
           border:none;border-radius:var(--r-sm);
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
  .apptile .ap{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}
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
  .apptile .ust{position:absolute;top:24px;right:6px;font-size:9px;font-weight:700;
                letter-spacing:.04em;text-transform:uppercase;border-radius:999px;padding:2px 6px}
  .apptile .ust.running{color:var(--warn);background:color-mix(in srgb,var(--warn) 18%,transparent)}
  .apptile .ust.done{color:var(--ok);background:color-mix(in srgb,var(--ok) 18%,transparent)}
  .apptile .ust.failed{color:var(--bad);background:color-mix(in srgb,var(--bad) 18%,transparent)}
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
      <div style="min-width:0"><h1>Dashy<span class="host" id="host"></span></h1>
        <p class="beta">beta</p></div>
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
        <button type="button" id="settings-nav-back">Dashboard</button>
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
              <p>Check for updates to your installed TrueNAS apps.</p>
              <button class="mi" id="mi-check" type="button">
                <span id="mi-checktxt">Check for updates</span><span class="rt" id="mi-checkmsg"></span></button>
            </article>
            <article class="settings-card"><div class="settings-glyph">↑</div><h4>Dashy update</h4>
              <p>Keep Dashy current with the latest release.</p>
              <button class="mi" id="mi-self" type="button">
                <span class="dots" id="mi-selfdots" hidden><i></i><i></i><i></i></span>
                <span id="mi-selftxt">Update Dashy</span><span class="rt" id="mi-selfver"></span></button>
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
        <span><b>Make it yours.</b> Drag, resize and hide tiles. Your layout
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

/* ---------- preferences ------------------------------------------------
   Per device, in localStorage. Dashy has no login, so there is nowhere else
   sensible to put them, and every read is guarded: private windows and
   blocked site data both throw rather than return null.                   */
const PKEY='overlook.prefs.v2';
/* Blue leads, but the four below stay: amber, red and green already mean warn,
   bad and ok, so an accent that strays into them reads as an alert that is not
   there. Everything on offer sits in the blue-to-stone range. */
const ACCENTS={
  blue:    {light:'#2563eb', dark:'#5b8cff', name:'Blue'},
  dusk:    {light:'#4a57c8', dark:'#7d87e6', name:'Dusk'},
  harbour: {light:'#1d7391', dark:'#49a7c6', name:'Harbour'},
  heather: {light:'#7b57ad', dark:'#a583d6', name:'Heather'},
  granite: {light:'#51606e', dark:'#8c9bab', name:'Granite'}
};

/* A saved choice may no longer exist. Map it to the nearest survivor rather
   than silently dropping someone back to default. */
const ACCENT_WAS={teal:'harbour', blue:'dusk', violet:'heather',
                  amber:'granite', yellow:'granite', rose:'heather', green:'harbour'};
function migrateAccent(a){ return ACCENTS[a] ? a : (ACCENT_WAS[a] || 'blue'); }
const DEFAULTS={theme:'system', accent:'blue', order:CARDK.slice(), hidden:[], wide:[], size:{},
                setup:{}, urls:{}};
let prefs=Object.assign({}, DEFAULTS);
let lastTick=0;
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
      if(Array.isArray(d.hidden)) prefs.hidden=d.hidden.filter(k=>CARDK.indexOf(k)>=0);
      if(Array.isArray(d.wide)) prefs.wide=d.wide.filter(k=>CARDK.indexOf(k)>=0);
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
    const next=P.hidden.filter(k=>CARDK.indexOf(k)>=0);
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
    const next=P.wide.filter(k=>CARDK.indexOf(k)>=0);
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
const SIZES=[1,2,4];
function sizeOf(k){ const n=prefs.size[k]; return SIZES.indexOf(n)>=0 ? n : 1; }
function nextSize(k){ return SIZES[(SIZES.indexOf(sizeOf(k))+1) % SIZES.length]; }

function cleanSize(o){
  const out={};
  for(const k in o){
    if(CARDK.indexOf(k)<0) continue;
    const n=Number(o[k]);
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

function cardTools(k){
  return '<div class="cardtools">'+
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
               '<p class="hfoot" id="f-health"></p>'+
             '</button>'+cardTools(c.k)+
           '</div></div>';
  }
  const head = c.dot
    ? '<div class="bigrow"><i class="pdot ok" id="'+c.dot+'"></i>'+
        '<p class="big" id="v-'+c.k+'">\u2013</p></div>'+
      '<p class="sub" id="s-'+c.k+'"></p>'
    : '<p class="big" id="v-'+c.k+'">\u2013</p><p class="sub" id="s-'+c.k+'"></p>';
  const body = head;
  const spark = c.trend
    ? '<div class="sparkwrap"><svg class="spark" id="k-'+c.k+
      '" preserveAspectRatio="none" viewBox="0 0 160 20"></svg></div>' : '';
  const tools=cardTools(c.k);
  return '<div class="cardwrap" data-k="'+c.k+'">'+
           '<div class="jig">'+
             '<button class="card" data-pop="'+(c.pop||c.k)+'">'+
               '<p class="lbl">'+c.label+'</p>'+body+spark+
             '</button>'+tools+
           '</div>'+
         '</div>';
}

/* The pools are not known until the first poll, so the Storage section is
   built from the data rather than declared. One card per pool: "2 pools, 60%
   full" hid which pool was the full one, which is the only thing you wanted
   to know. */
function poolCards(){
  return ((data&&data.pools)||[]).map(p=>({
    k:'pool-'+p.name, label:p.name, sec:'storage', pop:'store', pool:p.name}));
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

function vmIcon(name){
  const h=hueOf(name||'vm');
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
          '<p class="lbl vmlbl">'+vmIcon(vmName(v))+
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
  mem:   d=>[(d.mem_hist||[]).map(r=>[r[0],r[1]+r[2]]), 'var(--brand)', null],
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
    const h = w==='4' ? 112 : (w==='2' ? 40 : 20);
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
    $('s-mem').textContent=ram(d.mem.apps!==undefined?d.mem.apps:d.mem.used)+' of '+ram(d.mem.total)
      +(d.mem.arc?' \\u00b7 '+ram(d.mem.arc)+' cache':'');
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
    const k='pool-'+p.name;
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
    bad=true;
  }else if(recent!==null && recent < 86400){
    note = 'restarted '+durLong(recent)+' ago';
    issue=true;
  }
  footNote=note; footIssue=issue;


  $('host').textContent=d.host||'';
  paintHealth(d);
  drawSparks(d);
  paintLinks(d.links||{});
  // uptime lives in the settings pane now; the home screen stays clean
  const mv=$('settings-version');
  mv.innerHTML='<span>'+(d.app||'Dashy')+' v'+d.version+'</span>'+
    '<span class="rt"><i class="pdot '+(footIssue?'warn':'ok')+'"></i>up '+
    footUp[0]+' '+footUp[1]+'</span>';
  mv.title = footIssue && footNote ? footNote : '';
  lastTick=Date.now();
}

/* The collector looks every half hour, which is the right cadence for a
   background check and the wrong one for somebody standing there waiting.
   This asks now, and shows that it is asking. */
let checking=false;
function doCheck(){
  if(checking) return;
  checking=true;
  const row=$('mi-check'), t=$('mi-checktxt'), m=$('mi-checkmsg');
  if(row) row.dataset.state='busy';
  if(t) t.textContent='Checking\u2026';
  if(m) m.textContent='';
  const done=(msg)=>{
    checking=false;
    if(row) row.dataset.state='';
    if(t) t.textContent='Check for updates';
    if(m) m.textContent=msg||'';
  };
  // a registry that never answers would otherwise spin for ever
  const ac=('AbortController' in window) ? new AbortController() : null;
  const bail=setTimeout(()=>{ if(ac) ac.abort(); }, 20000);
  fetch('/api/check',{method:'POST', signal: ac ? ac.signal : undefined})
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
    const ic=b.querySelector('svg'), dots=$('mi-selfdots');
    if(ic) ic.style.display = state==='running' ? 'none' : '';
    if(dots) dots.hidden = state!=='running';
    if(txt) txt.textContent=label;
    if(ver) ver.textContent=right||'';
    if(gear) gear.classList.toggle('dot', state==='ready');
  };

  // Both of these are written to a file that nothing ever clears, so they age
  // out here or they pin the row: 'Updated' would sit in the menu for weeks,
  // and a worker that died mid-update would leave 'Updating...' forever.
  const age=(st && st.t) ? (Date.now()/1000 - st.t) : 1e9;
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
  if(st && st.state==='failed' && age < 3600)
    return set('idle','Install update', st.msg||'that did not work');
  if(st && st.state==='done' && age < 3600) return set('idle','Install update','just updated');
  set('idle','Install update', u ? 'up to date' : 'not checked yet');
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

// the single most pressing thing about the machine, worst first
/* How bad is it, in one word. The card colours itself from this, which is
   the whole point: an amber border reads as "this one" far faster than an
   amber digit inside an identical white box. */
function severity(d){
  const pools=d.pools||[], drives=d.drives||[], svc=d.services||[];
  if(pools.some(p=>(p.health||'').toUpperCase()!=='ONLINE')) return 'bad';
  if(drives.some(x=>x.ok===false)) return 'bad';
  if(drives.some(x=>x.level==='bad')) return 'bad';
  if(svc.some(s=>!s.code)) return 'warn';
  if(pools.some(p=>p.pct>=85)) return 'warn';
  if(drives.some(x=>x.level==='warn')) return 'warn';
  return 'ok';
}

function paintHealth(d){
  const sev=severity(d), msg=headline(d);
  const wrap=document.querySelector('.cardwrap[data-k="health"]');
  if(wrap) wrap.dataset.state = sev==='ok' ? '' : sev;
  const v=$('v-health'); if(v) v.textContent = sev==='ok' ? 'All systems normal' : msg;
  const sb=$('s-health');
  if(sb) sb.textContent = sev==='ok' ? '' : 'everything else normal';
  const f=$('f-health');
  if(f) f.textContent = (d.uptime||d.uptime===0) ? ('up '+upWord(d.uptime)) : '';
}

function upWord(sec){
  const d=Math.floor(sec/86400);
  if(d>=2) return d+' days';
  const h=Math.floor(sec/3600);
  if(h>=2) return h+' hours';
  return Math.max(1,Math.floor(sec/60))+' min';
}

function headline(d){
  const pools=d.pools||[], drives=d.drives||[], svc=d.services||[];
  const sick=pools.find(p=>(p.health||'').toUpperCase()!=='ONLINE');
  if(sick) return sick.name+' '+sick.health;
  const failed=drives.find(x=>x.ok===false);
  if(failed) return failed.dev+' SMART failure';
  const hotDrive=drives.find(x=>x.level==='bad');
  if(hotDrive) return hotDrive.dev+' at '+hotDrive.temp+'\\u00b0C';
  const down=svc.filter(s=>!s.code);
  if(down.length) return down.length===1 ? down[0].name+' not responding'
                                         : down.length+' apps not responding';
  const full=pools.slice().sort((a,b)=>b.pct-a.pct)[0];
  if(full && full.pct>=85) return full.name+' '+full.pct.toFixed(0)+'% full';
  // the old header pill used to carry this; keep the signal, lose the box
  if(d.pools_age!==null&&d.pools_age!==undefined&&d.pools_age>180) return 'pool data stale';
  const warm=drives.find(x=>x.level==='warn');
  if(warm) return warm.dev+' at '+warm.temp+'\\u00b0C';
  return 'all systems normal';
}

function rowsCores(){
  const list=data.cpu_cores||[];
  if(!list.length) return '<div class="row"><span class="t2">No per-thread data yet.</span></div>';
  return list.map(c=>
    '<div class="row"><span class="tag">'+c.name.trim()+'</span>'+
      '<div class="bar"><i style="width:'+c.pct+'%" data-level="'+level(c.pct)+'"></i></div>'+
      '<span class="val" data-level="'+level(c.pct)+'">'+c.pct.toFixed(0)+'%</span></div>').join('');
}

function rowsCoreTemps(){
  const list=data.core_temps||[];
  if(!list.length) return '<div class="row"><span class="t2">No per-core sensors.</span></div>';
  return list.map(c=>{
    const lv=c.temp>=85?'bad':c.temp>=75?'warn':'ok';
    const w=Math.max(4,Math.min(100,(c.temp-25)/70*100));
    return '<div class="row"><span class="tag">'+c.name+'</span>'+
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
      '<i style="width:'+pc(arc)+'%;background:var(--brand)"></i>'+
      '<i style="width:'+pc(free)+'%;background:transparent"></i></div>'+
    '<div class="legend">'+
      '<span><u style="background:var(--accent)"></u>Apps '+ram(apps)+'</span>'+
      '<span><u style="background:var(--brand)"></u>ZFS cache '+ram(arc)+'</span>'+
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
  else if (st.errors) bits.push(st.errors.toLowerCase());
  if (!bits.length) return '';
  return '<p class="pnote'+(bad ? ' bad' : '')+'">'+bits.join('  \u00b7  ')+'</p>';
}

function rowsPools(){
  return (data.pools||[]).map(p=>{
    const ok=(p.health||'').toUpperCase()==='ONLINE', lv=level(p.pct);
    const col = lv==='ok' ? 'var(--accent)' : lv==='warn' ? 'var(--warn)' : 'var(--bad)';
    return '<div class="row"><span class="l"><span class="t1">'+p.name+'</span>'+
      '<span class="t2">'+disk(p.used)+' used \\u00b7 '+disk(p.free)+' free of '+disk(p.size)+'</span></span>'+
      '<span class="smart '+(ok?'ok':'bad')+'">'+(p.health||'?')+'</span>'+
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
      '<span class="l"><span class="t1">'+x.dev+'</span>'+
      '<span class="t2">'+(x.size?disk(x.size):'')+(x.model?' \\u00b7 '+x.model:'')+yrs+'</span></span>'+
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
  const n = (data.procs && data.procs.ncpu) || (data.cpu_cores || 0);
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
    out.push(srow('bad', 'Dashy ' + su.latest + ' is available',
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
                    disk(p.free) + ' left of ' + disk(p.size), 'pool'));
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

  const head = out.length
    ? '<p class="shead">' + out.length + (out.length === 1 ? ' thing wants' : ' things want') +
      ' your attention</p>'
    : '<div class="allok"><p>Nothing needs you.</p>' +
      '<p class="t2">Every app is answering, every pool is online and no drive is warm.</p></div>';

  // what is normal, so the sheet says something either way
  const normal = [];
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
    '<span class="l"><span class="t1">' + title + '</span>' +
    (sub ? '<span class="t2">' + sub + '</span>' : '') + '</span>' +
    '</' + (pop ? 'button' : 'div') + '>';
}

function rowsApps(){
  const list=data.services||[];
  const st=data.updates||{};
  if(!list.length) return '<div class="row"><span class="t2">Nothing published yet.</span></div>';
  return '<div class="appgrid">'+list.map(s=>
    '<a class="apptile" data-up="'+(s.code?'1':'0')+'"'+
      ' data-uid="'+esc(s.id)+'" data-name="'+esc(s.name)+'"'+
      ' href="'+appURL(s)+'"'+
      ' role="button" aria-label="'+esc(s.name)+' details">'+
      '<span class="sdot"></span>'+
      (s.update?'<button class="upd" data-app="'+s.app+'" data-name="'+s.name+'" '+
                 'data-ver="'+(s.version||'')+'" aria-label="Update '+s.name+'"></button>':'')+
      (st[s.app]?'<span class="ust '+st[s.app].state+'">'+st[s.app].msg+'</span>':'')+
      (isPinned(s)?'<span class="pin" aria-hidden="true"></span>':'')+
      appIcon(s)+
      '<span class="an">'+s.name+'</span>'+
      '<span class="ap">'+(s.version||'')+'</span></a>').join('')+'</div>';
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
  const netDev=(data.net||[]).slice().sort((a,b)=>(b.down+b.up)-(a.down+a.up))[0];
  const netHist=netDev?(data.net_hist||{})[netDev.name]||[]:[];
  const netNow=netDev?(netDev.down||0)+(netDev.up||0):0;
  const ioNow=(data.io||[]).reduce((n,x)=>n+(x.read||0)+(x.write||0),0);
  const ioHist=(data.io_hist||[]).map(r=>[r[0],(r[1]||0)+(r[2]||0)]);
  const ramHist=(data.mem_hist||[]).map(r=>[
    r[0], data.mem&&data.mem.total ? 100*((r[1]||0)+(r[2]||0))/data.mem.total : 0]);
  const memPct=data.mem?Math.round(data.mem.pct):null;
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
    [{key:1,colour:'var(--accent)',label:'apps'},{key:2,colour:'var(--brand)',label:'ZFS cache'}], ram);
}
function powerChart(){
  return chart(data.power_hist||[],
    [{key:1,colour:'var(--warn)',label:'package watts'}], v=>v.toFixed(0)+' W');
}

function rowsIo(){
  const list=(data.io||[]).filter(x=>x.read!==null&&x.read!==undefined&&(x.read||x.write));
  if(!list.length) return '';
  return list.map(x=>
    '<div class="row"><span class="l"><span class="t1">'+x.name+'</span>'+
      '<span class="t2">live disk activity</span></span>'+
      '<span class="val" style="min-width:92px">'+rate(x.read)+'<br>'+
      '<span style="font-size:11px;color:var(--muted);font-weight:600">'+rate(x.write)+' write</span>'+
      '</span></div>').join('');
}

function rowsNet(){
  const list=data.net||[];
  if(!list.length) return '<div class="row"><span class="t2">No interface data.</span></div>';
  return list.map(n=>
    '<div class="row"><span class="l"><span class="t1">'+n.name+'</span>'+
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
  cpu:   ()=>['CPU', loadRow()+powerChart()+
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
    '<div class="row"><span class="l"><span class="t1">'+name+'</span>'+
      '<span class="t2">currently '+(ver||'unknown')+'</span></span></div>'+
    '<div class="row"><span class="t2 wrap">The app will restart while it upgrades, so it will be '+
      'briefly unavailable. A ZFS snapshot of its data is taken first, so there is a rollback '+
      'point. The upgrade starts within a minute.</span></div>'+
    '<div class="m-row" style="padding:12px 16px 14px;display:flex;gap:10px">'+
      '<button class="ubtn no" id="u-no">Cancel</button>'+
      '<button class="ubtn yes" id="u-yes" data-app="'+app+'">Update '+name+'</button></div>';
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
  if(ev.target.id==='su-yes'){
    ev.target.disabled=true; ev.target.textContent='Queued\u2026';
    fetch('/api/update',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({app:'_self'})}).catch(()=>{});
    setTimeout(closePop, 900);
    return;
  }
  if(ev.target.id==='u-yes'){
    const app=ev.target.dataset.app;
    ev.target.disabled=true; ev.target.textContent='Queued\\u2026';
    fetch('/api/update',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({app:app})})
      .then(r=>r.json()).then(()=>{ setTimeout(()=>{ openPop('apps'); }, 600); })
      .catch(()=>{ ev.target.textContent='Failed'; });
    return;
  }
  // --- settings menu -----------------------------------------------
  if(ev.target.closest('#settings')){
    ev.preventDefault(); settingsOpen()?closeSettings():openSettings(); return;
  }
  if(ev.target.closest('#settings-back')){ closeSettings(); return; }
  if(ev.target.closest('#settings-nav-back')){ closeSettings(); return; }
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
  if(ev.target.closest('#mi-check')){ doCheck(); return; }
  const selfRow=ev.target.closest('#mi-self');
  if(selfRow){
    if(selfRow.dataset.state==='ready'){ closeSettings(); askSelfUpdate(); }
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

async function tick(){
  try{
    const r=await fetch('/api/stats',{cache:'no-store'});
    if(!r.ok) throw new Error(r.status);
    paint(await r.json());
    renderPop();
  }catch(e){
    // Swallowed on purpose - a dropped poll must not kill the page - but
    // recorded, because an exception thrown inside paint() is invisible
    // otherwise and has cost real debugging time more than once.
    window.__lastTickError = (e && e.stack) ? String(e.stack) : String(e);
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
    server_version = "soldash"

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

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/layout":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
            except (ValueError, TypeError):
                body = {}
            ok, msg, rev = save_prefs(body)
            return self._send(200 if ok else 400,
                              json.dumps({"ok": ok, "msg": msg, "rev": rev}),
                              "application/json; charset=utf-8")
        if path == "/api/check":
            ok, msg = check_now()
            return self._send(200, json.dumps({"ok": ok, "msg": msg}),
                              "application/json; charset=utf-8")
        if path == "/api/vm":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 4096)) or b"{}")
            except (ValueError, TypeError):
                body = {}
            ok, msg = request_vm(body.get("id"), str(body.get("action", "")))
            return self._send(200 if ok else 400,
                              json.dumps({"ok": ok, "msg": msg}),
                              "application/json; charset=utf-8")
        if path == "/api/serve":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 8192)) or b"{}")
            except (ValueError, TypeError):
                body = {}
            ok, msg = request_serve(body.get("host") or "")
            return self._send(200 if ok else 400,
                              json.dumps({"ok": ok, "msg": msg}),
                              "application/json; charset=utf-8")
        if path != "/api/update":
            return self._send(404, json.dumps({"error": "not found"}),
                              "application/json; charset=utf-8")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, TypeError):
            body = {}
        ok, msg = request_upgrade(str(body.get("app", "")))
        self._send(200 if ok else 400,
                   json.dumps({"ok": ok, "msg": msg}),
                   "application/json; charset=utf-8")


if __name__ == "__main__":
    threading.Thread(target=vitals_loop, daemon=True).start()
    threading.Thread(target=services_loop, daemon=True).start()
    time.sleep(2.2)                      # let the first CPU sample land before serving
    print("%s on :%s for %s (tailnet %s), data %s"
          % (APP_NAME, PORT, _state["host"], tailnet_host() or "not published", DATA), flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
