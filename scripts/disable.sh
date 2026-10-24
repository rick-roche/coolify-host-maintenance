#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run with sudo/root'; exit 1; }
if systemctl is-active --quiet hostcare-maintenance.service; then
  echo 'Maintenance is running. Let it finish; do not kill a package transaction.'; exit 1
fi
systemctl disable --now hostcare-maintenance.timer hostcare-monitor.timer hostcare-summary.timer
systemctl disable hostcare-boot.service
if [[ -e /var/lib/hostcare/owns-apt-config ]]; then
  rm -f /etc/apt/apt.conf.d/99hostcare /var/lib/hostcare/owns-apt-config
fi
python3 - <<'PY'
import json,subprocess
from pathlib import Path
p=Path('/var/lib/hostcare/apt-before.json')
if p.exists():
    for unit,status in json.loads(p.read_text()).items():
        if status['enabled']:subprocess.run(['systemctl','enable',unit],check=True)
        if status['active']:subprocess.run(['systemctl','start',unit],check=True)
    p.unlink()
PY
echo 'Hostcare disabled; previous apt timer settings restored. Config/state are retained.'
