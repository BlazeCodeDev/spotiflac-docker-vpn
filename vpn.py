import json
import logging
import subprocess
import time
import urllib.request

log               = logging.getLogger(__name__)
_ip_cache:  dict  = {}
_connected_since: float | None = None
_VPN_UPTIME_FILE = "/vpn/tunnel_up_since"


def _read_tunnel_start() -> float:
    try:
        return float(open(_VPN_UPTIME_FILE).read().strip())
    except Exception:
        return time.time()


def tunnel_status() -> dict:
    global _connected_since
    try:
        for iface in ("tun0", "wg0"):
            r = subprocess.run(["ip", "link", "show", iface], capture_output=True)
            if r.returncode == 0:
                if _connected_since is None:
                    _connected_since = _read_tunnel_start()
                return dict(connected=True, interface=iface, connected_since=_connected_since)
    except FileNotFoundError:
        pass
    _connected_since = None
    return dict(connected=False, interface=None, connected_since=None)


def ip_info() -> dict:
    now = time.time()
    if _ip_cache.get("ts", 0) > now - 30:
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
