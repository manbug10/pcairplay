# AirPlayPC on Arch Linux

This directory is the native Arch Linux port. It does not require PowerShell,
Wine, Bonjour, or the Windows UxPlay bundle.

## Components

| File | Purpose |
|---|---|
| `pcairplay_common.py` | Shared settings, validation, UxPlay argv, XDG paths, route and process probes |
| `pcairplay.py` | `start`, `doctor`, and headless self-test commands |
| `airplay-ui.py` | GTK 4 controller, status/log view, PIN display, lifecycle management |
| `frame-mirror.py` | X11-only phone-style bezel which tracks the UxPlay video window |
| `assistive-control.py` | Combined in-app AirPlay view plus BLE HID touchpad/keyboard for iOS AssistiveTouch |
| `setup.sh` | pacman/AUR/source install, mDNS detection, firewall and desktop integration |
| `PKGBUILD` | Optional system-wide `pcairplay-git` package recipe |
| `pcairplay.service` | Optional rootless systemd user service |

Top-level `setup.sh`, `start-airplay.sh`, `doctor.sh`, `AirPlayPC.sh`, and
`AirPlayPC-Control.sh` are convenience launchers for these files.

## Design choices

- UxPlay 1.73 or newer is required. Startup and diagnostics verify both the
  version and the `-scrsv`/`-vrtp` options because those options are required by
  the embedded receiver and may be absent from builds without D-Bus support.
  The Windows-only 1.x/2.x packaging split does not apply on Arch.
- TCP and UDP 7000–7002 are fixed by default because Linux firewalls cannot make
  a Windows-style executable-scoped allow rule. `--dynamic-ports` remains
  available on trusted networks without a firewall.
- Current UxPlay defaults to a built-in mDNS responder. The installer checks the
  actual binary with `ldd`; it enables Avahi only when the installed build links
  the external DNS-SD compatibility library.
- `-scrsv 1` uses UxPlay's D-Bus integration to prevent blanking only while
  video is active.
- The GUI owns its UxPlay process group. Closing the GUI terminates the receiver
  and frame helper, while the optional systemd service is intentionally separate.
- Wayland supports normal mirroring and fullscreen. The decorative frame is
  X11-only: this is a compositor security boundary, not a missing package.

## Verification

The tests do not require a display or an installed UxPlay engine:

```bash
python arch/pcairplay.py self-test
python arch/airplay-ui.py --self-test
python arch/frame-mirror.py --self-test
python arch/assistive-control.py --self-test
bash -n arch/setup.sh setup.sh start-airplay.sh doctor.sh AirPlayPC.sh AirPlayPC-Control.sh
```

After installing UxPlay, use `./doctor.sh` for live GStreamer, mDNS, route,
firewall, rival-process, and port checks.

## AssistiveTouch control

Run `pcairplay-control` and click **Start mirror here**. UxPlay forwards the
decrypted H.264 stream over a loopback-only RTP socket to a GTK4 GStreamer
paintable, so the phone screen and controls remain in one native window on X11
or Wayland. Then pair **AirPlayPC Input** at
iPhone **Settings → Accessibility → Touch → AssistiveTouch → Devices →
Bluetooth Devices**. This supplies pointer and keyboard input while UxPlay
supplies the image. Embedded mode requests 1080p H.264 because raw RTP does not
carry enough session metadata for deterministic H.264/H.265 depayloader
switching. The normal receiver retains H.265 and 4K support.

The video pane publishes an absolute HID pointer report and draws its own
low-latency cursor overlay. Clicking or dragging directly on the mirrored phone
therefore targets the corresponding screen position, and the cursor remains
part of a window capture even when the compositor omits the host cursor.
Horizontal two-finger scrolling is emitted as the HID Consumer AC Pan usage.
Vertical scrolling is deliberately damped and both axes accumulate fractional
motion, making slow gestures useful without losing horizontal home-screen
paging.
System actions use extra pointer buttons because iPhone ignores generic desktop
Consumer actions: under AssistiveTouch → Devices → AirPlayPC Input, assign
Button 2 to Home, Button 3 to App Switcher, and Button 4 to Spotlight through
**Customize Additional Buttons**. Forget and re-pair an older AirPlayPC Input
pairing first so iOS refreshes the HID descriptor. Click a phone text field in
the embedded video to type directly from the PC; keeping Full Keyboard Access
off avoids its reserved hardware-keyboard shortcuts.
Hold/Select supports drag-selection directly over the embedded screen, with
dedicated hardware-keyboard shortcuts for Select All, Copy, and Paste. The local
text proxy retains its contents after Enter/Send and clears only when focus
leaves.

The installer pins and SHA-256-verifies `bluez-peripheral` 0.2.0a5 inside the
private application directory; it does not modify the system Python environment.
AirPlayPC registers its own BlueZ pairing agent, so pairing does not depend on a
GNOME, KDE, or blueman applet. Start control mode only when you intend to pair:
Bluetooth HID uses encrypted iOS bonds but Just Works pairing has no
man-in-the-middle verification. A nearby device could pair during that brief
window. After the iPhone subscribes, AirPlayPC stops being discoverable and
pairable while retaining a non-discoverable advertisement for the bonded phone;
all prior adapter settings are restored when control mode exits. Input is sent
only to subscribed characteristics and is captured only by the focused control
window.

For a tracked system-wide install instead of the user-local setup script, copy
`arch/PKGBUILD` to an empty build directory and run `makepkg -si`. IMPORTANT:
`uxplay` lives in the AUR, not in the official repositories, and `makepkg -s`
resolves dependencies with pacman alone - a plain `makepkg -si` therefore fails
with `target not found: uxplay` on a clean Arch system. Install the dependency
first with an AUR helper (`paru -S uxplay` or `yay -S uxplay`), then run
`makepkg -si`; or use the helper for both, e.g. `paru -S pcairplay-git` after
adding this PKGBUILD locally.
