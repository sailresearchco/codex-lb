#!/bin/sh
# Run after Sail's canonical rootfs/configure-systemd setup and a clean app stop.
# Boot conversion is a separate, explicit Sailbox reboot operation.
set -eu

test "$(id -u)" -eq 0
test -x /usr/local/bin/saild
test -f /etc/sailbox/systemd
test -x /usr/lib/systemd/systemd
test -x /usr/bin/python3
test -f /workspace/CodexLB/start.py
test -f /var/lib/codex-lb/store.db
test -f /var/lib/codex-lb/encryption.key

listeners=$(ss -H -ltn 'sport = :2455')
if [ -n "$listeners" ]; then
    echo 'Stop the existing Codex LB application before staging systemd.' >&2
    exit 1
fi

unit_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
systemd-analyze verify "$unit_dir/codexlb.slice" "$unit_dir/codexlb.service"

install -m 0644 "$unit_dir/codexlb.slice" /etc/systemd/system/codexlb.slice
install -m 0644 "$unit_dir/codexlb.service" /etc/systemd/system/codexlb.service
install -d -m 0755 /etc/systemd/journald.conf.d
install -m 0644 "$unit_dir/zz-codexlb-journal.conf" /etc/systemd/journald.conf.d/zz-codexlb.conf
install -d -m 2755 /var/log/journal
systemd-tmpfiles --create --prefix /var/log/journal
systemctl --no-reload enable codexlb.service
echo 'Codex LB service enabled. Activate the boot conversion with the Sailbox reboot API.'
