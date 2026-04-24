import os

from flask import Blueprint, jsonify, render_template, request

import worker
import vpn
from config import Config

bp = Blueprint("main", __name__)

_VALID_SERVICES = {"tidal", "qobuz", "amazon", "spoti", "youtube"}


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _validate_filename_fmt(fmt: str) -> bool:
    # Reject absolute paths and directory traversal
    return ".." not in fmt and not fmt.startswith("/") and not fmt.startswith("\\")


@bp.get("/")
def index():
    return render_template(
        "index.html",
        services=Config.SERVICES,
        filename_fmt=Config.FILENAME_FMT,
        artist_dirs=Config.ARTIST_DIRS,
        album_dirs=Config.ALBUM_DIRS,
        retry_min=Config.RETRY_MIN,
    )


@bp.post("/api/download")
def api_download():
    body = request.get_json(silent=True) or {}
    raw  = body.get("urls", "")
    urls = [u.strip() for u in raw.replace(",", "\n").splitlines() if u.strip()]
    if not urls:
        return jsonify(error="Keine URLs angegeben"), 400

    # Whitelist services — reject unknown values
    raw_services = body.get("services", Config.SERVICES)
    services = [s for s in raw_services if s in _VALID_SERVICES]
    if not services:
        return jsonify(error="Keine gültigen Dienste angegeben"), 400

    # Validate filename format — no path traversal
    filename_fmt = body.get("filename_format", Config.FILENAME_FMT)
    if not _validate_filename_fmt(filename_fmt):
        return jsonify(error="Ungültiges filename_format"), 400

    artist_dirs = bool(body.get("use_artist_subfolders", Config.ARTIST_DIRS))
    album_dirs  = bool(body.get("use_album_subfolders",  Config.ALBUM_DIRS))
    retry_min   = max(0, min(1440, _safe_int(body.get("retry_minutes"), Config.RETRY_MIN)))
    qobuz_token = str(body.get("qobuz_token", Config.QOBUZ_TOKEN))

    os.makedirs(Config.OUTPUT_DIR, exist_ok=True)

    ids = [
        worker.enqueue(
            url=url,
            output_dir=Config.OUTPUT_DIR,
            services=services,
            filename_fmt=filename_fmt,
            artist_dirs=artist_dirs,
            album_dirs=album_dirs,
            retry_min=retry_min,
            qobuz_token=qobuz_token,
        )
        for url in urls
    ]
    return jsonify(queued=len(ids), ids=ids)


@bp.get("/api/jobs")
def api_jobs():
    return jsonify(worker.get_jobs())


@bp.delete("/api/jobs")
def api_clear():
    return jsonify(cleared=worker.clear_done())


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
