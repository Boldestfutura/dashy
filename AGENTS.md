# Working on Dashy

Dashy is a one-page dashboard for a TrueNAS box. It shows vitals, pools, drives, VMs and
every installed app, and needs no API key. The whole app is `app/server.py`: a stdlib
Python server with the page's HTML, CSS and JS inline. The host-side cron scripts are
`app/collect.sh` and `app/worker.sh`, and `install.sh` / `test.sh` are for installs.

This repo is **public**. Never commit a real hostname, IP address, tailnet name or
screenshot of a real machine.

## Shipping a change

1. Bump `APP_VERSION` at the top of `app/server.py`. Use minor for features, patch for
   fixes. 1.0.0 shipped on 2026-10-27 after a full audit; a breaking change to prefs,
   the data files or the queue format would be 2.0.
2. Test locally against real data (see below), in light and dark, at desktop and phone
   width.
3. Commit as `Boldestfutura <102939353+Boldestfutura@users.noreply.github.com>`, then push
   to `main`.
4. GitHub Actions builds `ghcr.io/boldestfutura/dashy:<version>` and cuts a release. Watch
   it at `https://api.github.com/repos/Boldestfutura/dashy/actions/runs?per_page=1`.
5. The owner installs it from inside Dashy (Settings, then App updates). **Nothing updates
   unattended**, and that's deliberate.
   - Right after a push, "up to date" is normal for a minute: ghcr's tag list lags.
   - raw.githubusercontent also caches for several minutes.

## Testing locally

Copy `data/` and `config/` from a real install. Then run:

    DASH_PORT=8099 DASH_DATA=<copy>/data DASH_CONFIG=<copy>/config DASH_QUEUE=<tmp> \
      SERVE_JSON=/nonexistent python3 -I app/server.py

A local run reads *this* machine's `/proc` and can't reach the NAS's apps, so "N apps not
responding" is expected. For real numbers, save a NAS's `/api/stats` and feed it to the
page: override `window.fetch` for `/api/stats` and call `paint(stats)`. Drive the page over
CDP and click things; a screenshot alone has missed real bugs here.

## Rules learned the hard way

- **Prefs are whitelisted server-side.** Anything new stored in prefs must also be added to
  `_clean_prefs`, or saving silently drops it. `_ACCENTS` must cover every accent the page
  offers, plus retired names (they migrate through `ACCENT_WAS` in the page).
- **Theme palettes are declared three times**: light on `:root`, dark under
  `prefers-color-scheme` guarded by `:root:not([data-theme="light"])`, and dark again
  under `:root[data-theme="dark"]`. Surfaces (`--bg`, `--panel`, `--surf`, `--brd`,
  `--muted`, `--ink`) are then derived from the accent in OKLCH at runtime
  (`applySurfaces`).
- `--accent` and `--brand` are always the same colour. Don't use them to tell two series
  apart. Memory's ZFS cache uses `--mem-cache` (the accent mixed 60% into the panel).
- **Never put a `<button>` inside a `<button>`**; the parser hoists it out. Cards sit in a
  `.cardwrap` with their tools beside them.
- **Don't blanket-rename identifiers.** `worker.sh` matches the installed image against
  `*/dashy`. A find-and-replace once broke self-update in a way the UI couldn't recover
  from.
- `paint()` must assign `data` before calling `layoutSig()`. Keep `tick()`'s catch
  logging; an empty catch hid real failures.
- Status colours (ok / warn / bad) are reserved and always come with a glyph or text,
  never colour alone. Accents stay in the blue-to-stone range: Dusk (the default),
  Harbour, Heather and Granite.

## Things that must stay true (1.0 audit)

- **Health is one set of rules.** `healthRows` judges everything; `severity` and
  `headline` derive from it, and `rowsSystem` lists the same problems. Add a new check
  to `healthRows`, never to the card alone. Updates are not faults.
- **Frozen data must look frozen.** `paintStale` flags lost contact after 12s without a
  good poll; `pools.stamp` is written only after `zpool list` succeeds, and the page
  calls the collector stale after 180s.
- **Every POST is refused unless it is same-site JSON** (`_json_body`). The page must
  send `Content-Type: application/json` on every POST, even an empty one.
- **Both cron scripts hold a `flock`**, and anything that can hang (`smartctl`, `zpool`,
  `midclt`) runs under `timeout`. The worker writes status only through `status()`,
  which renames into place.
- The worker prunes its own `preupdate-`/`preupgrade-<app>-` snapshots to the newest
  five, matched by an anchored regex. Never widen that match.
- Escape anything from the NAS (`esc`) before it goes into markup.
- **Nothing about one NAS is baked in.** Hostname, TrueNAS version, CPU, board and memory
  come from `system.info` (collector → `system.json`); app titles and blurbs from the
  catalogue metadata (custom apps have none and are titled from their name); "which app
  is this dashboard" is the dataset name. CPU temps read coretemp, k10temp or zenpower.
- **Updates show every step.** Tap → `queued` (the `.req` file, via `queued()`) →
  `running` with a percentage (worker `job()`/`track()`) → done/failed, on the tile and
  in `#upbanner`. Confirming returns to the home screen. A self-update restart is
  expected (`selfBusy`), and a new version reloads the page.

## Current design (1.0.0)

- Every tile, home and settings, has a `--panel` fill, a 1px `--brd` outline and
  `--tile-shadow`. Shape scale: cards 16px, app tiles 12px.
- Two sizes only, 1x and 2x (4x was removed in 1.1.2; a stored 4 loads as 2). The health
  tile has a two-column checklist (`healthRows`) at 2x. The memory tile is a sparkline at
  1x, laid out like CPU temp, and an animated donut at 2x (`paintMemDonut`, SVG with
  circumference 100).
- In arrange mode the overlay covering each tile carries the tile's name (`.ctname`).
- App tiles are all one size: `.appgrid` uses `grid-auto-rows:1fr` and the name and version
  never wrap. A long version once made Plex's row taller and dropped its neighbours' text.
  The tile shows `niceVer()` ("Version 1.43.5", "Custom app"); the raw `human_version` stays
  in the tooltip and the app sheet.
- TrueNAS does not record a VM's guest OS. `vmOS()` guesses it from the name, description and
  install ISO / disk names (`media` in vms.json) and puts that OS's logo on the card (`VM_OS`,
  Simple Icons paths, inline). No match keeps the generic monitor icon.
- Settings, System, App updates is one button. It checks; if a version is waiting it
  becomes "Update to x" and opens the install sheet.
