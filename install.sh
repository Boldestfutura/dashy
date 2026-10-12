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
TSHOST=""
if [ -n "$SERVE" ] && [ -f "$SERVE" ] && python3 -c "import json;json.load(open('$SERVE'))" 2>/dev/null; then
  TSHOST=$(python3 -c "
import json
cfg=json.load(open('$SERVE'))
for h in (cfg.get('Web') or {}):
    print(h.rsplit(':',1)[0]); break")
fi

# Tailscale is how Dashy learns the tailnet name and which services you chose to
# publish. It is not required. Without it Dashy serves on the LAN and lists every
# app that binds a host port. Requiring it meant a new user with Tailscale running
# but nothing published yet just got told to go away, which helped nobody.
if [ -n "$TSHOST" ]; then
  TS=1
  info "using $SERVE"
  info "this machine is $TSHOST"
else
  TS=0
  if [ -n "$SERVE" ] && [ -f "$SERVE" ]; then
    info "found $SERVE, but nothing is published in it yet."
  else
    info "no Tailscale Serve config found."
  fi
  info "that is fine - installing for your LAN instead."
  # ...unless Tailscale is actually here, in which case offer to finish the job.
  # Serve on TrueNAS is a JSON file you write yourself, mounted into the
  # Tailscale container and named by TS_SERVE_CONFIG. Four fiddly steps that
  # people reasonably expect Tailscale to have done for them.
  if midclt call app.query '[["name","=","tailscale"]]' 2>/dev/null | grep -q '"name"'; then
    info ""
    info "Tailscale IS installed here, just not publishing anything yet."
    ans="$(ask "set Tailscale Serve up for you? (y/n)" "y")"
    case "$ans" in
      y|Y|yes|YES)
        info ""
        info "I need this machine's tailnet name. It is in the Tailscale admin"
        info "console, or run 'tailscale status' on any of your devices."
        info "It looks like  machine-name.tailXXXX.ts.net"
        TSHOST="$(ask "tailnet name" "")"
        if [ -n "$TSHOST" ]; then
          TS=2        # set it up after the app exists and the port is known
        else
          info "no name given - installing for your LAN instead."
        fi ;;
      *) info "installing for your LAN. Re-run this later to publish it." ;;
    esac
  else
    info "to publish on your tailnet later, install Tailscale and re-run this."
  fi
fi

# What a link in the page should point at. The app probes on 127.0.0.1, but a
# browser following a link needs an address that means this NAS from elsewhere.
LAN_IP="${LAN_IP:-$(ip route get 1.1.1.1 2>/dev/null \
        | awk '{for(i=1;i<=NF;i++) if($i=="src") {print $(i+1); exit}}')}"
[ -n "$LAN_IP" ] || LAN_IP="$(ask "this NAS's LAN address" "")"
[ -n "$LAN_IP" ] || die "need an address a browser can reach this NAS on."
info "LAN address $LAN_IP"

# ---------------------------------------------------------------- ports
say "picking ports"
LPORT="${LPORT:-}"
if [ -z "$LPORT" ]; then
  for p in $(seq 30800 30899); do
    ss -ltn 2>/dev/null | grep -q ":$p " || { LPORT=$p; break; }
  done
fi
[ -n "$LPORT" ] || die "no free local port in 30800-30899."

TPORT=""
if [ "$TS" != 0 ]; then
  if [ -z "${TPORT:-}" ]; then
    USED=$(python3 -c "
import json
try: cfg=json.load(open('$SERVE'))
except Exception: cfg={}
print(' '.join((cfg.get('TCP') or {}).keys()))" 2>/dev/null)
    for p in $(seq 8443 8499); do
      case " $USED " in *" $p "*) continue ;; esac
      TPORT=$p; break
    done
  fi
  [ -n "$TPORT" ] || die "no free tailnet port in 8443-8499."
  info "local $LPORT, tailnet $TPORT  ->  https://$TSHOST:$TPORT"
else
  info "local $LPORT  ->  http://$LAN_IP:$LPORT"
fi

# ---------------------------------------------------------------- packaging
say "how should the app be packaged"
info "image - pull the published container image. TrueNAS can then update it"
info "        from the Apps page like any other app."
info "files - stock python image with this directory mounted in. No registry,"
info "        and you can edit server.py in place."
MODE="${INSTALL_MODE:-$(ask "image or files" "image")}"
case "$MODE" in
  image|files) ;;
  *) die "INSTALL_MODE must be 'image' or 'files'" ;;
esac
IMAGE="${IMAGE:-ghcr.io/boldestfutura/dashy:latest}"
[ "$MODE" = image ] && info "using $IMAGE"

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
# collect.sh and worker.sh always land on disk: they run on the host from cron,
# whichever way the app itself is packaged. The rest matter only to a file
# install, but copying them costs nothing and keeps the directory honest.
cp "$REPO/app/"*.py "$REPO/app/"*.sh "$REPO/app/"*.png "$BASE/app/" || die "copy failed"
chmod +x "$BASE/app/collect.sh" "$BASE/app/worker.sh" "$BASE/app/entrypoint.sh"
info "$(ls -1 "$BASE/app" | wc -l) files -> $BASE/app"

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
python3 - "$APP_NAME" "$BASE" "$LPORT" "$SERVE" "$WLAT" "$WLON" "$WTZ" "$MODE" "$IMAGE" "$TS" "$LAN_IP" <<'PY'
import json, subprocess, sys
name, base, lport, serve, wlat, wlon, wtz, mode, image, ts, lan_ip = sys.argv[1:12]
env = {"DASH_PORT": lport, "DASH_DATA": "/data", "DASH_QUEUE": "/queue",
       "HOST_IP": "127.0.0.1", "LINK_HOST": lan_ip,
       "DASH_CONFIG": "/config",
       "PYTHONDONTWRITEBYTECODE": "1"}
if ts != "0":
    env["SERVE_JSON"] = "/serve/serve.json"
# so the dashboard's own "publish on the tailnet" button knows where to put it
env["SERVE_HOST_PATH"] = serve
if wlat and wlon:
    env.update({"WEATHER_LAT": wlat, "WEATHER_LON": wlon})
    if wtz:
        env["WEATHER_TZ"] = wtz
svc = {
    "container_name": name,
    "restart": "unless-stopped",
    # host networking: the real interface counters, and /proc is the host's
    "network_mode": "host",
    "working_dir": "/app",
    "environment": env,
    "volumes": [base + "/data:/data:ro",
                base + "/queue:/queue",
                base + "/config:/config"],
}
if ts != "0":
    svc["volumes"].append(serve + ":/serve/serve.json:ro")
if mode == "image":
    svc["image"] = image
    # read-write, and not at /app: the entrypoint copies the two cron scripts
    # back out to the host here, so an image update carries them along
    svc["volumes"].insert(0, base + "/app:/hostapp")
else:
    svc["image"] = "python:3.13-alpine"
    svc["command"] = ["python", "-u", "/app/server.py"]
    svc["volumes"].insert(0, base + "/app:/app:ro")
compose = {"services": {name: svc}}
r = subprocess.run(["midclt", "call", "-j", "app.create", json.dumps(
    {"custom_app": True, "app_name": name, "custom_compose_config": compose})],
    capture_output=True, text=True)
print("   app.create rc=%s %s" % (r.returncode, (r.stderr or "").strip()[:160]))
sys.exit(r.returncode)
PY
[ $? -eq 0 ] || die "app.create failed"

# ---------------------------------------------------------------- serve entry
if [ "$TS" = 2 ]; then
  say "setting up Tailscale Serve"
  [ -n "$SERVE" ] || SERVE="/mnt/$POOL/appconfig/serve.json"
  if python3 "$REPO/app/serve_setup.py" --host "$TSHOST" --tport "$TPORT" \
       --lport "$LPORT" --serve "$SERVE"; then
    TS=1
  else
    info "Serve setup failed. Dashy is installed and reachable at"
    info "http://$LAN_IP:$LPORT - fix Tailscale and re-run this to publish it."
    TS=0
  fi
fi

if [ "$TS" = 1 ]; then
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
fi

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
if [ "$TS" = 1 ]; then URL="https://$TSHOST:$TPORT"; else URL="http://$LAN_IP:$LPORT"; fi
cat <<EOF

   Dashy is at   $URL

   It takes a minute or two to look right: the pool, temperature and power
   figures come from the cron job, which runs on the minute.

   Packaging  $MODE
   Files      $BASE/app
   Data       $BASE/data

   To remove it again:  ./uninstall.sh
EOF
