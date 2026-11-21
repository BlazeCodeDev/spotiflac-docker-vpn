"""
Build-time patches for Mornye (targets the pinned version — see
entrypoint.sh MORNYE_PINNED / Dockerfile). Mornye is SpotiFLAC renamed (same
author and code base; the SpotiFLAC package was taken down after a cease and
desist), so the porting history below still applies. Every patch is a guarded,
idempotent string replacement: if the anchor text isn't found (e.g. a version
bump reformatted it) the patch reports and skips rather than corrupting a file.

One infrastructure fix for the extension/Turnstile download path and one
tagging fix — nothing about MusicBrainz lookups any more (see the history
note below):

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
  guard we never had). So the old Patch E is gone too.

* Mornye 1.1.0 stopped bundling the Spotify/Tidal metadata, Turnstile solver
  and signed-session modules (`core/spotify_metadata.py`, `core/solver.py`,
  `core/signed_session_*.py`, ...). They now ship in the registry-installed
  `utilities` extension (see worker._EXTRA_EXT_IDS); `mornye.core.<name>`
  stays importable as a lazy proxy that raises on first use until it is
  installed. That retired Patch F (signed_session_mono's cross-loop lock):
  its file is no longer in the package, and 2.x added a shared
  `core/cross_loop_lock.CrossLoopLock` of the same threading.Lock shape.
  Rolling back below 1.1.0 needs Patch F restored from git.

* Rolling the pin back below 4.0 needs the pre-4.x version of THIS file
  restored from git (it carried Patches A/B/E, which 3.x genuinely needed),
  not just a pin change. Prior porting history for reference:
  1.2.0->1.3.1, 1.3.1->1.4.5, 1.4.5->1.7.8, 1.7.8->3.0.4 (the provider-
  architecture cutover — bundled providers/*.py deleted, replaced by
  operator-installed extensions), 3.0.5->3.8.0, 3.8.0->4.1.0 (A/B/E retired), 4.1.2->5.0.1 (Patch F anchor), 5.0.1->5.0.3 (no patch changes), 5.0.3->5.0.6 (no patch changes), SpotiFLAC 5.0.6->Mornye 1.0.0 (package
  rename; F/G anchors identical, H's SOURCE_TAG URL changed), 1.0.0->1.0.3
  (repo moved to mornyeapp, so H's SOURCE_TAG URL changed again),
  1.0.3->1.0.4 (no patch changes), 1.0.4->1.0.5 (no patch changes),
  1.0.5->2.2.1 (F retired, G/H anchors unchanged).
"""
import importlib.util
import pathlib
import sys

_spec = importlib.util.find_spec("mornye")
if _spec is None or _spec.origin is None:
    print("[patch] mornye not found — aborting")
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

# ---------------------------------------------------------------------------
# Patch H: core/tagger.py — don't stamp the Mornye repo link as a comment
# ---------------------------------------------------------------------------
# The tagger writes SOURCE_TAG (a bare GitHub URL) into every downloaded file:
# ID3 COMM for MP3, the DESCRIPTION Vorbis tag for FLAC (which the M4A map
# turns into ©cmt). Skip both writes. routes.py `_strip_source_comment`
# removes the same comment from files that already have it during enrich.
_apply(
    "core/tagger.py",
    '    audio.add(COMM(encoding=3, lang="eng", desc="", text=[SOURCE_TAG]))\n',
    '    if SOURCE_TAG:\n'
    '        audio.add(COMM(encoding=3, lang="eng", desc="", text=[SOURCE_TAG]))\n',
    "MP3: no source-link COMM frame",
    already_marker="    if SOURCE_TAG:\n        audio.add(COMM(",
)
_apply(
    "core/tagger.py",
    '    tags["DESCRIPTION"] = SOURCE_TAG\n',
    '    if SOURCE_TAG:\n'
    '        tags["DESCRIPTION"] = SOURCE_TAG\n',
    "FLAC/M4A: no source-link DESCRIPTION tag",
    already_marker='    if SOURCE_TAG:\n        tags["DESCRIPTION"]',
)
for _url in (
    "https://github.com/mornyeapp/Mornye-Python",
    "https://github.com/BartolomeoRusso9/Mornye-Python",
    "https://github.com/BartolomeoRusso9/SpotiFLAC-Module-Version",
):
    _apply(
        "core/tagger.py",
        f'SOURCE_TAG = "{_url}"\n',
        'SOURCE_TAG = ""  # patched out (see patch_mornye.py Patch H)\n',
        "SOURCE_TAG emptied",
        already_marker='SOURCE_TAG = ""',
    )
