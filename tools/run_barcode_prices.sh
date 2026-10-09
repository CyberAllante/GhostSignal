#!/bin/zsh
# Daily barcode price lookups for GhostSignal (launchd: com.ajlabs.ghostsignal.barcodes)
set -a; source ~/.claude/.secrets/ghostsignal.env; set +a
cd ~/ghostsignal && /usr/bin/python3 tools/barcode_prices.py 95 >> ~/Library/Logs/ghostsignal-barcodes.log 2>&1
