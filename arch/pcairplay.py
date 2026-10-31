#!/usr/bin/env python3
"""Arch Linux CLI, diagnostics, and self-tests for AirPlayPC."""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import importlib.util
from dataclasses import replace
from pathlib import Path

from pcairplay_common import (
    DEFAULT_PORT,
    ReceiverSettings,
    SOURCE_LAYOUT,
    build_uxplay_args,
    command_output,
    competing_receivers,
    default_route,
    find_uxplay,
    gst_has,
    interface_network,
    inspect_uxplay,
    is_arch_linux,
    load_settings,
    new_log_path,
    parse_uxplay_support,
    resolve_device_name,
    save_settings,
    service_active,
    shell_join,
    uses_external_dnssd,
    uxplay_install_hint,
)


COLORS = {
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "cyan": "\033[36m", "gray": "\033[90m", "reset": "\033[0m",
}


def colored(text: str, color: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR") is not None:
        return text
    return COLORS[color] + text + COLORS["reset"]


def settings_from_args(args: argparse.Namespace) -> ReceiverSettings:
    port = None if args.dynamic_ports else args.port
    return ReceiverSettings(
        name=args.name, fps=args.fps, resolution=args.resolution,
        refresh_rate=args.refresh_rate, sync=args.sync, fullscreen=args.fullscreen,
        pin=args.pin, software_decode=args.software_decode,
        share_safe=args.share_safe, no_audio=args.no_audio, port=port,
        legacy_ports=args.legacy_ports, engine_debug=args.engine_debug,
    )


def add_receiver_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--name", default="PC", help="name shown in Screen Mirroring")
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--resolution", default="1920x1080")
    parser.add_argument("--refresh-rate", type=int, default=60)
    parser.add_argument("--sync", action="store_true", help="prioritize A/V sync over touch latency")
    parser.add_argument("--fullscreen", action="store_true")
    parser.add_argument("--pin", action="store_true", help="require an Apple TV-style PIN")
    parser.add_argument("--software-decode", action="store_true")
    parser.add_argument("--share-safe", action="store_true", help="force the capture-friendly OpenGL sink")
    parser.add_argument("--no-audio", action="store_true")
    ports = parser.add_mutually_exclusive_group()
    ports.add_argument("--port", type=int, default=DEFAULT_PORT, help="first of three fixed TCP/UDP ports")
    ports.add_argument("--legacy-ports", action="store_true")
    ports.add_argument("--dynamic-ports", action="store_true", help="let UxPlay choose ports (firewall-hostile)")
    parser.add_argument("--engine-debug", action="store_true")


def run_receiver(args: argparse.Namespace) -> int:
    executable = find_uxplay()
    if not executable:
        print(colored(f"UxPlay is not installed; {uxplay_install_hint()}.", "red"), file=sys.stderr)
        return 1
    support = inspect_uxplay(executable)
    if not support.compatible:
        print(colored(f"UxPlay is incompatible: {support.reason}", "red"), file=sys.stderr)
        print(
            "Install UxPlay 1.73 or newer with both -scrsv and -vrtp support.",
            file=sys.stderr,
        )
        return 1
    try:
        settings = settings_from_args(args)
        clean_name, changed = resolve_device_name(settings.name)
        settings = replace(settings, name=clean_name)
        built, notes = build_uxplay_args(settings)
    except ValueError as error:
        print(colored(f"Error: {error}", "red"), file=sys.stderr)
        return 2
    command = [executable, *built]
    if changed:
        notes.insert(0, f"Device name normalized to {settings.name!r}.")
    if args.dry_run:
        print("DRY RUN — nothing started")
        print(shell_join(command))
        for note in notes:
            print(colored(f"Note: {note}", "yellow"))
        return 0

    rivals = competing_receivers()
    if rivals:
        print(colored(f"Warning: another receiver is running: {', '.join(rivals)}", "yellow"))
        print("Stop it first so the phone does not see duplicate receivers.")
        return 1
    interface, address, _ = default_route()
    log_path = None if args.no_log else new_log_path()
    print(colored("Starting the AirPlay receiver", "cyan"))
    print(f"  Appears on iPhone as : {settings.name}")
    print(f"  Arch host            : {address or 'unknown'} ({interface or 'no default route'})")
    print(f"  Video                : {settings.resolution} @ {settings.refresh_rate}Hz, capped at {settings.fps}fps")
    if settings.legacy_ports:
        port_description = "legacy set (TCP 7000/7001/7100; UDP 6000/6001/7011)"
    elif settings.port is None:
        port_description = "dynamic"
    else:
        port_description = f"{settings.port}-{settings.port + 2} TCP/UDP"
    print(f"  Ports                : {port_description}")
    if log_path:
        print(f"  Log                  : {log_path}")
    for note in notes:
        print(colored(f"  Note                 : {note}", "yellow"))
    print(colored(f"On the iPhone: Control Center → Screen Mirroring → {settings.name}", "green"))
    print("Press Ctrl+C to stop.\n")

    log_file = log_path.open("w", encoding="utf-8") if log_path else None
    if log_file:
        log_file.write(f"engine  : {executable}\nversion : {support.version}\nargv    : {shell_join(built)}\n---\n")
        log_file.flush()
    stdbuf = shutil.which("stdbuf")
    launch = [stdbuf, "-oL", "-eL", *command] if stdbuf else command
    try:
        process = subprocess.Popen(
            launch, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, errors="replace",
        )
    except OSError as error:
        if log_file:
            log_file.close()
        print(colored(f"Could not start UxPlay: {error}", "red"), file=sys.stderr)
        return 1

    def stop(_signum: int, _frame: object) -> None:
        if process.poll() is None:
            process.terminate()

    previous_int = signal.signal(signal.SIGINT, stop)
    previous_term = signal.signal(signal.SIGTERM, stop)
    try:
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="")
            if log_file:
                log_file.write(line)
                log_file.flush()
        code = process.wait()
        # A clean Ctrl+C (or a graceful SIGTERM stop) must not leak the
        # signal status (-15/-2) to callers checking the exit code.
        if code in (0, -signal.SIGTERM, -signal.SIGINT):
            return 0
        return code
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        if log_file:
            log_file.close()


class Doctor:
    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    def heading(self, text: str) -> None:
        print(colored(f"\n== {text}", "cyan"))

    def passed(self, text: str) -> None:
        print(colored(f"  [PASS] {text}", "green"))

    def fail(self, text: str, fix: str | None = None) -> None:
        self.failures += 1
        print(colored(f"  [FAIL] {text}", "red"))
        if fix:
            print(f"         Fix: {fix}")

    def warn(self, text: str) -> None:
        self.warnings += 1
        print(colored(f"  [WARN] {text}", "yellow"))

    def info(self, text: str) -> None:
        print(colored(f"  [info] {text}", "gray"))


def doctor(args: argparse.Namespace) -> int:
    report = Doctor()
    print(colored("AirPlayPC diagnostics — Arch Linux", "cyan"))

    report.heading("Platform and engine")
    if is_arch_linux():
        report.passed("Arch Linux with pacman detected.")
    else:
        report.fail("This port requires Arch Linux or an Arch derivative.")
    executable = find_uxplay()
    if executable:
        support = inspect_uxplay(executable)
        if support.compatible:
            report.passed(f"UxPlay {support.version} at {executable}.")
        else:
            report.fail(
                f"UxPlay at {executable} is incompatible: {support.reason}",
                "Install UxPlay 1.73 or newer with -scrsv and -vrtp support.",
            )
    else:
        fix = "Run ./arch/setup.sh" if SOURCE_LAYOUT else "Install the 'uxplay' package (pacman or an AUR helper)."
        report.fail("UxPlay is not installed.", fix)

    report.heading("GStreamer")
    required = ["h264parse", "decodebin", "avdec_h264", "autovideosink", "autoaudiosink"]
    missing = [element for element in required if not gst_has(element)]
    if missing:
        report.fail(f"Missing GStreamer elements: {', '.join(missing)}.", "sudo pacman -S gst-plugins-base gst-plugins-good gst-plugins-bad gst-libav")
    else:
        report.passed("H.264 parser/decoder and automatic audio/video sinks are available.")
    if gst_has("h265parse") and (gst_has("avdec_h265") or gst_has("vah265dec")):
        report.passed("H.265 support is available.")
    else:
        report.warn("H.265 decoding is incomplete; 4K and some newer iPhone streams may fail.")

    report.heading("Service discovery")
    if executable and uses_external_dnssd(executable):
        if service_active("avahi-daemon.service"):
            report.passed("This UxPlay build uses DNS-SD and avahi-daemon is active.")
        else:
            report.fail("This UxPlay build needs Avahi, but avahi-daemon is inactive.", "sudo systemctl enable --now avahi-daemon.service")
    elif executable:
        report.passed("UxPlay uses its built-in mDNS responder; Avahi is not required.")
    else:
        report.info("mDNS implementation cannot be checked until UxPlay is installed.")
    if service_active("systemd-resolved.service"):
        report.info("systemd-resolved is active; this is compatible with UxPlay mDNS.")
    uxplay_running = "uxplay" in competing_receivers()
    if uxplay_running:
        code, udp_sockets = command_output(["ss", "-H", "-lun"], timeout=5)
        if code == 0 and re_search_port(udp_sockets, 5353):
            report.passed("A running receiver has an mDNS socket on UDP 5353.")
        elif code == 0:
            report.fail("UxPlay is running but no UDP 5353 mDNS socket is visible.")
        else:
            report.warn("UxPlay is running, but its mDNS socket could not be inspected.")

    report.heading("Network")
    interface, address, gateway = default_route()
    network = interface_network(interface)
    if interface and address:
        report.passed(f"Default route uses {interface}, address {address}, gateway {gateway or 'on-link'}.")
        if network:
            report.info(f"Local subnet: {network}")
    else:
        report.fail("No usable IPv4 default route was found.")
    if args.phone_ip:
        try:
            phone = ipaddress.ip_address(args.phone_ip)
        except ValueError:
            report.fail(f"Invalid phone address: {args.phone_ip}")
        else:
            if network and phone in network:
                report.passed(f"Phone {phone} is on the same subnet.")
            elif network:
                report.fail(f"Phone {phone} is outside {network}.", "Put both devices on the same LAN; mDNS does not cross subnets.")
            code, _ = command_output(["ping", "-c", "1", "-W", "2", str(phone)], timeout=4)
            if code == 0:
                report.passed("Phone replied to ICMP ping.")
            else:
                report.warn("Phone did not reply to ping (iOS may suppress ICMP while asleep).")

    report.heading("Firewall")
    firewalld = service_active("firewalld.service")
    ufw_code, ufw_output = command_output(["sudo", "-n", "ufw", "status"], timeout=5) if shutil.which("ufw") else (127, "")
    ufw_active = service_active("ufw.service") or (ufw_code == 0 and "Status: active" in ufw_output)
    if not firewalld and not ufw_active:
        code, output = command_output(["systemctl", "is-active", "nftables.service"])
        if code == 0 and output == "active":
            report.warn("nftables.service is active; verify UDP 5353 and TCP/UDP 7000-7002 are allowed.")
        else:
            report.passed("Neither firewalld nor UFW is active.")
    if firewalld:
        code, _ = command_output(["firewall-cmd", "--query-service=pcairplay"])
        if code == 0:
            report.passed("firewalld allows the AirPlayPC service (mDNS and TCP/UDP 7000-7002).")
        else:
            fix = "Re-run ./arch/setup.sh" if SOURCE_LAYOUT else "Add firewalld rules for mDNS and the receiver ports manually."
            report.fail("firewalld does not allow the AirPlayPC service.", fix)
    if ufw_active:
        if ufw_code != 0:
            report.warn("UFW is active, but its rules require root to inspect; run doctor with sudo or inspect `sudo ufw status`.")
        else:
            normalized = ufw_output.replace(" ", "")
            for value, label in [("5353/udp", "UDP 5353"), ("7000:7002/tcp", "TCP 7000-7002"), ("7000:7002/udp", "UDP 7000-7002")]:
                if value in normalized:
                    report.passed(f"UFW has a rule for {label}.")
                else:
                    fix = "Re-run ./arch/setup.sh" if SOURCE_LAYOUT else f"Run 'sudo ufw allow {value.replace('/', ' ')}'."
                    report.fail(f"UFW has no visible rule for {label}.", fix)

    report.heading("Processes and ports")
    rivals = competing_receivers(include_uxplay=False)
    if rivals:
        report.fail(f"Competing AirPlay receiver(s): {', '.join(rivals)}.", "Close them before starting AirPlayPC.")
    else:
        report.passed("No known competing receiver is running.")
    code, sockets = command_output(["ss", "-H", "-lntu"], timeout=5)
    if code == 0:
        occupied = []
        for port in range(DEFAULT_PORT, DEFAULT_PORT + 3):
            if re_search_port(sockets, port):
                occupied.append(str(port))
        if occupied and not uxplay_running:
            report.fail(f"Default receiver ports already in use: {', '.join(occupied)}.", "Stop the owner or pass --port with another free three-port range.")
        elif occupied:
            report.info(f"UxPlay is running and holds port(s): {', '.join(occupied)}.")
        else:
            report.passed("TCP/UDP 7000-7002 are currently free.")
    else:
        report.warn("Could not inspect listening sockets with ss.")

    report.heading("AssistiveTouch control (optional)")
    vendor = Path(__file__).resolve().parent / "vendor"
    if vendor.is_dir():
        sys.path.insert(0, str(vendor))
    if importlib.util.find_spec("bluez_peripheral") and importlib.util.find_spec("dbus_fast"):
        report.passed("Bluetooth HID Python support is installed.")
    else:
        fix = "re-run ./setup.sh" if SOURCE_LAYOUT else "install python-dbus-fast and the bluez_peripheral wheel (see README)."
        report.warn(f"Bluetooth HID support is missing; {fix} to enable iPhone control.")
    if service_active("bluetooth.service"):
        report.passed("bluetooth.service is active.")
    else:
        report.warn("bluetooth.service is inactive; AssistiveTouch control will not start.")
    adapters = [
        item for item in Path("/sys/class/bluetooth").glob("hci*")
        if re.fullmatch(r"hci\d+", item.name)
    ] if Path("/sys/class/bluetooth").exists() else []
    if adapters:
        report.passed(f"Bluetooth adapter detected: {', '.join(item.name for item in adapters)}.")
    else:
        report.warn("No Bluetooth adapter is currently visible to Linux.")
    if gst_has("gtk4paintablesink"):
        report.passed("GTK4 GStreamer sink is available for the embedded mirror.")
    else:
        report.warn("Embedded mirroring is unavailable; install gst-plugin-gtk4.")

    report.heading("Summary")
    if report.failures:
        print(colored(f"  {report.failures} problem(s), {report.warnings} warning(s).", "red"))
        return 1
    print(colored(f"  Ready to mirror. {report.warnings} warning(s).", "green"))
    return 0


def re_search_port(text: str, port: int) -> bool:
    return re.search(rf"(?:\*|\[[^]]+\]|[\d.:a-fA-F]+):{port}\b", text) is not None


def self_test() -> int:
    base = ReceiverSettings(name="Demo Screen", fps=30, resolution="1280x720", refresh_rate=60)
    args, notes = build_uxplay_args(base, probe_plugins=False)
    expected = ["-n", "Demo Screen", "-nh", "-s", "1280x720@60", "-fps", "30"]
    assert args[:7] == expected
    assert ["-vsync", "no"] == args[args.index("-vsync"):args.index("-vsync") + 2]
    assert args[args.index("-p") + 1] == "7000"
    assert "-h265" in args and "-scrsv" in args and not notes
    pinned, _ = build_uxplay_args(replace(base, pin=True), pin_code="0427", probe_plugins=False)
    assert pinned[pinned.index("-pin"):pinned.index("-pin") + 3] == ["-pin", "0427", "-reg"]
    share, _ = build_uxplay_args(replace(base, share_safe=True), probe_plugins=False)
    assert share[share.index("-vs"):share.index("-vs") + 2] == ["-vs", "glimagesink"]
    dynamic, _ = build_uxplay_args(replace(base, port=None), probe_plugins=False)
    assert "-p" not in dynamic
    legacy, _ = build_uxplay_args(replace(base, legacy_ports=True), probe_plugins=False)
    assert legacy[-1] == "-p"
    supported = parse_uxplay_support(
        "UxPlay 1.73.6\n  -scrsv <0|1>\n  -vrtp <pipeline>\n"
    )
    assert supported.compatible and supported.version == "1.73.6"
    old = parse_uxplay_support(
        "UxPlay 1.72.1\n  -scrsv <0|1>\n  -vrtp <pipeline>\n"
    )
    assert not old.compatible and "too old" in (old.reason or "")
    missing = parse_uxplay_support("UxPlay 1.74\n  -scrsv <0|1>\n")
    assert not missing.compatible and missing.missing_options == ("-vrtp",)
    with tempfile.TemporaryDirectory() as temporary:
        settings_path = Path(temporary) / "settings.json"
        save_settings(replace(base, port=None), settings_path)
        assert load_settings(settings_path).port is None
        settings_path.write_text('{"fps": true, "name": 42, "port": "bad"}', encoding="utf-8")
        recovered = load_settings(settings_path)
        assert recovered.fps == 60 and recovered.name == "PC" and recovered.port == 7000
    assert resolve_device_name("  Demo  ") == ("Demo", True)
    try:
        resolve_device_name("-bad")
    except ValueError:
        pass
    else:
        raise AssertionError("leading dash was accepted")
    print("Arch self-test passed: settings, UxPlay capabilities, and argv construction.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pcairplay", description="AirPlayPC for Arch Linux")
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start", help="start the AirPlay receiver")
    add_receiver_arguments(start)
    start.add_argument("--no-log", action="store_true")
    start.add_argument("--dry-run", action="store_true")
    check = subparsers.add_parser("doctor", help="diagnose installation and network problems")
    check.add_argument("--phone-ip")
    subparsers.add_parser("self-test", help=argparse.SUPPRESS)
    parsed = parser.parse_args(argv)
    if parsed.command == "start":
        return run_receiver(parsed)
    if parsed.command == "doctor":
        return doctor(parsed)
    return self_test()


if __name__ == "__main__":
    raise SystemExit(main())
