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

  B. extensions/provider.py — SpotiFLAC 1.8.0 deleted the whole providers/
     package (amazon.py, deezer.py, qobuz.py, tidal.py, etc. — the download
     backends are no longer bundled at all; see the module-level note below).
     Every JS-extension-backed download now flows through one shared hook,
     JSExtensionProvider.download() in extensions/provider.py, which only
     attempted an MB lookup when an ISRC was present (`if enrich_metadata and
     metadata.isrc:`) and used the plain ISRC-only fetch — same bug as the old
     per-provider patches, but now a single patch point fixes it for every
     JS extension at once. Python-style extensions (.sflx packages) bypass
     this hook entirely and are outside this script's reach — their MB
     tagging, if any, is whatever that third-party extension implements.

NOTE for version bumps: as of 1.8.0, SpotiFLAC ships NO bundled download
providers — Tidal/Qobuz/Amazon/Deezer/etc. are all supplied by externally
hosted "extensions" the operator installs from a registry URL they configure
themselves (see worker.refresh_extensions / Settings → System → Extensions).
The old providers/*.py patches (sync AsyncMBFetch-style and async
await-fetch_mb_metadata_async-style, covering amazon/apple_music/pandora/
youtube/gdstudio/deezer/qobuz/tidal, plus the amazon.py orphaned-FLAC cleanup)
were removed from this file in the 1.7.8 -> 3.0.4 port because those files no
longer exist. If a future release reshapes extensions/provider.py's MB block,
the affected patch will log "pattern not found — skipping" and must be
re-ported (see git history for the 1.2.0 -> 1.3.1, 1.3.1 -> 1.4.5, 1.4.5 ->
1.7.8, and 1.7.8 -> 3.0.4 ports — the last one is the provider-architecture
cutover, not just a call-shape change).
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
# NOTE: 1.7.8 added a `-> None` return annotation vs 1.4.5 — both variants are
# tried since older pins may still hit this same patch file.
for _init_old in (
    (
        "    def __init__(self, isrc: str) -> None:\n"
        "        self.isrc = isrc\n"
        "        try:\n"
        "            self.future = self._get_executor().submit(fetch_mb_metadata, isrc)\n"
        "        except RuntimeError:\n"
        "            self.future = self._get_executor().submit(fetch_mb_metadata, isrc)\n"
    ),
    (
        "    def __init__(self, isrc: str):\n"
        "        self.isrc = isrc\n"
        "        try:\n"
        "            self.future = self._get_executor().submit(fetch_mb_metadata, isrc)\n"
        "        except RuntimeError:\n"
        "            self.future = self._get_executor().submit(fetch_mb_metadata, isrc)\n"
    ),
):
    _apply(
        "core/musicbrainz.py",
        _init_old,
        _init_old.replace(
            "def __init__(self, isrc: str)",
            "def __init__(self, isrc: str, title: str = \"\", artist: str = \"\")",
        ).replace(
            "submit(fetch_mb_metadata, isrc)", "submit(fetch_mb_metadata_smart, isrc, title, artist)"
        ),
        "AsyncMBFetch now takes title/artist and uses the smart lookup",
        already_marker="def __init__(self, isrc: str, title: str = \"\"",
    )

# ---------------------------------------------------------------------------
# Patch B: extensions/provider.py — JSExtensionProvider.download()'s shared
# MusicBrainz hook. Covers every JS-extension-backed provider at once (the
# per-provider providers/*.py files this used to target no longer exist as
# of 1.8.0 — see module docstring).
# ---------------------------------------------------------------------------
_apply(
    "extensions/provider.py",
    (
        "        mb_tags: dict[str, str] = {}\n"
        "        if enrich_metadata and metadata.isrc:\n"
        "            try:\n"
        "                from SpotiFLAC.core.isrc_utils import normalize_isrc\n"
        "                from SpotiFLAC.core.musicbrainz import (\n"
        "                    fetch_mb_metadata_async,\n"
        "                    mb_result_to_tags,\n"
        "                )\n"
        "\n"
        "                isrc_clean = normalize_isrc(metadata.isrc)\n"
        "                if isrc_clean:\n"
        "                    mb_data = await fetch_mb_metadata_async(isrc_clean)\n"
        "                    mb_tags = mb_result_to_tags(mb_data)\n"
        "            except Exception as e:\n"
    ),
    (
        "        mb_tags: dict[str, str] = {}\n"
        "        if enrich_metadata:\n"
        "            try:\n"
        "                from SpotiFLAC.core.isrc_utils import normalize_isrc\n"
        "                from SpotiFLAC.core.musicbrainz import (\n"
        "                    fetch_mb_metadata_smart_async,\n"
        "                    mb_result_to_tags,\n"
        "                )\n"
        "\n"
        "                isrc_clean = normalize_isrc(metadata.isrc)\n"
        "                mb_data = await fetch_mb_metadata_smart_async(\n"
        "                    isrc_clean, metadata.title, metadata.first_artist\n"
        "                )\n"
        "                mb_tags = mb_result_to_tags(mb_data)\n"
        "            except Exception as e:\n"
    ),
    "MusicBrainz lookup no longer gated on ISRC presence, uses smart text-search fallback",
    already_marker="fetch_mb_metadata_smart_async(\n                    isrc_clean, metadata.title, metadata.first_artist",
)
