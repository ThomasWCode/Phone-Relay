#!/data/data/com.termux/files/usr/bin/bash
# Start the phone client in Termux, keeping the CPU awake while it runs.
# Stop it with Ctrl-C, or from anywhere with: touch ~/STOP_PHONE_AGENT
set -e
cd "$(dirname "$0")"
rm -f ~/STOP_PHONE_AGENT
termux-wake-lock
trap termux-wake-unlock EXIT
python phone_client.py "$@"
