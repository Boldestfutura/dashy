#!/usr/bin/env python3
"""
Set up Tailscale Serve for this NAS, so Dashy can be published on the tailnet.

On a normal host you would run `tailscale serve ...` and Tailscale keeps its own
state. The TrueNAS app is different: it reads a declarative JSON file handed to
it as TS_SERVE_CONFIG, and it never writes that file. Somebody has to create it,
mount it into the Tailscale container and point the app at it. That is four
fiddly steps with four ways to get it subtly wrong, which is what this does
instead.

Called two ways, both as root:
  - by install.sh, from the clone, at install time
  - by worker.sh, from the app directory, when the dashboard asks for it

Run with --dry-run to see every change without making any.

  serve_setup.py --host nas.tail1234.ts.net --tport 8455 --lport 30802 \
                 --serve /mnt/tank/appconfig/serve.json [--dry-run]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

TS_APP = "tailscale"
MOUNT = "/serve"


def mid(*a, job=False):
    cmd = ["midclt", "call"] + (["-j"] if job else []) + list(a)
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, (r.stdout or r.stderr).strip()


def die(msg):
    print("error: " + msg, file=sys.stderr)
    raise SystemExit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True,
                    help="this machine's tailnet name, or ${TS_CERT_DOMAIN}")
    ap.add_argument("--tport", type=int, required=True)
    ap.add_argument("--lport", type=int, required=True)
    ap.add_argument("--serve", required=True, help="host path for serve.json")
    ap.add_argument("--app", default=TS_APP, help="name of the Tailscale app")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    dry = a.dry_run
    tag = "would " if dry else ""

    # ---------------------------------------------------------- the app
    rc, out = mid("app.query", json.dumps([["name", "=", a.app]]))
    if rc != 0 or not json.loads(out or "[]"):
        die("no app called %r is installed. Install Tailscale first." % a.app)
    rc, out = mid("app.config", a.app)
    if rc != 0:
        die("could not read the Tailscale app config: " + out[:200])
    cfg = json.loads(out)
    if "tailscale" not in cfg or "storage" not in cfg:
        die("the Tailscale app config is not the shape this expects; "
            "set Serve up by hand rather than let this guess.")

    # ------------------------------------------------- where serve.json goes
    # Work out where it should live before writing anything. If the Tailscale
    # app is already pointed at a file, that file is the answer and --serve is
    # only a preference - adding a second mount at the same mount_path would
    # break the mount that is already working.
    ts = cfg["tailscale"]
    store = cfg["storage"]
    extra = store.setdefault("additional_storage", [])
    envs = ts.setdefault("additional_envs", [])
    cur_env = next((e for e in envs if e.get("name") == "TS_SERVE_CONFIG"), None)

    def host_of(inside_path):
        """Translate a path inside the container back to a host path."""
        for e in extra:
            mp = (e.get("mount_path") or "").rstrip("/")
            hp = (e.get("host_path_config") or {}).get("path")
            if mp and hp and (inside_path == mp or inside_path.startswith(mp + "/")):
                return os.path.join(hp, os.path.relpath(inside_path, mp))
        return None

    serve_path, inside, changed = None, None, False

    if cur_env and cur_env.get("value"):
        hp = host_of(cur_env["value"])
        if hp:
            serve_path, inside = hp, cur_env["value"]
            print("   Tailscale already reads %s (host %s)" % (inside, hp))
        else:
            print("   TS_SERVE_CONFIG is %s but nothing is mounted there"
                  % cur_env["value"])

    if serve_path is None:
        want_dir = os.path.dirname(os.path.abspath(a.serve))
        reuse = None
        for e in extra:
            hp = (e.get("host_path_config") or {}).get("path")
            if not hp:
                continue
            if want_dir == hp or want_dir.startswith(hp.rstrip("/") + "/"):
                reuse = e
                break
        taken = {(e.get("mount_path") or "").rstrip("/") for e in extra}
        if reuse:
            hp = reuse["host_path_config"]["path"]
            inside = os.path.join(reuse["mount_path"].rstrip("/"),
                                  os.path.relpath(a.serve, hp))
            serve_path = os.path.abspath(a.serve)
            print("   %s is already mounted at %s" % (hp, reuse["mount_path"]))
        else:
            mount = MOUNT
            n = 2
            while mount in taken:          # do not collide with an existing mount
                mount = "%s%d" % (MOUNT, n)
                n += 1
            serve_path = os.path.abspath(a.serve)
            inside = os.path.join(mount, os.path.basename(serve_path))
            print("%smount %s -> %s in the Tailscale container"
                  % (tag, want_dir, mount))
            extra.append({"type": "host_path", "read_only": True,
                          "mount_path": mount,
                          "host_path_config": {"path": want_dir, "acl_enable": False}})
            changed = True

    # ---------------------------------------------------------- the env var
    if cur_env and cur_env.get("value") == inside:
        print("   TS_SERVE_CONFIG is already %s" % inside)
    elif cur_env:
        print("%schange TS_SERVE_CONFIG from %s to %s"
              % (tag, cur_env.get("value"), inside))
        cur_env["value"] = inside
        changed = True
    else:
        print("%sset TS_SERVE_CONFIG=%s" % (tag, inside))
        envs.append({"name": "TS_SERVE_CONFIG", "value": inside})
        changed = True

    # ---------------------------------------------------------- serve.json
    sdir = os.path.dirname(serve_path)
    if not os.path.isdir(sdir):
        print("%screate %s" % (tag, sdir))
        if not dry:
            os.makedirs(sdir, exist_ok=True)
    try:
        with open(serve_path) as fh:
            serve = json.load(fh)
        print("%sedit %s" % (tag, serve_path))
    except (OSError, ValueError):
        serve = {}
        print("%screate %s" % (tag, serve_path))

    # BOTH halves, every time. A Web handler with no TCP listener is valid JSON
    # that silently never binds, and that one costs an afternoon to find.
    before = json.dumps(serve, sort_keys=True)
    serve.setdefault("TCP", {})[str(a.tport)] = {"HTTPS": True}
    serve.setdefault("Web", {})["%s:%d" % (a.host, a.tport)] = {
        "Handlers": {"/": {"Proxy": "http://127.0.0.1:%d" % a.lport}}}
    print("   TCP %d  HTTPS" % a.tport)
    print("   Web %s:%d -> http://127.0.0.1:%d" % (a.host, a.tport, a.lport))
    serve_changed = json.dumps(serve, sort_keys=True) != before
    if not serve_changed:
        print("   that entry is already there, byte for byte")
    else:
        # back up only when about to write, so a no-op run leaves no litter
        if os.path.exists(serve_path):
            bak = serve_path + ".bak-%d" % int(time.time())
            print("%sback it up to %s" % (tag, bak))
            if not dry:
                shutil.copy2(serve_path, bak)
        if not dry:
            with open(serve_path, "w") as fh:
                json.dump(serve, fh, indent=1)

    # ---------------------------------------------------------- apply
    if not changed:
        print("the Tailscale app already points at this file; nothing to change there.")
    if dry:
        print("\n--dry-run: nothing was changed.")
        return
    if not changed and not serve_changed:
        # Restarting Tailscale drops every tailnet connection on this box for a
        # few seconds. Not worth doing to achieve nothing.
        print("nothing to do - Serve is already set up this way.")
        return

    if changed:
        payload = {"values": {"tailscale": ts, "storage": store}}
        bak = "/tmp/tailscale-config-before-serve-%d.json" % int(time.time())
        with open(bak, "w") as fh:
            json.dump(cfg, fh, indent=1)
        print("Tailscale app config backed up to %s" % bak)
        rc, out = mid("app.update", a.app, json.dumps(payload), job=True)
        if rc != 0:
            die("app.update on %r failed, nothing else was changed: %s" % (a.app, out[:300]))
        print("Tailscale app updated")

    # Tailscale reads TS_SERVE_CONFIG at start, so a changed file needs a restart
    rc, out = mid("app.redeploy", a.app, job=True)
    print("Tailscale redeploy rc=%s" % rc)
    print("\nServe is configured. Two things it cannot do for you:")
    print("  - HTTPS certificates must be enabled for your tailnet")
    print("    (admin console -> DNS -> HTTPS Certificates)")
    print("  - the first certificate can take a minute to issue")


if __name__ == "__main__":
    main()
