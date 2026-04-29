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
    ffmpeg

# App dependencies
RUN pip install --no-cache-dir SpotiFLAC flask python-dotenv

# Patch SpotiFLAC: preserve / as directory separator in filename formats
COPY patch_spotiflac.py /tmp/patch_spotiflac.py
RUN python3 /tmp/patch_spotiflac.py

RUN mkdir -p /vpn /downloads /app/templates && \
    chmod 700 /vpn && \
    mkdir -p /etc/wireguard && chmod 700 /etc/wireguard

WORKDIR /app

# Unbuffered Python output so docker logs shows everything immediately
ENV PYTHONUNBUFFERED=1
# nc (netcat) for port self-test in entrypoint.sh
RUN apk add --no-cache netcat-openbsd

COPY entrypoint.sh /entrypoint.sh
COPY app.py config.py worker.py vpn.py routes.py /app/
COPY templates/ /app/templates/
COPY static/ /app/static/
RUN chmod +x /entrypoint.sh

VOLUME ["/downloads"]

# Required capabilities (set in docker run / compose):
#   --cap-add NET_ADMIN   — network interfaces, routing, iptables
#   --cap-add NET_RAW     — raw sockets (OpenVPN)
# Required device:
#   /dev/net/tun          — for OpenVPN tun interface

ENTRYPOINT ["/entrypoint.sh"]
