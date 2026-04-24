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
        _jobs[jid] = dict(id=jid, url=url, status="queued",
                          started_at=_now(), finished_at=None, error=None)
    threading.Thread(target=_run, daemon=True,
                     args=(jid, url, output_dir, services, filename_fmt,
                           artist_dirs, album_dirs, retry_min, qobuz_token)).start()
    return jid


def _run(job_id: str, url: str, output_dir: str, services: list, filename_fmt: str,
         artist_dirs: bool, album_dirs: bool, retry_min: int, qobuz_token: str) -> None:
    with _lock:
        _jobs[job_id]["status"] = "running"
    try:
        kwargs: dict = dict(
            url=url,
            output_dir=output_dir,
            services=services,
            filename_format=filename_fmt,
            use_artist_subfolders=artist_dirs,
            use_album_subfolders=album_dirs,
        )
        if retry_min:
            kwargs["loop"] = retry_min
        if qobuz_token:
            kwargs["qobuz_token"] = qobuz_token
        SpotiFLAC(**kwargs)
        with _lock:
            _jobs[job_id].update(status="done", finished_at=_now())
    except Exception as exc:
        log.error("Job %s failed: %s", job_id, exc)
        with _lock:
            _jobs[job_id].update(status="error", error=str(exc), finished_at=_now())
