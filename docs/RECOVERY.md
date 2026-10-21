# Recovery and boundaries

## App failure with a reachable host

Check `sudo hostcare status`, `systemctl status docker`, the relevant app's Coolify logs and `journalctl -u hostcare-monitor.service`. After three failed monitor cycles, the monitor restarts the existing matching containers; it never recreates a deleted resource. A bad selector/missing app requires updating the profile or redeploying in Coolify.

If the app is broken by a deployment, use Coolify's normal rollback/redeploy with a known-good immutable image. Check schema compatibility first. Host maintenance does not upgrade application images or roll database schemas back.

## Docker failure

The monitor tries a Docker service restart at most once per six hours. After Docker remains unavailable for more than ten minutes, it can reboot at most once per 24 hours. These action budgets are persisted before issuing commands. Do not delete `/var/lib/hostcare/state.json` to force repeated recovery attempts; diagnose the failure.

Docker startup relies on persistent restart policies. A manually stopped `unless-stopped` container may stay stopped across a reboot; if it is intentionally stopped, remove it from monitored resources while it should be off. Otherwise the monitor will treat it as a failure and try starting/restarting it.

## Host unreachable / failed boot

Use Coolify Cloud's unreachable notice and the Hetzner Console. Request a normal reboot/power reset there if appropriate, then use the console/rescue system if it fails. This repo holds only a read-only provider token and cannot hard reset, rebuild or replace the host.

If restoring is required, preserve the damaged server where possible. Create a replacement from a chosen Hetzner backup, rather than immediately overwriting the only copy. Match CPU architecture, reconnect to Coolify, confirm data, credentials, mounted storage, proxy/TLS and app health. Reapply this host-maintenance repo with the replacement's correct numeric ID/name. Provider backups cannot restore attached Volumes or data outside the server disk.

A live-server disk backup can require database crash recovery and is not a proven application-consistent backup. Restore to an isolated test environment before accepting production writes. Do not run the recovered maintenance scheduler against production identity values unintentionally; stop the Hostcare timers immediately on a test clone and use separate credentials/profile before activation.

## Update failure or stuck package operation

Do not kill apt/dpkg or start a second package manager. Inspect `journalctl -u hostcare-maintenance.service`, `/var/log/apt/term.log` and `/var/log/dpkg.log`. Package operations have no forced timeout. The host lock blocks competing Hostcare jobs, but cannot prevent another administrator/Coolify job from invoking apt or deploying containers.

Temporary Docker apt holds are recorded in state and removed in `finally`, at the next boot or before the next maintenance run. Pre-existing operator holds are preserved. Interrupted dpkg configuration itself is not repaired blindly: use Ubuntu's recovery procedure after checking the actual failure.

If the update changed behaviour incompatibly, there is no automatic package downgrade. Repair the host or recover a known-good disk backup into a replacement after accounting for writes since the backup. A package list is recorded at `/var/lib/hostcare/packages-before.json` for diagnosis; it is not a rollback artifact.

## Alerts absent

Run `sudo hostcare alert-test`. Verify SMTP/app-password permissions, sender/recipient, firewall egress and delivery/spam folders. Webhook delivery and SMTP acceptance do not prove a human received an alert. Failed messages are queued in host state and retried by the monitor while Linux can run. The provider token and webhook/password are never intentionally printed to journals.

Coolify unreachable alerts do not substitute for all of these custom notifications. A host that is alive but has a permanently hung maintenance process can need external deadman monitoring for prompt detection; this simplified version has no separate monitor for that scenario.

## Pause or disable

For a short deployment blackout, stop **timers** rather than killing active services:

```bash
sudo systemctl stop hostcare-maintenance.timer hostcare-monitor.timer
# After work:
sudo systemctl start hostcare-maintenance.timer hostcare-monitor.timer
```

Do not leave timers stopped indefinitely: the tool's apt override is still active. To disable for longer, use `scripts/disable.sh`, which restores the pre-install Ubuntu apt timer settings and removes the override. Existing config/state remain available for later activation.
