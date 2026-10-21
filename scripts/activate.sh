#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run with sudo/root'; exit 1; }
monitor_only=false
if [[ $# -eq 1 && $1 == --monitor-only ]]; then
  monitor_only=true
elif [[ $# -ne 0 ]]; then
  echo 'Usage: scripts/activate.sh [--monitor-only]'; exit 1
fi
if $monitor_only; then
  /usr/local/sbin/hostcare preflight --monitor-only
else
  /usr/local/sbin/hostcare preflight
fi
exec 9>/var/lib/hostcare/lock
flock -n 9 || { echo 'Hostcare job running; retry activation after it finishes.'; exit 1; }
if systemctl is-active --quiet hostcare-maintenance.service; then
  echo 'Maintenance is running; let it finish before activation.'; exit 1
fi
if $monitor_only; then
  if [[ -e /var/lib/hostcare/owns-apt-config ]]; then
    echo 'Hostcare currently owns apt scheduling. Run scripts/disable.sh before switching to monitor-only.'; exit 1
  fi
  systemctl disable --now hostcare-maintenance.timer
  systemctl enable hostcare-boot.service
  systemctl enable --now hostcare-monitor.timer
  echo 'Monitor-only enabled; Ubuntu apt timers are unchanged and Hostcare maintenance is disabled.'
  exit 0
fi
for unit in apt-daily.service apt-daily-upgrade.service; do
  if systemctl is-active --quiet "$unit"; then
    echo "$unit is running; wait for it to finish and retry activation."; exit 1
  fi
done
# Store original scheduler settings once so disabling Hostcare can restore them.
python3 - <<'PY'
import json,subprocess
from pathlib import Path
p=Path('/var/lib/hostcare/apt-before.json')
if not p.exists():
    status={}
    for u in ('apt-daily.timer','apt-daily-upgrade.timer'):
        status[u]={'enabled':subprocess.run(['systemctl','is-enabled','--quiet',u]).returncode==0,
                   'active':subprocess.run(['systemctl','is-active','--quiet',u]).returncode==0}
    p.write_text(json.dumps(status));p.chmod(0o600)
PY
if [[ -e /etc/apt/apt.conf.d/99hostcare && ! -e /var/lib/hostcare/owns-apt-config ]]; then
  echo 'Existing unmanaged /etc/apt/apt.conf.d/99hostcare; inspect before activating.'; exit 1
fi
cat > /etc/apt/apt.conf.d/99hostcare <<'EOF'
// Managed by coolify-host-maintenance
APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Unattended-Upgrade "0";
Unattended-Upgrade::Automatic-Reboot "false";
EOF
touch /var/lib/hostcare/owns-apt-config
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer
python3 - <<'PY'
import json,time
from pathlib import Path
p=Path('/var/lib/hostcare/state.json');s=json.loads(p.read_text()) if p.exists() else {}
s.setdefault('enabled_at',time.time());p.write_text(json.dumps(s,indent=2)+'\n');p.chmod(0o600)
PY
systemctl enable hostcare-boot.service
systemctl enable --now hostcare-maintenance.timer hostcare-monitor.timer
systemctl list-timers 'hostcare-*'
