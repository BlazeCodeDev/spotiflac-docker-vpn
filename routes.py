import hashlib
import logging
import os
import shutil

from flask import Blueprint, jsonify, render_template, request, send_file

import settings as _settings
import worker
import vpn
from config import Config

log = logging.getLogger(__name__)

bp = Blueprint("main", __name__)

_VALID_SERVICES   = {"tidal", "qobuz", "amazon", "deezer", "youtube"}
_VALID_QUALITIES  = {"high", "lossless", "hires"}


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default



@bp.get("/")
def index():
    return render_template(
        "index.html",
        services=Config.SERVICES,
        filename_fmt=Config.FILENAME_FMT,
        artist_dirs=Config.ARTIST_DIRS,
        album_dirs=Config.ALBUM_DIRS,
    )


@bp.post("/api/download")
def api_download():
    body = request.get_json(silent=True) or {}
    raw  = body.get("urls", "")
    urls = [u.strip() for u in raw.replace(",", "\n").splitlines() if u.strip()]
    if not urls:
        return jsonify(error="No URLs provided"), 400

    # Whitelist services — reject unknown values
    raw_services = body.get("services", Config.SERVICES)
    services = [s for s in raw_services if s in _VALID_SERVICES]
    if not services:
        return jsonify(error="No valid services specified"), 400

    # Filename format, folder structure and retry interval are always taken from
    # env vars — the UI may display them but cannot override them.
    raw_quality = body.get("quality", "lossless")
    quality = raw_quality if raw_quality in _VALID_QUALITIES else "lossless"
    qobuz_token = str(body.get("qobuz_token", Config.QOBUZ_TOKEN))

    # Optional offset fields for partial retries
    pre_success_count = _safe_int(body.get("pre_success_count", 0), 0)
    full_total        = _safe_int(body.get("full_total", 0), 0)
    pre_title         = str(body.get("pre_title", ""))

    os.makedirs(Config.OUTPUT_DIR, exist_ok=True)

    common = dict(
        output_dir=Config.OUTPUT_DIR,
        services=services,
        filename_fmt=Config.FILENAME_FMT,
        artist_dirs=Config.ARTIST_DIRS,
        album_dirs=Config.ALBUM_DIRS,
        qobuz_token=qobuz_token,
        quality=quality,
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


@bp.post("/api/tidal/refresh")
def api_tidal_refresh():
    try:
        from SpotiFLAC.providers.tidal import refresh_tidal_api_list
        urls = refresh_tidal_api_list(force=True)
        return jsonify(ok=True, count=len(urls))
    except Exception as exc:
        log.warning("Tidal API refresh failed: %s", exc)
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
        from SpotiFLAC.providers.spotify_metadata import SpotifyMetadataClient
        client = SpotifyMetadataClient(timeout_s=8)
        data   = client._get("/search", params={
            "q": q, "type": "track,album,playlist,artist",
            "limit": limit, "offset": offset,
        })
        results = []
        tracks_obj    = data.get("tracks", {})
        albums_obj    = data.get("albums", {})
        playlists_obj = data.get("playlists", {})
        artists_obj   = data.get("artists", {})

        for t in tracks_obj.get("items", []):
            if not t:
                continue
            artists  = ", ".join(a["name"] for a in t.get("artists", []))
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
        for a in albums_obj.get("items", []):
            if not a:
                continue
            artists = ", ".join(ar["name"] for ar in a.get("artists", []))
            imgs    = a.get("images", [])
            year    = (a.get("release_date") or "")[:4]
            results.append({
                "type":        "album",
                "title":       a["name"],
                "subtitle":    artists,
                "cover_url":   imgs[-1]["url"] if imgs else None,
                "url":         f"https://open.spotify.com/album/{a['id']}",
                "track_count": a.get("total_tracks"),
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
            tracks_obj.get("total", 0),
            albums_obj.get("total", 0),
            playlists_obj.get("total", 0),
            artists_obj.get("total", 0),
        )
        has_more = (offset + limit) < total

        return jsonify(results=results, has_more=has_more, next_offset=offset + limit)
    except Exception as exc:
        log.warning("Search failed: %s", exc)
        return jsonify(error="Search failed"), 502


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


# ── Settings ──────────────────────────────────────────────────────────────────

@bp.get("/api/settings")
def api_settings_get():
    return jsonify(_settings.load())


@bp.patch("/api/settings")
def api_settings_patch():
    body = request.get_json(silent=True) or {}
    allowed = {"retry_interval_min", "retry_max_count", "track_delay_s"}
    updates = {}
    errors  = {}
    for key in allowed:
        if key not in body:
            continue
        val = body[key]
        try:
            if key == "track_delay_s":
                updates[key] = float(val)
            else:
                updates[key] = int(val)
        except (TypeError, ValueError):
            errors[key] = f"must be a number"
    if errors:
        return jsonify(error="Invalid values", fields=errors), 400
    _settings.save(updates)
    return jsonify(ok=True, settings=_settings.load())
