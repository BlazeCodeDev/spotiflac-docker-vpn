import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone

from SpotiFLAC import SpotiFLAC

log     = logging.getLogger(__name__)
_jobs:   dict[str, dict]            = {}
_cancel: dict[str, threading.Event] = {}
_lock   = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def get_jobs() -> list[dict]:
    with _lock:
        jobs = sorted(_jobs.values(), key=lambda j: j.get("_seq", 0), reverse=True)
    return [{k: v for k, v in j.items() if not k.startswith("_")} for j in jobs]


def clear_done() -> int:
    with _lock:
        ids = [jid for jid, j in _jobs.items() if j["status"] in ("done", "error", "cancelled")]
        for jid in ids:
            _jobs.pop(jid, None)
            _cancel.pop(jid, None)
    return len(ids)


def remove_job(job_id: str) -> bool:
    ev = _cancel.get(job_id)
    if ev:
        ev.set()
    with _lock:
        return _jobs.pop(job_id, None) is not None


def cancel_job(job_id: str) -> bool:
    ev = _cancel.get(job_id)
    if ev:
        ev.set()
    with _lock:
        j = _jobs.get(job_id)
        if not j or j["status"] not in ("queued", "running"):
            return False
        j["status"]      = "cancelled"
        j["finished_at"] = _now()
    return True


def reorder_jobs(ids: list) -> None:
    with _lock:
        total = len(ids)
        for rank, jid in enumerate(ids):
            if jid in _jobs:
                _jobs[jid]["_seq"] = total - rank


def retry_job(job_id: str) -> bool:
    with _lock:
        j = _jobs.get(job_id)
        if not j or j["status"] not in ("error", "cancelled"):
            return False
        j.update(status="queued", started_at=_now(), finished_at=None, error=None)
    _cancel[job_id] = threading.Event()
    threading.Thread(target=_run, daemon=True, args=(job_id,)).start()
    return True


_seq = 0

def enqueue(url: str, output_dir: str, services: list, filename_fmt: str,
            artist_dirs: bool, album_dirs: bool, retry_min: int, qobuz_token: str) -> str:
    global _seq
    jid = str(uuid.uuid4())[:8]
    with _lock:
        _seq += 1
        _jobs[jid] = dict(
            id=jid, url=url, title=None, cover_url=None,
            status="queued", started_at=_now(), finished_at=None, error=None,
            output_dir=output_dir, services=services, filename_fmt=filename_fmt,
            artist_dirs=artist_dirs, album_dirs=album_dirs,
            retry_min=retry_min, _qobuz_token=qobuz_token,
            _seq=_seq,
        )
    _cancel[jid] = threading.Event()
    threading.Thread(target=_run, daemon=True, args=(jid,)).start()
    return jid


def _update(job_id: str, **kwargs) -> None:
    with _lock:
        j = _jobs.get(job_id)
        if j:
            j.update(kwargs)


def _fetch_metadata(url: str) -> tuple[str | None, str | None]:
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
        cover = imgs[-1].get("url") if imgs else None
        return name, cover
    except Exception:
        return None, None


def _run(job_id: str) -> None:
    ev = _cancel.setdefault(job_id, threading.Event())
    if ev.is_set():
        return

    with _lock:
        j = _jobs.get(job_id)
    if not j:
        return

    url          = j["url"]
    output_dir   = j["output_dir"]
    services     = j["services"]
    filename_fmt = j["filename_fmt"]
    artist_dirs  = j["artist_dirs"]
    album_dirs   = j["album_dirs"]
    retry_min    = j["retry_min"]
    qobuz_token  = j["_qobuz_token"] or ""

    _update(job_id, status="running", started_at=_now())

    title, cover_url = _fetch_metadata(url)
    meta: dict = {}
    if title:     meta["title"]     = title
    if cover_url: meta["cover_url"] = cover_url
    if meta:
        _update(job_id, **meta)

    while True:
        if ev.is_set():
            _update(job_id, status="cancelled", finished_at=_now())
            break

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
            _update(job_id, status="done", finished_at=_now(), error=None)
        except Exception as exc:
            log.error("Job %s failed: %s", job_id, exc)
            _update(job_id, status="error", error=str(exc), finished_at=_now())

        if not retry_min or ev.is_set():
            break

        for _ in range(retry_min * 60):
            if ev.is_set():
                break
            time.sleep(1)

        if ev.is_set():
            _update(job_id, status="cancelled", finished_at=_now())
            break

        _update(job_id, status="running", started_at=_now(), finished_at=None, error=None)

    _cancel.pop(job_id, None)
