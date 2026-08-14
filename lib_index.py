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

_index:           set[str]        = set()
_album_counts:    dict            = {}   # normalised album name → track count
_by_artist_title: dict[str, str]  = {}   # folded "artist|title" → absolute path
_index_lock:      threading.RLock = threading.RLock()
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


def _fold(s: str) -> str:
    """Casefold and strip punctuation for artist/title identity matching —
    collapses near-identical text so the same song is recognised even when
    written slightly differently (spacing, punctuation)."""
    return re.sub(r"\W+", " ", _nfc(str(s or "")).casefold()).strip()


def _artist_title_key(artist: str, title: str) -> str:
    return f"{_fold(artist)}|{_fold(title)}"


def _read_artist_title(path: str) -> tuple[str, str] | None:
    try:
        from mutagen import File as _MutagenFile
        f = _MutagenFile(path, easy=True)
        if f is None:
            return None
        artist = (f.get("artist") or [""])[0].strip()
        title  = (f.get("title") or [""])[0].strip()
        if not artist or not title:
            return None
        return artist, title
    except Exception:
        return None


def _build_index(root: str) -> tuple[set[str], dict, dict[str, str]]:
    """Full library scan. Also reads each audio file's embedded artist/title
    tags (see _read_artist_title) to build an identity index that's robust to
    album metadata varying between provider results for the same song — this
    used to run synchronously inside every download job's pre-scan (blocking
    every job start on a full-library tag read, brutal over network storage),
    now lives here in the existing background scan instead.
    """
    stems:            set[str]        = set()
    album_counts:     dict            = {}
    by_artist_title:  dict[str, str]  = {}
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

                full = os.path.join(dirpath, fname)
                tags = _read_artist_title(full)
                if tags:
                    artist, title = tags
                    by_artist_title[_artist_title_key(artist, title)] = full

            rel   = os.path.relpath(dirpath, root)
            parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
            if parts:
                key = _normalise_title(parts[-1])
                album_counts[key] = album_counts.get(key, 0) + len(audio)
    except Exception as exc:
        _log.warning("lib_index scan error: %s", exc)
    return stems, album_counts, by_artist_title


def _worker():
    global _index, _album_counts, _by_artist_title, _ready, _scanning, _last_elapsed, _last_scanned
    while True:
        _scan_event.wait()
        _scan_event.clear()

        root = _root_fn() if callable(_root_fn) else None
        if not root or not os.path.isdir(root):
            _ready = True
            continue

        _scanning = True
        t0            = time.monotonic()
        stems, ac, at = _build_index(root)
        elapsed       = time.monotonic() - t0

        with _index_lock:
            _index           = stems
            _album_counts    = ac
            _by_artist_title = at
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


def find_by_artist_title(artist: str, title: str) -> str | None:
    """Returns the path of an existing library track matching this artist+title
    (normalised), regardless of album/folder metadata — catches the same song
    downloaded again under a different album-derived path (different
    providers/playlist entries often disagree on album metadata for the same
    track). Used by worker.py's download pre-scan as a fallback when the
    exact expected-filename check misses.
    """
    if not artist or not title:
        return None
    with _index_lock:
        return _by_artist_title.get(_artist_title_key(artist, title))


def add_track(artist: str, title: str, path: str) -> None:
    """Incrementally registers a freshly-downloaded track so later tracks in
    the same (or a concurrent) job see it immediately, without waiting for
    the next full background rescan."""
    if not artist or not title:
        return
    with _index_lock:
        _by_artist_title[_artist_title_key(artist, title)] = path


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
