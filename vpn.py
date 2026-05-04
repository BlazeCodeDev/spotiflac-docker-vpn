import json
import logging
import subprocess
import time
import urllib.request

log               = logging.getLogger(__name__)
_ip_cache:  dict  = {}
_connected_since: float | None = None
_VPN_UPTIME_FILE = "/vpn/tunnel_up_since"


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
        log.warning("ip-api fetch failed: %s", exc)
        return {"error": str(exc)}
