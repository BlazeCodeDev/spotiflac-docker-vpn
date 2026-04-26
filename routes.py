import os

from flask import Blueprint, jsonify, render_template, request

import worker
import vpn
from config import Config

bp = Blueprint("main", __name__)

_VALID_SERVICES = {"tidal", "qobuz", "amazon", "deezer", "youtube"}


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
    qobuz_token = str(body.get("qobuz_token", Config.QOBUZ_TOKEN))

    os.makedirs(Config.OUTPUT_DIR, exist_ok=True)

    ids = [
        worker.enqueue(
            url=url,
            output_dir=Config.OUTPUT_DIR,
            services=services,
            filename_fmt=Config.FILENAME_FMT,
            artist_dirs=Config.ARTIST_DIRS,
            album_dirs=Config.ALBUM_DIRS,
            retry_min=Config.RETRY_MIN,
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
