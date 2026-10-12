#!/usr/bin/env bash
# Dashy installer for TrueNAS SCALE.
#
#   git clone <repo> && cd dashy && ./install.sh
#
# Everything it needs it works out for itself: the pool, a free local port, a
# free tailnet port, and where Tailscale keeps its Serve config. Anything it
# cannot work out it asks about. Nothing about any particular NAS is baked in.
#
# Run it as a user who can call midclt (root, or truenas_admin).
set -uo pipefail

REPO="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
APP_NAME="${APP_NAME:-dashy}"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
die()  { printf '\n\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
ask()  { # ask <prompt> <default>  -> echoes the answer
  local a; read -r -p "   $1 [$2]: " a </dev/tty; printf '%s' "${a:-$2}"; }

# ---------------------------------------------------------------- preflight
say "checking this is a TrueNAS SCALE box"
command -v midclt  >/dev/null || die "midclt not found - this installer is for TrueNAS SCALE."
command -v zfs     >/dev/null || die "zfs not found."
command -v python3 >/dev/null || die "python3 not found."
midclt call system.info >/dev/null 2>&1 || die "midclt call failed - are you root or truenas_admin?"
info "ok"

if midclt call app.query "[[\"name\",\"=\",\"$APP_NAME\"]]" 2>/dev/null | grep -q '"name"'; then
  die "an app called '$APP_NAME' already exists. Remove it first, or set APP_NAME=something-else."
fi

# ---------------------------------------------------------------- the pool
say "where should Dashy keep its files"
mapfile -t POOLS < <(zfs list -H -d 0 -o name 2>/dev/null)
[ "${#POOLS[@]}" -gt 0 ] || die "no ZFS pools found."
info "pools: ${POOLS[*]}"
# prefer a pool that already has an Applications dataset, else the first one
DEFPOOL="${POOLS[0]}"
for p in "${POOLS[@]}"; do
  zfs list -H -o name "$p/Applications" >/dev/null 2>&1 && { DEFPOOL="$p"; break; }
done
POOL="${POOL:-$(ask "pool" "$DEFPOOL")}"
zfs list -H -o name "$POOL" >/dev/null 2>&1 || die "no such pool: $POOL"
PARENT="$POOL/Applications"
BASE_DS="$PARENT/$APP_NAME"
BASE="/mnt/$BASE_DS"

# ---------------------------------------------------------------- serve.json
say "finding the Tailscale Serve config"
SERVE="${SERVE:-}"
if [ -z "$SERVE" ]; then
  # the tailscale app points at it with TS_SERVE_CONFIG; read that rather than guess
  SERVE=$(midclt call app.config tailscale 2>/dev/null | python3 -c '
import json,sys
try: cfg=json.load(sys.stdin)
except Exception: raise SystemExit
def walk(o):
    if isinstance(o,dict):
        for k,v in o.items():
            if k=="TS_SERVE_CONFIG" and isinstance(v,str): print(v); return True
            if walk(v): return True
    elif isinstance(o,list):
        for v in o:
            if walk(v): return True
    return False
walk(cfg)' 2>/dev/null | head -1)
  # that path is inside the tailscale container; find the host side of the mount
  if [ -n "$SERVE" ] && [ ! -f "$SERVE" ]; then
    CAND=$(find /mnt -maxdepth 4 -name "$(basename "$SERVE")" 2>/dev/null | head -1)
    [ -n "$CAND" ] && SERVE="$CAND"
  fi
fi
if [ -z "$SERVE" ] || [ ! -f "$SERVE" ]; then
  info "could not find it automatically."
  info "it is the file the tailscale app has bind-mounted as TS_SERVE_CONFIG."
  SERVE="$(ask "path to serve.json" "/mnt/$POOL/appconfig/serve.json")"
fi
[ -f "$SERVE" ] || die "no serve.json at $SERVE - set up Tailscale Serve first."
python3 -c "import json;json.load(open('$SERVE'))" 2>/dev/null || die "$SERVE is not valid JSON."
info "using $SERVE"

TSHOST=$(python3 -c "
import json
cfg=json.load(open('$SERVE'))
for h in (cfg.get('Web') or {}):
    print(h.rsplit(':',1)[0]); break")
[ -n "$TSHOST" ] || die "no Web entries in $SERVE - publish at least one service first."
info "this machine is $TSHOST"

# ---------------------------------------------------------------- ports
say "picking ports"
LPORT="${LPORT:-}"
if [ -z "$LPORT" ]; then
  for p in $(seq 30800 30899); do
    ss -ltn 2>/dev/null | grep -q ":$p " || { LPORT=$p; break; }
  done
fi
[ -n "$LPORT" ] || die "no free local port in 30800-30899."

TPORT="${TPORT:-}"
if [ -z "$TPORT" ]; then
  USED=$(python3 -c "
import json
cfg=json.load(open('$SERVE'))
print(' '.join((cfg.get('TCP') or {}).keys()))")
  for p in $(seq 8443 8499); do
    case " $USED " in *" $p "*) continue ;; esac
    TPORT=$p; break
  done
fi
[ -n "$TPORT" ] || die "no free tailnet port in 8443-8499."
info "local $LPORT, tailnet $TPORT  ->  https://$TSHOST:$TPORT"

# ---------------------------------------------------------------- weather
say "weather (optional, Open-Meteo, no API key)"
info "leave blank to skip the weather widget entirely."
WLAT="${WLAT:-$(ask "latitude"  "")}"
WLON="${WLON:-$(ask "longitude" "")}"
WTZ=""
[ -n "$WLAT" ] && WTZ="${WTZ:-$(ask "timezone" "$(cat /etc/timezone 2>/dev/null || echo UTC)")}"

# ---------------------------------------------------------------- datasets
say "creating datasets under $BASE_DS"
zfs list -H -o name "$PARENT" >/dev/null 2>&1 || \
  midclt call pool.dataset.create "{\"name\":\"$PARENT\",\"type\":\"FILESYSTEM\"}" >/dev/null
for d in "" /app /data /queue /config; do
  ds="$BASE_DS$d"
  if zfs list -H -o name "$ds" >/dev/null 2>&1; then
    info "exists  $ds"
  else
    midclt call pool.dataset.create "{\"name\":\"$ds\",\"type\":\"FILESYSTEM\"}" >/dev/null \
      && info "created $ds" || die "could not create $ds"
  fi
done

say "installing files"
cp "$REPO/app/server.py" "$REPO/app/collect.sh" "$REPO/app/worker.sh" "$BASE/app/" || die "copy failed"
chmod +x "$BASE/app/collect.sh" "$BASE/app/worker.sh"
info "server.py, collect.sh, worker.sh -> $BASE/app"

# the container runs as this uid; the data and queue dirs must be writable by it
midclt call -j filesystem.chown \
  "{\"path\":\"$BASE/data\",\"uid\":568,\"gid\":568,\"options\":{\"recursive\":true}}" >/dev/null 2>&1
midclt call -j filesystem.chown \
  "{\"path\":\"$BASE/queue\",\"uid\":568,\"gid\":568,\"options\":{\"recursive\":true}}" >/dev/null 2>&1
# layout.json is written by the app, so this one must match the container user
midclt call -j filesystem.chown \
  "{\"path\":\"$BASE/config\",\"uid\":568,\"gid\":0,\"options\":{\"recursive\":true}}" >/dev/null 2>&1

# ---------------------------------------------------------------- the app
say "creating the app"
python3 - "$APP_NAME" "$BASE" "$LPORT" "$SERVE" "$WLAT" "$WLON" "$WTZ" <<'PY'
import json, subprocess, sys
name, base, lport, serve, wlat, wlon, wtz = sys.argv[1:8]
env = {"DASH_PORT": lport, "DASH_DATA": "/data", "DASH_QUEUE": "/queue",
       "SERVE_JSON": "/serve/serve.json", "HOST_IP": "127.0.0.1",
       "DASH_CONFIG": "/config",
       "PYTHONDONTWRITEBYTECODE": "1"}
if wlat and wlon:
    env.update({"WEATHER_LAT": wlat, "WEATHER_LON": wlon})
    if wtz:
        env["WEATHER_TZ"] = wtz
compose = {"services": {name: {
    "image": "python:3.13-alpine",
    "container_name": name,
    "restart": "unless-stopped",
    # host networking: the real interface counters, and /proc is the host's
    "network_mode": "host",
    "command": ["python", "-u", "/app/server.py"],
    "working_dir": "/app",
    "environment": env,
    "volumes": [base + "/app:/app:ro",
                base + "/data:/data:ro",
                base + "/queue:/queue",
                base + "/config:/config",
                serve + ":/serve/serve.json:ro"],
}}}
r = subprocess.run(["midclt", "call", "-j", "app.create", json.dumps(
    {"custom_app": True, "app_name": name, "custom_compose_config": compose})],
    capture_output=True, text=True)
print("   app.create rc=%s %s" % (r.returncode, (r.stderr or "").strip()[:160]))
sys.exit(r.returncode)
PY
[ $? -eq 0 ] || die "app.create failed"

# ---------------------------------------------------------------- serve entry
say "publishing on the tailnet"
python3 - "$SERVE" "$TSHOST" "$TPORT" "$LPORT" <<'PY'
import json, shutil, sys, time
path, host, tport, lport = sys.argv[1:5]
shutil.copy2(path, path + ".bak-%d" % int(time.time()))
cfg = json.load(open(path))
# BOTH halves are required: TCP opens the listener and gets the cert, Web routes.
cfg.setdefault("TCP", {})[tport] = {"HTTPS": True}
cfg.setdefault("Web", {})["%s:%s" % (host, tport)] = {
    "Handlers": {"/": {"Proxy": "http://127.0.0.1:%s" % lport}}}
json.dump(cfg, open(path, "w"), indent=1)
print("   added TCP %s and a Web handler -> 127.0.0.1:%s" % (tport, lport))
PY

# ---------------------------------------------------------------- cron
say "installing the two root cron jobs"
for pair in "collect.sh|$APP_NAME pool stats" "worker.sh|$APP_NAME upgrade worker"; do
  f="${pair%%|*}"; desc="${pair#*|}"
  midclt call cronjob.create "{\"user\":\"root\",\"command\":\"sh $BASE/app/$f\",
    \"description\":\"$desc\",\"enabled\":true,\"stdout\":false,\"stderr\":false,
    \"schedule\":{\"minute\":\"*\",\"hour\":\"*\",\"dom\":\"*\",\"month\":\"*\",\"dow\":\"*\"}}" \
    >/dev/null 2>&1 && info "added: $desc" || info "could not add: $desc (add it by hand)"
done

# ---------------------------------------------------------------- done
say "done"
cat <<EOF

   Dashy is at   https://$TSHOST:$TPORT

   It takes a minute or two to look right: the pool, temperature and power
   figures come from the cron job, which runs on the minute.

   Files      $BASE/app
   Data       $BASE/data
   Serve      $SERVE   (backed up before the edit)

   To remove it again:  ./uninstall.sh
EOF
