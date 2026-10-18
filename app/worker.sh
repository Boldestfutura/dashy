#!/bin/sh
# Processes upgrade requests Overlook drops in the queue. Runs as root from cron.
# A request is a file named <app>.req; the app name is validated against the
# installed app list before anything is run.
# Paths derive from this script's own location, so the app works wherever the
# dataset lives: <base>/app/<this script>, <base>/data, <base>/queue.
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BASE=$(dirname -- "$APP_DIR")
Q="$BASE/queue"
D="$BASE/data"
S="$D/updates.json"

# --- a request from the dashboard to set up Tailscale Serve -----------------
# The container cannot do this itself: it has no midclt, and the Tailscale app
# has to be edited and redeployed. The dashboard drops the parameters here and
# this runs serve_setup.py, which is the same script install.sh uses.
if [ -f "$Q/_serve.req" ]; then
  REQ=$(cat "$Q/_serve.req" 2>/dev/null)
  rm -f "$Q/_serve.req"
  snote(){ python3 - "$1" "$2" "$S" <<'PYS'
import json,sys,time
st,msg,path=sys.argv[1],sys.argv[2],sys.argv[3]
try: d=json.load(open(path))
except Exception: d={}
d["_serve"]={"state":st,"msg":msg,"t":int(time.time())}
json.dump(d,open(path,"w"))
PYS
  }
  snote running "setting up Tailscale Serve"
  # The app sends a list of local ports it has actually discovered. Validate
  # it here too: the queue is a file, and a file is not an argument list.
  PORTS=$(printf '%s' "$REQ" | python3 -c "
import json,sys,re
try: r=json.load(sys.stdin)
except Exception: raise SystemExit
host=str(r.get('host') or '').strip()
if host and not re.match(r'^[A-Za-z0-9][A-Za-z0-9.-]{0,252}\$', host): raise SystemExit
out=[]
for p in (r.get('lports') or [])[:60]:
    try: p=int(p)
    except Exception: continue
    if 1 <= p <= 65535: out.append(str(p))
if not out: raise SystemExit
print((host or '-') + ' ' + ' '.join(sorted(set(out), key=int)))
" 2>/dev/null)
  SPATH=$(printf '%s' "$REQ" | python3 -c "
import json,sys
try: r=json.load(sys.stdin)
except Exception: raise SystemExit
s=str(r.get('serve') or '').strip()
if s and (not s.startswith('/mnt/') or '..' in s): raise SystemExit
print(s or '-')
" 2>/dev/null)
  if [ -z "$PORTS" ]; then
    snote failed "the request did not make sense"
  else
    set -- $PORTS
    HOST="$1"; shift
    [ "$HOST" = "-" ] && HOST='${TS_CERT_DOMAIN}'
    if [ -z "$SPATH" ] || [ "$SPATH" = "-" ]; then
      # no hint from the app, so put it beside the other app config on our pool:
      # /mnt/<pool>/Applications/<app> -> /mnt/<pool>/appconfig
      POOL=${BASE#/mnt/}; POOL=${POOL%%/*}
      SPATH="/mnt/$POOL/appconfig/serve.json"
    fi
    : > "$D/serve-setup.log"
    OK=0; BAD=0
    for LP in "$@"; do
      if python3 "$APP_DIR/serve_setup.py" --host "$HOST" --tport auto \
           --lport "$LP" --serve "$SPATH" >> "$D/serve-setup.log" 2>&1; then
        OK=$((OK+1))
      else
        BAD=$((BAD+1))
      fi
    done
    if [ "$BAD" = 0 ]; then
      snote done "published $OK"
    elif [ "$OK" = 0 ]; then
      snote failed "$(tail -1 "$D/serve-setup.log" 2>/dev/null | head -c 120)"
    else
      snote done "published $OK, $BAD failed"
    fi
  fi
fi

# --- a request to start, stop or restart a VM ---------------------------
# The container cannot reach midclt, so the dashboard drops the request here.
# Both the id and the action are validated against the installed VM list
# before anything runs: the queue is a file, and a file is not an argument.
if [ -f "$Q/_vm.req" ]; then
  VMREQ=$(cat "$Q/_vm.req" 2>/dev/null)
  rm -f "$Q/_vm.req"
  vnote(){ python3 - "$1" "$2" "$S" <<'PYV'
import json,sys,time
st,msg,path=sys.argv[1],sys.argv[2],sys.argv[3]
try: d=json.load(open(path))
except Exception: d={}
d["_vm"]={"state":st,"msg":msg,"t":int(time.time())}
json.dump(d,open(path,"w"))
PYV
  }
  PARSED=$(printf '%s' "$VMREQ" | python3 -c "
import json,sys
try: r=json.load(sys.stdin)
except Exception: raise SystemExit
a=str(r.get('action') or '')
if a not in ('start','stop','restart'): raise SystemExit
try: i=int(r.get('id'))
except Exception: raise SystemExit
print('%d %s' % (i, a))
" 2>/dev/null)

  if [ -z "$PARSED" ]; then
    vnote failed "that request did not make sense"
  else
    set -- $PARSED
    VMID="$1"; VMACT="$2"
    # the id has to be a VM that actually exists
    VMNAME=$(midclt call vm.query "[[\"id\",\"=\",$VMID]]" 2>/dev/null | python3 -c "
import json,sys
try: print(json.load(sys.stdin)[0]['name'])
except Exception: pass" 2>/dev/null)
    if [ -z "$VMNAME" ]; then
      vnote failed "no such VM"
    else
      vnote running "$VMACT $VMNAME"
      case "$VMACT" in
        start)   midclt call vm.start "$VMID" >/dev/null 2>&1 ;;
        stop)    timeout 120 midclt call -j vm.stop "$VMID" >/dev/null 2>&1 ;;
        restart)
          # vm.restart exists but has been unreliable here; stop, wait for it
          # to really be STOPPED, then start. Starting a VM that is still
          # shutting down leaves it off.
          timeout 120 midclt call -j vm.stop "$VMID" >/dev/null 2>&1
          i=0
          while [ $i -lt 25 ]; do
            st=$(midclt call vm.query "[[\"id\",\"=\",$VMID]]" 2>/dev/null | python3 -c "
import json,sys
try: print((json.load(sys.stdin)[0].get('status') or {}).get('state'))
except Exception: pass" 2>/dev/null)
            [ "$st" = "STOPPED" ] && break
            sleep 3; i=$((i+1))
          done
          midclt call vm.start "$VMID" >/dev/null 2>&1 ;;
      esac
      sleep 3
      NOW=$(midclt call vm.query "[[\"id\",\"=\",$VMID]]" 2>/dev/null | python3 -c "
import json,sys
try: print((json.load(sys.stdin)[0].get('status') or {}).get('state'))
except Exception: print('UNKNOWN')" 2>/dev/null)
      vnote done "$VMNAME is $NOW"
    fi
  fi
fi

# --- a request to update Overlook itself ---------------------------------------
# Two install shapes, and the update differs between them:
#   image  - the app runs a published container image, so pull a new one
#   files  - the app runs a stock python image with this directory mounted in,
#            so fetch the files from the repo
if [ -f "$Q/_self.req" ]; then
  rm -f "$Q/_self.req"
  APPNAME=$(basename "$BASE")
  REPO="${UPDATE_REPO:-Boldestfutura/dashy}"
  BRANCH="${UPDATE_BRANCH:-main}"
  BASEURL="https://raw.githubusercontent.com/$REPO/$BRANCH/app"
  note(){ python3 - "$1" "$2" "$S" "${3:-}" <<'PYN'
import json,sys,time
st,msg,path=sys.argv[1],sys.argv[2],sys.argv[3]
pct=sys.argv[4] if len(sys.argv)>4 else ""
try: d=json.load(open(path))
except Exception: d={}
row={"state":st,"msg":msg,"t":int(time.time())}
if pct.isdigit(): row["pct"]=int(pct)
d["_self"]=row
json.dump(d,open(path,"w"))
PYN
  }

  # Overlook is a published image, so an update is a pull. There used to be a
  # second path for a bind-mounted file install, which meant two ways to update
  # and a mode to get wrong; the installer only builds image installs now.
  APPNAME=$(basename -- "$BASE")
  IMAGE=$(midclt call app.config "$APPNAME" 2>/dev/null | python3 -c "
import json,sys
try: svc=list(json.load(sys.stdin)['services'].values())[0]
except Exception: raise SystemExit
print(svc.get('image') or '')" 2>/dev/null)

  case "$IMAGE" in
    */overlook:*|*/overlook)
      note running "taking a snapshot" 5
      stamp=$(date +%Y%m%d-%H%M%S)
      ds=${BASE#/mnt/}
      zfs list -H -o name "$ds" >/dev/null 2>&1 && \
        zfs snapshot -r "$ds@preupdate-$stamp" 2>/dev/null
      note running "pulling the new image" 10
      # midclt -j prints its own progress; read it as it goes and publish the
      # percentage, so the dashboard can say how far along it is rather than
      # leaving someone watching a word
      if midclt call -j app.pull_images "$APPNAME" '{"redeploy": true}' 2>&1 \
           | python3 -u -c "
import json, os, re, sys, time
S = sys.argv[1]
last, lastw = 0.0, 0.0
pat = re.compile(r'([0-9]{1,3}(?:\.[0-9]+)?)%')
while True:
    chunk = sys.stdin.read(256)
    if not chunk:
        break
    found = pat.findall(chunk)
    if not found:
        continue
    pct = float(found[-1])
    # the pull is the long middle of the job, so map it into 10-90
    pct = 10 + pct * 0.8
    now = time.time()
    if pct <= last or now - lastw < 1.0:
        continue
    last, lastw = pct, now
    try:
        d = json.load(open(S))
    except Exception:
        d = {}
    d['_self'] = {'state': 'running', 'msg': 'pulling the new image',
                  'pct': int(pct), 't': int(now)}
    tmp = S + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(d, fh)
    os.replace(tmp, S)
" "$S" >/dev/null 2>&1; then
        # the container's entrypoint copies the new cron scripts back out to
        # this directory as it starts, so cron picks them up too
        note done "updated"
      else
        note failed "could not pull the image"
      fi ;;
    *)
      note failed "this install does not run a Overlook image; update it by hand" ;;
  esac
fi

[ -d "$Q" ] || exit 0
set -- "$Q"/*.req
[ -e "$1" ] || exit 0

for f in "$Q"/*.req; do
  [ -e "$f" ] || continue
  app=$(basename "$f" .req)
  rm -f "$f"

  # _self and _serve are handled above, by name. If one turns up here it is a
  # request that arrived while its own block was still running, and it must
  # not be mistaken for an app: app.query would not find it and the loop would
  # record "no such app" against a key the dashboard reads as a failed update,
  # which then hides the real one.
  case "$app" in _*) continue ;; esac

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
