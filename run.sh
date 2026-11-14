#!/bin/sh
# Runs the overlay from source on Linux (or macOS). First, once:
#     python3 -m pip install -r requirements.txt     (Linux also needs python3-tk and tesseract-ocr from your distro)
cd "$(dirname "$0")" || exit 1
for py in .venv/bin/python3 python3 python; do
    if command -v "$py" >/dev/null 2>&1 && "$py" -c "import mss, cv2, numpy, PIL, tkinter" >/dev/null 2>&1; then
        exec "$py" maple_exp.py "$@"
    fi
done
echo "Couldn't find Python with the packages this needs. Install Python 3.10+ with tkinter, then run this once:"
echo "    python3 -m pip install -r requirements.txt"
exit 1
