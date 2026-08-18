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
        # Extension registry URLs (SpotiFLAC >= 1.8.0 ships no download
        # providers at all — these must be installed at runtime from a
        # registry the operator supplies and trusts themselves; see
        # worker.refresh_extensions and the "Extensions" settings group).
        "extension_registries": [s.strip() for s in os.environ.get("SPOTIFLAC_REGISTRIES", "").split(",") if s.strip()],
        "filename_fmt": os.environ.get("FILENAME_FORMAT", "{artist}/{album}/{track} {title}"),
        "qobuz_token":  os.environ.get("QOBUZ_TOKEN",    ""),
        # M3U playlist file generation for playlist downloads: "always", "ask" (prompt
        # per download), or "never".
        "m3u_mode": os.environ.get("M3U_MODE", "ask"),
        # Retry
        "retry_interval_min": int(os.environ.get("RETRY_MINUTES",       "5")),
        "retry_max_count":    int(os.environ.get("RETRY_MAX_COUNT",      "3")),
        # Performance
        "track_delay_s": float(os.environ.get("TRACK_DELAY_SECONDS", "4.0")),
        "max_workers":   int(os.environ.get("MAX_WORKERS",           "3")),
        # VPN rotation: reconnect after this many consecutive all-provider failures
        # (0 = disabled)
        "reconnect_threshold": int(os.environ.get("VPN_RECONNECT_THRESHOLD", "3")),
        # Metadata enrichment: fetch genre, label, BPM, UPC from external providers
        "enrich_metadata":     os.environ.get("ENRICH_METADATA",     "1") == "1",
        "enrich_providers":    [s.strip() for s in os.environ.get("ENRICH_PROVIDERS",    "deezer,apple").split(",") if s.strip()],
        "enrich_musicbrainz":  os.environ.get("ENRICH_MUSICBRAINZ",  "1") == "1",
        # ListenBrainz recommendations auto-download
        # Schedule: weekdays (Mon=0 … Sun=6) + local time of day. Default: daily 06:00.
        "listenbrainz_enabled":       os.environ.get("LB_ENABLED",  "0") == "1",
        "listenbrainz_username":      os.environ.get("LB_USERNAME",  ""),
        "listenbrainz_days":          [int(x) for x in os.environ.get("LB_DAYS", "0,1,2,3,4,5,6").split(",") if x.strip().isdigit()],
        "listenbrainz_time":          os.environ.get("LB_TIME", "06:00"),
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
