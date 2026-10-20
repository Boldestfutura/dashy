#!/bin/sh
# collect.sh and worker.sh do not run in here. They run on the host, as root,
# from cron, because the container can reach neither zpool, smartctl, the raw
# devices nor midclt. But they ship in this image so that there is one version
# of Dashy rather than two, and an image update has to carry them along or they
# quietly rot at whatever version they were installed at.
#
# So: on every start, copy them out to the host's app directory, which is
# mounted at /hostapp. By rename, because worker.sh may be running right now -
# the swap leaves the running shell reading the old inode.
if [ -d /hostapp ]; then
  for f in collect.sh worker.sh serve_setup.py; do
    if ! cmp -s "/app/$f" "/hostapp/$f" 2>/dev/null; then
      if cp "/app/$f" "/hostapp/$f.new" 2>/dev/null; then
        chmod +x "/hostapp/$f.new" 2>/dev/null
        mv "/hostapp/$f.new" "/hostapp/$f" 2>/dev/null \
          && echo "entrypoint: refreshed $f on the host" \
          || rm -f "/hostapp/$f.new"
      else
        echo "entrypoint: cannot write /hostapp/$f - is it mounted read-write?" >&2
      fi
    fi
  done
fi

exec "$@"
