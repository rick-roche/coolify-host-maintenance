# From download to an automated host

These instructions use your Mac for Git/SSH and your existing Ubuntu Hetzner server for maintenance. You do not need GitHub Actions runners, another VM, a NAS, S3 or a separate monitoring host.

## 1. Put the downloaded folder in a GitHub repo

Unzip `coolify-host-maintenance.zip`, open Terminal and enter its extracted `coolify-host-maintenance` directory. Create an **empty private repo** called `coolify-host-maintenance` in your GitHub account (do not initialize another README), then run:

```bash
git init -b main
git add .
git commit -m "Add single-host Coolify maintenance"
git remote add origin git@github.com:YOUR_ACCOUNT/coolify-host-maintenance.git
git push -u origin main
```

Use your usual GitHub authentication. No secrets are provided in the download. The included workflow runs tests only; it does not need production SSH credentials.

## 2. Enable backups for your existing server

Recommended: in Hetzner Console, select the correct project and server, open **Backups** and enable daily backups. This is a paid Hetzner feature, not another host. Wait until the first backup is available. By default activation and upgrades refuse until a recent available backup exists. If you intentionally want maintenance without a verified provider backup, set `backup_policy` to `allow_missing` in the profile (step 3); understand that a failed upgrade or disk loss may then be unrecoverable.

Note the server's **numeric ID** and **exact name**. A copied config for another server must not accidentally validate the wrong backup.

Create a **read-only project API token** in Hetzner's project security/API-token settings. Store it in your password manager. It will be entered only on the host in step 5, never in the repo. If you add hosts in another project, create a token for that project. This tool uses GET requests only; no cloud reset/rebuild credentials are required.

If the server has attached Hetzner Volumes, this simplified profile will refuse activation because provider disk backups exclude them. Do not bypass that check: agree a volume backup strategy first. The same protection gap applies to remote/NFS storage even though it is not discoverable as a provider Volume.

## 3. Generate a profile from the existing containers

From the repo directory on your Mac, replacing `YOUR_HOST` with your host's IP or your configured SSH hostname:

```bash
ssh root@YOUR_HOST 'python3 -' < scripts/discover.py > hosts/coolify-01.json
```

This uses existing admin SSH access. Verify the SSH host key when connecting for the first time. If root login is prohibited, run discovery through your existing sudo account, then install using the manual method below. Do not enable root SSH merely to follow this example.

The output includes observed container names/Compose labels and current Docker major, not container environment variables or credentials.

Open `hosts/coolify-01.json` in your editor and:

1. Set `host_name` to a friendly alert name, e.g. `coolify-01`.
2. Replace `hetzner_server_id: 0` with the numeric ID, and `hetzner_server_name` with the exact Hetzner name.
3. Review `resources`. Keep your long-running apps, databases and proxy. Remove temporary build/deploy helpers, intentionally stopped previews, and one-off jobs. Each selector must match at least one expected container.
4. Keep observed Compose project/service filters if they identify a persistent resource. Name-only filters are a fallback and may need changing after Coolify redeploys. Prefer an observed stable resource UUID label where available; never invent a Coolify label key.
5. Add HTTPS origin probes for the important web apps; see below.
6. Add every data filesystem to `disk_paths`; use `required_mounts` for mounts that must exist. These mount checks do not turn unsupported external volumes into backed-up storage.
7. Keep `backup_policy` as `required` (or omit it) for the normal backup gate. Set it to `allow_missing` only if you accept maintenance without a recent Hetzner backup. Hostcare still requires the read-only token, verifies server ID/name, and refuses attached Hetzner Volumes; API/network errors still block maintenance.
8. Leave the default maintenance time at 03:30 Dublin time, or change `maintenance_calendar`. Keep app deployments/other maintenance out of that interval.

Example app resource (replace every placeholder; the URL path must exist):

```json
{
  "name": "now-spinning",
  "filters": ["label=OBSERVED_LABEL_KEY=OBSERVED_RESOURCE_UUID"],
  "probes": [
    {
      "url": "https://YOUR_APP_DOMAIN/ACTUAL_HEALTH_ROUTE",
      "resolve": "YOUR_APP_DOMAIN:443:127.0.0.1",
      "status": [200]
    }
  ]
}
```

The probe connects to the local proxy with the real Host/SNI and validates the certificate. This bypasses Cloudflare cache. Use a publicly trusted origin certificate; if you use a Cloudflare Origin CA certificate, arrange trust/a suitable public-cert health hostname before using this probe. Do not add `-k` or disable TLS checks.

A health route should query persistence where practical. A root-page check is a basic availability signal, not proof that your database works. With no HTTP probe or Docker healthcheck, the tool checks only that the selected container is running. Your existing Now Spinning/Re:Play code is not modified by this repo.

Ensure long-running resources have persistent restart policy `unless-stopped` or `always` in Coolify/Compose. The preflight check rejects other policies. Docker restarts these after a reboot, while Hostcare handles resources that stay unhealthy. Container healthchecks by themselves do not automatically restart unhealthy containers.

Commit your reviewed profile:

```bash
git add hosts/coolify-01.json
git commit -m "Configure coolify-01 maintenance"
git push
```

## 4. Install the code/profile on the host

From your Mac:

```bash
bash scripts/deploy.sh coolify-01 root@YOUR_HOST
```

The script copies only `src`, `scripts`, the empty credential template and this host's profile to `/opt/coolify-host-maintenance`. It runs the installer as root. Your SSH key/port/jump-host settings can stay in `~/.ssh/config`; strict host-key verification remains enabled. IPv4/hostnames are supported by the convenience script; use normal SSH/SCP/manual installation for more specialized connection arrangements.

The installer supports Ubuntu 22.04/24.04/26.04 with official Docker CE packages already present. Ubuntu 26.04 support is newly enabled but has not yet been validated through a complete package-upgrade/reboot cycle; supervise the first maintenance run and verify recovery before relying on automation. It does not migrate repositories or install Coolify. It installs Python/curl only if missing, installs Hostcare/systemd units and preserves existing credentials/state. First installation **does not enable maintenance or change Ubuntu's automatic-update scheduler**.

Manual installation alternative: copy the repo to the host by your usual route, then run on the host:

```bash
sudo bash scripts/install.sh hosts/coolify-01.json
```

If installed manually outside `/opt/coolify-host-maintenance`, run activation/disable from that actual checkout path. The runtime executable/config locations are the same.

## 5. Enter the two credentials/settings on the host

Connect to the host:

```bash
ssh root@YOUR_HOST
sudoedit /etc/hostcare/secrets.json
```

Set `hetzner_read_token` to your read-only token. Configure **one** alert destination:

- **SMTP:** use an existing account's documented SMTP submission endpoint and SMTP password/app password, sender and recipient. Set `implicit_tls: false`, normally with port 587 for STARTTLS; or `implicit_tls: true`, normally port 465 for TLS from connection start. TLS validation is always enabled. Consumer accounts may require two-step verification and an app password; an ordinary sign-in password/OAuth-only account may not work with SMTP login.
- **Webhook:** set `webhook_url` to an existing endpoint accepting `{"content":"message"}`, such as a Discord webhook. Leave it blank if using SMTP. An arbitrary Coolify notification URL or Slack webhook is not assumed compatible with this payload.

SMTP template showing the shape, not working credentials:

```json
{
  "hetzner_read_token": "YOUR_READ_ONLY_TOKEN",
  "webhook_url": "",
  "smtp": {
    "host": "YOUR_SMTP_SERVER",
    "port": 587,
    "implicit_tls": false,
    "username": "YOUR_SMTP_USERNAME",
    "password": "YOUR_SMTP_APP_PASSWORD",
    "from": "YOUR_SENDER_EMAIL",
    "to": "YOUR_ALERT_EMAIL"
  }
}
```

Keep `/etc/hostcare/config.json` and `secrets.json` root-owned mode 0600. The installer sets this; preflight requires it. Never copy real credentials into `secrets.example.json` or a tracked host profile. Gitignore helps avoid accidental commits but is not a secret scanner.

## 6. Activate

On the host:

```bash
bash /opt/coolify-host-maintenance/scripts/activate.sh
```

Activation checks backups, disk/mounts, app health, restart policies and alert delivery. Confirm the test email/webhook reaches you. Then it disables Ubuntu's separate apt timers, installs a managed apt automatic-update/reboot override and enables Hostcare's timers/startup verifier. A single scheduler owns updates and reboots. It never terminates an already running apt transaction; retry after that job finishes.

If you want health monitoring but no package upgrades/reboots, enable **monitor-only** operation:

```bash
bash /opt/coolify-host-maintenance/scripts/activate.sh --monitor-only
```

This checks disk/mounts, Docker, monitored resources and alert delivery without requiring the backup gate. It enables health monitoring and boot recovery, but leaves Ubuntu's apt timers unchanged and keeps Hostcare's maintenance timer disabled. Hostcare will report backup problems through its monitor. If Hostcare already owns apt scheduling, first run `scripts/disable.sh` to restore the previous scheduler, then activate monitor-only.

If instead you want scheduled package maintenance without a current provider backup, set `"backup_policy": "allow_missing"` in the host profile and deploy it. Then use normal activation. Hostcare still checks the Hetzner server identity and refuses attached Hetzner Volumes; token/API failures still block maintenance. A missing or stale backup is recorded in status and logs and warned about, but does not block package updates/reboot. This deliberately accepts the risk that an update or disk failure may be unrecoverable.

Check:

```bash
sudo hostcare status
sudo systemctl list-timers 'hostcare-*'
```

If preflight fails, correct the reported condition and rerun activation. With the default `required` policy, common causes include waiting for the first provider backup; all policies still require a working token, matching server identity, no attached Hetzner Volumes, an appropriately narrow container filter, persistent restart policies, healthy probes and a working alert destination.

## 7. Prove a reboot and keep an outside observer

When downtime is acceptable, run one complete maintenance cycle:

```bash
sudo systemctl start --no-block hostcare-maintenance.service
```

It checks backup freshness, upgrades packages and reboots. After reconnecting:

```bash
sudo hostcare status
sudo journalctl -u hostcare-maintenance.service -u hostcare-boot.service --since today
```

Allow up to 10 minutes for postboot verification. Confirm `last_maintenance` updates, `pending_postboot` becomes false, your apps load and their data remains present. An all-green provider check is not a restore test: when practical test creating a disposable replacement from a backup too.

In Coolify Cloud keep **Server Unreachable/Reachable** notifications configured. They can alert when the host itself is inaccessible; they are distinct from Hostcare SMTP/webhook messages. Optionally disable repetitive **Server Patching** reminders once automation is proven. Intentional reboot downtime may still generate Coolify notifications; do not assume its checks share this scheduler's maintenance window.

On a single host there is no outside process here to reset a frozen kernel, email you if both Linux and SSH are dead, or detect a completely hung maintenance lock while the rest of the server appears reachable. Coolify Cloud is the existing external observer; a fully independent deadman/monitoring service is optional later, not required by this repo. A hung apt transaction may require manual intervention even though the host is reachable.

## 8. Add another host

Back on your Mac:

```bash
ssh root@SECOND_HOST 'python3 -' < scripts/discover.py > hosts/coolify-02.json
```

Fill its server identity, review resources/probes, choose a different time if wanted, commit the profile, then:

```bash
bash scripts/deploy.sh coolify-02 root@SECOND_HOST
```

On the second host, enter its credentials and activate exactly as above. Shared implementation; separate host configuration, state, credentials and recovery budgets. No changes are needed to the first host.

## 9. Change configuration later

Edit the profile in Git, commit, and run `deploy.sh` for that host again. The update reruns preflight before resuming previously enabled scheduling. Existing secrets are never overwritten. If a profile fails preflight, timers remain paused; fix it and redeploy/activate. To revert profile settings immediately on the host:

```bash
sudo cp /etc/hostcare/config.previous.json /etc/hostcare/config.json
sudo bash /opt/coolify-host-maintenance/scripts/activate.sh
```

Use Git to revert code changes and redeploy if necessary. `config.previous.json` restores only profile settings, not package versions or source code.
