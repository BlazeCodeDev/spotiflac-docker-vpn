"""ReplayGain: measure a track's loudness and write the correction into its tags.

Non-destructive by construction: the audio is only read (ffmpeg's loudnorm filter in analysis
mode, via Mornye's core/replaygain.py, output discarded), and the only change to the file is
three added tags — REPLAYGAIN_TRACK_GAIN, REPLAYGAIN_TRACK_PEAK and
REPLAYGAIN_REFERENCE_LOUDNESS. A file that already carries ReplayGain tags is left alone, so
values written by another tool (album gain included) are never overwritten.

The file's modification time is kept, nudged forward 2 s: the library's "Added" date stays
what it was, while the tag cache (keyed on size + mtime) still sees that the file changed.
"""
import asyncio
import logging
import os

log = logging.getLogger(__name__)

# Formats whose ReplayGain convention we write. Opus is left out on purpose: it has its own
# R128 gain header, and REPLAYGAIN_* tags in an Opus file are ignored or misapplied by players.
SUPPORTED = (".flac", ".ogg", ".oga", ".mp3", ".m4a", ".mp4")
_GAIN = "REPLAYGAIN_TRACK_GAIN"
_MP4_KEY = "----:com.apple.iTunes:"


def supported(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in SUPPORTED


def has_replaygain(path: str) -> bool:
    """True when the file already carries a track gain, in any of the usual spellings."""
    from mutagen import File as MFile
    try:
        audio = MFile(path)
    except Exception:
        return False
    return has_replaygain_tags(getattr(audio, "tags", None))


def has_replaygain_tags(tags) -> bool:
    """Same check on an already-opened (non-easy) mutagen tag object."""
    if tags is None:
        return False
    try:
        if hasattr(tags, "getall"):                      # ID3: TXXX (foobar2000 style) or RVA2
            return (any(f.desc.upper() == _GAIN for f in tags.getall("TXXX"))
                    or bool(tags.getall("RVA2")))
        keys = [str(k).upper() for k in tags.keys()]
        return _GAIN in keys or (_MP4_KEY + _GAIN).upper() in keys
    except Exception:
        return False


def measure(path: str) -> dict | None:
    """{"gain": dB, "peak": linear, "lufs": measured, "tags": {...}}, or None when the file
    can't be measured (no ffmpeg, undecodable, silent, Mornye too old)."""
    try:
        from mornye.core.replaygain import analyse_async
    except Exception as exc:
        log.debug("ReplayGain unavailable: %s", exc)
        return None
    try:
        res = asyncio.run(analyse_async(path))
    except Exception as exc:
        log.debug("ReplayGain analysis failed for %s: %s", path, exc)
        return None
    if res is None:
        return None
    return {"gain": res.track_gain_db, "peak": res.track_peak, "lufs": res.input_lufs, "tags": res.as_tags()}


def _write(path: str, tags: dict) -> None:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".flac":
        from mutagen.flac import FLAC
        audio = FLAC(path)
        for k, v in tags.items():
            audio[k] = [v]
        audio.save()
    elif ext in (".ogg", ".oga"):
        from mutagen.oggvorbis import OggVorbis
        audio = OggVorbis(path)
        for k, v in tags.items():
            audio[k] = [v]
        audio.save()
    elif ext == ".mp3":
        from mutagen.id3 import ID3, ID3NoHeaderError, TXXX
        try:
            audio = ID3(path)
        except ID3NoHeaderError:
            audio = ID3()
        for k, v in tags.items():
            audio.add(TXXX(encoding=3, desc=k, text=[v]))
        audio.save(path)
    elif ext in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4, MP4FreeForm
        audio = MP4(path)
        for k, v in tags.items():
            audio[_MP4_KEY + k] = [MP4FreeForm(v.encode())]
        audio.save()
    else:
        raise ValueError("format not supported")


def apply(path: str) -> tuple[str, str]:
    """Measure one file and tag it. Returns (status, text):
    "present"  — it already had ReplayGain tags; nothing was touched,
    "written"  — tags added,
    "skipped"  — a format ReplayGain isn't written for,
    "failed"   — couldn't be measured or the tags couldn't be saved (the file is unchanged)."""
    if not supported(path):
        return "skipped", f"ReplayGain isn’t written for {os.path.splitext(path)[1].lstrip('.').upper() or 'this format'} files"
    if has_replaygain(path):
        return "present", "ReplayGain already present"
    m = measure(path)
    if m is None:
        return "failed", "ReplayGain: couldn’t measure the loudness"
    try:
        st = os.stat(path)
        _write(path, m["tags"])
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    except Exception as exc:
        log.warning("ReplayGain tag write failed for %s: %s", path, exc)
        return "failed", f"ReplayGain: couldn’t save the tags ({exc})"
    return "written", f"ReplayGain {m['gain']:+.2f} dB (measured {m['lufs']:.1f} LUFS, peak {m['peak']:.3f})"
