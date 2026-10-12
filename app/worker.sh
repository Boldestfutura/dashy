#!/bin/sh
# Processes upgrade requests Dashy drops in the queue. Runs as root from cron.
# A request is a file named <app>.req; the app name is validated against the
# installed app list before anything is run.
# Paths derive from this script's own location, so the app works wherever the
# dataset lives: <base>/app/<this script>, <base>/data, <base>/queue.
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BASE=$(dirname -- "$APP_DIR")
Q="$BASE/queue"
D="$BASE/data"
S="$D/updates.json"

# --- a request to update Dashy itself, pulled straight from the repo --------
if [ -f "$Q/_self.req" ]; then
  rm -f "$Q/_self.req"
  REPO="${UPDATE_REPO:-Boldestfutura/dashy}"
  BRANCH="${UPDATE_BRANCH:-main}"
  BASEURL="https://raw.githubusercontent.com/$REPO/$BRANCH/app"
  TMP=$(mktemp -d)
  note(){ python3 - "$1" "$2" "$S" <<'PYN'
import json,sys,time
st,msg,path=sys.argv[1],sys.argv[2],sys.argv[3]
try: d=json.load(open(path))
except Exception: d={}
d["_self"]={"state":st,"msg":msg,"t":int(time.time())}
json.dump(d,open(path,"w"))
PYN
  }
  note running "updating"

  # Every file the app needs. Fetch all of them before touching anything.
  OK=1
  for f in server.py collect.sh worker.sh favicon.png icon-180.png icon-192.png \
           icon-512.png icon-512-maskable.png; do
    curl -fsSL -m 60 "$BASEURL/$f" -o "$TMP/$f" 2>/dev/null || OK=0
  done
  # A half-downloaded server.py would take the dashboard down, so prove it parses
  python3 -m py_compile "$TMP/server.py" 2>/dev/null || OK=0
  [ -s "$TMP/server.py" ] || OK=0

  if [ "$OK" = 1 ]; then
    stamp=$(date +%Y%m%d-%H%M%S)
    ds=${BASE#/mnt/}
    zfs list -H -o name "$ds" >/dev/null 2>&1 && \
      zfs snapshot -r "$ds@preupdate-$stamp" 2>/dev/null
    for f in server.py favicon.png icon-180.png icon-192.png icon-512.png \
             icon-512-maskable.png collect.sh; do
      cp "$TMP/$f" "$APP_DIR/$f" 2>/dev/null
    done
    # last, and by rename: this script is running from that path right now, and
    # an atomic swap leaves the running shell reading the old inode
    mv "$TMP/worker.sh" "$APP_DIR/worker.sh.new" 2>/dev/null && \
      mv "$APP_DIR/worker.sh.new" "$APP_DIR/worker.sh"
    chmod +x "$APP_DIR/collect.sh" "$APP_DIR/worker.sh" 2>/dev/null
    if midclt call -j app.redeploy "$(basename "$BASE")" >/dev/null 2>&1; then
      note done "updated"
    else
      note done "files updated, restart the app to finish"
    fi
  else
    note failed "download failed, nothing changed"
  fi
  rm -rf "$TMP"
fi

[ -d "$Q" ] || exit 0
set -- "$Q"/*.req
[ -e "$1" ] || exit 0

for f in "$Q"/*.req; do
  [ -e "$f" ] || continue
  app=$(basename "$f" .req)
  rm -f "$f"

  # only ever act on an app that actually exists
  known=$(midclt call app.query "[[\"name\",\"=\",\"$app\"]]" 2>/dev/null | head -c 20)
  case "$known" in
    "[]"|"") python3 - "$app" "$S" <<'PY'
import json,sys,time
app,path=sys.argv[1],sys.argv[2]
try: d=json.load(open(path))
except Exception: d={}
d[app]={"state":"failed","msg":"no such app","t":int(time.time())}
json.dump(d,open(path,"w"))
PY
      continue ;;
  esac

  python3 - "$app" "$S" "running" "upgrading" <<'PY'
import json,sys,time
app,path,st,msg=sys.argv[1:5]
try: d=json.load(open(path))
except Exception: d={}
d[app]={"state":st,"msg":msg,"t":int(time.time())}
json.dump(d,open(path,"w"))
PY

  # rollback point for the app's own data before the upgrade touches it
  stamp=$(date +%Y%m%d-%H%M%S)
  # The app's own dataset sits beside this one, whatever the pool is called,
  # and ix-apps lives on whichever pool the apps pool was set to - both are
  # derived rather than named, so this works on any TrueNAS install.
  apps_ds=${BASE%/*}; apps_ds=${apps_ds#/mnt/}
  ixcfg=$(zfs list -H -o name 2>/dev/null | grep -m1 '/ix-apps/app_configs$')
  for ds in "$apps_ds/$app" $ixcfg; do
    [ -n "$ds" ] || continue
    zfs list -H -o name "$ds" >/dev/null 2>&1 && \
      zfs snapshot -r "$ds@preupgrade-$app-$stamp" 2>/dev/null
  done

  if midclt call -j app.upgrade "$app" >/dev/null 2>&1; then
    res=done; msg="upgraded"
  else
    res=failed; msg="upgrade failed"
  fi
  python3 - "$app" "$S" "$res" "$msg" <<'PY'
import json,sys,time
app,path,st,msg=sys.argv[1:5]
try: d=json.load(open(path))
except Exception: d={}
d[app]={"state":st,"msg":msg,"t":int(time.time())}
json.dump(d,open(path,"w"))
PY
done
