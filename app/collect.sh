#!/bin/sh
# Publishes facts the dashboard container cannot read for itself. Runs as root from cron.
# Paths derive from this script's own location, so the app works wherever the
# dataset lives: <base>/app/<this script>, <base>/data, <base>/queue.
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BASE=$(dirname -- "$APP_DIR")
D="$BASE/data"

# One run at a time. A run takes most of its minute (the power sampling below
# is 50 seconds of it), so anything slow - a disk that stalls smartctl, a busy
# middleware - would otherwise start the next run on top of this one, and the
# pile only grows. Skip a minute instead.
exec 9>"$D/.collect.lock"
flock -n 9 || exit 0

# --- pools: name, size, allocated, free, health, then usable used and avail.
# zpool list counts raw disk space, parity included, so on RAIDZ it overstates
# both used and free by the parity share (4-wide RAIDZ1: a third). The root
# dataset's used/avail from zfs list is what can actually be stored. Appended
# as extra columns so an older server still reads the first five.
if timeout 30 zpool list -Hp -o name,size,allocated,free,health > "$D/pools.tsv.zp" 2>/dev/null; then
  timeout 30 zfs list -Hp -d 0 -o name,used,avail > "$D/pools.tsv.zfs" 2>/dev/null
  awk -F'\t' -v Z="$D/pools.tsv.zfs" 'BEGIN{OFS="\t"
      while ((getline l < Z) > 0) { split(l, f, "\t"); u[f[1]] = f[2]; a[f[1]] = f[3] } }
    { if ($1 in u) print $0, u[$1], a[$1]; else print $0 }' \
    "$D/pools.tsv.zp" > "$D/pools.tsv.tmp" && mv "$D/pools.tsv.tmp" "$D/pools.tsv"
  rm -f "$D/pools.tsv.zp" "$D/pools.tsv.zfs"
  # The stamp is how the dashboard tells fresh pool data from old. It used to
  # be written at the end of every run whether or not zpool had answered, so a
  # pool command that hung or failed still looked current.
  date +%s > "$D/pools.stamp"
fi

# --- CPU temperatures, from whichever sensor driver this CPU has. coretemp is
# --- Intel; k10temp (or the out-of-tree zenpower) is AMD. Reading coretemp alone
# --- left every AMD box with no CPU temperature at all.
python3 - "$D" <<'PYTEMP'
import glob, os, sys
D = sys.argv[1]


def rd(p):
    try:
        with open(p) as fh:
            return fh.read().strip()
    except OSError:
        return ""


pkg, cores = None, []
for h in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
    drv = rd(h + "/name")
    if drv not in ("coretemp", "k10temp", "zenpower"):
        continue
    for lab in sorted(glob.glob(h + "/temp*_label"),
                      key=lambda p: int("".join(c for c in os.path.basename(p) if c.isdigit()) or 0)):
        name, val = rd(lab), rd(lab[:-len("_label")] + "_input")
        if not val.lstrip("-").isdigit():
            continue
        # the package figure: Intel's "Package id 0", AMD's Tctl (Tdie on
        # older kernels and zenpower, which is the truer one when present)
        if name.startswith("Package") or name in ("Tdie", "Tctl"):
            if pkg is None or name == "Tdie":
                pkg = val
        # per core on Intel, per chiplet (CCD) on AMD
        elif name.startswith("Core") or name.startswith("Tccd"):
            cores.append((name, val))
    if pkg is not None:
        break

if pkg is not None:
    with open(D + "/cputemp.tmp", "w") as fh:
        fh.write(pkg + "\n")
    os.replace(D + "/cputemp.tmp", D + "/cputemp")
with open(D + "/cores.tsv.tmp", "w") as fh:
    for name, val in cores:
        fh.write("%s\t%s\n" % (name, val))
os.replace(D + "/cores.tsv.tmp", D + "/cores.tsv")
PYTEMP

# --- this machine, as TrueNAS describes it: its name, its version and the
# --- hardware. Nothing about any particular NAS is known to the page; it is
# --- all read here, on whatever box this runs on.
timeout 30 midclt call system.info 2>/dev/null | python3 -c "
import json,re,sys
try: i=json.load(sys.stdin)
except Exception: raise SystemExit
def cpu(v):
    # '13th Gen Intel(R) Core(TM) i3-13100' -> 'Intel Core i3-13100'
    v=re.sub(r'\((?:R|TM|r|tm)\)','',str(v or ''))
    v=re.sub(r'\b\d+(?:st|nd|rd|th) Gen\b|\bCPU\b|@.*$|\b\d+-Core Processor\b','',v)
    return ' '.join(v.split())
def clean(v):
    v=str(v or '').strip()
    # boards with blank DMI fields report placeholder strings
    return '' if v.lower() in ('default string','to be filled by o.e.m.','system product name',
                               'system manufacturer','not specified','none') else v
json.dump({'hostname': clean(i.get('hostname')),
           'version': clean(i.get('version')),
           'cpu': cpu(clean(i.get('model'))),
           'cores': i.get('physical_cores'),
           'threads': i.get('cores'),
           'memory': i.get('physmem'),
           'ecc': i.get('ecc_memory'),
           'maker': clean(i.get('system_manufacturer')),
           'product': clean(i.get('system_product')),
           'timezone': clean(i.get('timezone')),
           # which installed app is this dashboard: the dataset is named after it
           'app': sys.argv[1]}, sys.stdout)
" "$(basename -- "$BASE")" > "$D/system.json.tmp" 2>/dev/null && [ -s "$D/system.json.tmp" ] \
  && mv "$D/system.json.tmp" "$D/system.json" || rm -f "$D/system.json.tmp"

# --- boot/shutdown history, for the uptime record
last -x --time-format=iso reboot shutdown 2>/dev/null | head -60 > "$D/boots.txt.tmp" \
  && mv "$D/boots.txt.tmp" "$D/boots.txt"

# --- drives: one JSON object per line
: > "$D/drives.jsonl.tmp"
for dev in $(lsblk -dno NAME 2>/dev/null | grep -Ev '^(loop|zd|sr)'); do
  # a failing disk is exactly the one that stalls smartctl, so bound it
  timeout 30 smartctl -j -H -A -i "/dev/$dev" 2>/dev/null | python3 -c "
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
# --- and what TrueNAS is doing to each one right now, so a tile can say
# --- 'Removing' or 'Stopping' instead of calling a deliberate act an outage
timeout 30 midclt call core.get_jobs '[["state","=","RUNNING"],["method","in",["app.delete","app.stop","app.start","app.redeploy"]]]' \
  > "$D/appjobs.tmp" 2>/dev/null || echo '[]' > "$D/appjobs.tmp"
timeout 60 midclt call app.query 2>/dev/null | python3 -c "
import json,sys
try: apps=json.load(sys.stdin)
except Exception: raise SystemExit
VERB={'app.delete':'removing','app.stop':'stopping','app.start':'starting','app.redeploy':'starting'}
busy={}
try:
    for j in json.load(open(sys.argv[1])):
        args=j.get('arguments') or []
        if args and isinstance(args[0],str): busy[args[0]]=VERB.get(j.get('method'))
except Exception: pass
out=[]
for a in apps:
    ports=set(); bind={}
    for w in (a.get('active_workloads') or {}).get('used_ports') or []:
        for hp in w.get('host_ports') or []:
            ip=hp.get('host_ip'); port=hp.get('host_port')
            if not port: continue
            ports.add(port)
            # published on one address only, so it has to be reached there
            if ip and ip not in ('0.0.0.0','::'): bind[str(port)]=ip
    md=a.get('metadata') or {}
    # a custom app carries the catalogue's placeholder ('Custom App', 'This is a
    # custom app where...'), which says nothing about what it actually is
    if a.get('custom_app'): md=dict(md, title='', description='')
    # and HexOS's app calls itself 'iX App', which names the publisher, not the app
    if str(md.get('title') or '').strip().lower()=='ix app': md=dict(md, title='')
    desc=' '.join(str(md.get('description') or '').split())
    # the catalogue's first sentence is the useful part
    desc=desc.split('. ')[0].rstrip('.')
    out.append({'name':a.get('name'),'state':a.get('state'),'job':busy.get(a.get('name')),
                # the catalogue's own name and blurb; custom apps have neither
                'title':str(md.get('title') or ''),
                'desc':desc[:90],
                'ports':sorted(p for p in ports if p),
                'bind':bind,
                'version':a.get('human_version'),
                'custom':bool(a.get('custom_app')),
                'icon':md.get('icon') or md.get('icon_url') or '',
                # TrueNAS's own answer to 'what URL opens this app', path included
                'portals':a.get('portals') or {},
                'update':bool(a.get('upgrade_available')) or bool(a.get('image_updates_available'))})
json.dump(out,sys.stdout)
" "$D/appjobs.tmp" > "$D/apps.json.tmp" 2>/dev/null && mv "$D/apps.json.tmp" "$D/apps.json"
rm -f "$D/appjobs.tmp"

# --- live tiles: where Plex is, and its token. The token lives in Plex's own
# --- Preferences.xml, which only root can read; it is handed to the container in
# --- a file owned root:apps 0640, so it never reaches a browser or the apps list.
# --- Plex is found by what it is, not by name, and its config folder by the mount
# --- Plex itself reads, so this works for any Plex install. Every 30 minutes is
# --- plenty: the token only changes if the server is signed out and back in.
if [ ! -s "$D/plex.json" ] || [ -n "$(find "$D/plex.json" -mmin +30 2>/dev/null)" ]; then
  timeout 60 midclt call app.query 2>/dev/null | python3 -c "
import json,os,re,sys
D=sys.argv[1]
try: apps=json.load(sys.stdin)
except Exception: raise SystemExit
PREF='Library/Application Support/Plex Media Server/Preferences.xml'
found=None
for a in apps:
    md=a.get('metadata') or {}
    if 'plex' not in (str(a.get('name'))+' '+str(md.get('title'))).lower(): continue
    if a.get('state')!='RUNNING': continue
    wl=a.get('active_workloads') or {}
    for c in wl.get('container_details') or []:
        for m in c.get('volume_mounts') or []:
            p=os.path.join(m.get('source') or '',PREF)
            if os.path.isfile(p):
                ports=[hp.get('host_port') for w in wl.get('used_ports') or [] for hp in w.get('host_ports') or []]
                # host-networked Plex publishes nothing, but always listens on 32400
                port=32400 if (32400 in ports or not ports) else ports[0]
                found=(a.get('name'),p,port)
    if found: break
tmp=os.path.join(D,'plex.json.tmp')
if not found:
    try: os.remove(os.path.join(D,'plex.json'))
    except OSError: pass
    raise SystemExit
m=re.search(r'PlexOnlineToken=\"([^\"]+)\"',open(found[1]).read())
if not m: raise SystemExit
fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o640)
with os.fdopen(fd,'w') as fh: json.dump({'app':found[0],'port':found[2],'token':m.group(1)},fh)
os.chown(tmp,0,568); os.chmod(tmp,0o640)
os.replace(tmp,os.path.join(D,'plex.json'))
" "$D" 2>/dev/null
fi

# --- Pi-hole 6 answers its stats API only after a login, and the catalogue app keeps
# --- the web password in its own config. Read it here, as root, so the container
# --- never needs midclt. Every 30 minutes picks up a changed password.
if [ ! -s "$D/pihole.json" ] || [ -n "$(find "$D/pihole.json" -mmin +30 2>/dev/null)" ]; then
  timeout 60 midclt call app.query 2>/dev/null | python3 -c "
import json,os,re,subprocess,sys
D=sys.argv[1]
try: apps=json.load(sys.stdin)
except Exception: raise SystemExit
def find(d,key):
    if isinstance(d,dict):
        for k,v in d.items():
            if k==key and isinstance(v,str) and v: return v
            r=find(v,key)
            if r: return r
    return None
found=None
for a in apps:
    md=a.get('metadata') or {}
    if not re.search(r'pi-?hole',(str(a.get('name'))+' '+str(md.get('title'))).lower()): continue
    if a.get('state')!='RUNNING': continue
    try: cfg=json.loads(subprocess.run(['midclt','call','app.config',a.get('name')],capture_output=True,timeout=30).stdout)
    except Exception: continue
    pw=find(cfg,'web_password')
    if not pw: continue
    port=((cfg.get('network') or {}).get('web_port') or {}).get('port_number')
    if not port:
        # otherwise whatever is published for the container's port 80; host networking means 80
        wl=a.get('active_workloads') or {}
        port=next((hp.get('host_port') for w in wl.get('used_ports') or [] if w.get('container_port')==80
                   for hp in w.get('host_ports') or []),80)
    found=(a.get('name'),port,pw)
    break
tmp=os.path.join(D,'pihole.json.tmp')
if not found:
    try: os.remove(os.path.join(D,'pihole.json'))
    except OSError: pass
    raise SystemExit
fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o640)
with os.fdopen(fd,'w') as fh: json.dump({'app':found[0],'port':found[1],'password':found[2]},fh)
os.chown(tmp,0,568); os.chmod(tmp,0o640)
os.replace(tmp,os.path.join(D,'pihole.json'))
" "$D" 2>/dev/null
fi

# --- API keys for the live tiles of apps that need one (Immich, Audiobookshelf,
# --- SparkyFitness). The owner keeps them in keys.json beside the data folder, where
# --- the container cannot see it; this copies it in, readable by the app alone.
K="$(dirname "$D")/keys.json"
if [ -s "$K" ]; then
  if ! cmp -s "$K" "$D/keys.json" 2>/dev/null; then
    install -m 640 -o 0 -g 568 "$K" "$D/keys.json.tmp" 2>/dev/null && mv "$D/keys.json.tmp" "$D/keys.json"
  fi
else
  rm -f "$D/keys.json"
fi

# --- cache app icons locally, so the browser never calls out to the internet
# --- and the grid still works with no WAN. Refresh when TrueNAS metadata changes;
# --- icon managers store custom images as data URIs rather than remote URLs.
mkdir -p "$D/icons" 2>/dev/null
python3 - "$D" <<'PYICON' 2>/dev/null
import base64, hashlib, json, os, re, sys, urllib.parse, urllib.request
d = sys.argv[1]
icons = os.path.join(d, "icons")
ext_for = {"image/svg+xml": "svg", "image/png": "png",
           "image/webp": "webp", "image/jpeg": "jpg"}
allowed = set(ext_for.values())
try:
    apps = json.load(open(os.path.join(d, "apps.json")))
except Exception:
    raise SystemExit
for a in apps:
    name, source = a.get("name"), a.get("icon") or ""
    if not isinstance(name, str) or not re.match(r"^[a-z0-9][a-z0-9._-]{0,48}$", name):
        continue
    if not isinstance(source, str) or not source:
        continue
    try:
        signature = hashlib.sha256(source.encode("utf-8")).hexdigest()
        sig_path = os.path.join(icons, name + ".source")
        if os.path.isfile(sig_path) and open(sig_path).read().strip() == signature:
            if any(os.path.isfile(os.path.join(icons, name + "." + ext)) for ext in allowed):
                continue

        if source.startswith("data:"):
            header, payload = source.split(",", 1)
            bits = header[5:].split(";")
            mime = bits[0].lower()
            if mime not in ext_for:
                continue
            blob = (base64.b64decode(payload, validate=True) if "base64" in bits[1:]
                    else urllib.parse.unquote_to_bytes(payload))
            ext = ext_for[mime]
        else:
            parsed = urllib.parse.urlsplit(source)
            if parsed.scheme not in ("http", "https"):
                continue
            suffix = parsed.path.rsplit(".", 1)[-1].lower() if "." in parsed.path else ""
            ext = "jpg" if suffix == "jpeg" else suffix
            req = urllib.request.Request(source, headers={"User-Agent": "Dashy"})
            with urllib.request.urlopen(req, timeout=10) as r:
                blob = r.read(512 * 1024 + 1)
                mime_ext = ext_for.get(r.headers.get_content_type())
            if ext not in allowed:
                ext = mime_ext
            if ext not in allowed:
                continue

        if not blob or len(blob) > 512 * 1024:
            continue
        target = os.path.join(icons, name + "." + ext)
        tmp = target + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, target)
        for old in os.listdir(icons):
            if old.startswith(name + ".") and old.rsplit(".", 1)[-1] in allowed and old != os.path.basename(target):
                os.unlink(os.path.join(icons, old))
        with open(sig_path + ".tmp", "w") as fh:
            fh.write(signature + "\n")
        os.replace(sig_path + ".tmp", sig_path)
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

# --- per-app usage, for the app sheet. Each container is a cgroup, and its
# --- cgroup files say what the app itself is using: CPU time, memory (less the
# --- page cache it can drop, as docker stats counts it), bytes read and written.
# --- Network comes from the container's own namespace; an app on the host
# --- network has no figure of its own, and says so rather than borrowing the NAS's.
def containers():
    seen = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cgroup" % pid) as f:
                line = [l for l in f.read().splitlines() if l.startswith("0::")]
        except Exception:
            continue
        if not line:
            continue
        path = line[0][3:]
        tail = path.rsplit("/", 1)[-1]
        cid = tail[7:-6] if tail.startswith("docker-") and tail.endswith(".scope") else (
            tail if "/docker/" in path else "")
        if len(cid) >= 12 and cid not in seen:
            seen[cid] = (path, pid)
    return seen


def cgread(path, pid, hostns):
    base = "/sys/fs/cgroup" + path
    r = {"cpu": 0, "mem": 0, "rd": 0, "wr": 0, "ns": None, "rx": 0, "tx": 0}
    try:
        with open(base + "/cpu.stat") as f:
            for l in f:
                if l.startswith("usage_usec "):
                    r["cpu"] = int(l.split()[1])
        with open(base + "/memory.current") as f:
            r["mem"] = int(f.read().strip() or 0)
        with open(base + "/memory.stat") as f:
            for l in f:
                if l.startswith("inactive_file "):
                    r["mem"] = max(0, r["mem"] - int(l.split()[1]))
        with open(base + "/io.stat") as f:
            for l in f:
                for part in l.split()[1:]:
                    k, _, v = part.partition("=")
                    if k == "rbytes":
                        r["rd"] += int(v)
                    elif k == "wbytes":
                        r["wr"] += int(v)
    except Exception:
        return None
    try:
        ns = os.readlink("/proc/%s/ns/net" % pid)
        if ns != hostns:
            r["ns"] = ns
            with open("/proc/%s/net/dev" % pid) as f:
                for l in f.read().splitlines()[2:]:
                    name, _, rest = l.partition(":")
                    if name.strip() == "lo":
                        continue
                    v = rest.split()
                    r["rx"] += int(v[0])
                    r["tx"] += int(v[8])
    except Exception:
        pass
    return r


try:
    hostns = os.readlink("/proc/1/ns/net")
except Exception:
    hostns = ""
ctrs = containers()
c0, t0 = {cid: cgread(p, pid, hostns) for cid, (p, pid) in ctrs.items()}, time.monotonic()
a = sample()
time.sleep(2)
b = sample()
c1, t1 = {cid: cgread(p, pid, hostns) for cid, (p, pid) in ctrs.items()}, time.monotonic()

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

dt = max(t1 - t0, 0.5)
ncpu = os.cpu_count() or 1
apps, nsseen = {}, set()
for cid, (path, pid) in ctrs.items():
    x, y = c0.get(cid), c1.get(cid)
    if not x or not y:
        continue
    # the pid stands in for its container; appname maps it via the compose label
    name = appname(pid, cache, root)
    if not name:
        continue
    m = apps.setdefault(name, {"cpu": 0.0, "mem": 0, "rd": 0.0, "wr": 0.0,
                               "rx": 0.0, "tx": 0.0, "net": False})
    m["cpu"] += max(0, y["cpu"] - x["cpu"]) / (dt * 1e6 * ncpu) * 100
    m["mem"] += y["mem"]
    m["rd"] += max(0, y["rd"] - x["rd"]) / dt
    m["wr"] += max(0, y["wr"] - x["wr"]) / dt
    # containers in one compose project can share a namespace; count it once
    if y["ns"] and y["ns"] not in nsseen:
        nsseen.add(y["ns"])
        m["net"] = True
        m["rx"] += max(0, y["rx"] - x["rx"]) / dt
        m["tx"] += max(0, y["tx"] - x["tx"]) / dt
now = int(time.time())
for m in apps.values():
    for k in ("cpu",):
        m[k] = round(m[k], 2)
    for k in ("rd", "wr", "rx", "tx"):
        m[k] = round(m[k])
    if not m["net"]:
        m["rx"] = m["tx"] = None
    del m["net"]
# an hour of history at the collector's minute, carried over from the last run
try:
    with open(D + "/appstats.json") as f:
        hist = json.load(f).get("hist") or {}
except Exception:
    hist = {}
for name, m in apps.items():
    h = [r for r in (hist.get(name) or []) if isinstance(r, list) and now - r[0] < 3700]
    h.append([now, m["cpu"], m["mem"], m["rd"] + m["wr"],
              None if m["rx"] is None else m["rx"] + m["tx"]])
    hist[name] = h[-60:]
hist = {k: v for k, v in hist.items() if k in apps}
tmp = D + "/appstats.json.tmp"
with open(tmp, "w") as f:
    json.dump({"at": now, "apps": apps, "hist": hist}, f)
os.replace(tmp, D + "/appstats.json")
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

# A USB device passed through by vendor and product id has no "device" name at
# all, which the dashboard printed as "null". Name it the way TrueNAS's own
# passthrough list does, from the same ids.
names = {}
if any((d.get("attributes") or {}).get("dtype") == "USB" or d.get("dtype") == "USB"
       for v in (vms if isinstance(vms, list) else []) for d in (v.get("devices") or [])):
    try:
        ch = json.loads(subprocess.run(["midclt", "call", "vm.device.usb_passthrough_choices"],
                                       capture_output=True, text=True, timeout=60).stdout)
        for key, c in (ch or {}).items():
            cap = c.get("capability") or {}
            label = " ".join(x for x in (cap.get("vendor"), cap.get("product")) if x)
            names[(cap.get("vendor_id"), cap.get("product_id"))] = label
            names[key] = label
    except Exception:
        pass


def usb_label(a):
    ids = a.get("usb") or {}
    vid, pid = ids.get("vendor_id"), ids.get("product_id")
    dev = a.get("device")
    return (names.get((vid, pid)) or (dev and names.get(dev)) or dev
            or ("%s:%s" % (vid, pid) if vid and pid else "USB device"))


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
        # TrueNAS keeps no record of the guest OS. The install ISO and disk
        # names ('haos_ova-16.2.qcow2', 'ubuntu-24.04-live-server.iso') are
        # the best hint there is, so the page can put a logo on the card.
        "media": sorted({os.path.basename(str((d.get("attributes") or {}).get("path") or ""))
                         for d in devs
                         if (d.get("dtype") or (d.get("attributes") or {}).get("dtype"))
                         in ("CDROM", "DISK", "RAW")} - {""}),
        "usb": [usb_label(d.get("attributes") or {})
                for d in devs
                if (d.get("dtype") or (d.get("attributes") or {}).get("dtype")) == "USB"],
    })

out.sort(key=lambda x: (x.get("name") or "").lower())
tmp = D + "/vms.json.tmp"
with open(tmp, "w") as f:
    json.dump(out, f)
os.replace(tmp, D + "/vms.json")
PYVM

# --- VM console previews. libvirt answers root only, so the screenshots are
# --- taken here: every 10s through the minute, in the background so the power
# --- sampling below is not held up. Its own lock stops two minutes overlapping;
# --- fd 9 is closed so it never holds the collector's.
(
  flock -n 8 || exit 0
  python3 - "$D" <<'PYSHOT'
import json, os, struct, subprocess, sys, time, zlib

D = sys.argv[1]
URI = "qemu+unix:///system?socket=/run/truenas_libvirt/libvirt-sock"
MAXW = 960


def virsh(*a):
    return subprocess.run(["virsh", "-c", URI] + list(a), capture_output=True, timeout=20)


def ppm_png(raw):
    # virsh writes binary PPM (P6). Header: magic, width, height, maxval, each
    # separated by whitespace, comments allowed.
    tok, i = [], 0
    while len(tok) < 4:
        while raw[i:i + 1].isspace():
            i += 1
        if raw[i:i + 1] == b"#":
            i = raw.index(b"\n", i) + 1
            continue
        j = i
        while not raw[j:j + 1].isspace():
            j += 1
        tok.append(raw[i:j])
        i = j
    if tok[0] != b"P6" or tok[3] != b"255":
        return None
    w, h = int(tok[1]), int(tok[2])
    px = raw[i + 1:i + 1 + w * h * 3]
    step = -(-w // MAXW)            # whole-number downscale, nearest pixel
    ow, oh = len(range(0, w, step)), len(range(0, h, step))
    rows = []
    for y in range(0, h, step):
        r = px[y * w * 3:(y + 1) * w * 3]
        if step > 1:
            r = b"".join(r[x:x + 3] for x in range(0, w * 3, step * 3))
        rows.append(b"\0" + r)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", ow, oh, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) + chunk(b"IEND", b""))


def shoot():
    try:
        vms = json.load(open(D + "/vms.json"))
    except Exception:
        vms = []
    try:
        doms = virsh("list", "--name").stdout.decode().split()
    except Exception:
        doms = []
    keep = set()
    for v in vms:
        if (v.get("state") or "").upper() != "RUNNING":
            continue
        # TrueNAS names the libvirt domain "<id>_<name>"; match the id, not
        # the name, which TrueNAS may have mangled
        dom = next((d for d in doms if d.split("_", 1)[0] == str(v.get("id"))), None)
        if not dom:
            continue
        ppm = D + "/.vmshot.ppm"
        try:
            if virsh("screenshot", dom, ppm).returncode != 0:
                continue
            png = ppm_png(open(ppm, "rb").read())
        except Exception:
            png = None
        finally:
            try:
                os.unlink(ppm)
            except OSError:
                pass
        if not png:
            continue
        out = "%s/vmshot-%s.png" % (D, v.get("id"))
        with open(out + ".tmp", "wb") as f:
            f.write(png)
        os.chmod(out + ".tmp", 0o644)
        os.replace(out + ".tmp", out)
        keep.add(os.path.basename(out))
    # a stopped VM's last frame is not a preview of anything
    for n in os.listdir(D):
        if n.startswith("vmshot-") and n.endswith(".png") and n not in keep:
            try:
                os.unlink(os.path.join(D, n))
            except OSError:
                pass


for k in range(5):
    t = time.time()
    shoot()
    if k < 4:
        time.sleep(max(0, 10 - (time.time() - t)))
PYSHOT
) 8>"$D/.vmshot.lock" 9>&- &

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
APPNAME=$(basename -- "$BASE")

# One read of the app's own config gives both the image, which says how this
# install is packaged, and the port, which says where to ask it its version.
CFG=$(timeout 30 midclt call app.config "$APPNAME" 2>/dev/null)
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

# Half an hour between checks is fine right up until Dashy itself is updated,
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
    # Both names. The image moved from overlook to dashy, and matching only the
    # old one sent every install to GitHub instead, which offers a version a
    # minute or more before CI has built an image of it to install.
    ghcr.io/*/overlook:*|ghcr.io/*/overlook|ghcr.io/*/dashy:*|ghcr.io/*/dashy)
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

  # Dashy and the TrueNAS Apps page were on two independent half-hour timers
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
# --- half-hour cadence. This is what keeps both Dashy's own update markers and
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
