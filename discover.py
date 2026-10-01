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
import random
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
MAX_RELEASES = 60
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


def _artist_card(a: dict, reason: str, source: str) -> dict:
    return {"name": a.get("name", ""), "url": f"https://open.spotify.com/artist/{a['id']}", "cover_url": _cover(a.get("images")),
            "genres": [g for g in (a.get("genres") or [])[:2]], "reason": reason, "source": source}


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
        return {"state": "empty", "taste": [], "releases": [], "gaps": [], "songs": [], "artists": [], "seeds": []}
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

    finder = Songs(client, [(n, aid) for n, aid, _ in per_artist], top_g, prof["known"], track_owned)
    songs, _label, artists = finder.next("", reset=True)
    return {"state": "ready", "taste": taste, "releases": releases[:MAX_RELEASES], "gaps": gaps[:MAX_GAPS],
            "songs": songs, "artists": artists, "seeds": [a for a, _ in prof["artists"]], "similar": finder.similar_ok, "_finder": finder}


# Genres offered by "Surprise me" (the ones your library already leans on are left out).
_SURPRISE = ["indie rock", "hip hop", "jazz", "soul", "funk", "r&b", "reggae", "latin", "afrobeats", "k-pop", "ambient", "folk",
             "synthwave", "drum and bass", "techno", "disco", "blues", "classic rock", "metal", "country", "bossa nova", "trip hop",
             "shoegaze", "punk", "gospel", "lo-fi"]


class Songs:
    """Finds songs by artists you don't have yet, and keeps going where it stopped ("load more").
    A flavour steers it: "" mixes similar artists with your top genres, a genre name sticks to that genre,
    "~surprise" picks three genres your library doesn't lean on."""

    def __init__(self, client, per_artist, genres, known, track_owned):
        self.client, self.per_artist, self.genres = client, per_artist, genres
        self.known, self.track_owned = set(known), track_owned
        self.similar_ok = True
        self.shown: set[str] = set()   # artists already offered under any flavour, so a new flavour is never the old list again
        self.related: dict[str, list] = {}
        self.st: dict[str, dict] = {}
        self.lock = threading.Lock()

    def _state(self, flavour: str, reset: bool) -> dict:
        s = self.st.get(flavour)
        if s is None or reset:
            picked = None
            if flavour == "~surprise":
                mine = {_norm(g) for g, _ in self.genres}
                pool = [g for g in _SURPRISE if _norm(g) not in mine] or list(_SURPRISE)
                picked = random.sample(pool, min(3, len(pool)))
            # A fresh start begins at a random depth, since the top of a genre is what Mixed already showed.
            s = {"seen": set(), "taken": set(self.known) | self.shown, "cursor": {g: random.choice((0, 6, 12, 18, 24)) for g in (picked or [])},
                 "picked": picked, "sim": random.randrange(4) if flavour == "" and reset else 0, "fresh": True}
            self.st[flavour] = s
        return s

    def _related(self, name: str, aid: str) -> list:
        if aid not in self.related:
            try:
                self.related[aid] = self.client._get(f"/artists/{aid}/related-artists").get("artists") or []
            except Exception as exc:
                log.info("Discover: similar artists unavailable (%s)", exc)
                self.similar_ok = False
                self.related[aid] = []
        return self.related[aid]

    def next(self, flavour: str = "", reset: bool = False, want: int = 10) -> tuple[list[dict], str, list[dict]]:
        with self.lock:
            s = self._state(flavour, reset)
            cands: list[tuple[dict, str, str]] = []   # (artist, reason, source)

            def take(a, reason, source):
                if a and a.get("id") and _norm(a.get("name")) not in s["taken"]:
                    s["taken"].add(_norm(a["name"]))
                    self.shown.add(_norm(a["name"]))
                    cands.append((a, reason, source))

            if flavour == "":
                for name, aid in self.per_artist[:4]:
                    if not self.similar_ok:
                        break
                    rel = self._related(name, aid)
                    for a in rel[s["sim"] * 3:(s["sim"] + 1) * 3]:
                        take(a, f"Similar to {name}", "similar")
                s["sim"] += 1
                gl = [g for g, _ in self.genres[:3]]
            elif flavour == "~surprise":
                gl = s["picked"] or []
            else:
                gl = [flavour]
            for g in gl:
                if g not in s["cursor"]:
                    s["cursor"][g] = random.choice((0, 6, 12, 18, 24))
                added = 0
                for _page in range(4):   # skip pages whose artists were all offered already
                    off = s["cursor"][g]
                    try:
                        found = self.client._get("/search", params={"q": f'genre:"{g}"', "type": "artist", "limit": 6,
                                                                    "offset": off}).get("artists", {}).get("items") or []
                    except Exception as exc:
                        log.info("Discover: genre %s search failed (%s)", g, exc)
                        break
                    s["cursor"][g] = off + 6 if len(found) == 6 else 0
                    before = len(cands)
                    for a in found:
                        take(a, g.title() if g.islower() else g, "genres")
                    added += len(cands) - before
                    if added >= 3 or len(found) < 6:
                        break

            def tracks(c):
                try:
                    return c, _top_tracks(self.client, c[0]["id"])[:2]
                except Exception as exc:
                    log.info("Discover: top tracks for %s failed (%s)", c[0].get("name"), exc)
                    return c, []

            with ThreadPoolExecutor(4) as pool:
                got = list(pool.map(tracks, cands))
            sim, gen = [], []
            for (a, reason, source), ts in got:
                for t in ts:
                    key = (_norm(t.get("name")), _norm((t.get("artists") or [{}])[0].get("name")))
                    if key in s["seen"] or self.track_owned(t.get("name", ""), ", ".join(x["name"] for x in t.get("artists") or [])):
                        continue
                    s["seen"].add(key)
                    (sim if source == "similar" else gen).append(_song(t, reason, source))
            mixed = [x for pair in zip(sim, gen) for x in pair] + sim[len(gen):] + gen[len(sim):]
            log.info("Discover: flavour %r gave %d artists, %d songs", flavour, len(cands), len(mixed))
            artists = [_artist_card(a, reason, source) for (a, reason, source), _ in got if a.get("images")]
            return mixed[:want], ", ".join(s["picked"] or []), artists[:12]


def more_songs(recent: bool, flavour: str, more: bool) -> dict | None:
    """Another batch of songs (`more`) or a fresh start for `flavour`; None before the first build finished."""
    with _lock:
        c = _cache.get("recent" if recent else "all")
        finder = c and c["data"] and c["data"].get("_finder")
    if not finder:
        return None
    songs, label, artists = finder.next(flavour, reset=not more)
    hid = hidden()
    return {"songs": [x for x in songs if x["url"] not in hid], "artists": [x for x in artists if x["url"] not in hid],
            "label": label, "similar": finder.similar_ok}


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
                        "songs": [], "artists": [], "hidden": [], "error": ""}
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
    out = {k: v for k, v in data.items() if k != "_finder"} if data else {"state": "error" if error else "building", "taste": [], "releases": [], "gaps": [], "songs": [], "artists": []}
    for k in ("releases", "gaps", "songs", "artists"):
        out[k] = [x for x in out.get(k, []) if x["url"] not in hid]
    out.update(building=building, error=error if not data else "", built_at=at,
               hidden=[{"url": u, **v} for u, v in sorted(hid.items(), key=lambda kv: kv[1].get("at", ""), reverse=True)])
    return out
