"""Audio fingerprints (Chromaprint via the `fpcalc` binary) for the library's
mistag finder.

Purely local: fpcalc decodes the audio and returns a list of 32-bit ints;
two recordings of the same audio give lists whose bits mostly agree once
lined up. Nothing is sent anywhere.

Fingerprints are cached on disk keyed by (path, size, mtime) so a rescan of
an unchanged library only pays for new files.
"""
import base64
import concurrent.futures
import json
import logging
import os
import shutil
import subprocess
import threading
import zlib

_log = logging.getLogger(__name__)

try:
    import numpy as np
except Exception:  # pragma: no cover — numpy ships with SpotiFLAC
    np = None

FP_SECONDS = 120          # fingerprint the first two minutes — plenty to match
SIM_THRESHOLD = 0.88      # cross-encode (FLAC vs AAC) lands ~0.90+, unrelated ~0.5
MAX_OFFSET = 12           # ± ~1.5 s of lead-in difference (1 int ≈ 0.124 s)
MIN_OVERLAP = 0.6         # of the shorter fingerprint

_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(os.environ.get("SETTINGS_FILE", "/vpn/settings.json"))),
    "fingerprints.json",
)
_cache_lock = threading.Lock()


def available() -> bool:
    return np is not None and shutil.which("fpcalc") is not None


def _compute(path: str):
    """Fingerprint one file → np.uint32 array, or None if it can't be decoded."""
    try:
        out = subprocess.run(
            ["fpcalc", "-raw", "-json", "-length", str(FP_SECONDS), path],
            capture_output=True, timeout=120, check=True,
        ).stdout
        fp = json.loads(out).get("fingerprint") or []
        if len(fp) < 20:
            return None
        return np.array(fp, dtype=np.uint32)
    except Exception as exc:
        _log.debug("fpcalc failed for %s: %s", path, exc)
        return None


def _pack(arr) -> str:
    return base64.b64encode(zlib.compress(arr.tobytes())).decode()


def _unpack(s: str):
    return np.frombuffer(zlib.decompress(base64.b64decode(s)), dtype=np.uint32)


def _load_cache() -> dict:
    try:
        with open(_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        tmp = _CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, _CACHE_FILE)
    except Exception as exc:
        _log.debug("could not save fingerprint cache: %s", exc)


def fingerprints(paths: list[str], progress=None, workers: int = 4, cancel=None) -> dict:
    """{abs_path: np.uint32 array} for every path that could be fingerprinted."""
    with _cache_lock:
        cache = _load_cache()
    result: dict = {}
    todo: list[tuple[str, str]] = []
    total = len(paths)
    done = 0
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            done += 1
            continue
        stamp = f"{st.st_size}:{int(st.st_mtime)}"
        hit = cache.get(p)
        if hit and hit[0] == stamp:
            try:
                result[p] = _unpack(hit[1])
                done += 1
                continue
            except Exception:
                pass
        todo.append((p, stamp))
    if progress:
        progress(done, total)

    new_entries = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_compute, p): (p, stamp) for p, stamp in todo}
        for fut in concurrent.futures.as_completed(futs):
            if cancel is not None and cancel.is_set():
                ex.shutdown(wait=False, cancel_futures=True)
                break
            p, stamp = futs[fut]
            arr = fut.result()
            done += 1
            if arr is not None:
                result[p] = arr
                cache[p] = [stamp, _pack(arr)]
                new_entries += 1
            if progress:
                progress(done, total)

    # Keep what was computed even when cancelled — the next scan resumes from it.
    # Drop cache entries for files that no longer exist, then persist.
    if new_entries or len(cache) != len(paths):
        live = set(paths)
        cache = {k: v for k, v in cache.items() if k in live}
        with _cache_lock:
            _save_cache(cache)
    return result


_POP8 = None


def similarity(a, b) -> float:
    """Best bit-agreement (0–1) between two fingerprints over small offsets."""
    global _POP8
    if _POP8 is None:
        _POP8 = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)
    if len(a) < 40 or len(b) < 40:
        return 0.0
    # Cheap reject first: line a 100-int slice from a's middle up against the
    # same region of b at every offset. Unrelated audio sits near 0.5, so
    # anything under 0.7 here can't reach the threshold on the full compare.
    m = len(a) // 3
    seg = a[m:m + 100]
    if len(seg) >= 40:
        quick = 0.0
        for off in range(-MAX_OFFSET, MAX_OFFSET + 1):
            lo = m + off
            if lo < 0 or lo + len(seg) > len(b):
                continue
            d = np.bitwise_xor(seg, b[lo:lo + len(seg)]).view(np.uint8)
            quick = max(quick, 1.0 - _POP8[d].sum(dtype=np.int64) / (32.0 * len(seg)))
        if quick < 0.7:
            return quick
    need = int(min(len(a), len(b)) * MIN_OVERLAP)
    best = 0.0
    for off in range(-MAX_OFFSET, MAX_OFFSET + 1):
        x, y = (a[off:], b) if off >= 0 else (a, b[-off:])
        n = min(len(x), len(y))
        if n < need or n < 10:
            continue
        diff = np.bitwise_xor(x[:n], y[:n]).view(np.uint8)
        sim = 1.0 - _POP8[diff].sum(dtype=np.int64) / (32.0 * n)
        if sim > best:
            best = float(sim)
    return best
