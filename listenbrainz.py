import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_LB_API  = "https://api.listenbrainz.org/1"
_MB_API  = "https://musicbrainz.org/ws/2"
_MB_UA   = "SpotiFLAC-UI/1.0 ( ralf.lehmann10@gmail.com )"

_STATE_FILE = os.environ.get("LB_STATE_FILE", "/vpn/lb_state.json")

_lock   = threading.Lock()
_state: dict = {
    "running":        False,
    "last_check":     None,
    "last_error":     None,
    "next_check":     None,
    "playlists":      [],   # [{mbid, title, last_modified, track_count, enqueued, skipped}]
    "total_enqueued": 0,
}

_poll_event   = threading.Event()
_mb_rate_lock = threading.Lock()
_mb_last_req  = 0.0


# ── Persistence ───────────────────────────────────────────────────────────────

def _save() -> None:
    try:
        d = os.path.dirname(os.path.abspath(_STATE_FILE))
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_state, f, indent=2)
        os.replace(tmp, _STATE_FILE)
    except Exception as exc:
        log.warning("LB state save failed: %s", exc)


def _load() -> None:
    try:
        with open(_STATE_FILE) as f:
            data = json.load(f)
        for k in ("last_check", "last_error", "next_check", "playlists", "total_enqueued"):
            if k in data:
                _state[k] = data[k]
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning("LB state load failed: %s", exc)


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _http_get(url: str, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def _mb_spotify_url(mbid: str) -> str | None:
    """Look up a MusicBrainz recording and return its Spotify track URL (rate-limited to ~1 req/s)."""
    global _mb_last_req
    with _mb_rate_lock:
        wait = 1.1 - (time.time() - _mb_last_req)
        if wait > 0:
            time.sleep(wait)
        _mb_last_req = time.time()
    try:
        url  = f"{_MB_API}/recording/{mbid}?inc=url-rels&fmt=json"
        data = _http_get(url, headers={"User-Agent": _MB_UA})
        for rel in data.get("relations", []):
            resource = (rel.get("url") or {}).get("resource", "")
            if "open.spotify.com/track/" in resource:
                return resource
    except Exception as exc:
        log.debug("MB lookup failed for %s: %s", mbid, exc)
    return None


# ── Core logic ────────────────────────────────────────────────────────────────

def _mbid_from_lb_url(lb_url: str) -> str:
    """Extract UUID from https://listenbrainz.org/playlist/{uuid}"""
    return lb_url.rstrip("/").split("/")[-1]


def _spotify_url_for_track(track: dict) -> str | None:
    """
    Extract a Spotify track URL from a JSPF track object.

    Fast path: spotify_track_id in extension additional_metadata.
    Slow path: MusicBrainz URL-rels lookup (rate-limited; ~1 req/s).
    """
    ext  = track.get("extension", {})
    jspf = ext.get("https://musicbrainz.org/doc/jspf#track", {})
    meta = jspf.get("additional_metadata", {})
    sid  = meta.get("spotify_track_id")
    if sid:
        return f"https://open.spotify.com/track/{sid}"

    identifiers = track.get("identifier", [])
    if isinstance(identifiers, str):
        identifiers = [identifiers]
    for ident in identifiers:
        if "musicbrainz.org/recording/" in ident:
            mbid = ident.rstrip("/").split("/")[-1]
            url  = _mb_spotify_url(mbid)
            if url:
                return url
    return None


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_playlist_tracks(mbid: str) -> list[dict]:
    """Fetch the full playlist and return its track list."""
    url  = f"{_LB_API}/playlist/{urllib.parse.quote(mbid)}"
    data = _http_get(url, headers={"User-Agent": _MB_UA})
    playlist = data.get("playlist", data)
    return playlist.get("track", [])


def _do_sync(username: str, cancel: threading.Event | None = None) -> dict:
    """Fetch and process recommendation playlists. Returns a result summary dict."""
    url  = f"{_LB_API}/user/{urllib.parse.quote(username)}/playlists/recommendations"
    data = _http_get(url, headers={"User-Agent": _MB_UA})
    raw_playlists = data.get("playlists", [])

    with _lock:
        prev = {p["mbid"]: p for p in _state.get("playlists", []) if p.get("mbid")}

    import settings as _settings
    import worker
    from config import Config

    processed   = []
    total_enq   = 0

    for item in raw_playlists:
        if cancel and cancel.is_set():
            break

        playlist = item.get("playlist", item)
        ext      = playlist.get("extension", {})
        jspf     = ext.get("https://musicbrainz.org/doc/jspf#playlist", {})

        # MBID lives in the identifier URL
        lb_url   = playlist.get("identifier", "")
        mbid     = _mbid_from_lb_url(lb_url) if lb_url else ""
        title    = playlist.get("title", "Unknown")
        last_mod = jspf.get("last_modified_at", "")

        if not mbid:
            continue

        known_mod = prev.get(mbid, {}).get("last_modified")
        if known_mod and known_mod == last_mod:
            # Unchanged — carry forward previous stats
            p = prev[mbid]
            processed.append({
                "mbid":         mbid,
                "title":        title,
                "last_modified": last_mod,
                "track_count":  p.get("track_count", 0),
                "enqueued":     p.get("enqueued", 0),
                "skipped":      p.get("skipped", 0),
                "new":          False,
            })
            log.debug("LB: skipping unchanged playlist '%s'", title)
            continue

        # New or updated playlist — fetch tracks
        try:
            tracks = _fetch_playlist_tracks(mbid)
        except Exception as exc:
            log.warning("LB: could not fetch playlist '%s': %s", title, exc)
            continue

        cfg      = _settings.load()
        enqueued = skipped = 0
        for track in tracks:
            if cancel and cancel.is_set():
                break
            spotify = _spotify_url_for_track(track)
            if not spotify:
                skipped += 1
                log.debug("LB: no Spotify URL for '%s'", track.get("title", "?"))
                continue
            worker.enqueue(
                url          = spotify,
                output_dir   = Config.OUTPUT_DIR,
                services     = cfg["services"],
                filename_fmt = cfg["filename_fmt"],
                qobuz_token  = cfg["qobuz_token"],
                quality      = "lossless",
                pre_title    = track.get("title") or "",
            )
            enqueued += 1

        total_enq += enqueued
        log.info("LB: playlist '%s' — %d enqueued, %d skipped", title, enqueued, skipped)

        processed.append({
            "mbid":         mbid,
            "title":        title,
            "last_modified": last_mod,
            "track_count":  len(tracks),
            "enqueued":     enqueued,
            "skipped":      skipped,
            "new":          True,
        })

    return {"playlists": processed, "total_enqueued": total_enq}


# ── Background sync ───────────────────────────────────────────────────────────

_sync_cancel = threading.Event()


def _run_sync(username: str) -> None:
    """Run a full sync in the calling thread; updates _state throughout."""
    with _lock:
        _state["running"]    = True
        _state["last_error"] = None
    _sync_cancel.clear()

    try:
        result = _do_sync(username, _sync_cancel)
        now    = _now_utc()
        with _lock:
            _state["running"]        = False
            _state["last_check"]     = now
            _state["last_error"]     = None
            _state["playlists"]      = result["playlists"]
            _state["total_enqueued"] = (
                _state.get("total_enqueued", 0) + result["total_enqueued"]
            )
        _save()
    except Exception as exc:
        log.error("LB sync failed: %s", exc)
        now = _now_utc()
        with _lock:
            _state["running"]    = False
            _state["last_check"] = now
            _state["last_error"] = str(exc)
        _save()


def sync_now_bg(username: str = "") -> None:
    """Start an immediate sync in a background daemon thread."""
    import settings as _settings
    u = username or _settings.load().get("listenbrainz_username", "").strip()
    if not u:
        return
    with _lock:
        if _state.get("running"):
            return
    threading.Thread(target=_run_sync, args=(u,), daemon=True, name="lb-sync").start()


def trigger_sync() -> None:
    """Wake up the poll loop so it runs immediately."""
    _poll_event.set()


def _poll_loop() -> None:
    import settings as _settings
    poll_min = 60
    while True:
        try:
            cfg      = _settings.load()
            enabled  = cfg.get("listenbrainz_enabled", False)
            username = cfg.get("listenbrainz_username", "").strip()
            poll_min = max(5, int(cfg.get("listenbrainz_poll_minutes", 60)))

            if enabled and username:
                _run_sync(username)

            next_ts  = datetime.now(timezone.utc).timestamp() + poll_min * 60
            next_iso = datetime.fromtimestamp(next_ts, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            with _lock:
                _state["next_check"] = next_iso
            _save()
        except Exception as exc:
            log.error("LB poll loop error: %s", exc)

        _poll_event.wait(timeout=poll_min * 60)
        _poll_event.clear()


def start() -> None:
    _load()
    threading.Thread(target=_poll_loop, daemon=True, name="lb-poll").start()
    log.info("ListenBrainz poller started")


def get_state() -> dict:
    with _lock:
        return dict(_state)
