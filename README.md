# Coolify host maintenance

Reusable maintenance for **one or more independent Hetzner hosts managed by Coolify Cloud**. Each host maintains itself. No second server, object-storage bucket, restic repository or watchdog is required.

Keep the shared code in this GitHub repo and add one non-secret JSON file per host. Install once, configure credentials on the host, activate. Routine patching and recovery then run without GitHub Actions or your laptop being online.

**Start here:** [SETUP.md](docs/SETUP.md).

## Automatic behaviour

| Task                       | Default behaviour                                                                                                                                                               |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Maintenance                | Daily at 03:30 Europe/Dublin, with up to 5 minutes jitter                                                                                                                       |
| Backup gate                | By default verify Hetzner backups enabled, server identity matches and an available backup is at most 48 hours old; `backup_policy: allow_missing` explicitly waives backup availability/freshness |
| Pre-update checks          | Check disk space/inodes, required mounts and configured app health                                                                                                              |
| Updates                    | Ubuntu packages from existing apt repositories; Docker Engine/CLI within the configured major, plus installed containerd/Compose/Buildx packages                                |
| Reboot                     | Every successful maintenance run, even if Ubuntu does not request it                                                                                                            |
| Boot recovery              | Docker restart policies bring apps up; repair our interrupted temporary package holds; verify app health                                                                        |
| Monitoring                 | Roughly every 60 minutes after the previous check finishes; first check 12 minutes after boot                                                                                  |
| Recovery                   | Three unhealthy resource checks → restart existing matching containers, at most once/resource/hour; Docker outage → one daemon restart/6h, then at most one recovery reboot/24h |
| Alerts                     | SMTP to your existing inbox and/or a Discord-compatible webhook; queued retry on delivery failure; six-hour reminder limit per issue                                            |
| Monthly report             | First of each month at 09:00 Europe/Dublin; persistent systemd timer catches up after downtime and retries Discord delivery failures |
| Provider backup monitoring | Recheck backup freshness every 6 hours while monitoring, and before every update                                                                                                |

Healthy routine work is quiet. There is no automated app-image deployment, schema migration, volume deletion, image pruning, OS release upgrade, hard reset or disk restore. Automatic maintenance uses a host lock and avoids running concurrently with its own monitor/installer. It cannot lock Coolify Cloud or your CI pipeline: keep deployments outside the maintenance window.

The monthly Discord report summarizes tracked maintenance, monitoring, incidents, recovery actions, backup posture and a current app/disk snapshot. It explicitly marks partial coverage until a complete tracked month is available; monitoring intervals are observations, not uptime measurements. The report is considered delivered only when the configured webhook accepts it, regardless of SMTP status.

## What you need

- Your current Hetzner host, Ubuntu 22.04, 24.04 or 26.04 LTS, systemd and existing official Docker CE packages. Other OS/releases require validation before use. Ubuntu 26.04 is newly enabled based on the reported host/package combination and still needs a supervised maintenance-cycle validation before relying on unattended updates.
- By default, Hetzner daily server backups enabled with the first successful backup complete. To proceed without them, set `backup_policy` to `allow_missing` in the host profile and accept the risk of unrecoverable data loss after a failed update or disk loss.
- A **read-only** Hetzner project API token for checking server identity and backup status. This repo never invokes write/reset/rebuild API endpoints. Even with `allow_missing`, the token and matching server identity are required; API/network errors still block maintenance.
- An existing SMTP account with SMTP credentials/app password **or** a webhook accepting `{"content":"message"}`. Neither option needs another hosted server.
- Persistent `always`/`unless-stopped` restart policies for important containers, configured in Coolify/Compose so redeployments preserve them.

Coolify's host-unreachable notifications are an additional outside observer. **They do not automatically relay this script's custom errors.** SMTP/webhook settings here handle backup, update and health-check failures. A fully frozen host cannot run this script or send its own alert; keep Coolify Cloud unreachable notifications enabled. Kernel freeze/failed boot still needs a manual Hetzner reset/rescue.

## GitHub repo structure

```text
hosts/                 One non-secret profile per host
src/hostcare.py         Shared maintenance/recovery implementation
scripts/install.sh     Install/update on a host; preserve credentials
scripts/activate.sh    Verify readiness and enable automation
scripts/deploy.sh      Copy code + chosen profile from your Mac over SSH
scripts/discover.py    Generate a starter profile from current containers
scripts/disable.sh     Disable automation and restore previous apt timers
secrets.example.json   Empty template; real credentials stay on each host
tests/                 Mocked safety tests; no real reboots/updates
.github/workflows/     Validate syntax and tests on pushes/PRs
```

Start with a private repo if hostnames/IPs/resource IDs are personal. GitHub is for versioning and validation; the workflow does not SSH into production or deploy code automatically.

## Quick path

After following the detailed setup guide and creating `hosts/coolify-01.json`:

```bash
bash scripts/deploy.sh coolify-01 root@YOUR_HOST
ssh root@YOUR_HOST
sudoedit /etc/hostcare/secrets.json
bash /opt/coolify-host-maintenance/scripts/activate.sh
```

For another host, add `hosts/coolify-02.json`, configure that server's Hetzner backups/token/email, and repeat. Give hosts different maintenance times if you do not want simultaneous downtime.

## Scope of backup protection

This version checks **provider disk backups only** when `backup_policy` is `required` (the default). Hetzner backups exclude attached Volumes and do not guarantee consistency for running databases. The gate refuses hosts with attached Hetzner Volumes rather than pretending they are covered, including when `backup_policy` is `allow_missing`. Remote/NFS/plugin storage is also outside provider disk protection; do not use this profile for such data without a separate backup design.

The scripts neither stop apps for provider backups nor choose their timing. With required backups, expect up to 48 hours of data exposure at the gate's default tolerance, not a fresh pre-upgrade database backup. With `allow_missing`, maintenance may run with no provider restore point at all. Provider freshness checks prove an available backup exists, **not that it has been restored successfully**. For valuable data, add native database backups and test restoring them. Never treat a second copy on the same disk as protection against losing that disk.

## Updates to this repo and hosts

```bash
git pull --ff-only
bash scripts/deploy.sh coolify-01 root@YOUR_HOST
```

The installer keeps `/etc/hostcare/secrets.json` and recovery state. It backs up the previous profile as `config.previous.json`. Already enabled hosts resume timers only if the new profile passes preflight; failed preflight leaves them paused and reports the error. Fix/redeploy or restore the previous profile and activate again. There is no automatic git pull as root: an upstream branch change cannot silently become production host code.

Docker major version is captured automatically when a profile sets `docker_major` to `0`. After initial installation, set that actual major in the committed profile for an explicit policy. A future Engine major requires one policy change after review; Ubuntu LTS upgrades are also deliberate operations. Existing apt holds are respected; temporary Docker holds are recorded before changes and repaired after interruption.

## Operations

```bash
sudo hostcare status
sudo hostcare preflight
sudo hostcare alert-test
sudo systemctl list-timers 'hostcare-*'
sudo journalctl -u hostcare-maintenance.service -u hostcare-monitor.service -u hostcare-boot.service --since today
```

If backups are not ready, `sudo bash /opt/coolify-host-maintenance/scripts/activate.sh --monitor-only` enables health monitoring and boot recovery without changing Ubuntu's apt scheduler or enabling Hostcare package maintenance. Switch to normal activation after the backup gate passes.

For the first end-to-end maintenance run, when downtime is acceptable:

```bash
sudo systemctl start --no-block hostcare-maintenance.service
```

That command upgrades packages and reboots. Package operations intentionally have no timeout that would kill dpkg halfway through. A stuck upgrade requires checking apt/dpkg and service logs; another Hostcare job will not fight it. We do not blindly run `dpkg --configure -a` or downgrade packages after failure.

To disable automation, without deleting config/state:

```bash
sudo bash /opt/coolify-host-maintenance/scripts/disable.sh
```

Disabling refuses while maintenance is active, restores previous apt-timer enabled/active settings and removes only this tool's apt override. If those timers were disabled before installation, they stay disabled. It does not stop an active package transaction, change SSH/firewall rules, remove Docker or delete app data.

## Verification and limits

Mocked safety tests, Python/Bash syntax checks and generated systemd unit verification cover monthly reporting and delivery, supported Ubuntu releases, hourly monitoring, required and optional backup policies, monitor-only activation, webhook behavior, backup identity/freshness/pagination, attached-volume refusal, alert queues, recovery limits, TLS probes, reboot verification and interrupted package holds. This repo has **not** been installed or tested against your live host, credentials or provider account.

Before enabling on an important host, test a disposable clone or a restore from a provider backup. Then exercise one planned reboot, verify every app and its persistence, confirm a real alert reaches you, and inspect the next scheduled run. Mocked tests cannot prove live backup restorability, Docker compatibility or successful delivery to a phone.

Run local checks:

```bash
for file in scripts/*.sh; do bash -n "$file"; done
python3 -m py_compile src/hostcare.py scripts/discover.py
python3 -m unittest discover -s tests -v
```

See [RECOVERY.md](docs/RECOVERY.md) for when the host cannot repair itself.

## Primary references checked 21 October 2026

- [Coolify server patching](https://coolify.io/docs/core/infrastructure/servers/server-patching): the built-in patching interface remains manual.
- [Docker Ubuntu installation and upgrades](https://docs.docker.com/engine/install/ubuntu/): versioned apt packages.
- [Hetzner backup overview](https://docs.hetzner.com/cloud/servers/backups-snapshots/overview/): daily disk backups and attached-volume exclusions.
- [Hetzner backup FAQ](https://docs.hetzner.com/cloud/servers/backups-snapshots/faq/): running-system consistency caveat and restore behaviour.
- [Hetzner API](https://docs.hetzner.cloud/reference/cloud): read-only server/image inspection.
