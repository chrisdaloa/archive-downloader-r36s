#!/bin/sh
# Install Archive Downloader: downloads the launcher and the program from the main branch.
# Usage: install.sh [tools-folder]     (default: /opt/system/Tools, on the console)
# Your archive_sources.conf and archive_auth.conf are never touched.
set -e

BASE="https://raw.githubusercontent.com/chrisdaloa/archive-downloader-r36s/main/opt/system/Tools"
TOOLS="${1:-/opt/system/Tools}"
DIR="$TOOLS/archive_downloader"

fetch() {  # fetch <path in the repo, url-encoded> <destination>
    curl -fsSL -o "$2.new" "$BASE/$1" && mv -f "$2.new" "$2"
}

mkdir -p "$DIR"
fetch "Archive%20Downloader.sh" "$TOOLS/Archive Downloader.sh"
for f in archive_dl.py archive_sources.example.conf archive_auth.example.conf; do
    fetch "archive_downloader/$f" "$DIR/$f"
done
chmod 777 "$TOOLS/Archive Downloader.sh" "$DIR"/* 2>/dev/null || true
python3 -I -m py_compile "$DIR/archive_dl.py" && rm -rf "$DIR/__pycache__"

echo "Installed in $TOOLS. Restart EmulationStation: Tools -> Archive Downloader."
