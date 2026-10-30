#!/bin/bash

# =======================================
# Archive Downloader
# Download single files from archive.org into the system's ROM folder
# Preset sources: archive_sources.conf
# =======================================

# =======================================================
# Root privileges check
# =======================================================
if [ "$(id -u)" -ne 0 ]; then
    exec sudo -- "$0" "$@"
fi

# =======================================================
# Variables
# =======================================================
GPTOKEYB_PID=""
CURR_TTY="/dev/tty1"
TMP_KEYS="/tmp/keys.gptk.$$"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$SCRIPT_DIR/archive_downloader/archive_dl.py"
export TERM=linux

# =======================================================
# Start gamepad input
# =======================================================
Start_GPTKeyb() {
    pkill -9 -f gptokeyb 2>/dev/null || true
    if [ -n "${GPTOKEYB_PID:-}" ]; then
        kill "$GPTOKEYB_PID" 2>/dev/null
    fi
    sleep 0.1
    /opt/inttools/gptokeyb -1 "$0" -c "$TMP_KEYS" > /dev/null 2>&1 &
    GPTOKEYB_PID=$!
}

# =======================================================
# Stop gamepad input
# =======================================================
Stop_GPTKeyb() {
    if [ -n "$GPTOKEYB_PID" ]; then
        kill "$GPTOKEYB_PID" 2>/dev/null
        GPTOKEYB_PID=""
    fi
}

# =======================================================
# Font Selection
# =======================================================
ORIGINAL_FONT=$(setfont -v 2>&1 | grep -o '/.*\.psf.*')
setfont /usr/share/consolefonts/Lat7-TerminusBold22x11.psf.gz

# =======================================================
# Display Management
# =======================================================
printf "\e[?25l" > "$CURR_TTY"
dialog --clear
Stop_GPTKeyb
pgrep -f osk.py | xargs kill -9
printf "\033[H\033[2J" > "$CURR_TTY"
printf "Archive Downloader starting..." > "$CURR_TTY"
sleep 0.5

# =======================================================
# Exit the script
# =======================================================
Exit_Menu() {
    trap - EXIT
    printf "\033[H\033[2J" > "$CURR_TTY"
    printf "\e[?25h" > "$CURR_TTY"
    Stop_GPTKeyb
    rm -f "$TMP_KEYS"
    if [[ ! -e "/dev/input/by-path/platform-odroidgo2-joypad-event-joystick" ]]; then
        [ -n "$ORIGINAL_FONT" ] && setfont "$ORIGINAL_FONT"
    fi
    if [ -n "$RESTART_ES" ]; then
        touch /tmp/es-restart
        killall emulationstation
    fi
    exit 0
}

Msg() {
    dialog --title "$1" --msgbox "$2" 10 55 > "$CURR_TTY"
}

# =======================================================
# Dependencies
# =======================================================
if ! command -v python3 >/dev/null 2>&1; then
    Msg "Error" "python3 is not installed.\n\nCannot continue."
    Exit_Menu
fi

# =======================================================
# Main menu (TUI in Python, 20 = qualcosa installato)
# =======================================================
Main_Menu() {
    python3 -I "$PY" --tui --pad > "$CURR_TTY" < "$CURR_TTY"
    [ $? -eq 20 ] && RESTART_ES=1
    Exit_Menu
}

# =======================================================
# Gamepad Setup
# =======================================================
export SDL_GAMECONTROLLERCONFIG_FILE="/opt/inttools/gamecontrollerdb.txt"
chmod 666 /dev/uinput
cp /opt/inttools/keys.gptk "$TMP_KEYS"
# B = confirm/toggle, A = back
sed -i 's/^b = .*/b = enter/; s/^a = .*/a = esc/' "$TMP_KEYS"
# X = download (d), Y = select all (a): list shortcuts in the TUI
sed -i 's/^x = .*/x = d/; s/^y = .*/y = a/' "$TMP_KEYS"
# Start = open the .zip under the cursor to pick single files inside it
sed -i 's/^start = .*/start = o/' "$TMP_KEYS"
# L2 = filter the list by name (on-screen keyboard)
sed -i 's/^l2 = .*/l2 = f/' "$TMP_KEYS"
Start_GPTKeyb

# =======================================================
# Main Execution
# =======================================================
printf "\033[H\033[2J" > "$CURR_TTY"
dialog --clear
trap 'Exit_Menu' EXIT

Main_Menu
