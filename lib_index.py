import logging
import os
import re
import threading
import time

_log = logging.getLogger(__name__)

_AUDIO_EXTS   = frozenset({".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".wma"})
_TRACK_NUM_RE = re.compile(r'^\d+\s+')
_SPECIAL_RE   = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_index:      set[str]         = set()
_index_lock: threading.RLock = threading.RLock()
_scan_event: threading.Event = threading.Event()
_root_fn    = None
_ready      = False
_scanning   = False


def _normalise_filename(stem: str) -> str:
    """Normalise an on-disk filename stem for index storage."""
    return _TRACK_NUM_RE.sub("", stem).lower().strip()


def _normalise_title(title: str) -> str:
    """Normalise a raw track title to match how SpotiFLAC names the file."""
    sanitised = _SPECIAL_RE.sub("_", str(title)).strip().strip(".")[:200]
    return sanitised.lower().strip()


def _build_index(root: str) -> set[str]:
    stems: set[str] = set()
    try:
        for dirpath, _, files in os.walk(root):
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext in _AUDIO_EXTS:
                    stems.add(_normalise_filename(os.path.splitext(fname)[0]))
    except Exception as exc:
        _log.warning("lib_index scan error: %s", exc)
    return stems


def _worker():
    global _index, _ready, _scanning
    while True:
        _scan_event.wait()
        _scan_event.clear()

        root = _root_fn() if callable(_root_fn) else None
        if not root or not os.path.isdir(root):
            _ready = True
            continue

        _scanning = True
        t0        = time.monotonic()
        stems     = _build_index(root)
        elapsed   = time.monotonic() - t0

        with _index_lock:
            _index = stems
        _scanning = False
        _ready    = True
        _log.info("lib_index: full scan — %d tracks in %.2fs", len(stems), elapsed)


def start(root_fn) -> None:
    global _root_fn
    _root_fn = root_fn
    t = threading.Thread(target=_worker, name="lib-index", daemon=True)
    t.start()
    _scan_event.set()


def trigger_rescan() -> None:
    """Trigger a full filesystem walk (manual rescan / startup)."""
    _scan_event.set()


def add_titles(titles: list[str]) -> None:
    """Incrementally add downloaded track titles to the index without rescanning."""
    if not titles:
        return
    normalised = {_normalise_title(t) for t in titles}
    with _index_lock:
        _index.update(normalised)
    _log.debug("lib_index: added %d title(s) incrementally", len(normalised))


def check(titles: list[str]) -> list[bool]:
    with _index_lock:
        idx = _index
    return [_normalise_title(t) in idx for t in titles]


def ready() -> bool:
    return _ready


def status() -> dict:
    with _index_lock:
        count = len(_index)
    return {"scanning": _scanning, "count": count}
