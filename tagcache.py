"""Persistent cache of the tag fields the library scans need.

Every library scan (index, duplicate finder, mistag finder, enrich pre-pass)
used to open and parse every audio file with mutagen, every time. Over network
storage that is several round trips per file and dominated scan time. Here each
file is parsed once; its fields are stored keyed by path and checked against
(size, mtime) so an unchanged file costs only a stat on later scans. A file that
is re-tagged (enrich, repair) gets a new mtime and is simply read again.
"""
import concurrent.futures
import json
import logging
import os
import threading

_log = logging.getLogger(__name__)

_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(os.environ.get("SETTINGS_FILE", "/vpn/settings.json"))),
    "tagcache.json",
)
_FIELDS = ("title", "artist", "albumartist", "album", "isrc", "genre", "bpm",
           "musicbrainz_trackid", "tracknumber", "date")
# 2: added tracknumber/date and the "cover" flag for the library table; old entries are re-read once.
_VERSION = 2

_lock = threading.Lock()
_mem: dict | None = None     # {path: [stamp, record]}
_dirty = False


def _cache() -> dict:
    """Caller holds _lock."""
    global _mem
    if _mem is None:
        try:
            with open(_CACHE_FILE) as f:
                data = json.load(f)
            _mem = data.get("files", {}) if data.get("v") == _VERSION else {}
        except Exception:
            _mem = {}
    return _mem


def _save() -> None:
    """Caller holds _lock."""
    global _dirty
    try:
        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        tmp = _CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"v": _VERSION, "files": _mem}, f, separators=(",", ":"))
        os.replace(tmp, _CACHE_FILE)
        _dirty = False
    except Exception as exc:
        _log.debug("could not save tag cache: %s", exc)


def _stamp(st: os.stat_result) -> str:
    return f"{st.st_size}:{st.st_mtime_ns}"


def _parse(path: str) -> dict:
    """Read one file. Returns {} for files mutagen can't parse, so they are
    cached as unreadable too rather than retried every scan."""
    from mutagen import File as MFile
    try:
        audio = MFile(path, easy=True)
    except Exception:
        return {}
    if audio is None:
        return {}
    rec = {}
    for k in _FIELDS:
        try:
            v = audio.get(k)
        except Exception:
            v = None
        if v:
            rec[k] = str(v[0]).strip()
    info = getattr(audio, "info", None)
    rec["dur"] = float(getattr(info, "length", 0) or 0)
    rec["bitrate"] = int(getattr(info, "bitrate", 0) or 0)
    codec = str(getattr(info, "codec", "") or "")
    if codec:
        rec["codec"] = codec
    rec["cover"] = _has_cover(path)
    return rec


def _has_cover(path: str) -> bool:
    """True when the file carries an embedded picture. Cheap: tags only, no audio decoding."""
    from mutagen import File as MFile
    try:
        raw = MFile(path)
        if raw is None:
            return False
        if getattr(raw, "pictures", None):               # FLAC, and Ogg/Opus via mutagen's FLAC-style block
            return True
        tags = getattr(raw, "tags", None)
        if tags is None:
            return False
        if hasattr(tags, "getall"):                      # ID3 (mp3, wav)
            return bool(tags.getall("APIC"))
        if "covr" in tags:                               # MP4/M4A
            return bool(tags.get("covr"))
        return "metadata_block_picture" in tags          # Ogg Vorbis/Opus
    except Exception:
        return False


def _store(p: str, stamp: str, rec: dict) -> None:
    global _dirty
    with _lock:
        _cache()[p] = [stamp, rec]
        _dirty = True


def get_many(paths: list[str], progress=None, cancel=None, workers: int = 8) -> dict:
    """{path: record} for every path that exists. A record carries the tag
    fields above plus dur/bitrate/codec, and size/mtime from the stat."""
    out: dict = {}
    total = len(paths)

    def one(p):
        if cancel is not None and cancel.is_set():
            return p, None
        try:
            st = os.stat(p)
        except OSError:
            return p, None
        stamp = _stamp(st)
        with _lock:
            hit = _cache().get(p)
        if hit and hit[0] == stamp:
            rec = hit[1]
        else:
            rec = _parse(p)
            _store(p, stamp, rec)
        return p, {**rec, "size": st.st_size, "mtime": st.st_mtime}

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            for n, (p, rec) in enumerate(ex.map(one, paths), 1):
                if rec is not None:
                    out[p] = rec
                if progress:
                    progress(n, total)
    finally:
        # Persist whatever was read, even on cancel, so the next scan resumes.
        with _lock:
            if _dirty:
                _save()
    return out


def cached_many(paths: list[str]) -> dict:
    """{path: (stamp, record)} for every path with a cached record, without touching the disk: the
    record is as last read and may be stale (get_many() catches that). Lets the library table paint
    straight from memory; over network storage a stat per file per request is what made it slow.
    Records are shared, not copied: callers must not change them."""
    with _lock:
        c = _cache()
        return {p: (hit[0], hit[1]) for p in paths if (hit := c.get(p))}


def stamp_size_mtime(stamp: str) -> tuple[int, float]:
    """(size, mtime) as recorded in a stamp from cached_many()."""
    size, ns = stamp.split(":")
    return int(size), int(ns) / 1e9


def get(path: str) -> dict | None:
    return get_many([path]).get(path)


def prune(live_paths) -> None:
    """Forget files that are no longer in the library (after a full walk)."""
    live = set(live_paths)
    with _lock:
        c = _cache()
        gone = [k for k in c if k not in live]
        for k in gone:
            del c[k]
        if gone:
            _save()
