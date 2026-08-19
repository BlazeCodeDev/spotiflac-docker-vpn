# ── Git commit capture ──────────────────────────────────────────────────────
# Throwaway stage: needs the full .git directory (for packed-refs / detached
# HEAD / etc, which `git` handles correctly and a hand-rolled ref parse
# wouldn't) and a git binary, but neither should end up in the final image —
# only the tiny GIT_COMMIT file is copied out below.
FROM python:3.12-alpine AS gitinfo
RUN apk add --no-cache git
WORKDIR /src
COPY .git /src/.git
RUN git rev-parse --short HEAD > /GIT_COMMIT 2>/dev/null || echo unknown > /GIT_COMMIT

FROM python:3.12-alpine

# VPN + networking tools
# flac: core/flac_validation.py shells out to the `flac` binary to verify FLAC
# integrity. Upstream treats a missing binary as "valid, skip the check"
# rather than a false-positive corruption.
# nodejs: as of 1.8.0 SpotiFLAC bundles no download providers at all — every
# provider is an externally-installed "extension" (see worker.refresh_extensions),
# and the default/legacy-aliased ones (tidal-web, qobuz-web, ytmusic-spotiflac,
# ...) run as JavaScript extensions via a Node.js bridge (extensions/runtime.py).
# Baked in at build time rather than relying on SpotiFLAC's own runtime
# auto-install, which would otherwise hit the network for the first time from
# inside the iptables kill-switch on first extension use.
# xvfb + chromium: several extensions (qobuz-web, amazon, deezer, and Tidal's
# own "LOSSLESS API" path all hit this) go through core/solver.py to solve a
# Cloudflare Turnstile challenge via a real (though virtual) Chromium browser
# — deliberately not `--headless`, since a fully headless browser is more
# likely to be challenged in the first place. Without both binaries present,
# every one of them fails identically with "[Errno 2] No such file or
# directory: 'Xvfb'". solver.py starts/stops Xvfb itself (spawns `Xvfb :99`
# and sets DISPLAY) and already passes --no-sandbox/--disable-dev-shm-usage,
# so no extra entrypoint wiring or docker-compose shm_size bump is needed —
# just the two binaries. Confirmed via Alpine's package index that `xvfb`
# provides /usr/bin/Xvfb and `chromium` provides /usr/bin/chromium-browser,
# which is exactly the path solver.py's _find_chrome() checks first.
RUN apk add --no-cache \
    openvpn \
    wireguard-tools \
    openresolv \
    iptables \
    ip6tables \
    bash \
    bind-tools \
    iputils \
    ffmpeg \
    flac \
    nodejs \
    xvfb \
    chromium \
    su-exec

# Belt-and-suspenders: core/solver.py's _find_chrome() checks CHROME_PATH
# before falling back to path probing/PATH search, so this pins the exact
# binary instead of relying on that fallback order matching Alpine's layout.
ENV CHROME_PATH=/usr/bin/chromium-browser

# App runs as an unprivileged user (entrypoint drops root via su-exec) so a
# compromised SpotiFLAC can't touch the NET_ADMIN kill-switch. The uid/gid are
# re-created at runtime from PUID/PGID; this just seeds the home directory.
RUN mkdir -p /home/appuser && chmod 755 /home/appuser

# App dependencies (SpotiFLAC is installed separately for easy in-place upgrades)
RUN pip install --no-cache-dir flask python-dotenv gunicorn

# SpotiFLAC goes to /spotiflac so it can be upgraded via a named volume without
# rebuilding the image.  Docker copies this directory into a fresh named volume
# on first run, and subsequent pip upgrades via the UI persist there.
# Pinned to a FIXED version (kept in lockstep with entrypoint.sh SPOTIFLAC_PINNED
# and with patch_spotiflac.py, whose matches are version-specific). Not
# auto-upgraded on boot. Bump only after re-verifying the patches apply.
# requests is declared as a real dependency by SpotiFLAC itself (kept explicit
# here so a future downgrade of the pin doesn't silently reopen the packaging
# gap that existed before 1.4.5 declared it). typing_extensions is pulled in
# transitively via pydantic — confirmed with a bare `import SpotiFLAC` during
# the 3.0.4 bump; not pinned explicitly unless that stops being true.
RUN pip install --no-cache-dir --target /spotiflac "SpotiFLAC==3.0.5" requests
ENV PYTHONPATH=/spotiflac

RUN mkdir -p /vpn /downloads /app/templates && \
    chmod 700 /vpn && \
    mkdir -p /etc/wireguard && chmod 700 /etc/wireguard

WORKDIR /app

# Unbuffered Python output so docker logs shows everything immediately
ENV PYTHONUNBUFFERED=1
# nc (netcat) for port self-test in entrypoint.sh
RUN apk add --no-cache netcat-openbsd

COPY entrypoint.sh /entrypoint.sh
COPY patch_spotiflac.py /app/patch_spotiflac.py
COPY app.py config.py worker.py vpn.py routes.py settings.py lib_index.py listenbrainz.py /app/
COPY templates/ /app/templates/
COPY static/ /app/static/
COPY --from=gitinfo /GIT_COMMIT /app/GIT_COMMIT
RUN chmod +x /entrypoint.sh

# Apply patches to the build-time SpotiFLAC install.  The entrypoint re-runs
# this on every startup so upgrades via the UI are patched automatically.
RUN python3 /app/patch_spotiflac.py

VOLUME ["/downloads"]

# Required capabilities (set in docker run / compose):
#   --cap-add NET_ADMIN   — network interfaces, routing, iptables
#   --cap-add NET_RAW     — raw sockets (OpenVPN)
# Required device:
#   /dev/net/tun          — for OpenVPN tun interface

ENTRYPOINT ["/entrypoint.sh"]
