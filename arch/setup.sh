#!/usr/bin/env bash
# Install or remove the Arch Linux AirPlayPC integration.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/pcairplay"
readonly BIN_DIR="$HOME/.local/bin"
readonly DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
readonly ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
readonly SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
readonly UXPLAY_TAG="v1.73.6"
readonly BLUEZ_PERIPHERAL_WHEEL="bluez_peripheral-0.2.0a5-py3-none-any.whl"
readonly BLUEZ_PERIPHERAL_URL="https://files.pythonhosted.org/packages/00/c3/be30e1f8ac9c9664024443fff8493e07850c4ea7a07f6d2a7410adc3a196/$BLUEZ_PERIPHERAL_WHEEL"
readonly BLUEZ_PERIPHERAL_SHA256="385a2e83aa212de0a8f9d2ae9db89cb648a4286d81bb11c62146e56c8edf31f7"
DRY_RUN=0
UNINSTALL=0
NO_GUI=0
SOURCE_BUILD=0

usage() {
    cat <<'EOF'
Usage: ./arch/setup.sh [--dry-run] [--uninstall] [--no-gui] [--source]

  --dry-run    print changes without making them
  --uninstall  remove AirPlayPC integration and its firewall rules
  --no-gui     install only the CLI and diagnostics
  --source     build pinned UxPlay v1.73.6 even if a package is available

UxPlay itself is left installed during --uninstall because it may be used by
other applications.
EOF
}

while (($#)); do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --uninstall) UNINSTALL=1 ;;
        --no-gui) NO_GUI=1 ;;
        --source) SOURCE_BUILD=1 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

note() { printf '\n==> %s\n' "$*"; }
ok() { printf '    [ok] %s\n' "$*"; }
warn() { printf '    [!!] %s\n' "$*" >&2; }
run() {
    if ((DRY_RUN)); then printf '    + '; printf '%q ' "$@"; printf '\n';
    else "$@"; fi
}

if [[ ! -f /etc/arch-release ]] || ! command -v pacman >/dev/null; then
    printf 'This installer supports Arch Linux and Arch derivatives only.\n' >&2
    exit 1
fi

remove_firewall_rules() {
    if command -v firewall-cmd >/dev/null && systemctl is-active --quiet firewalld.service; then
        run sudo firewall-cmd --permanent --remove-service=pcairplay || true
        run sudo firewall-cmd --permanent --delete-service=pcairplay || true
        run sudo firewall-cmd --reload
    fi
    if command -v ufw >/dev/null && ufw_is_active; then
        if ((DRY_RUN)); then
            printf '    + sudo ufw --force delete <each rule commented AirPlayPC>\n'
        else
            local numbers=()
            mapfile -t numbers < <(sudo ufw status numbered | sed -nE 's/^\[[[:space:]]*([0-9]+)\].*AirPlayPC.*/\1/p' | sort -rn)
            local number
            for number in "${numbers[@]}"; do sudo ufw --force delete "$number"; done
        fi
    fi
}

ufw_is_active() {
    # `ufw status` requires root. The world-readable configuration is the most
    # reliable preview check on Arch because ufw.service may be inactive after
    # its oneshot rules have already been loaded into the kernel.
    if [[ -r /etc/ufw/ufw.conf ]] && grep -Eiq '^[[:space:]]*ENABLED[[:space:]]*=[[:space:]]*yes' /etc/ufw/ufw.conf; then
        return 0
    fi
    if systemctl is-active --quiet ufw.service 2>/dev/null; then
        return 0
    fi
    if ((DRY_RUN)); then
        sudo -n ufw status 2>/dev/null | grep -q '^Status: active'
    else
        sudo ufw status 2>/dev/null | grep -q '^Status: active'
    fi
}

if ((UNINSTALL)); then
    note 'Removing AirPlayPC user integration'
    if ((DRY_RUN)); then
        printf '    + systemctl --user disable --now %q\n' 'pcairplay.service'
        printf '    + rm -rf %q\n' "$APP_DIR"
        printf '    + rm -f %q %q %q %q %q %q %q %q\n' \
            "$BIN_DIR/pcairplay" "$BIN_DIR/pcairplay-ui" \
            "$BIN_DIR/pcairplay-control" "$DESKTOP_DIR/pcairplay.desktop" \
            "$DESKTOP_DIR/pcairplay-diagnostics.desktop" "$DESKTOP_DIR/pcairplay-control.desktop" \
            "$ICON_DIR/pcairplay.svg" "$SYSTEMD_DIR/pcairplay.service"
    else
        # Stop and disable the unit BEFORE deleting its file: daemon-reload
        # alone never stops an active unit, and skipping this left the receiver
        # running (plus a dangling wants symlink) after a "successful" removal.
        if command -v systemctl >/dev/null; then
            systemctl --user disable --now pcairplay.service 2>/dev/null || true
        fi
        rm -rf -- "$APP_DIR"
        rm -f -- "$BIN_DIR/pcairplay" "$BIN_DIR/pcairplay-ui" "$BIN_DIR/pcairplay-control" \
            "$DESKTOP_DIR/pcairplay.desktop" "$DESKTOP_DIR/pcairplay-diagnostics.desktop" \
            "$DESKTOP_DIR/pcairplay-control.desktop" \
            "$ICON_DIR/pcairplay.svg" "$SYSTEMD_DIR/pcairplay.service"
    fi
    remove_firewall_rules
    if command -v systemctl >/dev/null; then run systemctl --user daemon-reload || true; fi
    if command -v update-desktop-database >/dev/null; then run update-desktop-database "$DESKTOP_DIR" || true; fi
    ok 'Removed AirPlayPC. UxPlay, logs, settings, and pairing data were preserved.'
    exit 0
fi

install_uxplay_from_source() {
    note "Building UxPlay $UXPLAY_TAG from source"
    run sudo pacman -S --needed --noconfirm base-devel cmake git openssl libplist \
        gst-plugins-base gst-plugins-good gst-plugins-bad gst-libav libx11 dbus
    if ((DRY_RUN)); then
        printf '    + git clone --branch %q --depth 1 https://github.com/FDH2/UxPlay.git /tmp/...\n' "$UXPLAY_TAG"
        printf '    + cmake -S /tmp/... -B /tmp/.../build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local\n'
        printf '    + cmake --build /tmp/.../build --parallel\n'
        printf '    + sudo cmake --install /tmp/.../build\n'
        return
    fi
    local work
    work="$(mktemp -d)"
    trap 'rm -rf -- "$work"' RETURN
    git clone --branch "$UXPLAY_TAG" --depth 1 https://github.com/FDH2/UxPlay.git "$work/UxPlay"
    cmake -S "$work/UxPlay" -B "$work/UxPlay/build" \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local
    cmake --build "$work/UxPlay/build" --parallel
    sudo cmake --install "$work/UxPlay/build"
    rm -rf -- "$work"
    trap - RETURN
}

if ! command -v uxplay >/dev/null || ((SOURCE_BUILD)); then
    if ((SOURCE_BUILD)); then
        install_uxplay_from_source
    elif pacman -Si uxplay >/dev/null 2>&1; then
        note 'Installing UxPlay from a configured package repository'
        run sudo pacman -S --needed --noconfirm uxplay
    elif command -v paru >/dev/null; then
        note 'Installing UxPlay from AUR with paru'
        run paru -S --needed --noconfirm uxplay
    elif command -v yay >/dev/null; then
        note 'Installing UxPlay from AUR with yay'
        run yay -S --needed --noconfirm uxplay
    else
        warn 'No repository package or AUR helper found; using the auditable pinned source build.'
        install_uxplay_from_source
    fi
else
    ok "UxPlay is already installed at $(command -v uxplay)."
fi

check_uxplay() {
    if ! command -v uxplay >/dev/null; then
        ((DRY_RUN)) && warn 'UxPlay compatibility will be checked after installation.'
        return
    fi
    local help_output version oldest
    help_output="$(uxplay -h 2>&1 || true)"
    version="$(sed -nE 's/^UxPlay ([0-9]+(\.[0-9]+)+).*/\1/p' <<<"$help_output" | head -n1)"
    if [[ -z "$version" ]]; then
        printf 'Could not determine the installed UxPlay version from uxplay -h output.\n' >&2
        exit 1
    fi
    oldest="$(printf '%s\n' '1.73' "$version" | sort -V | head -n1)"
    if [[ "$oldest" != '1.73' ]]; then
        printf 'UxPlay %s is too old; AirPlayPC requires UxPlay 1.73 or newer.\n' "$version" >&2
        exit 1
    fi
    for option in -scrsv -vrtp; do
        if ! grep -q -- "$option" <<<"$help_output"; then
            printf 'The installed UxPlay %s build lacks required option %s. Install a full 1.73+ build.\n' "$version" "$option" >&2
            exit 1
        fi
    done
    ok "UxPlay $version provides the required -scrsv and -vrtp options."
}

check_uxplay

note 'Installing runtime dependencies'
packages=(gst-plugins-base gst-plugins-good gst-plugins-bad gst-libav iproute2 avahi)
if ((!NO_GUI)); then packages+=(gtk3 gtk4 python-gobject gst-plugin-gtk4 wmctrl bluez bluez-utils python-dbus-fast curl); fi
# --noconfirm keeps non-interactive runs (CI, curl | bash) from aborting at
# the first pacman prompt, consistent with the source-build path above.
run sudo pacman -S --needed --noconfirm "${packages[@]}"

if ((!NO_GUI)); then
    note 'Enabling Bluetooth for AssistiveTouch control'
    run sudo systemctl enable --now bluetooth.service
fi

if command -v uxplay >/dev/null && ldd "$(command -v uxplay)" 2>/dev/null | grep -Eq 'libdns_sd|libavahi-compat-libdns_sd'; then
    note 'Enabling Avahi for this DNS-SD-linked UxPlay build'
    run sudo systemctl enable --now avahi-daemon.service
elif command -v uxplay >/dev/null; then
    ok 'UxPlay uses its built-in mDNS responder; Avahi does not need to be enabled.'
else
    ok 'The installed UxPlay build will determine whether Avahi is needed.'
fi

note 'Configuring the active firewall'
firewall_found=0
if command -v firewall-cmd >/dev/null && systemctl is-active --quiet firewalld.service; then
    firewall_found=1
    if ((DRY_RUN)); then
        run sudo firewall-cmd --permanent --get-services
        run sudo firewall-cmd --permanent --new-service=pcairplay
    elif ! sudo firewall-cmd --permanent --get-services | tr ' ' '\n' | grep -qx pcairplay; then
        run sudo firewall-cmd --permanent --new-service=pcairplay
    fi
    run sudo firewall-cmd --permanent --service=pcairplay --set-description='AirPlayPC receiver and mDNS discovery'
    run sudo firewall-cmd --permanent --service=pcairplay --add-port=5353/udp
    run sudo firewall-cmd --permanent --service=pcairplay --add-port=7000-7002/tcp
    run sudo firewall-cmd --permanent --service=pcairplay --add-port=7000-7002/udp
    run sudo firewall-cmd --permanent --add-service=pcairplay
    run sudo firewall-cmd --reload
    ok 'Opened the dedicated AirPlayPC service in firewalld.'
fi
if command -v ufw >/dev/null && ufw_is_active; then
    firewall_found=1
    run sudo ufw allow 5353/udp comment AirPlayPC-mDNS
    run sudo ufw allow 7000:7002/tcp comment AirPlayPC
    run sudo ufw allow 7000:7002/udp comment AirPlayPC
    ok 'Opened mDNS and receiver ports in UFW.'
fi
if ((firewall_found == 0)); then
    if systemctl is-active --quiet nftables.service 2>/dev/null; then
        warn 'nftables.service is active but has no safe generic rule-editing interface.'
        warn 'Allow UDP 5353 and TCP/UDP 7000-7002 in your nftables policy.'
    else
        ok 'No active firewalld or UFW service found; no firewall changes needed.'
    fi
fi

note 'Installing AirPlayPC for the current user'
run install -d "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR" "$ICON_DIR" "$SYSTEMD_DIR"
run install -m 0644 "$SCRIPT_DIR/pcairplay_common.py" "$APP_DIR/pcairplay_common.py"
run install -m 0755 "$SCRIPT_DIR/pcairplay.py" "$APP_DIR/pcairplay.py"
run ln -sfn "$APP_DIR/pcairplay.py" "$BIN_DIR/pcairplay"
if ((!NO_GUI)); then
    run install -m 0755 "$SCRIPT_DIR/airplay-ui.py" "$APP_DIR/airplay-ui.py"
    run install -m 0755 "$SCRIPT_DIR/frame-mirror.py" "$APP_DIR/frame-mirror.py"
    run install -m 0755 "$SCRIPT_DIR/assistive-control.py" "$APP_DIR/assistive-control.py"
    run ln -sfn "$APP_DIR/airplay-ui.py" "$BIN_DIR/pcairplay-ui"
    run ln -sfn "$APP_DIR/assistive-control.py" "$BIN_DIR/pcairplay-control"
    run install -m 0644 "$SCRIPT_DIR/pcairplay.desktop" "$DESKTOP_DIR/pcairplay.desktop"
    run install -m 0644 "$SCRIPT_DIR/pcairplay-diagnostics.desktop" "$DESKTOP_DIR/pcairplay-diagnostics.desktop"
    run install -m 0644 "$SCRIPT_DIR/pcairplay-control.desktop" "$DESKTOP_DIR/pcairplay-control.desktop"
    if ((DRY_RUN)); then
        printf '    + rewrite Exec=/TryExec= in the installed .desktop files to %q\n' "$BIN_DIR"
    else
        # The shipped .desktop files use bare command names, which a PKGBUILD
        # install resolves via /usr/bin. A user-local install lives in
        # ~/.local/bin, which the systemd user manager and several Wayland
        # sessions do NOT put on PATH - without absolute paths the menu
        # launchers fail with "command not found" after a clean install.
        sed -i "s|^Exec=pcairplay-ui$|Exec=$BIN_DIR/pcairplay-ui|; s|^TryExec=.*$|TryExec=$BIN_DIR/pcairplay-ui|" \
            "$DESKTOP_DIR/pcairplay.desktop"
        sed -i "s|^Exec=pcairplay-control --start$|Exec=$BIN_DIR/pcairplay-control --start|; s|^TryExec=.*$|TryExec=$BIN_DIR/pcairplay-control|" \
            "$DESKTOP_DIR/pcairplay-control.desktop"
        sed -i "s|^Exec=pcairplay doctor$|Exec=$BIN_DIR/pcairplay doctor|; s|^TryExec=.*$|TryExec=$BIN_DIR/pcairplay|" \
            "$DESKTOP_DIR/pcairplay-diagnostics.desktop"
    fi
    run install -m 0644 "$SCRIPT_DIR/pcairplay.svg" "$ICON_DIR/pcairplay.svg"

    note 'Installing pinned Bluetooth HID helper'
    if ((DRY_RUN)); then
        printf '    + curl --fail --location %q --output /tmp/%q\n' "$BLUEZ_PERIPHERAL_URL" "$BLUEZ_PERIPHERAL_WHEEL"
        printf '    + verify SHA-256 %s and extract into %q\n' "$BLUEZ_PERIPHERAL_SHA256" "$APP_DIR/vendor"
    else
        wheel_file="$(mktemp --suffix=.whl)"
        trap 'rm -f -- "$wheel_file"' EXIT
        curl --fail --location --silent --show-error "$BLUEZ_PERIPHERAL_URL" --output "$wheel_file"
        printf '%s  %s\n' "$BLUEZ_PERIPHERAL_SHA256" "$wheel_file" | sha256sum --check --status || {
            printf 'Bluetooth helper SHA-256 verification failed.\n' >&2
            exit 1
        }
        rm -rf -- "$APP_DIR/vendor"
        install -d "$APP_DIR/vendor"
        python -m zipfile -e "$wheel_file" "$APP_DIR/vendor"
        rm -rf -- "$APP_DIR/vendor/tests"
        rm -f -- "$wheel_file"
        trap - EXIT
    fi
fi
run install -m 0644 "$SCRIPT_DIR/pcairplay.service" "$SYSTEMD_DIR/pcairplay.service"
# Best-effort refreshes: failing here (e.g. no user systemd running) must not
# abort the whole install AFTER every file is already in place.
if command -v systemctl >/dev/null; then run systemctl --user daemon-reload || true; fi
if command -v update-desktop-database >/dev/null && ((!NO_GUI)); then run update-desktop-database "$DESKTOP_DIR" || true; fi

note 'Verification'
if ((DRY_RUN)); then
    printf '    + %q self-test\n' "$APP_DIR/pcairplay.py"
    if ((!NO_GUI)); then printf '    + %q --self-test\n' "$APP_DIR/assistive-control.py"; fi
else
    "$APP_DIR/pcairplay.py" self-test
    if ((!NO_GUI)); then "$APP_DIR/assistive-control.py" --self-test; fi
fi
ok 'AirPlayPC is installed.'
printf '\nRun “pcairplay doctor”, then open AirPlayPC from the application menu or run “pcairplay start”.\n'
if ((!NO_GUI)); then printf 'For an in-app mirror and phone control, open “AirPlayPC Mirror & Control” or run “pcairplay-control”.\n'; fi
