#!/bin/sh
# Publishes facts the dashboard container cannot read for itself. Runs as root from cron.
# Paths derive from this script's own location, so the app works wherever the
# dataset lives: <base>/app/<this script>, <base>/data, <base>/queue.
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BASE=$(dirname -- "$APP_DIR")
D="$BASE/data"

# --- pools: name, size, allocated, free, health
zpool list -Hp -o name,size,allocated,free,health > "$D/pools.tsv.tmp" 2>/dev/null \
  && mv "$D/pools.tsv.tmp" "$D/pools.tsv"

# --- CPU package temperature, from the coretemp sensor
for h in /sys/class/hwmon/hwmon*; do
  [ "$(cat "$h/name" 2>/dev/null)" = "coretemp" ] || continue
  for l in "$h"/temp*_label; do
    case "$(cat "$l" 2>/dev/null)" in
      Package*) cat "${l%_label}_input" 2>/dev/null > "$D/cputemp.tmp" && \
                mv "$D/cputemp.tmp" "$D/cputemp"; break 2 ;;
    esac
  done
done

# --- per-core temperatures
: > "$D/cores.tsv.tmp"
for h in /sys/class/hwmon/hwmon*; do
  [ "$(cat "$h/name" 2>/dev/null)" = "coretemp" ] || continue
  for l in "$h"/temp*_label; do
    lb=$(cat "$l" 2>/dev/null)
    case "$lb" in Core*) printf "%s\t%s\n" "$lb" "$(cat "${l%_label}_input" 2>/dev/null)" >> "$D/cores.tsv.tmp" ;; esac
  done
done
mv "$D/cores.tsv.tmp" "$D/cores.tsv"

# --- boot/shutdown history, for the uptime record
last -x --time-format=iso reboot shutdown 2>/dev/null | head -60 > "$D/boots.txt.tmp" \
  && mv "$D/boots.txt.tmp" "$D/boots.txt"

# --- drives: one JSON object per line
: > "$D/drives.jsonl.tmp"
for dev in $(lsblk -dno NAME 2>/dev/null | grep -Ev '^(loop|zd|sr)'); do
  smartctl -j -H -A -i "/dev/$dev" 2>/dev/null | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: raise SystemExit
st=d.get('smart_status') or {}
print(json.dumps({
  'dev':   '$dev',
  'model': d.get('model_name') or '',
  'size':  (d.get('user_capacity') or {}).get('bytes'),
  'temp':  (d.get('temperature') or {}).get('current'),
  'ok':    st.get('passed'),
  'hours': (d.get('power_on_time') or {}).get('hours'),
  'rpm':   d.get('rotation_rate'),
}))" >> "$D/drives.jsonl.tmp"
done
mv "$D/drives.jsonl.tmp" "$D/drives.jsonl"

# --- installed apps and the host ports they publish, so the dashboard can
# --- name a tailnet mapping without anything being hard-coded
midclt call app.query 2>/dev/null | python3 -c "
import json,sys
try: apps=json.load(sys.stdin)
except Exception: raise SystemExit
out=[]
for a in apps:
    ports=set()
    for w in (a.get('active_workloads') or {}).get('used_ports') or []:
        for hp in w.get('host_ports') or []:
            if hp.get('host_ip') in ('0.0.0.0','::'):
                ports.add(hp.get('host_port'))
    md=a.get('metadata') or {}
    out.append({'name':a.get('name'),'state':a.get('state'),
                'ports':sorted(p for p in ports if p),
                'version':a.get('human_version'),
                'icon':md.get('icon') or md.get('icon_url') or '',
                # TrueNAS's own answer to 'what URL opens this app', path included
                'portals':a.get('portals') or {},
                'update':bool(a.get('upgrade_available')) or bool(a.get('image_updates_available'))})
json.dump(out,sys.stdout)
" > "$D/apps.json.tmp" 2>/dev/null && mv "$D/apps.json.tmp" "$D/apps.json"

# --- cache app icons locally, so the browser never calls out to the internet
# --- and the grid still works with no WAN. Fetched once, then left alone.
mkdir -p "$D/icons" 2>/dev/null
python3 - "$D" <<'PYICON' 2>/dev/null
import json, os, sys, urllib.request
d = sys.argv[1]
icons = os.path.join(d, "icons")
try:
    apps = json.load(open(os.path.join(d, "apps.json")))
except Exception:
    raise SystemExit
have = {f.rsplit(".", 1)[0] for f in os.listdir(icons)} if os.path.isdir(icons) else set()
for a in apps:
    name, url = a.get("name"), a.get("icon") or ""
    if not name or not url or name in have:
        continue
    ext = url.rsplit(".", 1)[-1].lower()
    if ext not in ("svg", "png", "webp", "jpg", "jpeg"):
        continue
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "overlook"})
        with urllib.request.urlopen(req, timeout=10) as r:
            blob = r.read(512 * 1024)
        tmp = os.path.join(icons, name + "." + ext + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, os.path.join(icons, name + "." + ext))
    except Exception:
        pass
PYICON

# --- what is actually using the machine right now. The container cannot see
# --- host processes (no pid namespace sharing), so sample here. Two reads of
# --- /proc two seconds apart give a real instantaneous figure; ps would give
# --- the average since each process started, which is a different question.
# ---
# --- A process inside a container has /docker/<id> in its cgroup, and root can
# --- read that container's metadata to recover the app it belongs to - so a
# --- spike can be blamed on "plex" rather than on "Plex Media Server".
python3 - "$D" <<'PYPROC'
import json, os, sys, time

D = sys.argv[1]
HZ = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")


def sample():
    out = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % pid) as f:
                st = f.read()
            # comm is parenthesised and may itself contain spaces or brackets
            head, _, rest = st.rpartition(")")
            comm = head.partition("(")[2]
            f = rest.split()
            out[pid] = (comm, int(f[11]) + int(f[12]), int(f[21]) * PAGE)
        except Exception:
            continue
    return out


def appname(pid, cache, root):
    try:
        with open("/proc/%s/cgroup" % pid) as f:
            cg = f.read()
    except Exception:
        return ""
    i = cg.find("/docker/")
    if i < 0:
        return ""
    cid = cg[i + 8:].split("\n")[0].strip().split("/")[0]
    if len(cid) < 12:
        return ""
    if cid in cache:
        return cache[cid]
    name = ""
    try:
        with open(os.path.join(root, "containers", cid, "config.v2.json")) as f:
            meta = json.load(f)
        # Compose records the project, which on TrueNAS is the app name. The
        # container name is /ix-<app>-<service>-<n>, and an app whose name has
        # a hyphen in it makes that unsplittable - so only fall back to it.
        labels = ((meta.get("Config") or {}).get("Labels") or {})
        name = labels.get("com.docker.compose.project") or ""
        # TrueNAS names every compose project ix-<app>; that prefix is an
        # implementation detail and should not reach the dashboard.
        if name.startswith("ix-"):
            name = name[3:]
        if not name:
            raw = (meta.get("Name") or "").lstrip("/")
            name = raw[3:].rsplit("-", 2)[0] if raw.startswith("ix-") else raw
    except Exception:
        name = ""
    cache[cid] = name
    return name


root = "/var/lib/docker"
try:
    import subprocess
    cfg = json.loads(subprocess.run(["midclt", "call", "docker.config"],
                                    capture_output=True, text=True, timeout=30).stdout)
    ds = cfg.get("dataset")
    if ds:
        # Ask ZFS where the dataset actually is. Assuming /mnt/<dataset> is
        # wrong on TrueNAS: HDDs/ix-apps mounts at /mnt/.ix-apps, so the
        # lookup silently opened a path that did not exist and every process
        # came back with no app name.
        mp = subprocess.run(["zfs", "get", "-H", "-o", "value", "mountpoint", ds],
                            capture_output=True, text=True, timeout=30).stdout.strip()
        if mp and mp not in ("-", "none", "legacy"):
            root = os.path.join(mp, "docker")
        else:
            root = "/mnt/%s/docker" % ds
except Exception:
    pass

a = sample()
time.sleep(2)
b = sample()

cache, rows = {}, []
for pid, (comm, ticks, rss) in b.items():
    if pid not in a:
        continue
    pct = (ticks - a[pid][1]) * 100.0 / (2.0 * HZ)
    if pct < 0:
        pct = 0.0
    rows.append({"pid": int(pid), "comm": comm, "app": appname(pid, cache, root),
                 "cpu": round(pct, 1), "rss": rss})

rows.sort(key=lambda r: -r["cpu"])
top_cpu = [r for r in rows if r["cpu"] > 0][:8]
rows.sort(key=lambda r: -r["rss"])
top_mem = rows[:8]

tmp = D + "/procs.json.tmp"
with open(tmp, "w") as f:
    json.dump({"at": int(time.time()), "cpu": top_cpu, "mem": top_mem,
               "ncpu": os.cpu_count()}, f)
os.replace(tmp, D + "/procs.json")
PYPROC

# --- pool health beyond "ONLINE": scrubs, and the three error counters that
# --- are the first sign of a disk going. zpool list cannot report any of it.
python3 - "$D" <<'PYSTATUS'
import json, os, re, subprocess, sys

D = sys.argv[1]
out = {}
try:
    names = subprocess.run(["zpool", "list", "-Hp", "-o", "name"],
                           capture_output=True, text=True, timeout=30).stdout.split()
except Exception:
    names = []

for name in names:
    try:
        txt = subprocess.run(["zpool", "status", name],
                             capture_output=True, text=True, timeout=60).stdout
    except Exception:
        continue
    rec = {"scan": "", "errors": "", "read": 0, "write": 0, "cksum": 0, "frag": None}

    m = re.search(r"^\s*scan:\s*(.+?)(?=^\s*\w+:|\Z)", txt, re.S | re.M)
    if m:
        rec["scan"] = " ".join(m.group(1).split())
    m = re.search(r"^\s*errors:\s*(.+)$", txt, re.M)
    if m:
        rec["errors"] = m.group(1).strip()

    # the per-vdev counters, summed: anything non-zero is worth surfacing
    for line in txt.splitlines():
        f = line.split()
        if len(f) >= 5 and f[1] in ("ONLINE", "DEGRADED", "FAULTED", "OFFLINE",
                                    "UNAVAIL", "REMOVED"):
            try:
                rec["read"] += int(f[2]); rec["write"] += int(f[3]); rec["cksum"] += int(f[4])
            except ValueError:
                pass
    try:
        frag = subprocess.run(["zpool", "list", "-Hp", "-o", "frag", name],
                              capture_output=True, text=True, timeout=30).stdout.strip()
        rec["frag"] = int(frag.rstrip("%")) if frag not in ("", "-") else None
    except Exception:
        pass
    out[name] = rec

tmp = D + "/poolstatus.json.tmp"
with open(tmp, "w") as f:
    json.dump(out, f)
os.replace(tmp, D + "/poolstatus.json")
PYSTATUS

# --- virtual machines. The container has no midclt, so the names, states and
# --- sizes come from here like everything else.
python3 - "$D" <<'PYVM'
import json, os, subprocess, sys

D = sys.argv[1]
out = []
try:
    vms = json.loads(subprocess.run(["midclt", "call", "vm.query"],
                                    capture_output=True, text=True, timeout=60).stdout)
except Exception:
    vms = []

for v in vms if isinstance(vms, list) else []:
    st = v.get("status") or {}
    devs = v.get("devices") or []
    kinds = {}
    for d in devs:
        a = d.get("attributes") or {}
        k = d.get("dtype") or a.get("dtype") or "?"
        kinds[k] = kinds.get(k, 0) + 1
    # vcpus x cores x threads is the real total; the UI should not have to
    # know that TrueNAS splits it three ways
    cpus = (v.get("vcpus") or 1) * (v.get("cores") or 1) * (v.get("threads") or 1)
    out.append({
        "id": v.get("id"),
        "name": v.get("name"),
        "description": v.get("description") or "",
        "state": st.get("state") or "UNKNOWN",
        "cpus": cpus,
        "memory": v.get("memory"),
        "autostart": bool(v.get("autostart")),
        "devices": kinds,
        "usb": [(d.get("attributes") or {}).get("device")
                for d in devs
                if (d.get("dtype") or (d.get("attributes") or {}).get("dtype")) == "USB"],
    })

out.sort(key=lambda x: (x.get("name") or "").lower())
tmp = D + "/vms.json.tmp"
with open(tmp, "w") as f:
    json.dump(out, f)
os.replace(tmp, D + "/vms.json")
PYVM

# --- CPU package power. RAPL is root-only, so sample it here and leave a trail
# --- the container can read. 25 samples at 2s covers most of each minute.
E=/sys/class/powercap/intel-rapl:0/energy_uj
if [ -r "$E" ]; then
  prev=$(cat "$E" 2>/dev/null)
  i=0
  while [ $i -lt 25 ]; do
    sleep 2
    cur=$(cat "$E" 2>/dev/null)
    [ -n "$cur" ] && [ -n "$prev" ] && [ "$cur" -ge "$prev" ] 2>/dev/null && \
      echo "$(date +%s)	$(( (cur - prev) / 2000000 ))" >> "$D/power.tsv"
    prev=$cur
    i=$((i+1))
  done
  tail -n 200 "$D/power.tsv" > "$D/power.tmp" 2>/dev/null && mv "$D/power.tmp" "$D/power.tsv"
fi

date +%s > "$D/pools.stamp"

# --- is there a newer Overlook? Checked every 30 min, not every minute: neither
# --- GitHub nor ghcr needs 1440 requests a day to answer the same question.
# ---
# --- Where to look depends on how this install is packaged. An image install
# --- can only ever run what the registry has, and CI publishes that a minute
# --- or so behind the push - so asking GitHub would offer an update that is
# --- not installable yet. Ask whichever one is actually the source.
REPO="${UPDATE_REPO:-Boldestfutura/dashy}"
BRANCH="${UPDATE_BRANCH:-main}"
STAMP="$D/selfupdate.stamp"
NOW=$(date +%s)
LAST=$(cat "$STAMP" 2>/dev/null || echo 0)
APPNAME=$(basename -- "$BASE")

# One read of the app's own config gives both the image, which says how this
# install is packaged, and the port, which says where to ask it its version.
CFG=$(midclt call app.config "$APPNAME" 2>/dev/null)
IMAGE=$(printf '%s' "$CFG" | python3 -c "
import json,sys
try: svc=list(json.load(sys.stdin)['services'].values())[0]
except Exception: raise SystemExit
print(svc.get('image') or '')" 2>/dev/null)
DPORT=$(printf '%s' "$CFG" | python3 -c "
import json,sys
try: svc=list(json.load(sys.stdin)['services'].values())[0]
except Exception: raise SystemExit
print((svc.get('environment') or {}).get('DASH_PORT') or '')" 2>/dev/null)

# Half an hour between checks is fine right up until Overlook itself is updated,
# at which point the cached "latest" is being compared against a version that
# is no longer running - which shows the user a stale and meaningless upgrade.
# So if the running version has moved since the last check, check again now.
CUR=""
if [ -n "$DPORT" ]; then
  CUR=$(curl -fsS -m 5 "http://127.0.0.1:$DPORT/version" 2>/dev/null \
        | sed -n 's/.*"version"[^"]*"\([^"]*\)".*/\1/p')
  WAS=$(python3 -c "
import json
try: print(json.load(open('$D/selfupdate.json')).get('current') or '')
except Exception: print('')" 2>/dev/null)
  if [ -n "$CUR" ] && [ "$CUR" != "$WAS" ]; then
    LAST=0
  fi
fi

if [ $(( NOW - LAST )) -ge 1800 ]; then
  echo "$NOW" > "$STAMP"

  VER=""
  SRC="$REPO"
  case "$IMAGE" in
    ghcr.io/*overlook:*|ghcr.io/*overlook)
      # highest version tag the registry actually holds
      GREPO=${IMAGE#ghcr.io/}; GREPO=${GREPO%%:*}
      SRC="ghcr.io/$GREPO"
      VER=$(python3 - "$GREPO" <<'PYGHCR' 2>/dev/null
import json, sys, urllib.request
repo = sys.argv[1]

def get(url, tok=None):
    r = urllib.request.Request(url, headers={"User-Agent": "overlook"})
    if tok:
        r.add_header("Authorization", "Bearer " + tok)
    with urllib.request.urlopen(r, timeout=20) as f:
        return json.load(f)

def vt(v):
    out = []
    for part in v.split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out + [0] * (4 - len(out)))[:4]

try:
    tok = get("https://ghcr.io/token?service=ghcr.io&scope=repository:%s:pull" % repo)["token"]
    tags = [t for t in get("https://ghcr.io/v2/%s/tags/list" % repo, tok).get("tags") or []
            if t and t[0].isdigit()]
    if tags:
        print(max(tags, key=vt))
except Exception:
    pass
PYGHCR
) ;;
  esac

  # file install, or the registry did not answer
  if [ -z "$VER" ]; then
    SRC="$REPO"
    RAW="https://raw.githubusercontent.com/$REPO/$BRANCH/app/server.py"
    VER=$(curl -fsSL -m 20 "$RAW" 2>/dev/null \
          | grep -m1 '^APP_VERSION' | sed 's/.*"\(.*\)".*/\1/')
  fi

  case "$VER" in
    [0-9]*)
      printf '{"latest":"%s","current":"%s","checked":%s,"repo":"%s"}\n' \
        "$VER" "$CUR" "$NOW" "$SRC" \
        > "$D/selfupdate.json.tmp" && mv "$D/selfupdate.json.tmp" "$D/selfupdate.json" ;;
  esac

  # Overlook and the TrueNAS Apps page were on two independent half-hour timers
  # that never lined up, so the dashboard could know about an update for up to
  # thirty minutes before the Apps page did. If there is one, clear the sweep
  # stamp: the block at the foot of this script then runs it on this same pass.
  if [ -n "$VER" ] && [ -n "$CUR" ] && python3 -c "
import sys
def vt(v):
    out = []
    for part in str(v).split('.'):
        digits = ''.join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out + [0] * (4 - len(out)))[:4]
sys.exit(0 if vt('$VER') > vt('$CUR') else 1)" 2>/dev/null; then
    rm -f "$D/imagesweep.stamp"
  fi
fi

# --- TrueNAS sweeps the registries for newer images only when the Docker
# --- service starts (app.image.op.check_update has no periodic caller), so a
# --- custom app can sit on a stale "no update" for weeks. Nudge it on the same
# --- half-hour cadence. This is what keeps both Overlook's own update markers and
# --- the TrueNAS Apps page honest. Set IMAGE_UPDATE_SWEEP=0 to leave it alone.
if [ "${IMAGE_UPDATE_SWEEP:-1}" = 1 ]; then
  ISTAMP="$D/imagesweep.stamp"
  ILAST=$(cat "$ISTAMP" 2>/dev/null || echo 0)
  if [ $(( NOW - ILAST )) -ge 1800 ]; then
    echo "$NOW" > "$ISTAMP"
    # the sweep routinely outruns midclt's client timeout while still finishing
    # server-side, so start it and walk away rather than failing on the wait
    (midclt call app.image.op.check_update >/dev/null 2>&1 &)
  fi
fi
