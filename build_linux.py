"""Builds the Linux download: release/Maple-Classic-Exp-Overlay-v<version>-Linux-<arch>.tar.gz

It packs the program, Python and everything it needs (Tk included) into one folder with the program
"Maple-Classic-Exp-Overlay" in it, and puts that folder in a .tar.gz. Tesseract OCR (for map names) is not
packed: every distro has it (sudo apt install tesseract-ocr, sudo dnf install tesseract, sudo pacman -S
tesseract tesseract-data-eng), and the overlay finds it in /usr/bin.

    python3 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements-build-linux.txt
    .venv/bin/python build_linux.py              builds the folder and the .tar.gz
    .venv/bin/python build_linux.py check        starts the built program for a few seconds (needs DISPLAY,
                                                 e.g. xvfb-run -a) and checks it's still running

Python needs tkinter (Debian/Ubuntu: python3-tk). The program is built for the CPU it's built on (x86_64 on
GitHub) and runs on distros with the same or a newer glibc than the build machine's.
"""
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "Maple Classic Exp Overlay"                 # the folder
FILE_NAME = NAME.replace(" ", "-")                 # the program and the download (no spaces: easier in a terminal)
VERSION = re.search(r'^VERSION = "([^"]+)"', (ROOT / "maple_exp.py").read_text(encoding="utf-8"), re.M)[1]
ARCH = platform.machine()
BUILD, RELEASE = ROOT / "build" / "linux", ROOT / "release"
APP = BUILD / "dist" / NAME

# Never used, left out to make the download smaller: Pillow's AVIF support (only PNGs are made).
UNUSED = ["_internal/PIL/_avif*.so"]

READ_ME_FIRST = f"""{NAME} v{VERSION}  (beta, Linux)

1. Install Tesseract OCR once, for map names (EXP and mesos work without it):
     Debian/Ubuntu: sudo apt install tesseract-ocr
     Fedora:        sudo dnf install tesseract
     Arch:          sudo pacman -S tesseract tesseract-data-eng
2. Start it:  ./{FILE_NAME}   (or double-click it in your file manager)
3. Start MapleStory (Wine, Proton, Lutris or Bottles) and press Start in the overlay.

It needs an X11 session, or Wayland with the game running through XWayland (Wine's default).
Keep this whole folder together: the program needs the files next to it.
The full guide is on the GitHub page you downloaded this from.
"""


def main():
    shutil.rmtree(BUILD, ignore_errors=True)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
                    "--onedir", "--name", FILE_NAME, "--add-data", f"{ROOT / 'assets'}:assets",
                    "--collect-submodules", "Xlib",          # (python-xlib loads its parts by name at run time)
                    "--hidden-import", "PIL._tkinter_finder",  # (Pillow's link to Tk, which Windows' build finds itself)
                    "--distpath", str(BUILD / "dist"), "--workpath", str(BUILD / "work"),
                    "--specpath", str(BUILD), str(ROOT / "maple_exp.py")], check=True)
    if APP.exists():
        shutil.rmtree(APP)
    (BUILD / "dist" / FILE_NAME).rename(APP)
    for pattern in UNUSED:
        for f in APP.glob(pattern):
            print(f"Leaving out {f.relative_to(APP)} ({f.stat().st_size / 1e6:.0f} MB, not used)")
            f.unlink()
    (APP / "READ ME FIRST.txt").write_text(READ_ME_FIRST, encoding="utf-8")

    RELEASE.mkdir(exist_ok=True)
    out = RELEASE / f"{FILE_NAME}-v{VERSION}-Linux-{ARCH}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(APP, arcname=NAME, filter=lambda t: t.replace(uid=0, gid=0, uname="", gname="", deep=False))
    print(f"\nDone: {out} ({out.stat().st_size / 1e6:.0f} MB)")


def check(seconds=8):
    """Starts the built program on its own temp data folder; it must still be running after a few seconds."""
    program = APP / FILE_NAME
    if not program.is_file():
        sys.exit(f"Not built yet: {program}")
    env = dict(os.environ, MAPLE_EXP_DATA=tempfile.mkdtemp())       # (never the real data; nothing is sent)
    proc = subprocess.Popen([str(program)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(seconds)
    alive = proc.poll() is None
    proc.terminate()
    try:
        out = proc.communicate(timeout=10)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        out = proc.communicate()[0]
    errors = Path(env["MAPLE_EXP_DATA"]) / "errors.log"
    if not alive or errors.exists():
        print(out.decode(errors="replace"))
        if errors.exists():
            print(errors.read_text(encoding="utf-8", errors="replace"))
        sys.exit("FAIL: the built program didn't keep running cleanly")
    print(f"PASS: the built program ran for {seconds} s with no errors")


if __name__ == "__main__":
    if sys.argv[1:] == ["check"]:
        check()
    else:
        main()
