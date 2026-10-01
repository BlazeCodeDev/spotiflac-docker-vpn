"""Container log output: one line per event, a fixed layout, and a choice of what is shown.

    21:35:09  INFO   VPN       Connected · tun0
    21:36:10  INFO   DOWNLOAD  Downloading  Bob Sinclar – World, Hold On
    21:36:48  ERROR  ENRICH    Bob Sinclar – World, Hold On: Unrecognised audio format

Warnings and errors always print. Information lines print when their category is switched on
(Settings → Logging); the default is the short list in DEFAULT. Code that wants a clean line calls
`event(category, text)`. Everything else that is merely logged (library chatter, SpotiFLAC's own
output through `logging`) belongs to the "detail" category and stays hidden until it is enabled.
entrypoint.sh applies the same switches to its own lines by reading the same settings file.
"""
import logging
import os
import time

# key, label in the log, name in the settings page, one-line help
CATEGORIES = [
    ("vpn",       "VPN",      "VPN",                 "Connecting, connected, reconnecting, and the exit IP"),
    ("downloads", "DOWNLOAD", "Downloads",           "Each song as it starts, finishes or fails"),
    ("enrich",    "ENRICH",   "Enrichment",          "Batch enrichment, repaired albums and per-song errors"),
    ("library",   "LIBRARY",  "Library",             "Index scans and health scans"),
    ("system",    "SYSTEM",   "Startup",             "Start-up summary and settings changes"),
    ("requests",  "HTTP",     "Web requests",        "Every request the web interface makes (very chatty)"),
    ("detail",    "DETAIL",   "Diagnostic detail",   "Everything else: pre-scans, provider chatter, internals"),
]
KEYS = [c[0] for c in CATEGORIES]
LABEL = {c[0]: c[1] for c in CATEGORIES}
DEFAULT = ["vpn", "downloads", "enrich", "system"]

# Plain `logging` records are sorted into a category by the logger that made them.
# (category, label shown) — anything not listed is "detail".
_BY_LOGGER = {
    "werkzeug":    ("requests", "HTTP"),
    "gunicorn":    ("system",   "SYSTEM"),
    "startup":     ("system",   "SYSTEM"),
    "lib_index":   ("library",  "LIBRARY"),
    "tagcache":    ("library",  "LIBRARY"),
    "audiofp":     ("library",  "LIBRARY"),
    "worker":      ("detail",   "DOWNLOAD"),
    "vpn":         ("detail",   "VPN"),
    "listenbrainz": ("detail",  "SYSTEM"),
    "routes":      ("detail",   "SYSTEM"),
    "settings":    ("detail",   "SYSTEM"),
}
_LEVEL = {logging.DEBUG: "DEBUG", logging.INFO: "INFO", logging.WARNING: "WARN", logging.ERROR: "ERROR", logging.CRITICAL: "ERROR"}

_enabled: frozenset = frozenset(DEFAULT)


def apply(categories) -> None:
    """Switch which information categories print. Unknown names are ignored."""
    global _enabled
    _enabled = frozenset(c for c in (categories or []) if c in KEYS)


def enabled() -> frozenset:
    return _enabled


def _classify(record: logging.LogRecord) -> tuple[str, str]:
    cat = getattr(record, "cat", None)
    if cat:
        return cat, LABEL.get(cat, cat.upper())
    root = record.name.split(".", 1)[0]
    return _BY_LOGGER.get(root, ("detail", "SYSTEM"))


class _Gate(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.cat_key, record.cat_label = _classify(record)
        if record.levelno >= logging.WARNING:
            return True
        return record.cat_key in _enabled


class _Format(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        text = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            if "detail" in _enabled:      # the full traceback is part of the diagnostic detail
                if not record.exc_text:
                    record.exc_text = self.formatException(record.exc_info)
                text += "\n" + record.exc_text
            else:
                text += f" · {type(record.exc_info[1]).__name__}: {record.exc_info[1]}"
        # Continuation lines (tracebacks, multi-line messages) line up under the message.
        text = text.replace("\n", "\n" + " " * 30)
        return f"{ts}  {_LEVEL.get(record.levelno, 'INFO'):<5}  {getattr(record, 'cat_label', 'SYSTEM'):<8}  {text}"


_events = logging.getLogger("events")


def event(cat: str, text: str, level: int = logging.INFO) -> None:
    """One log line in a category. Errors and warnings always print."""
    _events.log(level, text, extra={"cat": cat})


def setup(categories=None) -> None:
    """Install the handler on the root logger; call once at start-up."""
    apply(categories if categories is not None else DEFAULT)
    handler = logging.StreamHandler()
    handler.setFormatter(_Format())
    handler.addFilter(_Gate())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if "detail" in _enabled and os.environ.get("LOG_LEVEL", "").lower() == "debug" else logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    # pydoll logs every browser interaction at INFO while it solves a Cloudflare challenge.
    logging.getLogger("pydoll").setLevel(logging.WARNING)


def short(seconds: float) -> str:
    """12s, 3m 05s, 1h 02m"""
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"
