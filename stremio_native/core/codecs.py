"""Codec support: probe a stream for decoders GStreamer lacks, guess from release names,
and install the missing plugins through PackageKit."""

import re
import shutil
import subprocess

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstPbutils", "1.0")
from gi.repository import GLib, Gst, GstPbutils  # noqa: E402

CODEC_PACKAGES = ["gstreamer1.0-libav"]
INSTALL_COMMAND = "sudo apt install " + " ".join(CODEC_PACKAGES)
PROBE_TIMEOUT_S = 20

# (pattern in a release name, decoder needed, human name)
_HINTS = [
    (r"\b(DDP|DD\+|E-?AC-?3|Atmos)", "avdec_eac3", "Dolby Digital Plus audio"),
    (r"\b(AC-?3|DD[ .]?[257]\.[01])\b", "avdec_ac3", "Dolby Digital audio"),
    (r"\bTrueHD\b", "avdec_truehd", "Dolby TrueHD audio"),
    (r"\bDTS", "avdec_dca", "DTS audio"),
    (r"\b(XviD|DivX)\b", "avdec_mpeg4", "XviD/DivX video"),
]
_HEVC = r"\b(x265|HEVC|H\.?265)\b"


def have(element):
    return Gst.ElementFactory.find(element) is not None


def ffmpeg_installed():
    return have("avdec_h264")


def guess_missing(text):
    """Decoders a release probably needs that aren't installed, judged from its name."""
    if ffmpeg_installed():
        return []
    found = [label for pattern, element, label in _HINTS if re.search(pattern, text, re.I) and not have(element)]
    if re.search(_HEVC, text, re.I) and not have("libde265dec"):
        found.append("HEVC video")
    return found


def probe(url):
    """Blocking. Returns human-readable names of the decoders this stream needs but lacks.
    Raises on network or timeout errors – callers should then just try to play."""
    disc = GstPbutils.Discoverer.new(PROBE_TIMEOUT_S * Gst.SECOND)
    try:
        info = disc.discover_uri(url)
    except GLib.Error as e:
        info = getattr(e, "info", None)
        if info is None:
            raise
    missing = []
    for detail in info.get_missing_elements_installer_details() or []:
        # "gstreamer|1.0|app|E-AC-3 (ATSC A/52B) decoder|decoder-audio/x-eac3"
        parts = detail.split("|")
        name = parts[3] if len(parts) > 3 else detail
        if name not in missing:
            missing.append(name)
    return missing


def can_install():
    return shutil.which("pkcon") is not None


def install():
    """Blocking. Install the codec packages via PackageKit (the system asks for the password)."""
    proc = subprocess.run(["pkcon", "install", "--noninteractive", *CODEC_PACKAGES],
                          capture_output=True, text=True, timeout=900)
    if proc.returncode not in (0, 4):  # 4 = nothing to do (already installed)
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        raise RuntimeError("\n".join(tail) or f"pkcon exited with {proc.returncode}")


def reload_plugins():
    Gst.update_registry()
    return ffmpeg_installed()


_SUB_RENDERERS = ("textoverlay", "assrender", "dvbsuboverlay", "cc708overlay", "ttmlrender")
_removed = {}


def set_embedded_subtitles(enabled):
    """Show or hide subtitle tracks muxed into the video. GTK's player can't pick tracks,
    so this takes GStreamer's subtitle renderers out of this process's plugin registry
    (playbin then ignores the text). Applies to the next stream opened."""
    registry = Gst.Registry.get()
    for name in _SUB_RENDERERS:
        if enabled:
            factory = _removed.pop(name, None)
            if factory is not None:
                registry.add_feature(factory)
        elif name not in _removed:
            factory = Gst.ElementFactory.find(name)
            if factory is not None:
                _removed[name] = factory
                registry.remove_feature(factory)
