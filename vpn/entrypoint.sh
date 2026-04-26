#!/bin/sh
set -e

# ── Log-Level ─────────────────────────────────────────────────────────────────
# LOG_LEVEL=info  (default) — normales Logging
# LOG_LEVEL=debug           — iptables, Netzwerk, Env-Vars, App-Status
LOG_LEVEL="${LOG_LEVEL:-info}"

log()   { echo "[vpn] $(date '+%H:%M:%S') INFO  $*"; }
err()   { echo "[vpn] $(date '+%H:%M:%S') ERROR $*" >&2; }
die()   { err "$*"; exit 1; }
debug() {
    [ "$LOG_LEVEL" = "debug" ] && echo "[vpn] $(date '+%H:%M:%S') DEBUG $*" || true
}

# ─────────────────────────────────────────────────────────────────────────────
CREDS_DIR=/vpn
mkdir -p "$CREDS_DIR"
if ! chmod 700 "$CREDS_DIR" 2>/dev/null; then
    log "WARN: chmod 700 auf $CREDS_DIR fehlgeschlagen — Volume evtl. read-only"
fi

# ── Debug: Env-Vars und System-Info beim Start ────────────────────────────────
if [ "$LOG_LEVEL" = "debug" ]; then
    echo "[vpn] ══════════════════ DEBUG START ══════════════════"
    echo "[vpn] Kernel : $(uname -r)"
    echo "[vpn] Env-Vars (ohne Secrets):"
    env | grep -v -i 'PASS\|KEY\|TOKEN\|SECRET\|BASE64' | sort | sed 's/^/[vpn]   /'
    echo "[vpn] Netzwerk-Interfaces:"
    ip addr 2>/dev/null | sed 's/^/[vpn]   /' || echo "[vpn]   (ip nicht verfügbar)"
    echo "[vpn] Routing-Tabelle:"
    ip route 2>/dev/null | sed 's/^/[vpn]   /' || true
    echo "[vpn] ═══════════════════════════════════════════════"
fi

# ── Protokoll-Auswahl ─────────────────────────────────────────────────────────
VPN_PROTOCOL="${VPN_PROTOCOL:-openvpn}"
case "$VPN_PROTOCOL" in
    openvpn|wireguard) ;;
    *) die "VPN_PROTOCOL muss 'openvpn' oder 'wireguard' sein, erhalten: $VPN_PROTOCOL" ;;
esac
log "Protokoll: $VPN_PROTOCOL"

# ─────────────────────────────────────────────────────────────────────────────
# OpenVPN Setup
# ─────────────────────────────────────────────────────────────────────────────
setup_openvpn() {
    CONFIG_PATH="$CREDS_DIR/config.ovpn"
    AUTH_PATH="$CREDS_DIR/auth.txt"

    if [ -n "$VPN_CONFIG_BASE64" ]; then
        echo "$VPN_CONFIG_BASE64" | base64 -d > "$CONFIG_PATH"
        log "Konfiguration aus VPN_CONFIG_BASE64 geladen"

    elif [ -n "$VPN_CONFIG_FILE" ] && [ -f "$VPN_CONFIG_FILE" ]; then
        if [ "$VPN_CONFIG_FILE" != "$CONFIG_PATH" ]; then
            cp "$VPN_CONFIG_FILE" "$CONFIG_PATH"
            log "Konfiguration von $VPN_CONFIG_FILE nach $CONFIG_PATH kopiert"
        else
            log "Konfiguration liegt bereits an $CONFIG_PATH"
        fi

    elif [ -n "$VPN_SERVER" ]; then
        : "${VPN_USER:?VPN_USER ist erforderlich wenn VPN_SERVER genutzt wird}"
        : "${VPN_PASS:?VPN_PASS ist erforderlich wenn VPN_SERVER genutzt wird}"
        if [ -z "$VPN_CA_CERT_BASE64" ]; then
            die "VPN_CA_CERT_BASE64 ist erforderlich wenn VPN_SERVER genutzt wird"
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
        die "Setze VPN_CONFIG_BASE64, VPN_CONFIG_FILE oder VPN_SERVER + VPN_CA_CERT_BASE64"
    fi

    if [ -n "$VPN_USER" ] && [ -n "$VPN_PASS" ]; then
        printf '%s\n%s\n' "${VPN_USER}" "${VPN_PASS}" > "$AUTH_PATH"
        chmod 600 "$AUTH_PATH" 2>/dev/null || true
        if ! grep -q "^auth-user-pass" "$CONFIG_PATH"; then
            echo "auth-user-pass $AUTH_PATH" >> "$CONFIG_PATH"
        else
            sed -i "s|^auth-user-pass.*|auth-user-pass $AUTH_PATH|" "$CONFIG_PATH"
        fi
        log "auth-user-pass aus VPN_USER/VPN_PASS gesetzt"
    else
        log "VPN_USER/VPN_PASS nicht gesetzt — Credentials aus Config-File"
    fi

    debug "OpenVPN-Config (ohne Credentials):"
    [ "$LOG_LEVEL" = "debug" ] && grep -v "auth-user-pass\|password\|pass" "$CONFIG_PATH" | sed 's/^/[vpn]   /' || true

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
        log "WG-Konfig aus WG_CONFIG_BASE64 geladen"
    elif [ -n "$WG_CONFIG_FILE" ] && [ -f "$WG_CONFIG_FILE" ]; then
        if [ "$WG_CONFIG_FILE" != "$WG_CONF" ]; then
            cp "$WG_CONFIG_FILE" "$WG_CONF"
        fi
        log "WG-Konfig von $WG_CONFIG_FILE geladen"
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
        log "WG-Konfig aus Env-Vars generiert"
    fi

    chmod 600 "$WG_CONF" 2>/dev/null || true
    VPN_SERVERS=$(grep "^Endpoint" "$WG_CONF" | sed 's/.*=[[:space:]]*//' | sed 's/:[0-9]*$//' | tr -d '[]' | sort -u)
    VPN_IFACE="wg0"
}

# ─────────────────────────────────────────────────────────────────────────────
# Hostname → IP auflösen (vor DROP-Policy!)
# ─────────────────────────────────────────────────────────────────────────────
resolve_servers() {
    RESOLVED=""
    for server in $VPN_SERVERS; do
        ip=$(getent hosts "$server" 2>/dev/null | awk '{print $1}' | head -1)
        if [ -n "$ip" ]; then
            log "VPN-Server aufgelöst: $server → $ip"
            RESOLVED="$RESOLVED $ip"
        else
            log "WARN: Konnte $server nicht auflösen — nutze Hostname direkt"
            RESOLVED="$RESOLVED $server"
        fi
    done
    VPN_SERVERS="$RESOLVED"
}

# ─────────────────────────────────────────────────────────────────────────────
# Kill-Switch
# ─────────────────────────────────────────────────────────────────────────────
apply_killswitch() {
    log "Aktiviere Kill-Switch (IPv4 + IPv6)..."
    IFACE_PATTERN="${VPN_IFACE%%[0-9]*}+"

    DOCKER_IFACE=$(ip route show default 2>/dev/null | awk '{print $5}' | head -1)
    DOCKER_IFACE="${DOCKER_IFACE:-eth0}"
    # Save gateway before VPN routes override the default
    DOCKER_GW=$(ip route show default 2>/dev/null | awk '{print $3}' | head -1)
    WEB_PORT="${PORT:-5000}"

    debug "VPN-Interface-Pattern : $IFACE_PATTERN"
    debug "Docker-Bridge-Interface: $DOCKER_IFACE"
    debug "Web-UI-Port            : $WEB_PORT"
    debug "VPN-Server-IPs         : $VPN_SERVERS"

    # ── IPv4 ──────────────────────────────────────────────────────────────────
    iptables -F INPUT  2>/dev/null || true
    iptables -F OUTPUT 2>/dev/null || true
    iptables -P INPUT  DROP
    iptables -P OUTPUT DROP

    iptables -A INPUT  -i lo -j ACCEPT
    iptables -A OUTPUT -o lo -j ACCEPT
    # VPN tunnel: alle Pakete auf dem Tunnel-Interface erlauben (kein conntrack nötig)
    iptables -A INPUT  -i "$IFACE_PATTERN" -j ACCEPT
    iptables -A OUTPUT -o "$IFACE_PATTERN" -j ACCEPT
    # Web-UI: eingehende Anfragen + ausgehende Antworten (stateless, kein conntrack)
    iptables -A INPUT  -p tcp --dport "$WEB_PORT" -j ACCEPT
    iptables -A OUTPUT -p tcp --sport "$WEB_PORT" -j ACCEPT
    log "Web-UI Port $WEB_PORT: INPUT+OUTPUT ACCEPT (stateless, kein conntrack)"

    for target in $VPN_SERVERS; do
        iptables -A OUTPUT -d "$target" -j ACCEPT
        iptables -A INPUT  -s "$target" -j ACCEPT
        debug "Whitelist: $target"
    done

    if [ -n "$ALLOW_SUBNETS" ]; then
        for subnet in $(echo "$ALLOW_SUBNETS" | tr ',' ' '); do
            [ -z "$subnet" ] && continue
            iptables -A OUTPUT -d "$subnet" -j ACCEPT
            iptables -A INPUT  -s "$subnet" -j ACCEPT
            log "Extra-Subnet freigegeben: $subnet"
        done
    fi

    # ── IPv6 ──────────────────────────────────────────────────────────────────
    if command -v ip6tables > /dev/null 2>&1; then
        ip6tables -F INPUT  2>/dev/null || true
        ip6tables -F OUTPUT 2>/dev/null || true
        ip6tables -P INPUT  DROP
        ip6tables -P OUTPUT DROP
        ip6tables -A INPUT  -i lo -j ACCEPT
        ip6tables -A OUTPUT -o lo -j ACCEPT
        ip6tables -A INPUT  -i "$IFACE_PATTERN" -j ACCEPT
        ip6tables -A OUTPUT -o "$IFACE_PATTERN" -j ACCEPT
        log "IPv6 Kill-Switch aktiv"
    else
        log "WARN: ip6tables nicht verfügbar — IPv6 nicht geblockt"
    fi

    # ── Debug: komplette iptables-Regeln ausgeben ─────────────────────────────
    if [ "$LOG_LEVEL" = "debug" ]; then
        echo "[vpn] ══════════════ iptables -L -v -n ══════════════"
        iptables -L -v -n 2>/dev/null | sed 's/^/[vpn]   /' || true
        echo "[vpn] ═══════════════════════════════════════════════"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Return-Routing: Antwortpakete vom eth0-Interface gehen zurück über eth0,
# nicht in den VPN-Tunnel. Notwendig weil OpenVPN 0.0.0.0/1 + 128.0.0.0/1
# via tun0 injiziert und sonst alle Docker-Port-Mapping-Antworten im Tunnel
# verschwinden.
# ─────────────────────────────────────────────────────────────────────────────
setup_return_routing() {
    ETH0_IP=$(ip -4 addr show "$DOCKER_IFACE" 2>/dev/null \
        | awk '/inet /{print $2}' | cut -d/ -f1 | head -1)

    if [ -z "$DOCKER_GW" ] || [ -z "$ETH0_IP" ]; then
        log "WARN: Return-Routing nicht eingerichtet (GW='$DOCKER_GW', IP='$ETH0_IP')"
        return
    fi

    ip route add table 200 default via "$DOCKER_GW" dev "$DOCKER_IFACE" 2>/dev/null || true
    ip rule add from "$ETH0_IP" table 200 priority 100 2>/dev/null || true
    log "Return-Routing: Pakete von $ETH0_IP → $DOCKER_GW ($DOCKER_IFACE)"
}

# ─────────────────────────────────────────────────────────────────────────────
# VPN starten
# ─────────────────────────────────────────────────────────────────────────────
start_vpn() {
    case "$VPN_PROTOCOL" in
        openvpn)
            log "Starte OpenVPN..."
            openvpn \
                --config "$CREDS_DIR/config.ovpn" \
                --auth-nocache \
                --log /vpn/openvpn.log \
                --writepid /vpn/openvpn.pid \
                --daemon
            ;;
        wireguard)
            log "Starte WireGuard..."
            mkdir -p /etc/wireguard
            cp "$CREDS_DIR/wg0.conf" /etc/wireguard/wg0.conf
            chmod 600 /etc/wireguard/wg0.conf
            wg-quick up wg0 2>&1 | sed 's/^/[wg]   /'
            ;;
    esac
}

# ─────────────────────────────────────────────────────────────────────────────
# Auf Tunnel warten
# ─────────────────────────────────────────────────────────────────────────────
wait_for_tunnel() {
    log "Warte auf Interface $VPN_IFACE (max ${VPN_CONNECT_TIMEOUT:-30}s)..."
    max="${VPN_CONNECT_TIMEOUT:-30}"
    i=0
    while [ "$i" -lt "$max" ]; do
        if ip link show "$VPN_IFACE" > /dev/null 2>&1; then
            log "Tunnel $VPN_IFACE ist online"
            if [ "$LOG_LEVEL" = "debug" ]; then
                echo "[vpn] ══════════════ Netzwerk nach VPN-Start ══════════"
                ip addr 2>/dev/null | sed 's/^/[vpn]   /' || true
                echo "[vpn] ---"
                ip route 2>/dev/null | sed 's/^/[vpn]   /' || true
                echo "[vpn] ═══════════════════════════════════════════════"
            fi
            return 0
        fi
        # OpenVPN-Fehler früh erkennen
        if [ "$VPN_PROTOCOL" = "openvpn" ] && [ -f /vpn/openvpn.log ]; then
            if grep -q "AUTH_FAILED\|TLS Error\|Connection refused\|SIGTERM" /vpn/openvpn.log 2>/dev/null; then
                err "OpenVPN meldet Fehler — Logs:"
                cat /vpn/openvpn.log >&2
                die "OpenVPN-Verbindung fehlgeschlagen"
            fi
        fi
        i=$((i + 1))
        sleep 1
    done

    err "Tunnel kam nicht innerhalb von ${max}s hoch"
    [ -f /vpn/openvpn.log ] && cat /vpn/openvpn.log >&2
    die "Timeout"
}

# ─────────────────────────────────────────────────────────────────────────────
# App starten und überwachen
# ─────────────────────────────────────────────────────────────────────────────
start_app() {
    # Default falls APP_CMD nicht in der .env gesetzt ist
    APP_CMD="${APP_CMD:-python /app/app.py}"
    WEB_PORT="${PORT:-5000}"

    log "Starte App: $APP_CMD"

    # Python-Output ungepuffert damit Logs sofort in docker logs erscheinen
    export PYTHONUNBUFFERED=1

    sh -c "$APP_CMD" 2>&1 &
    APP_PID=$!
    log "App PID: $APP_PID"

    # Warten bis Flask hochgefahren ist (max 15s)
    i=0
    while [ "$i" -lt 15 ]; do
        sleep 1
        # Prozess noch am Leben?
        if ! kill -0 "$APP_PID" 2>/dev/null; then
            err "App-Prozess (PID $APP_PID) ist sofort abgestürzt"
            err "Mögliche Ursachen: fehlendes Modul, falscher APP_CMD, Permission-Error"
            die "App-Start fehlgeschlagen — prüfe docker logs"
        fi
        # Port erreichbar?
        if nc -z 127.0.0.1 "$WEB_PORT" 2>/dev/null; then
            log "App antwortet auf Port $WEB_PORT nach ${i}s ✓"
            return 0
        fi
        i=$((i + 1))
    done

    # Prozess läuft, aber Port noch nicht offen — trotzdem weitermachen
    if kill -0 "$APP_PID" 2>/dev/null; then
        log "WARN: App (PID $APP_PID) läuft, Port $WEB_PORT noch nicht offen nach 15s"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Tunnel + App-Prozess überwachen
# ─────────────────────────────────────────────────────────────────────────────
monitor_tunnel() {
    log "Monitoring aktiv (Intervall: ${VPN_CHECK_INTERVAL:-10}s)..."
    while true; do
        sleep "${VPN_CHECK_INTERVAL:-10}"

        # Tunnel-Check
        if ! ip link show "$VPN_IFACE" > /dev/null 2>&1; then
            err "Tunnel $VPN_IFACE nicht mehr aktiv — Container wird beendet"
            [ -n "${APP_PID:-}" ] && kill "$APP_PID" 2>/dev/null || true
            exit 1
        fi

        # OpenVPN-Prozess-Check
        if [ "$VPN_PROTOCOL" = "openvpn" ] && [ -f /vpn/openvpn.pid ]; then
            OVPN_PID=$(cat /vpn/openvpn.pid)
            if ! kill -0 "$OVPN_PID" 2>/dev/null; then
                err "OpenVPN-Prozess (PID $OVPN_PID) tot — Container wird beendet"
                [ -n "${APP_PID:-}" ] && kill "$APP_PID" 2>/dev/null || true
                exit 1
            fi
            debug "OpenVPN PID $OVPN_PID lebt"
        fi

        # App-Prozess-Check
        if [ -n "${APP_PID:-}" ] && ! kill -0 "$APP_PID" 2>/dev/null; then
            err "App-Prozess (PID $APP_PID) tot — Container wird beendet"
            exit 1
        fi

        # Optionaler Ping-Check durch den Tunnel
        if [ -n "$VPN_PING_HOST" ]; then
            if ! ping -c 1 -W 5 -I "$VPN_IFACE" "$VPN_PING_HOST" > /dev/null 2>&1; then
                err "Ping $VPN_PING_HOST via $VPN_IFACE fehlgeschlagen — Container wird beendet"
                [ -n "${APP_PID:-}" ] && kill "$APP_PID" 2>/dev/null || true
                exit 1
            fi
            debug "Ping $VPN_PING_HOST OK"
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
setup_return_routing
start_app
monitor_tunnel
