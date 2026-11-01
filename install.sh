#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/aradar46/vlc_lyrics_display.git"
INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/vlc-lyrics"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"

uninstall() {
    echo "Uninstalling VLC Lyrics Overlay..."
    rm -rf "$INSTALL_DIR"
    rm -f "$BIN_DIR/vlc-lyrics"
    rm -f "$APPS_DIR/org.vlc.lyrics.overlay.desktop"
    rm -f "$APPS_DIR/vlc-lyrics.desktop"
    rm -f "$ICONS_DIR/vlc-lyrics.svg"
    rm -f "$ICONS_DIR/org.vlc.lyrics.overlay.svg"
    
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$APPS_DIR" 2>/dev/null || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -f -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" 2>/dev/null || true
    fi
    echo "Uninstalled successfully."
    exit 0
}

if [ "${1:-}" = "--uninstall" ] || [ "${1:-}" = "uninstall" ]; then
    uninstall
fi

echo "Installing VLC Lyrics Overlay..."

# 1. Fetch / update repository
mkdir -p "$(dirname "$INSTALL_DIR")"
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "Updating existing installation in $INSTALL_DIR..."
    git -C "$INSTALL_DIR" pull --ff-only
elif [ -d "$INSTALL_DIR" ]; then
    rm -rf "$INSTALL_DIR"
    git clone "$REPO_URL" "$INSTALL_DIR"
else
    # Check if run from a local repo checkout containing the files
    LOCAL_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
    if [ -n "$LOCAL_SRC" ] && [ -f "$LOCAL_SRC/lyrics_overlay.py" ] && [ "$LOCAL_SRC" != "$INSTALL_DIR" ]; then
        echo "Copying from local repository $LOCAL_SRC to $INSTALL_DIR..."
        mkdir -p "$INSTALL_DIR"
        cp -a "$LOCAL_SRC"/. "$INSTALL_DIR"/
    else
        echo "Cloning from $REPO_URL to $INSTALL_DIR..."
        git clone "$REPO_URL" "$INSTALL_DIR"
    fi
fi

# 2. Set up virtual environment with system site-packages (for GTK 4 and PyGObject)
echo "Setting up Python environment..."
python3 -m venv --system-site-packages "$INSTALL_DIR/.venv"

# Ensure requests is installed in the sandboxed venv if not available from system
if ! "$INSTALL_DIR/.venv/bin/python3" -c "import requests" 2>/dev/null; then
    echo "Installing requests into virtual environment..."
    "$INSTALL_DIR/.venv/bin/pip" install --quiet requests
fi

# Verify PyGObject and GTK 4
if ! "$INSTALL_DIR/.venv/bin/python3" -c "import gi; gi.require_version('Gtk', '4.0')" 2>/dev/null; then
    echo ""
    echo "Notice: PyGObject with GTK 4 was not detected in system packages."
    echo "Please ensure GTK 4 and PyGObject are installed on your distribution:"
    echo "  Arch/EndeavourOS: sudo pacman -S python-gobject gtk4"
    echo "  Debian/Ubuntu:    sudo apt install python3-gi gir1.2-gtk-4.0"
    echo "  Fedora:           sudo dnf install python3-gobject gtk4"
    echo ""
fi

# 3. Install launcher binary
mkdir -p "$BIN_DIR"
cat << 'EOF' > "$BIN_DIR/vlc-lyrics"
#!/usr/bin/env bash
SCRIPT_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/vlc-lyrics"
exec "$SCRIPT_DIR/.venv/bin/python3" "$SCRIPT_DIR/lyrics_overlay.py" "$@"
EOF
chmod +x "$BIN_DIR/vlc-lyrics"

# 4. Install scalable icon
mkdir -p "$ICONS_DIR"
cp "$INSTALL_DIR/vlc-lyrics.svg" "$ICONS_DIR/vlc-lyrics.svg"
cp "$INSTALL_DIR/vlc-lyrics.svg" "$ICONS_DIR/org.vlc.lyrics.overlay.svg"

# 5. Install desktop launcher for GNOME app list
mkdir -p "$APPS_DIR"
cat << EOF > "$APPS_DIR/org.vlc.lyrics.overlay.desktop"
[Desktop Entry]
Type=Application
Name=VLC Lyrics Overlay
GenericName=Lyrics Overlay
Comment=Synchronized floating lyrics overlay for VLC
Exec=$BIN_DIR/vlc-lyrics
Icon=vlc-lyrics
Terminal=false
Categories=AudioVideo;Audio;Player;
StartupWMClass=org.vlc.lyrics.overlay
Keywords=vlc;lyrics;music;overlay;subtitles;lrc;
EOF

# 6. Update desktop and icon databases
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS_DIR" 2>/dev/null || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" 2>/dev/null || true
fi

echo "Installation complete."
echo "Launch it from the GNOME application grid or by running 'vlc-lyrics'."
