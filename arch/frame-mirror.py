#!/usr/bin/env python3
"""X11 phone-style bezel which follows the UxPlay video window.

Wayland intentionally prevents clients from inspecting or positioning other
clients' windows, so this helper is explicitly X11-only.  UxPlay itself remains
fully supported on Wayland.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path


def geometry(video: tuple[int, int, int, int]) -> list[tuple[int, int, int, int]]:
    x, y, width, height = video
    side, top, bottom = 18, 46, 25
    return [
        (x - side, y - top, width + side * 2, top),
        (x - side, y + height, width + side * 2, bottom),
        (x - side, y, side, height),
        (x + width, y, side, height),
    ]


def find_window(name: str) -> tuple[int, int, int, int] | None:
    try:
        result = subprocess.run(
            ["wmctrl", "-lG"], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, check=False, timeout=5,
        )
    except subprocess.TimeoutExpired:
        return None
    for line in result.stdout.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        title = parts[7]
        if title == name or name.casefold() in title.casefold():
            try:
                return tuple(map(int, parts[2:6]))  # type: ignore[return-value]
            except ValueError:
                continue
    return None


def self_test() -> int:
    assert geometry((100, 200, 300, 600)) == [
        (82, 154, 336, 46), (82, 800, 336, 25), (82, 200, 18, 600), (400, 200, 18, 600)
    ]
    print("Framed-mirror geometry self-test passed.")
    return 0


def uxplay_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # Exists but owned by another user: still alive.
    return True


def run(name: str, uxplay_pid: int | None = None) -> int:
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
        print(
            "The iPhone frame helper requires an X11 session. Wayland does not let one app inspect "
            "or position itself around another app's window. Mirroring itself is fully supported; "
            "turn off the frame or log into an X11 desktop session.", file=sys.stderr,
        )
        return 2
    if not os.environ.get("DISPLAY"):
        print("No X11 display is available.", file=sys.stderr)
        return 2
    if not shutil.which("wmctrl"):
        print("wmctrl is required: sudo pacman -S wmctrl", file=sys.stderr)
        return 2
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gdk, GLib, Gtk
    except (ImportError, ValueError) as error:
        print(f"GTK 3 / python-gobject is required: {error}", file=sys.stderr)
        return 2

    lock_path = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"pcairplay-frame-{os.getuid()}.lock"
    lock = lock_path.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0

    css = Gtk.CssProvider()
    css.load_from_data(b"window { background: #07090d; border-color: #343842; } .top { border-radius: 22px 22px 0 0; } .bottom { border-radius: 0 0 22px 22px; }")
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    windows = []
    for index in range(4):
        window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        window.set_decorated(False); window.set_keep_above(True)
        window.set_skip_taskbar_hint(True); window.set_skip_pager_hint(True)
        if index == 0: window.get_style_context().add_class("top")
        if index == 1: window.get_style_context().add_class("bottom")
        windows.append(window)

    probe_busy = []

    def apply(found: tuple[int, int, int, int] | None) -> bool:
        if found is None:
            for item in windows: item.hide()
        else:
            for item, (x, y, width, height) in zip(windows, geometry(found)):
                item.move(x, y); item.resize(width, height); item.show()
        probe_busy.clear()
        return False  # One-shot idle callback.

    def probe() -> None:
        found = find_window(name)
        GLib.idle_add(apply, found)

    def update():
        # Follow the receiver's lifetime when a PID was supplied: without
        # this the frame keeps polling forever after uxplay is gone.
        if uxplay_pid is not None and not uxplay_alive(uxplay_pid):
            Gtk.main_quit()
            return False
        # wmctrl is an external process: query it from a worker thread and
        # apply the geometry on the GTK loop, so a busy window manager can
        # never freeze the frame.  At most one probe runs at a time.
        if not probe_busy:
            probe_busy.append(True)
            threading.Thread(target=probe, daemon=True).start()
        return True

    GLib.timeout_add(250, update)
    update()
    Gtk.main()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Decorate the UxPlay video window with an iPhone-like frame")
    parser.add_argument("--name", default="PC")
    parser.add_argument("--pid", type=int, default=None, help="exit automatically when this uxplay process ends")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    return self_test() if args.self_test else run(args.name, args.pid)


if __name__ == "__main__":
    raise SystemExit(main())
