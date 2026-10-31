#!/usr/bin/env python3
"""GTK 4 desktop controller for AirPlayPC on Arch Linux."""

from __future__ import annotations

import os
import secrets
import signal
import shutil
import subprocess
import sys
import threading
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from pcairplay_common import (  # noqa: E402
    APP_ID,
    APP_NAME,
    ReceiverSettings,
    available_terminal_command,
    build_uxplay_args,
    competing_receivers,
    find_uxplay,
    inspect_uxplay,
    load_settings,
    new_log_path,
    save_settings,
    uxplay_install_hint,
)


def gui_imports():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gio, GLib, Gtk
        return Gio, GLib, Gtk
    except (ImportError, ValueError) as error:
        print(
            f"AirPlayPC GUI requires gtk4 and python-gobject ({error}).\n"
            "Install them with: sudo pacman -S gtk4 python-gobject",
            file=sys.stderr,
        )
        return None


class AirPlayWindow:
    def __init__(self, app, Gio, GLib, Gtk) -> None:
        self.Gio, self.GLib, self.Gtk = Gio, GLib, Gtk
        self.app = app
        self.settings = load_settings()
        self.process: subprocess.Popen[str] | None = None
        self.frame_process: subprocess.Popen[str] | None = None
        self.log_file = None
        self.pin_code: str | None = None

        self.window = Gtk.ApplicationWindow(application=app, title=APP_NAME)
        self.window.set_default_size(660, 650)
        self.window.connect("close-request", self.on_close)
        header = Gtk.HeaderBar()
        self.window.set_titlebar(header)
        doctor = Gtk.Button(label="Diagnostics")
        doctor.connect("clicked", self.open_diagnostics)
        header.pack_end(doctor)
        control = Gtk.Button(label="Mirror & Control")
        control.connect("clicked", self.open_control)
        header.pack_end(control)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        root.set_margin_top(18); root.set_margin_bottom(18)
        root.set_margin_start(22); root.set_margin_end(22)
        self.window.set_child(root)

        title = Gtk.Label()
        title.set_markup("<span size='x-large' weight='bold'>AirPlayPC</span>\n<span foreground='#888'>Native iPhone Screen Mirroring receiver</span>")
        title.set_xalign(0)
        root.append(title)

        grid = Gtk.Grid(column_spacing=14, row_spacing=10)
        grid.set_hexpand(True)
        root.append(grid)
        self.controls = []
        row = 0

        self.name = Gtk.Entry(text=self.settings.name)
        self.name.set_hexpand(True)
        row = self.add_row(grid, row, "Device name", self.name)

        self.resolution = Gtk.DropDown.new_from_strings(["1280x720", "1920x1080", "2560x1440", "3840x2160"])
        values = ["1280x720", "1920x1080", "2560x1440", "3840x2160"]
        self.resolution.set_selected(values.index(self.settings.resolution) if self.settings.resolution in values else 1)
        row = self.add_row(grid, row, "Resolution", self.resolution)

        self.fps = Gtk.DropDown.new_from_strings(["30", "60", "90", "120"])
        fps_values = [30, 60, 90, 120]
        self.fps.set_selected(fps_values.index(self.settings.fps) if self.settings.fps in fps_values else 1)
        row = self.add_row(grid, row, "Frame-rate cap", self.fps)

        self.port = Gtk.SpinButton.new_with_range(1024, 65533, 1)
        self.port.set_value(self.settings.port or 7000)
        row = self.add_row(grid, row, "Base port", self.port)

        switches = Gtk.Grid(column_spacing=26, row_spacing=10)
        root.append(switches)
        self.sync = self.add_switch(switches, 0, 0, "A/V sync", self.settings.sync)
        self.fullscreen = self.add_switch(switches, 1, 0, "Fullscreen", self.settings.fullscreen)
        self.pin = self.add_switch(switches, 0, 1, "Require PIN", self.settings.pin)
        self.frame = self.add_switch(switches, 1, 1, "iPhone frame (X11)", self.settings.frame)
        self.share_safe = self.add_switch(switches, 0, 2, "Share-safe video", self.settings.share_safe)
        self.no_audio = self.add_switch(switches, 1, 2, "No audio", self.settings.no_audio)
        self.software = self.add_switch(switches, 0, 3, "Software decode", self.settings.software_decode)
        self.debug = self.add_switch(switches, 1, 3, "Engine debug", self.settings.engine_debug)
        self.fullscreen.connect("notify::active", self.mutual_exclusion, self.frame)
        self.frame.connect("notify::active", self.mutual_exclusion, self.fullscreen)
        self.frame.connect("notify::active", self.frame_toggled)
        if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
            self.frame.set_active(False)
            self.frame.set_sensitive(False)
            self.frame.set_tooltip_text("The frame requires X11; normal mirroring works on Wayland.")

        status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        status_box.add_css_class("card")
        root.append(status_box)
        self.status = Gtk.Label(label="Stopped", xalign=0)
        self.status.set_margin_top(12); self.status.set_margin_start(14)
        self.pin_label = Gtk.Label(label="", xalign=0)
        self.pin_label.set_margin_bottom(12); self.pin_label.set_margin_start(14)
        status_box.append(self.status); status_box.append(self.pin_label)

        self.start_button = Gtk.Button(label="Start receiver")
        self.start_button.add_css_class("suggested-action")
        self.start_button.connect("clicked", self.toggle_receiver)
        root.append(self.start_button)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        self.log_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scroller.set_child(self.log_view)
        root.append(scroller)

    def add_row(self, grid, row, text, widget):
        label = self.Gtk.Label(label=text, xalign=0)
        grid.attach(label, 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)
        self.controls.append(widget)
        return row + 1

    def add_switch(self, grid, column, row, text, value):
        box = self.Gtk.Box(orientation=self.Gtk.Orientation.HORIZONTAL, spacing=8)
        label = self.Gtk.Label(label=text, xalign=0, hexpand=True)
        switch = self.Gtk.Switch(active=value)
        box.append(label); box.append(switch)
        grid.attach(box, column, row, 1, 1)
        self.controls.append(switch)
        return switch

    def mutual_exclusion(self, source, _param, other):
        if source.get_active() and other.get_active():
            other.set_active(False)

    def current_settings(self) -> ReceiverSettings:
        resolutions = ["1280x720", "1920x1080", "2560x1440", "3840x2160"]
        frame_rates = [30, 60, 90, 120]
        return ReceiverSettings(
            name=self.name.get_text(), resolution=resolutions[self.resolution.get_selected()],
            fps=frame_rates[self.fps.get_selected()], refresh_rate=60,
            sync=self.sync.get_active(), fullscreen=self.fullscreen.get_active(),
            pin=self.pin.get_active(), frame=self.frame.get_active(),
            share_safe=self.share_safe.get_active(), no_audio=self.no_audio.get_active(),
            software_decode=self.software.get_active(), engine_debug=self.debug.get_active(),
            port=int(self.port.get_value()),
        )

    def append_log(self, text: str):
        buffer = self.log_view.get_buffer()
        buffer.insert(buffer.get_end_iter(), text)
        # Reuse the buffer-owned insert mark instead of creating (and leaking)
        # a new TextMark per line, and keep the log bounded for long sessions.
        self.log_view.scroll_mark_onscreen(buffer.get_insert())
        line_count = buffer.get_line_count()
        if line_count > 2000:
            start = buffer.get_iter_at_line(0)
            end = buffer.get_iter_at_line(line_count - 2000)
            buffer.delete(start, end)
        return False

    def set_running(self, running: bool):
        for control in self.controls:
            if control is not self.frame:
                control.set_sensitive(not running)
        self.start_button.set_label("Stop receiver" if running else "Start receiver")
        if running:
            self.start_button.remove_css_class("suggested-action")
            self.start_button.add_css_class("destructive-action")
        else:
            self.start_button.remove_css_class("destructive-action")
            self.start_button.add_css_class("suggested-action")

    def toggle_receiver(self, _button):
        if self.process and self.process.poll() is None:
            self.stop_receiver()
        else:
            self.start_receiver()

    def start_receiver(self):
        executable = find_uxplay()
        if not executable:
            self.status.set_text(f"UxPlay is missing — {uxplay_install_hint()}")
            return
        support = inspect_uxplay(executable)
        if not support.compatible:
            self.status.set_text(f"UxPlay is incompatible: {support.reason}")
            return
        rivals = competing_receivers()
        if rivals:
            self.status.set_text(f"Close competing receiver: {', '.join(rivals)}")
            return
        settings = self.current_settings()
        self.pin_code = f"{secrets.randbelow(10000):04d}" if settings.pin else None
        try:
            args, notes = build_uxplay_args(settings, pin_code=self.pin_code)
        except ValueError as error:
            self.status.set_text(str(error))
            return
        save_settings(settings)
        if self.log_file:
            try:
                self.log_file.close()
            except (OSError, ValueError):
                pass
            self.log_file = None
        log_path = new_log_path("airplay-ui")
        self.log_file = log_path.open("w", encoding="utf-8")
        self.log_file.write("argv: " + " ".join(args) + "\n---\n")
        self.log_file.flush()
        launch = [shutil.which("stdbuf"), "-oL", "-eL", executable, *args] if shutil.which("stdbuf") else [executable, *args]
        try:
            self.process = subprocess.Popen(
                launch, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, errors="replace", start_new_session=True,
            )
        except OSError as error:
            self.status.set_text(f"Could not start UxPlay: {error}")
            self.log_file.close(); self.log_file = None
            return
        self.set_running(True)
        self.status.set_text("Starting…")
        self.pin_label.set_text(f"PIN: {self.pin_code}" if self.pin_code else "")
        self.append_log(f"Log: {log_path}\n")
        for note in notes:
            self.append_log(f"Note: {note}\n")
        threading.Thread(target=self.read_output, daemon=True).start()
        if settings.frame:
            self.start_frame()

    def read_output(self):
        process = self.process
        assert process and process.stdout
        log_file = self.log_file  # Captured locally: the UI may reset it on restart.
        for line in process.stdout:
            if process is not self.process:
                break  # A new receiver took over; drop stale status updates.
            if log_file:
                try:
                    log_file.write(line); log_file.flush()
                except (ValueError, OSError):
                    log_file = None
            lower = line.lower()
            if "initialized server socket" in lower:
                self.GLib.idle_add(self.status.set_text, "Discoverable")
            elif "raop_rtp_mirror starting" in lower or "starting mirroring" in lower:
                self.GLib.idle_add(self.status.set_text, "Mirroring")
            elif "connection" in lower and ("accepted" in lower or "request" in lower):
                self.GLib.idle_add(self.status.set_text, "iPhone connected")
            self.GLib.idle_add(self.append_log, line)
        code = process.wait()
        self.GLib.idle_add(self.receiver_exited, process, code)

    def receiver_exited(self, process, code: int):
        # Ignore stale exits so a late notification from a previous receiver
        # cannot tear down the log or state of a freshly started one.
        if process is not self.process:
            return False
        if self.log_file:
            self.log_file.close(); self.log_file = None
        self.process = None
        self.stop_frame()
        self.set_running(False)
        self.pin_label.set_text("")
        self.status.set_text("Stopped" if code in (0, -signal.SIGTERM) else f"Receiver exited with status {code}")
        return False

    def stop_receiver(self):
        process = self.process
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            # Escalate to SIGKILL so the UI never stays on "Stopping…" if
            # UxPlay hangs and keeps ignoring SIGTERM.
            threading.Timer(5.0, self._force_kill_receiver, args=(process,)).start()
        self.status.set_text("Stopping…")
        self.stop_frame()

    def _force_kill_receiver(self, process):
        try:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def frame_toggled(self, switch, _param):
        if self.process and self.process.poll() is None:
            if switch.get_active(): self.start_frame()
            else: self.stop_frame()

    def start_frame(self):
        if self.frame_process and self.frame_process.poll() is None:
            return
        script = SCRIPT_DIR / "frame-mirror.py"
        command = [sys.executable, str(script), "--name", self.name.get_text()]
        if self.process:
            command += ["--pid", str(self.process.pid)]
        try:
            self.frame_process = subprocess.Popen(command)
        except OSError as error:
            self.status.set_text(f"Could not start the framed mirror: {error}")

    def stop_frame(self):
        if self.frame_process and self.frame_process.poll() is None:
            self.frame_process.terminate()
            try:
                self.frame_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.frame_process.kill()
                try:
                    self.frame_process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass  # Reaped later by subprocess machinery.
        self.frame_process = None

    def open_diagnostics(self, _button):
        command = available_terminal_command([sys.executable, str(SCRIPT_DIR / "pcairplay.py"), "doctor"])
        if command:
            subprocess.Popen(command)
        else:
            self.status.set_text("No terminal emulator found; run: pcairplay doctor")

    def open_control(self, _button):
        script = SCRIPT_DIR / "assistive-control.py"
        try:
            subprocess.Popen([sys.executable, str(script), "--start"])
        except OSError as error:
            self.status.set_text(f"Could not open AssistiveTouch control: {error}")

    def on_close(self, _window):
        try:
            save_settings(self.current_settings())
        except Exception as error:
            # Never lose the user's configuration silently.
            self.append_log(f"Warning: could not save settings: {error}\n")
            print(f"Could not save settings: {error}", file=sys.stderr)
        self.stop_receiver()
        return False


def self_test() -> int:
    settings = ReceiverSettings(name="Arch Demo", frame=True, port=7000)
    args, _ = build_uxplay_args(settings, pin_code="1234", probe_plugins=False)
    assert args[:2] == ["-n", "Arch Demo"]
    assert args[args.index("-pin"):args.index("-pin") + 3] == ["-pin", "1234", "-reg"]
    assert settings.frame and "-p" in args
    print("GTK controller self-test passed (no display required).")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    imported = gui_imports()
    if not imported:
        return 1
    Gio, GLib, Gtk = imported
    app = Gtk.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
    holder = {}

    def activate(application):
        if "window" not in holder:
            holder["window"] = AirPlayWindow(application, Gio, GLib, Gtk)
        holder["window"].window.present()

    app.connect("activate", activate)
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
