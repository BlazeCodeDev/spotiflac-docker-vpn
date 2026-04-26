"""
Build-time patch: fix SpotiFLAC's build_filename so that / in a filename
format (e.g. {artist}/{year} - {album}/{track}. {title}) is kept as a
directory separator instead of being stripped by the sanitize() call.
"""
import pathlib, re, sys

_MODELS = pathlib.Path(
    f"/usr/local/lib/python{sys.version_info.major}.{sys.version_info.minor}"
    "/site-packages/SpotiFLAC/core/models.py"
)

OLD = "    result = sanitize(result)\n"
NEW = (
    "    # Sanitize each path component individually so / separators are kept.\n"
    "    result = \"/\".join(\n"
    "        re.sub(r'[\\\\*?:\"<>|]', \"\", part).strip()\n"
    "        for part in result.split(\"/\")\n"
    "        if part.strip()\n"
    "    )\n"
)

text = _MODELS.read_text()
if OLD not in text:
    print(f"[patch] {_MODELS.name}: pattern not found — already patched or different version")
    sys.exit(0)
_MODELS.write_text(text.replace(OLD, NEW, 1))
print(f"[patch] {_MODELS.name}: build_filename patched to preserve / separators")
