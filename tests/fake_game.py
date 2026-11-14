"""A stand-in for MapleStory under Wine on Linux, for tests and demos: an X11 window named like Wine
names the game (WM_CLASS "maplestory.exe", title "MapleStory") showing a saved game screenshot.

    python3 tests/fake_game.py [frame.png] [x y]      (default: docs/screenshot-game.jpg at 0,0)

Runs until killed. Needs DISPLAY and python-xlib."""
import sys
from pathlib import Path

import cv2
import numpy as np
from Xlib import X, Xutil
from Xlib.display import Display

HERE = Path(__file__).resolve().parent


def main():
    frame = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "docs" / "screenshot-game.jpg"
    x, y = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) > 3 else (0, 0)
    img = cv2.imread(str(frame), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    bgrx = np.dstack([img, np.full((h, w), 255, np.uint8)]).tobytes()

    d = Display()
    screen = d.screen()
    win = screen.root.create_window(x, y, w, h, 0, screen.root_depth, X.InputOutput, X.CopyFromParent,
                                    background_pixel=screen.black_pixel, event_mask=X.ExposureMask)
    win.set_wm_class("maplestory.exe", "maplestory.exe")
    win.set_wm_name("MapleStory")
    win.change_property(d.intern_atom("_NET_WM_NAME"), d.intern_atom("UTF8_STRING"), 8, b"MapleStory")
    win.set_wm_normal_hints(flags=Xutil.USPosition | Xutil.USSize | Xutil.PMinSize | Xutil.PMaxSize,
                            x=x, y=y, width=w, height=h, min_width=w, min_height=h, max_width=w, max_height=h)
    gc = win.create_gc()
    win.map()
    rows = max(1, 200_000 // (w * 4))            # (put_image in strips: one X request has a size limit)
    while True:
        ev = d.next_event()
        if ev.type == X.Expose and ev.count == 0:
            for top in range(0, h, rows):
                part = bgrx[top * w * 4:min(h, top + rows) * w * 4]
                win.put_image(gc, 0, top, w, min(rows, h - top), X.ZPixmap, screen.root_depth, 0, part)
            d.flush()


if __name__ == "__main__":
    main()
