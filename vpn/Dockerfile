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

RUN mkdir -p /vpn /downloads /app/templates && \
    chmod 700 /vpn && \
    mkdir -p /etc/wireguard && chmod 700 /etc/wireguard

WORKDIR /app

# Python-Output ungepuffert → docker logs zeigt alles sofort
ENV PYTHONUNBUFFERED=1
# nc (netcat) für Port-Selbsttest in entrypoint.sh
RUN apk add --no-cache netcat-openbsd

COPY vpn/entrypoint.sh /entrypoint.sh
COPY app.py config.py worker.py vpn.py routes.py /app/
COPY templates/ /app/templates/
RUN chmod +x /entrypoint.sh

VOLUME ["/downloads"]

# Required capabilities (set in docker run / compose):
#   --cap-add NET_ADMIN   — network interfaces, routing, iptables
#   --cap-add NET_RAW     — raw sockets (OpenVPN)
# Required device:
#   /dev/net/tun          — for OpenVPN tun interface

ENTRYPOINT ["/entrypoint.sh"]
