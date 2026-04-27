import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone

from SpotiFLAC import SpotiFLAC

log     = logging.getLogger(__name__)
_jobs:   dict[str, dict]            = {}
_cancel: dict[str, threading.Event] = {}
_lock   = threading.Lock()

_STATE_FILE = os.environ.get("STATE_FILE", "/vpn/jobs.json")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _save() -> None:
    try:
        with _lock:
            snapshot = {"seq": _seq, "jobs": {jid: dict(j) for jid, j in _jobs.items()}}
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp, _STATE_FILE)
    except Exception as exc:
        log.warning("State save failed: %s", exc)


def _load() -> None:
    global _seq
    try:
        with open(_STATE_FILE) as f:
            snapshot = json.load(f)
        for jid, j in snapshot.get("jobs", {}).items():
            if j.get("status") in ("running", "queued"):
                j["status"]      = "error"
                j["error"]       = "Interrupted by restart"
                j["finished_at"] = _now()
            _jobs[jid] = j
        _seq = snapshot.get("seq", 0)
        log.info("Loaded %d job(s) from %s", len(_jobs), _STATE_FILE)
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning("State load failed: %s", exc)


_load()


def get_jobs() -> list[dict]:
    with _lock:
        jobs = sorted(_jobs.values(), key=lambda j: j.get("_seq", 0), reverse=True)
    return [{k: v for k, v in j.items() if not k.startswith("_")} for j in jobs]


def _cleanup_empty_dirs() -> None:
    from config import Config
    root = os.path.normpath(os.path.abspath(Config.OUTPUT_DIR))
    if not os.path.isdir(root):
        return
    with _lock:
        active_dirs = {
            os.path.normpath(os.path.abspath(j["output_dir"]))
            for j in _jobs.values()
            if j["status"] in ("running", "queued") and j.get("output_dir")
        }
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        abs_dir = os.path.normpath(os.path.abspath(dirpath))
        if abs_dir == root:
            continue
        if any(abs_dir == ad or abs_dir.startswith(ad + os.sep) for ad in active_dirs):
            continue
        try:
            if not os.listdir(abs_dir):
                os.rmdir(abs_dir)
        except OSError:
            pass


def clear_done() -> int:
    with _lock:
        ids = [jid for jid, j in _jobs.items() if j["status"] in ("done", "error", "cancelled")]
        for jid in ids:
            _jobs.pop(jid, None)
            _cancel.pop(jid, None)
    _save()
    _cleanup_empty_dirs()
    return len(ids)


def remove_job(job_id: str) -> bool:
    ev = _cancel.get(job_id)
    if ev:
        ev.set()
    with _lock:
        found = _jobs.pop(job_id, None) is not None
    if found:
        _save()
        _cleanup_empty_dirs()
    return found


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
    _save()
    _cleanup_empty_dirs()
    return True


def reorder_jobs(ids: list) -> None:
    with _lock:
        total = len(ids)
        for rank, jid in enumerate(ids):
            if jid in _jobs:
                _jobs[jid]["_seq"] = total - rank
    _save()


def retry_job(job_id: str) -> bool:
    with _lock:
        j = _jobs.get(job_id)
        if not j or j["status"] not in ("error", "cancelled"):
            return False
        j.update(status="queued", started_at=_now(), finished_at=None, error=None)
    _cancel[job_id] = threading.Event()
    _save()
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
    _save()
    threading.Thread(target=_run, daemon=True, args=(jid,)).start()
    return jid


def _update(job_id: str, **kwargs) -> None:
    with _lock:
        j = _jobs.get(job_id)
        if j:
            j.update(kwargs)
    _save()


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
    qobuz_token  = j.get("_qobuz_token") or ""

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
