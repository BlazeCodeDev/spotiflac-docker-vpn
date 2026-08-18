import hashlib
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

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



@bp.get("/")
def index():
    cfg = _settings.load()
    return render_template(
        "index.html",
        services=cfg["services"],
        filename_fmt=cfg["filename_fmt"],
        git_commit=_GIT_COMMIT,
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
    ok = worker.retry_job(job_id)
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


@bp.get("/api/library")
def api_library():
    rel = request.args.get("path", "")
    try:
        target = _safe_lib_path(rel)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if not os.path.isdir(target):
        return jsonify(error="Not a directory"), 404
    entries = []
    try:
        names = sorted(os.listdir(target),
                       key=lambda n: (not os.path.isdir(os.path.join(target, n)), n.lower()))
    except PermissionError:
        return jsonify(error="Permission denied"), 403
    for name in names:
        p = os.path.join(target, name)
        try:
            st    = os.stat(p)
            is_dir = os.path.isdir(p)
            entries.append({
                "name":  name,
                "type":  "dir" if is_dir else "file",
                "size":  0 if is_dir else st.st_size,
                "mtime": st.st_mtime,
                "path":  _lib_rel(p),
            })
        except OSError:
            pass
    return jsonify(path=rel, entries=entries)


@bp.get("/api/library/stamp")
def api_library_stamp():
    rel = request.args.get("path", "")
    try:
        target = _safe_lib_path(rel)
    except ValueError:
        return jsonify(stamp=""), 200
    try:
        parts = []
        for name in sorted(os.listdir(target)):
            p = os.path.join(target, name)
            try:
                st = os.stat(p)
                parts.append(f"{name}:{st.st_mtime:.0f}:{st.st_size}")
            except OSError:
                pass
        stamp = hashlib.md5("\n".join(parts).encode()).hexdigest()[:12]
    except Exception:
        stamp = ""
    return jsonify(stamp=stamp)


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
        if os.path.isdir(target):
            shutil.rmtree(target)
        else:
            os.remove(target)
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
        os.rename(target, dest)
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
        shutil.move(src, dest)
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

    for dirpath, _dirs, files in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        if rel_dir == ".":
            rel_dir = ""
        dir_lower  = rel_dir.lower()
        dir_parts  = [p for p in dir_lower.split("/") if p]

        for fname in sorted(files):
            fname_lower = fname.lower()
            if track  and track  not in fname_lower:                              continue
            if artist and not any(artist in p for p in dir_parts):                continue
            if album  and not any(album  in p for p in dir_parts):                continue
            if year   and year not in fname_lower and year not in dir_lower:      continue
            fpath = os.path.join(dirpath, fname)
            try:
                st = os.stat(fpath)
            except OSError:
                continue
            results.append({
                "name":  fname,
                "type":  "file",
                "size":  st.st_size,
                "mtime": st.st_mtime,
                "path":  _lib_rel(fpath),
                "dir":   rel_dir,
            })
            if len(results) >= CAP:
                capped = True
                break
        if capped:
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


@bp.get("/api/tasks")
def api_tasks():
    import math
    idx = lib_index.status()
    if idx["scanning"]:
        idx_detail = "Scanning…"
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
            "done_count":     es["done"],
            "total_count":    es["total"],
            "errors_log":     es.get("error_log", []),
            "moved_log":      es.get("moved_log", []),
            "dupes_log":      es.get("dupes_log", []),
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
    """Embed cover art bytes into a FLAC/MP3/M4A file. Returns True on success."""
    ext = os.path.splitext(abs_path)[1].lower()
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC, Picture
            audio = FLAC(abs_path)
            pic = Picture()
            pic.type = 3  # front cover
            pic.mime = mime
            pic.data = image_data
            audio.add_picture(pic)
            audio.save()
            return True
        elif ext == ".mp3":
            from mutagen.id3 import ID3, APIC
            try:
                audio = ID3(abs_path)
            except Exception:
                audio = ID3()
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


def _find_duplicate_tracks(rel_paths: list[str], root: str) -> list[dict]:
    """Return duplicate tracks to remove, each as {"removed": rel, "kept": rel}.

    Identity key: (normalised first artist, normalised title, duration bucket).
    Duration is bucketed to ±5 s to tolerate minor format differences.

    "Best" is decided by metadata correctness first (_metadata_score — has a
    verified MusicBrainz id, genre/BPM/album/ISRC tags present, cover art),
    then by actual audio bitrate as a tiebreaker between two similarly
    (in)complete copies. Bitrate rather than file extension/size: format
    alone doesn't capture a poorly-encoded FLAC vs. a clean high-bitrate M4A,
    and size conflates quality with track length.
    """
    from mutagen import File as MFile

    # First pass: score every file, grouped by identity key.
    by_key: dict[tuple, list[tuple[int, int, str]]] = {}  # key → [(meta_score, bitrate, rel), ...]

    for rel in rel_paths:
        abs_path = os.path.join(root, *rel.replace("\\", "/").split("/"))
        if not os.path.isfile(abs_path):
            continue
        try:
            audio = MFile(abs_path, easy=True)
            if audio is None:
                continue
            title  = re.sub(r"[^\w]", "",
                            str((audio.get("title") or [""])[0]).lower())
            artist = re.sub(r"[^\w]", "",
                            str((audio.get("albumartist") or
                                 audio.get("artist") or [""])[0])
                            .split(",")[0].lower())
            if not title or not artist:
                continue
            # Bucket duration to nearest 5 s so minor encoding differences
            # between formats don't prevent matching.
            dur = round(getattr(audio.info, "length", 0) / 5) * 5
            key = (artist, title, dur)

            meta_score = _metadata_score(abs_path, audio)
            bitrate    = getattr(audio.info, "bitrate", 0) or 0
            by_key.setdefault(key, []).append((meta_score, bitrate, rel))
        except Exception:
            continue

    # Second pass: within each group of more than one copy, keep the single
    # best and pair every other copy with it — so callers can show exactly
    # what was removed *and* what survived in its place, not just a count.
    result: list[dict] = []
    for entries in by_key.values():
        if len(entries) < 2:
            continue
        best = max(entries, key=lambda e: (e[0], e[1]))
        for meta_score, bitrate, rel in entries:
            if rel != best[2]:
                result.append({"removed": rel, "kept": best[2]})

    return result


def _run_enrich_bg(rel_paths: list, root: str, providers: list,
                   use_mb: bool, fmt: str, enrich_all: bool = False) -> None:
    """Background thread: enriches files and updates _enrich_state."""
    global _enrich_state
    from mutagen import File as MFile

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

    try:
        from SpotiFLAC.core.metadata_enrichment import enrich_metadata as _enrich
    except ImportError:
        try:
            # SpotiFLAC 1.3+ exposes only the async enricher — wrap it on the
            # worker's persistent event loop so this threaded, sync function is
            # otherwise unchanged.
            from SpotiFLAC.core.metadata_enrichment import enrich_metadata_async as _enrich_async
            from worker import _run_coro_sync
            def _enrich(*a, **k):
                return _run_coro_sync(_enrich_async(*a, **k))
        except ImportError:
            with _enrich_lock:
                _enrich_state["running"] = False
                _enrich_state["elapsed"] = 0.0
            log.warning("Metadata enrichment not available — upgrade SpotiFLAC")
            return

    _mb_lookup = _mb_to_tags = None
    if use_mb:
        try:
            from SpotiFLAC.core.musicbrainz import fetch_mb_metadata_smart, mb_result_to_tags as _mb_to_tags
            _mb_lookup = lambda isrc, title, artist: fetch_mb_metadata_smart(isrc, title, artist)
        except ImportError:
            try:
                # Older SpotiFLAC without the text-search fallback (see
                # patch_spotiflac.py Patch 4) — ISRC-only lookup still works.
                from SpotiFLAC.core.musicbrainz import fetch_mb_metadata, mb_result_to_tags as _mb_to_tags
                _mb_lookup = lambda isrc, title, artist: fetch_mb_metadata(isrc)
            except ImportError:
                pass

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

        try:
            audio = MFile(abs_path, easy=True)
            if audio is None:
                raise ValueError("Unrecognised format")

            title  = str((audio.get("title")       or [""])[0]).strip()
            artist = str((audio.get("artist")      or
                          audio.get("albumartist") or [""])[0]).strip()
            isrc   = str((audio.get("isrc")        or [""])[0]).strip()

            has_genre = bool(str((audio.get("genre") or [""])[0]).strip())
            has_bpm   = bool(str((audio.get("bpm")   or [""])[0]).strip())
            has_mbid  = _has_mbid(abs_path)

            if not (has_genre and has_bpm and has_mbid):
                result   = _enrich(title, artist, isrc=isrc, providers=providers, timeout_s=12)
                tags     = result.as_tags()

                if not has_mbid and (isrc or (title and artist)) and _mb_lookup and _mb_to_tags:
                    try:
                        mb_tags = _mb_to_tags(_mb_lookup(isrc, title, artist))
                        for k, v in mb_tags.items():
                            tags.setdefault(k, v)
                    except Exception as exc:
                        log.debug("MusicBrainz lookup failed for %s: %s", rel, exc)

                did_save = _write_enriched_tags(abs_path, tags)

                cover_url = result.cover_url_hd
                if cover_url and not _has_cover(abs_path):
                    cover_data = _fetch_cover(cover_url)
                    if cover_data:
                        _embed_cover(abs_path, *cover_data)
                        did_save = True

                if did_save:
                    enriched += 1
                audio2 = MFile(abs_path, easy=True)
            else:
                if not _has_cover(abs_path):
                    result = _enrich(title, artist, isrc=isrc, providers=providers, timeout_s=12)
                    cover_url = result.cover_url_hd
                    if cover_url:
                        cover_data = _fetch_cover(cover_url)
                        if cover_data and _embed_cover(abs_path, *cover_data):
                            enriched += 1
                audio2 = audio
            if audio2:
                ext     = os.path.splitext(abs_path)[1]
                new_rel = _org_target(audio2, fmt, ext)
                cur_rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
                if new_rel != cur_rel:
                    dst_abs = os.path.join(root, *new_rel.replace("\\", "/").split("/"))
                    os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
                    if os.path.exists(dst_abs):
                        # A correctly-named copy already exists — remove the stale
                        # duplicate (e.g. old "Artist A, Artist B/" after switching
                        # to first_artist_only) rather than leaving it orphaned.
                        os.remove(abs_path)
                    else:
                        shutil.move(abs_path, dst_abs)
                    moved += 1
                    if len(moved_log) < 50:
                        moved_log.append({"from": cur_rel, "to": new_rel})
                    _cleanup_empty_dirs_up(os.path.dirname(abs_path), root)

        except Exception as exc:
            log.warning("Enrich failed for %s: %s", rel, exc)
            errors += 1
            if len(error_log) < 50:
                error_log.append({"path": rel, "error": str(exc)[:120]})

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

    for key in ("retry_interval_min", "retry_max_count", "max_workers", "reconnect_threshold"):
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
            cleaned = [s for s in raw if s in _VALID_SERVICES]
            if not cleaned:
                errors["services"] = "no valid service names"
            else:
                updates["services"] = cleaned

    if "m3u_mode" in body:
        raw = str(body["m3u_mode"])
        if raw in ("always", "ask", "never"):
            updates["m3u_mode"] = raw
        else:
            errors["m3u_mode"] = "must be 'always', 'ask', or 'never'"

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


