import logging
import os
import re
import threading
import time
import unicodedata

import applog

_log = logging.getLogger(__name__)

_AUDIO_EXTS   = frozenset({".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".wma"})
_TRACK_NUM_RE = re.compile(r'^\d+\s+')
_SPECIAL_RE   = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_PAREN_RE     = re.compile(r'\s*[\(\[].*?[\)\]]\s*$')

_index:           set[str]        = set()
_album_counts:    dict            = {}   # normalised album name → track count
_artist_albums:   dict            = {}   # folded "artist|album" → track count (folder and tags)
_artist_titles:   set[str]        = set()  # folded "artist|title" for every credited artist, for title checks
_by_artist_title: dict[str, str]  = {}   # folded "artist|title" → absolute path
_files:           list | None     = None # [(rel_dir, filename)] of every audio file, for search
_index_lock:      threading.RLock = threading.RLock()
_scan_event:   threading.Event = threading.Event()
_root_fn       = None
_ready         = False
_scanning      = False
_last_elapsed  = None   # seconds the most recent scan took
_last_scanned  = None   # time.time() when the most recent scan finished
_progress      = {"phase": "", "done": 0, "total": 0}


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


_SPLIT_RE = re.compile(r"\s*(?:,|;|&|/|(?i:\bfeat\.?|\bft\.?|\bwith\b))\s*|\s+x\s+")


def _artists(*credits: str) -> set[str]:
    """Every artist named in these credits, folded ("A feat. B, C" → a, b, c), plus each full credit."""
    out = set()
    for c in credits:
        if c:
            out.add(_fold(c))
            out.update(_fold(p) for p in _SPLIT_RE.split(c) if p and p.strip())
    out.discard("")
    return out


def _album_key(artist: str, album: str) -> str:
    return f"{artist}|{_normalise_title(album)}"


def _build_index(root: str) -> tuple[set[str], dict, dict[str, str], list, dict, set]:
    """Full library scan. Also reads each audio file's embedded artist/title
    tags (see _read_artist_title) to build an identity index that's robust to
    album metadata varying between provider results for the same song — this
    used to run synchronously inside every download job's pre-scan (blocking
    every job start on a full-library tag read, brutal over network storage),
    now lives here in the existing background scan instead.
    """
    import tagcache
    stems:            set[str]        = set()
    album_counts:     dict            = {}
    by_artist_title:  dict[str, str]  = {}
    folder_albums:    dict            = {}
    tag_albums:       dict            = {}
    artist_titles:    set[str]        = set()
    paths:            list[str]       = []
    file_list:        list            = []
    _progress.update(phase="Listing files", done=0, total=0)
    try:
        for dirpath, _, files in os.walk(root):
            audio = [f for f in files if os.path.splitext(f)[1].lower() in _AUDIO_EXTS]
            if not audio:
                continue
            rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
            rel_dir = "" if rel_dir == "." else rel_dir
            for fname in sorted(audio):
                stem = _normalise_filename(os.path.splitext(fname)[0])
                stems.add(stem)
                base = _base(stem)
                if base != stem:
                    stems.add(base)  # also index without "(Remastered)" etc.
                paths.append(os.path.join(dirpath, fname))
                _progress["done"] = len(paths)
                file_list.append((rel_dir, fname))

            rel   = os.path.relpath(dirpath, root)
            parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
            if parts:
                key = _normalise_title(parts[-1])
                album_counts[key] = album_counts.get(key, 0) + len(audio)
                if len(parts) >= 2:   # {artist}/{album}/… layouts
                    for a in _artists(parts[-2]):
                        k = _album_key(a, parts[-1])
                        folder_albums[k] = folder_albums.get(k, 0) + len(audio)

        # Tags come from the persistent cache (tagcache): unchanged files cost a
        # stat, only new/changed ones are opened — in parallel.
        _progress.update(phase="Reading tags", done=0, total=len(paths))

        def prog(d, t):
            _progress["done"], _progress["total"] = d, t

        for full, rec in tagcache.get_many(paths, progress=prog).items():
            artist, title = rec.get("artist", ""), rec.get("title", "")
            if artist and title:
                by_artist_title[_artist_title_key(artist, title)] = full
            names = _artists(artist, rec.get("albumartist", ""))
            if title:
                artist_titles.update(f"{a}|{_fold(title)}" for a in names)
                b = _fold(_base(title))
                artist_titles.update(f"{a}|{b}" for a in names)
            if rec.get("album"):
                for a in names:
                    k = _album_key(a, rec["album"])
                    tag_albums[k] = tag_albums.get(k, 0) + 1
        tagcache.prune(paths)
    except Exception as exc:
        _log.warning("lib_index scan error: %s", exc)
    # Folder and tag counts can disagree (untagged files, a various-artists folder); trust the larger.
    artist_albums = {k: max(folder_albums.get(k, 0), tag_albums.get(k, 0)) for k in folder_albums.keys() | tag_albums.keys()}
    return stems, album_counts, by_artist_title, file_list, artist_albums, artist_titles


def _worker():
    global _index, _album_counts, _by_artist_title, _files, _ready, _scanning, _last_elapsed, _last_scanned
    global _artist_albums, _artist_titles
    while True:
        _scan_event.wait()
        _scan_event.clear()

        root = _root_fn() if callable(_root_fn) else None
        if not root or not os.path.isdir(root):
            _ready = True
            continue

        _scanning = True
        t0            = time.monotonic()
        stems, ac, at, fl, aa, ats = _build_index(root)
        elapsed       = time.monotonic() - t0

        with _index_lock:
            _index           = stems
            _album_counts    = ac
            _by_artist_title = at
            _files           = fl
            _artist_albums   = aa
            _artist_titles   = ats
        _scanning     = False
        _ready        = True
        _last_elapsed = elapsed
        _last_scanned = time.time()
        applog.event("library", f"Indexed      {len(fl):,} songs in {applog.short(elapsed) if elapsed >= 1 else f'{elapsed:.1f}s'}")


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


def all_files() -> list | None:
    """[(rel_dir, filename)] of every audio file as of the last scan (plus
    tracks added since), or None before the first scan has finished."""
    with _index_lock:
        return None if _files is None else list(_files)


def add_track(artist: str, title: str, path: str) -> None:
    """Incrementally registers a freshly-downloaded track so later tracks in
    the same (or a concurrent) job see it immediately, without waiting for
    the next full background rescan."""
    root = _root_fn() if callable(_root_fn) else None
    with _index_lock:
        if artist and title:
            _by_artist_title[_artist_title_key(artist, title)] = path
            _artist_titles.update(f"{a}|{_fold(title)}" for a in _artists(artist))
        if _files is not None and root and path:
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if not rel.startswith(".."):
                d, _, f = rel.rpartition("/")
                _files.append((d, f))


def check_track(title: str, artists: str = "") -> bool:
    """True if this song is in the library. With `artists` ("A, B"), only that artist's song counts:
    title-only matching made every "Intro" or "Home" look owned."""
    if not artists:
        return check([title])[0]
    t, b = _fold(title), _fold(_base(title))
    with _index_lock:
        idx = _artist_titles
    return any(f"{a}|{t}" in idx or f"{a}|{b}" in idx for a in _artists(artists))


def album_count(album: str, artists: str = "") -> int:
    """How many songs of this album are in the library. With `artists`, only that artist's album counts
    (otherwise one "Greatest Hits" folder matches every artist's)."""
    norm = _normalise_title(album)
    base = _base(norm)
    with _index_lock:
        if artists:
            names = _artists(artists)
            return max((_artist_albums.get(f"{a}|{n}", 0) for a in names for n in (norm, base)), default=0)
        return _album_counts.get(norm) or _album_counts.get(base) or 0


def check_album(album: str, total: int | None, artists: str = "") -> str:
    """Returns 'full', 'partial', or 'none'."""
    count = album_count(album, artists)
    if not count:
        return "none"
    if total and count < total:
        return "partial"
    return "full"


def status() -> dict:
    with _index_lock:
        count = len(_files) if _files is not None else len(_index)
    return {
        "scanning":      _scanning,
        "phase":         _progress["phase"],
        "done":          _progress["done"],
        "total":         _progress["total"],
        "first":         not _ready,
        "count":         count,
        "last_elapsed":  _last_elapsed,
        "last_scanned":  _last_scanned,
    }


def ready() -> bool:
    return _ready
