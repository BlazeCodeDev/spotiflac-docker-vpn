FROM python:3.12-alpine

# VPN + networking tools
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
    su-exec

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
# requests is pulled in unconditionally by SpotiFLAC's core/signed_session_desktop.py
# but isn't declared in SpotiFLAC's own dependencies (upstream packaging gap as of
# 1.4.5) — install it explicitly or the app fails to import at all.
RUN pip install --no-cache-dir --target /spotiflac "SpotiFLAC==1.4.5" requests
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
