#!/bin/sh
# Add Stremio Native to the application menu for the current user.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"
cat > "$APPS/io.github.stremio_native.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Stremio
GenericName=Media Center
Comment=Watch movies and series from Stremio addons
Exec="$HERE/stremio-native"
Icon=video-x-generic
Terminal=false
Categories=AudioVideo;Video;Player;
StartupWMClass=stremio-native
EOF
update-desktop-database "$APPS" 2>/dev/null || true
echo "Installed $APPS/io.github.stremio_native.desktop"
