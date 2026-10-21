#!/usr/bin/env python3
"""Run on target host; emits a starter NON-SECRET profile, not container env."""
import json
import re
import socket
import subprocess


def get(args):
    return subprocess.check_output(args,text=True).strip()

ids=get(['docker','ps','-q']).split()
if not ids: raise SystemExit('No running containers; deploy apps first')
items=json.loads(get(['docker','inspect',*ids]))
resources=[]; seen=set()
for item in items:
    labels=item['Config'].get('Labels') or {}
    # Group by observed Compose identity, never guess a Coolify UUID label key.
    project=labels.get('com.docker.compose.project')
    service=labels.get('com.docker.compose.service')
    name=item['Name'].lstrip('/')
    if project and service:
        filters=['label=com.docker.compose.project='+project,'label=com.docker.compose.service='+service]
        key=project+'/'+service
    else:
        filters=['name=^/'+re.escape(name)+'$']; key=name
    if key in seen: continue
    seen.add(key)
    resources.append({'name':key,'filters':filters,'probes':[]})
print(json.dumps({
    'host_name':socket.gethostname(),'hetzner_server_id':0,
    'hetzner_server_name':'REPLACE_WITH_EXACT_NAME',
    'docker_major':int(get(['docker','version','--format','{{.Server.Version}}']).split('.')[0]),
    'maintenance_calendar':'*-*-* 03:30:00 Europe/Dublin',
    'backup_max_age_hours':48,'backup_policy':'required','min_free_gib':5,'disk_paths':['/','/var/lib/docker'],
    'required_mounts':[],'recovery_reboot':True,'resources':resources
},indent=2))
