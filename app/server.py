#!/usr/bin/env python3
"""Sol dashboard: host vitals plus every tailnet-published app, no dependencies."""
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

APP_VERSION = "0.13.0"
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

# Weather is OFF unless coordinates are supplied. There is no geolocation and
# no IP lookup anywhere in this app: whoever installs it types a latitude and
# longitude, or there is no weather. When it is on, this machine (not the
# browser) calls api.open-meteo.com every 15 minutes with those coordinates.
# Open-Meteo returns no place name, and a reverse-geocode call for one string
# is not worth it - the timezone's last segment is the city in every case here.
WEATHER_TTL = 900.0          # 15 min is plenty; the API asks for restraint
WEATHER_FAIL_TTL = 120.0     # after a failure, back off before retrying

SERVE = os.environ.get("SERVE_JSON", "/serve/serve.json")


def serve_tailnet():
    """This machine's own tailnet name, read from whatever Serve publishes.
    Nothing about the install is written down: serve.json already knows which
    host it is answering for, so a copy of this app on another NAS names
    itself correctly with no configuration."""
    try:
        with open(SERVE) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return ""
    for host in (cfg.get("Web") or {}):
        h = host.rsplit(":", 1)[0].strip()
        if h:
            return h
    return ""


# an explicit setting wins, otherwise discover it, otherwise fall back to the host
TAILNET = os.environ.get("TAILNET_HOST", "").strip() or serve_tailnet()
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
    "dashboard":      ("Dashy",          "This page"),
    "dashy":          ("Dashy",          "This page"),
    "rust-desk":      ("RustDesk",       "Remote desktop"),
}
# host-networked apps publish no ports, so they cannot be matched by owner
# Host networking means this app publishes no ports, so it never shows up in
# apps.json and has to recognise itself by the port it was told to serve on.
PORT_FALLBACK = {
    80:   ("TrueNAS", "The NAS itself"),
    443:  ("TrueNAS", "The NAS itself"),
    PORT: (APP_NAME,  "This page"),
}

def _hostname():
    """The NAS's name for the header. A container's /proc/sys/kernel/hostname is
    its own UTS namespace - host networking shares the network namespace, not
    that one - so it reports a container id unless we are told explicitly."""
    v = os.environ.get("HOST_NAME", "").strip()
    if v:
        return v.split(".")[0]
    # TAILNET_HOST is already set per install, and its first label is the machine
    if TAILNET:
        first = TAILNET.split(":")[0].split(".")[0].strip()
        if first:
            return first
    return "nas"


_lock = threading.Lock()
HIST = 150          # samples kept per interface, 2s apart -> about 5 minutes
_hist = {}
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


_wx = {"at": 0.0, "data": None}


def wmo(code, is_day):
    """WMO weather code -> (icon class the page draws, words). Unknown stays blank."""
    sun = "clear" if is_day else "night"
    table = [
        ((0,), sun, "clear"),
        ((1,), sun, "mainly clear"),
        ((2,), "partly" if is_day else "partlynight", "partly cloudy"),
        ((3,), "cloud", "overcast"),
        ((45, 48), "fog", "fog"),
        ((51, 53, 55, 56, 57), "drizzle", "drizzle"),
        ((61, 63, 65, 66, 67), "rain", "rain"),
        ((80, 81, 82), "rain", "showers"),
        ((71, 73, 75, 77, 85, 86), "snow", "snow"),
        ((95, 96, 99), "storm", "thunderstorm"),
    ]
    for codes, icon, words in table:
        if code in codes:
            return icon, words
    return "cloud", ""


def hourly_from(d, now_iso, span=24):
    """The next `span` hours starting at the current one. Open-Meteo returns
    local ISO strings like 2026-10-09T14:00, so align on the hour text."""
    h = d.get("hourly") or {}
    times = h.get("time") or []
    temps = h.get("temperature_2m") or []
    pops = h.get("precipitation_probability") or []
    codes = h.get("weather_code") or []
    if not times or not temps:
        return []

    start = 0
    if now_iso:
        stamp = now_iso[:13]            # YYYY-MM-DDTHH
        for i, t in enumerate(times):
            if t[:13] == stamp:
                start = i
                break

    out = []
    for i in range(start, min(start + span, len(times), len(temps))):
        try:
            icon, _ = wmo(int(codes[i]), (h.get("is_day") or [1])[i] == 1) \
                if i < len(codes) else ("cloud", "")
            out.append({
                "t": times[i][11:16],                       # HH:MM
                "temp": round(float(temps[i])),
                "pop": int(pops[i]) if i < len(pops) and pops[i] is not None else 0,
                "icon": icon,
            })
        except (TypeError, ValueError, IndexError):
            continue
    return out


def weather_where():
    """Coordinates come from the town chosen during setup, and nowhere else.
    Weather is off until someone picks one - there are no built-in coordinates,
    no IP geolocation and no browser location API."""
    p = (load_prefs() or {}).get("weather") or {}
    if p.get("lat") is not None and p.get("lon") is not None:
        return (str(p["lat"]), str(p["lon"]),
                p.get("tz") or "UTC", p.get("name") or "")
    return None


def geocode(q):
    """Name to coordinates, proxied so the browser never talks to open-meteo
    itself. Town-level only - this API has no street addresses."""
    q = (q or "").strip()
    if not 2 <= len(q) <= 60:
        return []
    url = ("https://geocoding-api.open-meteo.com/v1/search?name=%s"
           "&count=6&language=en&format=json" % urllib.parse.quote(q))
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception:
        return []
    out = []
    for g in (d.get("results") or [])[:6]:
        bits = [g.get("name"), g.get("admin1"), g.get("country_code")]
        out.append({"name": ", ".join(b for b in bits if b),
                    "lat": round(float(g["latitude"]), 4),
                    "lon": round(float(g["longitude"]), 4),
                    "tz": g.get("timezone") or "UTC"})
    return out


def weather_now():
    where = weather_where()
    if not where:
        return None
    WEATHER_LAT_, WEATHER_LON_, tz_, place_ = where
    """Current conditions, cached. Any failure returns None and the widget hides."""
    key = (where[0], where[1])
    if _wx.get("key") != key:
        _wx["key"], _wx["at"], _wx["data"] = key, 0.0, None
    now = time.monotonic()
    if _wx["data"] is not None and now - _wx["at"] < WEATHER_TTL:
        return _wx["data"]
    if _wx["data"] is None and _wx["at"] and now - _wx["at"] < WEATHER_FAIL_TTL:
        return None

    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
           "&current=temperature_2m,apparent_temperature,weather_code,is_day"
           "&hourly=temperature_2m,precipitation_probability,weather_code,is_day"
           "&daily=temperature_2m_max,temperature_2m_min"
           "&timezone=%s&forecast_days=2"
           % (WEATHER_LAT_, WEATHER_LON_, urllib.parse.quote(tz_)))
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            d = json.loads(r.read().decode("utf-8"))
        cur = d["current"]
        day = d.get("daily") or {}
        icon, words = wmo(int(cur["weather_code"]), int(cur.get("is_day", 1)) == 1)
        out = {
            "temp": round(float(cur["temperature_2m"])),
            "feels": round(float(cur["apparent_temperature"])),
            "icon": icon,
            "words": words,
            "hi": round(float(day["temperature_2m_max"][0])) if day.get("temperature_2m_max") else None,
            "lo": round(float(day["temperature_2m_min"][0])) if day.get("temperature_2m_min") else None,
            "hours": hourly_from(d, cur.get("time")),
            "place": place_ or tz_.rsplit("/", 1)[-1].replace("_", " "),
        }
        _wx["data"] = out
    except Exception:          # offline, rate limited, shape changed - all the same here
        _wx["data"] = None
    _wx["at"] = now
    return _wx["data"]


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

    `host` is optional and normally empty: the Tailscale container substitutes
    ${TS_CERT_DOMAIN} with this node's own name, so nobody has to go and look
    it up."""
    host = (host or "").strip().lower()
    if host and (not re.match(r"^[a-z0-9][a-z0-9.-]{0,252}$", host)
                 or "." not in host):
        return False, "that does not look like a tailnet name"

    # Only ports Dashy has actually discovered, so this cannot be talked into
    # publishing something arbitrary that happens to be listening.
    with _lock:
        svcs = list(_state.get("services") or [])
    want = sorted({int(s["lport"]) for s in svcs
                   if s.get("lport") and not s.get("port")})
    if PORT not in {int(s["lport"]) for s in svcs if s.get("lport")}:
        want.append(PORT)              # Dashy publishes no ports of its own
    want = sorted(set(want))
    if not want:
        return True, "everything is already published"
    try:
        os.makedirs(QUEUE, exist_ok=True)
        with open(os.path.join(QUEUE, "_serve.req"), "w") as fh:
            json.dump({"host": host, "lports": want[:60],
                       "serve": SERVE_HOST_HINT}, fh)
    except OSError as e:
        return False, str(e)
    return True, "publishing %d app%s" % (len(want), "" if len(want) == 1 else "s")


def detect():
    """What this NAS appears to be running, so first-run setup can be a
    confirmation rather than an interrogation."""
    apps = installed_apps()
    return {"tailscale": bool(TAILNET) or "tailscale" in apps,
            # Tailscale is here but nothing is published, which is the normal
            # state of a new install and the one worth offering to fix
            "serve_offer": (not TAILNET) and ("tailscale" in apps),
            "hexos": "hexos" in apps,
            "openclaw": "openclaw" in apps,
            "weather": weather_where() is not None}


def header_links(services, setup=None):
    """Resolved from what is actually published, then filtered by whatever the
    owner said they use during setup."""
    setup = setup or {}
    """The two header buttons, resolved from what is actually published.
    Nothing hard-coded, so a port change elsewhere carries through."""
    out = {"truenas": None, "chat": None, "hexos": None}
    for s in services:
        if s.get("app") == "openclaw":
            out["chat"] = s.get("url")
        elif s.get("lport") in (80, 443) and not out["truenas"]:
            # :443 is implicit in https and looks like a mistake in a link
            out["truenas"] = (s.get("url") or "").replace(":443/", "/")

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
_ACCENTS = ("teal", "blue", "violet", "amber", "yellow", "rose", "green")
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

    w = d.get("weather")
    if isinstance(w, dict):
        try:
            lat, lon = float(w["lat"]), float(w["lon"])
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                nm = str(w.get("name") or "")[:80]
                tz = str(w.get("tz") or "UTC")[:60]
                out["weather"] = {"lat": round(lat, 4), "lon": round(lon, 4),
                                  "name": nm, "tz": tz}
        except (KeyError, TypeError, ValueError):
            pass

    su = d.get("setup")
    if isinstance(su, dict):
        clean = {}
        for flag in ("done", "tailscale", "hexos", "openclaw", "weather"):
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

    out, claimed = [], set()
    for a in apps:
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
        if chosen is None:
            continue                 # host networking, or nothing web-facing
        claimed.add(chosen)
        label, desc = KNOWN.get(a.get("name"),
                                ((a.get("name") or "?").replace("-", " ").title(), ""))
        out.append({"port": pub.get(chosen), "lport": chosen,
                    "path": ppath if chosen == pport else "/",
                    "name": label, "desc": desc,
                    "state": a.get("state"), "app": a.get("name"),
                    "version": a.get("version"), "update": bool(a.get("update")),
                    "iconsrc": ("/appicon/" + a["name"]) if a.get("icon") else None})

    # Published ports that belong to no installed app: the TrueNAS UI on 443,
    # and Dashy itself, which publishes nothing because it runs host networking.
    for lport, tport in pub.items():
        if lport in claimed:
            continue
        label, desc = PORT_FALLBACK.get(lport, ("Port %d" % lport, ""))
        out.append({"port": tport, "lport": lport, "path": "/",
                    "name": label, "desc": desc,
                    "state": None, "app": None, "version": None, "update": False,
                    "iconsrc": "/icon-192.png" if lport == PORT else None})

    # published first, in tailnet-port order, then everything else by name, so
    # an existing install's tiles keep their order and the newly visible ones
    # land after them rather than shuffling the lot
    out.sort(key=lambda x: (x["port"] is None, x["port"] or 0,
                            (x["name"] or "").lower()))
    return out


def probe(port):
    """Is something answering on the host at this port? Any HTTP reply counts as up."""
    url = "http://%s:%d/" % (HOST_IP, port)
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code                      # 401/403 still means it is alive
    except (urllib.error.URLError, socket.timeout, OSError, ValueError):
        return 0


def services_loop():
    while True:
        found = []
        for svc in discover():
            svc = dict(svc)
            svc["code"] = probe(svc["lport"])
            path = svc.get("path") or "/"
            # Both, always. Which one a tile uses is the viewer's setting, and
            # sending both means flipping it is instant on every device instead
            # of waiting for the next poll to recompute a single url.
            svc["url_lan"] = "http://%s:%d%s" % (LINK_HOST, svc["lport"], path)
            svc["url_ts"] = ("https://%s:%d%s" % (TAILNET, svc["port"], path)
                             if (TAILNET and svc.get("port")) else None)
            svc["url"] = svc["url_ts"] or svc["url_lan"]
            found.append(svc)
        with _lock:
            _state["services"] = found
        weather_now()          # cached 15 min; kept off the request path
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
    out["tailnet"] = TAILNET
    out["services"] = svc
    out["updates"] = update_status()
    out["weather"] = _wx["data"]          # whatever the loop last fetched
    out["prefs"] = load_prefs()
    out["detected"] = detect()
    out["selfupdate"] = self_update()
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
<link rel="icon" href="/favicon.png" sizes="32x32" type="image/png">
<link rel="apple-touch-icon" href="/icon-180.png">
<link rel="manifest" href="/manifest.webmanifest">
<title>Dashy</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect x='2' y='2' width='28' height='28' rx='8.5' fill='%230d9488'/%3E%3Cpath d='M6.5 19h4L13 12l3.5 11 3-8 1.5 4h4.5' fill='none' stroke='%23ffffff' stroke-width='2.3' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
<style>
  :root{
    color-scheme:light dark;
    --bg:#f5f6f8; --panel:#ffffff; --brd:rgba(16,20,28,.09);
    --ink:#0f1218; --muted:#6a7280; --faint:rgba(16,20,28,.06);
    --ok:#1fa971; --warn:#e0a32e; --bad:#e0443a;
    --accent:#2f6df6; --brand:#0d9488;
    --shadow:0 1px 2px rgba(16,20,28,.04), 0 12px 30px -14px rgba(16,20,28,.20);
    --r:18px;
  }
  /* dark palette twice: once for "follow the system" (suppressed when the user
     has explicitly chosen light), once for an explicit dark choice */
  @media (prefers-color-scheme:dark){
    :root:not([data-theme="light"]){
      --bg:#0a0c10; --panel:#171b22; --brd:rgba(255,255,255,.09);
      --ink:#eef1f5; --muted:#949cab; --faint:rgba(255,255,255,.06);
      --ok:#34d399; --warn:#fbbf24; --bad:#f87171;
      --accent:#5b8cff; --brand:#14b8a6;
      --shadow:0 1px 2px rgba(0,0,0,.4), 0 18px 42px -18px rgba(0,0,0,.7);
      color-scheme:dark;
    }
  }
  :root[data-theme="dark"]{
      --bg:#0a0c10; --panel:#171b22; --brd:rgba(255,255,255,.09);
      --ink:#eef1f5; --muted:#949cab; --faint:rgba(255,255,255,.06);
      --ok:#34d399; --warn:#fbbf24; --bad:#f87171;
      --accent:#5b8cff; --brand:#14b8a6;
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
  .wx{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:999px;
      background:var(--faint);border:1px solid var(--brd);font-size:12.5px;font-weight:600;
      color:var(--ink);font-family:inherit;cursor:pointer;
      transition:border-color .15s ease,transform .15s ease}
  .wx:hover{border-color:var(--muted);transform:translateY(-1px)}
  .wx svg{width:18px;height:18px;flex:0 0 auto;display:block}
  .wx .t{font-size:14px;letter-spacing:-.01em}
  .wx .c{color:var(--muted);font-weight:500}
  /* the condition words are the first thing to go when it gets tight */
  @media (max-width:480px){.wx .c{display:none}}
  .ibtn{width:34px;height:34px;flex:0 0 auto;display:inline-flex;align-items:center;
        justify-content:center;border-radius:10px;background:var(--faint);
        border:1px solid var(--brd);color:var(--muted);text-decoration:none;
        transition:color .15s ease,border-color .15s ease,transform .15s ease}
  .ibtn:hover{color:var(--ink);border-color:var(--muted);transform:translateY(-1px)}
  .ibtn svg{width:17px;height:17px;display:block}


  /* ---- settings dropdown ---------------------------------------------- */
  .menuwrap{position:relative}
  .menu{position:absolute;top:calc(100% + 9px);right:0;z-index:60;
        width:min(296px,calc(100vw - 28px));background:var(--panel);
        backdrop-filter:blur(18px) saturate(1.5);-webkit-backdrop-filter:blur(18px) saturate(1.5);
        border:1px solid var(--brd);border-radius:16px;box-shadow:var(--shadow);
        padding:7px;transform-origin:top right;overflow-y:auto;
        max-height:min(78vh,620px);overscroll-behavior:contain}
  .menu[hidden]{display:none}
  .menu.in{animation:menuin .19s cubic-bezier(.22,1,.36,1) both}
  @keyframes menuin{from{opacity:0;transform:translateY(-6px) scale(.97)}
                    to{opacity:1;transform:none}}
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
  /* Closing the pane and opening whatever is underneath used to be the same
     tap. This catches it, so a tap outside only ever closes. */
  .mscrim{position:fixed;inset:0;z-index:50;background:transparent}
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
  .mscrim[hidden]{display:none}
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

  .sw{display:grid;grid-template-columns:repeat(7,24px);gap:8px;
      justify-content:space-between;padding:4px 10px 10px}
  .sw button{width:24px;height:24px;border-radius:50%;border:2px solid transparent;
             cursor:pointer;padding:0;transition:transform .14s ease}
  .sw button:hover{transform:scale(1.12)}
  .sw button[aria-pressed="true"]{border-color:var(--ink);
                                  box-shadow:0 0 0 2px var(--panel) inset}

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
             border-radius:var(--r);z-index:3}
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
  .editbar .done{margin-left:auto;padding:7px 14px;border-radius:9px;border:none;
                 background:var(--brand);color:#fff;font:inherit;font-weight:700;cursor:pointer}
  .editbar .rst{padding:7px 12px;border-radius:9px;border:1px solid var(--brd);
                background:none;color:var(--muted);font:inherit;font-weight:600;cursor:pointer}


  /* ---- press feedback -------------------------------------------------
     One rule for every touch target. transform only, so it composites and
     cannot trigger layout; .09s in, a slower ease back out.              */
  .card,.apptile,.ibtn,.wx,.mi,.x,.seg button,.sw button,
  .cardtools button,.editbar button,.ubtn{
    transition:transform .16s cubic-bezier(.22,1,.36,1),
               background .13s ease, border-color .15s ease, color .15s ease, opacity .15s ease}
  .card:active,.apptile:active,.mi:active,.x:active,.seg button:active,
  .cardtools button:active,.editbar button:active,.ubtn:active{
    transform:scale(.967);transition-duration:.09s}
  .ibtn:active,.wx:active{transform:scale(.94);transition-duration:.09s}
  .sw button:active{transform:scale(.88);transition-duration:.09s}
  .apptile .upd:active{transform:scale(.88);transition-duration:.09s}
  @media (prefers-reduced-motion:reduce){
    .card:active,.apptile:active,.mi:active,.x:active,.seg button:active,
    .cardtools button:active,.editbar button:active,.ubtn:active,
    .ibtn:active,.wx:active,.sw button:active{transform:none}
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
  .card .sparkwrap{margin-top:auto;padding-top:10px;padding-bottom:1px}
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
  .sw2 i{position:absolute;top:2px;left:2px;width:20px;height:20px;border-radius:50%;
         background:var(--panel);box-shadow:0 1px 3px rgba(0,0,0,.25);
         transition:transform .18s cubic-bezier(.22,1,.36,1)}
  .sw2[aria-checked="true"]{background:var(--brand);border-color:var(--brand)}
  .sw2[aria-checked="true"] i{transform:translateX(18px);background:#fff}
  .cityres{margin-top:6px;display:flex;flex-direction:column;gap:4px}
  .cityres button{text-align:left;padding:9px 11px;border-radius:9px;border:1px solid var(--brd);
                  background:var(--faint);color:var(--ink);font:inherit;font-size:13px;
                  cursor:pointer}
  .cityres button:hover{border-color:var(--muted)}
  .cityres .none{padding:8px 2px;font-size:12px;color:var(--muted)}
  .wxpick{margin-top:7px;font-size:12.5px;font-weight:650;color:var(--brand);
          display:flex;align-items:center;gap:6px}
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

  .card{position:relative;background:var(--panel);border:1px solid var(--brd);
        border-radius:var(--r);padding:11px 12px;
        min-height:84px;display:flex;flex-direction:column;align-items:stretch}
  button.card{width:100%;text-align:left;font:inherit;color:inherit;
              cursor:pointer;appearance:none;
              transition:transform .15s ease, border-color .15s ease}
  .card .meterw{margin-top:auto}
  /* The alarm is the card, not the digit. A red number inside an identical
     white box does not read as "this one needs you". */
  .cardwrap[data-state="warn"] .card{border-color:color-mix(in srgb,var(--warn) 45%,var(--brd));
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
  .big{font-size:19px;font-weight:620;letter-spacing:-.015em;margin:3px 0 0;
       font-variant-numeric:tabular-nums;line-height:1.2}
  .big small{font-size:12px;font-weight:500;color:var(--muted);margin-left:2px;letter-spacing:0}
  .sub{margin:1px 0 0;font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums;text-align:left}

  /* margin-top:auto pins the bar to the bottom but guarantees nothing above
     it, so the wrapper keeps the pin and sets a floor on the gap */
  .meterw{margin-top:auto;padding-top:11px;padding-bottom:2px}
  .meter{height:3px;border-radius:2px;background:var(--faint);overflow:hidden;flex:0 0 auto}
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
    .sheet,.scrim,.scrim.out,.scrim.out .sheet,.sheet-b > *,.menu.in{animation:none}
  }
  .sheet-h{display:flex;align-items:center;justify-content:space-between;gap:10px;
           padding:15px 16px 13px;border-bottom:1px solid var(--brd);flex:0 0 auto}
  .sheet-h p{margin:0;font-weight:700;font-size:15px;flex:0 0 auto}
  .sh-sub{font-size:12.5px;font-weight:600;color:var(--muted);margin-right:auto;
          overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .x{appearance:none;border:none;background:var(--faint);color:var(--muted);cursor:pointer;
     width:28px;height:28px;border-radius:50%;font-size:19px;line-height:1}
  .sheet-b{overflow-y:auto;padding:6px 0 8px;-webkit-overflow-scrolling:touch;
           overscroll-behavior:contain}

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
  .ubtn{flex:1;appearance:none;border:1px solid var(--brd);border-radius:12px;padding:12px 0;
        font:inherit;font-weight:650;font-size:14px;cursor:pointer}
  .ubtn.no{background:var(--faint);color:var(--ink)}
  .ubtn.yes{background:var(--warn);color:#2a1c00;border-color:transparent}
  .ubtn:disabled{opacity:.6;cursor:default}

  .appgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(134px,1fr));gap:9px}
  .sheet .appgrid{padding:12px 16px 14px}
  .apptile{position:relative;text-decoration:none;color:inherit;display:flex;
           flex-direction:column;gap:6px;background:var(--panel);
           border:1px solid var(--brd);border-radius:var(--r);
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
      <svg class="mark" viewBox="0 0 32 32" role="img" aria-label="Dashy">
        <rect width="32" height="32" rx="8.5" fill="var(--brand)"/>
        <path d="M6.5 19h4L13 12l3.5 11 3-8 1.5 4h4.5" fill="none" stroke="#fff"
              stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <div style="min-width:0"><h1>Dashy<span class="host" id="host"></span></h1>
        <p class="beta">beta</p></div>
    </div>
    <div class="hdr-r">
      <button class="wx" id="wx" hidden type="button" data-pop="wx"
              aria-label="Weather, hourly forecast"></button>
      <a class="ibtn" id="chat" hidden target="_blank" rel="noopener"
         title="Chat" aria-label="Open chat"><svg viewBox="0 0 24 24" fill="none"
         stroke="currentColor" stroke-width="1.9" stroke-linecap="round"
         stroke-linejoin="round"><path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 9.6 9.6 0 0 1-2.9-.4L3 21l1.6-4.6A8.3 8.3 0 0 1 3.6 11.5a8.4 8.4 0 0 1 9-8.4 8.4 8.4 0 0 1 8.4 8.4z"/></svg></a>

      <div class="menuwrap">
        <button class="ibtn" id="settings" type="button" aria-haspopup="true"
                aria-expanded="false" title="Settings" aria-label="Settings"><svg viewBox="0 0 24 24" fill="none"
         stroke="currentColor" stroke-width="1.9" stroke-linecap="round"
         stroke-linejoin="round"><circle cx="12" cy="12" r="3.1"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></button>
        <div class="mscrim" id="mscrim" hidden></div>
        <div class="menu" id="menu" hidden role="menu" aria-labelledby="settings">
          <p class="mh">Updates</p>
          <button class="mi" id="mi-check" type="button" role="menuitem">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"
                 stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 4v5h-5"/></svg>
            <span id="mi-checktxt">Check for updates</span>
            <span class="rt" id="mi-checkmsg"></span></button>
          <button class="mi" id="mi-self" type="button" role="menuitem">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"
                 stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>
            <span class="dots" id="mi-selfdots" hidden><i></i><i></i><i></i></span>
            <span id="mi-selftxt">Update Dashy</span><span class="rt" id="mi-selfver"></span></button>
          <div class="msep"></div>
          <p class="mh">Appearance</p>
          <div class="seg" id="seg-theme" role="group" aria-label="Theme">
            <button type="button" data-theme="system" aria-pressed="true">System</button>
            <button type="button" data-theme="light"  aria-pressed="false">Light</button>
            <button type="button" data-theme="dark"   aria-pressed="false">Dark</button>
          </div>
          <p class="mh">Icon colour</p>
          <div class="sw" id="sw-accent" role="group" aria-label="Icon colour"></div>
          <div class="msep"></div>
          <p class="mh">Layout</p>
          <button class="mi" id="mi-edit" type="button" role="menuitem">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"
                 stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.6"/><rect x="14" y="3" width="7" height="7" rx="1.6"/><rect x="3" y="14" width="7" height="7" rx="1.6"/><rect x="14" y="14" width="7" height="7" rx="1.6"/></svg>
            Resize or remove cards</button>
          <div class="msep"></div>
          <p class="mh">Integrations</p>
          <div class="mrow"><span class="ml">Tailscale</span>
            <button class="sw2 sm" id="m-ts" role="switch" aria-checked="false"><i></i></button></div>
          <div class="mrow"><span class="ml">OpenClaw chat</span>
            <button class="sw2 sm" id="m-oc" role="switch" aria-checked="false"><i></i></button></div>
          <div class="mrow"><span class="ml">HexOS</span>
            <button class="sw2 sm" id="m-hx" role="switch" aria-checked="false"><i></i></button></div>
          <div class="mrow"><span class="ml">Weather</span>
            <button class="sw2 sm" id="m-wx" role="switch" aria-checked="false"><i></i></button></div>
          <div class="mrow nas" id="m-wxrow"><span class="ml">Weather location</span>
            <span class="mhint" id="m-wxnow">not set</span>
            <input class="fld sm" id="m-city" type="search" autocomplete="off"
                   placeholder="Type a town&hellip;">
            <div class="cityres" id="m-cityres"></div></div>
          <div class="mrow nas" id="m-serverow" hidden>
            <span class="ml">App links</span>
            <span class="mhint">Which address a tile opens</span>
            <div class="seg sm" id="m-links" role="group" aria-label="App links">
              <button type="button" data-links="ts" aria-pressed="true">Tailnet</button>
              <button type="button" data-links="lan" aria-pressed="false">Local</button>
            </div>
            <span class="pubmsg" id="m-tsmsg"></span></div>
          <div class="mrow nas"><span class="ml">TrueNAS address</span>
            <span class="mhint">Where you open the TrueNAS interface</span>
            <input class="fld sm" id="m-nas" type="url" inputmode="url"
                   placeholder="https://nas.example.ts.net/"></div>
          <button class="mi" id="mi-setup" type="button" role="menuitem">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"
                 stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l3-8 4 16 3-8h4"/></svg>
            Show the welcome screen</button>
          <div class="msep"></div>
          <p class="mh">Links</p>
          <a class="mi" id="mi-truenas" target="_blank" rel="noopener" role="menuitem">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"
                 stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></svg>
            TrueNAS</a>
          <a class="mi" id="mi-hexos" target="_blank" rel="noopener" role="menuitem">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"
                 stroke-linecap="round" stroke-linejoin="round"><path d="M12 2.6 20.4 7v10L12 21.4 3.6 17V7z"/><path d="M12 11.8 20.4 7M12 11.8V21.4M12 11.8 3.6 7"/></svg>
            HexOS</a>
          <a class="mi" id="mi-chat" target="_blank" rel="noopener" role="menuitem">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"
                 stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 9.6 9.6 0 0 1-2.9-.4L3 21l1.6-4.6A8.3 8.3 0 0 1 3.6 11.5a8.4 8.4 0 0 1 9-8.4 8.4 8.4 0 0 1 8.4 8.4z"/></svg>
            Chat</a>
          <div class="msep"></div>
          <button class="mi" id="mi-ver" type="button" role="menuitem"
                  style="color:var(--muted)"></button>
        </div>
      </div>
    </div>
  </header>

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
    <svg class="mark" viewBox="0 0 32 32" role="img" aria-label="Dashy">
      <rect width="32" height="32" rx="8.5" fill="var(--brand)"/>
      <path d="M6.5 19h4L13 12l3.5 11 3-8 1.5 4h4.5" fill="none" stroke="#fff"
            stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"/>
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
        <span class="txt"><b>OpenClaw<span class="auto" id="a-oc"></span></b>
          <span>Shows a chat button.</span></span>
        <button class="sw2" id="o-oc" role="switch" aria-checked="false"><i></i></button>
      </div>
      <div class="opt">
        <span class="txt"><b>HexOS<span class="auto" id="a-hx"></span></b>
          <span>Adds a link to your HexOS deck.</span></span>
        <button class="sw2" id="o-hx" role="switch" aria-checked="false"><i></i></button>
      </div>
      <div class="opt">
        <span class="txt"><b>Weather<span class="auto" id="a-wx"></span></b>
          <span>Conditions and an hourly forecast.</span></span>
        <button class="sw2" id="o-wx" role="switch" aria-checked="false"><i></i></button>
      </div>
      <div class="opt wxloc" id="wxloc" style="display:block">
        <span class="txt"><b>Which town?</b>
          <span>Type a town or city and pick it from the list. Town level only
          &mdash; never an address. Your NAS looks up the forecast every 15
          minutes, so <b>open-meteo.com</b> sees your server&rsquo;s IP and the
          town you chose. Your browser never contacts them, and there is no
          account or key.</span></span>
        <input class="fld" id="o-city" type="search" autocomplete="off"
               placeholder="Start typing a town&hellip;">
        <div class="cityres" id="o-cityres"></div>
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
    <div class="sheet-h"><p id="sh-t"></p><span class="sh-sub" id="sh-s"></span>
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
/* Cards only carry a bar where a proportion exists, so every one of these is
   allowed to be missing. */
function setBar(id, pct, lvl){
  const el=$(id); if(!el) return;
  el.style.width=Math.max(0,Math.min(100,pct))+'%';
  if(lvl) el.dataset.level=lvl;
}
function setMeter(el,pct){ if(!el) return;
  el.style.width=Math.max(0,Math.min(100,pct))+'%'; el.dataset.level=level(pct); }
// for bars where a FULL bar is the healthy state, like apps up or availability
function setMeterUp(el,pct){ if(!el) return;
  el.style.width=Math.max(0,Math.min(100,pct))+'%';
  el.dataset.level = pct>=99.5 ? 'good' : pct>=90 ? 'warn' : 'bad'; }
function when(ts){ return new Date(ts*1000).toLocaleDateString([], {day:'numeric',month:'short',year:'2-digit'}); }


// Every card declared once. Order and visibility come from saved prefs, so
// rearranging is a re-render rather than a special case per card.
const CARDS=[
  {k:'health',label:'Health', sec:'system', kind:'health'},
  {k:'cpu',   label:'Processor', sec:'system', trend:1},
  {k:'mem',   label:'Memory',    sec:'system', bar:1},
  {k:'ctemp', label:'CPU temp',  sec:'system', trend:1},
  {k:'net',   label:'Network',   sec:'system', trend:1},
  {k:'dtemp', label:'Drive temps', sec:'storage'}
];
const SECTIONS=[['system','System'],['storage','Storage'],['apps','Apps']];
const CARDK=CARDS.map(c=>c.k);

/* ---------- preferences ------------------------------------------------
   Per device, in localStorage. Dashy has no login, so there is nowhere else
   sensible to put them, and every read is guarded: private windows and
   blocked site data both throw rather than return null.                   */
const PKEY='dashy.prefs.v2';
const ACCENTS={
  teal:  {light:'#0d9488', dark:'#14b8a6'},
  blue:  {light:'#2f6df6', dark:'#5b8cff'},
  violet:{light:'#6d4aff', dark:'#8b6cff'},
  amber: {light:'#d9820a', dark:'#f0a92c'},
  yellow:{light:'#dcae00', dark:'#fcd34d'},
  rose:  {light:'#e0443a', dark:'#f87171'},
  green: {light:'#1fa971', dark:'#34d399'}
};
const DEFAULTS={theme:'system', accent:'teal', order:CARDK.slice(), hidden:[], wide:[], size:{},
                setup:{}, weather:null};
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
      if(ACCENTS[d.accent]) prefs.accent=d.accent;
      if(Array.isArray(d.hidden)) prefs.hidden=d.hidden.filter(k=>CARDK.indexOf(k)>=0);
      if(Array.isArray(d.wide)) prefs.wide=d.wide.filter(k=>CARDK.indexOf(k)>=0);
      if(d.size && typeof d.size==='object') prefs.size=cleanSize(d.size);
      migrateSizes();
      if(d.setup && typeof d.setup==='object') prefs.setup=d.setup;
      if(d.weather && typeof d.weather==='object') prefs.weather=d.weather;
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
                             setup:prefs.setup, weather:prefs.weather})});
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
  if(P.weather && typeof P.weather==='object') prefs.weather=P.weather;
  if(P.theme && P.theme!==prefs.theme){ prefs.theme=P.theme; applyTheme(); }
  if(P.accent && ACCENTS[P.accent] && P.accent!==prefs.accent){
    prefs.accent=P.accent; applyAccent();
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
function applyAccent(){
  // an inline custom property beats both palette blocks, so one value wins
  // in either theme; the light/dark pair keeps it legible in both
  const a=ACCENTS[prefs.accent]||ACCENTS.teal;
  document.documentElement.style.setProperty('--brand', isDark()?a.dark:a.light);
  document.querySelectorAll('#sw-accent button').forEach(b=>
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
  $('sw-accent').innerHTML=Object.keys(ACCENTS).map(k=>
    '<button type="button" data-accent="'+k+'" aria-label="'+k+'" aria-pressed="false" '+
    'style="background:'+ACCENTS[k].light+'"></button>').join('');
}



/* ---------- choosing a town ---------------------------------------------
   Search runs through our own server, so the browser never contacts the
   geocoder directly and nobody is asked for a precise location.           */
let citySearch=null;

function wxLocLabel(){
  const w=prefs.weather;
  return (w && w.name) ? w.name : '';
}

function renderCityPick(){
  const el=$('o-cityres');
  const name=wxLocLabel();
  if(name && !el.dataset.searching){
    el.innerHTML='<p class="wxpick">✓ '+esc(name)+'</p>';
  }
}

function citySearchNow(q, which){
  const el=$(which==='m' ? 'm-cityres' : 'o-cityres');
  if(!q || q.trim().length<2){ el.dataset.searching=''; renderCityPick(); return; }
  el.dataset.searching='1';
  clearTimeout(citySearch);
  citySearch=setTimeout(async()=>{
    try{
      const r=await fetch('/api/geocode?q='+encodeURIComponent(q.trim()));
      const j=await r.json();
      const rs=(j&&j.results)||[];
      if(!rs.length){ el.innerHTML='<p class="none">Nothing found.</p>'; return; }
      el.innerHTML=rs.map((g,i)=>
        '<button type="button" data-city="'+i+'">'+esc(g.name)+'</button>').join('');
      el._results=rs;
    }catch(e){ el.innerHTML='<p class="none">Could not search just now.</p>'; }
  }, 280);                       // one request per pause, not per keystroke
}

function pickCity(i, fromId){
  const el=$(fromId || 'o-cityres');
  const g=(el._results||[])[i];
  if(!g) return;
  prefs.weather={name:g.name, lat:g.lat, lon:g.lon, tz:g.tz};
  prefs.setup=Object.assign({}, prefs.setup, {weather:true});
  setSwitch('o-wx', true); setSwitch('m-wx', true);
  el.dataset.searching='';
  const fld=$(fromId==='m-cityres' ? 'm-city' : 'o-city');
  if(fld) fld.value='';
  el.innerHTML='';
  renderCityPick();
  const wn=$('m-wxnow'); if(wn) wn.textContent=g.name;
  savePrefs();
  if(data) paint(data);
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
    setSwitch('o-oc', pick('openclaw'));
    setSwitch('o-hx', pick('hexos'));
    setSwitch('o-wx', pick('weather'));
    $('a-ts').textContent=det.tailscale?'found':'';
    $('a-oc').textContent=det.openclaw?'found':'';
    $('a-hx').textContent=det.hexos?'found':'';
    $('a-wx').textContent=det.weather?'set':'';
    $('o-city').value='';
    $('o-cityres').dataset.searching='';
    renderCityPick();
    $('wxloc').style.display = getSwitch('o-wx') ? 'block' : 'none';
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
  prefs.setup={
    done:true,
    tailscale:getSwitch('o-ts'),
    openclaw:getSwitch('o-oc'),
    hexos:getSwitch('o-hx'),
    weather:getSwitch('o-wx'),
    truenas:/^https?:\\/\\//.test(nas) ? nas : ''
  };
  if(skipped){
    const det=(data&&data.detected)||{};
    prefs.setup.tailscale=!!det.tailscale;
    prefs.setup.openclaw=!!det.openclaw;
    prefs.setup.hexos=!!det.hexos;
    prefs.setup.weather=!!det.weather;
    prefs.setup.truenas='';
  }
  savePrefs();
  $('setup').hidden=true;
  if(data) paint(data);
}

/* ---------- settings menu --------------------------------------------- */
function menuOpen(){ return !$('menu').hidden; }
function openMenu(){
  const m=$('menu');
  m.hidden=false; m.classList.add('in');
  const sc=$('mscrim'); if(sc) sc.hidden=false;
  $('settings').setAttribute('aria-expanded','true');
}
function closeMenu(){
  const m=$('menu');
  m.hidden=true; m.classList.remove('in');
  const sc=$('mscrim'); if(sc) sc.hidden=true;
  $('settings').setAttribute('aria-expanded','false');
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
             '<button class="card hcard" data-pop="up">'+
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
  const body = head + (c.bar
    ? '<div class="meterw"><div class="meter"><i id="m-'+c.k+'"></i></div></div>' : '');
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
    k:'pool-'+p.name, label:p.name, sec:'storage', bar:1, pop:'store', pool:p.name}));
}
function layoutSig(){
  const pools=((data&&data.pools)||[]).map(p=>p.name).join(',');
  const apps=((data&&data.services)||[]).map(s=>s.name).join(',');
  return pools+'|'+apps;
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
  if(!built) build();
  else if(relayout || layoutSig()!==builtSig){ build(); }
  data=d;

  if(d.cpu!==null && d.cpu!==undefined){
    $('v-cpu').innerHTML=d.cpu.toFixed(0)+'<small>%</small>';
    setMeter($('m-cpu'), d.cpu);
  }
  $('s-cpu').textContent=d.cores+' threads'+(d.power?' \\u00b7 '+d.power+' W':'');

  if(d.mem){
    const p=(d.mem.apps_pct!==undefined)?d.mem.apps_pct:d.mem.pct;
    $('v-mem').innerHTML=p.toFixed(0)+'<small>%</small>';
    setMeter($('m-mem'), p);
    $('s-mem').textContent=ram(d.mem.apps!==undefined?d.mem.apps:d.mem.used)+' of '+ram(d.mem.total)
      +(d.mem.arc?' \\u00b7 '+ram(d.mem.arc)+' cache':'');
  }

  if(d.cpu_temp!==null && d.cpu_temp!==undefined){
    $('v-ctemp').innerHTML=d.cpu_temp.toFixed(0)+'<small>\\u00b0C</small>';
    $('v-ctemp').style.color=d.cpu_temp>=85?'var(--bad)':d.cpu_temp>=75?'var(--warn)':'';
    const w=Math.max(4,Math.min(100,(d.cpu_temp-25)/70*100));
    setBar('m-ctemp', w, d.cpu_temp>=85?'bad':d.cpu_temp>=75?'warn':'ok');
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
    const w=Math.max(4,Math.min(100,(hi-20)/45*100));
    setBar('m-dtemp', w, worst);
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
    setBar('m-'+k, p.pct, p.pct>=90?'bad':p.pct>=85?'warn':'ok');
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
  paintWeather(d.weather);
  paintLinks(d.links||{});
  // uptime lives in the settings pane now; the home screen stays clean
  const mv=$('mi-ver');
  mv.innerHTML='<span>'+(d.app||'Dashy')+' v'+d.version+'</span>'+
    '<span class="rt"><i class="pdot '+(footIssue?'warn':'ok')+'"></i>up '+
    footUp[0]+' '+footUp[1]+'</span>';
  mv.title = footIssue && footNote ? footNote : '';
  lastTick=Date.now();
}

// Weather glyphs, drawn rather than emoji. CSS vars work in SVG attributes,
// same trick the logo already uses, so they follow the light/dark palette.
const WX_ICON={
 clear:'<circle cx="12" cy="12" r="4.1" fill="var(--warn)"/><g stroke="var(--warn)" stroke-width="1.8" stroke-linecap="round"><path d="M12 2.7v2.1M12 19.2v2.1M4.4 12H2.3M21.7 12h-2.1M6.4 6.4 4.9 4.9M19.1 19.1l-1.5-1.5M17.6 6.4l1.5-1.5M4.9 19.1l1.5-1.5"/></g>',
 night:'<path d="M20.1 14.3A8.1 8.1 0 0 1 9.7 3.9 8.3 8.3 0 1 0 20.1 14.3z" fill="var(--muted)"/>',
 partly:'<circle cx="8.8" cy="8.4" r="3.1" fill="var(--warn)"/><path d="M7.4 18.8h9.3a3.3 3.3 0 0 0 .2-6.6 4.6 4.6 0 0 0-8.8-1.2 3.4 3.4 0 0 0-.7 7.8z" fill="var(--muted)"/>',
 partlynight:'<path d="M12.8 7.6A4.4 4.4 0 0 1 8.5 3.3 4.5 4.5 0 1 0 12.8 7.6z" fill="var(--muted)"/><path d="M7.4 18.8h9.3a3.3 3.3 0 0 0 .2-6.6 4.6 4.6 0 0 0-8.8-1.2 3.4 3.4 0 0 0-.7 7.8z" fill="var(--muted)"/>',
 cloud:'<path d="M6.9 18.6h10a3.5 3.5 0 0 0 .2-7 4.9 4.9 0 0 0-9.4-1.3 3.6 3.6 0 0 0-.8 8.3z" fill="var(--muted)"/>',
 fog:'<path d="M6.9 14.4h10a3.5 3.5 0 0 0 .2-7 4.9 4.9 0 0 0-9.4-1.3 3.6 3.6 0 0 0-.8 8.3z" fill="var(--muted)"/><g stroke="var(--muted)" stroke-width="1.8" stroke-linecap="round"><path d="M4.6 17.9h14.8M7.1 21h9.8"/></g>',
 drizzle:'<path d="M6.9 14.9h10a3.5 3.5 0 0 0 .2-7 4.9 4.9 0 0 0-9.4-1.3 3.6 3.6 0 0 0-.8 8.3z" fill="var(--muted)"/><g stroke="var(--accent)" stroke-width="1.8" stroke-linecap="round"><path d="M9.4 18.2v1.5M14.4 18.2v1.5"/></g>',
 rain:'<path d="M6.9 14.9h10a3.5 3.5 0 0 0 .2-7 4.9 4.9 0 0 0-9.4-1.3 3.6 3.6 0 0 0-.8 8.3z" fill="var(--muted)"/><g stroke="var(--accent)" stroke-width="1.8" stroke-linecap="round"><path d="M8.8 17.9 7.9 21M12.4 17.9l-.9 3.1M16 17.9l-.9 3.1"/></g>',
 snow:'<path d="M6.9 14.9h10a3.5 3.5 0 0 0 .2-7 4.9 4.9 0 0 0-9.4-1.3 3.6 3.6 0 0 0-.8 8.3z" fill="var(--muted)"/><g stroke="var(--accent)" stroke-width="1.7" stroke-linecap="round"><path d="M9 18.3v2.4M7.8 19.5h2.4M15 18.3v2.4M13.8 19.5h2.4"/></g>',
 storm:'<path d="M6.9 14.4h10a3.5 3.5 0 0 0 .2-7 4.9 4.9 0 0 0-9.4-1.3 3.6 3.6 0 0 0-.8 8.3z" fill="var(--muted)"/><path d="M13.3 16.2 9.6 21.3h2.6l-.7 2.1 3.7-5.2h-2.6z" fill="var(--warn)"/>'
};

// Forecast needs a different chart to the telemetry one: temperatures sit in a
// narrow band and can go negative, so the sqrt scale in chart() would be wrong.
function wxChart(hs){
  if(!hs||hs.length<3) return '';
  const W=430,H=112,n=hs.length;
  const temps=hs.map(h=>h.temp);
  let lo=Math.min.apply(null,temps), hi=Math.max.apply(null,temps);
  if(hi-lo<4){const mid=(hi+lo)/2; lo=mid-2; hi=mid+2;}   // a flat day should not look flat-lined
  const pad=(hi-lo)*0.18; lo-=pad; hi+=pad;
  const x=i=>(i/(n-1))*W;
  const y=v=>H-((v-lo)/(hi-lo))*(H-12)-6;
  const line=hs.map((h,i)=>(i?'L':'M')+x(i).toFixed(1)+' '+y(h.temp).toFixed(1)).join(' ');
  const area='M0 '+H+' '+hs.map((h,i)=>'L'+x(i).toFixed(1)+' '+y(h.temp).toFixed(1)).join(' ')+
             ' L'+W+' '+H+' Z';
  // rain chance sits behind as bars, so the two never fight for the same axis
  const bw=W/n;
  const bars=hs.map((h,i)=>h.pop>4?'<rect x="'+(x(i)-bw*0.34).toFixed(1)+'" y="'+
      (H-(h.pop/100)*(H-12)).toFixed(1)+'" width="'+(bw*0.68).toFixed(1)+'" height="'+
      ((h.pop/100)*(H-12)).toFixed(1)+'" fill="var(--accent)" opacity=".16" rx="1.5"/>':'').join('');
  const mark=(i,v)=>'<circle cx="'+x(i).toFixed(1)+'" cy="'+y(v).toFixed(1)+
      '" r="3.2" fill="var(--warn)"/>';
  const iHi=temps.indexOf(Math.max.apply(null,temps));
  const iLo=temps.indexOf(Math.min.apply(null,temps));
  const at=i=>hs[i]?hs[i].t:'';
  const mid=Math.floor(n/2);
  return '<div class="chart"><svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none">'+
      bars+
      '<path d="'+area+'" fill="var(--warn)" opacity=".10"/>'+
      '<path d="'+line+'" fill="none" stroke="var(--warn)" stroke-width="2.8" '+
        'stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>'+
      mark(iHi,temps[iHi])+mark(iLo,temps[iLo])+
    '</svg><div class="cap"><span>now</span><span>'+at(mid)+'</span><span>'+at(n-1)+'</span></div></div>'+
    '<div class="legend">'+
      '<span><u style="background:var(--warn)"></u>temperature</span>'+
      '<span><u style="background:var(--accent)"></u>chance of rain</span>'+
    '</div>';
}

function rowsWx(){
  const w=data.weather;
  if(!w) return '<div class="row"><span class="t2">No forecast available.</span></div>';
  const hs=w.hours||[];
  const wet=hs.filter(h=>h.pop>=40);
  const r=[];
  r.push('<div class="row"><span class="l"><span class="t1">Now</span>'+
    '<span class="t2">'+(w.words||'')+(w.feels!==null&&w.feels!==undefined&&w.feels!==w.temp?
      ' · feels '+w.feels+'°':'')+'</span></span>'+
    '<span class="val">'+w.temp+'°</span></div>');
  if(w.hi!==null&&w.hi!==undefined&&w.lo!==null&&w.lo!==undefined)
    r.push('<div class="row"><span class="l"><span class="t1">Today</span>'+
      '<span class="t2">high and low</span></span>'+
      '<span class="val">'+w.hi+'° / '+w.lo+'°</span></div>');
  if(hs.length){
    const next=hs.slice(1);
    const peak=next.reduce((a,b)=>b.pop>a.pop?b:a,next[0]||{pop:0,t:''});
    r.push('<div class="row"><span class="l"><span class="t1">Rain</span>'+
      '<span class="t2">'+(wet.length? wet.length+' of the next '+hs.length+' hours above 40%'
                                     : 'nothing above 40% in the next '+hs.length+' hours')+
      '</span></span><span class="val">'+(peak.pop||0)+'%</span></div>');
  }
  return r.join('');
}

function paintWeather(w){
  const el=$('wx');
  if((prefs.setup||{}).weather===false){ el.hidden=true; return; }
  if(!w||w.temp===null||w.temp===undefined){el.hidden=true;return;}
  el.hidden=false;
  el.innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true">'+
    (WX_ICON[w.icon]||WX_ICON.cloud)+'</svg>'+
    '<span class="t">'+w.temp+'\u00b0</span>'+
    (w.words?'<span class="c">'+w.words+'</span>':'');
  const bits=[];
  if(w.feels!==null&&w.feels!==undefined&&w.feels!==w.temp) bits.push('feels '+w.feels+'\u00b0');
  if(w.hi!==null&&w.hi!==undefined&&w.lo!==null&&w.lo!==undefined)
    bits.push(w.lo+'\u00b0 to '+w.hi+'\u00b0 today');
  el.title=bits.join(' \u00b7 ');
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
  if(st && st.state==='failed' && age < 3600)
    return set('idle','Install update', st.msg||'that did not work');
  if(u && u.available) return set('ready','Update to '+u.latest, u.current+' \u2192 '+u.latest);
  if(st && st.state==='done' && age < 3600) return set('idle','Install update','just updated');
  set('idle','Install update', u ? 'up to date' : 'not checked yet');
}

// Which address a tile opens. The server sends both, so this is a repaint and
// not a round trip - flip it on a phone and the laptop follows within a poll.
function appURL(s){
  const want=(prefs.setup||{}).links;
  if(want==='lan') return s.url_lan || s.url;
  return s.url_ts || s.url_lan || s.url;
}

function setLinks(mode){
  prefs.setup=Object.assign({}, prefs.setup, {links:mode});
  savePrefs();
  paintServeOffer();
  if(openKey==='apps') renderPop(false);
  build();
  // choosing the tailnet is also a request to make those addresses exist
  if(mode==='ts') publishAll();
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
  ['su-serve','m-serverow'].forEach(id=>{ const el=$(id); if(el) el.hidden=!on; });
  if(!on) return;
  const mode=((prefs.setup||{}).links==='lan') ? 'lan' : 'ts';
  ['su-links','m-links'].forEach(id=>{
    const g=$(id); if(!g) return;
    g.querySelectorAll('button').forEach(b=>
      b.setAttribute('aria-pressed', String(b.dataset.links===mode)));
  });
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
  const chat  = su.openclaw===false ? null : L.chat;
  const hexos = su.hexos ? L.hexos : null;
  set('chat',chat);
  set('mi-truenas',L.truenas);
  set('mi-chat',chat);
  set('mi-hexos',hexos);
  paintSelfUpdate();
  paintServeOffer();
  setSwitch('m-ts', !!su.tailscale);
  setSwitch('m-oc', !!su.openclaw);
  setSwitch('m-hx', !!su.hexos);
  setSwitch('m-wx', su.weather!==false);
  const wn=$('m-wxnow');
  if(wn) wn.textContent = wxLocLabel() || 'not set';
  const wr=$('m-wxrow');
  if(wr) wr.style.display = (su.weather===false) ? 'none' : 'block';
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

function rowsPools(){
  return (data.pools||[]).map(p=>{
    const ok=(p.health||'').toUpperCase()==='ONLINE', lv=level(p.pct);
    const col = lv==='ok' ? 'var(--accent)' : lv==='warn' ? 'var(--warn)' : 'var(--bad)';
    return '<div class="row"><span class="l"><span class="t1">'+p.name+'</span>'+
      '<span class="t2">'+disk(p.used)+' used \\u00b7 '+disk(p.free)+' free of '+disk(p.size)+'</span></span>'+
      '<span class="smart '+(ok?'ok':'bad')+'">'+(p.health||'?')+'</span>'+
      '<span class="val" data-level="'+lv+'">'+p.pct.toFixed(0)+'%</span></div>'+
      '<div class="stackbar" style="height:10px;margin:0 16px 12px">'+
        '<i style="width:'+p.pct+'%;background:'+col+'"></i></div>';
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

function rowsApps(){
  const list=data.services||[];
  const st=data.updates||{};
  if(!list.length) return '<div class="row"><span class="t2">Nothing published yet.</span></div>';
  return '<div class="appgrid">'+list.map(s=>
    '<a class="apptile" data-up="'+(s.code?'1':'0')+'" href="'+appURL(s)+'" target="_blank" rel="noopener">'+
      '<span class="sdot"></span>'+
      (s.update?'<button class="upd" data-app="'+s.app+'" data-name="'+s.name+'" '+
                 'data-ver="'+(s.version||'')+'" aria-label="Update '+s.name+'"></button>':'')+
      (st[s.app]?'<span class="ust '+st[s.app].state+'">'+st[s.app].msg+'</span>':'')+
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
  cpu:   ()=>['CPU', powerChart()+
            (data.power!==null&&data.power!==undefined
              ? '<div class="row"><span class="l"><span class="t1">Package power</span>'+
                '<span class="t2">whole chip \\u2014 this CPU reports no per-core figure</span></span>'+
                '<span class="val">'+data.power+' W</span></div>' : '')+
            rowsCores()],
  mem:   ()=>['Memory', memChart()+memBreakdown()],
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
  wx:    ()=>['Weather',              wxChart((data.weather||{}).hours)+rowsWx(),
                                      (data.weather||{}).place||''],
};

function renderPop(opening){
  if(!openKey || !data || !POPS[openKey]) return;
  const r=POPS[openKey]();
  const b=$('sh-b');

  if($('sh-t').textContent!==r[0]) $('sh-t').textContent=r[0];
  const sub=r[2]||'';
  if($('sh-s').textContent!==sub) $('sh-s').textContent=sub;

  // The poll runs every 3s. Rewriting innerHTML each time rebuilt the whole
  // subtree - images reloaded, charts redrew, scroll position jumped and the
  // entrance animation replayed. That was the flashing. Only touch the DOM
  // when the markup has actually changed.
  if(b._html===r[1] && !opening) return;
  b._html=r[1];
  b.innerHTML=r[1];
  wireIcons(b);

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
  if(!data || !POPS[k]) return;
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
  if(ev.target.id==='u-no'){ closePop(); return; }
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
    ev.preventDefault(); menuOpen()?closeMenu():openMenu(); return;
  }
  const seg=ev.target.closest('#seg-theme button');
  if(seg){ prefs.theme=seg.dataset.theme; savePrefs(); applyTheme(); return; }
  const sw=ev.target.closest('#sw-accent button');
  if(sw){ prefs.accent=sw.dataset.accent; savePrefs(); applyAccent(); return; }
  if(ev.target.closest('#mi-edit')){ closeMenu(); setEditing(true); return; }
  if(ev.target.closest('#mi-ver')){ closeMenu(); openPop('up'); return; }
  if(ev.target.closest('#mi-setup')){ closeMenu(); openSetup(true); return; }
  const lk=ev.target.closest('#su-links button, #m-links button');
  if(lk){ setLinks(lk.dataset.links); return; }
  if(ev.target.closest('#mi-check')){ doCheck(); return; }
  const selfRow=ev.target.closest('#mi-self');
  if(selfRow){
    if(selfRow.dataset.state==='ready'){ closeMenu(); askSelfUpdate(); }
    return;
  }
  const cb=ev.target.closest('.cityres button[data-city]');
  if(cb){ pickCity(parseInt(cb.dataset.city,10), cb.closest('.cityres').id); return; }
  const sw2=ev.target.closest('.sw2');
  if(sw2){
    const on = sw2.getAttribute('aria-checked')!=='true';
    setSwitch(sw2.id, on);
    if(sw2.id==='o-wx'){ $('wxloc').style.display = on ? 'block' : 'none'; }
    const menuKey={'m-ts':'tailscale','m-oc':'openclaw','m-hx':'hexos','m-wx':'weather'}[sw2.id];
    if(menuKey){                       // in the menu these apply immediately
      prefs.setup=Object.assign({}, prefs.setup, {done:true});
      prefs.setup[menuKey]=on;
      savePrefs();
      if(data) paint(data);
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
  // the scrim swallows this tap: close, and let nothing else see it
  if(ev.target.closest('#mscrim')){
    ev.preventDefault(); ev.stopPropagation(); closeMenu(); return;
  }
  if(menuOpen() && !ev.target.closest('#menu')){ closeMenu(); return; }

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
  }
}
// chrome first, so the theme and accent are right before any data lands
loadPrefs();
buildSwatches();
applyTheme();
// the shared layout is authoritative; the local copy is only a fallback for
// first paint and for when the server has no writable config mount

$('mi-ver').textContent='Dashy';
// a system theme change has to re-pick the accent shade too
if(darkMQ){
  const onScheme=()=>{ if(prefs.theme==='system') applyAccent(); };
  darkMQ.addEventListener ? darkMQ.addEventListener('change',onScheme)
                          : darkMQ.addListener(onScheme);
}
document.addEventListener('keydown',ev=>{ if(ev.key==='Escape' && menuOpen()) closeMenu(); });
function commitNas(){
  const v=($('m-nas').value||'').trim();
  const next=/^https?:\\/\\//.test(v) ? v : '';
  if((prefs.setup||{}).truenas===next) return;
  prefs.setup=Object.assign({}, prefs.setup, {done:true, truenas:next});
  savePrefs();
  if(data) paint(data);
}
document.addEventListener('change', ev=>{ if(ev.target.id==='m-nas') commitNas(); });
document.addEventListener('input', ev=>{
  if(ev.target.id==='o-city') citySearchNow(ev.target.value,'o');
  if(ev.target.id==='m-city') citySearchNow(ev.target.value,'m');
});
document.addEventListener('keydown', ev=>{
  if(ev.target.id==='m-nas' && ev.key==='Enter'){ ev.preventDefault(); commitNas(); $('m-nas').blur(); }
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
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
        {"src": "/icon-512-maskable.png", "sizes": "512x512", "type": "image/png",
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
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path.startswith("/appicon/"):
            self._send_appicon(path[len("/appicon/"):])
        elif path in ICONS:
            self._send_icon(ICONS[path])
        elif path == "/manifest.webmanifest":
            self._send(200, json.dumps(MANIFEST), "application/manifest+json",
                       cache="public, max-age=86400")
        elif path == "/api/stats":
            self._send(200, json.dumps(snapshot()), "application/json; charset=utf-8")
        elif path.startswith("/api/geocode"):
            from urllib.parse import parse_qs, urlparse
            q = (parse_qs(urlparse(self.path).query).get("q") or [""])[0]
            self._send(200, json.dumps({"results": geocode(q)}),
                       "application/json; charset=utf-8")
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
          % (APP_NAME, PORT, _state["host"], TAILNET or "not published", DATA), flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
