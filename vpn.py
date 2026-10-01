import json
import logging
import os
import subprocess
import threading
import time
import urllib.request

import applog

log               = logging.getLogger(__name__)
_ip_cache:  dict  = {}
_connected_since: float | None = None
# The entrypoint writes this file; its location moved to an app-user-writable
# state dir when the app was made to run unprivileged, so it's env-configurable.
_VPN_UPTIME_FILE = os.environ.get("VPN_UPTIME_FILE", "/vpn/tunnel_up_since")


def _read_tunnel_start() -> float | None:
    try:
        return float(open(_VPN_UPTIME_FILE).read().strip())
    except Exception:
        return None


# Read once at module load. The entrypoint writes this file before the app
# starts, so it is available from the very first request. Caching here means
# brief VPN blips (e.g. OpenVPN TLS renegotiation every ~1 h) that reset
# _connected_since to None don't lose the original timestamp on recovery —
# we reuse this value instead of re-reading (and risking a time.time() fallback).
_TUNNEL_START: float | None = _read_tunnel_start()


def tunnel_status() -> dict:
    global _connected_since
    try:
        for iface in ("tun0", "wg0"):
            r = subprocess.run(["ip", "link", "show", iface], capture_output=True)
            if r.returncode == 0:
                if _connected_since is None:
                    _connected_since = _TUNNEL_START
                return dict(connected=True, interface=iface, connected_since=_connected_since)
    except FileNotFoundError:
        pass
    _connected_since = None
    return dict(connected=False, interface=None, connected_since=None)


def ip_info() -> dict:
    now = time.time()
    if _ip_cache.get("ts", 0) > now - 300:
        return _ip_cache["data"]
    try:
        req = urllib.request.Request(
            "http://ip-api.com/json/?fields=status,country,countryCode,regionName,city,isp,query",
            headers={"User-Agent": "spotiflac-ui/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        _ip_cache["data"] = data
        _ip_cache["ts"]   = now
        return data
    except Exception as exc:
        log.debug("ip-api fetch failed: %s", exc)
        return {"error": str(exc)}


def _exit_place() -> str:
    """"203.0.113.4 · Frankfurt, Germany" for the tunnel's exit, or "" if the lookup fails."""
    for attempt in range(3):
        _ip_cache.clear()
        d = ip_info()
        if d.get("query"):
            where = ", ".join(x for x in (d.get("city"), d.get("country")) if x)
            return d["query"] + (f" · {where}" if where else "")
        time.sleep(2 * (attempt + 1))
    return ""


def start_watch(interval: float = 10.0) -> None:
    """Log the tunnel coming up (with its exit IP) and going down. The entrypoint logs the
    connecting/reconnecting side; this is what it can't see, the address the world gets."""
    def run():
        was_up, since = None, None
        while True:
            try:
                st = tunnel_status()
                now_since = _read_tunnel_start()
                if st["connected"]:
                    if was_up is not True or now_since != since:
                        place = _exit_place()
                        applog.event("vpn", "Connected · " + st["interface"] + (" · " + place if place else ""))
                    since = now_since
                elif was_up:
                    applog.event("vpn", "Tunnel down · traffic is blocked until it reconnects", 30)
                was_up = st["connected"]
            except Exception as exc:
                log.debug("VPN watch: %s", exc)
            time.sleep(interval)
    threading.Thread(target=run, daemon=True, name="vpn-watch").start()
