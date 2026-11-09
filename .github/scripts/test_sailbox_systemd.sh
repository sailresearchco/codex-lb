#!/usr/bin/env bash
set -euo pipefail

test "$(id -u)" -eq 0
test "$(cat /proc/1/comm)" = systemd
rehearsal_dir=$(mktemp -d /tmp/codexlb-systemd-ci.XXXXXX)
unit=codexlb-ci.service
slice=codexlbci.slice
cleanup() {
    systemctl stop "$unit" "$slice" 2>/dev/null || true
    rm -f "/run/systemd/system/$unit" "/run/systemd/system/$slice"
    systemctl daemon-reload
    rm -rf "$rehearsal_dir"
}
trap cleanup EXIT

cat > "$rehearsal_dir/app.py" <<'PY'
import pathlib
import signal
import sys
import time

signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
counter = pathlib.Path(__file__).with_name("starts")
starts = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(starts))
print(f"fixture start {starts}", flush=True)
if starts == 1:
    raise SystemExit(42)

while True:
    time.sleep(0.1)
PY

python3 - "$rehearsal_dir" <<'PY'
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
source = pathlib.Path("deploy/sailbox")
unit = (source / "codexlb.service").read_text()
unit = unit.replace("Slice=codexlb.slice", "Slice=codexlbci.slice")
unit = unit.replace("WorkingDirectory=/workspace/CodexLB", f"WorkingDirectory={target}")
unit = unit.replace("/workspace/CodexLB/start.py", str(target / "app.py"))
(target / "codexlb-ci.service").write_text(unit)
(target / "codexlbci.slice").write_text((source / "codexlb.slice").read_text())
PY

systemd-analyze verify "$rehearsal_dir/$unit" "$rehearsal_dir/$slice"
install -m 0644 "$rehearsal_dir/$unit" "/run/systemd/system/$unit"
install -m 0644 "$rehearsal_dir/$slice" "/run/systemd/system/$slice"
systemctl daemon-reload
systemctl start "$unit"

wait_for_start() {
    local expected=$1
    for _ in {1..100}; do
        if [[ -f "$rehearsal_dir/starts" ]] &&
            [[ $(cat "$rehearsal_dir/starts") -ge $expected ]] &&
            systemctl is-active --quiet "$unit"; then
            return 0
        fi
        sleep 0.2
    done
    journalctl -u "$unit" --no-pager -n 40
    return 1
}

wait_for_start 2
test "$(systemctl show -p NRestarts --value "$unit")" -ge 1
test "$(systemctl show -p ControlGroup --value "$unit")" = /codexlbci.slice/codexlb-ci.service
systemctl kill --kill-whom=all --signal=KILL "$unit"
wait_for_start 3
test "$(systemctl show -p NRestarts --value "$unit")" -ge 2
journalctl -u "$unit" --no-pager -n 40

systemctl stop "$unit"
stopped_starts=$(cat "$rehearsal_dir/starts")
sleep 6
test "$(cat "$rehearsal_dir/starts")" = "$stopped_starts"
test "$(systemctl show -p ActiveState --value "$unit")" = inactive
test "$(systemctl show -p ExecMainStatus --value "$unit")" -eq 0
