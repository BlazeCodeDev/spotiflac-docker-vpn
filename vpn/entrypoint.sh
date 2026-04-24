#!/bin/sh
set -e

log()  { echo "[vpn] $*"; }
err()  { echo "[vpn] ERROR: $*" >&2; }
die()  { err "$*"; exit 1; }

CREDS_DIR=/vpn
mkdir -p "$CREDS_DIR"
chmod 700 "$CREDS_DIR"

# ── Protocol selection ────────────────────────────────────────────────────────
VPN_PROTOCOL="${VPN_PROTOCOL:-openvpn}"
case "$VPN_PROTOCOL" in
    openvpn|wireguard) ;;
    *) die "VPN_PROTOCOL must be 'openvpn' or 'wireguard', got: $VPN_PROTOCOL" ;;
esac
log "Protocol: $VPN_PROTOCOL"

# ─────────────────────────────────────────────────────────────────────────────
# OpenVPN setup
# ─────────────────────────────────────────────────────────────────────────────
setup_openvpn() {
    : "${VPN_USER:?VPN_USER is required for OpenVPN}"
    : "${VPN_PASS:?VPN_PASS is required for OpenVPN}"

    CONFIG_PATH="$CREDS_DIR/config.ovpn"
    AUTH_PATH="$CREDS_DIR/auth.txt"

    if [ -n "$VPN_CONFIG_BASE64" ]; then
        echo "$VPN_CONFIG_BASE64" | base64 -d > "$CONFIG_PATH"
        log "Config from VPN_CONFIG_BASE64"

    elif [ -n "$VPN_CONFIG_FILE" ] && [ -f "$VPN_CONFIG_FILE" ]; then
        cp "$VPN_CONFIG_FILE" "$CONFIG_PATH"
        log "Config from VPN_CONFIG_FILE: $VPN_CONFIG_FILE"

    elif [ -n "$VPN_SERVER" ]; then
        # Require a real CA cert — no fake/empty CA allowed
        if [ -z "$VPN_CA_CERT_BASE64" ]; then
            die "VPN_CA_CERT_BASE64 is required when using VPN_SERVER. Get the CA cert from your VPN provider."
        fi
        VPN_PORT="${VPN_PORT:-1194}"
        VPN_TRANSPORT="${VPN_TRANSPORT:-udp}"
        log "Generating config for $VPN_SERVER:$VPN_PORT ($VPN_TRANSPORT)${VPN_COUNTRY:+ country=$VPN_COUNTRY}"

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
            chmod 600 "$CREDS_DIR/tls.key"
            echo "tls-auth $CREDS_DIR/tls.key 1" >> "$CONFIG_PATH"
        fi
    else
        die "Provide one of: VPN_CONFIG_BASE64, VPN_CONFIG_FILE, or VPN_SERVER + VPN_CA_CERT_BASE64"
    fi

    printf '%s\n%s\n' "${VPN_USER}" "${VPN_PASS}" > "$AUTH_PATH"
    chmod 600 "$AUTH_PATH"

    if ! grep -q "^auth-user-pass" "$CONFIG_PATH"; then
        echo "auth-user-pass $AUTH_PATH" >> "$CONFIG_PATH"
    else
        sed -i "s|^auth-user-pass.*|auth-user-pass $AUTH_PATH|" "$CONFIG_PATH"
    fi

    VPN_SERVERS=$(grep "^remote " "$CONFIG_PATH" | awk '{print $2}' | sort -u)
    VPN_IFACE="tun0"
}

# ─────────────────────────────────────────────────────────────────────────────
# WireGuard setup
# ─────────────────────────────────────────────────────────────────────────────
setup_wireguard() {
    WG_CONF="$CREDS_DIR/wg0.conf"

    if [ -n "$WG_CONFIG_BASE64" ]; then
        echo "$WG_CONFIG_BASE64" | base64 -d > "$WG_CONF"
    elif [ -n "$WG_CONFIG_FILE" ] && [ -f "$WG_CONFIG_FILE" ]; then
        cp "$WG_CONFIG_FILE" "$WG_CONF"
    else
        : "${WG_PRIVATE_KEY:?WG_PRIVATE_KEY required (or provide WG_CONFIG_FILE / WG_CONFIG_BASE64)}"
        : "${WG_ADDRESS:?WG_ADDRESS required (e.g. 10.0.0.2/32)}"
        : "${WG_SERVER_PUBLIC_KEY:?WG_SERVER_PUBLIC_KEY required}"
        : "${WG_ENDPOINT:?WG_ENDPOINT required (e.g. vpn.example.com:51820)}"

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

    chmod 600 "$WG_CONF"

    # Strip brackets from IPv6 endpoints like [::1]:51820 → ::1
    VPN_SERVERS=$(grep "^Endpoint" "$WG_CONF" \
        | sed 's/.*=[[:space:]]*//' \
        | sed 's/:[0-9]*$//' \
        | tr -d '[]' \
        | sort -u)

    VPN_IFACE="wg0"
}

# ─────────────────────────────────────────────────────────────────────────────
# Resolve VPN server hostnames to IPs BEFORE applying DROP policy
# ─────────────────────────────────────────────────────────────────────────────
resolve_servers() {
    RESOLVED=""
    for server in $VPN_SERVERS; do
        ip=$(getent hosts "$server" 2>/dev/null | awk '{print $1}' | head -1)
        if [ -n "$ip" ]; then
            log "Resolved VPN server: $server → $ip"
            RESOLVED="$RESOLVED $ip"
        else
            log "WARNING: Could not resolve $server — using hostname directly"
            RESOLVED="$RESOLVED $server"
        fi
    done
    VPN_SERVERS="$RESOLVED"
}

# ─────────────────────────────────────────────────────────────────────────────
# iptables kill-switch — IPv4 + IPv6
# ─────────────────────────────────────────────────────────────────────────────
apply_killswitch() {
    log "Applying kill-switch (IPv4 + IPv6)..."
    IFACE_PATTERN="${VPN_IFACE%%[0-9]*}+"

    # ── IPv4 ──────────────────────────────────────────────────────────────────
    iptables -F INPUT   2>/dev/null || true
    iptables -F OUTPUT  2>/dev/null || true
    iptables -F FORWARD 2>/dev/null || true
    iptables -P INPUT   DROP
    iptables -P OUTPUT  DROP
    iptables -P FORWARD DROP

    iptables -A INPUT  -i lo              -j ACCEPT
    iptables -A OUTPUT -o lo              -j ACCEPT
    iptables -A INPUT  -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    iptables -A INPUT  -i "$IFACE_PATTERN" -j ACCEPT
    iptables -A OUTPUT -o "$IFACE_PATTERN" -j ACCEPT

    for target in $VPN_SERVERS; do
        log "Whitelisting VPN server: $target"
        iptables -A OUTPUT -d "$target" -j ACCEPT
        iptables -A INPUT  -s "$target" -j ACCEPT
    done

    if [ -n "$ALLOW_SUBNETS" ]; then
        for subnet in $(echo "$ALLOW_SUBNETS" | tr ',' ' '); do
            [ -z "$subnet" ] && continue
            log "Whitelisting extra subnet: $subnet"
            iptables -A OUTPUT -d "$subnet" -j ACCEPT
            iptables -A INPUT  -s "$subnet" -j ACCEPT
        done
    fi

    # ── IPv6 — block everything except loopback and VPN tunnel ────────────────
    if command -v ip6tables > /dev/null 2>&1; then
        ip6tables -F INPUT   2>/dev/null || true
        ip6tables -F OUTPUT  2>/dev/null || true
        ip6tables -F FORWARD 2>/dev/null || true
        ip6tables -P INPUT   DROP
        ip6tables -P OUTPUT  DROP
        ip6tables -P FORWARD DROP

        ip6tables -A INPUT  -i lo              -j ACCEPT
        ip6tables -A OUTPUT -o lo              -j ACCEPT
        ip6tables -A INPUT  -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
        ip6tables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
        ip6tables -A INPUT  -i "$IFACE_PATTERN" -j ACCEPT
        ip6tables -A OUTPUT -o "$IFACE_PATTERN" -j ACCEPT
        log "IPv6 kill-switch active"
    else
        log "WARNING: ip6tables not available — IPv6 traffic is NOT blocked"
    fi

    log "Kill-switch active"
}

# ─────────────────────────────────────────────────────────────────────────────
# Start VPN
# ─────────────────────────────────────────────────────────────────────────────
start_vpn() {
    case "$VPN_PROTOCOL" in
        openvpn)
            log "Starting OpenVPN..."
            openvpn \
                --config "$CREDS_DIR/config.ovpn" \
                --auth-nocache \
                --log /vpn/openvpn.log \
                --writepid /vpn/openvpn.pid \
                --daemon
            ;;
        wireguard)
            log "Starting WireGuard..."
            mkdir -p /etc/wireguard
            cp "$CREDS_DIR/wg0.conf" /etc/wireguard/wg0.conf
            chmod 600 /etc/wireguard/wg0.conf
            wg-quick up wg0 2>&1 | while read -r line; do log "[wg] $line"; done
            ;;
    esac
}

# ─────────────────────────────────────────────────────────────────────────────
# Wait for tunnel interface
# ─────────────────────────────────────────────────────────────────────────────
wait_for_tunnel() {
    log "Waiting for $VPN_IFACE..."
    i=0
    max="${VPN_CONNECT_TIMEOUT:-30}"
    while [ "$i" -lt "$max" ]; do
        if ip link show "$VPN_IFACE" > /dev/null 2>&1; then
            log "Tunnel $VPN_IFACE is up"
            return 0
        fi
        i=$((i + 1))
        sleep 1
    done
    err "Tunnel did not come up within ${max}s"
    [ -f /vpn/openvpn.log ] && cat /vpn/openvpn.log >&2
    return 1
}

# ─────────────────────────────────────────────────────────────────────────────
# Monitor — kill container if VPN drops or OpenVPN process dies
# ─────────────────────────────────────────────────────────────────────────────
monitor_tunnel() {
    log "Monitoring $VPN_IFACE every ${VPN_CHECK_INTERVAL:-10}s..."
    while true; do
        sleep "${VPN_CHECK_INTERVAL:-10}"

        # Interface check
        if ! ip link show "$VPN_IFACE" > /dev/null 2>&1; then
            err "Tunnel $VPN_IFACE dropped — shutting down"
            [ -n "${APP_PID:-}" ] && kill "$APP_PID" 2>/dev/null || true
            exit 1
        fi

        # OpenVPN process check (not applicable for WireGuard)
        if [ "$VPN_PROTOCOL" = "openvpn" ] && [ -f /vpn/openvpn.pid ]; then
            OVPN_PID=$(cat /vpn/openvpn.pid)
            if ! kill -0 "$OVPN_PID" 2>/dev/null; then
                err "OpenVPN process ($OVPN_PID) died — shutting down"
                [ -n "${APP_PID:-}" ] && kill "$APP_PID" 2>/dev/null || true
                exit 1
            fi
        fi

        # Optional connectivity check
        if [ -n "$VPN_PING_HOST" ]; then
            if ! ping -c 1 -W 5 -I "$VPN_IFACE" "$VPN_PING_HOST" > /dev/null 2>&1; then
                err "Ping check via $VPN_IFACE to $VPN_PING_HOST failed — shutting down"
                [ -n "${APP_PID:-}" ] && kill "$APP_PID" 2>/dev/null || true
                exit 1
            fi
        fi
    done
}

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
case "$VPN_PROTOCOL" in
    openvpn)   setup_openvpn  ;;
    wireguard) setup_wireguard ;;
esac

resolve_servers      # must happen before apply_killswitch (DNS needs to work)
apply_killswitch
start_vpn
wait_for_tunnel

if [ -n "$APP_CMD" ]; then
    log "Starting application: $APP_CMD"
    sh -c "$APP_CMD" &   # sh -c instead of eval
    APP_PID=$!
fi

monitor_tunnel
