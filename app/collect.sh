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

# --- is there a newer Dashy? Checked every 30 min, not every minute: neither
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
if [ $(( NOW - LAST )) -ge 1800 ]; then
  echo "$NOW" > "$STAMP"
  APPNAME=$(basename -- "$BASE")
  IMAGE=$(midclt call app.config "$APPNAME" 2>/dev/null | python3 -c "
import json,sys
try: svc=list(json.load(sys.stdin)['services'].values())[0]
except Exception: raise SystemExit
print(svc.get('image') or '')" 2>/dev/null)

  VER=""
  SRC="$REPO"
  case "$IMAGE" in
    ghcr.io/*dashy:*|ghcr.io/*dashy)
      # highest version tag the registry actually holds
      GREPO=${IMAGE#ghcr.io/}; GREPO=${GREPO%%:*}
      SRC="ghcr.io/$GREPO"
      VER=$(python3 - "$GREPO" <<'PYGHCR' 2>/dev/null
import json, sys, urllib.request
repo = sys.argv[1]

def get(url, tok=None):
    r = urllib.request.Request(url, headers={"User-Agent": "dashy"})
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
      printf '{"latest":"%s","checked":%s,"repo":"%s"}\n' "$VER" "$NOW" "$SRC" \
        > "$D/selfupdate.json.tmp" && mv "$D/selfupdate.json.tmp" "$D/selfupdate.json" ;;
  esac
fi
