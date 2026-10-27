#!/usr/bin/env bash
# lpowerd installer.   sudo ./install.sh            install / upgrade
#                      sudo ./install.sh --uninstall [--purge]
set -euo pipefail

PREFIX=/usr
LIBDIR=$PREFIX/lib/lpowerd
SRC=$(cd "$(dirname "$0")" && pwd)
SLEEP_DIR=/usr/lib/systemd/system-sleep
UNIT_DIR=/etc/systemd/system

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root: sudo $0 $*"

uninstall() {
    say "stopping service"
    systemctl disable --now lpowerd.service 2>/dev/null || true
    pkill -x lpowerd-tray 2>/dev/null || true
    pkill -x lpowerd-gui 2>/dev/null || true
    say "removing files"
    rm -rf "$LIBDIR"
    rm -f $PREFIX/bin/lpowerd $PREFIX/bin/lpctl $PREFIX/bin/lpowerd-tray $PREFIX/bin/lpowerd-gui \
          /usr/share/applications/io.github.lpowerd.Gui.desktop \
          "$UNIT_DIR/lpowerd.service" \
          /usr/share/dbus-1/system.d/io.github.lpowerd.conf \
          /usr/share/dbus-1/system-services/io.github.lpowerd.service \
          /usr/share/polkit-1/actions/io.github.lpowerd.policy \
          "$SLEEP_DIR/lpowerd" \
          /etc/xdg/autostart/lpowerd-tray.desktop
    rm -rf /usr/share/doc/lpowerd
    if [ "${1:-}" = "--purge" ]; then
        rm -rf /var/lib/lpowerd
        say "settings purged"
    else
        say "settings kept in /var/lib/lpowerd (use --purge to remove)"
    fi
    systemctl daemon-reload
    systemctl reload dbus.service 2>/dev/null || true
    say "uninstalled"
}

if [ "${1:-}" = "--uninstall" ]; then
    uninstall "${2:-}"
    exit 0
fi

say "checking dependencies"
missing=()
python3 -c 'import dbus' 2>/dev/null || missing+=(python3-dbus)
python3 -c 'import gi; from gi.repository import GLib' 2>/dev/null || missing+=(python3-gi)
python3 -c 'import gi; gi.require_version("Adw", "1")' 2>/dev/null || missing+=(gir1.2-adw-1)
command -v pkexec >/dev/null || missing+=(polkitd pkexec)
if [ ${#missing[@]} -gt 0 ]; then
    if command -v apt-get >/dev/null; then
        say "installing ${missing[*]}"
        apt-get install -y "${missing[@]}"
    else
        die "please install: ${missing[*]}"
    fi
fi

if ! grep -qi lenovo /sys/class/dmi/id/sys_vendor 2>/dev/null; then
    warn "this does not look like a Lenovo machine; installing anyway"
fi
for svc in tlp auto-cpufreq; do
    if systemctl is-active -q "$svc" 2>/dev/null; then
        warn "$svc is running and may also write charge thresholds/profiles."
        warn "Disable its battery options (TLP: START/STOP_CHARGE_THRESH_BAT*) to avoid fights."
    fi
done

say "installing python package to $LIBDIR"
rm -rf "$LIBDIR"
install -d "$LIBDIR/lpowerd"
install -m 0644 "$SRC"/lpowerd/*.py "$LIBDIR/lpowerd/"
python3 -m compileall -q "$LIBDIR/lpowerd"
for b in lpowerd lpctl lpowerd-tray lpowerd-gui; do
    install -m 0755 "$SRC/bin/$b" "$PREFIX/bin/$b"
done

say "installing service, D-Bus, Polkit and sleep hook"
install -m 0644 "$SRC/data/systemd/lpowerd.service" "$UNIT_DIR/lpowerd.service"
install -Dm 0644 "$SRC/data/dbus/io.github.lpowerd.conf" /usr/share/dbus-1/system.d/io.github.lpowerd.conf
install -Dm 0644 "$SRC/data/dbus/io.github.lpowerd.service" /usr/share/dbus-1/system-services/io.github.lpowerd.service
install -Dm 0644 "$SRC/data/polkit/io.github.lpowerd.policy" /usr/share/polkit-1/actions/io.github.lpowerd.policy
install -Dm 0755 "$SRC/data/system-sleep/lpowerd" "$SLEEP_DIR/lpowerd"
install -Dm 0644 "$SRC/data/autostart/lpowerd-tray.desktop" /etc/xdg/autostart/lpowerd-tray.desktop
install -Dm 0644 "$SRC/data/applications/io.github.lpowerd.Gui.desktop" /usr/share/applications/io.github.lpowerd.Gui.desktop
update-desktop-database -q /usr/share/applications 2>/dev/null || true
install -Dm 0644 "$SRC/README.md" /usr/share/doc/lpowerd/README.md

systemctl reload dbus.service 2>/dev/null || systemctl reload dbus-broker.service 2>/dev/null || true
systemctl daemon-reload
systemctl enable lpowerd.service >/dev/null
systemctl restart lpowerd.service

sleep 1
if systemctl is-active -q lpowerd.service; then
    say "lpowerd is running"
else
    systemctl status --no-pager lpowerd.service || true
    die "service failed to start; see: journalctl -u lpowerd"
fi

# Start the tray for the invoking desktop user right away (autostart covers later logins).
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    uid=$(id -u "$SUDO_USER")
    if [ -S "/run/user/$uid/bus" ] && ! pgrep -u "$uid" -x lpowerd-tray >/dev/null; then
        sudo -u "$SUDO_USER" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
            setsid -f $PREFIX/bin/lpowerd-tray >/dev/null 2>&1 || true
    fi
fi

say "done. Open \"Power Control\" from the app launcher, click the gauge icon in the panel, or run: lpctl status"
