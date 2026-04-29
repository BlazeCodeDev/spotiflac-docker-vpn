# SpotiFLAC Docker

A self-hosted web UI for downloading lossless audio from Spotify links, running inside a Docker container with a built-in VPN and network kill-switch.

> **Disclosure:** The code in this repository was written by Claude. The architecture, feature choices, and overall direction were defined by me.

---

## Disclaimer

This project is intended for **educational purposes only**. It is not affiliated with, endorsed by, or in any way connected to Spotify, Tidal, Qobuz, Amazon Music, Deezer, YouTube, or any other music streaming service. All product names, logos, and trademarks are the property of their respective owners.

This software does not condone, facilitate, or encourage copyright infringement or piracy. Downloading copyrighted music without the authorisation of the rights holder may be illegal in your country. You are solely responsible for ensuring that your use of this software complies with all applicable laws and the terms of service of any platform you interact with.

---

## What this is

SpotiFLAC Docker wraps the [SpotiFLAC](https://github.com/streamingflac/spotiflac) Python library in a small Flask web server so you can queue downloads from a browser instead of running a CLI. You paste Spotify URLs (tracks, albums, or playlists), pick a quality level and which streaming services to try, and the container downloads the audio in the background.

Because downloading from services like Tidal or Qobuz often requires connecting from a specific country, the container starts a VPN tunnel before the app launches and enforces a kill-switch: if the tunnel drops, all outbound traffic is blocked immediately and the container exits, so downloads never leak through your real IP.

The UI polls the backend every few seconds and shows live progress, toast notifications on completion, and per-job controls for cancelling, retrying, and reordering.

---

## How it works

### VPN configuration

Two protocols are supported. You provide the configuration via environment variables; the entrypoint writes it to files at startup.

**OpenVPN** accepts three mutually exclusive inputs:

- `VPN_CONFIG_BASE64` — a base64-encoded `.ovpn` file. Useful when you want to store the config in an environment variable without dealing with newlines.
- `VPN_CONFIG_FILE` — a path to an `.ovpn` file mounted into the container (e.g. `/vpn/config.ovpn`).
- `VPN_SERVER` + `VPN_CA_CERT_BASE64` — the entrypoint generates a minimal config from the individual fields. Requires `VPN_USER`, `VPN_PASS`, and the CA certificate.

If `VPN_USER` and `VPN_PASS` are set alongside any of the above, the entrypoint writes them to a separate auth file and injects an `auth-user-pass` directive, overriding any inline credentials in the config.

**WireGuard** accepts:

- `WG_CONFIG_BASE64` — a base64-encoded `wg0.conf`.
- `WG_CONFIG_FILE` — a path to a mounted WireGuard config.
- Individual fields: `WG_PRIVATE_KEY`, `WG_ADDRESS`, `WG_SERVER_PUBLIC_KEY`, `WG_ENDPOINT`, and optional `WG_DNS`, `WG_PRESHARED_KEY`, `WG_ALLOWED_IPS`, `WG_KEEPALIVE`. The entrypoint generates a `wg0.conf` from these.

---

## Docker Compose setup

The repository includes a `docker-compose.yml` that covers the common case. Copy it, fill in your VPN credentials, and run `docker compose up -d`.

```yaml
services:
  spotiflac:
    image: spotiflac-docker-vpn:latest
    container_name: spotiflac-test
    pull_policy: never
    devices:
      - /dev/net/tun:/dev/net/tun
    environment:
      - TZ=Europe/Berlin
      - OUTPUT_DIR=/downloads
      - SPOTIFLAC_SERVICES=tidal,qobuz,amazon,spoti,youtube
      - FILENAME_FORMAT={artist}/{year} - {album}/{track}. {title}
      - USE_ARTIST_SUBFOLDERS=true
      - USE_ALBUM_SUBFOLDERS=true
      - RETRY_MINUTES=5
      - QOBUZ_TOKEN=
      - PORT=5000
      - UI_PASSWORD=
      - VPN_USER=
      - VPN_PASS=
      - VPN_CONFIG_FILE=/vpn/config.ovpn
      - LOG_LEVEL=debug
    cap_add:
      - NET_ADMIN
      - NET_RAW
    ports:
      - 5000:5000
    volumes:
      - music:/downloads
      - spotiflac_config:/vpn
    restart: unless-stopped

volumes:
  music:
  spotiflac_config:
```

A few things worth understanding about this compose file:

**`devices: /dev/net/tun`** — OpenVPN requires the TUN device to create a virtual network interface. Without this, OpenVPN cannot open the tunnel. WireGuard does not use TUN in the same way (it uses the kernel module directly), but passing the device is harmless.

**`cap_add: NET_ADMIN, NET_RAW`** — `NET_ADMIN` lets the entrypoint configure network interfaces, routing tables, and iptables rules. `NET_RAW` is required by OpenVPN for raw socket access. Without these, the kill-switch cannot be applied and the VPN cannot start.

**`volumes: spotiflac_config:/vpn`** — This named volume serves two purposes: it is where you place your `config.ovpn` (or WireGuard config) before first run, and it is where the job state file is written so downloads survive container restarts. Because it is a named volume, Docker manages its lifecycle separately from the container.

**`volumes: music:/downloads`** — All downloaded files land here. Mount this wherever your media library lives, or use a bind mount if you prefer a specific host path:

```yaml
volumes:
  - /path/to/your/music:/downloads
```

**`restart: unless-stopped`** — If the VPN tunnel drops, the monitoring loop in `entrypoint.sh` kills the app process and exits with code 1. With `restart: unless-stopped`, Docker restarts the container automatically, which triggers a fresh VPN connection attempt.

**`pull_policy: never`** — The image is built locally (`docker build -t spotiflac-docker-vpn .`). This prevents Docker Compose from trying to pull it from a registry on every `up`.

### Building the image

```bash
docker build -t spotiflac-docker-vpn .
```

The Dockerfile uses `python:3.12-alpine` as the base and installs OpenVPN, WireGuard tools, iptables, ffmpeg, and the SpotiFLAC Python package. The build-time patch script runs during the image build so the patched library is baked in.

### Placing your VPN config

Before starting the container for the first time, write your `.ovpn` file into the named volume. One way to do this is to start a temporary container with the volume mounted:

```bash
docker run --rm -v spotiflac_config:/vpn alpine sh -c "cat > /vpn/config.ovpn" < your-config.ovpn
```

Or if you prefer to pass the config via environment variable, base64-encode it:

```bash
VPN_CONFIG_BASE64=$(base64 -w 0 your-config.ovpn)
```

Then set `VPN_CONFIG_BASE64` in the compose environment and remove the `VPN_CONFIG_FILE` line.

---

## Environment variable reference

| Variable | Default | Description |
|----------|---------|-------------|
| `OUTPUT_DIR` | `./downloads` | Where downloaded files are written inside the container |
| `SPOTIFLAC_SERVICES` | `tidal,qobuz,amazon,youtube` | Comma-separated list of services to try, in order |
| `FILENAME_FORMAT` | `{artist} - {year} - {album}/{track}. {title}` | Filename template. Use `/` to create subdirectories |
| `USE_ARTIST_SUBFOLDERS` | `false` | Create a subfolder per artist (SpotiFLAC built-in option) |
| `USE_ALBUM_SUBFOLDERS` | `false` | Create a subfolder per album (SpotiFLAC built-in option) |
| `RETRY_MINUTES` | `0` | Minutes to wait before retrying a failed download. 0 disables retries |
| `QOBUZ_TOKEN` | — | Qobuz user auth token, required for Qobuz downloads |
| `PORT` | `5000` | Port the Flask app listens on |
| `UI_PASSWORD` | — | If set, enables HTTP Basic Auth. The username field is ignored; only the password is checked |
| `VPN_PROTOCOL` | `openvpn` | `openvpn` or `wireguard` |
| `VPN_COUNTRY` | — | Display-only label shown in the UI VPN badge |
| `VPN_CONFIG_FILE` | — | Path to `.ovpn` file inside the container |
| `VPN_CONFIG_BASE64` | — | Base64-encoded `.ovpn` file content |
| `VPN_SERVER` | — | OpenVPN server hostname (when building config from parts) |
| `VPN_PORT` | `1194` | OpenVPN server port |
| `VPN_TRANSPORT` | `udp` | `udp` or `tcp` |
| `VPN_USER` | — | OpenVPN username |
| `VPN_PASS` | — | OpenVPN password |
| `VPN_CA_CERT_BASE64` | — | Base64-encoded CA certificate (required with `VPN_SERVER`) |
| `VPN_TLS_KEY_BASE64` | — | Base64-encoded TLS auth key (optional, for `tls-auth`) |
| `VPN_CONNECT_TIMEOUT` | `30` | Seconds to wait for the tunnel interface to appear |
| `VPN_CHECK_INTERVAL` | `10` | Seconds between tunnel health checks |
| `VPN_PING_HOST` | — | If set, the monitor pings this host through the tunnel on each check |
| `WG_CONFIG_FILE` | — | Path to `wg0.conf` inside the container |
| `WG_CONFIG_BASE64` | — | Base64-encoded `wg0.conf` |
| `WG_PRIVATE_KEY` | — | WireGuard private key |
| `WG_ADDRESS` | — | WireGuard interface address (CIDR) |
| `WG_SERVER_PUBLIC_KEY` | — | Peer public key |
| `WG_ENDPOINT` | — | Peer endpoint (`host:port`) |
| `WG_DNS` | — | DNS server for the WireGuard interface |
| `WG_PRESHARED_KEY` | — | Optional preshared key |
| `WG_ALLOWED_IPS` | `0.0.0.0/0,::/0` | Allowed IPs (routes tunnelled through WireGuard) |
| `WG_KEEPALIVE` | `25` | PersistentKeepalive in seconds |
| `ALLOW_SUBNETS` | — | Comma-separated extra subnets to allow through the kill-switch (e.g. local NAS) |
| `LOG_LEVEL` | `info` | `info` or `debug`. Debug logs iptables rules, network state, and env vars at startup |
| `APP_CMD` | `python /app/app.py` | Command used to start the Flask app |

---

## Using the UI

Open `http://your-host:5000` in a browser. If `UI_PASSWORD` is set, your browser will prompt for a password.

The main input accepts Spotify track, album, or playlist URLs — one per line, or comma-separated. Click **Download** to enqueue them immediately, or **Search** to open a Spotify search dialog where you can browse and select items before queuing.

The **Settings** panel (gear icon) lets you:
- Toggle and reorder which services are tried, and in what order. The order set here is sent with each download request.
- Switch between Lossless (16-bit FLAC) and Hi-Res (24-bit, where available on Tidal and Qobuz).
- Enter a Qobuz token. This is stored in the browser session only and sent with each request — it is never persisted server-side beyond what the active download needs.
- View the filename format, folder structure toggles, and retry interval that are in effect (these are read-only in the UI because they are set via environment variables).

The **Downloads** panel lists all jobs. Each job shows the album art, title (fetched from Spotify metadata), status badge, quality, start/end time, and action buttons. You can filter the list by title or artist. Jobs can be dragged to reorder them (the order affects display only; the worker always runs the oldest-queued job first). Completed jobs can be cleared in bulk with the "Clear done" button, or removed individually.

The VPN badge in the header shows the current tunnel status. Hovering over it reveals the public IP, country, city, and ISP as reported by ip-api.com, plus the tunnel uptime.

---
