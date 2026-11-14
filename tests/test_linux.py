"""The Linux backend against a real X server: finds a stand-in game window (tests/fake_game.py, named the way
Wine names MapleStory), its rectangle, pictures of it, the overlay's own window and a hotkey press.
Needs DISPLAY (e.g. under Xvfb:  xvfb-run -a python3 tests/test_linux.py); skipped elsewhere.

It uses its own temporary data folder and never goes online."""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if not sys.platform.startswith("linux") or not os.environ.get("DISPLAY"):
    print("SKIP: needs Linux with an X display (try xvfb-run -a)")
    sys.exit(0)

os.environ["MAPLE_EXP_DATA"] = tempfile.mkdtemp()            # never touch your real data
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import cv2  # noqa: E402
import mss  # noqa: E402
import numpy as np  # noqa: E402
from Xlib import X  # noqa: E402
from Xlib.display import Display  # noqa: E402
from Xlib.ext import xtest  # noqa: E402

from platforms import create_platform, linux  # noqa: E402

failures = 0


def check(name, ok):
    global failures
    print(("PASS " if ok else "FAIL ") + name)
    failures += not ok


def wait_for(fn, seconds=8):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        got = fn()
        if got:
            return got
        time.sleep(0.1)
    return None


P = create_platform()
check("Linux gets the Linux backend", isinstance(P, linux.LinuxPlatform))
check("no game window while the game isn't running", P.find_game_window() is None)

FRAME = Path(os.environ["MAPLE_EXP_DATA"]) / "game.png"            # a picture no other window shows
frame = np.random.default_rng(7).integers(0, 256, (720, 1280, 3), np.uint8)
cv2.imwrite(str(FRAME), frame)
game = subprocess.Popen([sys.executable, str(HERE / "fake_game.py"), str(FRAME), "120", "60"])
try:
    wid = wait_for(P.find_game_window)
    check(f"the game window (WM_CLASS maplestory.exe) is found ({wid})", bool(wid))
    rect = wait_for(lambda: P.client_rect(wid)) if wid else None
    check(f"its game area is the window's inside, 1280x720 ({rect})", bool(rect) and rect[2:] == (1280, 720))
    check("it's still there", bool(wid) and P.window_exists(wid))
    if rect:
        def same_as_frame():
            with mss.mss() as sct:
                shot = np.array(sct.grab({"left": rect[0], "top": rect[1], "width": rect[2], "height": rect[3]}))[:, :, :3]
            return float(np.abs(shot.astype(int) - frame.astype(int)).mean()) < 1.0
        check("a screen copy of that rectangle is the game's picture (mss on X11)", wait_for(same_as_frame, 5))
        alone = P.capture_window(wid, *rect)
        if linux._composited(linux._X.display()):
            check("composited: the window is pictured alone, the same picture",
                  alone is not None and alone.shape == (720, 1280, 4) and np.array_equal(alone[:, :, :3], frame))
        else:
            check("no compositor: no picture of the window alone (screen copies are used instead)", alone is None)
finally:
    game.kill()
    game.wait()
check("a closed game window is gone", not wid or wait_for(lambda: not P.window_exists(wid), 5))
check("...and isn't found any more", wait_for(lambda: P.find_game_window() is None, 5))

# The overlay's own window, frame included
import tkinter as tk  # noqa: E402
root = tk.Tk()
root.geometry("300x200+400+300")
root.update()
time.sleep(0.5)
root.update()
own = P.own_window_rect(root)
inside = own and own[0] <= root.winfo_rootx() < own[0] + own[2] and own[1] <= root.winfo_rooty() < own[1] + own[3]
check(f"the overlay's own window is found on screen ({own})", bool(inside) and own[2] >= 300 and own[3] >= 200)
check(f"the screen's DPI is a number or unknown ({P.screen_dpi(10, 10)})", P.screen_dpi(10, 10) in (None,) or P.screen_dpi(10, 10) > 0)
root.destroy()

# Hotkeys: grabbed while the game (here: as if) is in front, a press arrives; not while another program is
pressed = []
hk = P.create_hotkeys(pressed.append)
check("hotkeys are offered on X11", hk.supported)
hk.in_front = lambda d: True
hk.set({"start": "Ctrl+Alt+Shift+F12"})
time.sleep(0.8)
d = Display()
codes = [d.keysym_to_keycode(k) for k in (0xFFE3, 0xFFE9, 0xFFE1, 0xFFC9)]     # Control_L, Alt_L, Shift_L, F12


def press():
    for c in codes:
        xtest.fake_input(d, X.KeyPress, c)
    for c in reversed(codes):
        xtest.fake_input(d, X.KeyRelease, c)
    d.sync()
    time.sleep(0.5)


press()
check(f"a hotkey press reaches the overlay ({pressed}, problems: {hk.problems})", pressed == ["start"])
hk.in_front = lambda d: False
time.sleep(0.6)
press()
check("hotkeys are off while another program is in front", pressed == ["start"])

print("\nALL PASSED" if not failures else f"\n{failures} FAILED")
sys.exit(1 if failures else 0)
