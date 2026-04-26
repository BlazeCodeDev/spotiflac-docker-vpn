import time
import uuid
import threading
import logging
from datetime import datetime, timezone

from SpotiFLAC import SpotiFLAC

log   = logging.getLogger(__name__)
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _fetch_metadata(url: str) -> tuple[str | None, str | None]:
    """Returns (label, cover_url). One lightweight Spotify API call, fails silently."""
    try:
        from SpotiFLAC.providers.spotify_metadata import SpotifyMetadataClient, parse_spotify_url
        info   = parse_spotify_url(url)
        kind   = info["type"]
        sid    = info["id"]
        if kind not in ("track", "album", "playlist"):
            return None, None
        client = SpotifyMetadataClient(timeout_s=5)
        if kind == "track":
            meta  = client.get_track(sid)
            label = f"{meta.artists} — {meta.title}" if meta.artists else meta.title
            return label, meta.cover_url or None
        data  = client._get(f"/{kind}s/{sid}")
        name  = data.get("name") or None
        imgs  = data.get("images", [])
        cover = imgs[-1].get("url") if imgs else None  # last = smallest
        return name, cover
    except Exception:
        return None, None


def get_jobs() -> list[dict]:
    with _lock:
        return list(reversed(list(_jobs.values())))


def clear_done() -> int:
    with _lock:
        keys = [k for k, v in _jobs.items() if v["status"] in ("done", "error")]
        for k in keys:
            del _jobs[k]
    return len(keys)


def enqueue(url: str, output_dir: str, services: list, filename_fmt: str,
            artist_dirs: bool, album_dirs: bool, retry_min: int, qobuz_token: str) -> str:
    jid = str(uuid.uuid4())[:8]
    with _lock:
        _jobs[jid] = dict(id=jid, url=url, title=None, cover_url=None, status="queued",
                          started_at=_now(), finished_at=None, error=None)
    threading.Thread(target=_run, daemon=True,
                     args=(jid, url, output_dir, services, filename_fmt,
                           artist_dirs, album_dirs, retry_min, qobuz_token)).start()
    return jid


def _run(job_id: str, url: str, output_dir: str, services: list, filename_fmt: str,
         artist_dirs: bool, album_dirs: bool, retry_min: int, qobuz_token: str) -> None:
    with _lock:
        _jobs[job_id]["status"] = "running"

    title, cover_url = _fetch_metadata(url)
    with _lock:
        if title:
            _jobs[job_id]["title"] = title
        if cover_url:
            _jobs[job_id]["cover_url"] = cover_url

    while True:
        try:
            kwargs: dict = dict(
                url=url,
                output_dir=output_dir,
                services=services,
                filename_format=filename_fmt,
                use_artist_subfolders=artist_dirs,
                use_album_subfolders=album_dirs,
            )
            if qobuz_token:
                kwargs["qobuz_token"] = qobuz_token
            SpotiFLAC(**kwargs)
            with _lock:
                _jobs[job_id].update(status="done", finished_at=_now(), error=None)
        except Exception as exc:
            log.error("Job %s failed: %s", job_id, exc)
            with _lock:
                _jobs[job_id].update(status="error", error=str(exc), finished_at=_now())

        if not retry_min:
            return

        time.sleep(retry_min * 60)
        with _lock:
            _jobs[job_id].update(status="running", started_at=_now(), finished_at=None, error=None)
