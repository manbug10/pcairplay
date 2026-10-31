#!/usr/bin/env python3
"""Bluetooth AssistiveTouch mouse/keyboard for AirPlayPC.

The iPhone sees this process as a standard BLE HID peripheral named
"AirPlayPC Input".  It is deliberately separate from AirPlay: UxPlay supplies
the picture while iOS AssistiveTouch consumes the input reports.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import shutil
import socket
import subprocess
import sys
import threading
import struct
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
VENDOR_DIR = SCRIPT_DIR / "vendor"
if VENDOR_DIR.is_dir():
    sys.path.insert(0, str(VENDOR_DIR))

from pcairplay_common import (  # noqa: E402
    ReceiverSettings,
    build_uxplay_args,
    competing_receivers,
    find_uxplay,
    inspect_uxplay,
    load_settings,
    new_log_path,
    uxplay_install_hint,
)

APP_ID = "io.github.gbulog.pcairplay.control"
DEVICE_NAME = "AirPlayPC Input"
# An empty XDG_RUNTIME_DIR must be treated as unset (XDG spec).
RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
SOCKET_PATH = Path(RUNTIME_DIR) / f"pcairplay-control-{os.getuid()}.sock"
EMBEDDED_RESOLUTION = "1920x1080"
PAIRING_AGENT_PATH = "/io/github/gbulog/pcairplay/agent"
VERTICAL_SCROLL_GAIN = 0.35
HORIZONTAL_SCROLL_GAIN = 1.25
MAX_SCROLL_DELTA = 8.0
LOGGER = logging.getLogger(__name__)

# Composite BLE HID report descriptor: relative mouse with two-axis scrolling,
# boot-compatible keyboard, consumer controls, and an absolute screen pointer.
REPORT_MAP = bytes([
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, 0x01,
    0x09, 0x01, 0xA1, 0x00, 0x05, 0x09, 0x19, 0x01,
    0x29, 0x08, 0x15, 0x00, 0x25, 0x01, 0x95, 0x08,
    0x75, 0x01, 0x81, 0x02, 0x05, 0x01, 0x09, 0x30, 0x09, 0x31,
    0x15, 0x81, 0x25, 0x7F, 0x75, 0x08,
    0x95, 0x02, 0x81, 0x06, 0x09, 0x38, 0x95, 0x01,
    0x81, 0x06, 0x05, 0x0C, 0x0A, 0x38, 0x02, 0x95,
    0x01, 0x81, 0x06, 0xC0, 0xC0,
    # Report 4: absolute pointer for directly controlling the embedded video.
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, 0x04,
    0x09, 0x01, 0xA1, 0x00, 0x05, 0x09, 0x19, 0x01,
    0x29, 0x08, 0x15, 0x00, 0x25, 0x01, 0x95, 0x08,
    0x75, 0x01, 0x81, 0x02, 0x05, 0x01, 0x09, 0x30, 0x09, 0x31,
    0x16, 0x00, 0x00, 0x26, 0xFF, 0x7F, 0x75, 0x10,
    0x95, 0x02, 0x81, 0x02, 0xC0, 0xC0,
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x02,
    0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00,
    0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
    0x95, 0x01, 0x75, 0x08, 0x81, 0x01, 0x95, 0x06,
    0x75, 0x08, 0x15, 0x00, 0x25, 0x65, 0x05, 0x07,
    0x19, 0x00, 0x29, 0x65, 0x81, 0x00, 0xC0,
    # Report 3: Consumer Control for Home, Search, and App Switcher.
    0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x03,
    0x15, 0x00, 0x26, 0xA0, 0x02, 0x19, 0x00, 0x2A,
    0xA0, 0x02, 0x75, 0x10, 0x95, 0x01, 0x81, 0x00,
    0xC0,
])

# USB HID keyboard usage codes. Modifiers use the first report byte:
# Ctrl=1, Shift=2, Alt/Option=4, GUI/Command=8.
KEY_CODES = {
    **{chr(ord("a") + index): 0x04 + index for index in range(26)},
    **{str((index + 1) % 10): 0x1E + index for index in range(10)},
    "\n": 0x28, "\r": 0x28, "\b": 0x2A, "\t": 0x2B, " ": 0x2C,
    "-": 0x2D, "=": 0x2E, "[": 0x2F, "]": 0x30, "\\": 0x31,
    ";": 0x33, "'": 0x34, "`": 0x35, ",": 0x36, ".": 0x37, "/": 0x38,
}
SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6",
    "&": "7", "*": "8", "(": "9", ")": "0", "_": "-", "+": "=",
    "{": "[", "}": "]", "|": "\\", ":": ";", '"': "'", "~": "`",
    "<": ",", ">": ".", "?": "/",
}


def encode_character(character: str) -> tuple[int, int] | None:
    if len(character) != 1:
        return None
    # Full Keyboard Access reserves an unmodified Space for "Activate".
    # Shift-Space still inserts a normal space in iOS text fields.
    if character == " ":
        return 0x02, KEY_CODES[" "]
    if "A" <= character <= "Z":
        return 0x02, KEY_CODES[character.lower()]
    if character in SHIFTED:
        return 0x02, KEY_CODES[SHIFTED[character]]
    code = KEY_CODES.get(character)
    return (0, code) if code is not None else None


def signed_byte(value: int | float) -> int:
    return max(-127, min(127, round(value))) & 0xFF


def build_mouse_report(
    buttons: int, dx: int | float = 0, dy: int | float = 0,
    wheel: int | float = 0, horizontal: int | float = 0,
) -> bytes:
    return bytes((
        buttons & 0xFF, signed_byte(dx), signed_byte(dy),
        signed_byte(wheel), signed_byte(horizontal),
    ))


def build_keyboard_report(modifier: int = 0, keys: list[int] | None = None) -> bytes:
    selected = (keys or [])[:6]
    return bytes((modifier & 0xFF, 0, *selected, *([0] * (6 - len(selected)))))


def build_absolute_report(buttons: int, x: int | float, y: int | float) -> bytes:
    x_value = max(0, min(32767, round(x)))
    y_value = max(0, min(32767, round(y)))
    return struct.pack("<BHH", buttons & 0xFF, x_value, y_value)


def build_consumer_report(usage: int = 0) -> bytes:
    return struct.pack("<H", max(0, min(0x2A0, usage)))


class ScrollAccumulator:
    """Turn high-resolution touchpad motion into stable HID wheel steps."""

    def __init__(self) -> None:
        self.pending_vertical = 0.0
        self.pending_horizontal = 0.0

    def consume(self, dx: float, dy: float) -> tuple[int, int]:
        dx = max(-MAX_SCROLL_DELTA, min(MAX_SCROLL_DELTA, float(dx)))
        dy = max(-MAX_SCROLL_DELTA, min(MAX_SCROLL_DELTA, float(dy)))
        if abs(dx) > abs(dy) * 1.25:
            dy = 0.0
        elif abs(dy) > abs(dx) * 1.25:
            dx = 0.0
        self.pending_vertical += -dy * VERTICAL_SCROLL_GAIN
        self.pending_horizontal += -dx * HORIZONTAL_SCROLL_GAIN
        vertical = int(self.pending_vertical)
        horizontal = int(self.pending_horizontal)
        self.pending_vertical -= vertical
        self.pending_horizontal -= horizontal
        return vertical, horizontal


def control_status_text(status: dict[str, Any]) -> str:
    subscriptions = status.get("subscriptions")
    if not isinstance(subscriptions, dict) or not any(subscriptions.values()):
        return f"Ready to pair as {DEVICE_NAME}"
    missing = [
        name
        for name in ("relative", "keyboard", "absolute", "consumer")
        if not subscriptions.get(name)
    ]
    if missing:
        return (
            f"Connected, but {', '.join(missing)} control needs {DEVICE_NAME} "
            "forgotten and paired again"
        )
    return f"Connected — control reports are reaching {DEVICE_NAME}"


def build_embedded_receiver_args(settings, rtp_port: int, *, probe_plugins: bool = True):
    """Build a single-window UxPlay command which forwards H.264 to GTK.

    H.265 is deliberately not advertised in this mode: RTP carries no codec
    metadata, so a fixed local receiver cannot safely switch depayloaders.
    1080p H.264 keeps the embedded path deterministic and broadly decodable.
    """
    embedded = replace(
        settings,
        resolution=EMBEDDED_RESOLUTION,
        fullscreen=False,
        pin=False,
        frame=False,
        share_safe=False,
    )
    args, notes = build_uxplay_args(embedded, probe_plugins=probe_plugins)
    args = [item for item in args if item != "-h265"]
    pipeline = (
        f"config-interval=1 pt=96 ! udpsink host=127.0.0.1 port={rtp_port} "
        "sync=false async=false"
    )
    args.extend(["-vrtp", pipeline])
    notes.append("Embedded view uses H.264 at 1080p for deterministic local RTP decoding.")
    return args, notes


class HidState:
    def __init__(
        self, mouse_characteristic: Any, keyboard_characteristic: Any,
        absolute_characteristic: Any = None, consumer_characteristic: Any = None,
    ) -> None:
        self.mouse = mouse_characteristic
        self.keyboard = keyboard_characteristic
        self.absolute = absolute_characteristic
        self.consumer = consumer_characteristic
        self.buttons = 0
        self.absolute_position = (16384, 16384)

    @staticmethod
    def _subscribed(characteristic: Any) -> bool:
        return bool(characteristic and getattr(characteristic, "_notify", False))

    @property
    def subscribed(self) -> bool:
        return any(self.subscriptions.values())

    @property
    def subscriptions(self) -> dict[str, bool]:
        return {
            "relative": self._subscribed(self.mouse),
            "keyboard": self._subscribed(self.keyboard),
            "absolute": self._subscribed(self.absolute),
            "consumer": self._subscribed(self.consumer),
        }

    async def mouse_move(self, dx: float, dy: float, wheel: float = 0, horizontal: float = 0) -> None:
        if not self._subscribed(self.mouse):
            return
        # Preserve the requested movement when a fast drag exceeds one signed
        # HID report rather than silently clipping it.
        remaining_x, remaining_y = round(dx), round(dy)
        remaining_wheel, remaining_horizontal = round(wheel), round(horizontal)
        while remaining_x or remaining_y or remaining_wheel or remaining_horizontal:
            part_x = max(-127, min(127, remaining_x))
            part_y = max(-127, min(127, remaining_y))
            part_wheel = max(-127, min(127, remaining_wheel))
            part_horizontal = max(-127, min(127, remaining_horizontal))
            self.mouse.changed(build_mouse_report(self.buttons, part_x, part_y, part_wheel, part_horizontal))
            remaining_x -= part_x; remaining_y -= part_y
            remaining_wheel -= part_wheel; remaining_horizontal -= part_horizontal
            await asyncio.sleep(0.004)

    def button(self, button: int, pressed: bool, absolute: bool = False) -> bool:
        target = self.absolute if absolute else self.mouse
        if not self._subscribed(target):
            return False
        mask = 1 << max(0, min(7, button - 1))
        self.buttons = self.buttons | mask if pressed else self.buttons & ~mask
        if absolute and self.absolute:
            self.absolute.changed(build_absolute_report(self.buttons, *self.absolute_position))
        else:
            self.mouse.changed(build_mouse_report(self.buttons))
        return True

    async def click_button(self, button: int, *, absolute: bool = True) -> None:
        if not self.button(button, True, absolute=absolute):
            return
        await asyncio.sleep(0.180)
        self.button(button, False, absolute=absolute)

    def absolute_move(self, x: float, y: float) -> None:
        if not self._subscribed(self.absolute):
            return
        self.absolute_position = (
            max(0, min(32767, round(x))),
            max(0, min(32767, round(y))),
        )
        self.absolute.changed(build_absolute_report(self.buttons, *self.absolute_position))

    async def key(self, modifier: int, code: int) -> None:
        if not self._subscribed(self.keyboard):
            return
        # iOS is stricter than desktop hosts about key dwell time and modifier
        # ordering. Model a real keyboard instead of emitting one very short
        # combined report; this is required for Space and Command shortcuts.
        self.keyboard.changed(build_keyboard_report())
        await asyncio.sleep(0.025)
        if modifier:
            self.keyboard.changed(build_keyboard_report(modifier))
            await asyncio.sleep(0.035)
        self.keyboard.changed(build_keyboard_report(modifier, [code]))
        await asyncio.sleep(0.180 if code == KEY_CODES[" "] else 0.110)
        if modifier:
            self.keyboard.changed(build_keyboard_report(modifier))
            await asyncio.sleep(0.035)
        self.keyboard.changed(build_keyboard_report())

    async def text(self, value: str) -> None:
        for character in value:
            encoded = encode_character(character)
            if encoded:
                await self.key(*encoded)
                await asyncio.sleep(0.018)

    async def consumer_key(self, usage: int) -> None:
        if not self._subscribed(self.consumer):
            return
        self.consumer.changed(build_consumer_report(usage))
        await asyncio.sleep(0.100)
        self.consumer.changed(build_consumer_report())


def create_services():
    try:
        from bluez_peripheral.gatt import (
            CharacteristicFlags as CharFlags,
            DescriptorFlags as DescFlags,
            Service,
            ServiceCollection,
            characteristic,
            descriptor,
        )
    except ImportError as error:
        raise RuntimeError(f"bluez-peripheral is missing; {uxplay_install_hint()}") from error

    class HidService(Service):
        def __init__(self) -> None:
            self.protocol = b"\x01"
            super().__init__("1812", True)

        @characteristic("2A4A", CharFlags.READ)
        def hid_information(self, _options):
            return b"\x11\x01\x00\x02"

        @characteristic("2A4B", CharFlags.READ | CharFlags.ENCRYPT_READ)
        def report_map(self, _options):
            return REPORT_MAP

        @characteristic("2A4C", CharFlags.WRITE_WITHOUT_RESPONSE | CharFlags.ENCRYPT_WRITE)
        def control_point(self, _options):
            return b""

        @control_point.setter
        def control_point(self, _value, _options):
            return None

        @characteristic("2A4E", CharFlags.READ | CharFlags.WRITE_WITHOUT_RESPONSE | CharFlags.ENCRYPT_READ | CharFlags.ENCRYPT_WRITE)
        def protocol_mode(self, _options):
            return self.protocol

        @protocol_mode.setter
        def protocol_mode(self, value, _options):
            if value:
                self.protocol = bytes(value[:1])

        @characteristic("2A4D", CharFlags.READ | CharFlags.NOTIFY | CharFlags.ENCRYPT_READ)
        def mouse_input(self, _options):
            return build_mouse_report(0)

        @descriptor("2908", mouse_input, DescFlags.READ | DescFlags.ENCRYPT_READ)
        def mouse_reference(self, _options):
            return b"\x01\x01"

        @characteristic("2A4D", CharFlags.READ | CharFlags.NOTIFY | CharFlags.ENCRYPT_READ)
        def keyboard_input(self, _options):
            return build_keyboard_report()

        @descriptor("2908", keyboard_input, DescFlags.READ | DescFlags.ENCRYPT_READ)
        def keyboard_reference(self, _options):
            return b"\x02\x01"

        @characteristic("2A4D", CharFlags.READ | CharFlags.NOTIFY | CharFlags.ENCRYPT_READ)
        def absolute_input(self, _options):
            return build_absolute_report(0, 16384, 16384)

        @descriptor("2908", absolute_input, DescFlags.READ | DescFlags.ENCRYPT_READ)
        def absolute_reference(self, _options):
            return b"\x04\x01"

        @characteristic("2A4D", CharFlags.READ | CharFlags.NOTIFY | CharFlags.ENCRYPT_READ)
        def consumer_input(self, _options):
            return build_consumer_report()

        @descriptor("2908", consumer_input, DescFlags.READ | DescFlags.ENCRYPT_READ)
        def consumer_reference(self, _options):
            return b"\x03\x01"

    class BatteryService(Service):
        def __init__(self) -> None:
            super().__init__("180F", True)

        @characteristic("2A19", CharFlags.READ)
        def battery_level(self, _options):
            return b"\x64"

    class DeviceInformationService(Service):
        def __init__(self) -> None:
            super().__init__("180A", True)

        @characteristic("2A29", CharFlags.READ)
        def manufacturer(self, _options):
            return b"AirPlayPC"

        @characteristic("2A50", CharFlags.READ)
        def pnp_id(self, _options):
            return b"\x02\x6B\x1D\x01\x00\x01\x00"

    hid = HidService()
    return ServiceCollection([hid, BatteryService(), DeviceInformationService()]), hid


async def handle_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    state: HidState, stop: asyncio.Event,
) -> None:
    # Typing is intentionally slow (~0.15 s per key), so text jobs run as
    # background tasks chained one after another. This keeps key order stable
    # while pointer moves and clicks on the same connection stay responsive
    # instead of being blocked behind a long paste.
    pending_text: asyncio.Task | None = None
    try:
        while line := await reader.readline():
            try:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("Control message must be a JSON object.")
                kind = message.get("type")
                if kind == "move":
                    await state.mouse_move(message.get("dx", 0), message.get("dy", 0))
                elif kind == "scroll":
                    await state.mouse_move(
                        0, 0, message.get("amount", 0),
                        message.get("horizontal", 0),
                    )
                elif kind == "button":
                    state.button(
                        int(message.get("button", 1)), bool(message.get("pressed")),
                        bool(message.get("absolute", False)),
                    )
                elif kind == "absolute":
                    state.absolute_move(float(message.get("x", 0)), float(message.get("y", 0)))
                elif kind == "key":
                    await state.key(int(message.get("modifier", 0)), int(message["code"]))
                elif kind == "text":
                    value = str(message.get("value", ""))[:2_000]

                    def _queue_text(previous: asyncio.Task | None, value: str = value) -> Any:
                        async def runner() -> None:
                            if previous is not None:
                                try:
                                    await previous
                                except asyncio.CancelledError:
                                    return
                                except Exception:
                                    LOGGER.exception("Previous text job failed")
                            await state.text(value)
                        return runner()

                    previous = pending_text if (pending_text and not pending_text.done()) else None
                    pending_text = asyncio.create_task(_queue_text(previous))
                elif kind == "consumer":
                    await state.consumer_key(int(message.get("usage", 0)))
                elif kind == "pointer_button":
                    await state.click_button(
                        int(message.get("button", 1)),
                        absolute=bool(message.get("absolute", True)),
                    )
                elif kind == "status":
                    response = {
                        "ready": True, "subscribed": state.subscribed,
                        "subscriptions": state.subscriptions, "buttons": state.buttons,
                    }
                    writer.write((json.dumps(response) + "\n").encode())
                    await writer.drain()
                elif kind == "shutdown":
                    stop.set()
                    writer.write(b'{"stopping":true}\n')
                    await writer.drain()
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                writer.write((json.dumps({"error": str(error)}) + "\n").encode())
                await writer.drain()
    except (ConnectionError, OSError) as error:
        LOGGER.debug("Local control client disconnected: %s", error)
    finally:
        if pending_text and not pending_text.done():
            pending_text.cancel()
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


def prepare_socket_path() -> None:
    """Remove a stale control socket without hijacking a live server.

    Blindly unlinking the socket would let a second instance steal the
    endpoint from a running server, leaving the old one orphaned while the
    new one receives the shutdown signal.
    """
    try:
        SOCKET_PATH.stat()
    except FileNotFoundError:
        return
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.25)
    try:
        probe.connect(str(SOCKET_PATH))
    except OSError:
        # Nobody is accepting connections: the socket file is stale.
        SOCKET_PATH.unlink(missing_ok=True)
    else:
        raise RuntimeError(
            f"Another AirPlayPC control server is already listening on {SOCKET_PATH}"
        )
    finally:
        probe.close()


async def serve() -> int:
    try:
        from bluez_peripheral.adapter import Adapter
        from bluez_peripheral.advert import Advertisement
        from bluez_peripheral.agent import NoIoAgent
        from bluez_peripheral.util import get_message_bus
    except ImportError as error:
        print(f"ERROR: Bluetooth support dependency is missing: {error}. {uxplay_install_hint().capitalize()}.", flush=True)
        return 2

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
            installed_signals.append(signum)
        except (NotImplementedError, RuntimeError):
            LOGGER.warning("Could not install handler for %s", signum.name)

    bus = adapter = agent = collection = advert = server = None
    advertisement: dict[str, Any] = {"current": None}
    agent_registered = False
    pairing_guard = None
    old_pairable = old_discoverable = None
    exit_code = 0
    try:
        bus = await get_message_bus()
        agent = NoIoAgent()
        # bluez-peripheral 0.2.0a5 forwards a null path when none is given.
        await agent.register(bus, path=PAIRING_AGENT_PATH)
        agent_registered = True
        adapter = await Adapter.get_first(bus)
        if not await adapter.get_powered():
            raise RuntimeError("Bluetooth is powered off; turn it on before starting control mode")
        old_pairable = await adapter.get_pairable()
        old_discoverable = await adapter.get_discoverable()
        await adapter.set_pairable(True)
        await adapter.set_discoverable(True)
        collection, hid = create_services()
        await collection.register(bus, adapter=adapter)
        advert = Advertisement(DEVICE_NAME, ["1812"], appearance=0x03C0, timeout=0, discoverable=True)
        await advert.register(bus, adapter=adapter)
        advertisement["current"] = advert
        prepare_socket_path()
        state = HidState(
            hid.mouse_input, hid.keyboard_input,
            hid.absolute_input, hid.consumer_input,
        )
        server = await asyncio.start_unix_server(lambda r, w: handle_client(r, w, state, stop), path=SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o600)
        pairing_guard = asyncio.create_task(
            close_pairing_after_subscription(
                adapter,
                state,
                stop,
                bus=bus,
                advertisement_type=Advertisement,
                advertisement=advertisement,
            )
        )
        print(f"READY {SOCKET_PATH}", flush=True)
        print(f"Pair {DEVICE_NAME!r} in iPhone Settings > Accessibility > Touch > AssistiveTouch > Devices.", flush=True)
        async with server:
            await stop.wait()
    except Exception as error:  # BlueZ errors vary across adapter/daemon versions.
        exit_code = 1
        LOGGER.exception("Could not start BLE HID peripheral")
        print(f"ERROR: Could not start BLE HID peripheral: {error}", flush=True)
        print(
            "Ensure bluetooth.service is running, no conflicting pairing agent is active, "
            "and the adapter supports LE peripheral advertising.",
            flush=True,
        )
    finally:
        stop.set()
        if server:
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=2.0)
            except (asyncio.TimeoutError, TimeoutError):
                LOGGER.warning("Timed out while closing the local control server")
        if pairing_guard:
            pairing_guard.cancel()
            try:
                await pairing_guard
            except asyncio.CancelledError:
                pass
        advert = advertisement["current"]
        SOCKET_PATH.unlink(missing_ok=True)
        cleanup_actions = []
        if advert:
            cleanup_actions.append(("advertisement", advert.unregister))
        if collection:
            cleanup_actions.append(("GATT services", collection.unregister))
        if adapter and old_pairable is not None:
            cleanup_actions.append(("pairable state", lambda: adapter.set_pairable(old_pairable)))
        if adapter and old_discoverable is not None:
            cleanup_actions.append(("discoverable state", lambda: adapter.set_discoverable(old_discoverable)))
        if agent and agent_registered:
            cleanup_actions.append(("pairing agent", agent.unregister))
        for label, action in cleanup_actions:
            try:
                await asyncio.wait_for(action(), timeout=2.0)
            except (asyncio.TimeoutError, TimeoutError):
                LOGGER.warning("Timed out while restoring %s", label)
            except Exception as error:
                LOGGER.warning("Could not restore %s: %s", label, error)
        if bus:
            bus.disconnect()
        for signum in installed_signals:
            loop.remove_signal_handler(signum)
    return exit_code


async def close_pairing_after_subscription(
    adapter: Any,
    state: HidState,
    stop: asyncio.Event,
    *,
    bus: Any = None,
    advertisement_type: Any = None,
    advertisement: dict[str, Any] | None = None,
) -> None:
    """Close Just-Works enrollment after the first HID client subscribes."""
    while not stop.is_set() and not state.subscribed:
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.25)
        except (asyncio.TimeoutError, TimeoutError):
            pass
    if stop.is_set() or not state.subscribed:
        return

    start_private_advertisement = False
    if bus and advertisement_type and advertisement:
        current = advertisement.get("current")
        try:
            if current:
                await current.unregister()
        except Exception as error:
            LOGGER.warning("Could not close discoverable advertising: %s", error)
        else:
            advertisement["current"] = None
            start_private_advertisement = True

    closed = True
    for label, setter in (
        ("pairable", adapter.set_pairable),
        ("discoverable", adapter.set_discoverable),
    ):
        for attempt in range(3):
            if stop.is_set():
                return
            try:
                await setter(False)
                break
            except Exception as error:
                if attempt == 2:
                    closed = False
                    LOGGER.warning("Could not close Bluetooth %s state: %s", label, error)
                else:
                    await asyncio.sleep(0.25)

    if start_private_advertisement and not stop.is_set():
        try:
            replacement = advertisement_type(
                DEVICE_NAME,
                ["1812"],
                appearance=0x03C0,
                timeout=0,
                discoverable=False,
            )
            await replacement.register(bus, adapter=adapter)
            advertisement["current"] = replacement
        except Exception as error:
            LOGGER.warning("Could not start bonded-client-only advertising: %s", error)
    if closed:
        LOGGER.info("Closed Bluetooth pairing after the HID client subscribed")


class ControlClient:
    def __init__(self) -> None:
        self.socket: socket.socket | None = None

    def connect(self) -> bool:
        if self.socket:
            return True
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(0.15)
            client.connect(str(SOCKET_PATH))
            self.socket = client
            return True
        except OSError:
            return False

    def close(self) -> None:
        if self.socket:
            self.socket.close()
        self.socket = None

    def send(self, message: dict[str, Any]) -> bool:
        if not self.connect():
            return False
        try:
            assert self.socket
            self.socket.sendall((json.dumps(message, separators=(",", ":")) + "\n").encode())
            return True
        except OSError:
            self.close()
            return False

    def status(self) -> dict[str, Any] | None:
        if not self.send({"type": "status"}):
            return None
        try:
            assert self.socket
            data = b""
            while b"\n" not in data:
                chunk = self.socket.recv(4096)
                if not chunk:
                    raise OSError("input server closed the connection")
                data += chunk
            response = json.loads(data.split(b"\n", 1)[0])
            if not isinstance(response, dict):
                raise ValueError("input server returned a non-object response")
            return response
        except (OSError, ValueError, json.JSONDecodeError):
            self.close()
            return None


def stop_child_process(
    process: subprocess.Popen[str] | None,
    *,
    process_group: bool = False,
    timeout: float = 6.0,
) -> None:
    if not process or process.poll() is not None:
        return
    try:
        if process_group:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=timeout)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        if process_group:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=2)


def run_gui(auto_start: bool = False) -> int:
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        gi.require_version("Gst", "1.0")
        from gi.repository import Gdk, Gio, GLib, Gst, Gtk
    except (ImportError, ValueError) as error:
        print(f"GTK 4 and GStreamer are required: {error}", file=sys.stderr)
        return 2
    Gst.init(None)

    class ControlWindow:
        def __init__(self, app) -> None:
            self.client = ControlClient()
            self.server_process = None
            self.receiver_process = None
            self.receiver_log = None
            self.video_pipeline = None
            self.video_sink = None
            self.last_motion = None
            self.video_cursor = (0.0, 0.0)
            self.video_cursor_visible = False
            self.video_last_motion = None
            self.absolute_ready = False
            self.video_button_down = False
            self.server_output = deque(maxlen=30)
            self.scroll_accumulator = ScrollAccumulator()
            self.closing = False
            self.window = Gtk.ApplicationWindow(application=app, title="AirPlayPC Mirror & Control")
            self.window.set_default_size(1180, 720)
            self.window.connect("close-request", self.close)
            root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            root.set_margin_top(18); root.set_margin_bottom(18)
            root.set_margin_start(18); root.set_margin_end(18)
            self.window.set_child(root)

            heading = Gtk.Label(xalign=0)
            heading.set_markup("<span size='x-large' weight='bold'>AirPlayPC Mirror &amp; Control</span>\n"
                               "<span foreground='#888'>The live iPhone screen and AssistiveTouch controls in one window</span>")
            root.append(heading)

            status_row = Gtk.Box(spacing=12)
            status_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
            self.receiver_status = Gtk.Label(label="Mirror is stopped", xalign=0, wrap=True)
            self.status_label = Gtk.Label(label="Bluetooth input is stopped", xalign=0, wrap=True)
            status_text.append(self.receiver_status); status_text.append(self.status_label)
            status_row.append(status_text)
            self.receiver_button = Gtk.Button(label="Start mirror here")
            self.receiver_button.add_css_class("suggested-action")
            self.receiver_button.connect("clicked", self.toggle_receiver)
            status_row.append(self.receiver_button)
            self.start_button = Gtk.Button(label="Start Bluetooth input")
            self.start_button.connect("clicked", self.toggle_server)
            status_row.append(self.start_button)
            root.append(status_row)

            content = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True)
            content.set_resize_start_child(True); content.set_shrink_start_child(False)
            content.set_resize_end_child(False); content.set_shrink_end_child(False)
            content.set_position(730)
            content.set_vexpand(True)
            root.append(content)

            video_frame = Gtk.Frame()
            video_frame.set_margin_end(8)
            video_overlay = Gtk.Overlay()
            self.picture = Gtk.Picture(hexpand=True, vexpand=True)
            self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)
            self.picture.set_can_shrink(True)
            self.picture.set_focusable(True)
            self.picture.set_tooltip_text("Click the mirrored screen, then type with the PC keyboard")
            self.picture.set_size_request(620, 480)
            video_overlay.set_child(self.picture)
            self.video_placeholder = Gtk.Label(
                label="Start the embedded mirror, then choose AirPlayPC\n"
                      "in iPhone Control Center → Screen Mirroring",
                justify=Gtk.Justification.CENTER,
            )
            self.video_placeholder.add_css_class("dim-label")
            self.video_placeholder.set_can_target(False)
            video_overlay.add_overlay(self.video_placeholder)
            self.video_cursor_layer = Gtk.DrawingArea(hexpand=True, vexpand=True)
            self.video_cursor_layer.set_halign(Gtk.Align.FILL)
            self.video_cursor_layer.set_valign(Gtk.Align.FILL)
            self.video_cursor_layer.set_can_target(False)
            self.video_cursor_layer.set_draw_func(self.draw_video_cursor)
            video_overlay.add_overlay(self.video_cursor_layer)
            video_motion = Gtk.EventControllerMotion()
            video_motion.connect("enter", self.video_motion_enter)
            video_motion.connect("motion", self.video_motion)
            video_motion.connect("leave", self.video_motion_leave)
            self.picture.add_controller(video_motion)
            video_click = Gtk.GestureClick(); video_click.set_button(1)
            video_click.connect("pressed", self.video_click_pressed)
            video_click.connect("released", self.video_click_released)
            self.picture.add_controller(video_click)
            video_scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES)
            video_scroll.connect("scroll", self.scrolled)
            self.picture.add_controller(video_scroll)
            video_keys = Gtk.EventControllerKey()
            video_keys.connect("key-pressed", self.video_key_pressed)
            self.picture.add_controller(video_keys)
            video_frame.set_child(video_overlay)
            content.set_start_child(video_frame)

            controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            controls.set_margin_start(8)
            controls.set_size_request(390, -1)
            content.set_end_child(controls)

            instructions = Gtk.Label(
                label=f"1. Start mirror here and select AirPlayPC on the iPhone.\n"
                      f"2. Pair {DEVICE_NAME} in AssistiveTouch → Devices. If it was paired before this update, forget and re-pair it.\n"
                      f"3. In {DEVICE_NAME}, assign Button 2=Home, Button 3=App Switcher, Button 4=Spotlight.\n"
                      f"4. Click the mirrored screen, then type directly with your PC keyboard.",
                xalign=0, wrap=True,
            )
            instructions.add_css_class("dim-label")
            controls.append(instructions)

            self.pad = Gtk.DrawingArea(content_width=370, content_height=300, hexpand=True, vexpand=True)
            self.pad.set_draw_func(self.draw_pad)
            self.pad.set_tooltip_text("Move to steer the iPhone pointer; click to tap; scroll for iPhone scrolling")
            motion = Gtk.EventControllerMotion()
            motion.connect("enter", self.motion_enter)
            motion.connect("motion", self.motion)
            motion.connect("leave", self.motion_leave)
            self.pad.add_controller(motion)
            click = Gtk.GestureClick(); click.set_button(1)
            click.connect("pressed", self.click_pressed); click.connect("released", self.click_released)
            self.pad.add_controller(click)
            scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES)
            scroll.connect("scroll", self.scrolled); self.pad.add_controller(scroll)
            controls.append(self.pad)

            action_row = Gtk.Box(spacing=8)
            self.drag_lock = Gtk.ToggleButton(label="Hold / Select")
            self.drag_lock.connect("toggled", self.drag_lock_toggled)
            action_row.append(self.drag_lock)
            for label, pointer_button in (("Home · B2", 2), ("Switcher · B3", 3), ("Spotlight · B4", 4)):
                button = Gtk.Button(label=label)
                button.connect("clicked", self.pointer_action, pointer_button)
                action_row.append(button)
            controls.append(action_row)

            edit_row = Gtk.Box(spacing=8)
            for label, code in (("Select All", KEY_CODES["a"]), ("Copy", KEY_CODES["c"]), ("Paste", KEY_CODES["v"])):
                button = Gtk.Button(label=label)
                button.connect("clicked", self.shortcut, 8, code)
                edit_row.append(button)
            controls.append(edit_row)

            type_row = Gtk.Box(spacing=8)
            self.text_entry = Gtk.Entry(hexpand=True, placeholder_text="Type text on the iPhone")
            self.text_entry.connect("activate", self.send_text)
            text_focus = Gtk.EventControllerFocus()
            text_focus.connect("leave", self.text_focus_left)
            self.text_entry.add_controller(text_focus)
            type_row.append(self.text_entry)
            send = Gtk.Button(label="Send"); send.connect("clicked", self.send_text); type_row.append(send)
            backspace = Gtk.Button(label="⌫"); backspace.connect("clicked", self.shortcut, 0, 0x2A); type_row.append(backspace)
            enter = Gtk.Button(label="Return"); enter.connect("clicked", self.shortcut, 0, 0x28); type_row.append(enter)
            controls.append(type_row)
            warning = Gtk.Label(
                label="This is iOS AssistiveTouch—not Apple iPhone Mirroring. Face ID, lock-screen entry, camera, microphone, and true multitouch remain unavailable.",
                xalign=0, wrap=True,
            )
            warning.add_css_class("dim-label"); controls.append(warning)
            GLib.timeout_add(1000, self.poll_status)

        def draw_pad(self, _area, context, width, height):
            context.set_source_rgb(0.08, 0.09, 0.12); context.rectangle(0, 0, width, height); context.fill()
            context.set_source_rgb(0.30, 0.34, 0.43); context.set_line_width(2); context.rectangle(1, 1, width - 2, height - 2); context.stroke()
            context.set_source_rgb(0.72, 0.75, 0.82); context.select_font_face("Sans"); context.set_font_size(18)
            context.move_to(24, 38); context.show_text("Touchpad — move, click, drag, or scroll")

        def draw_video_cursor(self, _area, context, _width, _height):
            if not self.video_cursor_visible:
                return
            x, y = self.video_cursor
            context.set_source_rgba(0.0, 0.0, 0.0, 0.85)
            context.arc(x, y, 11, 0, 6.2832); context.fill()
            context.set_source_rgba(1.0, 1.0, 1.0, 0.95)
            context.arc(x, y, 8, 0, 6.2832); context.set_line_width(3); context.stroke()

        def video_coordinates(self, x, y):
            """Map a widget position to normalized video coordinates.

            Returns None when the pointer is on the letterbox padding around
            the video so callers can drop the event instead of forwarding an
            edge tap to the phone.
            """
            width, height = self.picture.get_width(), self.picture.get_height()
            paintable = self.picture.get_paintable()
            intrinsic_width = paintable.get_intrinsic_width() if paintable else width
            intrinsic_height = paintable.get_intrinsic_height() if paintable else height
            if intrinsic_width <= 0 or intrinsic_height <= 0 or width <= 0 or height <= 0:
                return None
            scale = min(width / intrinsic_width, height / intrinsic_height)
            shown_width, shown_height = intrinsic_width * scale, intrinsic_height * scale
            left, top = (width - shown_width) / 2, (height - shown_height) / 2
            if x < left or y < top or x > left + shown_width or y > top + shown_height:
                return None
            return (
                (x - left) / shown_width,
                (y - top) / shown_height,
            )

        def video_motion_enter(self, _controller, x, y):
            self.video_cursor_visible = True
            self.video_last_motion = (x, y)
            self.video_motion(_controller, x, y)

        def video_motion(self, _controller, x, y):
            self.video_cursor = (x, y)
            self.video_cursor_layer.queue_draw()
            if self.absolute_ready:
                coordinates = self.video_coordinates(x, y)
                if coordinates:
                    normalized_x, normalized_y = coordinates
                    self.client.send({
                        "type": "absolute",
                        "x": normalized_x * 32767,
                        "y": normalized_y * 32767,
                    })
                # Motion over the letterbox keeps the last phone position:
                # never push the pointer to an edge from a dead-zone move.
            elif self.video_last_motion:
                self.client.send({
                    "type": "move",
                    "dx": (x - self.video_last_motion[0]) * 1.35,
                    "dy": (y - self.video_last_motion[1]) * 1.35,
                })
            self.video_last_motion = (x, y)

        def video_motion_leave(self, _controller):
            self.video_cursor_visible = False
            self.video_last_motion = None
            self.video_cursor_layer.queue_draw()

        def video_click_pressed(self, _gesture, _count, x, y):
            self.picture.grab_focus()
            if self.absolute_ready and self.video_coordinates(x, y) is None:
                return  # A click on the letterbox must not tap the phone edge.
            self.video_button_down = True
            self.client.send({
                "type": "button", "button": 1, "pressed": True,
                "absolute": self.absolute_ready,
            })

        def video_click_released(self, _gesture, _count, _x, _y):
            # Only release what was pressed, even if the pointer left the
            # video area mid-drag; otherwise the button stays stuck down.
            if not self.video_button_down:
                return
            self.video_button_down = False
            self.client.send({
                "type": "button", "button": 1, "pressed": False,
                "absolute": self.absolute_ready,
            })

        def video_key_pressed(self, _controller, keyval, _keycode, state):
            """Forward keys only while the embedded phone screen has focus."""
            special_keys = {
                Gdk.KEY_BackSpace: 0x2A,
                Gdk.KEY_Return: 0x28,
                Gdk.KEY_KP_Enter: 0x28,
                Gdk.KEY_Tab: 0x2B,
                Gdk.KEY_Escape: 0x29,
                Gdk.KEY_Delete: 0x4C,
                Gdk.KEY_Right: 0x4F,
                Gdk.KEY_Left: 0x50,
                Gdk.KEY_Down: 0x51,
                Gdk.KEY_Up: 0x52,
            }
            if keyval in special_keys:
                modifier = 0x02 if state & Gdk.ModifierType.SHIFT_MASK else 0
                self.client.send({
                    "type": "key", "modifier": modifier,
                    "code": special_keys[keyval],
                })
                return True

            codepoint = Gdk.keyval_to_unicode(keyval)
            character = chr(codepoint) if codepoint else ""
            # Translate familiar Linux Ctrl editing shortcuts to the iOS
            # Command equivalents. Other modified keys remain local.
            if state & Gdk.ModifierType.CONTROL_MASK:
                shortcut_key = character.lower()
                if shortcut_key in ("a", "c", "v", "x", "z"):
                    self.client.send({
                        "type": "key", "modifier": 0x08,
                        "code": KEY_CODES[shortcut_key],
                    })
                    return True
                return False
            blocked_modifiers = Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK
            if state & blocked_modifiers:
                return False
            if character and character.isprintable() and encode_character(character):
                self.client.send({"type": "text", "value": character})
                return True
            return False

        def ensure_server(self):
            if self.client.connect():
                return
            if self.server_process and self.server_process.poll() is None:
                return
            self.server_process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--server"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            self.server_output.clear()
            # The server logs to stdout continuously. If nobody drains the
            # pipe it fills up (~64 KB) and the BLE input silently freezes,
            # so a background thread keeps it drained at all times.
            threading.Thread(target=self._drain_server_output, daemon=True).start()
            self.status_label.set_text("Starting Bluetooth HID peripheral…")

        def _drain_server_output(self):
            process = self.server_process
            if not process or not process.stdout:
                return
            try:
                for line in process.stdout:
                    text = line.rstrip()
                    if text:
                        self.server_output.append(text)
                        LOGGER.info("BLE server: %s", text)
            except (ValueError, OSError):
                pass

        def toggle_server(self, _button):
            if self.client.status() or (self.server_process and self.server_process.poll() is None):
                self.client.send({"type": "shutdown"})
                self.client.close()
                if self.server_process and self.server_process.poll() is None:
                    self.server_process.terminate()
                self.server_process = None
                self.status_label.set_text("Bluetooth input is stopped")
                self.start_button.set_label("Start Bluetooth input")
            else:
                self.ensure_server(); self.start_button.set_label("Stop Bluetooth input")

        def toggle_receiver(self, _button):
            if self.receiver_process and self.receiver_process.poll() is None:
                self.stop_receiver()
            else:
                self.start_receiver()

        def free_rtp_port(self):
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            probe.close()
            return port

        def start_video_pipeline(self, port):
            if Gst.ElementFactory.find("gtk4paintablesink") is None:
                raise RuntimeError(f"gst-plugin-gtk4 is missing; {uxplay_install_hint()}")
            description = (
                f'udpsrc address=127.0.0.1 port={port} '
                'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96" '
                '! rtpjitterbuffer latency=35 drop-on-latency=true '
                '! rtph264depay ! h264parse ! decodebin '
                '! queue leaky=downstream max-size-buffers=2 '
                '! videoconvert ! gtk4paintablesink name=video_sink sync=false'
            )
            self.video_pipeline = Gst.parse_launch(description)
            self.video_sink = self.video_pipeline.get_by_name("video_sink")
            self.picture.set_paintable(self.video_sink.get_property("paintable"))
            bus = self.video_pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message::error", self.video_error)
            self.video_pipeline.set_state(Gst.State.PLAYING)

        def video_error(self, _bus, message):
            error, _debug = message.parse_error()
            self.receiver_status.set_text(f"Embedded video error: {error.message}")

        def start_receiver(self):
            executable = find_uxplay()
            if not executable:
                self.receiver_status.set_text(f"UxPlay is missing — {uxplay_install_hint()}")
                return
            support = inspect_uxplay(executable)
            if not support.compatible:
                self.receiver_status.set_text(f"UxPlay is incompatible: {support.reason}")
                return
            rivals = competing_receivers()
            if rivals:
                self.receiver_status.set_text(f"Stop the other receiver first: {', '.join(rivals)}")
                return
            port = self.free_rtp_port()
            try:
                args, _notes = build_embedded_receiver_args(load_settings(), port)
                self.start_video_pipeline(port)
            except (RuntimeError, ValueError, GLib.Error) as error:
                self.receiver_status.set_text(str(error))
                self.stop_video_pipeline()
                return
            log_path = new_log_path("mirror-control")
            self.receiver_log = log_path.open("w", encoding="utf-8")
            self.receiver_log.write("argv: " + " ".join(args) + "\n---\n")
            self.receiver_log.flush()
            launch = [executable, *args]
            if shutil.which("stdbuf"):
                launch = [shutil.which("stdbuf"), "-oL", "-eL", *launch]
            try:
                self.receiver_process = subprocess.Popen(
                    launch, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, errors="replace", start_new_session=True,
                )
            except OSError as error:
                self.receiver_status.set_text(f"Could not start UxPlay: {error}")
                self.stop_video_pipeline()
                self.receiver_log.close(); self.receiver_log = None
                return
            self.ensure_server()
            self.receiver_status.set_text("Discoverable — select AirPlayPC in Screen Mirroring")
            self.receiver_button.set_label("Stop mirror")
            self.receiver_button.remove_css_class("suggested-action")
            self.receiver_button.add_css_class("destructive-action")
            threading.Thread(target=self.read_receiver_output, daemon=True).start()

        def read_receiver_output(self):
            process = self.receiver_process
            if not process or not process.stdout:
                return
            for line in process.stdout:
                if self.receiver_log:
                    self.receiver_log.write(line); self.receiver_log.flush()
                lower = line.lower()
                if "raop_rtp_mirror starting" in lower or "starting mirroring" in lower:
                    GLib.idle_add(self.receiver_status.set_text, "Mirroring inside this window")
                elif "connection" in lower and ("accepted" in lower or "request" in lower):
                    GLib.idle_add(self.receiver_status.set_text, "iPhone connected — waiting for video")
            code = process.wait()
            GLib.idle_add(self.receiver_exited, process, code)

        def receiver_exited(self, process, code):
            if process is not self.receiver_process:
                return False
            self.receiver_process = None
            if self.receiver_log:
                self.receiver_log.close(); self.receiver_log = None
            self.stop_video_pipeline()
            self.receiver_button.set_label("Start mirror here")
            self.receiver_button.remove_css_class("destructive-action")
            self.receiver_button.add_css_class("suggested-action")
            self.receiver_status.set_text("Mirror is stopped" if code in (0, -signal.SIGTERM) else f"UxPlay exited with status {code}")
            return False

        def stop_video_pipeline(self):
            if self.video_pipeline:
                self.video_pipeline.set_state(Gst.State.NULL)
            self.video_pipeline = None; self.video_sink = None
            self.picture.set_paintable(None)
            self.video_placeholder.set_visible(True)

        def stop_receiver(self):
            process = self.receiver_process
            if process and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                self.receiver_status.set_text("Stopping mirror…")
            else:
                self.stop_video_pipeline()

        def poll_status(self):
            status = self.client.status()
            if status:
                subscriptions = status.get("subscriptions", {})
                self.absolute_ready = bool(subscriptions.get("absolute"))
                self.status_label.set_text(control_status_text(status))
                self.start_button.set_label("Stop Bluetooth input")
            elif self.server_process and self.server_process.poll() is not None:
                output = " \u2014 ".join(list(self.server_output)[-2:])
                self.status_label.set_text(output or "Bluetooth input failed to start")
                self.server_process = None; self.start_button.set_label("Start Bluetooth input")
            if self.video_sink:
                stats = self.video_sink.get_property("stats")
                try:
                    rendered = stats.get_value("rendered") if stats else 0
                except (AttributeError, TypeError):
                    rendered = 0
                if rendered:
                    self.video_placeholder.set_visible(False)
            return True

        def motion_enter(self, _controller, x, y):
            self.last_motion = (x, y)

        def motion(self, _controller, x, y):
            if self.last_motion is None:
                self.last_motion = (x, y)
                return
            dx, dy = x - self.last_motion[0], y - self.last_motion[1]
            self.last_motion = (x, y)
            self.client.send({"type": "move", "dx": dx * 1.35, "dy": dy * 1.35})

        def motion_leave(self, _controller):
            self.last_motion = None

        def click_pressed(self, _gesture, _count, _x, _y):
            if not self.drag_lock.get_active():
                self.client.send({"type": "button", "button": 1, "pressed": True})

        def click_released(self, _gesture, _count, _x, _y):
            if not self.drag_lock.get_active():
                self.client.send({"type": "button", "button": 1, "pressed": False})

        def scrolled(self, _controller, dx, dy):
            vertical, horizontal = self.scroll_accumulator.consume(dx, dy)
            if vertical or horizontal:
                self.client.send({
                    "type": "scroll", "amount": vertical,
                    "horizontal": horizontal,
                })
            return True

        def drag_lock_toggled(self, button):
            self.client.send({
                "type": "button", "button": 1, "pressed": button.get_active(),
                "absolute": self.absolute_ready and self.video_cursor_visible,
            })

        def shortcut(self, _button, modifier, code):
            self.client.send({"type": "key", "modifier": modifier, "code": code})

        def consumer_shortcut(self, _button, usage):
            self.client.send({"type": "consumer", "usage": usage})

        def pointer_action(self, _button, pointer_button):
            self.client.send({
                "type": "pointer_button",
                "button": pointer_button,
                "absolute": True,
            })

        def send_text(self, _widget):
            value = self.text_entry.get_text()
            if value:
                self.client.send({"type": "text", "value": value})

        def text_focus_left(self, _controller):
            GLib.idle_add(self.clear_text_if_unfocused)

        def clear_text_if_unfocused(self):
            if not self.text_entry.has_focus():
                self.text_entry.set_text("")
            return False

        def close(self, _window):
            if self.closing:
                return False
            self.closing = True
            if self.drag_lock.get_active():
                self.client.send({"type": "button", "button": 1, "pressed": False})
            self.client.send({"type": "shutdown"})
            self.client.close()
            stop_child_process(self.server_process)
            self.server_process = None
            stop_child_process(self.receiver_process, process_group=True)
            self.receiver_process = None
            self.stop_video_pipeline()
            return False

    app = Gtk.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
    holder = {}

    def activate(application):
        if "window" not in holder:
            holder["window"] = ControlWindow(application)
            if auto_start:
                GLib.idle_add(holder["window"].start_receiver)
        holder["window"].window.present()

    app.connect("activate", activate)
    try:
        return app.run([sys.argv[0]])
    finally:
        window = holder.get("window")
        if window:
            window.close(None)


def self_test() -> int:
    assert len(REPORT_MAP) == 181 and REPORT_MAP.count(0x85) == 4
    assert build_mouse_report(1, -1, 127, -5, 6) == b"\x01\xff\x7f\xfb\x06"
    assert build_mouse_report(0x80) == b"\x80\x00\x00\x00\x00"
    assert build_absolute_report(1, 0, 32767) == b"\x01\x00\x00\xff\x7f"
    assert build_absolute_report(0x80, 0, 0) == b"\x80\x00\x00\x00\x00"
    assert build_consumer_report(0x029F) == b"\x9f\x02"
    assert build_keyboard_report(2, [4]) == b"\x02\x00\x04\x00\x00\x00\x00\x00"
    assert encode_character("a") == (0, 4)
    assert encode_character("A") == (2, 4)
    assert encode_character("!") == (2, 0x1E)
    assert encode_character(" ") == (2, 0x2C)
    assert encode_character("é") is None
    embedded, notes = build_embedded_receiver_args(
        ReceiverSettings(pin=True), 5010, probe_plugins=False
    )
    assert "-h265" not in embedded and "-pin" not in embedded and embedded[-2:] == [
        "-vrtp", "config-interval=1 pt=96 ! udpsink host=127.0.0.1 port=5010 sync=false async=false",
    ]
    assert any(item.startswith(EMBEDDED_RESOLUTION) for item in embedded) and notes

    class FakeCharacteristic:
        def __init__(self):
            self.reports = []
            self._notify = True

        def changed(self, report):
            self.reports.append(report)

    mouse, keyboard = FakeCharacteristic(), FakeCharacteristic()
    asyncio.run(HidState(mouse, keyboard).key(8, KEY_CODES["h"]))
    assert keyboard.reports == [
        build_keyboard_report(),
        build_keyboard_report(8),
        build_keyboard_report(8, [KEY_CODES["h"]]),
        build_keyboard_report(8),
        build_keyboard_report(),
    ]
    consumer = FakeCharacteristic()
    asyncio.run(HidState(mouse, keyboard, None, consumer).consumer_key(0x0223))
    assert consumer.reports == [build_consumer_report(0x0223), build_consumer_report()]
    absolute = FakeCharacteristic()
    asyncio.run(HidState(mouse, keyboard, absolute).click_button(2, absolute=True))
    assert absolute.reports == [
        build_absolute_report(2, 16384, 16384),
        build_absolute_report(0, 16384, 16384),
    ]
    scroll = ScrollAccumulator()
    assert [scroll.consume(0, 1) for _ in range(3)][-1] == (-1, 0)
    assert scroll.consume(1, 0) == (0, -1)
    assert control_status_text({
        "subscriptions": {
            "relative": False,
            "keyboard": False,
            "absolute": False,
            "consumer": False,
        }
    }) == f"Ready to pair as {DEVICE_NAME}"

    # A crashed server must not leave the GTK status poll spinning on EOF.
    client_socket, server_socket = socket.socketpair()
    client = ControlClient()
    client.socket = client_socket

    def close_after_request() -> None:
        server_socket.recv(4096)
        server_socket.close()

    closer = threading.Thread(target=close_after_request)
    closer.start()
    assert client.status() is None
    closer.join(timeout=1)
    assert not closer.is_alive()
    print("AssistiveTouch HID and embedded-mirroring self-test passed.")
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="AirPlayPC Bluetooth AssistiveTouch control")
    parser.add_argument("--server", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--start", action="store_true", help="start the embedded mirror and Bluetooth input")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.server:
        return asyncio.run(serve())
    try:
        return run_gui(args.start)
    except KeyboardInterrupt:
        LOGGER.info("Control window interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
