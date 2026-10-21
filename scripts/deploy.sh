#!/usr/bin/env bash
set -euo pipefail
[[ $# -eq 2 ]] || { echo 'Usage: bash scripts/deploy.sh HOST_PROFILE root@HOST'; exit 1; }
profile=$1
target=$2
[[ $profile =~ ^[A-Za-z0-9_-]+$ ]] || { echo 'Profile must be letters, numbers, hyphen/underscore'; exit 1; }
[[ $target =~ ^root@[A-Za-z0-9.:-]+$ ]] || { echo 'Use root@hostname or root@IPv4 (existing admin SSH access).'; exit 1; }
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ -f "$repo_dir/hosts/$profile.json" ]] || { echo 'No matching hosts/profile.json'; exit 1; }
# Host key verification remains enabled. SSH config can supply port/key/jump host.
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes "$target" 'umask 077; mkdir -p /opt/coolify-host-maintenance'
# Transfer only code, templates and this profile. No .git or local secrets.
COPYFILE_DISABLE=1 tar -C "$repo_dir" -czf - src scripts secrets.example.json "hosts/$profile.json" |
  ssh -o BatchMode=yes -o StrictHostKeyChecking=yes "$target" 'tar --no-same-owner -xzf - -C /opt/coolify-host-maintenance'
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes "$target" "bash /opt/coolify-host-maintenance/scripts/install.sh /opt/coolify-host-maintenance/hosts/$profile.json"
