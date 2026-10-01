import hashlib
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata

import json

from flask import Blueprint, Response, jsonify, render_template, request, send_file, stream_with_context

import settings as _settings
import worker
import vpn
import lib_index
from config import Config

log = logging.getLogger(__name__)

bp = Blueprint("main", __name__)

# ── Background enrichment state ───────────────────────────────────────────────
_enrich_lock   = threading.Lock()
_enrich_cancel = threading.Event()
_enrich_state: dict = {
    "running":     False,
    "total":       0,
    "done":        0,
    "enriched":    0,
    "moved":       0,
    "dupes":       0,
    "errors":      0,
    "label":       "",
    "elapsed":     None,
    "started_at":  None,  # time.monotonic() when thread began; used for live ETA
    "error_log":   [],   # list of {"path": str, "error": str}
    "moved_log":   [],   # list of {"from": str, "to": str}
    "dupes_log":   [],   # list of {"removed": str, "kept": str}
}

_VALID_SERVICES   = {"tidal", "qobuz", "amazon", "deezer", "youtube"}
_VALID_QUALITIES  = {"high", "lossless", "hires"}

# Module-level client so the OAuth token is cached and reused across requests.
try:
    from SpotiFLAC.core.spotify_metadata import SpotifyMetadataClient as _SpotifyClient
    _spotify = _SpotifyClient(timeout_s=15)
except Exception:
    _spotify = None


def _patch_spotify_client(client):
    """Backfill _get() for older SpotiFLAC builds that lack it.

    Some intermediate releases ship a SpotifyMetaDataClient class whose
    public methods (get_track, get_album_tracks, …) call self._get()
    internally but the method itself was never defined.  We inject a
    compatible implementation directly onto the instance so every call
    path — our own and the library's — resolves correctly.
    """
    if client is None or hasattr(client, '_get'):
        return client
    import base64
    import json
    import time
    import types
    import urllib.parse
    import urllib.request

    _CID     = base64.b64decode("ODNlNDQzMGI0NzAwNDM0YmFhMjEyMjhhOWM3ZDExYzU=").decode()
    _CSEC    = base64.b64decode("OWJiOWUxMzFmZjI4NDI0Y2I2YTQyMGFmZGY0MWQ0NGE=").decode()
    _TOK_URL = "https://accounts.spotify.com/api/token"
    _API_BASE = "https://api.spotify.com/v1"

    def _get(self, path, **kwargs):
        now = time.time()
        if not getattr(self, '_compat_tok', '') or now >= getattr(self, '_compat_exp', 0) - 60:
            auth = base64.b64encode(f"{_CID}:{_CSEC}".encode()).decode()
            req = urllib.request.Request(
                _TOK_URL,
                data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
                headers={"Authorization": f"Basic {auth}",
                         "Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read())
            self._compat_tok = body["access_token"]
            self._compat_exp = now + body.get("expires_in", 3600)
        params = kwargs.get("params")
        url = f"{_API_BASE}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._compat_tok}"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    client._get = types.MethodType(_get, client)
    return client


if _spotify is not None:
    _patch_spotify_client(_spotify)


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _read_git_commit() -> str:
    # GIT_COMMIT env overrides the baked-in file (handy for local/dev runs
    # outside the Docker image, where /app/GIT_COMMIT won't exist).
    env = os.environ.get("GIT_COMMIT", "").strip()
    if env:
        return env
    try:
        with open("/app/GIT_COMMIT") as f:
            return f.read().strip()
    except OSError:
        return ""


_GIT_COMMIT = _read_git_commit()



def _asset_version() -> int:
    """Changes whenever a file of the UI changes, so browsers never run a stale script or stylesheet."""
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "app")
    try:
        return int(max(os.stat(os.path.join(root, f)).st_mtime for f in os.listdir(root)))
    except (OSError, ValueError):
        return 0


@bp.get("/")
def index():
    return render_template("app.html", git_commit=_GIT_COMMIT, asset_v=_asset_version())


@bp.get("/classic")
def classic():
    """The previous interface, kept as a fallback (and for Organize, which the new one doesn't have yet)."""
    cfg = _settings.load()
    return render_template(
        "index.html",
        services=cfg["services"],
        filename_fmt=cfg["filename_fmt"],
        git_commit=_GIT_COMMIT,
        quality=cfg.get("quality", "lossless"),
    )


@bp.post("/api/download")
def api_download():
    body = request.get_json(silent=True) or {}
    raw  = body.get("urls", "")
    urls = [u.strip() for u in raw.replace(",", "\n").splitlines() if u.strip()]
    if not urls:
        return jsonify(error="No URLs provided"), 400

    cfg = _settings.load()

    # Whitelist services — reject unknown values
    raw_services = body.get("services", cfg["services"])
    services = [s for s in raw_services if s in _VALID_SERVICES]
    if not services:
        return jsonify(error="No valid services specified"), 400

    raw_quality = body.get("quality", "lossless")
    quality = raw_quality if raw_quality in _VALID_QUALITIES else "lossless"
    qobuz_token = str(body.get("qobuz_token") or cfg["qobuz_token"])

    # Optional offset fields for partial retries
    pre_success_count = _safe_int(body.get("pre_success_count", 0), 0)
    full_total        = _safe_int(body.get("full_total", 0), 0)
    pre_title         = str(body.get("pre_title", ""))

    # "always"/"never" override whatever the client sent; "ask" trusts the
    # client's answer to the per-download prompt.
    m3u_mode = cfg.get("m3u_mode", "ask")
    if m3u_mode == "always":
        generate_m3u = True
    elif m3u_mode == "never":
        generate_m3u = False
    else:
        generate_m3u = bool(body.get("generate_m3u", False))

    os.makedirs(Config.OUTPUT_DIR, exist_ok=True)

    common = dict(
        output_dir=Config.OUTPUT_DIR,
        services=services,
        filename_fmt=cfg["filename_fmt"],
        qobuz_token=qobuz_token,
        quality=quality,
        generate_m3u=generate_m3u,
    )

    # Partial batch retry: multiple track URLs with an offset → one job
    if full_total and len(urls) > 1:
        ids = [worker.enqueue(
            url=urls[0],
            batch_urls=urls,
            pre_title=pre_title,
            pre_success_count=pre_success_count,
            full_total=full_total,
            **common,
        )]
    else:
        ids = [worker.enqueue(url=url, **common) for url in urls]

    return jsonify(queued=len(ids), ids=ids)


@bp.get("/api/jobs")
def api_jobs():
    return jsonify(worker.get_jobs())


@bp.delete("/api/jobs")
def api_clear():
    return jsonify(cleared=worker.clear_done())


@bp.delete("/api/jobs/<job_id>")
def api_remove_job(job_id: str):
    ok = worker.remove_job(job_id)
    return (jsonify(ok=True), 200) if ok else (jsonify(error="Not found"), 404)


@bp.post("/api/jobs/<job_id>/cancel")
def api_cancel_job(job_id: str):
    ok = worker.cancel_job(job_id)
    return (jsonify(ok=True), 200) if ok else (jsonify(error="Not found or not cancellable"), 400)


@bp.post("/api/jobs/reorder")
def api_reorder_jobs():
    body = request.get_json(silent=True) or {}
    ids = body.get("ids", [])
    if not isinstance(ids, list):
        return jsonify(error="ids must be a list"), 400
    worker.reorder_jobs(ids)
    return jsonify(ok=True)


@bp.post("/api/jobs/<job_id>/retry")
def api_retry_job(job_id: str):
    ok = worker.retry_job(job_id) or worker.retry_now(job_id)
    return (jsonify(ok=True), 200) if ok else (jsonify(error="Not found or not retryable"), 400)


@bp.post("/api/jobs/<job_id>/retry-partial")
def api_retry_job_partial(job_id: str):
    body = request.get_json(silent=True) or {}
    urls_raw = str(body.get("urls", "")).strip().splitlines()
    urls = [u.strip() for u in urls_raw if u.strip()]
    pre_success_count = _safe_int(body.get("pre_success_count", 0), 0)
    full_total        = _safe_int(body.get("full_total",        0), 0)
    ok = worker.retry_job_partial(job_id, urls, pre_success_count, full_total)
    return (jsonify(ok=True), 200) if ok else (jsonify(error="Not found or not retryable"), 400)


@bp.post("/api/vpn/reconnect")
def api_vpn_reconnect():
    try:
        open("/tmp/vpn_reconnect", "w").close()
        return jsonify(ok=True)
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@bp.post("/api/tidal/refresh")
def api_tidal_refresh():
    try:
        urls = worker.refresh_tidal_api_list(force=True)
        return jsonify(ok=True, count=len(urls))
    except Exception as exc:
        log.warning("Tidal API refresh failed: %s", exc)
        return jsonify(ok=False, error=str(exc)), 502


@bp.get("/api/extensions/status")
def api_extensions_status():
    try:
        return jsonify(worker.extensions_status())
    except Exception as exc:
        log.warning("Extension status check failed: %s", exc)
        return jsonify(any_installed=False, installed=[], missing=[], error=str(exc)), 500


@bp.post("/api/extensions/refresh")
def api_extensions_refresh():
    try:
        results = worker.refresh_extensions(force=True)
        if not results:
            return jsonify(ok=False, error="No extension registries configured"), 400
        return jsonify(ok=True, results=results)
    except Exception as exc:
        log.warning("Extension refresh failed: %s", exc)
        return jsonify(ok=False, error=str(exc)), 502


@bp.get("/api/vpn")
def api_vpn():
    status = vpn.tunnel_status()
    status.update(country=Config.VPN_COUNTRY, protocol=Config.VPN_PROTOCOL)
    return jsonify(status)


@bp.get("/api/ip")
def api_ip():
    data = vpn.ip_info()
    if "error" in data:
        return jsonify(data), 503
    return jsonify(data)


@bp.get("/api/search")
def api_search():
    q      = request.args.get("q", "").strip()
    offset = _safe_int(request.args.get("offset", 0), 0)
    limit  = 6
    if not q:
        return jsonify(error="No query provided"), 400
    try:
        client = _spotify
        if client is None:
            from SpotiFLAC.core.spotify_metadata import SpotifyMetadataClient
            client = _patch_spotify_client(SpotifyMetadataClient(timeout_s=15))
        data   = client._get("/search", params={
            "q": q, "type": "track,album,playlist,artist",
            "limit": limit, "offset": offset,
        })
        results = []
        tracks_obj    = data.get("tracks", {})
        albums_obj    = data.get("albums", {})
        playlists_obj = data.get("playlists", {})
        artists_obj   = data.get("artists", {})

        _seen_tracks: set = set()
        for t in tracks_obj.get("items", []):
            if not t:
                continue
            artists      = ", ".join(a["name"] for a in t.get("artists", []))
            first_artist = (t.get("artists") or [{}])[0].get("name", "")
            # Deduplicate tracks by (title, first artist) — same song appears
            # across multiple album editions (original, deluxe, remaster, etc.)
            _track_key = (re.sub(r"[^\w]", "", t["name"].lower()),
                          re.sub(r"[^\w]", "", first_artist.lower()))
            if _track_key in _seen_tracks:
                continue
            _seen_tracks.add(_track_key)
            album    = t.get("album", {})
            imgs     = album.get("images", [])
            year     = (album.get("release_date") or "")[:4]
            results.append({
                "type":        "track",
                "title":       t["name"],
                "subtitle":    f"{artists} · {album.get('name', '')}" if artists else album.get("name", ""),
                "cover_url":   imgs[-1]["url"] if imgs else None,
                "url":         f"https://open.spotify.com/track/{t['id']}",
                "duration_ms": t.get("duration_ms"),
                "year":        year or None,
            })
        _seen_albums: set = set()
        for a in albums_obj.get("items", []):
            if not a:
                continue
            artists      = ", ".join(ar["name"] for ar in a.get("artists", []))
            first_artist = (a.get("artists") or [{}])[0].get("name", "")
            track_count  = a.get("total_tracks") or 0
            # Deduplicate by (normalised title, first artist, track count).
            # Spotify catalogues the same album under multiple IDs with different
            # release dates. They always share the same name and track count, so
            # that triple is a reliable identity signal.  Albums that genuinely
            # differ (e.g. a Deluxe with bonus tracks) have a different count and
            # are kept as separate results.
            _album_key = (re.sub(r"[^\w]", "", a["name"].lower()),
                          re.sub(r"[^\w]", "", first_artist.lower()),
                          track_count)
            if _album_key in _seen_albums:
                continue
            _seen_albums.add(_album_key)
            imgs    = a.get("images", [])
            year    = (a.get("release_date") or "")[:4]
            results.append({
                "type":        "album",
                "title":       a["name"],
                "subtitle":    artists,
                "cover_url":   imgs[-1]["url"] if imgs else None,
                "url":         f"https://open.spotify.com/album/{a['id']}",
                "track_count": track_count or None,
                "year":        year or None,
            })
        for p in playlists_obj.get("items", []):
            if not p:
                continue
            owner = (p.get("owner") or {}).get("display_name", "")
            imgs  = p.get("images", [])
            results.append({
                "type":        "playlist",
                "title":       p["name"],
                "subtitle":    f"by {owner}" if owner else "",
                "cover_url":   imgs[-1]["url"] if imgs else None,
                "url":         f"https://open.spotify.com/playlist/{p['id']}",
                "track_count": (p.get("tracks") or {}).get("total"),
            })

        for a in artists_obj.get("items", []):
            if not a:
                continue
            imgs      = a.get("images", [])
            genres    = a.get("genres", [])
            followers = (a.get("followers") or {}).get("total", 0)
            subtitle  = genres[0].title() if genres else (f"{followers:,} followers" if followers else "")
            results.append({
                "type":      "artist",
                "title":     a["name"],
                "subtitle":  subtitle,
                "cover_url": imgs[-1]["url"] if imgs else None,
                "url":       f"https://open.spotify.com/artist/{a['id']}",
            })

        total = max(
            tracks_obj.get("total") or 0,
            albums_obj.get("total") or 0,
            playlists_obj.get("total") or 0,
            artists_obj.get("total") or 0,
        )
        has_more = (offset + limit) < total

        return jsonify(results=results, has_more=has_more, next_offset=offset + limit)
    except Exception as exc:
        log.warning("Search failed (%s): %s", type(exc).__name__, exc)
        return jsonify(error="Search failed"), 502


@bp.get("/api/search/expand")
def api_search_expand():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify(error="No URL provided"), 400
    try:
        client = _spotify
        if client is None:
            from SpotiFLAC.core.spotify_metadata import SpotifyMetadataClient
            client = _patch_spotify_client(SpotifyMetadataClient(timeout_s=15))
        name, tracks, *_ = client.get_url(url)
        return jsonify(
            title=name,
            tracks=[{
                "title":        t.title,
                "artists":      t.artists,
                "duration_ms":  t.duration_ms,
                "track_number": t.track_number,
                "url":          t.external_url,
                "cover_url":    t.cover_url,
                "year":         t.year,
            } for t in tracks],
        )
    except Exception as exc:
        log.warning("Expand failed: %s", exc)
        return jsonify(error=str(exc)), 502


# ── Library ───────────────────────────────────────────────────────────────────

def _lib_root() -> str:
    return os.path.realpath(os.path.abspath(Config.OUTPUT_DIR))


def _safe_lib_path(rel: str = "") -> str:
    root = _lib_root()
    rel  = (rel or "").lstrip("/")
    if not rel:
        return root
    target = os.path.realpath(os.path.join(root, rel))
    if target != root and not target.startswith(root + os.sep):
        raise ValueError("Path is outside the library")
    return target


def _lib_rel(abs_path: str) -> str:
    root = _lib_root()
    if abs_path == root:
        return ""
    return os.path.relpath(abs_path, root).replace(os.sep, "/")


def _scan_dir(target: str) -> list[tuple[os.DirEntry, os.stat_result]]:
    """One directory read + one stat per entry (scandir gets file vs folder
    from the directory listing itself — no extra isdir() round trips, which
    matters over network storage)."""
    out = []
    with os.scandir(target) as it:
        for e in it:
            try:
                out.append((e, e.stat()))
            except OSError:
                pass
    return out


def _dir_stamp(items) -> str:
    parts = sorted(f"{e.name}:{st.st_mtime:.0f}:{st.st_size}" for e, st in items)
    return hashlib.md5("\n".join(parts).encode()).hexdigest()[:12]


@bp.get("/api/library")
def api_library():
    rel = request.args.get("path", "")
    try:
        target = _safe_lib_path(rel)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if not os.path.isdir(target):
        return jsonify(error="Not a directory"), 404
    try:
        items = _scan_dir(target)
    except PermissionError:
        return jsonify(error="Permission denied"), 403
    base = _lib_rel(target)
    entries = []
    for e, st in sorted(items, key=lambda x: (not x[0].is_dir(), x[0].name.lower())):
        is_dir = e.is_dir()
        entries.append({
            "name":  e.name,
            "type":  "dir" if is_dir else "file",
            "size":  0 if is_dir else st.st_size,
            "mtime": st.st_mtime,
            "path":  f"{base}/{e.name}" if base else e.name,
        })
    # The change-stamp comes with the listing so the page needn't re-read the
    # folder straight away just to learn it.
    return jsonify(path=rel, entries=entries, stamp=_dir_stamp(items))


@bp.get("/api/library/stamp")
def api_library_stamp():
    rel = request.args.get("path", "")
    try:
        target = _safe_lib_path(rel)
    except ValueError:
        return jsonify(stamp=""), 200
    try:
        stamp = _dir_stamp(_scan_dir(target))
    except Exception:
        stamp = ""
    return jsonify(stamp=stamp)


def _audio_rels_under(abs_path: str) -> list[str]:
    """Library-relative paths of the audio file, or of every audio file inside a folder."""
    if os.path.isfile(abs_path):
        return [_lib_rel(abs_path)] if os.path.splitext(abs_path)[1].lower() in _AUDIO_EXTS else []
    out = []
    for dp, _dirs, fns in os.walk(abs_path):
        out += [_lib_rel(os.path.join(dp, f)) for f in fns if os.path.splitext(f)[1].lower() in _AUDIO_EXTS]
    return out


def _remap_rels(rels: list[str], old_base: str, new_base: str) -> dict[str, str]:
    return {r: new_base + r[len(old_base):] for r in rels}


@bp.delete("/api/library/file")
def api_library_delete():
    rel = request.args.get("path", "")
    if not rel:
        return jsonify(error="No path provided"), 400
    try:
        target = _safe_lib_path(rel)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if not os.path.exists(target):
        return jsonify(error="Not found"), 404
    try:
        rels = _audio_rels_under(target)
        if os.path.isdir(target):
            shutil.rmtree(target)
        else:
            os.remove(target)
        _scans_files_changed(removed=rels)
        lib_index.trigger_rescan()
        return jsonify(ok=True)
    except Exception as exc:
        log.error("Library delete failed: %s", exc)
        return jsonify(error=str(exc)), 500


@bp.post("/api/library/rename")
def api_library_rename():
    body     = request.get_json(silent=True) or {}
    rel      = body.get("path", "")
    new_name = body.get("name", "").strip()
    if not rel or not new_name:
        return jsonify(error="Missing path or name"), 400
    if "/" in new_name or "\\" in new_name:
        return jsonify(error="Name must not contain path separators"), 400
    try:
        target = _safe_lib_path(rel)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if not os.path.exists(target):
        return jsonify(error="Not found"), 404
    dest = os.path.join(os.path.dirname(target), new_name)
    if os.path.exists(dest):
        return jsonify(error="A file or folder with that name already exists"), 409
    try:
        rels = _audio_rels_under(target)
        os.rename(target, dest)
        _scans_files_changed(renamed=_remap_rels(rels, _lib_rel(target), _lib_rel(dest)))
        lib_index.trigger_rescan()
        return jsonify(ok=True)
    except Exception as exc:
        log.error("Library rename failed: %s", exc)
        return jsonify(error=str(exc)), 500


@bp.post("/api/library/move")
def api_library_move():
    body    = request.get_json(silent=True) or {}
    rel_src = body.get("path", "")
    rel_dst = body.get("dest", "").strip("/")
    if not rel_src:
        return jsonify(error="Missing source path"), 400
    try:
        src     = _safe_lib_path(rel_src)
        dst_dir = _safe_lib_path(rel_dst)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if not os.path.exists(src):
        return jsonify(error="Source not found"), 404
    try:
        os.makedirs(dst_dir, exist_ok=True)
    except Exception as exc:
        return jsonify(error=f"Cannot create destination: {exc}"), 500
    dest = os.path.join(dst_dir, os.path.basename(src))
    if os.path.exists(dest):
        return jsonify(error=f'"{os.path.basename(src)}" already exists at destination'), 409
    try:
        rels = _audio_rels_under(src)
        shutil.move(src, dest)
        _scans_files_changed(renamed=_remap_rels(rels, _lib_rel(src), _lib_rel(dest)))
        lib_index.trigger_rescan()
        return jsonify(ok=True)
    except Exception as exc:
        log.error("Library move failed: %s", exc)
        return jsonify(error=str(exc)), 500


@bp.get("/api/library/search")
def api_library_search():
    track  = request.args.get("track",  "").strip().lower()
    artist = request.args.get("artist", "").strip().lower()
    album  = request.args.get("album",  "").strip().lower()
    year   = request.args.get("year",   "").strip().lower()
    if not any([track, artist, album, year]):
        return jsonify(error="No search terms provided"), 400

    root = _lib_root()
    if not os.path.isdir(root):
        return jsonify(results=[], capped=False)

    results = []
    capped  = False
    CAP     = 200

    def candidates():
        # The Library Index already holds every audio file's path in memory —
        # match against that instead of walking the whole library per query.
        # Before the first index scan finishes, fall back to walking.
        files = lib_index.all_files()
        if files is not None:
            yield from files
            return
        for dirpath, _dirs, fnames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
            for fname in sorted(fnames):
                yield ("" if rel_dir == "." else rel_dir), fname

    for rel_dir, fname in candidates():
        dir_lower   = rel_dir.lower()
        fname_lower = fname.lower()
        if track  and track  not in fname_lower:                                          continue
        if artist and not any(artist in p for p in dir_lower.split("/") if p):            continue
        if album  and not any(album  in p for p in dir_lower.split("/") if p):            continue
        if year   and year not in fname_lower and year not in dir_lower:                  continue
        fpath = os.path.join(root, *rel_dir.split("/"), fname) if rel_dir else os.path.join(root, fname)
        try:
            st = os.stat(fpath)      # only matches touch the disk (≤ CAP of them)
        except OSError:
            continue                 # gone since the last index scan
        results.append({
            "name":  fname,
            "type":  "file",
            "size":  st.st_size,
            "mtime": st.st_mtime,
            "path":  f"{rel_dir}/{fname}" if rel_dir else fname,
            "dir":   rel_dir,
        })
        if len(results) >= CAP:
            capped = True
            break

    return jsonify(results=results, capped=capped)


_TRACK_NUM_RE = re.compile(r'^\d+\s+')


@bp.post("/api/library/check-items")
def api_library_check_items():
    body  = request.get_json(silent=True) or {}
    items = body.get("items", [])
    if not items:
        return jsonify({})

    result: dict[str, str] = {}
    tracks  = [i for i in items if i.get("type") == "track"]
    albums  = [i for i in items if i.get("type") in ("album", "playlist")]

    if tracks:
        hits = lib_index.check([t.get("title", "") for t in tracks])
        for item, hit in zip(tracks, hits):
            result[item.get("url", "")] = "full" if hit else "none"

    for item in albums:
        status = lib_index.check_album(item.get("title", ""), item.get("track_count"))
        result[item.get("url", "")] = status

    return jsonify(result)


@bp.post("/api/library/rescan")
def api_library_rescan():
    lib_index.trigger_rescan()
    return jsonify(ok=True)


# ── Library table data ───────────────────────────────────────────────────────
# One row per audio file for the library table. Rows come straight from the tag cache in memory, so
# a request never touches the disk. Files it hasn't read yet are parsed in a background thread and
# show up on a later poll (`pending` says how many are still missing); files changed since they were
# read are caught by a background check that stats everything (`checking` while it runs).
_tracks_fill = {"running": False, "done": 0, "total": 0}
_tracks_fill_lock = threading.Lock()
_tracks_tried: set[str] = set()
_VERIFY_EVERY = 10          # s between background checks the table triggers on its own
_tracks_verify = {"running": False, "again": False, "at": 0.0}


class _TagView:
    """Lets _org_target() read a tag-cache record as if it were a mutagen easy-tags object."""
    def __init__(self, rec: dict):
        self.rec = rec

    def get(self, key, default=None):
        v = self.rec.get(key)
        return [v] if v else default


def _tracks_fill_start(abs_paths: list[str]) -> None:
    import tagcache
    todo = [p for p in abs_paths if p not in _tracks_tried]
    if not todo:
        return
    with _tracks_fill_lock:
        if _tracks_fill["running"]:
            return
        _tracks_fill.update(running=True, done=0, total=len(todo))
        _tracks_tried.update(todo)

    def work():
        try:
            tagcache.get_many(todo, progress=lambda d, t: _tracks_fill.update(done=d, total=t))
        except Exception:
            log.exception("Library tag read failed")
        finally:
            _tracks_fill["running"] = False

    threading.Thread(target=work, daemon=True, name="tracks-fill").start()


def _tracks_verify_start(abs_paths: list[str], force: bool = False) -> None:
    """Re-check every file against its cached record in the background (re-reading changed ones),
    at most every _VERIFY_EVERY seconds unless forced (the browser asks after it changed files)."""
    import tagcache
    with _tracks_fill_lock:
        v = _tracks_verify
        if v["running"]:
            v["again"] = v["again"] or force   # the running pass may have seen a file before it changed
            return
        if not force and time.monotonic() - v["at"] < _VERIFY_EVERY:
            return
        v.update(running=True, again=False)

    def work():
        try:
            while True:
                tagcache.get_many(abs_paths, workers=16)
                with _tracks_fill_lock:
                    if not _tracks_verify["again"]:
                        break
                    _tracks_verify["again"] = False
        except Exception:
            log.exception("Library tag check failed")
        finally:
            with _tracks_fill_lock:
                _tracks_verify.update(running=False, at=time.monotonic())

    threading.Thread(target=work, daemon=True, name="tracks-verify").start()


def _track_row(rel: str, rec: dict, name_fmt: str) -> dict:
    d, _, fname = rel.rpartition("/")
    ext = os.path.splitext(fname)[1].lower()
    codec = str(rec.get("codec", "")).lower()
    no = re.match(r"\s*(\d+)", str(rec.get("tracknumber", "")))
    year = re.match(r"\d{4}", str(rec.get("date", "")))
    bpm = re.match(r"\d+", str(rec.get("bpm", "")))
    expected = ""
    if rec.get("title"):
        try:
            expected = _org_target(_TagView(rec), name_fmt, ext).rsplit("/", 1)[-1]
        except Exception:
            expected = ""
    return {
        "path": rel, "dir": d, "file": fname,
        "title": rec.get("title") or os.path.splitext(fname)[0],
        "artist": rec.get("artist") or rec.get("albumartist") or "",
        "album": rec.get("album", ""),
        "year": int(year.group()) if year else None,
        "no": int(no.group(1)) if no else None,
        "fmt": ext.lstrip("."), "codec": codec,
        "kbps": round((rec.get("bitrate") or 0) / 1000),
        "lossless": ext in (".flac", ".wav") or (ext == ".m4a" and "alac" in codec),
        "len": round(rec.get("dur") or 0), "size": rec["size"], "mtime": rec["mtime"],
        "genre": rec.get("genre", ""), "bpm": int(bpm.group()) if bpm else None,
        "mbid": rec.get("musicbrainz_trackid", ""), "isrc": rec.get("isrc", ""),
        "cover": bool(rec.get("cover")), "expected": expected,
    }


# Built rows are kept per file (abs path → (stamp, name format, row, search text)) so a page
# request only rebuilds rows for files that changed; _org_target() per song is the expensive part.
_row_cache: dict[str, tuple] = {}

_MISS_KEYS = ("genre", "mbid", "bpm", "cover")
_TRACK_CHIPS = {
    "lossless": lambda r: r["lossless"],
    "missing":  lambda r: any(not r[k] for k in _MISS_KEYS),
    "mbid":     lambda r: not r["mbid"],
    "cover":    lambda r: not r["cover"],
}
# Same orderings as the table's columns in app.js (COLS[].sort).
_TRACK_SORTS = {
    "no":     lambda r: r["no"] or 0,
    "title":  lambda r: r["title"].lower(),
    "artist": lambda r: r["artist"].lower(),
    "album":  lambda r: r["album"].lower(),
    "year":   lambda r: r["year"] or 0,
    "genre":  lambda r: r["genre"].lower() if r["genre"] else "~",
    "format": lambda r: r["fmt"] + str(r["kbps"]).zfill(4),
    "bpm":    lambda r: r["bpm"] or -1,
    "tags":   lambda r: sum(1 for k in _MISS_KEYS if r[k]),
    "size":   lambda r: r["size"],
    "len":    lambda r: r["len"],
    "isrc":   lambda r: r["isrc"],
    "file":   lambda r: r["file"].lower(),
    "dir":    lambda r: r["dir"].lower(),
}


def _library_rows(fresh: bool = False):
    """(rows, search texts, abs paths still unread) for every library file whose tags are cached,
    or None before the first scan. Unread files are queued for a background tag read, and a
    background check for changed files is started (always when `fresh`)."""
    import tagcache
    files = lib_index.all_files()
    if files is None:
        return None
    root = _lib_root()
    abs_by_rel = {}
    for d, f in files:
        rel = f"{d}/{f}" if d else f
        abs_by_rel[rel] = os.path.join(root, *rel.split("/"))
    all_abs = list(abs_by_rel.values())
    recs = tagcache.cached_many(all_abs)
    missing = [a for a in all_abs if a not in recs]
    _tracks_fill_start(missing)
    _tracks_verify_start(all_abs, force=fresh)
    fmt = _settings.load().get("filename_fmt") or "{artist}/{album}/{track} {title}"
    name_fmt = fmt.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or "{title}"
    rows, hays = [], []
    for rel, a in abs_by_rel.items():
        got = recs.get(a)
        if got is None:
            continue
        stamp, rec = got
        hit = _row_cache.get(a)
        if not hit or hit[:2] != (stamp, name_fmt) or hit[2]["path"] != rel:
            size, mtime = tagcache.stamp_size_mtime(stamp)
            row = _track_row(rel, {**rec, "size": size, "mtime": mtime}, name_fmt)
            hay = f"{row['title']} {row['artist']} {row['album']} {row['year'] or ''}".lower()
            hit = _row_cache[a] = (stamp, name_fmt, row, hay)
        rows.append(hit[2])
        hays.append(hit[3])
    if len(_row_cache) > len(rows) + 1000:   # drop rows of files that are gone
        live = set(abs_by_rel.values())
        for a in [a for a in _row_cache if a not in live]:
            _row_cache.pop(a, None)
    return rows, hays, missing


def _child_folders(rows: list[dict], p: str) -> list[dict]:
    prefix = p + "/" if p else ""
    out: dict[str, dict] = {}
    for r in rows:
        d = r["dir"]
        if not d.startswith(prefix) or len(d) == len(prefix):
            continue
        rest = d[len(prefix):]
        name = rest.split("/", 1)[0]
        f = out.get(name)
        if f is None:
            f = out[name] = {"name": name, "tracks": 0, "size": 0, "dirs": set(), "leaf": True, "miss": 0}
        f["tracks"] += 1
        f["size"] += r["size"]
        f["dirs"].add(d)
        if rest != name:
            f["leaf"] = False
        if any(not r[k] for k in _MISS_KEYS):
            f["miss"] += 1
    folders = sorted(out.values(), key=lambda f: (f["name"].casefold(), f["name"]))
    for f in folders:
        f["dirs"] = len(f["dirs"])
    return folders


def _tracks_page(rows: list[dict], hays: list[str]) -> dict:
    """One page of the library table: the folder and filter view, sort and search all applied here,
    so the browser only ever receives the rows it shows."""
    a = request.args
    per = max(1, min(500, _safe_int(a.get("per"), 50)))
    p = a.get("path", "").strip("/")
    q = a.get("q", "").strip().lower()
    chips = {c for c in a.get("chips", "").split(",") if c}
    flat = a.get("view", "tracks") == "tracks" or bool(q) or bool(chips)
    now = time.time()

    prefix = p + "/" if p else ""
    in_scope = [i for i, r in enumerate(rows) if not p or r["dir"] == p or r["dir"].startswith(prefix)]
    scope_size = sum(rows[i]["size"] for i in in_scope)
    tests = [_TRACK_CHIPS[c] for c in chips if c in _TRACK_CHIPS]
    new_only = "new" in chips
    picked = []
    for i in in_scope:
        r = rows[i]
        if not flat and r["dir"] != p:
            continue
        if q and q not in hays[i]:
            continue
        if new_only and (now - r["mtime"]) // 86400 > 7:
            continue
        if all(t(r) for t in tests):
            picked.append(r)

    sort_k, desc = a.get("sort", ""), a.get("dir") == "desc"
    if sort_k == "added":
        picked.sort(key=lambda r: r["path"])
        picked.sort(key=lambda r: (now - r["mtime"]) // 86400, reverse=desc)
    elif sort_k in _TRACK_SORTS:
        picked.sort(key=lambda r: r["path"])
        picked.sort(key=_TRACK_SORTS[sort_k], reverse=desc)   # stable: ties stay in path order

    folders = [] if flat else _child_folders(rows, p)
    if sort_k == "title" and desc:
        folders.reverse()

    # Folders come first, then songs; both count towards the page.
    total = len(folders) + len(picked)
    pages = max(1, -(-total // per))
    page = max(1, min(pages, _safe_int(a.get("page"), 1)))
    lo, hi = (page - 1) * per, page * per
    page_folders = folders[lo:hi]
    page_rows = picked[max(0, lo - len(folders)):max(0, hi - len(folders))]

    # Every song in an album shows the album's cover from one file, so the browser fetches it once.
    want = {r["dir"] for r in page_rows if r["cover"]}
    cover_src = {}
    if want:
        for r in rows:
            if r["cover"] and r["dir"] in want and r["dir"] not in cover_src:
                cover_src[r["dir"]] = r
    out_rows = []
    for r in page_rows:
        src = cover_src.get(r["dir"])
        out_rows.append({**r, "cpath": src["path"], "cmtime": src["mtime"]} if r["cover"] and src else r)

    return {"tracks": out_rows, "folders": page_folders, "count": len(picked), "total": total,
            "page": page, "pages": pages, "per": per,
            "scope": {"tracks": len(in_scope), "size": scope_size},
            # The folder being shown no longer holds any songs (moved or deleted): the page goes back to the top.
            "lost": bool(p) and not in_scope and bool(rows)}


@bp.get("/api/library/tracks")
def api_library_tracks():
    """Every library song, or with ?page= one page of the table (see _tracks_page for the other arguments)."""
    paged = "page" in request.args
    got = _library_rows(fresh=request.args.get("fresh") == "1")
    if got is None:
        empty = {"ready": False, "tracks": [], "pending": 0, "filling": None}
        if paged:
            empty.update(folders=[], count=0, total=0, page=1, pages=1, per=50, scope={"tracks": 0, "size": 0}, lost=False)
        return jsonify(**empty)
    rows, hays, missing = got
    out = _tracks_page(rows, hays) if paged else {"tracks": rows}
    out.update(ready=True, pending=len(missing) if _tracks_fill["running"] else 0, checking=_tracks_verify["running"],
               filling=dict(_tracks_fill) if _tracks_fill["running"] else None)
    return _json_cached(json.dumps(out, separators=(",", ":")).encode())


def _json_cached(body: bytes) -> Response:
    """JSON with an ETag (an unchanged list costs a 304) and gzip when the browser accepts it
    (a large library list shrinks to roughly a tenth)."""
    import gzip
    resp = Response(body, mimetype="application/json")
    resp.set_etag(hashlib.md5(body).hexdigest())
    resp.headers["Cache-Control"] = "no-cache"
    resp = resp.make_conditional(request)
    if resp.status_code == 200 and "gzip" in request.headers.get("Accept-Encoding", "") and len(body) > 1024:
        resp.set_data(gzip.compress(body, compresslevel=5))
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Vary"] = "Accept-Encoding"
    return resp


# Covers are served as small JPEG thumbnails (the list shows 40 px tiles; embedded art is often 1400 px+
# and hundreds of KB). Each size is made once per file version and kept on disk next to the settings,
# so neither the music file nor the full image is read again.
_COVER_SIZES = (96, 192, 480)
_COVER_DIR = os.path.join(os.path.dirname(os.path.abspath(os.environ.get("SETTINGS_FILE", "/vpn/settings.json"))), "covercache")


def _cover_thumb(abs_path: str, size: int) -> tuple[bytes, str] | None:
    st = os.stat(abs_path)
    key = hashlib.sha1(f"{abs_path}|{st.st_size}|{st.st_mtime_ns}|{size}".encode()).hexdigest()
    cached = os.path.join(_COVER_DIR, key[:2], key + ".jpg")
    try:
        with open(cached, "rb") as f:
            return f.read(), "image/jpeg"
    except OSError:
        pass
    data = _existing_cover_bytes(abs_path)
    if not data:
        return None
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.draft("RGB", (size, size))          # JPEG: decode at reduced scale, much faster for big covers
        im = im.convert("RGB")
        im.thumbnail((size, size), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82, optimize=True, progressive=True)
        out = buf.getvalue()
    except Exception:
        # No Pillow, or an image it can't read: send the original rather than nothing.
        mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/webp" if data[:4] == b"RIFF" else "image/jpeg"
        return data, mime
    try:
        os.makedirs(os.path.dirname(cached), exist_ok=True)
        tmp = cached + ".tmp"
        with open(tmp, "wb") as f:
            f.write(out)
        os.replace(tmp, cached)
    except OSError as exc:
        log.debug("cover cache write failed: %s", exc)
    return out, "image/jpeg"


@bp.get("/api/library/cover")
def api_library_cover():
    try:
        abs_path = _safe_lib_path(request.args.get("path", ""))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if not os.path.isfile(abs_path):
        return jsonify(error="Not found"), 404
    want = _safe_int(request.args.get("s", 0), 0)
    size = next((z for z in _COVER_SIZES if z >= want), _COVER_SIZES[-1]) if want else 0
    if size:
        got = _cover_thumb(abs_path, size)
        if not got:
            return jsonify(error="No cover"), 404
        data, mime = got
    else:
        data = _existing_cover_bytes(abs_path)
        if not data:
            return jsonify(error="No cover"), 404
        mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/webp" if data[:4] == b"RIFF" else "image/jpeg"
    resp = Response(data, mimetype=mime)
    # The URL carries the file's mtime (v=), so a re-tagged file gets a new URL; the old one can be cached for good.
    resp.headers["Cache-Control"] = "private, max-age=31536000, immutable" if request.args.get("v") else "private, max-age=86400"
    resp.last_modified = os.path.getmtime(abs_path)
    return resp.make_conditional(request)


# Waveforms for the song details: made the first time a song is opened (ffmpeg decodes it to a low-rate
# mono stream, which is plenty for ~120 bars) and kept on disk next to the cover thumbnails.
_WAVE_BARS = 120
_WAVE_RATE = 4000
_WAVE_DIR = os.path.join(os.path.dirname(_COVER_DIR), "wavecache")
_wave_slots = threading.BoundedSemaphore(2)      # each decode reads a whole file off the share


def _waveform(abs_path: str) -> list[int]:
    """Loudness per slice of the song (RMS, 0–100, the loudest slice is 100)."""
    import array
    st = os.stat(abs_path)
    key = hashlib.sha1(f"{abs_path}|{st.st_size}|{st.st_mtime_ns}|{_WAVE_BARS}".encode()).hexdigest()
    cached = os.path.join(_WAVE_DIR, key[:2], key + ".json")
    try:
        with open(cached) as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    with _wave_slots:
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-i", abs_path, "-map", "0:a:0", "-ac", "1",
             "-ar", str(_WAVE_RATE), "-f", "s16le", "-"],
            capture_output=True, timeout=120)
    if out.returncode != 0 or len(out.stdout) < 2 * _WAVE_BARS:
        raise ValueError((out.stderr.decode(errors="replace").strip().splitlines() or ["Couldn’t decode the audio"])[-1])
    pcm = array.array("h")
    pcm.frombytes(out.stdout[:len(out.stdout) // 2 * 2])
    if sys.byteorder != "little":
        pcm.byteswap()
    step = len(pcm) / _WAVE_BARS
    rms = []
    for i in range(_WAVE_BARS):
        sl = pcm[int(i * step):int((i + 1) * step)]
        rms.append((sum(x * x for x in sl) / len(sl)) ** 0.5 if sl else 0.0)
    top = max(rms) or 1.0
    bars = [round(100 * v / top) for v in rms]
    try:
        os.makedirs(os.path.dirname(cached), exist_ok=True)
        tmp = cached + ".tmp"
        with open(tmp, "w") as f:
            json.dump(bars, f)
        os.replace(tmp, cached)
    except OSError as exc:
        log.debug("waveform cache write failed: %s", exc)
    return bars


@bp.get("/api/library/waveform")
def api_library_waveform():
    try:
        abs_path = _safe_lib_path(request.args.get("path", ""))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if not os.path.isfile(abs_path):
        return jsonify(error="Not found"), 404
    try:
        bars = _waveform(abs_path)
    except FileNotFoundError:
        return jsonify(error="ffmpeg isn’t installed"), 500
    except (ValueError, subprocess.TimeoutExpired) as exc:
        return jsonify(error=str(exc) or "Couldn’t decode the audio"), 422
    resp = jsonify(bars=bars)
    # Same as covers: the URL carries the file's mtime (v=), so a changed file gets a new URL.
    resp.headers["Cache-Control"] = "private, max-age=31536000, immutable" if request.args.get("v") else "no-cache"
    return resp


@bp.get("/api/jobs/<job_id>/download")
def api_job_download(job_id: str):
    """Send the files a finished job produced: the file itself for one song, a .zip for several."""
    import tempfile
    import zipfile
    job = next((j for j in worker.get_jobs() if j["id"] == job_id), None)
    if not job:
        return jsonify(error="Not found"), 404
    root = os.path.realpath(Config.OUTPUT_DIR)
    paths = []
    for r in job.get("track_results") or []:
        fp = r.get("file_path") if r.get("success") else None
        if fp:
            real = os.path.realpath(fp)
            if real.startswith(root + os.sep) and os.path.isfile(real):
                paths.append(real)
    if not paths:
        return jsonify(error="This download has no files on disk"), 404
    if len(paths) == 1:
        return send_file(paths[0], as_attachment=True, download_name=os.path.basename(paths[0]))
    name = re.sub(r'[\\/:*?"<>|]+', "_", job.get("title") or "download").strip() or "download"
    tmp = tempfile.NamedTemporaryFile(prefix="spotiflac-", suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED) as z:   # audio is already compressed
        for p in paths:
            z.write(p, arcname=os.path.relpath(p, root).replace(os.sep, "/"))
    resp = send_file(tmp.name, as_attachment=True, download_name=name + ".zip", mimetype="application/zip")
    resp.call_on_close(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name))
    return resp


@bp.get("/api/tasks")
def api_tasks():
    import math
    idx = lib_index.status()
    if idx["scanning"]:
        if idx["total"]:
            idx_detail = f"{idx['phase']} {idx['done']:,}/{idx['total']:,}"
        elif idx["done"]:
            idx_detail = f"{idx['phase']} · {idx['done']:,} found"
        else:
            idx_detail = "Scanning…"
        if idx["first"]:
            idx_detail = "First index · " + idx_detail
    elif idx["last_elapsed"] is not None:
        secs = idx["last_elapsed"]
        dur  = f"{secs:.1f}s" if secs < 60 else f"{math.floor(secs/60)}m {secs%60:.0f}s"
        idx_detail = f"{idx['count']:,} tracks indexed in {dur}"
    else:
        idx_detail = f"{idx['count']:,} tracks indexed"

    tasks = [
        {
            "id":      "lib-index",
            "label":   "Library Index",
            "running": idx["scanning"],
            "detail":  idx_detail,
            "progress_done":  idx["done"] if idx["scanning"] else 0,
            "progress_total": idx["total"] if idx["scanning"] else 0,
        },
    ]

    with _enrich_lock:
        es = dict(_enrich_state)
    if es["running"] or es["elapsed"] is not None:
        if es["running"]:
            pct  = f"{es['done']}/{es['total']}" if es["total"] else "…"
            done = es["done"]; total = es["total"]; t_start = es.get("started_at")
            if done > 0 and total > 0 and t_start:
                elapsed_now = time.monotonic() - t_start
                eta_s       = elapsed_now / done * (total - done)
                if eta_s < 60:
                    eta_str = f"~{eta_s:.0f}s"
                elif eta_s < 3600:
                    eta_str = f"~{math.floor(eta_s/60)}m {eta_s%60:.0f}s"
                else:
                    eta_str = f"~{math.floor(eta_s/3600)}h {math.floor(eta_s%3600/60)}m"
                detail = f"{pct} · {eta_str} remaining"
            else:
                detail = f"{pct} · estimating…"
            # Dedup is a pre-pass that fully completes before this "running"
            # state begins, so the count is already final — show it now
            # rather than only in the post-completion summary below.
            if es["dupes"]: detail += f" · {es['dupes']} dupes removed"
        else:
            secs   = es["elapsed"] or 0
            dur    = f"{secs:.1f}s" if secs < 60 else f"{math.floor(secs/60)}m {secs%60:.0f}s"
            detail = f"{es['enriched']} enriched in {dur}"
            if es["moved"]:  detail += f" · {es['moved']} moved"
            if es["dupes"]:  detail += f" · {es['dupes']} dupes removed"
            if es["errors"]: detail += f" · {es['errors']} errors"
        tasks.append({
            "id":             "lib-enrich",
            "label":          f"Metadata Enrichment — {es['label']}",
            "running":        es["running"],
            "detail":         detail,
            "cancellable":    es["running"],
            "enriched_count": es["enriched"],
            "moved_count":    es["moved"],
            "dupes_count":    es.get("dupes", 0),
            "errors_count":   es["errors"],
            "progress_done":  es["done"] if es["running"] else 0,
            "progress_total": es["total"] if es["running"] else 0,
            "done_count":     es["done"],
            "total_count":    es["total"],
            "errors_log":     es.get("error_log", []),
            "moved_log":      es.get("moved_log", []),
            "dupes_log":      es.get("dupes_log", []),
        })

    for kind, label in _SCAN_LABELS.items():
        st = _scans.get(kind)
        if not st or st.get("via") == "library":
            continue
        if st["running"]:
            detail = ("Stopping…" if st["stopping"]
                      else f"{st['phase']} {st['done']}/{st['total']}".strip() if st["total"] else "Starting…")
        elif st["error"]:
            detail = st["error"]
        else:
            detail = st["summary"] or "Done"
            if st["elapsed"] is not None and not st["cancelled"]:
                detail += f" · {st['elapsed']}s"
        tasks.append({
            "id":          f"scan-{kind}",
            "label":       label,
            "running":     st["running"],
            "detail":      detail,
            "cancellable": st["running"] and not st["stopping"],
            "progress_done":  st["done"] if st["running"] else 0,
            "progress_total": st["total"] if st["running"] else 0,
            "last_error":  st["error"] or "",
        })

    import listenbrainz as _lb
    lb  = _lb.get_state()
    cfg = _settings.load()
    if cfg.get("listenbrainz_username", "").strip():
        if lb.get("running"):
            lb_detail = "Syncing…"
        elif lb.get("last_error"):
            lb_detail = lb["last_error"]
        elif lb.get("last_check"):
            from datetime import datetime, timezone as _tz
            try:
                dt   = datetime.fromisoformat(lb["last_check"].replace("Z", "+00:00"))
                diff = int((datetime.now(_tz.utc) - dt).total_seconds())
                if diff < 60:    ago = "just now"
                elif diff < 3600:  ago = f"{diff // 60}m ago"
                elif diff < 86400: ago = f"{diff // 3600}h ago"
                else:              ago = f"{diff // 86400}d ago"
            except Exception:
                ago = lb["last_check"]
            lb_detail = f"Last sync: {ago}"
            n = lb.get("total_enqueued", 0)
            if n:
                lb_detail += f" · {n:,} tracks queued total"
        else:
            lb_detail = "Never synced"
        tasks.append({
            "id":         "lb-sync",
            "label":      "ListenBrainz",
            "running":    lb.get("running", False),
            "detail":     lb_detail,
            "syncable":   not lb.get("running", False),
            "last_error": lb.get("last_error") or "",
        })

    return jsonify(tasks=tasks, any_running=any(t["running"] for t in tasks))


@bp.get("/api/library/download")
def api_library_download():
    rel = request.args.get("path", "")
    if not rel:
        return jsonify(error="No path provided"), 400
    try:
        target = _safe_lib_path(rel)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if not os.path.isfile(target):
        return jsonify(error="Not a file"), 404
    return send_file(target, as_attachment=True, download_name=os.path.basename(target))


# ── Library organizer ─────────────────────────────────────────────────────────

_AUDIO_EXTS      = frozenset({".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".wma"})
_ENRICH_AUDIO    = frozenset({".flac", ".mp3", ".m4a"})  # formats we can write tags to
_FEAT_RE    = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", re.IGNORECASE)

# Strips trailing edition/remaster/year noise from album names so that
# "Album (2020 Remaster)", "Album (Deluxe Edition)", and "Album (1993)"
# all normalise to "Album" for deduplication and folder organisation.
_ALBUM_NOISE_RE = re.compile(
    r'\s*[\(\[]\s*(?:'
    r'(?:19|20)\d{2}(?:[\s\w]*)?'                             # (1993), (2020 Remaster), (2011 …)
    r'|(?:deluxe|super|special|expanded|anniversary|'
    r'   collectors?|limited|bonus|explicit)(?:\s+[\w\s]*)?'  # (Deluxe Edition), (Bonus Tracks)
    r'|remaster(?:ed)?'                                       # (Remastered)
    r')\s*[\)\]]'
    r'|\s*[-–]\s*(?:(?:19|20)\d{2}\s+)?remaster(?:ed)?\s*$', # - Remastered / - 2020 Remastered
    re.IGNORECASE | re.VERBOSE,
)


def _norm_album(name: str) -> str:
    """Normalise album name: strip edition/year noise, then remove non-word chars."""
    return re.sub(r"[^\w]", "", _ALBUM_NOISE_RE.sub("", name).strip().lower())


def _org_main_artist(audio_easy) -> str:
    """Return the primary artist, matching the first_artist_only=True download behaviour.

    SpotiFLAC stores artists as a single comma-joined string in the ARTIST/ALBUMARTIST
    tag when first_artist_only=False (the old default), e.g. "Artist A, Artist B".
    We mirror SpotiFLAC's own first_artist property — split on "," and take element 0 —
    so existing and future files resolve to the same folder.
    """
    for key in ("albumartist", "artist"):
        vals = audio_easy.get(key)
        if vals:
            # Take the first Mutagen tag value, then split on "," to isolate the
            # primary artist from any comma-joined multi-artist string.
            raw = str(vals[0]).strip().split(",")[0].strip()
            if raw:
                cleaned = _FEAT_RE.sub("", raw).strip()
                return cleaned if cleaned else raw
    return "Unknown Artist"


def _org_san(s: str, fallback: str = "_") -> str:
    return (re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(s))
            .strip().strip(".") or fallback)[:200]


def _org_target(audio_easy, fmt: str, ext: str) -> str:
    """Compute the target relative path for an audio file given its easy tags."""
    artist     = _org_san(_org_main_artist(audio_easy))
    raw_album  = str((audio_easy.get("album") or ["Unknown Album"])[0]).strip() or "Unknown Album"
    # Strip edition/year noise from album names so that "Album (2020 Remaster)"
    # and "Album (1993)" both organise into the same "Album/" folder.
    album      = _org_san(_ALBUM_NOISE_RE.sub("", raw_album).strip() or raw_album)
    title  = _org_san(str((audio_easy.get("title")  or ["Unknown Title"])[0]).strip()  or "Unknown Title")

    raw_trk = str((audio_easy.get("tracknumber") or ["0"])[0])
    try:
        trk = int(re.split(r"[/\-]", raw_trk)[0].strip())
    except (ValueError, AttributeError):
        trk = 0
    track = f"{trk:02d}" if trk else ""

    result = (fmt
              .replace("{artist}", artist)
              .replace("{album}",  album)
              .replace("{title}",  title)
              .replace("{track}",  track))
    parts = [_org_san(p) for p in result.replace("\\", "/").split("/") if p.strip()]
    return "/".join(parts or ["Unsorted"]) + ext.lower()


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, separators=(',', ':'))}\n\n"


_SOURCE_COMMENT_RE = re.compile(r"^\s*https?://github\.com/[^\s/]+/SpotiFLAC[\w.-]*/?\s*$", re.I)


def _strip_source_comment(abs_path: str) -> bool:
    """Remove the comment SpotiFLAC stamps on every file (a bare link to its
    GitHub repo). Only a comment that is exactly that link is removed — any
    real comment the user wrote is left alone. Returns True if the file changed."""
    ext = os.path.splitext(abs_path)[1].lower()
    is_src = lambda v: bool(_SOURCE_COMMENT_RE.match(str(v)))
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC
            audio = FLAC(abs_path)
            changed = False
            for key in ("DESCRIPTION", "COMMENT"):
                vals = audio.get(key) or []
                keep = [v for v in vals if not is_src(v)]
                if len(keep) != len(vals):
                    if keep:
                        audio[key] = keep
                    else:
                        del audio[key]
                    changed = True
            if changed:
                audio.save()
            return changed
        if ext == ".mp3":
            from mutagen.id3 import ID3
            audio = ID3(abs_path)
            drop = [k for k, f in audio.items()
                    if k.startswith("COMM") and all(is_src(t) for t in f.text)]
            for k in drop:
                del audio[k]
            if drop:
                audio.save(abs_path)
            return bool(drop)
        if ext == ".m4a":
            from mutagen.mp4 import MP4
            audio = MP4(abs_path)
            vals = audio.get("\xa9cmt") or []
            keep = [v for v in vals if not is_src(v)]
            if len(keep) != len(vals):
                if keep:
                    audio["\xa9cmt"] = keep
                else:
                    del audio["\xa9cmt"]
                audio.save()
                return True
    except Exception as exc:
        log.debug("Source-comment strip failed for %s: %s", abs_path, exc)
    return False


def _write_enriched_tags(abs_path: str, tags: dict) -> bool:
    """Write enriched tag dict to a FLAC/MP3/M4A file. Returns True if saved."""
    if not tags:
        return False
    ext = os.path.splitext(abs_path)[1].lower()
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC
            audio = FLAC(abs_path)
            for k, v in tags.items():
                audio[k] = [str(v)]
            audio.save()
            return True
        elif ext == ".mp3":
            from mutagen.id3 import ID3, TCON, TPUB, TBPM, TSRC, TXXX
            try:
                audio = ID3(abs_path)
            except Exception:
                audio = ID3()
            _FRAME_MAP: dict = {"GENRE": (TCON,), "BPM": (TBPM,), "ISRC": (TSRC,)}
            for k, v in tags.items():
                if k in _FRAME_MAP:
                    audio.add(_FRAME_MAP[k][0](encoding=3, text=[str(v)]))
                elif k == "ORGANIZATION":
                    audio.add(TPUB(encoding=3, text=[str(v)]))
                else:
                    audio.add(TXXX(encoding=3, desc=k, text=[str(v)]))
            audio.save(abs_path)
            return True
        elif ext == ".m4a":
            from mutagen.mp4 import MP4, MP4FreeForm
            audio = MP4(abs_path)
            for k, v in tags.items():
                if k == "GENRE":
                    audio["\xa9gen"] = [str(v)]
                elif k == "BPM":
                    try:
                        audio["tmpo"] = [int(v)]
                    except (ValueError, TypeError):
                        pass
                else:
                    audio[f"----:com.apple.iTunes:{k}"] = [MP4FreeForm(str(v).encode())]
            audio.save()
            return True
    except Exception as exc:
        log.warning("Tag write failed for %s: %s", abs_path, exc)
    return False


def _has_mbid(abs_path: str) -> bool:
    """Return True if the file already has a MusicBrainz recording id (mbid) tag."""
    ext = os.path.splitext(abs_path)[1].lower()
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC
            return bool(FLAC(abs_path).get("musicbrainz_trackid"))
        elif ext == ".mp3":
            from mutagen.id3 import ID3
            return any(f.desc == "MUSICBRAINZ_TRACKID" for f in ID3(abs_path).getall("TXXX"))
        elif ext == ".m4a":
            from mutagen.mp4 import MP4
            return "----:com.apple.iTunes:MUSICBRAINZ_TRACKID" in MP4(abs_path)
    except Exception:
        pass
    return False


def _has_cover(abs_path: str) -> bool:
    """Return True if the file already has embedded cover art."""
    ext = os.path.splitext(abs_path)[1].lower()
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC
            return bool(FLAC(abs_path).pictures)
        elif ext == ".mp3":
            from mutagen.id3 import ID3
            return bool(ID3(abs_path).getall("APIC"))
        elif ext == ".m4a":
            from mutagen.mp4 import MP4
            return "covr" in MP4(abs_path)
    except Exception:
        pass
    return False


def _embed_cover(abs_path: str, image_data: bytes, mime: str = "image/jpeg") -> bool:
    """Embed cover art bytes into a FLAC/MP3/M4A file, *replacing* any art
    already there (never appending a second picture). Returns True on success."""
    ext = os.path.splitext(abs_path)[1].lower()
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC, Picture
            audio = FLAC(abs_path)
            pic = Picture()
            pic.type = 3  # front cover
            pic.mime = mime
            pic.data = image_data
            audio.clear_pictures()
            audio.add_picture(pic)
            audio.save()
            return True
        elif ext == ".mp3":
            from mutagen.id3 import ID3, APIC
            try:
                audio = ID3(abs_path)
            except Exception:
                audio = ID3()
            audio.delall("APIC")
            audio.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=image_data))
            audio.save(abs_path)
            return True
        elif ext == ".m4a":
            from mutagen.mp4 import MP4, MP4Cover
            audio = MP4(abs_path)
            fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            audio["covr"] = [MP4Cover(image_data, imageformat=fmt)]
            audio.save()
            return True
    except Exception as exc:
        log.warning("Cover embed failed for %s: %s", abs_path, exc)
    return False


def _existing_cover_bytes(abs_path: str) -> bytes | None:
    """Raw bytes of the first embedded cover image, or None."""
    ext = os.path.splitext(abs_path)[1].lower()
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC
            pics = FLAC(abs_path).pictures
            return pics[0].data if pics else None
        elif ext == ".mp3":
            from mutagen.id3 import ID3
            apics = ID3(abs_path).getall("APIC")
            return apics[0].data if apics else None
        elif ext == ".m4a":
            from mutagen.mp4 import MP4
            covr = MP4(abs_path).get("covr")
            return bytes(covr[0]) if covr else None
    except Exception:
        pass
    return None


def _img_dimensions(data: bytes | None) -> tuple[int, int] | None:
    """(width, height) of a PNG or JPEG byte string, dependency-free.

    Returns None for anything it can't parse (truncated data, WebP, etc.) —
    callers treat "unknown" as "can't prove it's better/worse".
    """
    if not data or len(data) < 24:
        return None
    # PNG: IHDR is always the first chunk, width/height at bytes 16..24
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))
    # JPEG: walk the segment markers to the Start-Of-Frame
    if data[:2] == b"\xff\xd8":
        i, n = 2, len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker == 0xFF:
                i += 1
                continue
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if i + 4 > n:
                break
            seg_len = int.from_bytes(data[i + 2:i + 4], "big")
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                if i + 9 <= n:
                    h = int.from_bytes(data[i + 5:i + 7], "big")
                    w = int.from_bytes(data[i + 7:i + 9], "big")
                    return (w, h)
                break
            i += 2 + seg_len
    return None


# Embedded covers at or above this width are treated as "already good" — a
# provider lookup to try to beat them is skipped on the otherwise-complete path.
_COVER_GOOD_WIDTH = 1400


def _upgrade_cover(abs_path: str, cover_url: str, has_cover: bool) -> tuple[bool, str]:
    """Fetch the provider's HD cover and embed it when the file has none, or
    when it is clearly larger than the one already embedded. Never replaces a
    cover with a smaller/equal one. Returns (changed, status_message)."""
    if not cover_url:
        return False, ("Cover art already present" if has_cover else "No cover art found")
    fetched = _fetch_cover(cover_url)
    if not fetched:
        return False, "Cover art unavailable"
    new_data, mime = fetched
    new_dim = _img_dimensions(new_data)
    if has_cover:
        old_data = _existing_cover_bytes(abs_path)
        old_dim = _img_dimensions(old_data) if old_data else None
        if old_data is not None:
            if not new_dim:
                return False, "Kept existing cover — couldn't measure the provider's"
            if old_dim and new_dim[0] <= old_dim[0] * 1.05 and new_dim[1] <= old_dim[1] * 1.05:
                return False, (f"Kept existing cover — already {old_dim[0]}×{old_dim[1]}"
                               f" (provider had {new_dim[0]}×{new_dim[1]})")
            # new one is bigger, or the old one couldn't be measured → replace
    if _embed_cover(abs_path, new_data, mime):
        verb = "Replaced cover art" if has_cover else "Cover art embedded"
        return True, (f"{verb} — {new_dim[0]}×{new_dim[1]}" if new_dim else verb)
    return False, "Cover art unavailable"


def _fetch_cover(url: str) -> tuple[bytes, str] | None:
    """Download cover image; returns (bytes, mime_type) or None on failure."""
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=10) as r:
            data = r.read()
        ctype = r.headers.get_content_type() or ""
        mime = "image/png" if "png" in ctype else "image/jpeg"
        return data, mime
    except Exception as exc:
        log.debug("Cover fetch failed for %s: %s", url, exc)
    return None


def _cleanup_empty_dirs_up(dirpath: str, root: str) -> None:
    """Remove empty ancestor dirs from dirpath up to (but not including) root."""
    real_root = os.path.realpath(os.path.abspath(root))
    cur = os.path.realpath(os.path.abspath(dirpath))
    while cur != real_root and cur.startswith(real_root + os.sep):
        try:
            if os.listdir(cur):
                break
            os.rmdir(cur)
        except OSError:
            break
        cur = os.path.dirname(cur)


def _metadata_score(abs_path: str, audio) -> int:
    """Counts "this file's metadata is complete/verified" signals.

    Used as the primary tiebreaker between duplicate copies of the same song
    (see _find_duplicate_tracks) — the better-tagged copy is worth keeping
    even over a technically higher-bitrate but poorly-tagged one, since a
    poorly-tagged file just gets re-enriched anyway (cost: one API round
    trip) while a wrongly-kept low-quality file is permanent.
    """
    score = 0
    if _has_mbid(abs_path):
        score += 1
    for field in ("genre", "bpm", "album", "isrc"):
        if str((audio.get(field) or [""])[0]).strip():
            score += 1
    if _has_cover(abs_path):
        score += 1
    return score


# ── Duplicate detection ──────────────────────────────────────────────────────
# Titles differ between releases of the same recording only by decoration —
# "(feat. X)", "- Remastered 2011", "(Album Version)", "(Deluxe Edition)".
# Those are stripped before comparing. Words that mark a genuinely different
# recording (live, remix, acoustic, instrumental, …) are NOT stripped: they
# become a "version" set that must match exactly, so "Song" and "Song (Live)"
# are never grouped even though their base titles are equal.
# Credits move between fields from release to release — "A & B", "A, B",
# "A feat. B", or artist "A" with title "Song (feat. B)" — so artists are
# compared as sets: two copies match when they share any credited artist.
_DUP_FEAT_PAREN_RE = re.compile(r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\b[^\)\]]*[\)\]]", re.I)
_DUP_FEAT_TAIL_RE  = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", re.I)
_DUP_NOISE_WORDS   = r"(?:re-?master(?:ed)?|album version|single version|deluxe(?: edition)?|bonus track|explicit|clean)"
_DUP_NOISE_PAREN_RE = re.compile(r"\s*[\(\[][^\)\]]*\b" + _DUP_NOISE_WORDS + r"\b[^\)\]]*[\)\]]", re.I)
_DUP_NOISE_DASH_RE  = re.compile(r"\s+[-\u2013\u2014]\s+(?:\d{4}\s+)?" + _DUP_NOISE_WORDS + r"\b.*$", re.I)
# Soundtrack / compilation provenance: '- From "Saturday Night Fever" Soundtrack',
# '(From the Motion Picture "X")', '(Original Soundtrack Version)'.
_DUP_FROM_PAREN_RE  = re.compile(r"\s*[\(\[]\s*(?:from\b|[^\)\]]*\bsoundtrack\b)[^\)\]]*[\)\]]", re.I)
_DUP_FROM_DASH_RE   = re.compile(r"\s+[-\u2013\u2014]\s+(?:from\b|[^-\u2013\u2014]*\bsoundtrack\b).*$", re.I)
_DUP_FEAT_CREDIT_RE = re.compile(r"[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+([^\)\]]*)[\)\]]"
                                 r"|\s(?:feat\.?|ft\.?|featuring)\s+(.*)$", re.I)
_DUP_ARTIST_SPLIT_RE = re.compile(
    r"\s*(?:,|;|/|&|\+|\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b|\bx\b|\bvs\.?)\s*", re.I)
_DUP_VERSION_RE = re.compile(
    r"\b(remix|mix|live|acoustic|instrumental|demo|karaoke|cover|acapella|a cappella|"
    r"reprise|edit|orchestral|piano|unplugged|sped up|slowed)\b", re.I)
_DUP_DUR_TOL      = 3.0    # seconds — same recording, different encode/release
_DUP_ISRC_DUR_TOL = 10.0   # an ISRC is exact, so allow more slack (padding/fades)


def _dup_fold(s: str) -> str:
    return re.sub(r"\W+", " ", unicodedata.normalize("NFC", s).casefold()).strip()


def _dup_artists(artist: str, title: str = "") -> frozenset:
    """Every artist credited in the artist tag, plus any "feat." credit in the title."""
    parts = _DUP_ARTIST_SPLIT_RE.split(artist or "")
    for m in _DUP_FEAT_CREDIT_RE.finditer(title or ""):
        parts += _DUP_ARTIST_SPLIT_RE.split(m.group(1) or m.group(2) or "")
    return frozenset(f for f in map(_dup_fold, parts) if f)


def _dup_artist_key(artist: str) -> str:
    """The first credited artist — used for sorting."""
    return _dup_fold(_DUP_ARTIST_SPLIT_RE.split(artist or "", maxsplit=1)[0])


def _dup_title_key(title: str) -> tuple[str, frozenset]:
    """(decoration-free folded title, set of version-marker words)."""
    t = _DUP_FEAT_PAREN_RE.sub("", str(title or ""))
    versions = frozenset(w.lower() for w in _DUP_VERSION_RE.findall(t))
    prev = None
    while prev != t:   # peel stacked suffixes: "X (Remastered) (feat. Y)"
        prev = t
        t = _DUP_NOISE_PAREN_RE.sub("", t)
        t = _DUP_NOISE_DASH_RE.sub("", t)
        t = _DUP_FROM_PAREN_RE.sub("", t)
        t = _DUP_FROM_DASH_RE.sub("", t)
        t = _DUP_FEAT_TAIL_RE.sub("", t)
    return _dup_fold(t), versions


def _dup_same_tags(a: dict, b: dict) -> bool:
    """Same song by its tags: equal decoration-free title and version markers,
    and at least one credited artist in common."""
    return a["tkey"] == b["tkey"] and bool(a["artists"] & b["artists"])


class _ScanCancelled(Exception):
    pass


def _check_cancel(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise _ScanCancelled()


def _dup_info(abs_path: str, rel: str, rec: dict):
    """Identity + quality info for one file from its tagcache record, or None."""
    title  = rec.get("title", "")
    artist = rec.get("artist") or rec.get("albumartist") or ""
    if not title or not artist:
        return None
    ext   = os.path.splitext(abs_path)[1].lower()
    codec = rec.get("codec", "").lower()
    isrc  = re.sub(r"[^A-Z0-9]", "", rec.get("isrc", "").upper())
    return {
        "rel": rel, "abs": abs_path, "title": title, "artist": artist,
        "album": rec.get("album", ""), "isrc": isrc if len(isrc) >= 10 else "",
        "mbid": rec.get("musicbrainz_trackid", "").strip().lower(),
        "dur": rec.get("dur", 0.0), "bitrate": rec.get("bitrate", 0),
        "ext": ext.lstrip("."), "size": rec["size"], "mtime": rec["mtime"],
        "lossless": ext in (".flac", ".wav") or (ext == ".m4a" and "alac" in codec),
        "tkey": _dup_title_key(title),
        "artists": _dup_artists(artist, title),
        # _metadata_score reads these like a mutagen easy-tags object
        "_audio": {k: [rec[k]] for k in ("genre", "bpm", "album", "isrc") if rec.get(k)},
    }


def _dup_read(abs_path: str, rel: str):
    import tagcache
    rec = tagcache.get(abs_path)
    return _dup_info(abs_path, rel, rec) if rec else None


def _read_infos(rel_paths: list[str], root: str, progress=None, cancel=None) -> list[dict]:
    """Identity info for many files, via the persistent tag cache (only new or
    changed files are actually opened, in parallel); order is kept."""
    import tagcache
    pairs = [(rel, os.path.join(root, *rel.replace("\\", "/").split("/"))) for rel in rel_paths]
    recs = tagcache.get_many([a for _, a in pairs], progress=progress, cancel=cancel)
    _check_cancel(cancel)
    out: list[dict] = []
    for rel, abs_path in pairs:
        rec = recs.get(abs_path)
        if rec:
            inf = _dup_info(abs_path, rel, rec)
            if inf:
                out.append(inf)
    return out


def _find_duplicate_groups(rel_paths: list[str], root: str, *,
                           lossless_first: bool = True, progress=None,
                           cancel=None, phase=None) -> list[dict]:
    """Group files that are the same recording, wherever they live.

    Two files are linked when either
      * they share an ISRC or MusicBrainz recording id (exact ids) and
        durations are within 10 s, or
      * titles match after stripping decoration (see above), they share a
        credited artist, the
        version markers (live/remix/…) are identical, and durations are
        within 3 s (a chain of neighbours, so 2:29.9 / 2:30.1 still match —
        unlike the old fixed 5 s buckets, which split them at a boundary).
    Groups are the connected components, so a file linked by ISRC to one copy
    and by title to another pulls all three together.

    Returns [{"files": [...], "keep": rel, "match": "isrc"|"mbid"|"title"}], each
    file dict ranked best-first; "keep" is the suggested survivor. Ranking is
    lossless first (when lossless_first), then metadata completeness, then
    bitrate — "best" for the user is normally the lossless copy.
    """
    if phase:
        phase("Reading tags")
    infos = _read_infos(rel_paths, root, progress, cancel)

    parent = list(range(len(infos)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_id: dict[tuple[str, str], list[int]] = {}
    by_text: dict[tuple, list[int]] = {}
    for i, inf in enumerate(infos):
        for kind in ("isrc", "mbid"):
            if inf[kind]:
                by_id.setdefault((kind, inf[kind]), []).append(i)
        by_text.setdefault(inf["tkey"], []).append(i)

    for members in by_id.values():
        for a in range(len(members)):
            for b in range(a + 1, len(members)):
                ia, ib = members[a], members[b]
                if abs(infos[ia]["dur"] - infos[ib]["dur"]) <= _DUP_ISRC_DUR_TOL:
                    union(ia, ib)

    for members in by_text.values():
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda i: infos[i]["dur"])
        for x, a in enumerate(members):
            for b in members[x + 1:]:
                da, db = infos[a]["dur"], infos[b]["dur"]
                if not (da and db) or db - da > _DUP_DUR_TOL:
                    break
                if infos[a]["artists"] & infos[b]["artists"]:
                    union(a, b)

    comps: dict[int, list[int]] = {}
    for i in range(len(infos)):
        comps.setdefault(find(i), []).append(i)

    groups: list[dict] = []
    for members in comps.values():
        _check_cancel(cancel)
        if len(members) < 2:
            continue
        files = [infos[i] for i in members]
        for f in files:
            f["score"] = _metadata_score(f["abs"], f["_audio"])
        files.sort(key=lambda f: ((f["lossless"] if lossless_first else 0),
                                  f["score"], f["bitrate"], f["size"]),
                   reverse=True)
        isrcs = {f["isrc"] for f in files}
        mbids = {f["mbid"] for f in files}
        match = ("isrc" if (len(isrcs) == 1 and "" not in isrcs)
                 else "mbid" if (len(mbids) == 1 and "" not in mbids) else "title")
        groups.append({"files": files, "keep": files[0]["rel"], "match": match})
    groups.sort(key=lambda g: (_dup_artist_key(g["files"][0]["artist"]),
                               g["files"][0]["title"].casefold()))
    return groups


def _find_duplicate_tracks(rel_paths: list[str], root: str) -> list[dict]:
    """Duplicates to remove, each as {"removed": rel, "kept": rel} — the
    enrich pre-pass view of _find_duplicate_groups(). Ranks metadata
    completeness first (the survivor is about to be enriched anyway), unlike
    the review dialog, which prefers lossless."""
    result: list[dict] = []
    for grp in _find_duplicate_groups(rel_paths, root, lossless_first=False):
        for f in grp["files"]:
            if f["rel"] != grp["keep"]:
                result.append({"removed": f["rel"], "kept": grp["keep"]})
    return result


# ── Background library scans ─────────────────────────────────────────────────
# The duplicate and mistag scans run in a background thread, so closing the
# dialog doesn't stop them; they show up under Background Tasks, can be
# cancelled there or from the dialog, and keep their result until the library
# changes under them.
_scans: dict[str, dict] = {}
_scans_lock = threading.Lock()
# "library" runs both finders in one pass (tags are read once) and files each result under its own kind,
# so the duplicate and mistag endpoints keep working for either way of starting a scan.
_SCAN_LABELS = {"library": "Library Scan", "dups": "Duplicate Scan", "mistag": "Mistag Scan"}


def _dup_result(groups: list[dict]) -> dict:
    return {"groups": [{
        "match": g["match"], "keep": g["keep"],
        "files": [{k: f[k] for k in ("rel", "title", "artist", "album", "ext",
                                     "size", "bitrate", "dur", "lossless", "score")}
                  for f in g["files"]],
    } for g in groups]}


def _dup_summary(groups: list[dict]) -> str:
    extra = sum(len(g["files"]) - 1 for g in groups)
    return (f"{len(groups)} song{'s' if len(groups) != 1 else ''} with duplicates · "
            f"{extra} extra cop{'ies' if extra != 1 else 'y'}") if groups else "No duplicates found"


def _mis_summary(res: dict) -> str:
    n = len(res["groups"])
    return f"{n} group{'s' if n != 1 else ''} to review" if n else "No mistagged songs found"


def _scan_store(kind: str, result: dict, summary: str, elapsed: float) -> None:
    """File a finished result under its own kind (used by the combined scan)."""
    with _scans_lock:
        _scans[kind] = {"running": False, "done": 0, "total": 0, "cancelled": False, "error": None,
                        "started_at": time.monotonic(), "elapsed": elapsed, "summary": summary,
                        "stopping": False, "phase": "", "result": result, "cancel": threading.Event(),
                        "via": "library"}


def _scan_public(st: dict, with_result: bool = True) -> dict:
    out = {k: st.get(k) for k in ("running", "done", "total", "cancelled", "error",
                                  "started_at", "elapsed", "summary", "stopping", "phase")}
    if with_result and st.get("result") is not None:
        out["result"] = st["result"]
    return out


def _scan_start(kind: str) -> bool:
    """Start a scan unless one of that kind is already running."""
    root = _lib_root()
    with _scans_lock:
        cur = _scans.get(kind)
        if cur and cur["running"]:
            return False
        # The combined scan and the single ones both write the same results, so never run them together.
        others = ("dups", "mistag") if kind == "library" else ("library",)
        if any((_scans.get(o) or {}).get("running") for o in others):
            return False
        st = {"running": True, "done": 0, "total": 0, "cancelled": False, "error": None,
              "started_at": time.monotonic(), "elapsed": None, "summary": "", "stopping": False, "phase": "",
              "result": None, "cancel": threading.Event()}
        _scans[kind] = st

    def progress(d, t):
        st["done"], st["total"] = d, t

    def phase(name):
        st["phase"], st["done"] = name, 0

    def work():
        try:
            rels = [os.path.relpath(a, root).replace(os.sep, "/") for a, _ in _org_collect(root)]
            st["total"] = len(rels)
            if kind == "library":
                seen = {"read": False}

                def lphase(name):          # the second finder re-reads the same (now cached) tags; don't announce that twice
                    if name == "Reading tags":
                        if seen["read"]:
                            return
                        seen["read"] = True
                    phase(name)

                groups = _find_duplicate_groups(rels, root, progress=progress, cancel=st["cancel"], phase=lphase)
                _scan_store("dups", _dup_result(groups), _dup_summary(groups), round(time.monotonic() - st["started_at"], 1))
                res = _find_mistagged_groups(rels, root, progress=progress, cancel=st["cancel"], phase=lphase)
                _scan_store("mistag", res, _mis_summary(res), round(time.monotonic() - st["started_at"], 1))
                nd, nm = len(groups), len(res["groups"])
                st["summary"] = (f"{nd} duplicate group{'s' if nd != 1 else ''} · {nm} mistagged group{'s' if nm != 1 else ''}"
                                 if nd or nm else "Nothing to fix")
            elif kind == "dups":
                groups = _find_duplicate_groups(rels, root, progress=progress, cancel=st["cancel"], phase=phase)
                st["result"] = _dup_result(groups)
                st["summary"] = _dup_summary(groups)
            else:
                res = _find_mistagged_groups(rels, root, progress=progress, cancel=st["cancel"], phase=phase)
                st["result"] = res
                st["summary"] = _mis_summary(res)
        except _ScanCancelled:
            st["cancelled"] = True
            st["summary"] = "Cancelled"
        except Exception as exc:
            log.exception("%s failed", _SCAN_LABELS[kind])
            st["error"] = str(exc)[:200]
        finally:
            st["elapsed"] = round(time.monotonic() - st["started_at"], 1)
            st["running"] = False
            if st["cancelled"]:
                # A stopped scan has nothing to show — drop it so it also
                # disappears from Background Tasks instead of lingering.
                with _scans_lock:
                    if _scans.get(kind) is st:
                        del _scans[kind]

    threading.Thread(target=work, daemon=True, name=f"scan-{kind}").start()
    return True


def _scans_files_changed(removed=(), renamed=None) -> None:
    """Files were deleted or renamed (duplicate review, repair, mistag actions, library edits).
    Patch every stored scan result to match instead of throwing it away, so what is left stays
    reviewable without another scan: removed files drop out, renamed ones get their new path, and a
    group that no longer has two files is gone."""
    gone = set(removed)
    renamed = renamed or {}
    if not gone and not renamed:
        return
    with _scans_lock:
        for kind, st in _scans.items():
            res = st.get("result")
            if st["running"] or not res or "groups" not in res:
                continue
            groups = []
            for g in res["groups"]:
                files = []
                for f in g["files"]:
                    if f["rel"] in gone:
                        continue
                    if f["rel"] in renamed:
                        f = {**f, "rel": renamed[f["rel"]], **({"name_ok": True} if kind == "mistag" else {})}
                    files.append(f)
                if len(files) < 2:
                    continue
                g = {**g, "files": files}
                if kind == "dups":      # the suggested survivor may itself have been removed or renamed
                    keep = renamed.get(g.get("keep"), g.get("keep"))
                    g["keep"] = keep if any(f["rel"] == keep for f in files) else files[0]["rel"]
                groups.append(g)
            res["groups"] = groups
            if kind == "dups":
                st["summary"] = _dup_summary(groups)
            elif kind == "mistag":
                st["summary"] = _mis_summary(res)


@bp.post("/api/library/scan/<kind>")
def api_scan_start(kind):
    if kind not in _SCAN_LABELS:
        return jsonify(error="Unknown scan"), 404
    if not os.path.isdir(_lib_root()):
        return jsonify(error="Library directory not found"), 404
    return jsonify(started=_scan_start(kind))


@bp.get("/api/library/scan/<kind>")
def api_scan_status(kind):
    st = _scans.get(kind)
    if st is None:
        return jsonify(idle=True)
    return jsonify(_scan_public(st))


@bp.delete("/api/library/scan/<kind>")
def api_scan_cancel(kind):
    st = _scans.get(kind)
    if st and st["running"]:
        st["stopping"] = True
        st["cancel"].set()
        return jsonify(ok=True)
    return jsonify(ok=False)


def _write_album_tag(abs_path: str, album: str) -> None:
    ext = os.path.splitext(abs_path)[1].lower()
    if ext == ".flac":
        from mutagen.flac import FLAC
        audio = FLAC(abs_path)
        audio["album"] = [album]
        audio.save()
    elif ext == ".mp3":
        from mutagen.id3 import ID3, TALB
        try:
            audio = ID3(abs_path)
        except Exception:
            audio = ID3()
        audio.setall("TALB", [TALB(encoding=3, text=[album])])
        audio.save(abs_path)
    elif ext == ".m4a":
        from mutagen.mp4 import MP4
        audio = MP4(abs_path)
        audio["\xa9alb"] = [album]
        audio.save()
    else:
        raise ValueError("can't write tags to this format")


@bp.post("/api/library/duplicates/apply")
def api_dup_apply():
    """Delete the files the user chose to remove, and set the surviving copy's
    album tag when the group carries an `album`. Each group names the copy
    that stays; a removal is refused unless that copy still exists and is a
    different file, so a request can never leave a song with zero copies."""
    body = request.get_json(silent=True) or {}
    root = _lib_root()
    removed = freed = albums_fixed = 0
    removed_rels: list[str] = []
    errors: list[str] = []
    for grp in body.get("groups") or []:
        try:
            keep_abs = _safe_lib_path(str(grp.get("keep", "")))
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if not os.path.isfile(keep_abs):
            errors.append(f"Kept copy missing, nothing removed: {grp.get('keep')}")
            continue
        for rel in grp.get("remove") or []:
            try:
                target = _safe_lib_path(str(rel))
                if os.path.splitext(target)[1].lower() not in _AUDIO_EXTS:
                    raise ValueError("not an audio file")
                if not os.path.isfile(target):
                    raise ValueError("not found")
                if os.path.samefile(target, keep_abs):
                    raise ValueError("same file as the kept copy")
                size = os.path.getsize(target)
                os.remove(target)
                _cleanup_empty_dirs_up(os.path.dirname(target), root)
                removed += 1
                freed += size
                removed_rels.append(str(rel))
            except (ValueError, OSError) as exc:
                errors.append(f"{rel}: {exc}")
        album = str(grp.get("album") or "").strip()
        if album:
            try:
                _write_album_tag(keep_abs, album)
                albums_fixed += 1
            except Exception as exc:
                errors.append(f"{grp.get('keep')}: album tag not written ({exc})")
    if removed_rels:
        _scans_files_changed(removed=removed_rels)
    if removed:
        lib_index.trigger_rescan()
    log.info("Duplicate review: removed %d file(s), freed %d bytes, %d album tag(s) set, %d error(s)",
             removed, freed, albums_fixed, len(errors))
    return jsonify(removed=removed, freed=freed, albums=albums_fixed, errors=errors[:20])


# ── Mistag finder + repair ───────────────────────────────────────────────────
# The duplicate finder above groups files whose *tags* agree. This one looks at
# the audio: two files that sound identical (Chromaprint fingerprint) or are the
# same length to a fraction of a second, yet carry different artist/title tags,
# mean at least one of them holds the wrong song. Which one can't be decided
# from the files alone, so the user ticks the ones to repair, and repair
# deletes each and downloads it again from its own tags.
_MISTAG_LEN_TOL = 0.3   # seconds — "same length" when there is no fingerprint


def _find_mistagged_groups(rel_paths: list[str], root: str, progress=None, cancel=None,
                           phase=None) -> dict:
    import audiofp
    if phase:
        phase("Reading tags")
    infos = [i for i in _read_infos(rel_paths, root, progress, cancel) if i["dur"] > 0]

    fp_reason = audiofp.unavailable_reason()
    fp_ok = not fp_reason
    fp_failed = 0
    if fp_ok:
        if phase:
            phase("Fingerprinting")
        log.info("Mistag scan: fingerprinting %d file(s)", len(infos))
        fps = audiofp.fingerprints([i["abs"] for i in infos], progress=progress, cancel=cancel)
        fp_failed = len(infos) - len(fps)
        if infos and not fps:
            # fpcalc is installed but decoded nothing — treat as unavailable and say why.
            fp_ok = False
            fp_reason = f"fpcalc failed on every file ({audiofp._last_error or 'unknown error'})"
        if fp_failed:
            log.warning("Mistag scan: fpcalc could not fingerprint %d of %d file(s); last error: %s",
                        fp_failed, len(infos), audiofp._last_error)
    else:
        log.warning("Mistag scan: audio fingerprinting unavailable — %s", fp_reason)
        fps = {}
    if phase:
        phase("Comparing")

    infos.sort(key=lambda i: i["dur"])
    parent = list(range(len(infos)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    links: list[tuple[int, int, float, str]] = []
    n = len(infos)
    tol = _DUP_DUR_TOL if fp_ok else _MISTAG_LEN_TOL
    for a in range(n):
        _check_cancel(cancel)
        ia = infos[a]
        for b in range(a + 1, n):
            ib = infos[b]
            gap = ib["dur"] - ia["dur"]
            if gap > tol:
                break
            if _dup_same_tags(ia, ib):
                continue          # same tags → an ordinary duplicate, handled elsewhere
            fa, fb = fps.get(ia["abs"]), fps.get(ib["abs"])
            if fa is not None and fb is not None:
                sim = audiofp.similarity(fa, fb)
                if sim >= audiofp.SIM_THRESHOLD:
                    links.append((a, b, sim, "audio"))
            elif (ia["artists"] & ib["artists"]
                  and (gap <= 0.1 or (gap <= _MISTAG_LEN_TOL
                                      and ia["album"].casefold() == ib["album"].casefold()))):
                # No fingerprint to check with: same artist and (near-)identical
                # length. Weak evidence, so shown as "unverified".
                links.append((a, b, 0.0, "length"))

    for a, b, _, _ in links:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    comps: dict[int, set[int]] = {}
    sims: dict[int, float] = {}
    audio_root: set[int] = set()
    for a, b, sim, kind in links:
        r = find(a)
        comps.setdefault(r, set()).update((a, b))
        sims[r] = max(sims.get(r, 0.0), sim)
        if kind == "audio":
            audio_root.add(r)

    groups = []
    for r, members in comps.items():
        files = []
        for i in sorted(members, key=lambda i: infos[i]["rel"]):
            f = infos[i]
            stem = re.sub(r"\W+", " ", os.path.splitext(os.path.basename(f["rel"]))[0].casefold())
            files.append({
                **{k: f[k] for k in ("rel", "title", "artist", "album", "ext", "size",
                                     "bitrate", "dur", "lossless")},
                "name_ok": _dup_title_key(f["title"])[0] in stem,
            })
        groups.append({"kind": "audio" if r in audio_root else "length", "sim": round(sims.get(r, 0.0), 3),
                       "files": files})
    groups.sort(key=lambda g: (g["kind"] != "audio", g["files"][0]["artist"].casefold(),
                               g["files"][0]["title"].casefold()))
    return {"groups": groups, "fingerprint": fp_ok, "fp_reason": fp_reason, "fp_failed": fp_failed}


def _read_source_url(abs_path: str) -> str:
    """The Spotify URL SpotiFLAC embedded when it downloaded this file, if any."""
    ext = os.path.splitext(abs_path)[1].lower()
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC
            for v in FLAC(abs_path).get("url") or []:
                if "open.spotify.com/track/" in v:
                    return v.strip()
        elif ext == ".mp3":
            from mutagen.id3 import ID3
            for f in ID3(abs_path).getall("WXXX"):
                if "open.spotify.com/track/" in f.url:
                    return f.url.strip()
    except Exception:
        pass
    return ""


def _resolve_track_url(title: str, artist: str, isrc: str) -> str:
    """Find the Spotify track for a file with no embedded link. ISRC first (exact);
    otherwise the first search hit whose title and first artist both match. Never
    guesses — returns "" when nothing matches cleanly."""
    client = _spotify
    if client is None:
        from SpotiFLAC.core.spotify_metadata import SpotifyMetadataClient
        client = _patch_spotify_client(SpotifyMetadataClient(timeout_s=15))
    queries = []
    if isrc:
        queries.append((f"isrc:{isrc}", True))
    queries.append((f"track:{title} artist:{artist}", False))
    want_t = _dup_title_key(title)
    want_a = _dup_artists(artist, title)
    for q, exact in queries:
        try:
            data = client._get("/search", params={"q": q, "type": "track", "limit": 5})
        except Exception as exc:
            log.debug("Track lookup failed for %r: %s", q, exc)
            continue
        for t in (data.get("tracks") or {}).get("items") or []:
            if not t:
                continue
            url = ((t.get("external_urls") or {}).get("spotify")) or ""
            if not url:
                continue
            if exact:
                return url
            names = ", ".join(a.get("name") or "" for a in t.get("artists") or [])
            if _dup_title_key(t.get("name", "")) == want_t and _dup_artists(names, t.get("name", "")) & want_a:
                return url
    return ""


@bp.post("/api/library/repair")
def api_library_repair():
    """Delete each named file and queue a fresh download of the song its tags
    describe. A file is only deleted once its download link has been found, so
    a song that can't be resolved is left untouched and reported."""
    body = request.get_json(silent=True) or {}
    root = _lib_root()
    cfg  = _settings.load()
    plan: list[tuple[str, str, str]] = []
    errors: list[str] = []
    for rel in body.get("files") or []:
        try:
            abs_path = _safe_lib_path(str(rel))
            if os.path.splitext(abs_path)[1].lower() not in _AUDIO_EXTS or not os.path.isfile(abs_path):
                raise ValueError("not an audio file")
            inf = _dup_read(abs_path, str(rel))
            if not inf:
                raise ValueError("no title/artist tags to look the song up by")
            url = _read_source_url(abs_path) or _resolve_track_url(inf["title"], inf["artist"], inf["isrc"])
            if not url:
                raise ValueError("couldn't find this song on Spotify")
            plan.append((str(rel), abs_path, url))
        except (ValueError, OSError) as exc:
            errors.append(f"{rel}: {exc}")

    repaired = []
    urls: list[str] = []
    for rel, abs_path, url in plan:
        try:
            os.remove(abs_path)
            _cleanup_empty_dirs_up(os.path.dirname(abs_path), root)
        except OSError as exc:
            errors.append(f"{rel}: {exc}")
            continue
        repaired.append({"rel": rel, "url": url})
        if url not in urls:
            urls.append(url)

    ids: list = []
    if repaired:
        _scans_files_changed(removed=[r["rel"] for r in repaired])
    if urls:
        lib_index.trigger_rescan()
        os.makedirs(Config.OUTPUT_DIR, exist_ok=True)
        ids = [worker.enqueue(
            url=u, output_dir=Config.OUTPUT_DIR, services=cfg["services"],
            filename_fmt=cfg["filename_fmt"], qobuz_token=str(cfg["qobuz_token"]),
            quality="lossless", generate_m3u=False,
        ) for u in urls]
    log.info("Library repair: %d file(s) queued for redownload, %d error(s)", len(repaired), len(errors))
    return jsonify(repaired=repaired, queued=len(ids), ids=ids, errors=errors[:20])


def _mistag_audio_paths(files) -> tuple[list[tuple[str, str]], list[str]]:
    """[(rel, abs)] for each named library audio file, plus per-file errors."""
    ok, errors = [], []
    for rel in files or []:
        try:
            abs_path = _safe_lib_path(str(rel))
            if os.path.splitext(abs_path)[1].lower() not in _AUDIO_EXTS or not os.path.isfile(abs_path):
                raise ValueError("not an audio file")
            ok.append((str(rel), abs_path))
        except (ValueError, OSError) as exc:
            errors.append(f"{rel}: {exc}")
    return ok, errors


@bp.post("/api/library/mistag/delete")
def api_mistag_delete():
    """Delete files outright — for the unwanted version of a song (radio edit,
    duplicate encode, …) where nothing needs to be downloaded again."""
    body = request.get_json(silent=True) or {}
    root = _lib_root()
    targets, errors = _mistag_audio_paths(body.get("files"))
    removed = []
    for rel, abs_path in targets:
        try:
            os.remove(abs_path)
            _cleanup_empty_dirs_up(os.path.dirname(abs_path), root)
            removed.append(rel)
        except OSError as exc:
            errors.append(f"{rel}: {exc}")
    if removed:
        _scans_files_changed(removed=removed)
        lib_index.trigger_rescan()
    log.info("Mistag delete: %d file(s) removed, %d error(s)", len(removed), len(errors))
    return jsonify(removed=removed, errors=errors[:20])


@bp.post("/api/library/mistag/rename")
def api_mistag_rename():
    """Rename each file (in its own folder) to what the filename format makes of
    its tags, so the filename matches the title tag again."""
    from mutagen import File as MFile
    import audiofp
    body = request.get_json(silent=True) or {}
    root = _lib_root()
    fmt  = _settings.load().get("filename_fmt") or "{artist}/{album}/{track} {title}"
    name_fmt = fmt.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or "{title}"
    targets, errors = _mistag_audio_paths(body.get("files"))
    renamed: dict[str, str] = {}
    for rel, abs_path in targets:
        try:
            audio = MFile(abs_path, easy=True)
            if audio is None or not (audio.get("title") or [""])[0].strip():
                raise ValueError("no title tag to name the file after")
            ext  = os.path.splitext(abs_path)[1]
            name = _org_target(audio, name_fmt, ext).rsplit("/", 1)[-1]
            dest = os.path.join(os.path.dirname(abs_path), name)
            if dest == abs_path:
                renamed[rel] = rel
                continue
            if os.path.exists(dest):
                raise ValueError(f'"{name}" already exists in that folder')
            os.rename(abs_path, dest)
            audiofp.rename(abs_path, dest)
            renamed[rel] = _lib_rel(dest)
        except Exception as exc:
            errors.append(f"{rel}: {exc}")
    if renamed:
        _scans_files_changed(renamed=renamed)
        lib_index.trigger_rescan()
    log.info("Mistag rename: %d file(s) renamed, %d error(s)", len(renamed), len(errors))
    return jsonify(renamed=renamed, errors=errors[:20])


# Per-path guard so the same file can't be single-song-enriched twice at once
# (double-click, or re-click while the first run is still streaming). Different
# files may enrich concurrently — no shared mutable state between them.
_single_enrich_active: set = set()
_single_enrich_active_lock = threading.Lock()


def _enrich_setup(use_mb: bool):
    """Resolve (enrich_fn, mb_lookup, mb_to_tags) against the installed SpotiFLAC.

    enrich_fn is None when metadata enrichment isn't available at all. Shared by
    the batch enricher and the single-song SSE endpoint so both agree on exactly
    which SpotiFLAC APIs they're calling.
    """
    try:
        from SpotiFLAC.core.metadata_enrichment import enrich_metadata as enrich_fn
    except ImportError:
        try:
            # SpotiFLAC 1.3+ exposes only the async enricher — wrap it on the
            # worker's persistent event loop so a threaded/streamed caller is
            # otherwise unchanged.
            from SpotiFLAC.core.metadata_enrichment import enrich_metadata_async as _ea
            from worker import _run_coro_sync
            def enrich_fn(*a, **k):
                return _run_coro_sync(_ea(*a, **k))
        except ImportError:
            return None, None, None

    mb_lookup = mb_to_tags = None
    if use_mb:
        try:
            import inspect as _inspect
            from SpotiFLAC.core.musicbrainz import (
                fetch_mb_metadata, mb_result_to_tags as mb_to_tags,
            )
            if "title" in _inspect.signature(fetch_mb_metadata).parameters:
                # SpotiFLAC >= 4.0: fetch_mb_metadata itself does the
                # ISRC-unlinked -> title/artist text-search fallback when
                # given keyword-only title=/artist=.
                mb_lookup = lambda isrc, title, artist: fetch_mb_metadata(
                    isrc, title=title, artist=artist
                )
            else:
                # Pre-4.0: the title/artist fallback lived in
                # patch_spotiflac.py's fetch_mb_metadata_smart (retired at the
                # 3.8.0 -> 4.1.0 bump). Use it if present, else ISRC-only.
                try:
                    from SpotiFLAC.core.musicbrainz import fetch_mb_metadata_smart
                    mb_lookup = lambda isrc, title, artist: fetch_mb_metadata_smart(
                        isrc, title, artist
                    )
                except ImportError:
                    mb_lookup = lambda isrc, title, artist: fetch_mb_metadata(isrc)
        except ImportError:
            pass
    return enrich_fn, mb_lookup, mb_to_tags


def _enrich_one_file(abs_path, rel, root, providers, use_mb, fmt,
                     enrich_fn, mb_lookup, mb_to_tags, upgrade_cover=True):
    """Enrich one audio file, reporting every step.

    Generator: yields {"type":"step", "id":str, "text":str, "pending":bool}
    progress events, then exactly one terminal
    {"type":"result", "enriched":bool, "moved":str|None, "error":str|None,
     "elapsed":float}.

    Shared by the batch enricher (drains the events, acts on the result) and the
    /api/library/enrich-one SSE endpoint (streams every event to the browser).
    Mirrors the per-file logic the batch path used inline before.
    """
    from mutagen import File as MFile
    _t0 = time.monotonic()
    enriched = False
    moved = None
    try:
        yield {"type": "step", "id": "read", "text": "Reading current tags…", "pending": True}
        audio = MFile(abs_path, easy=True)
        if audio is None:
            raise ValueError("Unrecognised audio format")

        title  = str((audio.get("title") or [""])[0]).strip()
        artist = str((audio.get("artist") or audio.get("albumartist") or [""])[0]).strip()
        isrc   = str((audio.get("isrc") or [""])[0]).strip()
        has_genre = bool(str((audio.get("genre") or [""])[0]).strip())
        has_bpm   = bool(str((audio.get("bpm") or [""])[0]).strip())
        has_mbid  = _has_mbid(abs_path)
        has_cover = _has_cover(abs_path)
        if _strip_source_comment(abs_path):
            enriched = True

        _who = (f"“{title}”" if title else "(untitled)") + (f" by {artist}" if artist else "")
        yield {"type": "step", "id": "read",
               "text": f"Read tags — {_who}" + (f" · ISRC {isrc}" if isrc else ""),
               "pending": False}

        _fields = (("genre", has_genre), ("BPM", has_bpm),
                   ("MusicBrainz ID", has_mbid), ("cover art", has_cover))
        _present = [n for n, v in _fields if v]
        _missing = [n for n, v in _fields if not v]
        yield {"type": "step", "id": "inspect",
               "text": ("Present: " + ", ".join(_present) if _present else "Nothing present yet")
                       + (" · missing: " + ", ".join(_missing) if _missing else " · all present"),
               "pending": False}

        if not (has_genre and has_bpm and has_mbid):
            yield {"type": "step", "id": "providers",
                   "text": "Querying providers: " + ", ".join(providers) + "…", "pending": True}
            result = enrich_fn(title, artist, isrc=isrc, providers=providers, timeout_s=12)
            tags = result.as_tags()
            _found = []
            if getattr(result, "genre", ""): _found.append(f"genre={result.genre}")
            if getattr(result, "label", ""): _found.append(f"label={result.label}")
            if getattr(result, "bpm", 0):    _found.append(f"BPM={result.bpm}")
            yield {"type": "step", "id": "providers",
                   "text": "Providers: " + (", ".join(_found) if _found else "no new data"),
                   "pending": False}

            if not has_mbid and (isrc or (title and artist)) and mb_lookup and mb_to_tags:
                yield {"type": "step", "id": "mb", "text": "MusicBrainz lookup…", "pending": True}
                _mbid = None
                try:
                    mb_tags = mb_to_tags(mb_lookup(isrc, title, artist))
                    for k, v in mb_tags.items():
                        tags.setdefault(k, v)
                    _mbid = mb_tags.get("MUSICBRAINZ_TRACKID") or mb_tags.get("musicbrainz_trackid")
                except Exception as exc:
                    log.debug("MusicBrainz lookup failed for %s: %s", rel, exc)
                yield {"type": "step", "id": "mb",
                       "text": (f"MusicBrainz: matched {str(_mbid)[:8]}…" if _mbid
                                else "MusicBrainz: no match"),
                       "pending": False}

            did_save = _write_enriched_tags(abs_path, tags)
            yield {"type": "step", "id": "write",
                   "text": (f"Wrote {len(tags)} tag(s): " + ", ".join(tags)
                            if (did_save and tags) else "No new tags to write"),
                   "pending": False}

            cover_url = getattr(result, "cover_url_hd", "")
            if cover_url and (not has_cover or upgrade_cover):
                yield {"type": "step", "id": "cover", "text": "Checking cover art…", "pending": True}
                _changed, _msg = _upgrade_cover(abs_path, cover_url, has_cover)
                did_save = did_save or _changed
                yield {"type": "step", "id": "cover", "text": _msg, "pending": False}
            elif not has_cover:
                yield {"type": "step", "id": "cover",
                       "text": "No cover art found", "pending": False}

            enriched = bool(did_save) or enriched
            audio2 = MFile(abs_path, easy=True)
        else:
            # genre + BPM + MusicBrainz id are all present. Still worth a
            # provider round-trip if there's no cover at all, or the embedded
            # one is smaller than a typical HD cover and could be upgraded.
            _need_cover = not has_cover
            if has_cover and upgrade_cover:
                _od = _img_dimensions(_existing_cover_bytes(abs_path))
                if _od is None or _od[0] < _COVER_GOOD_WIDTH:
                    _need_cover = True
            if _need_cover:
                yield {"type": "step", "id": "cover", "text": "Checking cover art…", "pending": True}
                result = enrich_fn(title, artist, isrc=isrc, providers=providers, timeout_s=12)
                cover_url = getattr(result, "cover_url_hd", "")
                _changed, _msg = _upgrade_cover(abs_path, cover_url, has_cover)
                enriched = enriched or _changed
                yield {"type": "step", "id": "cover", "text": _msg, "pending": False}
            else:
                yield {"type": "step", "id": "skip",
                       "text": "Genre, BPM, MusicBrainz ID and cover art all already present",
                       "pending": False}
            audio2 = audio

        if audio2:
            ext = os.path.splitext(abs_path)[1]
            new_rel = _org_target(audio2, fmt, ext)
            cur_rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
            if new_rel != cur_rel:
                dst_abs = os.path.join(root, *new_rel.replace("\\", "/").split("/"))
                # new_rel and cur_rel can be different *strings* yet point at
                # the very file we just enriched — a case-insensitive volume,
                # Unicode NFC/NFD, or a trailing dot/space that _org_san
                # strips. Deleting abs_path here would destroy the only copy.
                if os.path.exists(dst_abs) and os.path.samefile(dst_abs, abs_path):
                    yield {"type": "step", "id": "organize",
                           "text": "Filename already matches the format", "pending": False}
                elif os.path.exists(dst_abs):
                    # A *different* file already holds the target name. That is
                    # not proof of a duplicate — it can be a real name
                    # collision between two distinct recordings — so never
                    # delete either file. Leave this one where it is; the user
                    # can merge/dedupe it deliberately from the library.
                    yield {"type": "step", "id": "organize",
                           "text": f"Target name already taken by a different file — left this one in place ({new_rel})",
                           "pending": False}
                else:
                    os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
                    shutil.move(abs_path, dst_abs)
                    _cleanup_empty_dirs_up(os.path.dirname(abs_path), root)
                    yield {"type": "step", "id": "organize",
                           "text": f"Moved → {new_rel}", "pending": False}
                    moved = new_rel
            else:
                yield {"type": "step", "id": "organize",
                       "text": "Filename already matches the format", "pending": False}

        yield {"type": "result", "enriched": enriched, "moved": moved,
               "error": None, "elapsed": round(time.monotonic() - _t0, 1)}
    except Exception as exc:
        log.warning("Enrich failed for %s: %s", rel, exc)
        yield {"type": "result", "enriched": enriched, "moved": moved,
               "error": str(exc)[:200], "elapsed": round(time.monotonic() - _t0, 1)}


def _run_enrich_bg(rel_paths: list, root: str, providers: list,
                   use_mb: bool, fmt: str, enrich_all: bool = False) -> None:
    """Background thread: enriches files and updates _enrich_state."""
    global _enrich_state

    if enrich_all:
        rel_paths = []
        for dp, _, fnames in os.walk(root):
            for fname in sorted(fnames):
                if os.path.splitext(fname)[1].lower() in _ENRICH_AUDIO:
                    rel_paths.append(
                        os.path.relpath(os.path.join(dp, fname), root).replace(os.sep, "/")
                    )
        if not rel_paths:
            with _enrich_lock:
                _enrich_state.update(running=False, elapsed=0.0)
            return

    # ── Deduplication pre-pass ────────────────────────────────────────────────
    dupes = 0
    dupes_log: list[dict] = []
    dup_entries = _find_duplicate_tracks(rel_paths, root)
    if dup_entries:
        dup_set = {e["removed"] for e in dup_entries}
        for entry in dup_entries:
            rel = entry["removed"]
            abs_path = os.path.join(root, *rel.replace("\\", "/").split("/"))
            try:
                os.remove(abs_path)
                _cleanup_empty_dirs_up(os.path.dirname(abs_path), root)
                dupes += 1
                if len(dupes_log) < 50:
                    dupes_log.append({"removed": rel, "kept": entry["kept"]})
                log.info("Dedup: removed duplicate %s (kept %s)", rel, entry["kept"])
            except OSError as exc:
                log.warning("Dedup: could not remove %s: %s", rel, exc)
        rel_paths = [r for r in rel_paths if r not in dup_set]

    with _enrich_lock:
        _enrich_state["total"] = len(rel_paths)
        _enrich_state["dupes"] = dupes
        _enrich_state["dupes_log"] = list(dupes_log)

    _enrich, _mb_lookup, _mb_to_tags = _enrich_setup(use_mb)
    if _enrich is None:
        with _enrich_lock:
            _enrich_state["running"] = False
            _enrich_state["elapsed"] = 0.0
        log.warning("Metadata enrichment not available — upgrade SpotiFLAC")
        return

    total    = len(rel_paths)
    enriched = moved = errors = 0
    error_log: list = []
    moved_log: list = []
    t0       = time.monotonic()

    with _enrich_lock:
        _enrich_state.update(total=total, done=0, enriched=0, moved=0,
                             dupes=dupes, dupes_log=list(dupes_log),
                             errors=0, error_log=[], moved_log=[], started_at=t0)

    for i, rel in enumerate(rel_paths):
        if _enrich_cancel.is_set():
            break

        try:
            abs_path = _safe_lib_path(rel)
        except ValueError:
            errors += 1
            if len(error_log) < 50:
                error_log.append({"path": rel, "error": "Path outside library"})
            with _enrich_lock:
                _enrich_state.update(done=i + 1, errors=errors, error_log=list(error_log))
            continue

        if not os.path.isfile(abs_path):
            errors += 1
            if len(error_log) < 50:
                error_log.append({"path": rel, "error": "File not found"})
            with _enrich_lock:
                _enrich_state.update(done=i + 1, errors=errors, error_log=list(error_log))
            continue

        _res = {"error": "no result"}
        for _ev in _enrich_one_file(abs_path, rel, root, providers, use_mb, fmt,
                                    _enrich, _mb_lookup, _mb_to_tags):
            if _ev.get("type") == "result":
                _res = _ev
        if _res.get("error"):
            errors += 1
            if len(error_log) < 50:
                error_log.append({"path": rel, "error": str(_res["error"])[:120]})
        else:
            if _res.get("enriched"):
                enriched += 1
            if _res.get("moved"):
                moved += 1
                if len(moved_log) < 50:
                    moved_log.append({"from": rel, "to": _res["moved"]})

        with _enrich_lock:
            _enrich_state.update(done=i + 1, enriched=enriched, moved=moved, errors=errors,
                                 error_log=list(error_log), moved_log=list(moved_log))

    elapsed = time.monotonic() - t0
    with _enrich_lock:
        _enrich_state.update(running=False, elapsed=elapsed,
                             enriched=enriched, moved=moved,
                             dupes=dupes, dupes_log=list(dupes_log),
                             errors=errors,
                             error_log=list(error_log), moved_log=list(moved_log))
    log.info("Enrich done — %d enriched, %d moved, %d dupes removed, %d errors in %.1fs",
             enriched, moved, dupes, errors, elapsed)


@bp.post("/api/library/enrich")
def api_library_enrich():
    global _enrich_state

    with _enrich_lock:
        if _enrich_state["running"]:
            return jsonify(error="Enrichment already running"), 409

    body       = request.get_json(silent=True) or {}
    enrich_all = body.get("all", False)
    rel_paths  = body.get("paths", [])

    cfg       = _settings.load()
    providers = cfg.get("enrich_providers", ["deezer", "apple"])
    use_mb    = cfg.get("enrich_musicbrainz", True)
    fmt       = cfg.get("filename_fmt", "{artist}/{album}/{track} {title}")
    root      = _lib_root()

    if not enrich_all and not rel_paths:
        return jsonify(error="No audio files found"), 400

    label = "all files" if enrich_all else f"{len(rel_paths)} file{'' if len(rel_paths) == 1 else 's'}"
    _enrich_cancel.clear()
    with _enrich_lock:
        _enrich_state.update(running=True, label=label,
                             total=len(rel_paths),  # 0 when enrich_all (updated by thread)
                             done=0, enriched=0, moved=0, errors=0, elapsed=None,
                             started_at=None, error_log=[], moved_log=[])

    threading.Thread(
        target=_run_enrich_bg,
        args=(rel_paths, root, providers, use_mb, fmt),
        kwargs={"enrich_all": enrich_all},
        daemon=True, name="lib-enrich",
    ).start()

    return jsonify(ok=True, total=len(rel_paths))


@bp.delete("/api/library/enrich")
def api_library_enrich_cancel():
    _enrich_cancel.set()
    return jsonify(ok=True)


@bp.get("/api/library/enrich-one")
def api_library_enrich_one():
    """Enrich a single library file, streaming granular progress as SSE.

    Independent of the batch enricher (doesn't touch _enrich_state or its
    'running' guard) so it works even while a full library enrich is going,
    and it deliberately skips the batch path's duplicate-deletion pre-pass —
    a per-song action shouldn't delete anything.
    """
    rel = (request.args.get("path", "") or "").lstrip("/")
    try:
        abs_path = _safe_lib_path(rel)
    except ValueError:
        return jsonify(error="Path is outside the library"), 400
    if not os.path.isfile(abs_path):
        return jsonify(error="File not found"), 404
    if os.path.splitext(abs_path)[1].lower() not in _ENRICH_AUDIO:
        return jsonify(error="Only FLAC, MP3 and M4A files can be tagged"), 400

    cfg       = _settings.load()
    providers = cfg.get("enrich_providers", ["deezer", "apple"])
    use_mb    = cfg.get("enrich_musicbrainz", True)
    fmt       = cfg.get("filename_fmt", "{artist}/{album}/{track} {title}")
    root      = _lib_root()

    def generate():
        with _single_enrich_active_lock:
            if rel in _single_enrich_active:
                yield _sse({"type": "error", "msg": "This file is already being enriched"})
                return
            # Cap so spam-clicking can't starve the (small) gunicorn thread pool.
            if len(_single_enrich_active) >= 3:
                yield _sse({"type": "error",
                            "msg": "Too many single-song enrichments at once — wait for one to finish"})
                return
            _single_enrich_active.add(rel)
        try:
            enrich_fn, mb_lookup, mb_to_tags = _enrich_setup(use_mb)
            if enrich_fn is None:
                yield _sse({"type": "error",
                            "msg": "Metadata enrichment isn't available in this SpotiFLAC build"})
                return
            for ev in _enrich_one_file(abs_path, rel, root, providers, use_mb, fmt,
                                       enrich_fn, mb_lookup, mb_to_tags):
                yield _sse(ev)
        except Exception as exc:
            log.warning("enrich-one stream failed for %s: %s", rel, exc)
            yield _sse({"type": "error", "msg": str(exc)[:200]})
        finally:
            with _single_enrich_active_lock:
                _single_enrich_active.discard(rel)

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _org_collect(root: str) -> list[tuple[str, str]]:
    """Snapshot all audio file paths before any moves happen."""
    files: list[tuple[str, str]] = []
    for dirpath, dirs, fnames in os.walk(root):
        dirs.sort()
        for fname in sorted(fnames):
            ext = os.path.splitext(fname)[1].lower()
            if ext in _AUDIO_EXTS:
                files.append((os.path.join(dirpath, fname), ext))
    return files


@bp.post("/api/library/organize/preview")
def api_org_preview():
    body = request.get_json(silent=True) or {}
    fmt  = str(body.get("format", "{artist}/{album}/{track} {title}")).strip() or "{artist}/{album}/{track} {title}"
    root = _lib_root()
    if not os.path.isdir(root):
        return jsonify(error="Library directory not found"), 404

    def generate():
        from mutagen import File as MFile
        all_files = _org_collect(root)
        total     = len(all_files)
        yield _sse({"type": "total", "total": total})
        ops: list[dict] = []
        for i, (src_abs, ext) in enumerate(all_files):
            src_rel = os.path.relpath(src_abs, root).replace(os.sep, "/")
            try:
                audio = MFile(src_abs, easy=True)
                if audio is None:
                    ops.append({"src": src_rel, "error": "Unrecognised format"})
                else:
                    dst_rel = _org_target(audio, fmt, ext)
                    ops.append({"src": src_rel, "dst": dst_rel, "changed": src_rel != dst_rel})
            except Exception as exc:
                ops.append({"src": src_rel, "error": str(exc)[:120]})
            yield _sse({"type": "progress", "done": i + 1, "total": total})
        yield _sse({"type": "done", "ops": ops})

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@bp.post("/api/library/organize/apply")
def api_org_apply():
    body = request.get_json(silent=True) or {}
    fmt  = str(body.get("format", "{artist}/{album}/{track} {title}")).strip() or "{artist}/{album}/{track} {title}"
    root = _lib_root()
    if not os.path.isdir(root):
        return jsonify(error="Library directory not found"), 404

    def generate():
        from mutagen import File as MFile
        all_files = _org_collect(root)
        total     = len(all_files)
        yield _sse({"type": "total", "total": total})
        moved = errors = 0
        for i, (src_abs, ext) in enumerate(all_files):
            src_rel = os.path.relpath(src_abs, root).replace(os.sep, "/")
            try:
                if not os.path.isfile(src_abs):
                    pass  # already relocated — not an error
                else:
                    audio = MFile(src_abs, easy=True)
                    if audio is None:
                        raise ValueError("Unrecognised format")
                    dst_rel = _org_target(audio, fmt, ext)
                    if src_rel != dst_rel:
                        dst_abs = os.path.join(root, *dst_rel.split("/"))
                        if os.path.exists(dst_abs) and os.path.normcase(src_abs) != os.path.normcase(dst_abs):
                            errors += 1
                        else:
                            os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
                            shutil.move(src_abs, dst_abs)
                            moved += 1
            except Exception:
                errors += 1
            yield _sse({"type": "progress", "done": i + 1, "total": total,
                        "moved": moved, "errors": errors})
        # Remove empty directories left behind
        for dp, _, _ in os.walk(root, topdown=False):
            if dp == root:
                continue
            try:
                if not os.listdir(dp):
                    os.rmdir(dp)
            except OSError:
                pass
        yield _sse({"type": "done", "moved": moved, "errors": errors})

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── ListenBrainz recommendations ─────────────────────────────────────────────

@bp.get("/api/listenbrainz")
def api_lb_state():
    import listenbrainz as _lb
    return jsonify(_lb.get_state())


@bp.post("/api/listenbrainz/sync")
def api_lb_sync():
    import listenbrainz as _lb
    cfg      = _settings.load()
    username = cfg.get("listenbrainz_username", "").strip()
    if not username:
        return jsonify(error="No ListenBrainz username configured"), 400
    state = _lb.get_state()
    if state.get("running"):
        return jsonify(error="Sync already in progress"), 409
    _lb.sync_now_bg(username)
    return jsonify(ok=True)


# ── Provider stats ────────────────────────────────────────────────────────────

@bp.get("/api/providers")
def api_providers():
    try:
        from SpotiFLAC.core.provider_stats import ProviderScorer
        scorer = ProviderScorer()

        async def _read_stats():
            async with scorer._stats_lock:
                return list(scorer._stats.items())

        # _stats_lock is an asyncio.Lock — needs `async with`, not a plain
        # `with`, which raises "'Lock' object does not support the context
        # manager protocol". Run it on the shared persistent loop like every
        # other SpotiFLAC async call (see worker._run_coro_sync).
        stats_items = worker._run_coro_sync(_read_stats())
    except Exception as exc:
        log.warning("Provider stats unavailable: %s", exc)
        return jsonify(providers=[])

    by_type: dict = {}
    for key, stat in stats_items:
        ptype, _, _ = key.partition(":")
        g = by_type.setdefault(ptype, {
            "name": ptype, "successes": 0, "failures": 0,
            "last_outcome": "", "last_attempt": 0.0, "score": 0.0, "api_count": 0,
        })
        g["successes"]  += stat.successes
        g["failures"]   += stat.failures
        g["score"]      += stat.score()
        g["api_count"]  += 1
        if stat.last_attempt > g["last_attempt"]:
            g["last_attempt"] = stat.last_attempt
            g["last_outcome"] = stat.last_outcome

    for g in by_type.values():
        total = g["successes"] + g["failures"]
        g["rate"] = round(g["successes"] / total * 100) if total else None
        if g["last_outcome"] == "success":
            g["health"] = "good"
        elif g["last_outcome"] == "failure":
            g["health"] = "bad" if (g["rate"] is None or g["rate"] < 50) else "degraded"
        else:
            g["health"] = "unknown"

    providers = sorted(by_type.values(), key=lambda p: p["last_attempt"], reverse=True)
    return jsonify(providers=providers)


@bp.delete("/api/providers")
def api_providers_reset():
    try:
        from SpotiFLAC.core.provider_stats import ProviderScorer
        ProviderScorer().reset()
        return jsonify(ok=True)
    except Exception as exc:
        log.warning("Provider stats reset failed: %s", exc)
        return jsonify(error=str(exc)), 500


# ── Settings ──────────────────────────────────────────────────────────────────

@bp.get("/api/settings")
def api_settings_get():
    return jsonify(_settings.load())


@bp.patch("/api/settings")
def api_settings_patch():
    body    = request.get_json(silent=True) or {}
    updates = {}
    errors  = {}

    for key in ("retry_interval_min", "retry_max_count", "max_workers", "reconnect_threshold", "download_timeout_s"):
        if key not in body:
            continue
        try:
            updates[key] = int(body[key])
        except (TypeError, ValueError):
            errors[key] = "must be an integer"

    if "track_delay_s" in body:
        try:
            updates["track_delay_s"] = float(body["track_delay_s"])
        except (TypeError, ValueError):
            errors["track_delay_s"] = "must be a number"

    for key in ("filename_fmt", "qobuz_token"):
        if key in body:
            updates[key] = str(body[key])

    if "enrich_metadata" in body:
        updates["enrich_metadata"] = bool(body["enrich_metadata"])

    if "enrich_musicbrainz" in body:
        updates["enrich_musicbrainz"] = bool(body["enrich_musicbrainz"])

    if "listenbrainz_enabled" in body:
        updates["listenbrainz_enabled"] = bool(body["listenbrainz_enabled"])

    if "listenbrainz_username" in body:
        updates["listenbrainz_username"] = str(body["listenbrainz_username"]).strip()

    if "listenbrainz_days" in body:
        raw = body["listenbrainz_days"]
        if not isinstance(raw, list):
            errors["listenbrainz_days"] = "must be a list"
        else:
            try:
                days = sorted({int(d) for d in raw if 0 <= int(d) <= 6})
                updates["listenbrainz_days"] = days or list(range(7))
            except (TypeError, ValueError):
                errors["listenbrainz_days"] = "must be integers 0-6 (Mon-Sun)"

    if "listenbrainz_time" in body:
        raw = str(body["listenbrainz_time"]).strip()
        try:
            hh, mm = (int(p) for p in raw.split(":")[:2])
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError
            updates["listenbrainz_time"] = f"{hh:02d}:{mm:02d}"
        except (TypeError, ValueError):
            errors["listenbrainz_time"] = "must be HH:MM"

    if "enrich_providers" in body:
        raw = body["enrich_providers"]
        if not isinstance(raw, list):
            errors["enrich_providers"] = "must be a list"
        else:
            valid_ep = {"deezer", "apple", "tidal", "qobuz"}
            cleaned  = [p for p in raw if p in valid_ep]
            updates["enrich_providers"] = cleaned or ["deezer", "apple"]

    if "services" in body:
        raw = body["services"]
        if not isinstance(raw, list):
            errors["services"] = "must be a list"
        else:
            # Empty is valid: Settings now only shows services whose extension
            # is installed, so having none configured yet (no extensions
            # installed) is a normal transient state, not an error.
            updates["services"] = [s for s in raw if s in _VALID_SERVICES]

    if "m3u_mode" in body:
        raw = str(body["m3u_mode"])
        if raw in ("always", "ask", "never"):
            updates["m3u_mode"] = raw
        else:
            errors["m3u_mode"] = "must be 'always', 'ask', or 'never'"

    if "quality" in body:
        raw = str(body["quality"])
        if raw in _VALID_QUALITIES:
            updates["quality"] = raw
        else:
            errors["quality"] = "must be 'high', 'lossless', or 'hires'"

    if "extension_registries" in body:
        raw = body["extension_registries"]
        if not isinstance(raw, list):
            errors["extension_registries"] = "must be a list"
        else:
            cleaned = [u.strip() for u in raw if isinstance(u, str) and u.strip().startswith(("http://", "https://"))]
            updates["extension_registries"] = cleaned

    if errors:
        return jsonify(error="Invalid values", fields=errors), 400
    _settings.save(updates)
    return jsonify(ok=True, settings=_settings.load())


_sf_version_cache: dict = {}


def _ver_tuple(v: str) -> tuple:
    parts = [int(x) for x in v.split(".")[:3]]
    return tuple(parts + [0] * (3 - len(parts)))


def _sf_installed_version() -> str:
    """Read version from /spotiflac dist-info, returning the highest found.

    pip install --upgrade --target can leave old dist-info dirs alongside the
    new one; taking the max ensures we always report the installed version.
    """
    import glob
    found: list[str] = []
    for di in glob.glob("/spotiflac/SpotiFLAC-*.dist-info") + glob.glob("/spotiflac/spotiflac-*.dist-info"):
        try:
            with open(os.path.join(di, "METADATA")) as f:
                for line in f:
                    if line.lower().startswith("version:"):
                        found.append(line.split(":", 1)[1].strip())
                        break
        except OSError:
            pass
    if found:
        return max(found, key=_ver_tuple)
    try:
        from importlib.metadata import version as _imv
        return _imv("SpotiFLAC")
    except Exception:
        return "unknown"


@bp.get("/api/spotiflac/version")
def api_spotiflac_version():
    import urllib.request as _ureq

    installed = _sf_installed_version()

    now = time.time()
    if _sf_version_cache.get("ts", 0) > now - 3600:
        latest = _sf_version_cache["latest"]
    else:
        try:
            req = _ureq.Request(
                "https://pypi.org/pypi/SpotiFLAC/json",
                headers={"User-Agent": "spotiflac-ui/1.0"},
            )
            with _ureq.urlopen(req, timeout=8) as resp:
                latest = json.loads(resp.read())["info"]["version"]
            _sf_version_cache["latest"] = latest
            _sf_version_cache["ts"] = now
        except Exception as exc:
            log.debug("PyPI version check failed: %s", exc)
            latest = installed

    try:
        update_available = (
            installed != "unknown"
            and _ver_tuple(latest) > _ver_tuple(installed)
        )
    except Exception:
        update_available = False

    return jsonify(installed=installed, latest=latest, update_available=update_available)


