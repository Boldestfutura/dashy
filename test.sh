#!/usr/bin/env bash
# Checks whether this box can run Overlook, and whether an existing install is
# healthy. Safe to run at any time: it reads, it never changes anything.
#
#   ./test.sh              # check the default app name, 'overlook'
#   APP_NAME=dashboard ./test.sh
#
# Exit code is the number of failures, so CI can use it.
set -uo pipefail

APP_NAME="${APP_NAME:-dashy}"
PASS=0; FAIL=0; SKIP=0

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
warn() { printf '  \033[33m–\033[0m %s\n' "$*"; SKIP=$((SKIP+1)); }
head_() { printf '\n\033[1m%s\033[0m\n' "$*"; }

head_ "Can this box run Overlook?"
for c in midclt zfs python3 curl; do
  command -v "$c" >/dev/null && ok "$c present" || bad "$c missing"
done
midclt call system.info >/dev/null 2>&1 \
  && ok "midclt answers (you have the rights it needs)" \
  || bad "midclt call failed — run as root or truenas_admin"
python3 -c 'import json,urllib.request,http.server' 2>/dev/null \
  && ok "python standard library is enough (no pip needed)" \
  || bad "python3 is missing modules Overlook uses"

head_ "Tailscale Serve (optional)"
SERVE="${SERVE:-$(find /mnt -maxdepth 4 -name serve.json 2>/dev/null | head -1)}"
if [ -n "$SERVE" ] && [ -f "$SERVE" ]; then
  ok "serve.json found at $SERVE"
  if python3 -c "import json;json.load(open('$SERVE'))" 2>/dev/null; then
    ok "serve.json is valid JSON"
    python3 - "$SERVE" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
web, tcp = cfg.get("Web") or {}, cfg.get("TCP") or {}
if not web:
    print("  \033[33m\u2013\033[0m nothing published yet - Overlook will install for your LAN")
    sys.exit(0)
print("  \033[32m✓\033[0m %d service(s) published" % len(web))
host = next(iter(web)).rsplit(":", 1)[0]
print("  \033[32m✓\033[0m this machine is %s" % host)
# every Web host needs a matching TCP listener or the port never binds
missing = [h for h in web if h.rsplit(":", 1)[-1] not in tcp]
if missing:
    print("  \033[33m–\033[0m %d Web entr(ies) have no TCP listener: %s"
          % (len(missing), ", ".join(missing[:3])))
else:
    print("  \033[32m✓\033[0m every Web entry has a TCP listener")
PY
  else
    bad "serve.json is not valid JSON"
  fi
else
  warn "no Tailscale Serve config - Overlook will install for your LAN instead"
  warn "(app tiles will link to this NAS's LAN address rather than a tailnet name)"
fi

head_ "Is $APP_NAME installed?"
APPJSON=$(midclt call app.query "[[\"name\",\"=\",\"$APP_NAME\"]]" 2>/dev/null)
if ! printf '%s' "$APPJSON" | grep -q '"name"'; then
  warn "not installed yet — run ./install.sh"
  printf '\n%d passed, %d failed, %d skipped\n' "$PASS" "$FAIL" "$SKIP"
  exit "$FAIL"
fi

STATE=$(printf '%s' "$APPJSON" | python3 -c "
import json,sys
try: print(json.load(sys.stdin)[0]['state'])
except Exception: print('UNKNOWN')")
[ "$STATE" = RUNNING ] && ok "app state is RUNNING" || bad "app state is $STATE"

PORT=$(printf '%s' "$APPJSON" | python3 -c "
import json,sys
try:
    a=json.load(sys.stdin)[0]
    for p in (a.get('active_workloads') or {}).get('used_ports') or []:
        for hp in p.get('host_ports') or []:
            print(hp['host_port']); raise SystemExit
except Exception: pass")
# host networking publishes nothing, so fall back to the configured port
[ -z "$PORT" ] && PORT=$(midclt call app.config "$APP_NAME" 2>/dev/null | python3 -c "
import json,sys
try:
    c=json.load(sys.stdin)['services']
    print(list(c.values())[0].get('environment',{}).get('DASH_PORT',''))
except Exception: pass")

head_ "Does it answer?"
if [ -n "$PORT" ]; then
  ok "listening port is $PORT"
  for p in /version /api/stats /manifest.webmanifest; do
    code=$(curl -s -o /dev/null -m 8 -w '%{http_code}' "http://127.0.0.1:$PORT$p" 2>/dev/null)
    [ "$code" = 200 ] && ok "$p -> 200" || bad "$p -> ${code:-no answer}"
  done
  VER=$(curl -s -m 8 "http://127.0.0.1:$PORT/version" 2>/dev/null \
        | python3 -c "import json,sys;print(json.load(sys.stdin).get('version','?'))" 2>/dev/null)
  [ -n "$VER" ] && ok "reports version $VER"
else
  bad "could not work out which port it serves on"
fi

head_ "Data the container cannot collect itself"
BASE=$(midclt call app.config "$APP_NAME" 2>/dev/null | python3 -c "
import json,sys
try:
    s=list(json.load(sys.stdin)['services'].values())[0]
    for v in s.get('volumes',[]):
        if v.split(':')[1] == '/data': print(v.split(':')[0].rsplit('/',1)[0]); break
except Exception: pass")
if [ -n "$BASE" ] && [ -d "$BASE" ]; then
  ok "install lives at $BASE"
  for f in data/pools.tsv data/apps.json; do
    if [ -f "$BASE/$f" ]; then
      age=$(( $(date +%s) - $(stat -c %Y "$BASE/$f") ))
      if [ "$age" -lt 180 ]; then ok "$f is fresh (${age}s old)"
      else bad "$f is ${age}s old — is the collect.sh cron job running?"; fi
    else
      bad "$f missing — collect.sh has not run"
    fi
  done
  [ -d "$BASE/config" ] && ok "config dataset present (shared settings)" \
                        || warn "no config dataset — settings will not sync between devices"
  if [ -d "$BASE/config" ]; then
    touch "$BASE/config/.wtest" 2>/dev/null \
      && { ok "config is writable by this user"; rm -f "$BASE/config/.wtest"; } \
      || warn "config not writable by $(id -un) (the container may still own it)"
  fi
else
  warn "could not locate the install directory"
fi

head_ "Update check"
if [ -n "$BASE" ] && [ -f "$BASE/data/selfupdate.json" ]; then
  LATEST=$(python3 -c "
import json;print(json.load(open('$BASE/data/selfupdate.json')).get('latest',''))" 2>/dev/null)
  [ -n "$LATEST" ] && ok "repo says the current release is $LATEST" \
                   || bad "selfupdate.json is there but unreadable"
  if [ -n "$VER" ] && [ -n "$LATEST" ] && [ "$VER" != "$LATEST" ]; then
    warn "running $VER, repo has $LATEST - Update Overlook is waiting in the settings pane"
  fi
else
  warn "no update check yet (it runs every 30 minutes, give it that long)"
fi

head_ "Cron"
CRON=$(midclt call cronjob.query 2>/dev/null | python3 -c "
import json,sys
n=sys.argv[1]
c=[x for x in json.load(sys.stdin) if '/'+n+'/app/' in x.get('command','')]
print(len(c))
for x in c: print('   ', x.get('description') or x['command'], '| enabled:', x.get('enabled'))
" "$APP_NAME" 2>/dev/null)
NUM=$(printf '%s' "$CRON" | head -1)
if [ "${NUM:-0}" -ge 2 ]; then ok "$NUM cron jobs registered"
elif [ "${NUM:-0}" -ge 1 ]; then bad "only $NUM cron job — expected 2 (collect + worker)"
else bad "no cron jobs found for $APP_NAME"; fi
printf '%s' "$CRON" | tail -n +2

printf '\n\033[1m%d passed, %d failed, %d skipped\033[0m\n' "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" -eq 0 ] && printf 'Looks healthy.\n' || printf 'Something needs attention.\n'
exit "$FAIL"
