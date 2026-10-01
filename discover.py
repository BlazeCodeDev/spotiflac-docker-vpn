"""Discover: suggestions built from what is already in the library.

Everything here is derived from the library's tags plus Spotify's public catalogue:
  releases  albums and singles by artists you collect that you don't have yet
  gaps      albums you own most of (the rest is one download away)
  songs     tracks by artists you don't have, found through similar artists and your top genres
Results are built in the background (a few dozen Spotify calls) and cached; "not interested" is stored
on disk and applied when the cache is read, so hiding something is instant.
"""
import json
import logging
import os
import re
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_FILE = os.environ.get("DISCOVER_FILE", "/vpn/discover.json")
_TTL = 6 * 3600
_lock = threading.Lock()
_cache: dict[str, dict] = {}     # "all" / "recent" -> {data, at, building, error, round}
_hidden_lock = threading.Lock()

TOP_ARTISTS = 12
MAX_RELEASES = 20
MAX_GAPS = 6
MAX_SONGS = 15


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).casefold()
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(re.sub(r"[\W_]+", " ", s).split())


# ── Hidden suggestions ────────────────────────────────────────────────────────
def _hidden_load() -> dict:
    try:
        with open(_FILE) as f:
            return dict(json.load(f).get("hidden") or {})
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.warning("Discover hidden list unreadable: %s", exc)
        return {}


def hidden() -> dict:
    with _hidden_lock:
        return _hidden_load()


def _hidden_save(h: dict) -> None:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(_FILE)), exist_ok=True)
        tmp = _FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"hidden": h}, f)
        os.replace(tmp, _FILE)
    except Exception as exc:
        log.warning("Discover hidden list not saved: %s", exc)


def hide(url: str, title: str = "", sub: str = "") -> None:
    if not url:
        return
    with _hidden_lock:
        h = _hidden_load()
        h[url] = {"title": title[:200], "sub": sub[:200], "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        _hidden_save(h)


def unhide(url: str) -> None:
    with _hidden_lock:
        h = _hidden_load()
        if h.pop(url, None) is not None:
            _hidden_save(h)


# ── Reading the library ───────────────────────────────────────────────────────
def _split_genres(g: str) -> list[str]:
    return [x.strip() for x in re.split(r"[;/,]", g or "") if x.strip()]


def _primary(rec: dict) -> str:
    """The artist a song belongs to: the album artist, else the first credited artist."""
    a = str(rec.get("albumartist") or "").strip()
    if not a or _norm(a) in ("various artists", "various", "va", "diverse"):
        a = str(rec.get("artist") or "").strip().split(",")[0].split(";")[0].strip()
    return a


def library_profile(records, recent_days: int = 0) -> dict:
    """Top artists, genres and the set of every artist name from the cached tag records."""
    cutoff = time.time() - recent_days * 86400 if recent_days else 0
    artists: Counter = Counter()
    names: dict[str, str] = {}
    genres: Counter = Counter()
    known: set[str] = set()
    for rec in records:
        if not rec:
            continue
        for part in re.split(r"[;,]", str(rec.get("artist") or "")):
            if part.strip():
                known.add(_norm(part))
        a = _primary(rec)
        if a:
            known.add(_norm(a))
        if cutoff and float(rec.get("mtime") or 0) < cutoff:
            continue
        if a:
            artists[_norm(a)] += 1
            names.setdefault(_norm(a), a)
        for g in _split_genres(rec.get("genre", "")):
            genres[g.title() if g.islower() else g] += 1
    return {"artists": [(names[k], n) for k, n in artists.most_common(TOP_ARTISTS) if k],
            "genres": genres.most_common(5), "known": known}


# ── Spotify ───────────────────────────────────────────────────────────────────
def _artist_id(client, name: str) -> tuple[str, str] | None:
    data = client._get("/search", params={"q": f'artist:"{name}"', "type": "artist", "limit": 5})
    for a in (data.get("artists") or {}).get("items") or []:
        if a and _norm(a.get("name")) == _norm(name):
            return a["id"], a["name"]
    return None


def _cover(images) -> str:
    imgs = images or []
    return imgs[-2]["url"] if len(imgs) > 1 else imgs[0]["url"] if imgs else ""


def _top_tracks(client, artist_id: str) -> list[dict]:
    data = client._get(f"/artists/{artist_id}/top-tracks", params={"market": "US"})
    return [t for t in data.get("tracks") or [] if t]


def _song(t: dict, reason: str, source: str) -> dict:
    album = t.get("album") or {}
    return {"title": t.get("name", ""), "artists": ", ".join(a["name"] for a in t.get("artists") or []),
            "album": album.get("name", ""), "cover_url": _cover(album.get("images")),
            "url": f"https://open.spotify.com/track/{t['id']}", "duration_ms": t.get("duration_ms") or 0,
            "reason": reason, "source": source}


# ── Building ──────────────────────────────────────────────────────────────────
def _build(records, client, releases_of, track_owned, album_have, album_status, recent: bool, rnd: int) -> dict:
    prof = library_profile(records, 30 if recent else 0)
    if not prof["artists"]:
        return {"state": "empty", "taste": [], "releases": [], "gaps": [], "songs": [], "seeds": []}
    top_g = prof["genres"]
    wmax = top_g[0][1] if top_g else 1
    taste = [{"name": g, "weight": round(n / wmax * 100)} for g, n in top_g]

    def one(name):
        try:
            found = _artist_id(client, name)
            return (name, found[0], releases_of(client, found[0])) if found else None
        except Exception as exc:
            log.info("Discover: %s skipped (%s)", name, exc)
            return None

    with ThreadPoolExecutor(4) as pool:
        per_artist = [r for r in pool.map(one, [a for a, _ in prof["artists"]]) if r]

    releases, gaps = [], []
    for name, _aid, rels in per_artist:
        for r in rels:
            tc = r["track_count"]
            st = album_status(r["title"], tc)
            if st == "full" or (tc <= 3 and track_owned(r["title"], name)):
                continue
            item = {"title": r["title"], "artist": name, "type": r["type"], "year": r["year"], "url": r["url"],
                    "cover_url": r["cover_url"], "track_count": tc, "date": r.get("date", "")}
            if st == "partial":
                have = album_have(r["title"])
                item.update(have=have)
                if tc >= 4 and have * 2 >= tc:
                    gaps.append(item)
                    continue
            releases.append(item)
    # A collaboration shows up under each of its artists.
    releases = list({r["url"]: r for r in releases}.values())
    gaps = list({r["url"]: r for r in gaps}.values())
    releases.sort(key=lambda r: r["date"] or r["year"] or "", reverse=True)
    gaps.sort(key=lambda r: r["have"] / max(1, r["track_count"]), reverse=True)

    # Songs: artists you don't have, via similar artists (when Spotify still serves them) and your genres.
    songs, seen, taken = [], set(), set(prof["known"])
    similar_ok = True

    def add_artist(a, reason, source):
        if not a or _norm(a.get("name")) in taken:
            return
        taken.add(_norm(a["name"]))
        try:
            for t in _top_tracks(client, a["id"])[:2]:
                key = (_norm(t.get("name")), _norm((t.get("artists") or [{}])[0].get("name")))
                if key in seen or track_owned(t.get("name", ""), ", ".join(x["name"] for x in t.get("artists") or [])):
                    continue
                seen.add(key)
                songs.append(_song(t, reason, source))
        except Exception as exc:
            log.info("Discover: top tracks for %s failed (%s)", a.get("name"), exc)

    for name, aid, _ in per_artist[:4]:
        if not similar_ok:
            break
        try:
            rel = client._get(f"/artists/{aid}/related-artists").get("artists") or []
        except Exception as exc:
            log.info("Discover: similar artists unavailable (%s)", exc)
            similar_ok = False
            break
        for a in rel[(rnd % 3) * 3:(rnd % 3) * 3 + 3]:
            add_artist(a, f"Similar to {name}", "similar")
    for g, _n in top_g[:3]:
        try:
            found = client._get("/search", params={"q": f'genre:"{g}"', "type": "artist", "limit": 6,
                                                   "offset": (rnd % 4) * 6}).get("artists", {}).get("items") or []
        except Exception as exc:
            log.info("Discover: genre %s search failed (%s)", g, exc)
            continue
        for a in found[:4]:
            add_artist(a, g, "genres")
    # Interleave the sources so one of them doesn't fill the list.
    sim = [s for s in songs if s["source"] == "similar"]
    gen = [s for s in songs if s["source"] == "genres"]
    mixed = [x for pair in zip(sim, gen) for x in pair] + sim[len(gen):] + gen[len(sim):]
    return {"state": "ready", "taste": taste, "releases": releases[:MAX_RELEASES], "gaps": gaps[:MAX_GAPS],
            "songs": mixed[:MAX_SONGS], "seeds": [a for a, _ in prof["artists"]], "similar": similar_ok}


def snapshot(get_records, client_fn, releases_of, track_owned, album_have, album_status,
             recent: bool = False, refresh: bool = False) -> dict:
    """The cached suggestions (building them in the background when missing, stale or `refresh`)."""
    key = "recent" if recent else "all"
    with _lock:
        c = _cache.setdefault(key, {"data": None, "at": 0.0, "building": False, "error": "", "round": 0})
        stale = not c["data"] or time.time() - c["at"] > _TTL
        if (stale or refresh) and not c["building"]:
            records = get_records()
            if records is None:
                return {"state": "building", "building": True, "why": "library", "taste": [], "releases": [], "gaps": [],
                        "songs": [], "hidden": [], "error": ""}
            c["building"], c["error"] = True, ""
            if refresh:
                c["round"] += 1
            rnd = c["round"]

            def work():
                try:
                    data = _build(records, client_fn(), releases_of, track_owned, album_have, album_status, recent, rnd)
                    with _lock:
                        c["data"] = data
                        # An empty library is looked at again soon, not kept for hours.
                        c["at"] = time.time() if data["state"] == "ready" else time.time() - _TTL + 60
                except Exception as exc:
                    log.warning("Discover build failed: %s", exc)
                    with _lock:
                        c["error"] = str(exc)[:200] or "Spotify didn’t answer"
                finally:
                    with _lock:
                        c["building"] = False
            threading.Thread(target=work, daemon=True, name="discover").start()
        data, building, error, at = c["data"], c["building"], c["error"], c["at"]
    hid = hidden()
    out = dict(data) if data else {"state": "error" if error else "building", "taste": [], "releases": [], "gaps": [], "songs": []}
    for k in ("releases", "gaps", "songs"):
        out[k] = [x for x in out.get(k, []) if x["url"] not in hid]
    out.update(building=building, error=error if not data else "", built_at=at,
               hidden=[{"url": u, **v} for u, v in sorted(hid.items(), key=lambda kv: kv[1].get("at", ""), reverse=True)])
    return out
