import hashlib
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

import json

from flask import Blueprint, Response, jsonify, render_template, request, send_file, stream_with_context

import settings as _settings
import worker
import vpn
import lib_index
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
    cfg = _settings.load()
    return render_template(
        "index.html",
        services=cfg["services"],
        filename_fmt=cfg["filename_fmt"],
    )


@bp.post("/api/download")
def api_download():
    body = request.get_json(silent=True) or {}
    raw  = body.get("urls", "")
    urls = [u.strip() for u in raw.replace(",", "\n").splitlines() if u.strip()]
    if not urls:
        return jsonify(error="No URLs provided"), 400

    cfg = _settings.load()

    # Whitelist services — reject unknown values
    raw_services = body.get("services", cfg["services"])
    services = [s for s in raw_services if s in _VALID_SERVICES]
    if not services:
        return jsonify(error="No valid services specified"), 400

    raw_quality = body.get("quality", "lossless")
    quality = raw_quality if raw_quality in _VALID_QUALITIES else "lossless"
    qobuz_token = str(body.get("qobuz_token") or cfg["qobuz_token"])

    # Optional offset fields for partial retries
    pre_success_count = _safe_int(body.get("pre_success_count", 0), 0)
    full_total        = _safe_int(body.get("full_total", 0), 0)
    pre_title         = str(body.get("pre_title", ""))

    os.makedirs(Config.OUTPUT_DIR, exist_ok=True)

    common = dict(
        output_dir=Config.OUTPUT_DIR,
        services=services,
        filename_fmt=cfg["filename_fmt"],
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


@bp.post("/api/vpn/reconnect")
def api_vpn_reconnect():
    try:
        open("/tmp/vpn_reconnect", "w").close()
        return jsonify(ok=True)
    except Exception as exc:
        return jsonify(error=str(exc)), 500


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


@bp.get("/api/search/expand")
def api_search_expand():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify(error="No URL provided"), 400
    try:
        from SpotiFLAC.providers.spotify_metadata import SpotifyMetadataClient
        client = SpotifyMetadataClient(timeout_s=15)
        name, tracks = client.get_url(url)
        return jsonify(
            title=name,
            tracks=[{
                "title":        t.title,
                "artists":      t.artists,
                "duration_ms":  t.duration_ms,
                "track_number": t.track_number,
                "url":          t.external_url,
                "cover_url":    t.cover_url,
                "year":         t.year,
            } for t in tracks],
        )
    except Exception as exc:
        log.warning("Expand failed: %s", exc)
        return jsonify(error=str(exc)), 502


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


_TRACK_NUM_RE = re.compile(r'^\d+\s+')


@bp.post("/api/library/check-items")
def api_library_check_items():
    body  = request.get_json(silent=True) or {}
    items = body.get("items", [])
    if not items:
        return jsonify({})

    result: dict[str, str] = {}
    tracks  = [i for i in items if i.get("type") == "track"]
    albums  = [i for i in items if i.get("type") in ("album", "playlist")]

    if tracks:
        hits = lib_index.check([t.get("title", "") for t in tracks])
        for item, hit in zip(tracks, hits):
            result[item.get("url", "")] = "full" if hit else "none"

    for item in albums:
        status = lib_index.check_album(item.get("title", ""), item.get("track_count"))
        result[item.get("url", "")] = status

    return jsonify(result)


@bp.post("/api/library/rescan")
def api_library_rescan():
    lib_index.trigger_rescan()
    return jsonify(ok=True)


@bp.get("/api/tasks")
def api_tasks():
    import math
    idx = lib_index.status()
    if idx["scanning"]:
        detail = "Scanning…"
    elif idx["last_elapsed"] is not None:
        secs = idx["last_elapsed"]
        dur  = f"{secs:.1f}s" if secs < 60 else f"{math.floor(secs/60)}m {secs%60:.0f}s"
        detail = f"{idx['count']:,} tracks indexed in {dur}"
    else:
        detail = f"{idx['count']:,} tracks indexed"
    tasks = [
        {
            "id":      "lib-index",
            "label":   "Library Index",
            "running": idx["scanning"],
            "detail":  detail,
        },
    ]
    return jsonify(tasks=tasks, any_running=any(t["running"] for t in tasks))


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


# ── Library organizer ─────────────────────────────────────────────────────────

_AUDIO_EXTS = frozenset({".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".wma"})
_FEAT_RE    = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", re.IGNORECASE)


def _org_main_artist(audio_easy) -> str:
    """Return the artist string, joining multiple tag values to match build_filename output."""
    for key in ("albumartist", "artist"):
        vals = audio_easy.get(key)
        if vals:
            # Mutagen stores multi-artist tracks as a list of separate tag values.
            # Re-join with ", " to match the comma-separated string that build_filename
            # uses when constructing the download path.
            raw = ", ".join(str(v).strip() for v in vals if str(v).strip())
            if raw:
                cleaned = _FEAT_RE.sub("", raw).strip()
                return cleaned if cleaned else raw
    return "Unknown Artist"


def _org_san(s: str, fallback: str = "_") -> str:
    return (re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(s))
            .strip().strip(".") or fallback)[:200]


def _org_target(audio_easy, fmt: str, ext: str) -> str:
    """Compute the target relative path for an audio file given its easy tags."""
    artist = _org_san(_org_main_artist(audio_easy))
    album  = _org_san(str((audio_easy.get("album")  or ["Unknown Album"])[0]).strip() or "Unknown Album")
    title  = _org_san(str((audio_easy.get("title")  or ["Unknown Title"])[0]).strip()  or "Unknown Title")

    raw_trk = str((audio_easy.get("tracknumber") or ["0"])[0])
    try:
        trk = int(re.split(r"[/\-]", raw_trk)[0].strip())
    except (ValueError, AttributeError):
        trk = 0
    track = f"{trk:02d}" if trk else ""

    result = (fmt
              .replace("{artist}", artist)
              .replace("{album}",  album)
              .replace("{title}",  title)
              .replace("{track}",  track))
    parts = [_org_san(p) for p in result.replace("\\", "/").split("/") if p.strip()]
    return "/".join(parts or ["Unsorted"]) + ext.lower()


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, separators=(',', ':'))}\n\n"


def _org_collect(root: str) -> list[tuple[str, str]]:
    """Snapshot all audio file paths before any moves happen."""
    files: list[tuple[str, str]] = []
    for dirpath, dirs, fnames in os.walk(root):
        dirs.sort()
        for fname in sorted(fnames):
            ext = os.path.splitext(fname)[1].lower()
            if ext in _AUDIO_EXTS:
                files.append((os.path.join(dirpath, fname), ext))
    return files


@bp.post("/api/library/organize/preview")
def api_org_preview():
    body = request.get_json(silent=True) or {}
    fmt  = str(body.get("format", "{artist}/{album}/{track} {title}")).strip() or "{artist}/{album}/{track} {title}"
    root = _lib_root()
    if not os.path.isdir(root):
        return jsonify(error="Library directory not found"), 404

    def generate():
        from mutagen import File as MFile
        all_files = _org_collect(root)
        total     = len(all_files)
        yield _sse({"type": "total", "total": total})
        ops: list[dict] = []
        for i, (src_abs, ext) in enumerate(all_files):
            src_rel = os.path.relpath(src_abs, root).replace(os.sep, "/")
            try:
                audio = MFile(src_abs, easy=True)
                if audio is None:
                    ops.append({"src": src_rel, "error": "Unrecognised format"})
                else:
                    dst_rel = _org_target(audio, fmt, ext)
                    ops.append({"src": src_rel, "dst": dst_rel, "changed": src_rel != dst_rel})
            except Exception as exc:
                ops.append({"src": src_rel, "error": str(exc)[:120]})
            yield _sse({"type": "progress", "done": i + 1, "total": total})
        yield _sse({"type": "done", "ops": ops})

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@bp.post("/api/library/organize/apply")
def api_org_apply():
    body = request.get_json(silent=True) or {}
    fmt  = str(body.get("format", "{artist}/{album}/{track} {title}")).strip() or "{artist}/{album}/{track} {title}"
    root = _lib_root()
    if not os.path.isdir(root):
        return jsonify(error="Library directory not found"), 404

    def generate():
        from mutagen import File as MFile
        all_files = _org_collect(root)
        total     = len(all_files)
        yield _sse({"type": "total", "total": total})
        moved = errors = 0
        for i, (src_abs, ext) in enumerate(all_files):
            src_rel = os.path.relpath(src_abs, root).replace(os.sep, "/")
            try:
                if not os.path.isfile(src_abs):
                    pass  # already relocated — not an error
                else:
                    audio = MFile(src_abs, easy=True)
                    if audio is None:
                        raise ValueError("Unrecognised format")
                    dst_rel = _org_target(audio, fmt, ext)
                    if src_rel != dst_rel:
                        dst_abs = os.path.join(root, *dst_rel.split("/"))
                        if os.path.exists(dst_abs) and os.path.normcase(src_abs) != os.path.normcase(dst_abs):
                            errors += 1
                        else:
                            os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
                            shutil.move(src_abs, dst_abs)
                            moved += 1
            except Exception:
                errors += 1
            yield _sse({"type": "progress", "done": i + 1, "total": total,
                        "moved": moved, "errors": errors})
        # Remove empty directories left behind
        for dp, _, _ in os.walk(root, topdown=False):
            if dp == root:
                continue
            try:
                if not os.listdir(dp):
                    os.rmdir(dp)
            except OSError:
                pass
        yield _sse({"type": "done", "moved": moved, "errors": errors})

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Settings ──────────────────────────────────────────────────────────────────

@bp.get("/api/settings")
def api_settings_get():
    return jsonify(_settings.load())


@bp.patch("/api/settings")
def api_settings_patch():
    body    = request.get_json(silent=True) or {}
    updates = {}
    errors  = {}

    for key in ("retry_interval_min", "retry_max_count", "max_workers", "reconnect_threshold"):
        if key not in body:
            continue
        try:
            updates[key] = int(body[key])
        except (TypeError, ValueError):
            errors[key] = "must be an integer"

    if "track_delay_s" in body:
        try:
            updates["track_delay_s"] = float(body["track_delay_s"])
        except (TypeError, ValueError):
            errors["track_delay_s"] = "must be a number"

    for key in ("filename_fmt", "qobuz_token"):
        if key in body:
            updates[key] = str(body[key])

    if "services" in body:
        raw = body["services"]
        if not isinstance(raw, list):
            errors["services"] = "must be a list"
        else:
            cleaned = [s for s in raw if s in _VALID_SERVICES]
            if not cleaned:
                errors["services"] = "no valid service names"
            else:
                updates["services"] = cleaned

    if errors:
        return jsonify(error="Invalid values", fields=errors), 400
    _settings.save(updates)
    return jsonify(ok=True, settings=_settings.load())


_sf_version_cache: dict = {}


def _ver_tuple(v: str) -> tuple:
    parts = [int(x) for x in v.split(".")[:3]]
    return tuple(parts + [0] * (3 - len(parts)))


def _sf_installed_version() -> str:
    """Read version from /spotiflac dist-info, returning the highest found.

    pip install --upgrade --target can leave old dist-info dirs alongside the
    new one; taking the max ensures we always report the installed version.
    """
    import glob
    found: list[str] = []
    for di in glob.glob("/spotiflac/SpotiFLAC-*.dist-info") + glob.glob("/spotiflac/spotiflac-*.dist-info"):
        try:
            with open(os.path.join(di, "METADATA")) as f:
                for line in f:
                    if line.lower().startswith("version:"):
                        found.append(line.split(":", 1)[1].strip())
                        break
        except OSError:
            pass
    if found:
        return max(found, key=_ver_tuple)
    try:
        from importlib.metadata import version as _imv
        return _imv("SpotiFLAC")
    except Exception:
        return "unknown"


@bp.get("/api/spotiflac/version")
def api_spotiflac_version():
    import urllib.request as _ureq

    installed = _sf_installed_version()

    now = time.time()
    if _sf_version_cache.get("ts", 0) > now - 3600:
        latest = _sf_version_cache["latest"]
    else:
        try:
            req = _ureq.Request(
                "https://pypi.org/pypi/SpotiFLAC/json",
                headers={"User-Agent": "spotiflac-ui/1.0"},
            )
            with _ureq.urlopen(req, timeout=8) as resp:
                latest = json.loads(resp.read())["info"]["version"]
            _sf_version_cache["latest"] = latest
            _sf_version_cache["ts"] = now
        except Exception as exc:
            log.debug("PyPI version check failed: %s", exc)
            latest = installed

    try:
        update_available = (
            installed != "unknown"
            and _ver_tuple(latest) > _ver_tuple(installed)
        )
    except Exception:
        update_available = False

    return jsonify(installed=installed, latest=latest, update_available=update_available)


@bp.post("/api/spotiflac/update")
def api_spotiflac_update():
    pip = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade",
         "--target", "/spotiflac", "SpotiFLAC"],
        capture_output=True, text=True,
    )
    if pip.returncode != 0:
        return jsonify(error=pip.stderr or "pip failed"), 500

    patch = subprocess.run(
        [sys.executable, "/app/patch_spotiflac.py"],
        capture_output=True, text=True,
    )

    # Kill the Gunicorn master — entrypoint.sh's monitor_tunnel watches APP_PID
    # and calls exit 1 when it dies, which makes Docker restart the container.
    # os.kill(1, SIGTERM) is unreliable: Linux silently ignores SIGTERM on PID 1
    # when the process has no registered handler (init protection).
    def _restart():
        time.sleep(1)
        os.kill(os.getppid(), signal.SIGTERM)
    threading.Thread(target=_restart, daemon=True).start()

    return jsonify(ok=True, pip=pip.stdout.strip(), patch=patch.stdout.strip())
