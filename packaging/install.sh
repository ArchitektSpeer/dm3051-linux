#!/bin/sh
# Install DM3051 Linux as a desktop application for the current user (no root needed).
#   sh packaging/install.sh [--desktop] [WHEEL | SOURCE_DIR]   install or upgrade; --desktop adds a desktop icon
#   sh packaging/install.sh --uninstall                        remove everything this script installed
# USB access still needs the udev rule once (see docs/INSTALL.md); this script never uses sudo.
set -eu

NAME="DM3051 Linux"
COMMENT="RIGOL DM3051 multimeter: live readings, trend plot, statistics, CSV logging"

here=$(cd "$(dirname "$0")" && pwd)
data=${XDG_DATA_HOME:-$HOME/.local/share}
home=$data/dm3051-linux
apps=$data/applications
icons=$data/icons/hicolor/scalable/apps
desktop=$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")
source=$here/..
on_desktop=0

for arg in "$@"; do
    case $arg in
        --desktop) on_desktop=1 ;;
        --uninstall)
            rm -rf "$home"
            rm -f "$apps/dm3051.desktop" "$icons/dm3051.svg" "$desktop/dm3051.desktop"
            command -v update-desktop-database >/dev/null && update-desktop-database "$apps" 2>/dev/null || true
            echo "Removed $NAME."
            exit 0 ;;
        -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
        -*) echo "Unknown option $arg" >&2; exit 2 ;;
        *) source=$arg ;;
    esac
done

python3 -c 'import tkinter' 2>/dev/null || echo "Warning: Tk is missing (Arch: sudo pacman -S tk); only the CLI will work." >&2
python3 -m venv "$home/venv"
"$home/venv/bin/python" -m pip install --quiet --upgrade pip
"$home/venv/bin/python" -m pip install --quiet --upgrade --force-reinstall --no-cache-dir "$source"

mkdir -p "$apps" "$icons"
install -m 644 "$here/dm3051.svg" "$icons/dm3051.svg"
cat > "$apps/dm3051.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$NAME
Comment=$COMMENT
Exec=$home/venv/bin/dm3051
Icon=dm3051
Terminal=false
Categories=Science;Electronics;
Keywords=multimeter;DMM;RIGOL;DM3051;
StartupWMClass=Dm3051
EOF
chmod 755 "$apps/dm3051.desktop"
if [ "$on_desktop" = 1 ] && [ -d "$desktop" ]; then
    install -m 755 "$apps/dm3051.desktop" "$desktop/dm3051.desktop"
    command -v gio >/dev/null && gio set "$desktop/dm3051.desktop" metadata::trusted true 2>/dev/null || true
fi
command -v update-desktop-database >/dev/null && update-desktop-database "$apps" 2>/dev/null || true
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true

echo "Installed $("$home/venv/bin/python" -c 'import dm3051; print(dm3051.__version__)') to $home"
echo "Start it from the application menu, with $home/venv/bin/dm3051, or --demo to try it without a meter."
