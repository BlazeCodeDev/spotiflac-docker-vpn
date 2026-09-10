"""
Build-time patches for SpotiFLAC (targets the pinned version — see
entrypoint.sh SPOTIFLAC_PINNED / Dockerfile). Every patch is a guarded,
idempotent string replacement: if the anchor text isn't found (e.g. a version
bump reformatted it) the patch reports and skips rather than corrupting a file.

Two infrastructure fixes for the extension/Turnstile download path — nothing
about MusicBrainz tagging any more (see the history note below):

  F. core/signed_session_mono.py — the Amazon "amz.geeked.wtf" bypass's
     module-level browser-session singleton guards itself with an
     `asyncio.Lock`, which breaks across event loops: core/solver.py's
     Turnstile solve wraps its work in a fresh `asyncio.run()` per call, and
     the JS-extension runtime can drive this from more than one OS thread at
     once. Swap it for a threading.Lock wrapped in a minimal poll-based async
     context manager (`_CrossLoopLock`).

  G. extensions/_bridge.js — the synchronous JS<->Python bridge call has a
     60 s ceiling (`BRIDGE_TIMEOUT_MS`), but `session.signedFetch` — the call
     a JS extension makes to trigger Python's signed-session/Turnstile flow —
     can legitimately take up to ~170 s (that's how big core/solver.py's own
     hard watchdog is deliberately sized). Raise it to 200 s.

NOTE for version bumps / rollbacks:

* As of SpotiFLAC 4.0, `core/musicbrainz.py`'s `fetch_mb_metadata[_async]`
  natively take keyword-only `title=`/`artist=`/`duration_ms=`/`album=`/
  `total_tracks=`/`release_date=` and do the ISRC-unlinked → title/artist
  text-search fallback themselves (`_fallback_query` / `_pick_fallback_
  recording`). `extensions/provider.py`'s MB hook already passes all of
  those. So the old Patch A (`fetch_mb_metadata_smart[_async]`) and Patch B
  (provider MB hook) are gone — `routes.py`'s `_enrich_setup` calls the
  native `fetch_mb_metadata(isrc, title=…, artist=…)` directly now.

* Also as of 4.0, `core/signed_session_mobile.py` fixes the cross-loop auth
  lock itself (`_AUTH_LOCKS: dict[str, threading.Lock]`, `_AUTH_LOCKS_GUARD`,
  `_AsyncThreadLock` — same shape as _CrossLoopLock, plus a creation-race
  guard we never had). So the old Patch E is gone too. `signed_session_mono`
  was NOT given the same treatment upstream, hence Patch F still exists.

* Rolling the pin back below 4.0 needs the pre-4.x version of THIS file
  restored from git (it carried Patches A/B/E, which 3.x genuinely needed),
  not just a `SPOTIFLAC_PINNED` change. Prior porting history for reference:
  1.2.0->1.3.1, 1.3.1->1.4.5, 1.4.5->1.7.8, 1.7.8->3.0.4 (the provider-
  architecture cutover — bundled providers/*.py deleted, replaced by
  operator-installed extensions), 3.0.5->3.8.0, 3.8.0->4.1.0 (A/B/E retired).
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
# Patch F: core/signed_session_mono.py — cross-event-loop/cross-thread lock
# ---------------------------------------------------------------------------
# `_MonochromeBrowserSession` (Amazon's amz.geeked.wtf bypass) is a
# module-level singleton whose `self._lock` is an asyncio.Lock. That lock is
# bound to whichever event loop first awaited it — but core/solver.py's
# Turnstile solve()/solve_with_callback() each run their work under a fresh
# asyncio.run(), and the JS-extension runtime pool can call in from more than
# one OS thread for parallel downloads. Result: "<Lock ...> is bound to a
# different event loop" crashes, or — with the naive "recreate the lock if
# lock._loop isn't the running loop" fix — silent loss of mutual exclusion
# (asyncio.Lock's uncontended acquire never touches _loop) and, on a genuinely
# contended cross-thread acquire, a permanent hang (Future.set_result isn't
# thread-safe cross-loop).
#
# Fix: a threading.Lock (belongs to no loop) wrapped in a minimal async
# context manager that polls it non-blockingly — the same shape SpotiFLAC's
# own core/signed_session_mobile.py adopted natively in 4.0 (_AsyncThreadLock).
# Verified against a real repro: two OS threads, each its own asyncio.run(),
# contending on the shared singleton lock with blocking work inside the
# critical section — old code hangs, this doesn't.
_CROSS_LOOP_LOCK_CLASS = '''class _CrossLoopLock:
    """threading.Lock-backed async context manager — unlike asyncio.Lock,
    this genuinely serializes access across different event loops/threads.
    asyncio.Lock is unsafe here: its uncontended fast path never binds
    `_loop`, so a later cross-thread contended acquire can silently adopt the
    wrong loop and deadlock on release (Future.set_result isn't thread-safe
    cross-loop). See patch_spotiflac.py Patch F."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    async def __aenter__(self) -> "_CrossLoopLock":
        while not self._lock.acquire(blocking=False):
            await asyncio.sleep(0.02)
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._lock.release()


'''

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
# Patch G: extensions/_bridge.js — synchronous bridge-call ceiling
# ---------------------------------------------------------------------------
# The JS-worker-thread `bridgeCall()` blocks synchronously (Atomics.wait on a
# SharedArrayBuffer) until Python answers, capped by BRIDGE_TIMEOUT_MS. That
# cap covers `session.signedFetch`, which on the Python side triggers the
# whole signed-session/Turnstile flow (core/solver.py: browser launch +
# Cloudflare challenge). core/solver.py's own hard watchdog for that solve is
# deliberately sized to ~170 s ((10+10+10)*3 + 20 + 60), so a legitimate
# solve routinely outlives the 60 s bridge cap → "Bridge timeout for
# session.signedFetch" on every Turnstile-gated extension. Raise the cap to
# 200 s: above the 170 s Python budget, still under our own
# DownloadOptions.timeout_s (download_timeout_s, default 300 s) that bounds
# the whole extension .download() call this sits inside.
#
# 4.0 refactored this from an inline `if (waited > 60_000)` to a named const
# with a separate long timeout for file transfers (TRANSFER_METHODS); only
# BRIDGE_TIMEOUT_MS needs changing — session.signedFetch is not a transfer
# method. Older 3.x pins used the inline literal — try both.
for _old, _new in (
    (
        "  const BRIDGE_TIMEOUT_MS   = 60_000;\n",
        "  const BRIDGE_TIMEOUT_MS   = 200_000;\n",
    ),
    (
        "      if (waited > 60_000) throw new Error(`Bridge timeout for ${method}`);\n",
        "      if (waited > 200_000) throw new Error(`Bridge timeout for ${method}`);\n",
    ),
):
    _apply(
        "extensions/_bridge.js",
        _old,
        _new,
        "raised the synchronous bridge-call ceiling from 60s to 200s to cover a legitimate Turnstile solve (Python's own watchdog budgets up to ~170s)",
        already_marker="200_000",
    )
