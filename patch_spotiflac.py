"""
Build-time patches for SpotiFLAC (targets the pinned version — see
entrypoint.sh SPOTIFLAC_PINNED / Dockerfile). Every patch is a guarded,
idempotent string replacement: if the anchor text isn't found (e.g. a version
bump reformatted it) the patch reports and skips rather than corrupting a file.

Ensure the missing MusicBrainz recording id (mbid) — and the rest of the MB
tag set — actually lands on *every* downloaded track:

  A. core/musicbrainz.py — the built-in lookup is ISRC-only, and a large share
     of tracks either have no ISRC or one MusicBrainz doesn't index, so they
     silently get no mbid. Add a title+artist text-search fallback, exposed as
     fetch_mb_metadata_smart (sync) and fetch_mb_metadata_smart_async (async),
     and route AsyncMBFetch through it.

  B. providers/*.py — providers only attempted a MB lookup when an ISRC was
     present (`AsyncMBFetch(isrc) if isrc else None`, or `if metadata.isrc:`),
     so ISRC-less tracks (common on YouTube, not rare elsewhere) never got a
     lookup. Always look up, passing title/artist for the text fallback.

  C. providers/deezer.py — additionally, Deezer only embedded the MB tags for
     album downloads (`extra_tags=mb_tags if is_album else {}`), so single-track
     Deezer downloads never got an mbid even when the lookup succeeded.

NOTE for version bumps: SpotiFLAC ships both a sync provider style
(AsyncMBFetch) and an async one (await fetch_mb_metadata_async). Both are
handled below. If a future release changes these call shapes, the affected
patch will log "pattern not found — skipping" and the mbid feature must be
re-ported (see git history for the 1.2.0 -> 1.3.1 and 1.3.1 -> 1.4.5 ports).
"""
import importlib.util
import pathlib
import sys

_spec = importlib.util.find_spec("SpotiFLAC")
if _spec is None or _spec.origin is None:
    print("[patch] SpotiFLAC not found — aborting")
    sys.exit(1)
_BASE = pathlib.Path(_spec.origin).parent


def _apply(rel_path, old, new, note, *, already_marker=None):
    """Idempotent single replacement with clear logging."""
    fpath = _BASE / rel_path
    if not fpath.exists():
        print(f"[patch] {rel_path}: file not found — skipping")
        return
    text = fpath.read_text()
    marker = already_marker if already_marker is not None else new
    if marker in text:
        print(f"[patch] {rel_path}: already patched — skipping")
        return
    if old not in text:
        print(f"[patch] {rel_path}: pattern not found — skipping (different version?)")
        return
    fpath.write_text(text.replace(old, new, 1))
    print(f"[patch] {rel_path}: {note}")


# ---------------------------------------------------------------------------
# Patch A: core/musicbrainz.py — title/artist text-search fallback
# ---------------------------------------------------------------------------
# Reuses the existing helpers already present in this module:
#   _query_recordings / _query_recordings_async, _parse_mb_response,
#   should_skip_mb, set_mb_status, fetch_mb_metadata[_async].

_MB_NEW_FUNCS = r'''def _lucene_escape(s: str) -> str:
    """Escape Lucene specials so free-text titles/artists (parentheses, colons,
    etc.) don't break the MusicBrainz query syntax."""
    specials = set('+-!(){}[]^"~*?:\\/')
    return "".join(("\\" + ch) if ch in specials else ch for ch in s.strip())


def _mb_text_query(title: str, artist: str) -> str:
    return f'recording:"{_lucene_escape(title)}" AND artist:"{_lucene_escape(artist)}"'


def fetch_mb_metadata_by_text(title: str, artist: str) -> dict:
    """Sync fallback: match a recording by title + artist when ISRC lookup fails."""
    if not title or not artist:
        return {}
    if should_skip_mb():
        logger.debug("[musicbrainz] text search skipped (offline recently)")
        return {}
    try:
        data = _query_recordings(_mb_text_query(title, artist))
        set_mb_status(True)
        return _parse_mb_response(data)
    except Exception as e:
        set_mb_status(False)
        logger.debug("[musicbrainz] text search failed for %r/%r: %s", title, artist, e)
        return {}


async def fetch_mb_metadata_by_text_async(title: str, artist: str) -> dict:
    """Async fallback: match a recording by title + artist when ISRC lookup fails."""
    if not title or not artist:
        return {}
    if should_skip_mb():
        logger.debug("[musicbrainz] text search skipped (offline recently)")
        return {}
    try:
        data = await _query_recordings_async(_mb_text_query(title, artist))
        set_mb_status(True)
        return _parse_mb_response(data)
    except Exception as e:
        set_mb_status(False)
        logger.debug("[musicbrainz] text search (async) failed for %r/%r: %s", title, artist, e)
        return {}


def fetch_mb_metadata_smart(isrc: str, title: str = "", artist: str = "") -> dict:
    """ISRC lookup first (cached, rate-limited); fall back to a title/artist
    text search when the ISRC is missing or MusicBrainz has no match for it.
    This is what actually guarantees an mbid for tracks whose ISRC isn't in
    MusicBrainz's ISRC index (a very common gap)."""
    res = fetch_mb_metadata(isrc) if isrc else {}
    if res.get("mbid_track"):
        return res
    if title and artist:
        text_res = fetch_mb_metadata_by_text(title, artist)
        if text_res.get("mbid_track"):
            return text_res
    return res


async def fetch_mb_metadata_smart_async(isrc: str, title: str = "", artist: str = "") -> dict:
    """Async twin of fetch_mb_metadata_smart (for providers on the async path)."""
    res = await fetch_mb_metadata_async(isrc) if isrc else {}
    if res.get("mbid_track"):
        return res
    if title and artist:
        text_res = await fetch_mb_metadata_by_text_async(title, artist)
        if text_res.get("mbid_track"):
            return text_res
    return res


'''

_apply(
    "core/musicbrainz.py",
    "def fetch_mb_metadata(isrc: str) -> dict:\n",
    _MB_NEW_FUNCS + "def fetch_mb_metadata(isrc: str) -> dict:\n",
    "added ISRC->text-search mbid fallback (fetch_mb_metadata_smart[_async])",
    already_marker="def fetch_mb_metadata_smart",
)

# AsyncMBFetch: accept title/artist and route through the smart lookup.
_apply(
    "core/musicbrainz.py",
    (
        "    def __init__(self, isrc: str):\n"
        "        self.isrc = isrc\n"
        "        try:\n"
        "            self.future = self._get_executor().submit(fetch_mb_metadata, isrc)\n"
        "        except RuntimeError:\n"
        "            self.future = self._get_executor().submit(fetch_mb_metadata, isrc)\n"
    ),
    (
        "    def __init__(self, isrc: str, title: str = \"\", artist: str = \"\"):\n"
        "        self.isrc = isrc\n"
        "        try:\n"
        "            self.future = self._get_executor().submit(fetch_mb_metadata_smart, isrc, title, artist)\n"
        "        except RuntimeError:\n"
        "            self.future = self._get_executor().submit(fetch_mb_metadata_smart, isrc, title, artist)\n"
    ),
    "AsyncMBFetch now takes title/artist and uses the smart lookup",
    already_marker="def __init__(self, isrc: str, title: str = \"\"",
)

# ---------------------------------------------------------------------------
# Patch B (sync-style providers): amazon, apple_music, pandora, youtube, gdstudio
# `AsyncMBFetch(_isrc_for_mb) if _isrc_for_mb else None`
#   -> always construct, passing title/artist for the text fallback.
# ---------------------------------------------------------------------------
_SYNC_OLD = "            mb_fetcher = AsyncMBFetch(_isrc_for_mb) if _isrc_for_mb else None\n"
_SYNC_NEW = "            mb_fetcher = AsyncMBFetch(_isrc_for_mb, metadata.title, metadata.first_artist)\n"
for _p in (
    "providers/amazon.py",
    "providers/apple_music.py",
    "providers/pandora.py",
    "providers/youtube.py",
    "providers/gdstudio.py",
):
    _apply(_p, _SYNC_OLD, _SYNC_NEW, "MusicBrainz lookup no longer gated on ISRC presence")

# ---------------------------------------------------------------------------
# Patch B (async-style providers): deezer, qobuz, tidal
# Add the smart-async import, then always look up via title/artist fallback.
# ---------------------------------------------------------------------------
_ASYNC_IMPORT_OLD = "from ..core.musicbrainz import fetch_mb_metadata_async, mb_result_to_tags\n"
_ASYNC_IMPORT_NEW = "from ..core.musicbrainz import fetch_mb_metadata_async, fetch_mb_metadata_smart_async, mb_result_to_tags\n"
for _p in ("providers/deezer.py", "providers/qobuz.py", "providers/tidal.py"):
    _apply(_p, _ASYNC_IMPORT_OLD, _ASYNC_IMPORT_NEW,
           "import fetch_mb_metadata_smart_async",
           already_marker="fetch_mb_metadata_smart_async")

# deezer: concurrent MB task — always create, with title/artist fallback.
_apply(
    "providers/deezer.py",
    (
        "            mb_task = (\n"
        "                asyncio.create_task(fetch_mb_metadata_async(isrc_to_use))\n"
        "                if isrc_to_use\n"
        "                else None\n"
        "            )\n"
    ),
    (
        "            mb_task = asyncio.create_task(\n"
        "                fetch_mb_metadata_smart_async(\n"
        "                    isrc_to_use, metadata.title, metadata.first_artist\n"
        "                )\n"
        "            )\n"
    ),
    "MB lookup task always created (title/artist fallback)",
    already_marker="fetch_mb_metadata_smart_async(",
)

# deezer: stop gating the MB tags behind is_album (single tracks got no mbid).
_apply(
    "providers/deezer.py",
    "                extra_tags=mb_tags if is_album else {},\n",
    "                extra_tags=mb_tags,\n",
    "MB tags (mbid) now embedded for single-track downloads too",
)

# qobuz: always look up (was `if metadata.isrc:`), with title/artist fallback.
_apply(
    "providers/qobuz.py",
    (
        "            mb_tags: dict[str, str] = {}\n"
        "            if metadata.isrc:\n"
        "                mb_tags = mb_result_to_tags(\n"
        "                    await fetch_mb_metadata_async(metadata.isrc)\n"
        "                )\n"
    ),
    (
        "            mb_tags: dict[str, str] = {}\n"
        "            mb_tags = mb_result_to_tags(\n"
        "                await fetch_mb_metadata_smart_async(\n"
        "                    metadata.isrc, metadata.title, metadata.first_artist\n"
        "                )\n"
        "            )\n"
    ),
    "MusicBrainz lookup no longer gated on ISRC presence",
    already_marker="fetch_mb_metadata_smart_async(",
)

# tidal: always look up (was `if metadata.isrc:`), with title/artist fallback.
_apply(
    "providers/tidal.py",
    (
        "            mb_tags: dict[str, str] = {}\n"
        "            if metadata.isrc:\n"
        "                mb_data = await fetch_mb_metadata_async(metadata.isrc)\n"
        "                mb_tags = mb_result_to_tags(mb_data)\n"
    ),
    (
        "            mb_tags: dict[str, str] = {}\n"
        "            mb_data = await fetch_mb_metadata_smart_async(\n"
        "                metadata.isrc, metadata.title, metadata.first_artist\n"
        "            )\n"
        "            mb_tags = mb_result_to_tags(mb_data)\n"
    ),
    "MusicBrainz lookup no longer gated on ISRC presence",
    already_marker="fetch_mb_metadata_smart_async(",
)

# ---------------------------------------------------------------------------
# Patch D: providers/amazon.py — clean up the orphaned FLAC on Antra repair failure
# ---------------------------------------------------------------------------
# When the Antra direct-download path's post-download integrity check fails
# (e.g. the `flac` binary is missing, or the file is genuinely corrupt), the
# code logs a warning and falls through to try the next Amazon quality tier —
# but never deletes the file it just wrote. It sits in output_dir's root,
# named by ASIN (not the tagged filename), forever. Delete it before moving on.
_apply(
    "providers/amazon.py",
    (
        "                                    logger.warning(\n"
        "                                        \"[amazon] Antra FLAC repair failed: %s\",\n"
        "                                        repair_msg,\n"
        "                                    )\n"
        "                                else:\n"
        "                                    logger.warning(\"[amazon] Antra FLAC remux failed.\")\n"
    ),
    (
        "                                    logger.warning(\n"
        "                                        \"[amazon] Antra FLAC repair failed: %s\",\n"
        "                                        repair_msg,\n"
        "                                    )\n"
        "                                    if os.path.exists(out):\n"
        "                                        os.remove(out)\n"
        "                                else:\n"
        "                                    logger.warning(\"[amazon] Antra FLAC remux failed.\")\n"
    ),
    "orphaned FLAC (from a failed integrity repair) now cleaned up before falling back",
    already_marker="if os.path.exists(out):\n                                        os.remove(out)",
)
