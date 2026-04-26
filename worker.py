import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from SpotiFLAC import SpotiFLAC

log     = logging.getLogger(__name__)
_cancel: dict[str, threading.Event] = {}
_DB     = ""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


@contextmanager
def _db():
    conn = sqlite3.connect(_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path: str) -> None:
    global _DB
    _DB = path
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id           TEXT PRIMARY KEY,
                url          TEXT NOT NULL,
                title        TEXT,
                cover_url    TEXT,
                status       TEXT NOT NULL DEFAULT 'queued',
                started_at   TEXT,
                finished_at  TEXT,
                error        TEXT,
                output_dir   TEXT,
                services     TEXT,
                filename_fmt TEXT,
                artist_dirs  INTEGER DEFAULT 1,
                album_dirs   INTEGER DEFAULT 1,
                retry_min    INTEGER DEFAULT 0,
                qobuz_token  TEXT DEFAULT ''
            )
        """)
        # Jobs interrupted by a container restart are marked as errors so they
        # show up in the UI and can be retried.
        conn.execute(
            "UPDATE jobs SET status='error', error='Interrupted (container restart)', "
            "finished_at=? WHERE status IN ('running', 'queued')",
            (_now(),)
        )


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["artist_dirs"] = bool(d.get("artist_dirs"))
    d["album_dirs"]  = bool(d.get("album_dirs"))
    return d


def get_jobs() -> list[dict]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY rowid DESC").fetchall()
    return [_row_to_dict(r) for r in rows]


def clear_done() -> int:
    with _db() as conn:
        cur = conn.execute(
            "DELETE FROM jobs WHERE status IN ('done', 'error', 'cancelled')"
        )
        return cur.rowcount


def remove_job(job_id: str) -> bool:
    ev = _cancel.get(job_id)
    if ev:
        ev.set()
    with _db() as conn:
        cur = conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        return cur.rowcount > 0


def cancel_job(job_id: str) -> bool:
    ev = _cancel.get(job_id)
    if ev:
        ev.set()
    with _db() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] not in ("queued", "running"):
            return False
        conn.execute(
            "UPDATE jobs SET status='cancelled', finished_at=? WHERE id=?",
            (_now(), job_id)
        )
    return True


def retry_job(job_id: str) -> bool:
    with _db() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] not in ("error", "cancelled"):
            return False
        conn.execute(
            "UPDATE jobs SET status='queued', started_at=?, finished_at=NULL, "
            "error=NULL WHERE id=?",
            (_now(), job_id)
        )
    _cancel[job_id] = threading.Event()
    threading.Thread(target=_run, daemon=True, args=(job_id,)).start()
    return True


def enqueue(url: str, output_dir: str, services: list, filename_fmt: str,
            artist_dirs: bool, album_dirs: bool, retry_min: int, qobuz_token: str) -> str:
    jid = str(uuid.uuid4())[:8]
    with _db() as conn:
        conn.execute(
            "INSERT INTO jobs (id, url, status, started_at, output_dir, services, "
            "filename_fmt, artist_dirs, album_dirs, retry_min, qobuz_token) "
            "VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)",
            (jid, url, _now(), output_dir, json.dumps(services), filename_fmt,
             int(artist_dirs), int(album_dirs), retry_min, qobuz_token)
        )
    _cancel[jid] = threading.Event()
    threading.Thread(target=_run, daemon=True, args=(jid,)).start()
    return jid


def _update(job_id: str, **kwargs) -> None:
    if not kwargs:
        return
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [job_id]
    with _db() as conn:
        conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", vals)


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

    with _db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return

    url          = row["url"]
    output_dir   = row["output_dir"]
    services     = json.loads(row["services"] or "[]")
    filename_fmt = row["filename_fmt"]
    artist_dirs  = bool(row["artist_dirs"])
    album_dirs   = bool(row["album_dirs"])
    retry_min    = row["retry_min"]
    qobuz_token  = row["qobuz_token"] or ""

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

        # Interruptible sleep so cancel takes effect within 1 s
        for _ in range(retry_min * 60):
            if ev.is_set():
                break
            time.sleep(1)

        if ev.is_set():
            _update(job_id, status="cancelled", finished_at=_now())
            break

        _update(job_id, status="running", started_at=_now(), finished_at=None, error=None)

    _cancel.pop(job_id, None)
