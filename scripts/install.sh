#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run this installer with sudo/root.'; exit 1; }
[[ $# -eq 1 && -f $1 ]] || { echo 'Usage: sudo bash scripts/install.sh hosts/HOST.json'; exit 1; }
source /etc/os-release
source "$(dirname -- "${BASH_SOURCE[0]}")/platform.sh"
hostcare_supported_os "$ID" "$VERSION_ID" || {
  echo 'Supported: Ubuntu 22.04, 24.04 or 26.04 LTS with Docker CE already configured by Coolify.'; exit 1;
}
for pkg in docker-ce docker-ce-cli; do
  [[ $(dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null) == 'install ok installed' ]] || {
    echo 'Requires existing official Docker CE apt packages; repository migration is not automated.'; exit 1;
  }
done
if [[ -e /etc/systemd/system/hostcare-restore.timer ]]; then
  echo 'Older restic/watchdog bundle detected. Remove its units before using this simplified repo.'; exit 1
fi
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
profile=$(realpath "$1")
# Package installation happens before handing over the scheduler. Never kill dpkg.
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
if ! command -v python3 >/dev/null || ! command -v curl >/dev/null; then
  apt-get -o DPkg::Lock::Timeout=300 update
  apt-get -o DPkg::Lock::Timeout=300 install -y python3 curl ca-certificates
fi
# Validate JSON/calendar before changing installed units or stopping timers.
python3 - "$profile" <<'PY'
import json,sys,subprocess
from pathlib import Path
c=json.loads(Path(sys.argv[1]).read_text())
subprocess.run(['systemd-analyze','calendar',c['maintenance_calendar']],check=True,stdout=subprocess.DEVNULL)
PY
# Avoid replacing code while a Hostcare job is running. All jobs share this lock.
install -d -m 0700 /etc/hostcare /var/lib/hostcare
exec 9>/var/lib/hostcare/lock
flock -n 9 || { echo 'Hostcare is busy. Wait for maintenance/monitor to finish and retry.'; exit 1; }
was_enabled=false
if systemctl is-enabled --quiet hostcare-maintenance.timer 2>/dev/null; then was_enabled=true; fi
was_monitor_only=false
if ! $was_enabled && systemctl is-enabled --quiet hostcare-monitor.timer 2>/dev/null; then was_monitor_only=true; fi
systemctl stop hostcare-maintenance.timer hostcare-monitor.timer hostcare-summary.timer 2>/dev/null || true
install -m 0755 "$repo_dir/src/hostcare.py" /usr/local/sbin/hostcare
if [[ -e /etc/hostcare/config.json ]]; then
  cp -p /etc/hostcare/config.json /etc/hostcare/config.previous.json
fi
install -m 0600 "$profile" /etc/hostcare/config.json
if [[ ! -e /etc/hostcare/secrets.json ]]; then
  install -m 0600 "$repo_dir/secrets.example.json" /etc/hostcare/secrets.json
fi
python3 - <<'PY'
import json,subprocess
from pathlib import Path
p=Path('/etc/hostcare/config.json');c=json.loads(p.read_text())
if c.get('docker_major',0)==0:
    c['docker_major']=int(subprocess.check_output(['docker','version','--format','{{.Server.Version}}'],text=True).split('.')[0])
p.write_text(json.dumps(c,indent=2)+'\n')
# Store the effective profile's schedule in the unit, never shell-evaluate it.
schedule=c['maintenance_calendar']
if '\n' in schedule or '\r' in schedule: raise SystemExit('Invalid calendar value')
units={
 'hostcare-maintenance.service': '''[Unit]
Description=Check Hetzner backup, upgrade Ubuntu/Docker and reboot
After=network-online.target docker.service
Wants=network-online.target
OnFailure=hostcare-alert@%n.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/hostcare maintenance
TimeoutStartSec=infinity
TimeoutStopSec=10min
UMask=0077
Nice=10
''',
 'hostcare-maintenance.timer': f'''[Unit]
Description=Nightly maintenance
[Timer]
OnCalendar={schedule}
RandomizedDelaySec=5m
Persistent=false
[Install]
WantedBy=timers.target
''',
 'hostcare-monitor.service': '''[Unit]
Description=Health checks and bounded recovery
After=network-online.target
Wants=network-online.target
OnFailure=hostcare-alert@%n.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/hostcare monitor
TimeoutStartSec=30min
UMask=0077
''',
 'hostcare-monitor.timer': '''[Unit]
Description=Resource recovery loop
[Timer]
OnBootSec=12min
OnUnitInactiveSec=60min
[Install]
WantedBy=timers.target
''',
 'hostcare-summary.service': '''[Unit]
Description=Send monthly Hostcare operations report
After=network-online.target
Wants=network-online.target
OnFailure=hostcare-alert@%n.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/hostcare monthly-summary
TimeoutStartSec=2min
UMask=0077
''',
 'hostcare-summary.timer': '''[Unit]
Description=Monthly Hostcare Discord report
[Timer]
OnCalendar=*-*-01 09:00:00 Europe/Dublin
Persistent=true
[Install]
WantedBy=timers.target
''',
 'hostcare-boot.service': '''[Unit]
Description=Verify application recovery after reboot
After=network-online.target docker.service
Wants=network-online.target docker.service
OnFailure=hostcare-alert@%n.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/hostcare boot
TimeoutStartSec=20min
UMask=0077
[Install]
WantedBy=multi-user.target
''',
 'hostcare-alert@.service': '''[Unit]
Description=Alert on failed Hostcare unit
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/hostcare failure %i
TimeoutStartSec=2min
UMask=0077
'''}
for name,body in units.items():
    path=Path('/etc/systemd/system')/name
    path.write_text(body);path.chmod(0o644)
PY
systemctl daemon-reload
systemctl enable docker.service
if $was_enabled; then
  # Updated profiles are checked before resuming previously enabled scheduling.
  flock -u 9
  if /usr/local/sbin/hostcare preflight; then
    systemctl enable --now hostcare-maintenance.timer hostcare-monitor.timer
    systemctl enable --now hostcare-summary.timer
  else
    echo 'Update installed but timers paused: preflight failed. Fix config then run scripts/activate.sh.'; exit 1
  fi
else
  if $was_monitor_only; then
    flock -u 9
    if /usr/local/sbin/hostcare preflight --monitor-only; then
      systemctl enable hostcare-boot.service
      systemctl enable --now hostcare-monitor.timer
      systemctl enable --now hostcare-summary.timer
      echo 'Updated; monitor-only operation resumed. Ubuntu apt scheduling remains unchanged.'
    else
      echo 'Update installed but monitoring remains paused: preflight failed. Fix config then rerun scripts/activate.sh --monitor-only.'; exit 1
    fi
  else
    echo 'Installed; timers remain off. Configure /etc/hostcare/secrets.json, then run scripts/activate.sh.'
  fi
fi
