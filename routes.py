import logging
import os

from flask import Blueprint, jsonify, render_template, request

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
        retry_min=Config.RETRY_MIN,
        track_delay_s=Config.TRACK_DELAY_S,
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
        retry_min=Config.RETRY_MIN,
        qobuz_token=qobuz_token,
        quality=quality,
    )

    # Partial batch retry: multiple track URLs with an offset → one job
    if pre_success_count and full_total and len(urls) > 1:
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
