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
        req = urllib.request.Request(url, headers={"User-Agent": "dashy"})
        with urllib.request.urlopen(req, timeout=10) as r:
            blob = r.read(512 * 1024)
        tmp = os.path.join(icons, name + "." + ext + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, os.path.join(icons, name + "." + ext))
    except Exception:
        pass
PYICON

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

# --- does the repo have a newer Dashy? Checked every 30 min, not every minute:
# --- GitHub does not need 1440 requests a day to answer the same question.
REPO="${UPDATE_REPO:-Boldestfutura/dashy}"
BRANCH="${UPDATE_BRANCH:-main}"
STAMP="$D/selfupdate.stamp"
NOW=$(date +%s)
LAST=$(cat "$STAMP" 2>/dev/null || echo 0)
if [ $(( NOW - LAST )) -ge 1800 ]; then
  echo "$NOW" > "$STAMP"
  RAW="https://raw.githubusercontent.com/$REPO/$BRANCH/app/server.py"
  VER=$(curl -fsSL -m 20 "$RAW" 2>/dev/null \
        | grep -m1 '^APP_VERSION' | sed 's/.*"\(.*\)".*/\1/')
  case "$VER" in
    [0-9]*)
      printf '{"latest":"%s","checked":%s,"repo":"%s"}\n' "$VER" "$NOW" "$REPO" \
        > "$D/selfupdate.json.tmp" && mv "$D/selfupdate.json.tmp" "$D/selfupdate.json" ;;
  esac
fi
