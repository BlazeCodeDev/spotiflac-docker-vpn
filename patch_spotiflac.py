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
1.7.8, 1.7.8 -> 3.0.4, and 3.0.5 -> 3.8.0 ports — the 1.7.8 -> 3.0.4 one is
the provider-architecture cutover, not just a call-shape change; 3.0.5 -> 3.8.0
was two independent indentation/import-shape drifts, not architectural).
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
_PROVIDER_MB_OLD_BASE = (
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
)
_PROVIDER_MB_NEW_BASE = (
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
)


def _reindent(text: str, extra_spaces: int) -> str:
    """Shifts every non-blank line right by extra_spaces."""
    if extra_spaces == 0:
        return text
    pad = " " * extra_spaces
    return "\n".join((pad + line if line else line) for line in text.split("\n"))


# extensions/provider.py's download() nested this whole block one indent
# level deeper as of 3.8.0 (3.0.4-3.7.x had it at 8-space base; 3.8.0+ at
# 12-space — indentation-only drift, the code itself is unchanged). Try both
# depths so a downgrade of the pin still patches cleanly too.
for _extra in (0, 4):
    _marker = (
        "fetch_mb_metadata_smart_async(\n"
        + " " * (20 + _extra)
        + "isrc_clean, metadata.title, metadata.first_artist"
    )
    _apply(
        "extensions/provider.py",
        _reindent(_PROVIDER_MB_OLD_BASE, _extra),
        _reindent(_PROVIDER_MB_NEW_BASE, _extra),
        "MusicBrainz lookup no longer gated on ISRC presence, uses smart text-search fallback",
        already_marker=_marker,
    )

# ---------------------------------------------------------------------------
# Patch E: core/signed_session_mobile.py — cross-event-loop/cross-thread auth
# lock bug (crash, and worse, a silent deadlock+correctness bug once "fixed"
# the naive way — see the long comment below before touching this again).
# ---------------------------------------------------------------------------
# _AUTH_LOCKS caches one asyncio.Lock per auth namespace (e.g. "zarz-v2", the
# Tidal/Deezer/Qobuz-web signed-session flow) for the LIFETIME OF THE PROCESS,
# on the documented assumption that "no download starts its own asyncio.run()"
# — i.e. every caller shares one event loop. core/solver.py's Turnstile
# solve()/solve_with_callback() break that assumption: each call wraps its
# work in a fresh asyncio.run(), so it gets its own throwaway loop, and
# JSExtensionProvider's runtime pool can call into this from more than one
# OS thread at once (parallel downloads). Observed symptom #1: "<Lock ...>
# is bound to a different event loop" crashes on every provider using this
# shared layer, after the very first Turnstile solve.
#
# FIRST FIX ATTEMPTED (do not repeat this): make _get_auth_lock "loop-aware"
# by recreating the asyncio.Lock whenever `lock._loop is not
# asyncio.get_running_loop()`. This stopped the crash but broke actual
# mutual exclusion, silently: asyncio.Lock's fast, UNCONTENDED acquire path
# never touches `_loop` at all — only a CONTENDED wait does. So two
# concurrent callers on different loops/threads would each see `_loop`
# unset, both treat the lock as "theirs", and both proceed at once. In
# production this showed up as two coroutines driving the *same* pydoll
# browser tab simultaneously (duplicate "Clicking element" log lines on the
# same object_id, same devtools connection) — worse, if the loop mismatch
# happened to occur on a genuinely CONTENDED cross-thread acquire, the
# waiting side's Future got bound to its own loop, and the releasing
# thread's `future.set_result()` call (not thread-safe across loops) could
# silently never wake it, hanging forever.
#
# ACTUAL FIX: stop using asyncio.Lock for this. A plain threading.Lock has
# none of these gaps (it doesn't care what loop or thread touches it), so
# wrap one in a minimal async-compatible context manager that polls it
# non-blockingly. This is deliberately the same shape as the
# "_AsyncThreadLockCtx" wrapper this file's own comments say used to exist
# for exactly this reason, before being optimized away under the
# single-shared-loop assumption that doesn't hold in this deployment.
_CROSS_LOOP_LOCK_CLASS = '''class _CrossLoopLock:
    """threading.Lock-backed async context manager — unlike asyncio.Lock,
    this genuinely serializes access across different event loops/threads.
    See the long comment in patch_spotiflac.py (Patch E) for why asyncio.Lock
    is unsafe here: its uncontended fast path never binds `_loop`, so a later
    cross-thread contended acquire can silently adopt the wrong loop and
    deadlock on release (Future.set_result isn't thread-safe cross-loop)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    async def __aenter__(self) -> "_CrossLoopLock":
        while not self._lock.acquire(blocking=False):
            await asyncio.sleep(0.02)
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._lock.release()


'''

# 3.8.0 inserted `import tempfile` between secrets/time, breaking the plain
# adjacency anchor — and critically, the lock-swap patch below (which embeds
# a bare `threading.Lock()` call) has no way to detect that *this* patch
# silently failed, so it applied on top of a file with no `import threading`
# at all. Caught only by actually calling _get_auth_lock() under contention,
# not by "applies clean + imports" (see the long Patch E comment above).
for _secrets_anchor in ("import secrets\nimport time\n", "import secrets\nimport tempfile\nimport time\n"):
    _apply(
        "core/signed_session_mobile.py",
        _secrets_anchor,
        _secrets_anchor.replace("import secrets\n", "import secrets\nimport threading\n", 1),
        "added threading import",
        already_marker="import threading\n",
    )

_apply(
    "core/signed_session_mobile.py",
    (
        "_AUTH_LOCKS: dict[str, asyncio.Lock] = {}\n"
        "\n"
        "\n"
        "def _get_auth_lock(namespace: str) -> asyncio.Lock:\n"
        "    \"\"\"Return the asyncio.Lock for the given namespace, creating it if absent.\"\"\"\n"
        "    lock = _AUTH_LOCKS.get(namespace)\n"
        "    if lock is None:\n"
        "        lock = asyncio.Lock()\n"
        "        _AUTH_LOCKS[namespace] = lock\n"
        "    return lock\n"
    ),
    (
        _CROSS_LOOP_LOCK_CLASS
        + "_AUTH_LOCKS: dict[str, _CrossLoopLock] = {}\n"
        "\n"
        "\n"
        "def _get_auth_lock(namespace: str) -> _CrossLoopLock:\n"
        "    \"\"\"Return the cross-loop-safe lock for the given namespace, creating it if absent.\"\"\"\n"
        "    lock = _AUTH_LOCKS.get(namespace)\n"
        "    if lock is None:\n"
        "        lock = _CrossLoopLock()\n"
        "        _AUTH_LOCKS[namespace] = lock\n"
        "    return lock\n"
    ),
    "auth lock is now a threading.Lock-backed cross-loop/cross-thread-safe lock instead of asyncio.Lock",
    already_marker="class _CrossLoopLock:",
)

# ---------------------------------------------------------------------------
# Patch F: core/signed_session_mono.py — same underlying bug (and the same
# rejected first fix — see Patch E's comment), for the Amazon
# "amz.geeked.wtf" bypass's module-level browser-session singleton.
# ---------------------------------------------------------------------------
_apply(
    "core/signed_session_mono.py",
    "import os\nimport time\n",
    "import os\nimport threading\nimport time\n",
    "added threading import",
)

# NOTE: _CrossLoopLock must be inserted as its own top-level class BEFORE
# class _MonochromeBrowserSession, not spliced into its body — an unindented
# `class` statement dropped mid-body would (correctly) dedent Python out of
# the enclosing class, silently reparenting the rest of it as methods of
# _CrossLoopLock instead. Two separate, narrowly-anchored patches avoid that.
_apply(
    "core/signed_session_mono.py",
    "class _MonochromeBrowserSession:\n",
    _CROSS_LOOP_LOCK_CLASS + "class _MonochromeBrowserSession:\n",
    "added _CrossLoopLock helper class",
    already_marker="class _CrossLoopLock:",
)

_apply(
    "core/signed_session_mono.py",
    (
        "        self._browser: Chrome | None = None\n"
        "        self._tab = None\n"
        "        self._lock = asyncio.Lock()\n"
    ),
    (
        "        self._browser: Chrome | None = None\n"
        "        self._tab = None\n"
        "        self._lock = _CrossLoopLock()\n"
    ),
    "browser-session lock is now a threading.Lock-backed cross-loop/cross-thread-safe lock instead of asyncio.Lock",
)

# ---------------------------------------------------------------------------
# Patch G: extensions/_bridge.js — the synchronous JS<->Python bridge call
# (Atomics.wait loop) has a hardcoded 60s ceiling, but session.signedFetch —
# the call a JS extension makes to trigger Python's signed-session/Turnstile
# flow — can legitimately take far longer. core/solver.py's own "hard
# watchdog" (the timeout that force-kills a wedged browser) is deliberately
# sized to (_RELOAD_CHECK_SECONDS + _MAX_NAV_POLL_SECONDS +
# _MAX_IFRAME_RECT_POLL_SECONDS) * _MAX_RELOAD_ATTEMPTS + 10*(attempts-1) +
# 60s cleanup buffer = (10+10+10)*3 + 20 + 60 = 170s as of 3.8.0 — i.e.
# upstream's own Python side expects a legitimate solve to take up to 170s,
# but the JS side of the same round trip gives up at 60s and throws "Bridge
# timeout for session.signedFetch" — a mismatch between the two sides of one
# feature, not a deliberately strict limit. Observed in production: every
# Turnstile-gated provider (deezer here, but the same bridge code path
# covers qobuz-web/amazon/tidal's LOSSLESS-API extensions too) failing with
# exactly this message. Bumped to 200s — comfortably above the 170s Python
# budget with margin, still well under our own DownloadOptions.timeout_s
# (download_timeout_s setting, default 300s) that bounds the whole extension
# call this sits inside.
# ---------------------------------------------------------------------------
_apply(
    "extensions/_bridge.js",
    "      if (waited > 60_000) throw new Error(`Bridge timeout for ${method}`);\n",
    "      if (waited > 200_000) throw new Error(`Bridge timeout for ${method}`);\n",
    "raised the synchronous bridge-call ceiling from 60s to 200s to cover a legitimate Turnstile solve (Python's own watchdog budgets up to 170s)",
)
