#!/bin/sh
# One-time setup: the bundled torrent engine and (optionally) codecs and a menu entry.
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
if python3 -c "import libtorrent" 2>/dev/null; then
    echo "libtorrent: system package found"
else
    echo "Installing the libtorrent wheel into $HERE/vendor (no root needed)…"
    python3 -m pip install --quiet --target "$HERE/vendor" --only-binary=:all: libtorrent
fi
if ! gst-inspect-1.0 avdec_h264 >/dev/null 2>&1; then
    echo
    echo "Most releases need FFmpeg's GStreamer plugin (Dolby audio, HEVC 10-bit, XviD…). Install it with:"
    echo "    sudo apt install gstreamer1.0-libav"
    echo "(or use the Install codecs button inside the app)"
fi
"$HERE/install-desktop.sh"
