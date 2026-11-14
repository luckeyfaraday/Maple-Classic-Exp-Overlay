"""Linux: MapleStory runs under Wine, Proton (Steam) or Lutris/Bottles (all Wine underneath), so its
window is an X11 window, found by its WM_CLASS (Wine names it after the program: "maplestory.exe") or
its title. Everything goes through X11 with python-xlib, so it works in an X11 session and, under
Wayland, through XWayland (where Wine's windows live unless Wine's own Wayland driver is switched on).
Positions are in X11 pixels, the units mss screenshots and tkinter use too.

When the windows are composited (a compositing window manager, or any XWayland session) each window
keeps its own picture, so the game is pictured alone and windows on top of it (this overlay's included)
don't get in the way. Without a compositor that picture would just be what's on screen, so the overlay
takes ordinary screen copies instead, like on Windows.

Wayland limits: a game window that isn't an XWayland window (Wine's experimental Wayland driver) can't be
seen at all, and on XWayland "Always on top" and hotkeys only reach other XWayland windows (the
compositor decides the rest)."""
import fcntl
import os
import platform
import re
import select
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import numpy as np

from .base import Hotkeys, Platform, parse_hotkey

try:
    from Xlib import X, Xatom
    from Xlib.display import Display
except ImportError:                     # (only python-xlib's absence: then no window can be found, said so in the UI)
    Display = None

# Programs whose windows can have "MapleStory" in the title without being the game (WM_CLASS, lowercase).
_NOT_GAME = {"firefox", "firefox-esr", "navigator", "google-chrome", "chromium", "chromium-browser", "brave-browser",
             "microsoft-edge", "vivaldi-stable", "opera", "librewolf", "discord", "vesktop", "obs", "com.obsproject.studio",
             "code", "code - oss", "vscodium", "cursor", "zed", "dev.zed.zed", "nautilus", "org.gnome.nautilus",
             "dolphin", "thunar", "nemo", "pcmanfm", "eog", "org.gnome.eog", "gwenview", "feh", "gimp",
             "gnome-terminal-server", "org.gnome.terminal", "konsole", "kitty", "alacritty", "xterm", "urxvt",
             "wezterm", "org.wezfurlong.wezterm", "tilix", "terminator", "xfce4-terminal", "ghostty",
             "com.mitchellh.ghostty", "foot", "slack", "telegramdesktop", "org.telegram.desktop", "signal",
             "steam", "steamwebhelper", "lutris", "net.lutris.lutris", "bottles", "com.usebottles.bottles",
             "heroic", "mpv", "vlc", "obsidian"}

# The overlay's Windows fonts -> fontconfig's generic families (always there, resolved to the distro's own font).
_FONTS = {"Segoe UI": ("Sans", None), "Segoe UI Semibold": ("Sans", "bold"), "Consolas": ("Monospace", None)}

_GAME_EXE = "maplestory.exe"
_MODS = {0x2: 1 << 2, 0x1: 1 << 3, 0x4: 1 << 0}       # Windows MOD_ (base.HOTKEY_MODS) -> X11 Control, Mod1 (Alt), Shift
_LOCKS = (0, 1 << 1, 1 << 4, (1 << 1) | (1 << 4))    # the same key with Caps Lock and/or Num Lock on


def keysym_of(vk):
    """X11 keysym for one of the overlay's keys (a Windows virtual key: 0-9, A-Z, F1-F12), or None."""
    if 0x30 <= vk <= 0x39:
        return vk                                       # XK_0..XK_9 have the same codes
    if 0x41 <= vk <= 0x5A:
        return vk + 0x20                                # XK_a..XK_z
    if 0x70 <= vk <= 0x7B:
        return 0xFFBE + vk - 0x70                       # XK_F1..XK_F12
    return None


def x11_mods(mods):
    """X11 modifier mask for base.HOTKEY_MODS' bits."""
    return sum(x for w, x in _MODS.items() if mods & w)


def is_game(wm_class, title, pid, own_pid):
    """Could a window with this WM_CLASS (instance, class), title and process id be MapleStory's?"""
    names = [c.lower() for c in wm_class if c]
    if pid and pid == own_pid:
        return False
    if _GAME_EXE in names:                              # Wine and Proton name the window after the program
        return True
    if any(n in _NOT_GAME for n in names):
        return False
    return "maplestory" in title.lower().replace(" ", "")


def session_type():
    """'x11', 'wayland' or 'tty' (no display at all), from the environment."""
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
        return "wayland"
    return "x11" if os.environ.get("DISPLAY") else "tty"


class _X:
    """One X connection per thread (python-xlib's aren't thread-safe): the reader's thread, tkinter's, hotkeys'."""
    _local = threading.local()

    @classmethod
    def display(cls):
        d = getattr(cls._local, "display", None)
        if d is None:
            if Display is None or not os.environ.get("DISPLAY"):
                return None
            try:
                d = Display()
            except Exception:
                return None
            cls._local.display = d
        return d


def _prop(d, win, name, kind=X.AnyPropertyType if Display else 0):
    try:
        p = win.get_full_property(d.intern_atom(name), kind)
    except Exception:
        return None
    return p.value if p else None


def _text(value):
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def window_info(d, win):
    """(wm_class, title, pid) of an X window."""
    try:
        wm_class = win.get_wm_class() or ()
    except Exception:
        wm_class = ()
    title = _text(_prop(d, win, "_NET_WM_NAME")) or _text(_prop(d, win, "WM_NAME"))
    pid = _prop(d, win, "_NET_WM_PID")
    return tuple(wm_class), title, int(pid[0]) if pid is not None and len(pid) else 0


def top_windows(d):
    """The top-level (client) windows: the window manager's list, or every child of the root without one."""
    root = d.screen().root
    ids = _prop(d, root, "_NET_CLIENT_LIST")
    if ids is not None and len(ids):
        return [d.create_resource_object("window", int(i)) for i in ids]
    try:
        found = []
        for child in root.query_tree().children:
            if _prop(d, child, "WM_STATE") is not None:
                found.append(child)
            else:                                       # (a reparenting WM's frame: the client is inside)
                found += [c for c in child.query_tree().children if _prop(d, c, "WM_STATE") is not None]
        return found or list(root.query_tree().children)
    except Exception:
        return []


def _composited(d):
    """Do windows keep their own picture (so one can be read while covered)? XWayland always does."""
    if session_type() == "wayland":
        return True
    try:
        return d.get_selection_owner(d.intern_atom(f"_NET_WM_CM_S{d.get_default_screen()}")) != X.NONE
    except Exception:
        return False


class LinuxHotkeys(Hotkeys):
    """X11 key grabs on their own thread and their own connection, taken only while MapleStory or the
    overlay is the active window (so other programs can still use those keys), like on Windows. Under
    Wayland they only work while an XWayland window (the game under Wine is one) is in front."""

    def __init__(self, on_press):
        super().__init__(on_press)
        self._wanted, self._paused, self._changed = {}, False, True
        self._lock = threading.Lock()
        self.supported = Display is not None and bool(os.environ.get("DISPLAY"))
        if self.supported:
            threading.Thread(target=self._run, daemon=True).start()

    def set(self, keys, on=True):
        with self._lock:
            self._wanted = {a: parse_hotkey(k) for a, k in keys.items() if on and parse_hotkey(k)}
            self._changed = True

    def pause(self, paused):
        with self._lock:
            self._paused, self._changed = paused, True

    def in_front(self, d):
        active = _prop(d, d.screen().root, "_NET_ACTIVE_WINDOW")
        if active is None or not len(active) or not active[0]:
            return False
        wm_class, title, pid = window_info(d, d.create_resource_object("window", int(active[0])))
        return pid == os.getpid() or is_game(wm_class, title, pid, os.getpid())

    def _grab(self, d, root, mods, vk):
        """Grabs the key with every lock combination; False if another program has it."""
        code = d.keysym_to_keycode(keysym_of(vk))
        if not code:
            return None
        failed = []
        for lock in _LOCKS:
            root.grab_key(code, x11_mods(mods) | lock, True, X.GrabModeAsync, X.GrabModeAsync,
                          onerror=lambda *a: failed.append(1))
        d.sync()
        return (code, x11_mods(mods)) if not failed else False

    def _ungrab(self, d, root, grabbed):
        for code, mask in grabbed:
            for lock in _LOCKS:
                root.ungrab_key(code, mask | lock)
        d.sync()

    def _run(self):
        try:
            d = Display()
        except Exception:
            self.supported = False
            return
        root = d.screen().root
        active, was_on = {}, None              # (keycode, mask) -> action
        while True:
            ready, _, _ = select.select([d], [], [], 0.25)
            if ready or d.pending_events():
                while d.pending_events():
                    ev = d.next_event()
                    if ev.type == X.KeyPress:
                        action = active.get((ev.detail, ev.state & 0x0D))     # Shift, Control, Mod1 only
                        if action:
                            self.on_press(action)
            try:
                on = self.in_front(d)
            except Exception:
                on = False
            with self._lock:
                changed, wanted, paused, self._changed = self._changed, dict(self._wanted), self._paused, False
            on = on and not paused
            if not changed and on == was_on:
                continue
            self._ungrab(d, root, active)
            active, was_on = {}, on
            if on:
                for action, (mods, vk) in wanted.items():
                    got = self._grab(d, root, mods, vk)
                    if got:
                        active[got] = action
                        self.problems.pop(action, None)
                    elif got is False:
                        self._ungrab(d, root, [(d.keysym_to_keycode(keysym_of(vk)), x11_mods(mods))])
                        self.problems[action] = "used by another program"


class LinuxPlatform(Platform):
    name = "Linux"

    def version(self):
        try:
            name = platform.freedesktop_os_release().get("PRETTY_NAME") or "Linux"
        except OSError:
            name = "Linux"
        return f"{name} ({platform.release()}, {session_type()})"

    def data_root(self):
        return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")

    def tesseract_candidates(self, home_dir, app_dir):
        on_path = [Path(d) / "tesseract" for d in os.environ.get("PATH", "").split(os.pathsep)
                   if d.strip() and os.path.isabs(d.strip())]
        return [home_dir / "tesseract" / "tesseract", app_dir / "tesseract" / "tesseract",
                Path("/usr/bin/tesseract"), Path("/usr/local/bin/tesseract"), *on_path]

    def tessdata_candidates(self, exe):
        share = [Path("/usr/share/tesseract-ocr/5/tessdata"), Path("/usr/share/tesseract-ocr/4.00/tessdata"),
                 Path("/usr/share/tessdata"), Path("/usr/local/share/tessdata")]
        return [exe.parent / "tessdata", exe.parent.parent / "share" / "tessdata", *share]

    def tessdata_arg(self, exe, folder):
        return str(folder)                     # (always given: a distro's Tesseract only looks where it was built for)

    def claim_single_instance(self, key, title, old_titles=()):
        folder = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        self._lock = open(Path(folder) / f"MapleEXP-{key}.lock", "w")      # unlocked when the process ends
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            pass
        if not self._raise_window(title, *old_titles):
            try:
                import tkinter as tk
                from tkinter import messagebox
                r = tk.Tk()
                r.withdraw()
                messagebox.showinfo(title, f"{title} is already running.")
                r.destroy()
            except Exception:
                print(f"{title} is already running.")
        return False

    def _raise_window(self, *titles):
        """Asks the window manager to bring a window with one of these titles to the front."""
        d = _X.display()
        if d is None:
            return False
        try:
            from Xlib.protocol import event
            root = d.screen().root
            for win in top_windows(d):
                if window_info(d, win)[1].startswith(titles):
                    atom = d.intern_atom("_NET_ACTIVE_WINDOW")
                    ev = event.ClientMessage(window=win, client_type=atom, data=(32, [2, X.CurrentTime, 0, 0, 0]))
                    root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
                    d.flush()
                    return True
        except Exception:
            pass
        return False

    def missing_permission(self):
        if Display is None:
            return "Can't see other windows: python-xlib is missing (pip install python-xlib)."
        if not os.environ.get("DISPLAY"):
            return ("Can't see other windows: no X11 display. Under Wayland the game must run through XWayland"
                    " (Wine's default), and the overlay with DISPLAY set.")
        return None

    def find_game_window(self):
        d = _X.display()
        if d is None:
            return None
        found, own = [], os.getpid()
        for win in top_windows(d):
            if is_game(*window_info(d, win), own):
                rect = self.client_rect(win.id)
                if rect:                       # not a launcher's tiny or hidden window
                    found.append((rect[2] * rect[3], win.id))
        return max(found)[1] if found else None

    def _window(self, d, wid):
        return d.create_resource_object("window", int(wid))

    def window_exists(self, wid):
        d = _X.display()
        if d is None:
            return False
        try:
            self._window(d, wid).get_attributes()
            return True
        except Exception:
            return False

    def client_rect(self, wid):
        d = _X.display()
        if d is None:
            return None
        try:
            win = self._window(d, wid)
            if win.get_attributes().map_state != X.IsViewable:          # minimized, or on another workspace
                return None
            state = _prop(d, win, "_NET_WM_STATE")
            if state is not None and d.intern_atom("_NET_WM_STATE_HIDDEN") in list(state):
                return None
            geo = win.get_geometry()
            pos = win.translate_coords(d.screen().root, 0, 0)
        except Exception:
            return None
        if geo.width < 100 or geo.height < 100:
            return None
        return -pos.x, -pos.y, geo.width, geo.height

    captures_window_alone = True

    def capture_window(self, wid, left, top, width, height):
        d = _X.display()
        if d is None or not _composited(d):
            return None                        # (not composited: covered parts would show what's on top)
        try:
            win = self._window(d, wid)
            pos = win.translate_coords(d.screen().root, left, top)
            image = win.get_image(-pos.x, -pos.y, width, height, X.ZPixmap, 0xFFFFFFFF)
        except Exception:
            return None
        if image.depth not in (24, 32) or len(image.data) != width * height * 4:
            return None                        # (another pixel layout than the usual 32-bit BGRX)
        shot = np.frombuffer(image.data, np.uint8).reshape(height, width, 4).copy()
        shot[:, :, 3] = 255
        return shot

    def own_window_rect(self, root):
        d = _X.display()
        if d is None:
            return None
        try:
            win, xroot = self._window(d, root.winfo_id()), d.screen().root
            while True:                        # up to the window manager's frame, if there is one
                parent = win.query_tree().parent
                if parent.id == xroot.id or parent.id == 0:
                    break
                win = parent
            geo = win.get_geometry()
            pos = win.translate_coords(xroot, 0, 0)
            return -pos.x, -pos.y, geo.width, geo.height
        except Exception:
            return None

    def screen_dpi(self, x, y):
        # The desktop's text scaling (Xft.dpi, set by GNOME, KDE and the like); otherwise tkinter's own guess.
        d = _X.display()
        if d is None:
            return None
        res = _text(_prop(d, d.screen().root, "RESOURCE_MANAGER", Xatom.STRING))
        m = re.search(r"^Xft\.dpi:\s*([\d.]+)", res, re.M)
        return round(float(m.group(1))) if m else None

    def create_hotkeys(self, on_press):
        return LinuxHotkeys(on_press)

    def register_fonts(self, paths):
        # Added to fontconfig for this program only (FcConfigAppFontAddFile): nothing is installed.
        # Tk draws with Xft, which asks the same fontconfig, so the families show up in tkinter.
        try:
            import ctypes
            fc = ctypes.CDLL("libfontconfig.so.1")
            fc.FcConfigAppFontAddFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            return all(fc.FcConfigAppFontAddFile(None, str(p).encode()) for p in paths)
        except Exception:
            return False

    def font(self, family, weight):
        lin_family, lin_weight = _FONTS.get(family, (family, None))
        return lin_family, lin_weight or weight

    def open_folder(self, path):
        subprocess.Popen(["xdg-open", str(path)])

    def show_file(self, path):
        # The file manager's "show this file" (Nautilus, Dolphin, Nemo...), else just its folder.
        if shutil.which("gdbus"):
            try:
                uri = Path(path).resolve().as_uri()
                done = subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.FileManager1",
                                       "--object-path", "/org/freedesktop/FileManager1",
                                       "--method", "org.freedesktop.FileManager1.ShowItems", f"['{uri}']", ""],
                                      capture_output=True, timeout=5)
                if done.returncode == 0:
                    return
            except Exception:
                pass
        subprocess.Popen(["xdg-open", str(Path(path).parent)])
