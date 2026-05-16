import logging
import os
import re
import threading
import time
import unicodedata

_log = logging.getLogger(__name__)

_AUDIO_EXTS   = frozenset({".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".wma"})
_TRACK_NUM_RE = re.compile(r'^\d+\s+')
_SPECIAL_RE   = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_PAREN_RE     = re.compile(r'\s*[\(\[].*?[\)\]]\s*$')

_index:        set[str]         = set()
_album_counts: dict             = {}   # normalised album name → track count
_index_lock:   threading.RLock = threading.RLock()
_scan_event:   threading.Event = threading.Event()
_root_fn       = None
_ready         = False
_scanning      = False
_last_elapsed  = None   # seconds the most recent scan took
_last_scanned  = None   # time.time() when the most recent scan finished


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _normalise_filename(stem: str) -> str:
    return _nfc(_TRACK_NUM_RE.sub("", stem).lower().strip())


def _normalise_title(title: str) -> str:
    sanitised = _SPECIAL_RE.sub("_", _nfc(str(title))).strip().strip(".")[:200]
    return sanitised.lower().strip()


def _base(s: str) -> str:
    """Strip trailing parenthetical content: '(Remastered)', '[feat. X]', etc."""
    return _PAREN_RE.sub("", s).strip()


def _build_index(root: str) -> tuple[set[str], dict]:
    stems:        set[str]  = set()
    album_counts: dict      = {}
    try:
        for dirpath, _, files in os.walk(root):
            audio = [f for f in files if os.path.splitext(f)[1].lower() in _AUDIO_EXTS]
            if not audio:
                continue
            for fname in audio:
                stem = _normalise_filename(os.path.splitext(fname)[0])
                stems.add(stem)
                base = _base(stem)
                if base != stem:
                    stems.add(base)  # also index without "(Remastered)" etc.

            rel   = os.path.relpath(dirpath, root)
            parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
            if parts:
                key = _normalise_title(parts[-1])
                album_counts[key] = album_counts.get(key, 0) + len(audio)
    except Exception as exc:
        _log.warning("lib_index scan error: %s", exc)
    return stems, album_counts


def _worker():
    global _index, _album_counts, _ready, _scanning, _last_elapsed, _last_scanned
    while True:
        _scan_event.wait()
        _scan_event.clear()

        root = _root_fn() if callable(_root_fn) else None
        if not root or not os.path.isdir(root):
            _ready = True
            continue

        _scanning = True
        t0        = time.monotonic()
        stems, ac = _build_index(root)
        elapsed   = time.monotonic() - t0

        with _index_lock:
            _index        = stems
            _album_counts = ac
        _scanning     = False
        _ready        = True
        _last_elapsed = elapsed
        _last_scanned = time.time()
        _log.info("lib_index: full scan — %d tracks, %d albums in %.2fs",
                  len(stems), len(ac), elapsed)


def start(root_fn) -> None:
    global _root_fn
    _root_fn = root_fn
    t = threading.Thread(target=_worker, name="lib-index", daemon=True)
    t.start()
    _scan_event.set()


def trigger_rescan() -> None:
    _scan_event.set()


def add_titles(titles: list[str]) -> None:
    if not titles:
        return
    normalised = set()
    for t in titles:
        n = _normalise_title(t)
        normalised.add(n)
        b = _base(n)
        if b != n:
            normalised.add(b)
    with _index_lock:
        _index.update(normalised)
    _log.debug("lib_index: added %d title(s) incrementally", len(titles))


def check(titles: list[str]) -> list[bool]:
    """Check a list of track titles. Returns True if the title (or its base) is indexed."""
    with _index_lock:
        idx = _index
    result = []
    for t in titles:
        norm = _normalise_title(t)
        if norm in idx:
            result.append(True)
        else:
            result.append(_base(norm) in idx)
    return result


def check_album(album: str, total: int | None) -> str:
    """Returns 'full', 'partial', or 'none'."""
    norm = _normalise_title(album)
    base = _base(norm)
    with _index_lock:
        count = _album_counts.get(norm) or _album_counts.get(base) or 0
    if not count:
        return "none"
    if total and count < total:
        return "partial"
    return "full"


def status() -> dict:
    with _index_lock:
        count = len(_index)
    return {
        "scanning":      _scanning,
        "count":         count,
        "last_elapsed":  _last_elapsed,
        "last_scanned":  _last_scanned,
    }


def ready() -> bool:
    return _ready
