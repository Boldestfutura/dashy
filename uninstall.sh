#!/usr/bin/env bash
# Removes Overlook: the app, its cron jobs and its Serve entry.
# The datasets are left alone - deleting data is your call, not the script's.
set -uo pipefail
APP_NAME="${APP_NAME:-overlook}"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }

command -v midclt >/dev/null || { echo "midclt not found"; exit 1; }

say "stopping and deleting the app"
midclt call -j app.delete "$APP_NAME" >/dev/null 2>&1 \
  && info "app '$APP_NAME' removed" || info "no app called '$APP_NAME'"

say "removing the cron jobs"
midclt call cronjob.query 2>/dev/null | python3 -c "
import json,subprocess,sys
name=sys.argv[1]
for c in json.load(sys.stdin):
    if '/'+name+'/app/' in c.get('command',''):
        subprocess.run(['midclt','call','cronjob.delete',str(c['id'])],
                       capture_output=True)
        print('   removed cron: '+(c.get('description') or str(c['id'])))
" "$APP_NAME"

say "removing the Serve entry"
SERVE="${SERVE:-}"
if [ -z "$SERVE" ]; then
  SERVE=$(find /mnt -maxdepth 4 -name serve.json 2>/dev/null | head -1)
fi
if [ -n "$SERVE" ] && [ -f "$SERVE" ]; then
  python3 - "$SERVE" "$APP_NAME" <<'PY'
import json, shutil, sys, time
path, name = sys.argv[1], sys.argv[2]
cfg = json.load(open(path))
# find the Web entry pointing at whatever local port this app was using
import re, subprocess
out = subprocess.run(["midclt","call","app.query"], capture_output=True, text=True).stdout
ports = set()
try:
    for a in json.loads(out or "[]"):
        if a.get("name") == name:
            for p in (a.get("active_workloads") or {}).get("used_ports") or []:
                for hp in p.get("host_ports") or []:
                    ports.add(str(hp.get("host_port")))
except Exception:
    pass
removed = []
for host, entry in list((cfg.get("Web") or {}).items()):
    tgt = ((entry.get("Handlers") or {}).get("/") or {}).get("Proxy", "")
    if ports and tgt.rsplit(":", 1)[-1] in ports:
        cfg["Web"].pop(host)
        tport = host.rsplit(":", 1)[-1]
        (cfg.get("TCP") or {}).pop(tport, None)
        removed.append(host)
if removed:
    shutil.copy2(path, path + ".bak-%d" % int(time.time()))
    json.dump(cfg, open(path, "w"), indent=1)
    print("   removed: " + ", ".join(removed))
else:
    print("   nothing matched in " + path + " - remove it by hand if needed")
PY
else
  info "no serve.json found - remove the entry by hand if there is one"
fi

say "done"
info "The datasets were left in place. Delete them yourself when you are sure:"
info "  zfs list -r <pool>/Applications/$APP_NAME"
