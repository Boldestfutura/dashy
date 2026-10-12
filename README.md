# Dashy

A dashboard for TrueNAS SCALE, published on your tailnet. It reads the machine
directly and finds your apps on its own, so there is nothing to maintain as you
add and remove things.

![Dashy on a desktop browser](docs/01-desktop.png)

> **Early days.** This is pre-1.0 and still finding its shape. It runs happily
> every day on the NAS it was built for, but expect things to move around.

## What it does

- **Live vitals.** CPU, memory, temperatures, pool capacity, disk and network
  throughput. Every tile opens a detail sheet with per-core figures, drive
  temperatures, SMART state, throughput history and an uptime record.
- **Your apps, found automatically.** Nothing is hard-coded. Dashy joins your
  Tailscale Serve config against the TrueNAS app list, so publishing a service
  makes a tile appear for it, with its real icon, version and status. Remove one
  and the tile goes.
- **Updates from the dashboard.** Apps with an update show a marker; tapping it
  queues the upgrade and a recursive ZFS snapshot is taken first. Dashy updates
  itself the same way, straight from this repository.
- **Make it yours.** Hold a tile to drag it, resize it to double width with a
  sparkline, or hide it. Light, dark or system theme and six accent colours.
  **Layout and theme are stored on the server**, so they follow you to every
  device and apply within seconds without a refresh.
- **Optional weather** from a town you choose.

| | |
|:--:|:--:|
| ![Phone](docs/03-phone.png) | ![Apps](docs/04-apps.png) |
| On a phone | Apps, with versions and update markers |
| ![Settings](docs/05-settings.png) | ![Arrange](docs/06-arrange.png) |
| Settings | Hold-to-drag arrange mode |

![Dark mode](docs/02-desktop-dark.png)

## Requirements

- **TrueNAS SCALE** — the app list, versions and upgrades go through `midclt`.
- **Tailscale** with Serve configured, publishing at least one service. This is
  how Dashy discovers what you run.
- Permission to call `midclt` (root, or `truenas_admin`).

## Install

```sh
git clone https://github.com/Boldestfutura/dashy.git
cd dashy
./install.sh
```

It works out the pool, a free local port, a free tailnet port and where
Tailscale keeps `serve.json`, then asks about anything it cannot determine.
When it finishes it prints your URL. First run walks you through a short setup.

It also asks how you want it packaged:

- **image** (default) — pulls `ghcr.io/boldestfutura/dashy:latest`. TrueNAS then
  treats Dashy like any other app and will offer updates on the Apps page.
- **files** — a stock `python:3.13-alpine` with this directory mounted in. No
  registry involved, and you can edit `server.py` in place and restart.

Either way the two cron scripts live on the host, because they need `zpool`,
`smartctl` and `midclt`. In image mode the container copies them out to the
host as it starts, so an image update carries them along.

Override any of its choices with environment variables:

```sh
POOL=tank LPORT=30802 TPORT=8455 APP_NAME=dashy ./install.sh
INSTALL_MODE=files ./install.sh
IMAGE=ghcr.io/you/your-fork:latest ./install.sh
```

Give it a minute after installing — pool capacity, temperatures and power come
from a cron job that runs on the minute.

## Check it worked

```sh
./test.sh
```

Twenty-odd checks, read-only, safe to run at any time. It tells you whether the
box can run Dashy at all, whether Tailscale Serve is set up properly, whether
the app answers, whether the cron jobs are registered and whether the data they
write is actually fresh. Run it before installing and it does the preflight
checks alone.

It exits with the number of failures, so CI can use it. If something is wrong
this is the fastest way to find out what.

## Updating

Dashy tracks this repository. Every half hour `collect.sh` reads the version
number from `main`; if it is higher than the running one, **Update Dashy**
appears at the top of the settings pane with a dot on the gear.

Tapping it queues the update and the root worker does the rest: it downloads
the files, refuses to go on unless `server.py` actually compiles, takes a
recursive ZFS snapshot of the install, replaces the files and redeploys the
app. `worker.sh` replaces itself by rename, last, so the copy that is running
finishes on the old inode.

What "the rest" is depends on how Dashy is packaged. The worker asks the app
which shape it is rather than guessing:

- **image install** — `app.pull_images`, which pulls the new image and
  redeploys. The container then copies the new cron scripts out to the host.
- **file install** — downloads the files from this repository, refuses to go on
  unless `server.py` actually compiles, and swaps them in. `worker.sh` replaces
  itself last and by rename, so the copy that is running finishes on the old
  inode.

Both take a recursive ZFS snapshot of the install first.

If an update goes wrong, the snapshot is called `@preupdate-<timestamp>`:

```sh
zfs rollback <pool>/Applications/dashy@preupdate-20261012-184500
midclt call -j app.redeploy dashy
```

On an image install you can also just pin the old tag — every release is
published as `ghcr.io/boldestfutura/dashy:<version>`, not only `:latest`.

To track a fork instead, set `UPDATE_REPO=you/your-fork` (and `UPDATE_BRANCH`
if it is not `main`) in the app's environment. Both scripts read it.

### The TrueNAS Apps page

On an **image install** the Apps page works normally: TrueNAS polls ghcr.io and
offers the update itself. On a **file install** it cannot — there is no image to
pull — so updates go through the dashboard.

One wrinkle on an image install: the version check reads this repository, but
the thing it installs is the image, and the image is built by CI a minute or
two after a push. Tap Update in that window and you will pull the previous
image and stay on the old version. The next check puts the row back.

## What leaves your network

Dashy is built to keep to itself. In full, everything that ever goes out:

| What | When | What it sees |
|---|---|---|
| **open-meteo.com** | Only if you turn weather on. Your NAS, every 15 minutes. | Your server's IP and the **town you picked**. Never an address, never a browser request, no account, no API key. |
| **Geocoding (open-meteo)** | Only while you are typing a town name in setup. | The text you typed, proxied through your own server. |
| **media.sys.truenas.net** | Once per app, when an icon is first seen. | Your server's IP. Icons are then cached locally and served by Dashy. |
| **raw.githubusercontent.com** | Every 30 minutes, to read the version number in this repository. | Your server's IP. Nothing is sent about you, your NAS or what you run. |
| **ghcr.io** | Image installs only. When TrueNAS checks for a newer image, and when you install one. | Your server's IP. This is an ordinary registry pull. |

That is the complete list. Your browser never contacts anything but your own
NAS. There is no telemetry, no analytics, no phone-home and no account anywhere
in this project.

The version check is a plain `GET` of a public file. It reports nothing. If you
would rather it never ran, point `UPDATE_REPO` at a fork, or comment the block
out at the end of `app/collect.sh` — nothing else depends on it.

**Weather is off until you choose a town.** There are no built-in coordinates,
no IP geolocation and no browser location API. If you never turn it on, nothing
ever contacts a weather service.

## What it installs

| Thing | Where |
|---|---|
| App | a custom app, host networking, one published port |
| Image | `ghcr.io/boldestfutura/dashy` (image installs only) |
| Code | `<pool>/Applications/dashy/app` |
| Data | `<pool>/Applications/dashy/data` — written by cron, read by the app |
| Config | `<pool>/Applications/dashy/config` — shared settings |
| Queue | `<pool>/Applications/dashy/queue` — upgrade requests |
| Cron | two root jobs, every minute: `collect.sh`, `worker.sh` |
| Serve | one `TCP` entry and one `Web` handler added to your `serve.json` |

`serve.json` is backed up before it is touched.

### Why cron

A container cannot read `zpool` capacity, and `intel-rapl` energy counters are
mode 400 root-only. `collect.sh` publishes those to a file the app reads.
`worker.sh` performs upgrades, because the container has no `midclt`. The app
asks for an upgrade by dropping a file in the queue; the worker validates the
app name against the installed list before acting on it.

### Why host networking

So the network card reads real interface counters rather than a Docker virtual
adapter, and so `/proc` is the host's.

## Configuration

Everything is optional — the app discovers or asks for all of it.

| Variable | Default |
|---|---|
| `DASH_PORT` | `8080` (the installer picks a free one) |
| `TAILNET_HOST` | discovered from `serve.json` |
| `HOST_NAME` | first label of the tailnet hostname |
| `HOST_IP` | `127.0.0.1` |
| `WEATHER_LAT` / `WEATHER_LON` / `WEATHER_TZ` | unset — use the setup screen instead |
| `HEXOS_URL` | `https://deck.hexos.com/dash` |
| `UPDATE_REPO` / `UPDATE_BRANCH` | `Boldestfutura/dashy` / `main` |
| `INSTALL_MODE` / `IMAGE` | `image` / `ghcr.io/boldestfutura/dashy:latest` (installer only) |

## Uninstall

```sh
./uninstall.sh
```

Removes the app, its cron jobs and its Serve entry. It leaves your datasets
alone — deleting data should be a decision, not a side effect.

## Known limitations

- **The TrueNAS Apps page shows a generic icon.** Custom apps have no icon
  field, and TrueNAS SCALE no longer supports third-party catalogues
  (`catalog.update` accepts only `preferred_trains`), so there is no supported
  way to set one. Dashy does ship a proper icon for the browser tab and the
  phone home screen.
- Upgrades of **other** apps go through `midclt app.upgrade`, so anything that
  tool cannot upgrade, Dashy cannot either.
- Power figures need Intel RAPL. On a CPU without it, the power chart is empty.
- **There is no authentication.** It is meant to sit behind Tailscale, where
  reaching it at all means you are already on the tailnet. Do not expose it to
  the internet.

## Licence

MIT.
