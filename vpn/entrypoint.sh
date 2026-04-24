#!/bin/sh
set -e

# Hilfsfunktionen für schöneres Logging
log()  { echo "[vpn] $*"; }
err()  { echo "[vpn] ERROR: $*" >&2; }
die()  { err "$*"; exit 1; }

# Arbeitsverzeichnis für VPN-Konfigurationen
CREDS_DIR=/vpn
mkdir -p "$CREDS_DIR"

# Sicherstellen, dass das Verzeichnis beschreibbar ist (wichtig für auth.txt)
if ! chmod 700 "$CREDS_DIR" 2>/dev/null; then
    log "WARNING: Could not change permissions on $CREDS_DIR. Ensure the volume is not read-only."
fi

# ── Protokoll-Auswahl ────────────────────────────────────────────────────────
VPN_PROTOCOL="${VPN_PROTOCOL:-openvpn}"
case "$VPN_PROTOCOL" in
    openvpn|wireguard) ;;
    *) die "VPN_PROTOCOL muss 'openvpn' oder 'wireguard' sein, erhalten: $VPN_PROTOCOL" ;;
esac
log "Protocol: $VPN_PROTOCOL"

# ─────────────────────────────────────────────────────────────────────────────
# OpenVPN Setup
# ─────────────────────────────────────────────────────────────────────────────
setup_openvpn() {
    : "${VPN_USER:?VPN_USER ist erforderlich für OpenVPN}"
    : "${VPN_PASS:?VPN_PASS ist erforderlich für OpenVPN}"

    CONFIG_PATH="$CREDS_DIR/config.ovpn"
    AUTH_PATH="$CREDS_DIR/auth.txt"

    if [ -n "$VPN_CONFIG_BASE64" ]; then
        echo "$VPN_CONFIG_BASE64" | base64 -d > "$CONFIG_PATH"
        log "Konfiguration aus VPN_CONFIG_BASE64 geladen"

    elif [ -n "$VPN_CONFIG_FILE" ] && [ -f "$VPN_CONFIG_FILE" ]; then
        # PRÜFUNG: Nur kopieren, wenn Quelle und Ziel unterschiedlich sind
        if [ "$VPN_CONFIG_FILE" != "$CONFIG_PATH" ]; then
            cp "$VPN_CONFIG_FILE" "$CONFIG_PATH"
            log "Konfiguration von $VPN_CONFIG_FILE nach $CONFIG_PATH kopiert"
        else
            log "Konfiguration liegt bereits am Zielort ($CONFIG_PATH). Kopieren übersprungen."
        fi

    elif [ -n "$VPN_SERVER" ]; then
        if [ -z "$VPN_CA_CERT_BASE64" ]; then
            die "VPN_CA_CERT_BASE64 ist erforderlich, wenn VPN_SERVER genutzt wird."
        fi
        VPN_PORT="${VPN_PORT:-1194}"
        VPN_TRANSPORT="${VPN_TRANSPORT:-udp}"
        log "Generiere Konfiguration für $VPN_SERVER:$VPN_PORT ($VPN_TRANSPORT)"

        echo "$VPN_CA_CERT_BASE64" | base64 -d > "$CREDS_DIR/ca.crt"

        cat > "$CONFIG_PATH" <<EOF
client
dev tun
proto ${VPN_TRANSPORT}
remote ${VPN_SERVER} ${VPN_PORT}
resolv-retry infinite
nobind
persist-key
persist-tun
remote-cert-tls server
ca ${CREDS_DIR}/ca.crt
auth-nocache
verb 2
auth-user-pass ${AUTH_PATH}
EOF
        if [ -n "$VPN_TLS_KEY_BASE64" ]; then
            echo "$VPN_TLS_KEY_BASE64" | base64 -d > "$CREDS_DIR/tls.key"
            chmod 600 "$CREDS_DIR/tls.key" 2>/dev/null || true
            echo "tls-auth $CREDS_DIR/tls.key 1" >> "$CONFIG_PATH"
        fi
    else
        die "Fehlende VPN-Daten: Setze VPN_CONFIG_BASE64, VPN_CONFIG_FILE oder VPN_SERVER + CA."
    fi

    # Zugangsdaten-Datei erstellen
    printf '%s\n%s\n' "${VPN_USER}" "${VPN_PASS}" > "$AUTH_PATH"
    chmod 600 "$AUTH_PATH" 2>/dev/null || true

    # Sicherstellen, dass die Konfiguration die auth.txt nutzt
    if ! grep -q "^auth-user-pass" "$CONFIG_PATH"; then
        echo "auth-user-pass $AUTH_PATH" >> "$CONFIG_PATH"
    else
        sed -i "s|^auth-user-pass.*|auth-user-pass $AUTH_PATH|" "$CONFIG_PATH"
    fi

    VPN_SERVERS=$(grep "^remote " "$CONFIG_PATH" | awk '{print $2}' | sort -u)
    VPN_IFACE="tun0"
}

# ─────────────────────────────────────────────────────────────────────────────
# WireGuard Setup
# ─────────────────────────────────────────────────────────────────────────────
setup_wireguard() {
    WG_CONF="$CREDS_DIR/wg0.conf"

    if [ -n "$WG_CONFIG_BASE64" ]; then
        echo "$WG_CONFIG_BASE64" | base64 -d > "$WG_CONF"
    elif [ -n "$WG_CONFIG_FILE" ] && [ -f "$WG_CONFIG_FILE" ]; then
        if [ "$WG_CONFIG_FILE" != "$WG_CONF" ]; then
            cp "$WG_CONFIG_FILE" "$WG_CONF"
        fi
    else
        : "${WG_PRIVATE_KEY:?WG_PRIVATE_KEY erforderlich}"
        : "${WG_ADDRESS:?WG_ADDRESS erforderlich}"
        : "${WG_SERVER_PUBLIC_KEY:?WG_SERVER_PUBLIC_KEY erforderlich}"
        : "${WG_ENDPOINT:?WG_ENDPOINT erforderlich}"

        WG_ALLOWED_IPS="${WG_ALLOWED_IPS:-0.0.0.0/0,::/0}"
        WG_KEEPALIVE="${WG_KEEPALIVE:-25}"

        cat > "$WG_CONF" <<EOF
[Interface]
PrivateKey = $WG_PRIVATE_KEY
Address = $WG_ADDRESS
${WG_DNS:+DNS = $WG_DNS}

[Peer]
PublicKey = $WG_SERVER_PUBLIC_KEY
${WG_PRESHARED_KEY:+PresharedKey = $WG_PRESHARED_KEY}
Endpoint = $WG_ENDPOINT
AllowedIPs = $WG_ALLOWED_IPS
PersistentKeepalive = $WG_KEEPALIVE
EOF
    fi

    chmod 600 "$WG_CONF" 2>/dev/null || true
    VPN_SERVERS=$(grep "^Endpoint" "$WG_CONF" | sed 's/.*=[[:space:]]*//' | sed 's/:[0-9]*$//' | tr -d '[]' | sort -u)
    VPN_IFACE="wg0"
}

# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktionen für Netzwerk & Start
# ─────────────────────────────────────────────────────────────────────────────
resolve_servers() {
    RESOLVED=""
    for server in $VPN_SERVERS; do
        ip=$(getent hosts "$server" 2>/dev/null | awk '{print $1}' | head -1)
        if [ -n "$ip" ]; then
            log "VPN-Server aufgelöst: $server → $ip"
            RESOLVED="$RESOLVED $ip"
        else
            log "WARNUNG: Konnte $server nicht auflösen - nutze Hostname"
            RESOLVED="$RESOLVED $server"
        fi
    done
    VPN_SERVERS="$RESOLVED"
}

apply_killswitch() {
    log "Aktiviere Kill-Switch (IPv4 + IPv6)..."
    IFACE_PATTERN="${VPN_IFACE%%[0-9]*}+"

    # Docker-Bridge-Interface ermitteln (eth0 in den meisten Containern)
    DOCKER_IFACE=$(ip route show default 2>/dev/null | awk '{print $5}' | head -1)
    DOCKER_IFACE="${DOCKER_IFACE:-eth0}"
    WEB_PORT="${PORT:-5000}"

    # IPv4 Regeln
    iptables -F INPUT 2>/dev/null || true
    iptables -F OUTPUT 2>/dev/null || true
    iptables -P INPUT DROP
    iptables -P OUTPUT DROP

    iptables -A INPUT  -i lo -j ACCEPT
    iptables -A OUTPUT -o lo -j ACCEPT
    iptables -A INPUT  -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    iptables -A INPUT  -i "$IFACE_PATTERN" -j ACCEPT
    iptables -A OUTPUT -o "$IFACE_PATTERN" -j ACCEPT

    # Web-UI: eingehende Verbindungen auf dem Docker-Bridge-Interface erlauben.
    # Download-Traffic läuft trotzdem ausschließlich durch den VPN-Tunnel,
    # weil SpotiFLAC ausgehende Verbindungen initiiert (OUTPUT DROP + nur tun/wg erlaubt).
    iptables -A INPUT -i "$DOCKER_IFACE" -p tcp --dport "$WEB_PORT" -j ACCEPT
    log "Web-UI erlaubt auf $DOCKER_IFACE:$WEB_PORT (ausgehender Traffic bleibt VPN-only)"

    for target in $VPN_SERVERS; do
        iptables -A OUTPUT -d "$target" -j ACCEPT
        iptables -A INPUT  -s "$target" -j ACCEPT
    done

    # IPv6 Block
    if command -v ip6tables > /dev/null 2>&1; then
        ip6tables -P INPUT DROP
        ip6tables -P OUTPUT DROP
        log "IPv6 Kill-Switch aktiv"
    fi
}

start_vpn() {
    case "$VPN_PROTOCOL" in
        openvpn)
            log "Starte OpenVPN..."
            openvpn --config "$CREDS_DIR/config.ovpn" --auth-nocache --log /vpn/openvpn.log --writepid /vpn/openvpn.pid --daemon
            ;;
        wireguard)
            log "Starte WireGuard..."
            mkdir -p /etc/wireguard
            cp "$CREDS_DIR/wg0.conf" /etc/wireguard/wg0.conf
            wg-quick up wg0
            ;;
    esac
}

wait_for_tunnel() {
    log "Warte auf Interface $VPN_IFACE..."
    max="${VPN_CONNECT_TIMEOUT:-30}"
    i=0
    while [ "$i" -lt "$max" ]; do
        if ip link show "$VPN_IFACE" > /dev/null 2>&1; then
            log "Tunnel ist online!"
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done
    die "Tunnel konnte nicht innerhalb von ${max}s aufgebaut werden."
}

monitor_tunnel() {
    log "Monitoring läuft..."
    while true; do
        sleep "${VPN_CHECK_INTERVAL:-10}"
        if ! ip link show "$VPN_IFACE" > /dev/null 2>&1; then
            err "VPN-Verbindung abgebrochen!"
            exit 1
        fi
    done
}

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
case "$VPN_PROTOCOL" in
    openvpn)   setup_openvpn  ;;
    wireguard) setup_wireguard ;;
esac

resolve_servers
apply_killswitch
start_vpn
wait_for_tunnel

if [ -n "$APP_CMD" ]; then
    log "Starte Anwendung: $APP_CMD"
    sh -c "$APP_CMD" &
    APP_PID=$!
fi

monitor_tunnel
