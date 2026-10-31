"""Shared Arch Linux support for AirPlayPC.

This module deliberately has no third-party Python dependencies.  The GUI is
optional; the CLI, diagnostics, and self-tests still work on a minimal Arch
installation.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import struct
import subprocess
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Iterable, Sequence


APP_NAME = "AirPlayPC"
APP_ID = "io.github.gbulog.pcairplay"
DEFAULT_PORT = 7000
UXPLAY_MIN_VERSION = (1, 73)
UXPLAY_REQUIRED_OPTIONS = ("-scrsv", "-vrtp")


def _xdg_path(variable: str, fallback: str) -> Path:
    # An empty XDG variable must be treated as unset per the XDG base
    # directory spec, otherwise config/state land in a cwd-relative path.
    value = os.environ.get(variable) or ""
    base = Path(value) if value.strip() else Path.home() / fallback
    return base.expanduser()


CONFIG_DIR = _xdg_path("XDG_CONFIG_HOME", ".config") / "pcairplay"
STATE_DIR = _xdg_path("XDG_STATE_HOME", ".local/state") / "pcairplay"
SETTINGS_PATH = CONFIG_DIR / "ui-settings.json"

# This module lives next to the repository scripts in a source checkout, but
# is installed to /usr/lib/pcairplay (or similar) by the PKGBUILD. Use that
# to give setup advice that actually applies to the current installation.
SOURCE_LAYOUT = (Path(__file__).resolve().parent / "setup.sh").is_file()


def uxplay_install_hint() -> str:
    """Return the right repair hint for this installation type."""
    if SOURCE_LAYOUT:
        return "run ./setup.sh from the AirPlayPC folder"
    return "install the 'uxplay' package (pacman or your AUR helper)"


@dataclass
class ReceiverSettings:
    name: str = "PC"
    fps: int = 60
    resolution: str = "1920x1080"
    refresh_rate: int = 60
    sync: bool = False
    fullscreen: bool = False
    pin: bool = False
    software_decode: bool = False
    share_safe: bool = False
    no_audio: bool = False
    port: int | None = DEFAULT_PORT
    legacy_ports: bool = False
    engine_debug: bool = False
    frame: bool = False


@dataclass(frozen=True)
class UxPlaySupport:
    version: str | None
    compatible: bool
    missing_options: tuple[str, ...] = ()
    reason: str | None = None


def load_settings(path: Path = SETTINGS_PATH) -> ReceiverSettings:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ReceiverSettings()
    if not isinstance(raw, dict):
        return ReceiverSettings()
    defaults = ReceiverSettings()
    values = {}
    for item in fields(ReceiverSettings):
        if item.name not in raw:
            continue
        value = raw[item.name]
        default = getattr(defaults, item.name)
        if item.name == "port":
            valid = value is None or (isinstance(value, int) and not isinstance(value, bool))
        elif isinstance(default, bool):
            valid = isinstance(value, bool)
        elif isinstance(default, int):
            valid = isinstance(value, int) and not isinstance(value, bool)
        else:
            valid = isinstance(value, str)
        if valid:
            values[item.name] = value
    try:
        return ReceiverSettings(**values)
    except TypeError:
        return ReceiverSettings()


def save_settings(settings: ReceiverSettings, path: Path = SETTINGS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(settings), indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def resolve_device_name(name: str) -> tuple[str, bool]:
    clean = name.replace("\x00", "").strip().rstrip("\\") or "PC"
    if clean.startswith("-"):
        raise ValueError("The device name cannot start with '-'.")
    if len(clean.encode("utf-8")) > 255:
        raise ValueError("The device name must be at most 255 UTF-8 bytes.")
    return clean, clean != name


def gst_has(element: str) -> bool:
    inspect = shutil.which("gst-inspect-1.0")
    if not inspect:
        return False
    return subprocess.run(
        [inspect, element], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
    ).returncode == 0


def build_uxplay_args(
    settings: ReceiverSettings,
    *,
    pin_code: str | None = None,
    probe_plugins: bool = True,
) -> tuple[list[str], list[str]]:
    name, _ = resolve_device_name(settings.name)
    if not re.fullmatch(r"\d{1,4}x\d{1,4}", settings.resolution):
        raise ValueError("Resolution must look like 1920x1080.")
    if not 1 <= settings.refresh_rate <= 255:
        raise ValueError("Refresh rate must be between 1 and 255.")
    if not 1 <= settings.fps <= 255:
        raise ValueError("Frame rate must be between 1 and 255.")
    if settings.port is not None and not 1024 <= settings.port <= 65533:
        raise ValueError("The base port must be between 1024 and 65533.")
    if pin_code is not None and not re.fullmatch(r"\d{4}", pin_code):
        raise ValueError("PIN must contain exactly four digits.")

    args = [
        "-n", name,
        "-nh",
        "-s", f"{settings.resolution}@{settings.refresh_rate}",
        "-fps", str(settings.fps),
        "-nc",
        "-nohold",
        "-h265",
        "-scrsv", "1",
    ]
    args.extend(["-vsync"] if settings.sync else ["-vsync", "no"])
    notes: list[str] = []

    if settings.share_safe:
        if not probe_plugins or gst_has("glimagesink"):
            args.extend(["-vs", "glimagesink"])
        else:
            notes.append("Share-safe mode requested, but glimagesink is unavailable; using automatic video output.")
    if settings.no_audio:
        args.extend(["-as", "0"])
    if settings.legacy_ports:
        args.append("-p")
    elif settings.port is not None:
        args.extend(["-p", str(settings.port)])
    if settings.fullscreen:
        args.append("-fs")
    if pin_code:
        args.extend(["-pin", pin_code, "-reg"])
    elif settings.pin:
        args.extend(["-pin", "-reg"])
    if settings.software_decode:
        args.append("-avdec")
    if settings.engine_debug:
        args.extend(["-d", "1"])
    return args, notes


def find_uxplay() -> str | None:
    override = os.environ.get("UXPLAY_BIN")
    if override and os.access(override, os.X_OK):
        return override
    return shutil.which("uxplay")


def parse_uxplay_support(help_output: str) -> UxPlaySupport:
    match = re.search(r"UxPlay\s+([0-9]+(?:\.[0-9]+)+)", help_output)
    version = match.group(1) if match else None
    missing = tuple(
        option for option in UXPLAY_REQUIRED_OPTIONS if option not in help_output
    )
    if version:
        parts = tuple(int(item) for item in version.split("."))
        if parts[:2] < UXPLAY_MIN_VERSION:
            minimum = ".".join(str(item) for item in UXPLAY_MIN_VERSION)
            return UxPlaySupport(
                version,
                False,
                missing,
                f"UxPlay {version} is too old; AirPlayPC requires {minimum} or newer.",
            )
    if missing:
        return UxPlaySupport(
            version,
            False,
            missing,
            "This UxPlay build lacks required option(s): " + ", ".join(missing),
        )
    if not version:
        return UxPlaySupport(
            None,
            False,
            (),
            "UxPlay's version could not be read from `uxplay -h`.",
        )
    return UxPlaySupport(version, True)


def inspect_uxplay(executable: str) -> UxPlaySupport:
    try:
        result = subprocess.run(
            [executable, "-h"], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return UxPlaySupport(
            None, False, (), f"Could not inspect UxPlay capabilities: {error}"
        )
    return parse_uxplay_support(result.stdout)


def uxplay_version(executable: str) -> str | None:
    return inspect_uxplay(executable).version


def new_log_path(prefix: str = "start-airplay") -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    paths = sorted(STATE_DIR.glob(f"{prefix}-*.log"), key=lambda item: item.stat().st_mtime, reverse=True)
    for old in paths[4:]:
        try:
            old.unlink()
        except OSError:
            pass
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return STATE_DIR / f"{prefix}-{stamp}.log"


def default_route() -> tuple[str | None, str | None, str | None]:
    """Return interface, source address, and gateway for the preferred IPv4 route."""
    ip = shutil.which("ip")
    result = subprocess.run(
        [ip, "-j", "route", "show", "default"], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    ) if ip else None
    try:
        routes = json.loads(result.stdout) if result else []
    except (ValueError, TypeError):
        routes = []
    if not routes:
        return _proc_default_route()
    route = min(routes, key=lambda value: value.get("metric", 0))
    interface = route.get("dev")
    source = route.get("prefsrc")
    gateway = route.get("gateway")
    if not source and interface:
        addr = subprocess.run(
            [ip, "-j", "-4", "addr", "show", "dev", interface], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        )
        try:
            entries = json.loads(addr.stdout)
            source = next(
                item["local"] for entry in entries for item in entry.get("addr_info", [])
                if item.get("family") == "inet" and item.get("scope") == "global"
            )
        except (ValueError, StopIteration, KeyError, TypeError):
            source = None
    return interface, source, gateway


def _interface_ipv4(interface: str, request: int) -> str | None:
    """Use ioctl as a fallback when netlink access is restricted."""
    try:
        import fcntl
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            packed = struct.pack("256s", interface[:15].encode("ascii"))
            return socket.inet_ntoa(fcntl.ioctl(sock.fileno(), request, packed)[20:24])
    except (OSError, UnicodeEncodeError):
        return None


def _proc_default_route() -> tuple[str | None, str | None, str | None]:
    try:
        lines = Path("/proc/net/route").read_text(encoding="ascii").splitlines()[1:]
    except OSError:
        return None, None, None
    candidates = []
    for line in lines:
        columns = line.split()
        if len(columns) < 8 or columns[1] != "00000000":
            continue
        try:
            flags, metric = int(columns[3], 16), int(columns[6])
            gateway = socket.inet_ntoa(struct.pack("<I", int(columns[2], 16)))
        except (ValueError, OSError):
            continue
        if flags & 0x1:
            candidates.append((metric, columns[0], gateway))
    if not candidates:
        return None, None, None
    _, interface, gateway = min(candidates)
    return interface, _interface_ipv4(interface, 0x8915), gateway


def interface_network(interface: str | None) -> ipaddress.IPv4Network | None:
    if not interface:
        return None
    ip = shutil.which("ip")
    result = subprocess.run(
        [ip, "-j", "-4", "addr", "show", "dev", interface], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    ) if ip else None
    try:
        entries = json.loads(result.stdout) if result else []
        info = next(
            item for entry in entries for item in entry.get("addr_info", [])
            if item.get("family") == "inet" and item.get("scope") == "global"
        )
        return ipaddress.ip_network(f"{info['local']}/{info['prefixlen']}", strict=False)
    except (ValueError, StopIteration, KeyError, TypeError):
        address = _interface_ipv4(interface, 0x8915)
        mask = _interface_ipv4(interface, 0x891B)
        if address and mask:
            try:
                return ipaddress.ip_network(f"{address}/{mask}", strict=False)
            except ValueError:
                pass
        return None


def process_names() -> set[str]:
    names: set[str] = set()
    proc = Path("/proc")
    try:
        entries = proc.iterdir()
    except OSError:
        return names
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            names.add((entry / "comm").read_text(encoding="utf-8").strip())
        except OSError:
            continue
    return names


def competing_receivers(*, include_uxplay: bool = True) -> list[str]:
    patterns = (
        re.compile(r"^uxplay$", re.I),
        re.compile(r"^(pigeoncast|airserver|reflector.*|airmypc|lonelyscreen|x-mirage)$", re.I),
    )
    result = []
    for name in process_names():
        if (include_uxplay or name.lower() != "uxplay") and any(pattern.match(name) for pattern in patterns):
            result.append(name)
    return sorted(result, key=str.lower)


def shell_join(parts: Sequence[str]) -> str:
    return shlex.join(parts)


def is_arch_linux() -> bool:
    return Path("/etc/arch-release").exists() and shutil.which("pacman") is not None


def command_output(command: Sequence[str], timeout: float = 8) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, check=False,
        )
        return result.returncode, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        return 127, str(error)


def service_active(name: str, *, user: bool = False) -> bool:
    command = ["systemctl"]
    if user:
        command.append("--user")
    command.extend(["is-active", "--quiet", name])
    return subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0


def uses_external_dnssd(executable: str) -> bool:
    code, output = command_output(["ldd", executable])
    return code == 0 and ("libdns_sd" in output or "libavahi-compat-libdns_sd" in output)


def available_terminal_command(command: Iterable[str]) -> list[str] | None:
    terminal = os.environ.get("TERMINAL")
    candidates: list[list[str]] = []
    if terminal:
        candidates.append(shlex.split(terminal) + ["-e"])
    candidates.extend([
        ["xdg-terminal-exec"], ["kgx", "--"], ["konsole", "-e"],
        ["gnome-terminal", "--"], ["xfce4-terminal", "-e"], ["xterm", "-e"],
    ])
    for prefix in candidates:
        if shutil.which(prefix[0]):
            return prefix + list(command)
    return None


def local_hostname() -> str:
    return socket.gethostname().split(".", 1)[0]
