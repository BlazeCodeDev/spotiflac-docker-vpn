#!/bin/sh
set -e

# ── Log level ─────────────────────────────────────────────────────────────────
# LOG_LEVEL=info  (default) — standard logging
# LOG_LEVEL=debug           — iptables, network, env vars, app status
LOG_LEVEL="${LOG_LEVEL:-info}"

# Pinned SpotiFLAC version — single source of truth. Kept in lockstep with the
# Dockerfile build install and with patch_spotiflac.py (whose string matches are
# version-specific). Bump only after re-verifying the patches against the new
# release. Set the SPOTIFLAC_VERSION env to override at runtime.
SPOTIFLAC_PINNED="1.3.1"

log()   { echo "[vpn] $(date '+%H:%M:%S') INFO  $*"; }
err()   { echo "[vpn] $(date '+%H:%M:%S') ERROR $*" >&2; }
die()   { err "$*"; exit 1; }
debug() {
    [ "$LOG_LEVEL" = "debug" ] && echo "[vpn] $(date '+%H:%M:%S') DEBUG $*" || true
}

# ─────────────────────────────────────────────────────────────────────────────
CREDS_DIR=/vpn
mkdir -p "$CREDS_DIR"
# 0711: the app runs as an unprivileged user (see run_as_app) and must be able
# to *traverse* /vpn to reach its state subdir, but must NOT be able to list or
# read the VPN credentials living directly in /vpn. Every secret file in here is
# additionally locked to 0600 (see lock_down_creds), so 0711 exposes nothing.
if ! chmod 711 "$CREDS_DIR" 2>/dev/null; then
    log "WARN: chmod 711 on $CREDS_DIR failed — volume may be read-only"
fi

# ── Unprivileged app user + writable app-state dir ─────────────────────────────
# The web app / SpotiFLAC must NOT run as root: this container holds NET_ADMIN,
# so a root-run app (or a compromised SpotiFLAC pulled from PyPI) could flush the
# kill-switch and leak the real IP. Running as an unprivileged user without
# NET_ADMIN makes the kill-switch actually enforcing against the app.
#
# PUID/PGID default to 1000 and SHOULD be set to match the owner of the mounted
# downloads share (e.g. the uid your CIFS/SMB mount uses) — otherwise writing
# downloads may fail with permission errors.
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
APP_USER=appuser
APP_HOME=/home/appuser
APP_STATE_DIR="$CREDS_DIR/state"        # app state, owned by the app user
TUNNEL_UP_FILE="$APP_STATE_DIR/tunnel_up_since"

addgroup -g "$PGID" "$APP_USER" 2>/dev/null || true
adduser -D -H -u "$PUID" -G "$APP_USER" -h "$APP_HOME" "$APP_USER" 2>/dev/null || true
mkdir -p "$APP_STATE_DIR" "$APP_HOME"

# Migrate any pre-existing state from the old /vpn root location into the
# app-owned subdir so upgrades don't lose settings / job history.
for _f in jobs.json settings.json lb_state.json tunnel_up_since; do
    if [ -f "$CREDS_DIR/$_f" ] && [ ! -f "$APP_STATE_DIR/$_f" ]; then
        mv "$CREDS_DIR/$_f" "$APP_STATE_DIR/$_f" 2>/dev/null || true
    fi
done

chown -R "$PUID:$PGID" "$APP_STATE_DIR" "$APP_HOME" 2>/dev/null || true
# Best-effort: give the app user the downloads dir. No-op on CIFS/SMB mounts
# (they honour the server-side uid) — hence the PUID guidance above.
chown "$PUID:$PGID" /downloads 2>/dev/null || true

# Point the app's state files at the writable, app-owned subdir. Exported so the
# unprivileged child process (run_as_app) inherits them.
export STATE_FILE="${STATE_FILE:-$APP_STATE_DIR/jobs.json}"
export SETTINGS_FILE="${SETTINGS_FILE:-$APP_STATE_DIR/settings.json}"
export LB_STATE_FILE="${LB_STATE_FILE:-$APP_STATE_DIR/lb_state.json}"
export VPN_UPTIME_FILE="${VPN_UPTIME_FILE:-$TUNNEL_UP_FILE}"

# Lock every credential file in /vpn to owner-only so the traversable (0711)
# creds dir never exposes secrets to the app user.
lock_down_creds() {
    for _cf in config.ovpn auth.txt ca.crt tls.key wg0.conf openvpn.log; do
        [ -f "$CREDS_DIR/$_cf" ] && chmod 600 "$CREDS_DIR/$_cf" 2>/dev/null || true
    done
    [ -d "$CREDS_DIR/configs" ] && chmod 700 "$CREDS_DIR/configs" 2>/dev/null || true
}

# Run a command as the unprivileged app user (drops root + NET_ADMIN). HOME is
# injected explicitly because su-exec does not set it, and SpotiFLAC writes its
# cache under $HOME/.cache.
run_as_app() {
    su-exec "$PUID:$PGID" env "HOME=$APP_HOME" "$@"
}

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
# ─────────────────────────────────────────────────────────────────────────────
# Config rotation helpers (OpenVPN only)
# Place multiple .ovpn files in /vpn/configs/ to enable server rotation.
# All server IPs are whitelisted at startup so the kill-switch never needs
# updating when rotating.
# ─────────────────────────────────────────────────────────────────────────────
_VPN_CFGS=/tmp/vpn_cfgs       # one config path per line
_VPN_CFG_IDX=/tmp/vpn_cfg_idx # current index (0-based)

_init_config_rotation() {
    _cdir="$CREDS_DIR/configs"
    [ -d "$_cdir" ] || return 0
    ls "$_cdir"/*.ovpn 2>/dev/null | sort > "$_VPN_CFGS"
    _cnt=$(wc -l < "$_VPN_CFGS")
    [ "$_cnt" -eq 0 ] && { rm -f "$_VPN_CFGS"; return 0; }
    echo "0" > "$_VPN_CFG_IDX"
    log "Config rotation: $_cnt server config(s) in $_cdir"
    # Use the first config as the starting point
    cp "$(head -1 "$_VPN_CFGS")" "$CREDS_DIR/config.ovpn"
    # Merge all remote hostnames into VPN_SERVERS so apply_killswitch
    # whitelists every possible server at startup
    while IFS= read -r _cfg; do
        _svrs=$(grep "^remote " "$_cfg" 2>/dev/null | awk '{print $2}')
        VPN_SERVERS="$VPN_SERVERS $_svrs"
    done < "$_VPN_CFGS"
    VPN_SERVERS=$(printf '%s\n' $VPN_SERVERS | sort -u | tr '\n' ' ')
}

_rotate_vpn_config() {
    [ -f "$_VPN_CFGS" ] || return 0
    _cnt=$(wc -l < "$_VPN_CFGS")
    [ "$_cnt" -le 1 ] && return 0
    _i=$(cat "$_VPN_CFG_IDX" 2>/dev/null || echo 0)
    _i=$(( (_i + 1) % _cnt ))
    echo "$_i" > "$_VPN_CFG_IDX"
    _next=$(sed -n "$((_i + 1))p" "$_VPN_CFGS")
    cp "$_next" "$CREDS_DIR/config.ovpn"
    log "Rotated to VPN config $_i: $(basename "$_next")"
}

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
# DNS leak prevention
# ─────────────────────────────────────────────────────────────────────────────
# On a Docker network the container's resolver is Docker's embedded DNS
# (127.0.0.11), which forwards queries UPSTREAM from the *host* namespace — i.e.
# outside the tunnel and outside our iptables. So even with the kill-switch up,
# every hostname lookup (deezer, tidal, musicbrainz, spotify …) would leak to
# the ISP in cleartext, tying the real IP to the activity. Once the tunnel is up
# we point resolv.conf at a public resolver so lookups egress through tun0/wg0.
#
# NOTE: musl libc (Alpine) queries all listed nameservers in parallel, so this
# MUST be a full replacement — appending a public resolver next to 127.0.0.11
# would still leak. During a reconnect we temporarily restore Docker's resolver
# so OpenVPN can re-resolve the (whitelisted) server hostname while down.
VPN_DNS="${VPN_DNS:-1.1.1.1 1.0.0.1}"

set_vpn_dns() {
    # WireGuard's wg-quick may already have installed tunnel DNS via resolvconf;
    # only take over when resolv.conf still points at Docker's embedded resolver.
    if ! grep -q '127.0.0.11' /etc/resolv.conf 2>/dev/null; then
        log "DNS: resolver already changed from Docker default — leaving as-is"
        return 0
    fi
    printf 'nameserver %s\n' $VPN_DNS > /etc/resolv.conf 2>/dev/null \
        && log "DNS: lookups now routed through tunnel via: $VPN_DNS" \
        || log "WARN: could not rewrite /etc/resolv.conf — DNS may leak"
}

restore_docker_dns() {
    if [ -f /tmp/resolv.conf.docker ]; then
        cat /tmp/resolv.conf.docker > /etc/resolv.conf 2>/dev/null \
            && debug "DNS: restored Docker resolver for reconnect" || true
    fi
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
    iptables -F INPUT   2>/dev/null || true
    iptables -F OUTPUT  2>/dev/null || true
    iptables -F FORWARD 2>/dev/null || true
    iptables -P INPUT   DROP
    iptables -P OUTPUT  DROP
    iptables -P FORWARD DROP

    iptables -A INPUT  -i lo -j ACCEPT
    iptables -A OUTPUT -o lo -j ACCEPT
    # VPN tunnel: allow all packets on the tunnel interface (no conntrack needed)
    iptables -A INPUT  -i "$IFACE_PATTERN" -j ACCEPT
    iptables -A OUTPUT -o "$IFACE_PATTERN" -j ACCEPT
    # Web UI: allow only on the Docker bridge interface, not the VPN tunnel.
    iptables -A INPUT  -i "$DOCKER_IFACE" -p tcp --dport "$WEB_PORT" -j ACCEPT
    # Egress on the UI port is restricted to ESTABLISHED replies so a rogue
    # process that binds local port 5000 can't use it as a plaintext bypass to
    # arbitrary destinations. Fall back to the stateless rule if the conntrack
    # match isn't available on this kernel.
    if iptables -A OUTPUT -o "$DOCKER_IFACE" -p tcp --sport "$WEB_PORT" \
            -m conntrack --ctstate ESTABLISHED -j ACCEPT 2>/dev/null; then
        log "Web UI port $WEB_PORT: INPUT ACCEPT + OUTPUT ESTABLISHED-only on $DOCKER_IFACE"
    else
        iptables -A OUTPUT -o "$DOCKER_IFACE" -p tcp --sport "$WEB_PORT" -j ACCEPT
        log "WARN: conntrack unavailable — Web UI port $WEB_PORT egress is stateless on $DOCKER_IFACE"
    fi

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
            date +%s > "$TUNNEL_UP_FILE" 2>/dev/null || true
            chown "$PUID:$PGID" "$TUNNEL_UP_FILE" 2>/dev/null || true
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
# Update SpotiFLAC — runs after tunnel is up so pip can reach PyPI.
# ─────────────────────────────────────────────────────────────────────────────
update_spotiflac() {
    # SpotiFLAC is pinned to a FIXED version, never auto-upgraded to "latest".
    # Rationale:
    #   * Reproducibility — the same code runs on every boot.
    #   * Supply chain — SpotiFLAC is pulled from PyPI and phones home at import
    #     time; auto-chasing latest would silently ingest unreviewed releases.
    #   * Patch compatibility — patch_spotiflac.py (the mbid fixes etc.) matches
    #     this version's source; a newer release can reformat those lines and
    #     silently skip the patches.
    # Override with SPOTIFLAC_VERSION only after re-verifying the patches apply.
    SPOTIFLAC_VERSION="${SPOTIFLAC_VERSION:-$SPOTIFLAC_PINNED}"
    log "Installing SpotiFLAC (pinned) $SPOTIFLAC_VERSION..."

    if _out=$(pip install --target /spotiflac "SpotiFLAC==$SPOTIFLAC_VERSION" 2>&1); then
        _rc=0
    else
        _rc=$?
    fi
    if [ "$_rc" -ne 0 ]; then
        err "SpotiFLAC install failed: $(echo "$_out" | tail -1)"
        return
    fi
    if echo "$_out" | grep -qi "successfully installed"; then
        _ver=$(echo "$_out" | grep -o "SpotiFLAC-[0-9][^ ]*" | head -1)
        log "SpotiFLAC installed ${_ver:-$SPOTIFLAC_VERSION} — re-patching"
        python3 /app/patch_spotiflac.py 2>&1 | sed 's/^/[vpn] /' || true
    else
        log "SpotiFLAC already at pinned version $SPOTIFLAC_VERSION"
    fi
    # /spotiflac is written as root; make sure the app user can import it.
    chmod -R a+rX /spotiflac 2>/dev/null || true
}

# ─────────────────────────────────────────────────────────────────────────────
# Start and monitor the app process
# ─────────────────────────────────────────────────────────────────────────────
start_app() {
    # Single worker to preserve shared in-memory job queue; threads handle
    # concurrent requests. Timeout 300s covers long SSE streams (library organizer).
    APP_CMD="${APP_CMD:-gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 1 --threads 4 --timeout 300 app:app}"
    WEB_PORT="${PORT:-5000}"

    log "Starting app as ${APP_USER} (uid $PUID) — no NET_ADMIN, kill-switch enforcing"

    # Unbuffered Python output so logs appear immediately in docker logs
    export PYTHONUNBUFFERED=1

    # Drop root (and thus NET_ADMIN/NET_RAW): the app can no longer alter the
    # kill-switch even if SpotiFLAC is compromised.
    run_as_app sh -c "$APP_CMD" 2>&1 &
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
    # Tunnel is down: put Docker's resolver back so OpenVPN can re-resolve its
    # (IP-whitelisted) server hostname. set_vpn_dns runs again once tunnel is up.
    restore_docker_dns
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
                _rotate_vpn_config
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
                date +%s > "$TUNNEL_UP_FILE" 2>/dev/null || true
                chown "$PUID:$PGID" "$TUNNEL_UP_FILE" 2>/dev/null || true
                set_vpn_dns
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

        # App-requested VPN reconnect (triggered when downloads are all failing)
        if [ -f /tmp/vpn_reconnect ]; then
            rm -f /tmp/vpn_reconnect
            log "Download worker requested VPN reconnect (IP block detected)"
            if reconnect_vpn; then
                log "VPN reconnected to new server after IP block"
            else
                err "VPN reconnect after IP block failed — will retry next cycle"
            fi
            continue
        fi

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
# Re-apply SpotiFLAC patches on every startup so in-place pip upgrades via the
# UI are automatically patched before the app starts.
log "Applying SpotiFLAC patches..."
python3 /app/patch_spotiflac.py 2>&1 | sed 's/^/[vpn] /' || true

# Snapshot Docker's embedded resolver BEFORE we start swapping DNS, so it can
# be restored during reconnects (see restore_docker_dns).
cp /etc/resolv.conf /tmp/resolv.conf.docker 2>/dev/null || true

case "$VPN_PROTOCOL" in
    openvpn)   setup_openvpn; _init_config_rotation ;;
    wireguard) setup_wireguard ;;
esac

lock_down_creds        # 0600 every secret in /vpn before the app user exists
resolve_servers        # uses Docker DNS (still active — kill-switch not up yet)
apply_killswitch
start_vpn
wait_for_tunnel
setup_return_routing
set_vpn_dns            # route DNS through the tunnel (fixes ISP DNS leak)
update_spotiflac
start_app
monitor_tunnel
