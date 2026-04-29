"""
Build-time patches for SpotiFLAC:

1. models.py — fix build_filename so / in a filename format is kept as a
   directory separator instead of being stripped by the sanitize() call.

2. downloader.py — forward opts.quality to provider.download_track(), mapping
   abstract quality levels ("lossless", "hires") to provider-specific strings.
"""
import pathlib, sys

_BASE = pathlib.Path(
    f"/home/coder/.local/lib/python{sys.version_info.major}.{sys.version_info.minor}"
    "/site-packages/SpotiFLAC"
)

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
