"""
Build-time patches for SpotiFLAC:

1. models.py — fix build_filename so / in a filename format is kept as a
   directory separator instead of being stripped by the sanitize() call.

2. downloader.py — forward opts.quality to provider.download_track(), mapping
   abstract quality levels ("lossless", "hires") to provider-specific strings.

3. providers/deezer.py — the MusicBrainz tags (incl. MUSICBRAINZ_TRACKID, the
   recording mbid) were only embedded when downloading an album (is_album=True),
   so single-track downloads never got an mbid. Every other provider embeds
   mb_tags unconditionally — align deezer.py with that.

4. core/musicbrainz.py — MusicBrainz lookups were ISRC-only, and a huge
   fraction of tracks either have no ISRC or have one MusicBrainz doesn't
   index, so they silently never got an mbid. Adds a title+artist text-search
   fallback (fetch_mb_metadata_smart) used whenever the ISRC lookup comes up
   empty.

5. providers/*.py — every provider only attempted a MusicBrainz lookup at all
   when metadata.isrc was truthy, so tracks without an ISRC (very common for
   YouTube, and not rare elsewhere) never got a lookup attempt in the first
   place. Always construct AsyncMBFetch (passing title/artist along for the
   new text-search fallback) instead of gating on ISRC presence.
"""
import importlib.util, pathlib, sys

_spec = importlib.util.find_spec("SpotiFLAC")
if _spec is None or _spec.origin is None:
    print("[patch] SpotiFLAC not found — aborting")
    sys.exit(1)
_BASE = pathlib.Path(_spec.origin).parent

# ---------------------------------------------------------------------------
# Patch 1: models.py — preserve / as directory separator in build_filename
# ---------------------------------------------------------------------------

_MODELS = _BASE / "core/models.py"

_MODELS_OLD = "    result = sanitize(result)\n"
_MODELS_NEW = (
    "    # Sanitize each path component individually so / separators are kept.\n"
    "    result = \"/\".join(\n"
    "        re.sub(r'[\\\\*?:\"<>|]', \"\", part).strip()\n"
    "        for part in result.split(\"/\")\n"
    "        if part.strip()\n"
    "    )\n"
)

text = _MODELS.read_text()
if _MODELS_OLD not in text:
    print(f"[patch] {_MODELS.name}: already patched or different version — skipping")
else:
    _MODELS.write_text(text.replace(_MODELS_OLD, _MODELS_NEW, 1))
    print(f"[patch] {_MODELS.name}: build_filename patched to preserve / separators")

# ---------------------------------------------------------------------------
# Patch 2: downloader.py — forward quality per provider
# ---------------------------------------------------------------------------

_DOWNLOADER = _BASE / "downloader.py"

_DL_INSERT_BEFORE = "def _provider_extension(name: str) -> str:\n"
_DL_QUALITY_MAP = (
    "_QUALITY_MAP: dict[str, dict[str, str]] = {\n"
    "    \"tidal\":  {\"high\": \"LOSSLESS\", \"lossless\": \"LOSSLESS\", \"hires\": \"HI_RES\"},\n"
    "    \"qobuz\":  {\"high\": \"6\",        \"lossless\": \"7\",        \"hires\": \"27\"},\n"
    "}\n"
    "\n"
    "def _resolve_quality(provider_name: str, quality: str) -> str:\n"
    "    return _QUALITY_MAP.get(provider_name, {}).get(quality, quality)\n"
    "\n"
    "\n"
)

_DL_TRACK_OLD = (
    "            first_artist_only   = opts.first_artist_only,\n"
    "            allow_fallback      = opts.allow_fallback,\n"
)
_DL_TRACK_NEW = (
    "            first_artist_only   = opts.first_artist_only,\n"
    "            quality             = _resolve_quality(provider.name, opts.quality),\n"
    "            allow_fallback      = opts.allow_fallback,\n"
)

dl_text = _DOWNLOADER.read_text()

if _DL_TRACK_NEW in dl_text:
    print(f"[patch] {_DOWNLOADER.name}: already patched — skipping")
else:
    if _DL_INSERT_BEFORE not in dl_text or _DL_TRACK_OLD not in dl_text:
        print(f"[patch] {_DOWNLOADER.name}: pattern not found — skipping (different version?)")
    else:
        dl_text = dl_text.replace(_DL_INSERT_BEFORE, _DL_QUALITY_MAP + _DL_INSERT_BEFORE, 1)
        dl_text = dl_text.replace(_DL_TRACK_OLD, _DL_TRACK_NEW, 1)
        _DOWNLOADER.write_text(dl_text)
        print(f"[patch] {_DOWNLOADER.name}: quality forwarded to provider.download_track()")

# ---------------------------------------------------------------------------
# Patch 3: providers/deezer.py — always embed MusicBrainz tags (mbid), not
# only for album downloads.
# ---------------------------------------------------------------------------

_DEEZER = _BASE / "providers/deezer.py"

_DEEZER_OLD = "                extra_tags              = mb_tags if is_album else {},\n"
_DEEZER_NEW = "                extra_tags              = mb_tags,\n"

deezer_text = _DEEZER.read_text()
if _DEEZER_OLD not in deezer_text:
    print(f"[patch] {_DEEZER.name}: already patched or different version — skipping")
else:
    _DEEZER.write_text(deezer_text.replace(_DEEZER_OLD, _DEEZER_NEW, 1))
    print(f"[patch] {_DEEZER.name}: MusicBrainz tags (mbid) now embedded for single-track downloads too")

# ---------------------------------------------------------------------------
# Patch 4: core/musicbrainz.py — title/artist text-search fallback for tracks
# whose ISRC is missing or isn't indexed by MusicBrainz.
# ---------------------------------------------------------------------------

_MB_CORE = _BASE / "core/musicbrainz.py"

_MB_NEW_FUNCS = r'''def _parse_mb_recording(data: dict) -> dict:
    """Shared response parser for both ISRC and text-search recording lookups."""
    parsed: dict = {
        "genre": "", "original_date": "", "bpm": "", "mbid_track": "",
        "mbid_album": "", "mbid_artist": "", "mbid_relgroup": "",
        "mbid_albumartist": "", "albumartist_sort": "", "catalognumber": "",
        "label": "", "barcode": "", "organization": "",
        "country": "", "script": "", "status": "",
        "media": "", "type": "", "artist_sort": ""
    }

    recs = data.get("recordings", [])
    if recs:
        rec = recs[0]
        parsed["mbid_track"] = rec.get("id", "")
        parsed["original_date"] = rec.get("first-release-date", "")
        parsed["bpm"] = str(rec.get("bpm", "")) if rec.get("bpm") else ""

        credits = rec.get("artist-credit", [])
        if credits:
            artist_ids = []
            sort_names = []
            for c in credits:
                artist_obj = c.get("artist", {})
                a_id = artist_obj.get("id")
                a_sort = artist_obj.get("sort-name", "")
                phrase = c.get("joinphrase", "")
                if a_id: artist_ids.append(a_id)
                if a_sort: sort_names.append(a_sort + phrase)
            parsed["mbid_artist"] = "; ".join(artist_ids)
            parsed["artist_sort"] = "".join(sort_names)

        all_tags = rec.get("tags", [])
        for c in credits:
            all_tags.extend(c.get("artist", {}).get("tags", []))
        if all_tags:
            sorted_tags = sorted(all_tags, key=lambda x: x.get("count", 0), reverse=True)
            genres = []
            for t in sorted_tags:
                name = t.get("name", "").title()
                if name and name not in genres: genres.append(name)
            parsed["genre"] = "; ".join(genres[:5])

        releases = rec.get("releases", [])
        if releases:
            def _release_score(r: dict) -> int:
                score = 0
                if r.get("barcode"): score += 2
                if r.get("label-info"): score += 2
                if r.get("country"): score += 1
                if r.get("status") == "Official": score += 1
                return score

            rel = max(releases, key=_release_score)
            parsed["mbid_album"]    = rel.get("id", "")
            parsed["mbid_relgroup"] = rel.get("release-group", {}).get("id", "")
            parsed["status"]        = rel.get("status", "")
            parsed["type"]          = rel.get("release-group", {}).get("primary-type", "")
            parsed["country"]       = rel.get("country", "")
            parsed["script"]        = rel.get("text-representation", {}).get("script", "")
            media = rel.get("media", [])
            if media:
                parsed["media"] = media[0].get("format", "")

            rel_credits = rel.get("artist-credit", [])
            if rel_credits:
                aa_ids = []
                aa_sort_names = []
                for c in rel_credits:
                    artist_obj = c.get("artist", {})
                    a_id   = artist_obj.get("id")
                    a_sort = artist_obj.get("sort-name", "")
                    phrase = c.get("joinphrase", "")
                    if a_id:   aa_ids.append(a_id)
                    if a_sort: aa_sort_names.append(a_sort + phrase)
                parsed["mbid_albumartist"] = "; ".join(aa_ids)
                parsed["albumartist_sort"] = "".join(aa_sort_names)

            for r in releases:
                if not parsed.get("barcode") and r.get("barcode"):
                    parsed["barcode"] = r["barcode"]
                for li in r.get("label-info", []):
                    lbl = li.get("label") or {}
                    if not parsed.get("label") and lbl.get("name"):
                        parsed["label"]        = lbl["name"]
                        parsed["organization"] = lbl["name"]
                    if not parsed.get("catalognumber") and li.get("catalog-number"):
                        parsed["catalognumber"] = li["catalog-number"]
                if parsed.get("barcode") and parsed.get("label") and parsed.get("catalognumber"):
                    break

    return parsed


def _lucene_escape(s: str) -> str:
    """Escape Lucene special characters so free-text titles/artists (which
    often contain parentheses, colons, etc.) don't break the MB query syntax."""
    specials = set('+-!(){}[]^"~*?:\\/')
    return "".join(("\\" + ch) if ch in specials else ch for ch in s.strip())


def fetch_mb_metadata_by_text(title: str, artist: str) -> dict:
    """
    Fallback for tracks with no ISRC, or whose ISRC MusicBrainz doesn't have
    indexed: text-search MusicBrainz for a matching recording by title + artist.
    Not cached/deduplicated like fetch_mb_metadata() — callers should only
    reach this after an ISRC lookup has already failed to find an mbid.
    """
    if not title or not artist:
        return {}

    if should_skip_mb():
        logger.debug("[musicbrainz] text search skipped (offline recently)")
        return {}

    query = f'recording:"{_lucene_escape(title)}" AND artist:"{_lucene_escape(artist)}"'

    try:
        data = _query_recordings(query)
        set_mb_status(True)
        return _parse_mb_recording(data)
    except Exception as e:
        set_mb_status(False)
        logger.debug("[musicbrainz] text search failed for %r / %r: %s", title, artist, e)
        return {}


def fetch_mb_metadata_smart(isrc: str, title: str = "", artist: str = "") -> dict:
    """ISRC lookup first (cached, rate-limited), falling back to a title/artist
    text search when the ISRC is missing or MusicBrainz has no match for it.
    This is what actually guarantees an mbid gets found for tracks whose ISRC
    isn't in MusicBrainz's ISRC index (a very common gap)."""
    res = fetch_mb_metadata(isrc) if isrc else {}
    if res.get("mbid_track"):
        return res
    if title and artist:
        text_res = fetch_mb_metadata_by_text(title, artist)
        if text_res.get("mbid_track"):
            return text_res
    return res


'''

_MB_INSERT_BEFORE = "def fetch_mb_metadata(isrc: str) -> dict:\n"

_MB_PARSE_BLOCK_OLD = '''        parsed: dict = {
            "genre": "", "original_date": "", "bpm": "", "mbid_track": "",
            "mbid_album": "", "mbid_artist": "", "mbid_relgroup": "",
            "mbid_albumartist": "", "albumartist_sort": "", "catalognumber": "",
            "label": "", "barcode": "", "organization": "",
            "country": "", "script": "", "status": "",
            "media": "", "type": "", "artist_sort": ""
        }

        recs = data.get("recordings", [])
        if recs:
            rec = recs[0]
            parsed["mbid_track"] = rec.get("id", "")
            parsed["original_date"] = rec.get("first-release-date", "")
            parsed["bpm"] = str(rec.get("bpm", "")) if rec.get("bpm") else ""

            credits = rec.get("artist-credit", [])
            if credits:
                artist_ids = []
                sort_names = []
                for c in credits:
                    artist_obj = c.get("artist", {})
                    a_id = artist_obj.get("id")
                    a_sort = artist_obj.get("sort-name", "")
                    phrase = c.get("joinphrase", "")
                    if a_id: artist_ids.append(a_id)
                    if a_sort: sort_names.append(a_sort + phrase)
                parsed["mbid_artist"] = "; ".join(artist_ids)
                parsed["artist_sort"] = "".join(sort_names)

            all_tags = rec.get("tags", [])
            for c in credits:
                all_tags.extend(c.get("artist", {}).get("tags", []))
            if all_tags:
                sorted_tags = sorted(all_tags, key=lambda x: x.get("count", 0), reverse=True)
                genres = []
                for t in sorted_tags:
                    name = t.get("name", "").title()
                    if name and name not in genres: genres.append(name)
                parsed["genre"] = "; ".join(genres[:5])

            releases = rec.get("releases", [])
            if releases:
                def _release_score(r: dict) -> int:
                    score = 0
                    if r.get("barcode"): score += 2
                    if r.get("label-info"): score += 2
                    if r.get("country"): score += 1
                    if r.get("status") == "Official": score += 1
                    return score

                rel = max(releases, key=_release_score)
                parsed["mbid_album"]    = rel.get("id", "")
                parsed["mbid_relgroup"] = rel.get("release-group", {}).get("id", "")
                parsed["status"]        = rel.get("status", "")
                parsed["type"]          = rel.get("release-group", {}).get("primary-type", "")
                parsed["country"]       = rel.get("country", "")
                parsed["script"]        = rel.get("text-representation", {}).get("script", "")
                media = rel.get("media", [])
                if media:
                    parsed["media"] = media[0].get("format", "")

                rel_credits = rel.get("artist-credit", [])
                if rel_credits:
                    aa_ids = []
                    aa_sort_names = []
                    for c in rel_credits:
                        artist_obj = c.get("artist", {})
                        a_id   = artist_obj.get("id")
                        a_sort = artist_obj.get("sort-name", "")
                        phrase = c.get("joinphrase", "")
                        if a_id:   aa_ids.append(a_id)
                        if a_sort: aa_sort_names.append(a_sort + phrase)
                    parsed["mbid_albumartist"] = "; ".join(aa_ids)
                    parsed["albumartist_sort"] = "".join(aa_sort_names)

                for r in releases:
                    if not parsed.get("barcode") and r.get("barcode"):
                        parsed["barcode"] = r["barcode"]
                    for li in r.get("label-info", []):
                        lbl = li.get("label") or {}
                        if not parsed.get("label") and lbl.get("name"):
                            parsed["label"]        = lbl["name"]
                            parsed["organization"] = lbl["name"]
                        if not parsed.get("catalognumber") and li.get("catalog-number"):
                            parsed["catalognumber"] = li["catalog-number"]
                    if parsed.get("barcode") and parsed.get("label") and parsed.get("catalognumber"):
                        break

        res = parsed  # Lookup riuscito
'''
_MB_PARSE_BLOCK_NEW = '        res = _parse_mb_recording(data)  # Lookup riuscito\n'

_MB_INIT_OLD = (
    "    def __init__(self, isrc: str):\n"
    "        self.isrc = isrc\n"
    "        try:\n"
    "            self.future = self._get_executor().submit(fetch_mb_metadata, isrc)\n"
    "        except RuntimeError:\n"
    "            # executor spento e non ancora ricreato — retry\n"
    "            self.future = self._get_executor().submit(fetch_mb_metadata, isrc)\n"
)
_MB_INIT_NEW = (
    "    def __init__(self, isrc: str, title: str = \"\", artist: str = \"\"):\n"
    "        self.isrc = isrc\n"
    "        try:\n"
    "            self.future = self._get_executor().submit(fetch_mb_metadata_smart, isrc, title, artist)\n"
    "        except RuntimeError:\n"
    "            # executor spento e non ancora ricreato — retry\n"
    "            self.future = self._get_executor().submit(fetch_mb_metadata_smart, isrc, title, artist)\n"
)

mb_text = _MB_CORE.read_text()
if "_parse_mb_recording" in mb_text:
    print(f"[patch] {_MB_CORE.name}: already patched or different version — skipping")
elif _MB_INSERT_BEFORE not in mb_text or _MB_PARSE_BLOCK_OLD not in mb_text or _MB_INIT_OLD not in mb_text:
    print(f"[patch] {_MB_CORE.name}: pattern not found — skipping (different version?)")
else:
    mb_text = mb_text.replace(_MB_INSERT_BEFORE, _MB_NEW_FUNCS + _MB_INSERT_BEFORE, 1)
    mb_text = mb_text.replace(_MB_PARSE_BLOCK_OLD, _MB_PARSE_BLOCK_NEW, 1)
    mb_text = mb_text.replace(_MB_INIT_OLD, _MB_INIT_NEW, 1)
    _MB_CORE.write_text(mb_text)
    print(f"[patch] {_MB_CORE.name}: added ISRC->text-search mbid fallback (fetch_mb_metadata_smart)")

# ---------------------------------------------------------------------------
# Patch 5: providers/*.py — always attempt a MusicBrainz lookup (passing
# title/artist for the text-search fallback), instead of skipping it entirely
# when metadata.isrc is empty.
# ---------------------------------------------------------------------------

_PROVIDER_MB_EDITS = [
    ("providers/gdstudio.py",
     "            mb_fetcher = AsyncMBFetch(metadata.isrc) if metadata.isrc else None\n",
     "            mb_fetcher = AsyncMBFetch(metadata.isrc, metadata.title, metadata.first_artist)\n"),
    ("providers/apple_music.py",
     "            # Trigger Asincrono MusicBrainz\n"
     "            mb_fetcher = None\n"
     "            if metadata.isrc:\n"
     "                mb_fetcher = AsyncMBFetch(metadata.isrc)\n",
     "            # Trigger Asincrono MusicBrainz\n"
     "            mb_fetcher = AsyncMBFetch(metadata.isrc, metadata.title, metadata.first_artist)\n"),
    ("providers/amazon.py",
     "            from ..core.musicbrainz import AsyncMBFetch\n"
     "            mb_fetcher = AsyncMBFetch(metadata.isrc) if getattr(metadata, \"isrc\", None) else None\n",
     "            from ..core.musicbrainz import AsyncMBFetch\n"
     "            mb_fetcher = AsyncMBFetch(getattr(metadata, \"isrc\", \"\") or \"\", metadata.title, metadata.first_artist)\n"),
    ("providers/pandora.py",
     "            mb_fetcher = AsyncMBFetch(metadata.isrc) if metadata.isrc else None\n",
     "            mb_fetcher = AsyncMBFetch(metadata.isrc, metadata.title, metadata.first_artist)\n"),
    ("providers/deezer.py",
     "            mb_fetcher = AsyncMBFetch(isrc_to_use) if isrc_to_use else None\n",
     "            mb_fetcher = AsyncMBFetch(isrc_to_use, metadata.title, metadata.first_artist)\n"),
    ("providers/qobuz.py",
     "            mb_fetcher = None\n"
     "            if (enrich_metadata or embed_genre) and metadata.isrc:\n"
     "                mb_fetcher = AsyncMBFetch(metadata.isrc)\n",
     "            mb_fetcher = None\n"
     "            if enrich_metadata or embed_genre:\n"
     "                mb_fetcher = AsyncMBFetch(metadata.isrc, metadata.title, metadata.first_artist)\n"),
    ("providers/tidal.py",
     "            mb_fetcher = None\n"
     "            if metadata.isrc:\n"
     "                mb_fetcher = AsyncMBFetch(metadata.isrc)\n",
     "            mb_fetcher = AsyncMBFetch(metadata.isrc, metadata.title, metadata.first_artist)\n"),
    ("providers/youtube.py",
     "            mb_fetcher = AsyncMBFetch(metadata.isrc) if metadata.isrc else None\n",
     "            mb_fetcher = AsyncMBFetch(metadata.isrc, metadata.title, metadata.first_artist)\n"),
]

for rel_path, old, new in _PROVIDER_MB_EDITS:
    fpath = _BASE / rel_path
    if not fpath.exists():
        print(f"[patch] {rel_path}: file not found — skipping")
        continue
    ftext = fpath.read_text()
    if new in ftext:
        print(f"[patch] {rel_path}: already patched — skipping")
    elif old not in ftext:
        print(f"[patch] {rel_path}: pattern not found — skipping (different version?)")
    else:
        fpath.write_text(ftext.replace(old, new, 1))
        print(f"[patch] {rel_path}: MusicBrainz lookup no longer gated on ISRC presence")
