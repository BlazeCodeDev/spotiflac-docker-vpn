#!/bin/sh
set -e

# ── Log level ─────────────────────────────────────────────────────────────────
# LOG_LEVEL=info  (default) — standard logging
# LOG_LEVEL=debug           — iptables, network, env vars, app status
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
    log "WARN: chmod 700 on $CREDS_DIR failed — volume may be read-only"
fi

# ── Debug: env vars and system info at startup ────────────────────────────────
if [ "$LOG_LEVEL" = "debug" ]; then
    echo "[vpn] ══════════════════ DEBUG START ══════════════════"
    echo "[vpn] Kernel : $(uname -r)"
    echo "[vpn] Env vars (secrets excluded):"
    env | grep -v -i 'PASS\|KEY\|TOKEN\|SECRET\|BASE64' | sort | sed 's/^/[vpn]   /'
    echo "[vpn] Network interfaces:"
    ip addr 2>/dev/null | sed 's/^/[vpn]   /' || echo "[vpn]   (ip not available)"
    echo "[vpn] Routing table:"
    ip route 2>/dev/null | sed 's/^/[vpn]   /' || true
    echo "[vpn] ═══════════════════════════════════════════════"
fi

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
    CONFIG_PATH="$CREDS_DIR/config.ovpn"
    AUTH_PATH="$CREDS_DIR/auth.txt"

    if [ -n "$VPN_CONFIG_BASE64" ]; then
        echo "$VPN_CONFIG_BASE64" | base64 -d > "$CONFIG_PATH"
        log "Config loaded from VPN_CONFIG_BASE64"

    elif [ -n "$VPN_CONFIG_FILE" ] && [ -f "$VPN_CONFIG_FILE" ]; then
        if [ "$VPN_CONFIG_FILE" != "$CONFIG_PATH" ]; then
            cp "$VPN_CONFIG_FILE" "$CONFIG_PATH"
            log "Config copied from $VPN_CONFIG_FILE to $CONFIG_PATH"
        else
            log "Config already at $CONFIG_PATH"
        fi

    elif [ -n "$VPN_SERVER" ]; then
        : "${VPN_USER:?VPN_USER is required when using VPN_SERVER}"
        : "${VPN_PASS:?VPN_PASS is required when using VPN_SERVER}"
        if [ -z "$VPN_CA_CERT_BASE64" ]; then
            die "VPN_CA_CERT_BASE64 is required when using VPN_SERVER"
        fi
        VPN_PORT="${VPN_PORT:-1194}"
        VPN_TRANSPORT="${VPN_TRANSPORT:-udp}"
        log "Generating config for $VPN_SERVER:$VPN_PORT ($VPN_TRANSPORT)"
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
redirect-gateway def1 bypass-dhcp
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
        die "Set VPN_CONFIG_BASE64, VPN_CONFIG_FILE, or VPN_SERVER + VPN_CA_CERT_BASE64"
    fi

    if [ -n "$VPN_USER" ] && [ -n "$VPN_PASS" ]; then
        printf '%s\n%s\n' "${VPN_USER}" "${VPN_PASS}" > "$AUTH_PATH"
        chmod 600 "$AUTH_PATH" 2>/dev/null || true
        if ! grep -q "^auth-user-pass" "$CONFIG_PATH"; then
            echo "auth-user-pass $AUTH_PATH" >> "$CONFIG_PATH"
        else
            sed -i "s|^auth-user-pass.*|auth-user-pass $AUTH_PATH|" "$CONFIG_PATH"
        fi
        log "auth-user-pass set from VPN_USER/VPN_PASS"
    else
        log "VPN_USER/VPN_PASS not set — credentials from config file"
    fi

    debug "OpenVPN config (credentials excluded):"
    [ "$LOG_LEVEL" = "debug" ] && grep -v "auth-user-pass\|password\|pass" "$CONFIG_PATH" | sed 's/^/[vpn]   /' || true

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
        log "WireGuard config loaded from WG_CONFIG_BASE64"
    elif [ -n "$WG_CONFIG_FILE" ] && [ -f "$WG_CONFIG_FILE" ]; then
        if [ "$WG_CONFIG_FILE" != "$WG_CONF" ]; then
            cp "$WG_CONFIG_FILE" "$WG_CONF"
        fi
        log "WireGuard config loaded from $WG_CONFIG_FILE"
    else
        : "${WG_PRIVATE_KEY:?WG_PRIVATE_KEY required}"
        : "${WG_ADDRESS:?WG_ADDRESS required}"
        : "${WG_SERVER_PUBLIC_KEY:?WG_SERVER_PUBLIC_KEY required}"
        : "${WG_ENDPOINT:?WG_ENDPOINT required}"
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
        log "WireGuard config generated from env vars"
    fi

    chmod 600 "$WG_CONF" 2>/dev/null || true
    VPN_SERVERS=$(grep "^Endpoint" "$WG_CONF" | sed 's/.*=[[:space:]]*//' | sed 's/:[0-9]*$//' | tr -d '[]' | sort -u)
    VPN_IFACE="wg0"
}

# ─────────────────────────────────────────────────────────────────────────────
# Resolve hostnames to IPs (before DROP policy is applied!)
# ─────────────────────────────────────────────────────────────────────────────
resolve_servers() {
    RESOLVED=""
    for server in $VPN_SERVERS; do
        ip=$(getent hosts "$server" 2>/dev/null | awk '{print $1}' | head -1)
        if [ -n "$ip" ]; then
            log "VPN server resolved: $server → $ip"
            RESOLVED="$RESOLVED $ip"
        else
            log "WARN: Could not resolve $server — using hostname directly"
            RESOLVED="$RESOLVED $server"
        fi
    done
    VPN_SERVERS="$RESOLVED"
}

# ─────────────────────────────────────────────────────────────────────────────
# Kill-switch
# ─────────────────────────────────────────────────────────────────────────────
apply_killswitch() {
    log "Activating kill-switch (IPv4 + IPv6)..."
    IFACE_PATTERN="${VPN_IFACE%%[0-9]*}+"

    DOCKER_IFACE=$(ip route show default 2>/dev/null | awk '{print $5}' | head -1)
    DOCKER_IFACE="${DOCKER_IFACE:-eth0}"
    # Save gateway before VPN routes override the default
    DOCKER_GW=$(ip route show default 2>/dev/null | awk '{print $3}' | head -1)
    WEB_PORT="${PORT:-5000}"

    debug "VPN interface pattern  : $IFACE_PATTERN"
    debug "Docker bridge interface: $DOCKER_IFACE"
    debug "Web UI port            : $WEB_PORT"
    debug "VPN server IPs         : $VPN_SERVERS"

    # ── IPv4 ──────────────────────────────────────────────────────────────────
    iptables -F INPUT  2>/dev/null || true
    iptables -F OUTPUT 2>/dev/null || true
    iptables -P INPUT  DROP
    iptables -P OUTPUT DROP

    iptables -A INPUT  -i lo -j ACCEPT
    iptables -A OUTPUT -o lo -j ACCEPT
    # VPN tunnel: allow all packets on the tunnel interface (no conntrack needed)
    iptables -A INPUT  -i "$IFACE_PATTERN" -j ACCEPT
    iptables -A OUTPUT -o "$IFACE_PATTERN" -j ACCEPT
    # Web UI: allow only on the Docker bridge interface, not the VPN tunnel
    iptables -A INPUT  -i "$DOCKER_IFACE" -p tcp --dport "$WEB_PORT" -j ACCEPT
    iptables -A OUTPUT -o "$DOCKER_IFACE" -p tcp --sport "$WEB_PORT" -j ACCEPT
    log "Web UI port $WEB_PORT: INPUT+OUTPUT ACCEPT on $DOCKER_IFACE only"

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
            log "Extra subnet allowed: $subnet"
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
        log "IPv6 kill-switch active"
    else
        log "WARN: ip6tables not available — IPv6 not blocked"
    fi

    # ── Debug: dump full iptables rules ──────────────────────────────────────
    if [ "$LOG_LEVEL" = "debug" ]; then
        echo "[vpn] ══════════════ iptables -L -v -n ══════════════"
        iptables -L -v -n 2>/dev/null | sed 's/^/[vpn]   /' || true
        echo "[vpn] ═══════════════════════════════════════════════"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Return routing: reply packets from eth0 go back via eth0, not into the VPN
# tunnel. Required because OpenVPN injects 0.0.0.0/1 + 128.0.0.0/1 via tun0,
# which would otherwise swallow all Docker port-mapped replies.
# ─────────────────────────────────────────────────────────────────────────────
setup_return_routing() {
    ETH0_IP=$(ip -4 addr show "$DOCKER_IFACE" 2>/dev/null \
        | awk '/inet /{print $2}' | cut -d/ -f1 | head -1)

    if [ -z "$DOCKER_GW" ] || [ -z "$ETH0_IP" ]; then
        log "WARN: Return routing not configured (GW='$DOCKER_GW', IP='$ETH0_IP')"
        return
    fi

    ip route add table 200 default via "$DOCKER_GW" dev "$DOCKER_IFACE" 2>/dev/null || true
    ip rule add from "$ETH0_IP" table 200 priority 100 2>/dev/null || true
    log "Return routing: packets from $ETH0_IP → $DOCKER_GW ($DOCKER_IFACE)"
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
            wg-quick up wg0 2>&1 | sed 's/^/[wg]   /'
            ;;
    esac
}

# ─────────────────────────────────────────────────────────────────────────────
# Wait for tunnel
# ─────────────────────────────────────────────────────────────────────────────
wait_for_tunnel() {
    log "Waiting for interface $VPN_IFACE (max ${VPN_CONNECT_TIMEOUT:-30}s)..."
    max="${VPN_CONNECT_TIMEOUT:-30}"
    i=0
    while [ "$i" -lt "$max" ]; do
        if ip link show "$VPN_IFACE" > /dev/null 2>&1; then
            date +%s > /vpn/tunnel_up_since
            log "Tunnel $VPN_IFACE is up"
            if [ "$LOG_LEVEL" = "debug" ]; then
                echo "[vpn] ══════════════ Network after VPN start ══════════"
                ip addr 2>/dev/null | sed 's/^/[vpn]   /' || true
                echo "[vpn] ---"
                ip route 2>/dev/null | sed 's/^/[vpn]   /' || true
                echo "[vpn] ═══════════════════════════════════════════════"
            fi
            return 0
        fi
        # Detect OpenVPN errors early
        if [ "$VPN_PROTOCOL" = "openvpn" ] && [ -f /vpn/openvpn.log ]; then
            if grep -q "AUTH_FAILED\|TLS Error\|Connection refused\|SIGTERM" /vpn/openvpn.log 2>/dev/null; then
                err "OpenVPN reported an error — logs:"
                cat /vpn/openvpn.log >&2
                die "OpenVPN connection failed"
            fi
        fi
        i=$((i + 1))
        sleep 1
    done

    err "Tunnel did not come up within ${max}s"
    [ -f /vpn/openvpn.log ] && cat /vpn/openvpn.log >&2
    die "Timeout"
}

# ─────────────────────────────────────────────────────────────────────────────
# Start and monitor the app process
# ─────────────────────────────────────────────────────────────────────────────
start_app() {
    # Default if APP_CMD is not set in .env
    APP_CMD="${APP_CMD:-python /app/app.py}"
    WEB_PORT="${PORT:-5000}"

    log "Starting app: $APP_CMD"

    # Unbuffered Python output so logs appear immediately in docker logs
    export PYTHONUNBUFFERED=1

    sh -c "$APP_CMD" 2>&1 &
    APP_PID=$!
    log "App PID: $APP_PID"

    # Wait for Flask to be ready (max 15s)
    i=0
    while [ "$i" -lt 15 ]; do
        sleep 1
        # Is the process still alive?
        if ! kill -0 "$APP_PID" 2>/dev/null; then
            err "App process (PID $APP_PID) crashed immediately"
            err "Possible causes: missing module, wrong APP_CMD, permission error"
            die "App failed to start — check docker logs"
        fi
        # Is the port reachable?
        if nc -z 127.0.0.1 "$WEB_PORT" 2>/dev/null; then
            log "App responding on port $WEB_PORT after ${i}s"
            return 0
        fi
        i=$((i + 1))
    done

    # Process is running but port not open yet — continue anyway
    if kill -0 "$APP_PID" 2>/dev/null; then
        log "WARN: App (PID $APP_PID) is running but port $WEB_PORT not open after 15s"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Reconnect VPN — restarts the VPN process after a tunnel drop.
# Kill-switch iptables rules are NOT touched; traffic stays blocked throughout.
# Returns 0 on success, 1 if all attempts are exhausted (caller then exits).
# ─────────────────────────────────────────────────────────────────────────────
reconnect_vpn() {
    _max="${VPN_RECONNECT_TRIES:-3}"
    log "Reconnecting VPN (kill-switch stays active, up to $_max attempts)..."
    _try=1
    while [ "$_try" -le "$_max" ]; do
        log "Reconnect attempt $_try/$_max..."
        case "$VPN_PROTOCOL" in
            openvpn)
                if [ -f /vpn/openvpn.pid ]; then
                    kill "$(cat /vpn/openvpn.pid)" 2>/dev/null || true
                    rm -f /vpn/openvpn.pid
                fi
                pkill -f openvpn 2>/dev/null || true
                sleep 2
                rm -f /vpn/openvpn.log
                openvpn \
                    --config "$CREDS_DIR/config.ovpn" \
                    --auth-nocache \
                    --log /vpn/openvpn.log \
                    --writepid /vpn/openvpn.pid \
                    --daemon
                ;;
            wireguard)
                wg-quick down wg0 2>/dev/null || true
                sleep 1
                wg-quick up wg0 2>&1 | sed 's/^/[wg]   /'
                ;;
        esac
        _max_wait="${VPN_CONNECT_TIMEOUT:-30}"
        _w=0
        while [ "$_w" -lt "$_max_wait" ]; do
            if ip link show "$VPN_IFACE" > /dev/null 2>&1; then
                date +%s > /vpn/tunnel_up_since
                log "Tunnel $VPN_IFACE is up (attempt $_try)"
                return 0
            fi
            if [ "$VPN_PROTOCOL" = "openvpn" ] && [ -f /vpn/openvpn.log ]; then
                if grep -q "AUTH_FAILED\|TLS Error\|Connection refused" /vpn/openvpn.log 2>/dev/null; then
                    err "OpenVPN fatal error on attempt $_try"
                    break
                fi
            fi
            _w=$((_w + 1))
            sleep 1
        done
        err "Reconnect attempt $_try/$_max failed"
        _try=$((_try + 1))
    done
    return 1
}

# ─────────────────────────────────────────────────────────────────────────────
# Monitor tunnel and app process
# ─────────────────────────────────────────────────────────────────────────────
monitor_tunnel() {
    log "Monitoring active (interval: ${VPN_CHECK_INTERVAL:-10}s)..."
    while true; do
        sleep "${VPN_CHECK_INTERVAL:-10}"

        # Tunnel check
        if ! ip link show "$VPN_IFACE" > /dev/null 2>&1; then
            err "Tunnel $VPN_IFACE is no longer active — attempting reconnect (kill-switch remains active)"
            if ! reconnect_vpn; then
                err "All reconnect attempts failed — shutting down container"
                [ -n "${APP_PID:-}" ] && kill "$APP_PID" 2>/dev/null || true
                exit 1
            fi
            continue
        fi

        # OpenVPN process check
        if [ "$VPN_PROTOCOL" = "openvpn" ] && [ -f /vpn/openvpn.pid ]; then
            OVPN_PID=$(cat /vpn/openvpn.pid)
            if ! kill -0 "$OVPN_PID" 2>/dev/null; then
                err "OpenVPN process (PID $OVPN_PID) died — attempting reconnect (kill-switch remains active)"
                if ! reconnect_vpn; then
                    err "All reconnect attempts failed — shutting down container"
                    [ -n "${APP_PID:-}" ] && kill "$APP_PID" 2>/dev/null || true
                    exit 1
                fi
            else
                debug "OpenVPN PID $OVPN_PID alive"
            fi
        fi

        # App process check
        if [ -n "${APP_PID:-}" ] && ! kill -0 "$APP_PID" 2>/dev/null; then
            err "App process (PID $APP_PID) died — shutting down container"
            exit 1
        fi

        # Optional ping check through the tunnel
        if [ -n "$VPN_PING_HOST" ]; then
            if ! ping -c 1 -W 5 -I "$VPN_IFACE" "$VPN_PING_HOST" > /dev/null 2>&1; then
                err "Ping $VPN_PING_HOST via $VPN_IFACE failed — attempting reconnect (kill-switch remains active)"
                if ! reconnect_vpn; then
                    err "All reconnect attempts failed — shutting down container"
                    [ -n "${APP_PID:-}" ] && kill "$APP_PID" 2>/dev/null || true
                    exit 1
                fi
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
