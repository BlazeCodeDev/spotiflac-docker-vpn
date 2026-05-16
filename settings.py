import json
import logging
import os
import threading

_SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "/vpn/settings.json")
_lock          = threading.Lock()
_log           = logging.getLogger(__name__)


def _defaults() -> dict:
    # Env vars are initial fallbacks — once the file is written they are
    # ignored and the UI is the single source of truth going forward.
    return {
        # Download behaviour
        "services":    [s.strip() for s in os.environ.get("SPOTIFLAC_SERVICES", "tidal,qobuz,amazon,youtube").split(",") if s.strip()],
        "filename_fmt": os.environ.get("FILENAME_FORMAT", "{artist}/{album}/{track} {title}"),
        "qobuz_token":  os.environ.get("QOBUZ_TOKEN",    ""),
        # Retry
        "retry_interval_min": int(os.environ.get("RETRY_MINUTES",       "5")),
        "retry_max_count":    int(os.environ.get("RETRY_MAX_COUNT",      "3")),
        # Performance
        "track_delay_s": float(os.environ.get("TRACK_DELAY_SECONDS", "4.0")),
        "max_workers":   int(os.environ.get("MAX_WORKERS",           "3")),
        # VPN rotation: reconnect after this many consecutive all-provider failures
        # (0 = disabled)
        "reconnect_threshold": int(os.environ.get("VPN_RECONNECT_THRESHOLD", "3")),
    }


def load() -> dict:
    d = _defaults()
    try:
        with open(_SETTINGS_FILE) as f:
            stored = json.load(f)
        d.update({k: stored[k] for k in d if k in stored})
    except FileNotFoundError:
        pass
    except Exception as exc:
        _log.warning("Settings load failed: %s", exc)
    return d


def save(updates: dict) -> None:
    allowed = _defaults().keys()
    with _lock:
        current = load()
        for k, v in updates.items():
            if k in allowed:
                current[k] = v
        _persist(current)


def _persist(data: dict) -> None:
    try:
        d = os.path.dirname(os.path.abspath(_SETTINGS_FILE))
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = _SETTINGS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _SETTINGS_FILE)
    except Exception as exc:
        _log.warning("Settings save failed: %s", exc)
