import heapq
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import asyncio as _asyncio

from SpotiFLAC.downloader import SpotiflacDownloader, DownloadWorker, DownloadOptions
try:
    from SpotiFLAC.downloader import download_one  # SpotiFLAC ≤ 1.2.x sync API
except ImportError:
    from SpotiFLAC.downloader import download_one_async as _dl_async  # SpotiFLAC 1.2.7+ async
    def download_one(metadata, output_dir, providers, opts, position=1, is_album=False):
        return _run_coro_sync(_dl_async(metadata, output_dir, providers, opts, position, is_album))

from SpotiFLAC.core.progress import DownloadManager

# Cached Spotify client — reuses OAuth token across jobs instead of re-fetching
# on every _fetch_metadata call. SpotifyMetadataClient handles token refresh.
try:
    from SpotiFLAC.providers.spotify_metadata import SpotifyMetadataClient as _SpotifyClient
    _spotify_client = _SpotifyClient(timeout_s=15)
except Exception:
    _spotify_client = None

# ── Persistent event loop ────────────────────────────────────────────────────
# SpotiFLAC's asyncio.Queue (progress.py) binds to the first event loop it is
# awaited from. Repeated asyncio.run() calls each produce a new loop, so the
# queue raises "bound to a different event loop" on every job after the first.
# Keeping a single loop alive for the process lifetime avoids this entirely.

_spf_loop_lock                              = threading.Lock()
_spf_loop:   _asyncio.AbstractEventLoop | None = None
_spf_thread: threading.Thread | None           = None


def _get_spf_loop() -> _asyncio.AbstractEventLoop:
    global _spf_loop, _spf_thread
    with _spf_loop_lock:
        if _spf_loop is None or _spf_loop.is_closed():
            _spf_loop   = _asyncio.new_event_loop()
            _spf_thread = threading.Thread(
                target=_spf_loop.run_forever,
                daemon=True,
                name="spotiflac-loop",
            )
            _spf_thread.start()
    return _spf_loop


def _run_coro_sync(coro) -> object:
    """Submit a coroutine to the shared persistent loop; block until it completes."""
    if not _asyncio.iscoroutine(coro):
        return coro
    loop = _get_spf_loop()
    try:
        running = _asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        # Blocking on .result() from the loop's own thread deadlocks the loop
        # (job hangs at "running" forever). Fail loudly so the job errors instead.
        coro.close()
        raise RuntimeError(
            "_run_coro_sync called from the spotiflac loop thread — "
            "would deadlock; await the coroutine directly instead"
        )
    return _asyncio.run_coroutine_threadsafe(coro, loop).result()


def _run_coro(result) -> None:
    """If result is an unawaited coroutine (async SpotiFLAC API), execute it synchronously."""
    if _asyncio.iscoroutine(result):
        _run_coro_sync(result)

log       = logging.getLogger(__name__)
_jobs:    dict[str, dict]            = {}
_cancel:  dict[str, threading.Event] = {}
_lock     = threading.Lock()
_semaphore = threading.BoundedSemaphore(3)  # replaced by init()

# Priority queue for ordered dispatch: (−_seq, counter, job_id)
_pq:     list  = []
_pq_ctr: int   = 0
_pq_cv         = threading.Condition(threading.Lock())


def _push_pq(job_id: str, seq: int) -> None:
    global _pq_ctr
    with _pq_cv:
        _pq_ctr += 1
        heapq.heappush(_pq, (-seq, _pq_ctr, job_id))
        _pq_cv.notify_all()


def _dispatcher() -> None:
    """Single thread: dispatches queued jobs in _seq order (highest = top of UI)."""
    while True:
        with _pq_cv:
            while not _pq:
                _pq_cv.wait()
            # Skip cancelled / removed jobs at the front
            while _pq:
                _, _, jid = _pq[0]
                with _lock:
                    j = _jobs.get(jid)
                ev = _cancel.get(jid)
                if not j or (ev and ev.is_set()) or j.get("status") == "cancelled":
                    heapq.heappop(_pq)
                else:
                    break
            if not _pq:
                continue
            _, _, job_id = heapq.heappop(_pq)

        # Wait for a concurrency slot, checking for cancellation each second
        acquired = False
        while not acquired:
            ev = _cancel.get(job_id)
            if ev and ev.is_set():
                break
            acquired = _semaphore.acquire(timeout=1)

        if acquired:
            threading.Thread(target=_run, daemon=True, args=(job_id,)).start()


def init(max_workers: int) -> None:
    global _semaphore
    _semaphore = threading.BoundedSemaphore(max_workers)
    log.info("MAX_WORKERS = %d", max_workers)
    threading.Thread(target=_dispatcher, daemon=True, name="job-dispatcher").start()

_STATE_FILE       = os.environ.get("STATE_FILE", "/vpn/jobs.json")
_VPN_RECONNECT    = "/tmp/vpn_reconnect"
_fail_streak      = 0
_fail_streak_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _save() -> None:
    try:
        with _lock:
            snapshot = {"seq": _seq, "jobs": {jid: dict(j) for jid, j in _jobs.items()}}
        d = os.path.dirname(os.path.abspath(_STATE_FILE))
        if d:
            os.makedirs(d, exist_ok=True)
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
        ids = [
            jid for jid, j in _jobs.items()
            if j["status"] == "done" and not j.get("fail_count")
        ]
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
        j["status"]        = "cancelled"
        j["finished_at"]   = _now()
        j["next_retry_at"] = None
    _save()
    _cleanup_empty_dirs()
    return True


def reorder_jobs(ids: list) -> None:
    global _pq_ctr
    with _lock:
        total = len(ids)
        for rank, jid in enumerate(ids):
            if jid in _jobs:
                _jobs[jid]["_seq"] = total - rank
    # Rebuild priority queue so the new order takes effect for pending jobs
    with _pq_cv:
        new_pq = []
        for _, _, jid in _pq:
            with _lock:
                j = _jobs.get(jid)
            if j:
                _pq_ctr += 1
                heapq.heappush(new_pq, (-j.get("_seq", 0), _pq_ctr, jid))
        _pq[:] = new_pq
        _pq_cv.notify_all()
    _save()


def retry_job(job_id: str) -> bool:
    with _lock:
        j = _jobs.get(job_id)
        if not j or j["status"] not in ("error", "cancelled"):
            return False
        j.update(status="queued", started_at=_now(), finished_at=None, error=None,
                 progress=None, total=None, track_results=None,
                 success_count=None, fail_count=None,
                 retry_count=0, retry_max=None, next_retry_at=None)
    _cancel[job_id] = threading.Event()
    _save()
    with _lock:
        seq = _jobs[job_id].get("_seq", 0)
    _push_pq(job_id, seq)
    return True


def retry_job_partial(job_id: str, batch_urls: list, pre_success_count: int,
                      full_total: int) -> bool:
    with _lock:
        j = _jobs.get(job_id)
        if not j or j["status"] not in ("done", "error", "cancelled"):
            return False
        j.update(
            status="queued", started_at=_now(), finished_at=None, error=None,
            progress=pre_success_count or None,
            total=full_total or None,
            track_results=None, success_count=None, fail_count=None,
            pre_success_count=pre_success_count, full_total=full_total,
            _batch_urls=batch_urls,
        )
        if batch_urls:
            j["url"] = batch_urls[0]
    _cancel[job_id] = threading.Event()
    _save()
    with _lock:
        seq = _jobs[job_id].get("_seq", 0)
    _push_pq(job_id, seq)
    return True


_seq = 0

def enqueue(url: str, output_dir: str, services: list, filename_fmt: str,
            qobuz_token: str,
            quality: str = "lossless",
            pre_success_count: int = 0, full_total: int = 0,
            batch_urls: list | None = None, pre_title: str = "") -> str:
    global _seq
    jid = str(uuid.uuid4())[:8]
    with _lock:
        _seq += 1
        _jobs[jid] = dict(
            id=jid, url=url, title=pre_title or None, cover_url=None, artist=None,
            status="queued", started_at=_now(), finished_at=None, error=None,
            progress=pre_success_count if full_total else None,
            total=full_total if full_total else None,
            track_results=None, success_count=None, fail_count=None,
            retry_count=0, retry_max=None, next_retry_at=None,
            pre_success_count=pre_success_count, full_total=full_total,
            _batch_urls=batch_urls or [],
            output_dir=output_dir, services=services, filename_fmt=filename_fmt,
            quality=quality, _qobuz_token=qobuz_token,
            _seq=_seq,
        )
    _cancel[jid] = threading.Event()
    _save()
    _push_pq(jid, _seq)
    return jid


def _update_fail_streak(success: bool) -> None:
    global _fail_streak
    import settings as _s
    threshold = _s.load().get("reconnect_threshold", 3)
    with _fail_streak_lock:
        if success:
            _fail_streak = 0
            return
        if threshold <= 0:
            return
        _fail_streak += 1
        if _fail_streak >= threshold:
            _fail_streak = 0
            try:
                open(_VPN_RECONNECT, "w").close()
                log.warning("All-provider failure streak hit %d — requesting VPN reconnect", threshold)
            except Exception:
                pass


def _update(job_id: str, **kwargs) -> None:
    with _lock:
        j = _jobs.get(job_id)
        if j:
            j.update(kwargs)
    _save()


def _fetch_metadata(url: str) -> tuple[str | None, str | None, str | None]:
    try:
        from SpotiFLAC.providers.spotify_metadata import SpotifyMetadataClient, parse_spotify_url
        info   = parse_spotify_url(url)
        kind   = info["type"]
        sid    = info["id"]
        if kind not in ("track", "album", "playlist"):
            return None, None, None
        client = _spotify_client or SpotifyMetadataClient(timeout_s=15)
        if not hasattr(client, '_get'):
            import base64, json, types, urllib.parse, urllib.request
            _CID  = base64.b64decode("ODNlNDQzMGI0NzAwNDM0YmFhMjEyMjhhOWM3ZDExYzU=").decode()
            _CSEC = base64.b64decode("OWJiOWUxMzFmZjI4NDI0Y2I2YTQyMGFmZGY0MWQ0NGE=").decode()
            def _get(self, path, **kwargs):
                now = time.time()
                if not getattr(self, '_tok', '') or now >= getattr(self, '_tok_exp', 0) - 60:
                    auth = base64.b64encode(f"{_CID}:{_CSEC}".encode()).decode()
                    req = urllib.request.Request(
                        "https://accounts.spotify.com/api/token",
                        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
                        headers={"Authorization": f"Basic {auth}",
                                 "Content-Type": "application/x-www-form-urlencoded"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        body = json.loads(resp.read())
                    self._tok = body["access_token"]
                    self._tok_exp = now + body.get("expires_in", 3600)
                url = f"https://api.spotify.com/v1/{path.lstrip('/')}"
                params = kwargs.get("params")
                if params:
                    url += "?" + urllib.parse.urlencode(params)
                req = urllib.request.Request(
                    url, headers={"Authorization": f"Bearer {self._tok}"}
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return json.loads(resp.read())
            client._get = types.MethodType(_get, client)
        if kind == "track":
            meta   = client.get_track(sid)
            artist = meta.artists or None
            label  = f"{meta.artists} — {meta.title}" if meta.artists else meta.title
            return label, meta.cover_url or None, artist
        data    = client._get(f"/{kind}s/{sid}")
        name    = data.get("name") or None
        imgs    = data.get("images", [])
        cover   = imgs[-1].get("url") if imgs else None
        artists = ", ".join(a["name"] for a in data.get("artists", []))
        return name, cover, artists or None
    except Exception:
        return None, None, None


def _explain_validation(ok: bool, msg: str, expected_s: int) -> tuple[bool, str]:
    """Translate SpotiFLAC's (Italian) validation result to (valid, english_reason)."""
    if ok:
        return True, ""
    import re
    m = re.search(r'file è (\d+)s', msg)
    actual_s = m.group(1) if m else "?"
    if "Preview" in msg:
        reason = f"30s preview detected (got {actual_s}s, expected ~{expected_s}s) — file removed"
    elif "troncato" in msg:
        reason = f"File truncated ({actual_s}s actual, expected ~{expected_s}s) — file removed"
    else:
        reason = f"Duration mismatch ({actual_s}s actual, expected ~{expected_s}s) — file removed"
    return False, reason


def _validate_track(filepath: str, expected_s: int) -> tuple[bool, str]:
    """Call SpotiFLAC download validation; returns (valid, english_reason).

    Sync-path only: must never run on the spotiflac loop thread, because the
    async fallback blocks on that loop (use _validate_track_async there).
    """
    if not filepath or expected_s <= 0:
        return True, ""
    try:
        try:
            from SpotiFLAC.core.download_validation import validate_downloaded_track
            ok, msg = validate_downloaded_track(filepath, expected_s)
        except ImportError:
            # SpotiFLAC 1.3+ exposes only the async validator.
            from SpotiFLAC.core.download_validation import validate_downloaded_track_async
            ok, msg = _run_coro_sync(validate_downloaded_track_async(filepath, expected_s))
        return _explain_validation(ok, msg, expected_s)
    except ImportError:
        return True, ""


async def _validate_track_async(filepath: str, expected_s: int) -> tuple[bool, str]:
    """Async twin of _validate_track for code already running on the spotiflac loop."""
    if not filepath or expected_s <= 0:
        return True, ""
    try:
        from SpotiFLAC.core.download_validation import validate_downloaded_track_async
        ok, msg = await validate_downloaded_track_async(filepath, expected_s)
    except ImportError:
        try:
            from SpotiFLAC.core.download_validation import validate_downloaded_track
            ok, msg = validate_downloaded_track(filepath, expected_s)
        except ImportError:
            return True, ""
    return _explain_validation(ok, msg, expected_s)


class _TrackingWorker(DownloadWorker):
    """DownloadWorker that fires callbacks after each track."""

    def __init__(self, *args, on_track_done, on_track_result=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_track_done   = on_track_done
        self._on_track_result = on_track_result

    def _resolve_output_dir(self) -> str:
        # The filename_format already encodes the full directory structure
        # (e.g. {artist}/{album}/{track} {title}), so let it be the sole
        # authority. Skipping the base-class album/playlist collection-name
        # subfolder prevents the album name from appearing twice in the path.
        import os as _os
        out = _os.path.normpath(self._opts.output_dir)
        _os.makedirs(out, exist_ok=True)
        return out

    def _track_output_dir(self, base: str, track) -> str:
        # SpotiFLAC 1.2.7 moved this to _track_output_dir_async; provide the sync
        # version here so _TrackingWorker.run() works with both API generations.
        os.makedirs(base, exist_ok=True)
        return base

    def run(self):
        from SpotiFLAC.core.models import build_filename
        from pathlib import Path as _Path

        manager  = DownloadManager()
        total    = len(self._tracks)
        start    = time.perf_counter()
        base_out = self._resolve_output_dir()
        done     = 0

        # Pre-scan: check the output folder for every track before starting any
        # downloads so files that already exist are never re-downloaded.
        # We also cache each track's output directory so the loop doesn't recompute it.
        track_dirs:   list[str]             = []
        pre_existing: list[_Path | None]    = []
        for i, track in enumerate(self._tracks):
            out_dir = self._track_output_dir(base_out, track)
            track_dirs.append(out_dir)
            found: _Path | None = None
            for ext in ('.flac', '.m4a', '.mp3'):
                fname = build_filename(
                    track,
                    fmt                  = self._opts.filename_format,
                    position             = i + 1,
                    include_track_number = self._opts.use_track_numbers,
                    use_album_track_number = self._opts.use_track_numbers,
                    first_artist_only    = self._opts.first_artist_only,
                    extension            = ext,
                )
                candidate = _Path(out_dir) / fname
                if candidate.exists() and candidate.stat().st_size > 0:
                    found = candidate
                    break
            pre_existing.append(found)

        skip_count = sum(1 for p in pre_existing if p)
        if skip_count:
            remaining = total - skip_count
            log.info(
                "Pre-scan: %d/%d tracks already on disk%s",
                skip_count, total,
                f", downloading {remaining} remaining" if remaining else " — nothing to download",
            )

        for i, track in enumerate(self._tracks):
            _run_coro(manager.start_download(track.id))
            existing = pre_existing[i]

            if existing:
                size_mb = existing.stat().st_size / (1024 * 1024)
                _run_coro(manager.complete_download(track.id, str(existing), size_mb))
                if self._on_track_result:
                    self._on_track_result({
                        "track_id": track.id,
                        "title": track.title, "artists": track.artists,
                        "success": True, "error": None,
                    })
                done += 1
                self._on_track_done(done)
                continue

            out_dir = track_dirs[i]
            result  = download_one(track, out_dir, self._providers, self._opts, i + 1,
                                   self._is_album)

            if result.success:
                expected_s = (track.duration_ms or 0) // 1000
                valid, val_reason = _validate_track(result.file_path or "", expected_s)
                if not valid:
                    log.warning("Validation failed for %s: %s", track.title, val_reason)
                    self._failed.append((track.id, track.title, track.artists, val_reason))
                    _run_coro(manager.fail_download(track.id, val_reason))
                    if self._on_track_result:
                        self._on_track_result({
                            "track_id": track.id,
                            "title": track.title, "artists": track.artists,
                            "success": False, "error": val_reason,
                        })
                else:
                    try:
                        size_mb = (
                            os.path.getsize(result.file_path) / (1024 * 1024)
                            if result.file_path and os.path.exists(result.file_path)
                            else 0.0
                        )
                    except OSError:
                        size_mb = 0.0
                    _run_coro(manager.complete_download(track.id, result.file_path or "", size_mb))
                    if self._on_track_result:
                        self._on_track_result({
                            "track_id": track.id,
                            "title": track.title, "artists": track.artists,
                            "success": True, "error": None,
                        })
            else:
                err = result.error or "unknown"
                self._failed.append((track.id, track.title, track.artists, err))
                _run_coro(manager.fail_download(track.id, err))
                if self._on_track_result:
                    self._on_track_result({
                        "track_id": track.id,
                        "title": track.title, "artists": track.artists,
                        "success": False, "error": err,
                    })

            done += 1
            self._on_track_done(done)

            if i < total - 1:
                time.sleep(self._opts.inter_track_delay_s)

        elapsed = time.perf_counter() - start
        self._print_summary(elapsed)
        return self._failed

    async def run_async(self):
        """Async version of run() for SpotiFLAC 1.2.9+ (fully async API)."""
        try:
            from SpotiFLAC.downloader import download_one_async as _dl_one_async
        except ImportError:
            _dl_one_async = None

        from SpotiFLAC.core.models import build_filename
        from pathlib import Path as _Path

        total    = len(self._tracks)
        start    = time.perf_counter()
        base_out = self._resolve_output_dir()
        done     = 0

        track_dirs:   list[str]          = []
        pre_existing: list[_Path | None] = []
        for i, track in enumerate(self._tracks):
            out_dir = self._track_output_dir(base_out, track)
            track_dirs.append(out_dir)
            found: _Path | None = None
            for ext in ('.flac', '.m4a', '.mp3'):
                fname = build_filename(
                    track,
                    fmt                    = self._opts.filename_format,
                    position               = i + 1,
                    include_track_number   = self._opts.use_track_numbers,
                    use_album_track_number = self._opts.use_track_numbers,
                    first_artist_only      = self._opts.first_artist_only,
                    extension              = ext,
                )
                candidate = _Path(out_dir) / fname
                if candidate.exists() and candidate.stat().st_size > 0:
                    found = candidate
                    break
            pre_existing.append(found)

        skip_count = sum(1 for p in pre_existing if p)
        if skip_count:
            remaining = total - skip_count
            log.info(
                "Pre-scan: %d/%d tracks already on disk%s",
                skip_count, total,
                f", downloading {remaining} remaining" if remaining else " — nothing to download",
            )

        for i, track in enumerate(self._tracks):
            coro = DownloadManager().start_download(track.id)
            if _asyncio.iscoroutine(coro):
                await coro
            existing = pre_existing[i]

            if existing:
                size_mb = existing.stat().st_size / (1024 * 1024)
                coro = DownloadManager().complete_download(track.id, str(existing), size_mb)
                if _asyncio.iscoroutine(coro):
                    await coro
                if self._on_track_result:
                    self._on_track_result({
                        "track_id": track.id,
                        "title": track.title, "artists": track.artists,
                        "success": True, "error": None,
                    })
                done += 1
                self._on_track_done(done)
                continue

            out_dir = track_dirs[i]
            if _dl_one_async is not None:
                result = await _dl_one_async(track, out_dir, self._providers, self._opts,
                                             i + 1, self._is_album)
            else:
                result = download_one(track, out_dir, self._providers, self._opts,
                                      i + 1, self._is_album)

            if result.success:
                expected_s = (track.duration_ms or 0) // 1000
                valid, val_reason = await _validate_track_async(result.file_path or "", expected_s)
                if not valid:
                    log.warning("Validation failed for %s: %s", track.title, val_reason)
                    self._failed.append((track.id, track.title, track.artists, val_reason))
                    coro = DownloadManager().fail_download(track.id, val_reason)
                    if _asyncio.iscoroutine(coro):
                        await coro
                    if self._on_track_result:
                        self._on_track_result({
                            "track_id": track.id,
                            "title": track.title, "artists": track.artists,
                            "success": False, "error": val_reason,
                        })
                else:
                    try:
                        size_mb = (
                            os.path.getsize(result.file_path) / (1024 * 1024)
                            if result.file_path and os.path.exists(result.file_path)
                            else 0.0
                        )
                    except OSError:
                        size_mb = 0.0
                    coro = DownloadManager().complete_download(track.id, result.file_path or "", size_mb)
                    if _asyncio.iscoroutine(coro):
                        await coro
                    if self._on_track_result:
                        self._on_track_result({
                            "track_id": track.id,
                            "title": track.title, "artists": track.artists,
                            "success": True, "error": None,
                        })
            else:
                err = result.error or "unknown"
                self._failed.append((track.id, track.title, track.artists, err))
                coro = DownloadManager().fail_download(track.id, err)
                if _asyncio.iscoroutine(coro):
                    await coro
                if self._on_track_result:
                    self._on_track_result({
                        "track_id": track.id,
                        "title": track.title, "artists": track.artists,
                        "success": False, "error": err,
                    })

            done += 1
            self._on_track_done(done)

            if i < total - 1:
                await _asyncio.sleep(self._opts.inter_track_delay_s)

        elapsed = time.perf_counter() - start
        self._print_summary(elapsed)
        return self._failed


class _TrackingDownloader(SpotiflacDownloader):
    """SpotiflacDownloader that reports on_progress(done, total) and per-track results."""

    def __init__(self, opts, on_progress, on_track_result=None):
        super().__init__(opts)
        self._on_progress    = on_progress
        self._on_track_result = on_track_result

    def _run_once(self, spotify_url, target_tracks=None):
        from SpotiFLAC.core.errors import SpotiflacError

        if target_tracks is not None:
            tracks          = target_tracks
            collection_name = ""
            is_album        = getattr(self._opts, 'is_album', False)
            is_playlist     = len(tracks) > 1
        elif hasattr(self, '_resolve_metadata'):
            # SpotiFLAC 0.4.7+ sync API
            try:
                collection_name, tracks, info = self._resolve_metadata(spotify_url)
            except SpotiflacError as exc:
                log.error("Metadata fetch failed: %s", exc)
                return []
            if not tracks:
                return []
            is_album    = info.get("type") == "album"
            is_playlist = info.get("type") in ("playlist", "artist", "artist_discography")
        elif hasattr(self, '_resolve_metadata_async'):
            # SpotiFLAC 1.2.7+ async API
            try:
                collection_name, tracks, info = _run_coro_sync(
                    self._resolve_metadata_async(spotify_url)
                )
            except Exception as exc:
                log.error("Metadata fetch failed: %s", exc)
                return []
            if not tracks:
                return []
            is_album    = info.get("type") == "album"
            is_playlist = info.get("type") in ("playlist", "artist", "artist_discography")
        else:
            # SpotiFLAC 0.3.x fallback
            from SpotiFLAC.providers.spotify_metadata import parse_spotify_url
            if hasattr(self._client, 'get_url'):
                collection_name, tracks = self._client.get_url(spotify_url)
            else:
                collection_name, tracks, *_ = _run_coro_sync(
                    self._client.get_url_async(spotify_url)
                )
            if not tracks:
                return []
            info        = parse_spotify_url(spotify_url)
            is_album    = info["type"] == "album"
            is_playlist = info["type"] == "playlist"

        total = len(tracks)
        self._on_progress(0, total)

        manager = DownloadManager()
        for t in tracks:
            _run_coro(manager.add_to_queue(t.id, t.title, t.artists, t.album, t.id))

        worker = _TrackingWorker(
            tracks          = tracks,
            opts            = self._opts,
            collection_name = collection_name,
            is_album        = is_album,
            is_playlist     = is_playlist,
            on_track_done   = lambda done: self._on_progress(done, total),
            on_track_result = self._on_track_result,
        )
        worker.run()
        return []

    def run(self, url: str) -> None:
        """Sync entry point: delegates to run_async for 1.2.9+, sync run for older."""
        if hasattr(SpotiflacDownloader, 'run_async'):
            _run_coro_sync(self.run_async(url))
        else:
            super().run(url)

    async def _run_worker_async(self, tracks, collection_name, info,
                                is_album, is_playlist, opts=None):
        """Override for SpotiFLAC 1.2.9+: swap DownloadWorker for _TrackingWorker."""
        effective = opts if opts is not None else self._opts
        manager   = DownloadManager()
        updated_tracks = []
        for i, t in enumerate(tracks):
            track_item_id    = t.id or getattr(t, 'external_url', '') or f"queue-{i}-{uuid.uuid4().hex}"
            track_spotify_id = t.id or getattr(t, 'external_url', '') or track_item_id
            coro = manager.add_to_queue(track_item_id, t.title, t.artists, t.album, track_spotify_id)
            if _asyncio.iscoroutine(coro):
                await coro
            if not t.id:
                try:
                    t = t.model_copy(update={"id": track_item_id})
                except AttributeError:
                    pass
            updated_tracks.append(t)

        total = len(updated_tracks)
        self._on_progress(0, total)

        worker = _TrackingWorker(
            tracks          = updated_tracks,
            opts            = effective,
            collection_name = collection_name,
            is_album        = is_album,
            is_playlist     = is_playlist,
            on_track_done   = lambda done: self._on_progress(done, total),
            on_track_result = self._on_track_result,
        )
        await worker.run_async()
        return []


def _run(job_id: str) -> None:
    """Execute one dispatch of a job. Semaphore slot is already held by dispatcher."""
    slot_held = True
    ev = _cancel.setdefault(job_id, threading.Event())

    try:
        if ev.is_set():
            _update(job_id, status="cancelled", finished_at=_now())
            _cancel.pop(job_id, None)
            return

        with _lock:
            j = _jobs.get(job_id)
        if not j:
            return

        url               = j["url"]
        output_dir        = j["output_dir"]
        services          = j["services"]
        filename_fmt      = j["filename_fmt"]
        quality           = j.get("quality") or "lossless"
        qobuz_token       = j.get("_qobuz_token") or ""
        pre_success_count = j.get("pre_success_count") or 0
        full_total        = j.get("full_total") or 0
        batch_urls        = j.get("_batch_urls") or []

        # Fetch metadata if title or cover are missing.
        # LB-sourced jobs have pre_title set so title is already known, but
        # cover_url is never pre-filled — fetch it whenever it is absent.
        if not j.get("title") or not j.get("cover_url"):
            title, cover_url, artist = _fetch_metadata(url)
            meta: dict = {}
            if title:     meta["title"]     = title
            if cover_url: meta["cover_url"] = cover_url
            if artist:    meta["artist"]    = artist
            if meta:
                _update(job_id, **meta)

        track_results: list[dict] = []
        succeeded = False
        try:
            if ev.is_set():
                _update(job_id, status="cancelled", finished_at=_now())
                _cancel.pop(job_id, None)
                return

            _update(job_id, status="running", started_at=_now(),
                    finished_at=None, error=None, next_retry_at=None,
                    progress=pre_success_count if full_total else None,
                    total=full_total if full_total else None,
                    track_results=None, success_count=None, fail_count=None)

            import dataclasses as _dc
            import settings as _settings
            cfg = _settings.load()
            _do_fields = {f.name for f in _dc.fields(DownloadOptions)}
            _enrich_kwargs: dict = {}
            if "enrich_metadata" in _do_fields and cfg.get("enrich_metadata"):
                _enrich_kwargs["enrich_metadata"]  = True
                _enrich_kwargs["enrich_providers"] = cfg.get("enrich_providers", ["deezer", "apple"])
            opts = DownloadOptions(
                output_dir          = output_dir,
                services            = services,
                filename_format     = filename_fmt,
                quality             = quality,
                inter_track_delay_s = cfg["track_delay_s"],
                use_track_numbers   = True,
                first_artist_only   = True,
                **_enrich_kwargs,
            )

            def _on_progress(done, total):
                eff_total = full_total or total
                eff_done  = pre_success_count + done
                if eff_total > 1:
                    _update(job_id, progress=eff_done, total=eff_total)

            def _on_track_result(r):
                track_results.append(r)

            if batch_urls:
                from SpotiFLAC.providers.spotify_metadata import SpotifyMetadataClient, parse_spotify_url
                client = SpotifyMetadataClient()
                tracks = []
                for burl in batch_urls:
                    try:
                        info = parse_spotify_url(burl)
                        if info["type"] == "track":
                            if hasattr(client, 'get_track'):
                                tracks.append(client.get_track(info["id"]))
                            else:
                                tracks.append(_run_coro_sync(client.get_track_async(info["id"])))
                    except Exception as exc:
                        log.warning("Batch metadata fetch failed for %s: %s", burl, exc)
                if not tracks:
                    raise RuntimeError("Could not fetch metadata for any tracks in batch")
                _on_progress(0, len(tracks))
                _TrackingWorker(
                    tracks=tracks, opts=opts, collection_name="",
                    is_album=False, is_playlist=True,
                    on_track_done=lambda done: _on_progress(done, len(tracks)),
                    on_track_result=_on_track_result,
                ).run()
            else:
                _TrackingDownloader(opts, _on_progress, _on_track_result).run(url)

            new_success   = sum(1 for r in track_results if r["success"])
            new_fail      = len(track_results) - new_success
            success_count = pre_success_count + new_success
            fail_count    = new_fail
            total_count   = full_total or len(track_results)

            _update(
                job_id,
                status        = "done",
                finished_at   = _now(),
                error         = None,
                progress      = success_count if total_count > 1 else None,
                total         = total_count   if total_count > 1 else None,
                track_results = track_results if track_results else None,
                success_count = success_count if track_results else None,
                fail_count    = fail_count    if track_results else None,
            )
            # Treat complete failure (all tracks failed) same as an exception so
            # auto-retry kicks in.  Partial success (at least one track OK) is done.
            succeeded = (new_success > 0) or not track_results
            _update_fail_streak(succeeded and bool(track_results))
        except Exception as exc:
            log.error("Job %s failed: %s", job_id, exc)
            _update(job_id, status="error", error=str(exc), finished_at=_now(),
                    track_results=track_results if track_results else None,
                    success_count=sum(1 for r in track_results if r["success"]) if track_results else None,
                    fail_count=sum(1 for r in track_results if not r["success"]) if track_results else None)

        if succeeded or ev.is_set():
            _cancel.pop(job_id, None)
            if succeeded and track_results:
                try:
                    import lib_index as _li
                    ok_titles = [r["title"] for r in track_results if r.get("success") and r.get("title")]
                    if ok_titles:
                        _li.add_titles(ok_titles)
                except Exception:
                    pass
            return

        # ── Retry logic ───────────────────────────────────────────────────────
        import settings as _settings
        cfg          = _settings.load()
        interval_min = cfg["retry_interval_min"]
        max_retries  = cfg["retry_max_count"]

        if not interval_min:
            _cancel.pop(job_id, None)
            return

        retry_count = (j.get("retry_count") or 0) + 1

        if max_retries > 0 and retry_count > max_retries:
            _update(job_id, status="error", finished_at=_now(),
                    error=f"Failed after {max_retries} auto-retr{'y' if max_retries == 1 else 'ies'}")
            _cancel.pop(job_id, None)
            return

        next_retry_at = (
            datetime.now(timezone.utc) + timedelta(seconds=interval_min * 60)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        _update(job_id, status="queued", finished_at=None, error=None,
                retry_count=retry_count, retry_max=max_retries,
                next_retry_at=next_retry_at)

        # Release slot before sleeping so other jobs can run.
        _semaphore.release()
        slot_held = False
        with _pq_cv:
            _pq_cv.notify_all()

        for _ in range(interval_min * 60):
            if ev.is_set():
                break
            time.sleep(1)

        if ev.is_set():
            _update(job_id, status="cancelled", finished_at=_now())
            _cancel.pop(job_id, None)
            return

        _update(job_id, next_retry_at=None)

        # Re-enqueue via priority queue so ordering is respected on the next attempt.
        with _lock:
            seq = (_jobs.get(job_id) or {}).get("_seq", 0)
        _push_pq(job_id, seq)

    finally:
        if slot_held:
            _semaphore.release()
            with _pq_cv:
                _pq_cv.notify_all()
