#!/usr/bin/env python3
"""Single-host maintenance; no third-party Python dependencies."""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import smtplib
import socket
import ssl
import subprocess
import sys
import time
from urllib.parse import urlsplit
import urllib.request
from email.message import EmailMessage

CONFIG = Path('/etc/hostcare/config.json')
SECRETS = Path('/etc/hostcare/secrets.json')
ROOT = Path('/var/lib/hostcare')
DOCKER = ('docker-ce', 'docker-ce-cli', 'docker-ce-rootless-extras', 'containerd.io', 'docker-buildx-plugin', 'docker-compose-plugin')


def run(args, timeout=120, check=True, env=None):
    p = subprocess.run(args, text=True, capture_output=True, timeout=timeout, env=env)
    if check and p.returncode:
        raise RuntimeError(f'{args[0]} failed (exit {p.returncode}); see apt/dpkg or service logs')
    return p


def atomic(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2)+'\n'); temp.chmod(0o600)
    with temp.open() as f:
        os.fsync(f.fileno())
    os.replace(temp, path)
    fd = os.open(str(path.parent), os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def request(url, token=''):
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer '+token} if token else {})
    # Never log URL-bearing exceptions (webhook URL may be a credential).
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except Exception:
        raise RuntimeError('Hetzner API request failed; check token/network/provider status') from None


def major(v):
    return int(v.split(':')[-1].split('.')[0])


def select_version(text, allowed):
    choices = [line.split('|')[1].strip() for line in text.splitlines() if '|' in line]
    choices = [v for v in choices if major(v) == allowed]
    if not choices:
        raise RuntimeError('No available Docker package in the configured major')
    best = choices[0]
    for v in choices[1:]:
        if run(['dpkg', '--compare-versions', v, 'gt', best], check=False).returncode == 0:
            best = v
    return best


def validate(c):
    if not isinstance(c.get('host_name'),str) or not c['host_name']:
        raise RuntimeError('Set a host_name for alert messages')
    for key in ('min_free_gib','backup_max_age_hours'):
        if not isinstance(c.get(key),(int,float)) or c[key]<=0:
            raise RuntimeError('Set a positive value for '+key)
    if not isinstance(c.get('disk_paths'),list) or not c['disk_paths']:
        raise RuntimeError('Set disk_paths for every data filesystem')
    if not isinstance(c.get('docker_major'), int) or c['docker_major'] < 20:
        raise RuntimeError('Set docker_major to the currently installed Engine major')
    if not isinstance(c.get('hetzner_server_id'), int) or c['hetzner_server_id'] <= 0:
        raise RuntimeError('Set the numeric Hetzner server ID')
    if not c.get('hetzner_server_name') or 'REPLACE' in c['hetzner_server_name']:
        raise RuntimeError('Set the exact Hetzner server name')
    if not c.get('resources'):
        raise RuntimeError('Configure at least one resource')
    names = [r['name'] for r in c['resources']]
    if len(names) != len(set(names)):
        raise RuntimeError('Resource names must be unique')
    for r in c['resources']:
        if not r.get('filters') or any(not isinstance(f, str) or not f for f in r['filters']):
            raise RuntimeError('Every resource needs nonempty Docker filters')
        for p in r.get('probes', []):
            if not p['url'].startswith('https://'):
                raise RuntimeError('Use HTTPS for origin probes')


class Care:
    def __init__(self, config, secrets, root=ROOT):
        self.c, self.secret, self.root = config, secrets, root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root/'state.json'
        self.s = json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self):
        atomic(self.path, self.s)

    def notify(self, key, message, resolved=False, force=False):
        alerts = self.s.setdefault('alerts', {})
        pending = self.s.setdefault('pending_alerts', {})
        if resolved and key not in alerts:
            pending.pop(key, None); self.save(); return True
        if not force and not resolved and time.time()-alerts.get(key, 0) < 21600:
            return True
        pending[key] = {'message':message, 'resolved':resolved}; self.save()
        text = f'[{self.c["host_name"]}] {message}'
        print(text, flush=True)
        sent = False
        if self.secret.get('webhook_url'):
            try:
                headers={'Content-Type':'application/json'}
                if urlsplit(self.secret['webhook_url']).hostname in ('discord.com','discordapp.com'):
                    headers['User-Agent']='DiscordBot (https://github.com/rick-roche/coolify-host-maintenance, 1.0)'
                req = urllib.request.Request(self.secret['webhook_url'], data=json.dumps({'content':text}).encode(), headers=headers)
                with urllib.request.urlopen(req, timeout=20) as r:
                    sent = 200 <= r.status < 300
            except Exception:
                print('Webhook delivery failed; queued for retry', flush=True)
        smtp = self.secret.get('smtp', {})
        if smtp.get('host'):
            try:
                msg=EmailMessage(); msg['From']=smtp['from']; msg['To']=smtp['to']
                msg['Subject']='Hostcare: '+self.c['host_name']+(' recovered' if resolved else ' needs attention')
                msg.set_content(text)
                cls = smtplib.SMTP_SSL if smtp.get('implicit_tls', False) else smtplib.SMTP
                kwargs = {'host':smtp['host'], 'port':smtp.get('port',587), 'timeout':20}
                if smtp.get('implicit_tls',False): kwargs['context']=ssl.create_default_context()
                with cls(**kwargs) as client:
                    if not smtp.get('implicit_tls',False): client.starttls(context=ssl.create_default_context())
                    if smtp.get('username'): client.login(smtp['username'],smtp['password'])
                    client.send_message(msg)
                sent=True
            except Exception:
                print('SMTP delivery failed; queued for retry', flush=True)
        if sent:
            pending.pop(key,None)
            if resolved: alerts.pop(key,None)
            else: alerts[key]=time.time()
            self.save()
        return sent

    def throttle(self, key, seconds):
        actions=self.s.setdefault('actions',{})
        if time.time()-actions.get(key,0)<seconds: return False
        actions[key]=time.time(); self.save(); return True

    def disks(self):
        for mount in self.c.get('required_mounts',[]):
            if run(['findmnt','-rn','--mountpoint',mount],check=False).returncode:
                raise RuntimeError('Required data mount is missing: '+mount)
        for path in self.c['disk_paths']:
            disk=shutil.disk_usage(path); stat=os.statvfs(path)
            if disk.free < self.c['min_free_gib']*1024**3 or (stat.f_files and stat.f_favail/stat.f_files<.05):
                raise RuntimeError('Disk space/inodes low at '+path)

    def backup_check(self):
        token=self.secret.get('hetzner_read_token','')
        if not token: raise RuntimeError('Configure a read-only Hetzner API token in secrets.json')
        base='https://api.hetzner.cloud/v1'
        server=request(base+'/servers/'+str(self.c['hetzner_server_id']),token)['server']
        if server['name']!=self.c['hetzner_server_name']:
            raise RuntimeError('Hetzner server name/ID mismatch; refusing updates')
        if not server.get('backup_window'):
            raise RuntimeError('Enable Hetzner backups and wait for the first completed backup')
        if server.get('volumes'):
            raise RuntimeError('Attached Hetzner Volumes are not covered by disk backups; add a verified volume-backup strategy before enabling this profile')
        images=[]; page=1
        while page:
            data=request(base+'/images?type=backup&per_page=50&page='+str(page),token)
            images.extend(i for i in data['images'] if i.get('bound_to')==self.c['hetzner_server_id'] and i.get('status')=='available')
            page=data['meta']['pagination'].get('next_page')
        latest=max((dt.datetime.fromisoformat(i['created'].replace('Z','+00:00')).timestamp() for i in images),default=0)
        if latest>time.time()+300 or time.time()-latest>self.c['backup_max_age_hours']*3600:
            raise RuntimeError('No fresh available Hetzner backup for this server')
        self.s['last_backup_check']=time.time(); self.s['backup_created']=latest; self.save()

    def containers(self,r):
        args=['docker','ps','-aq']
        for f in r['filters']: args+=['--filter',f]
        ids=run(args).stdout.split()
        return json.loads(run(['docker','inspect',*ids]).stdout) if ids else []

    def probe(self,p):
        args=['curl','--silent','--show-error','--fail','--max-time','15','--connect-timeout','5','--noproxy','*','--header','Cache-Control: no-cache','--write-out','\n%{http_code}']
        if p.get('resolve'): args+=['--resolve',p['resolve']]
        result=run([*args,p['url']],timeout=20,check=False)
        body,_,status=result.stdout.rpartition('\n')
        return result.returncode==0 and status in [str(x) for x in p.get('status',[200])] and (not p.get('body_regex') or re.search(p['body_regex'],body) is not None)

    def resource_ok(self,r):
        items=self.containers(r)
        return bool(items) and all(x['State']['Running'] and x['State'].get('Health',{}).get('Status','healthy')=='healthy' for x in items) and all(self.probe(p) for p in r.get('probes',[]))

    def healthy(self):
        return all(self.resource_ok(r) for r in self.c['resources'])

    def wait_healthy(self,seconds=600):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            try:
                if self.healthy(): return True
            except Exception: pass
            time.sleep(20)
        return False

    def preflight(self, monitor_only=False):
        self.disks()
        if not monitor_only: self.backup_check()
        run(['docker','info'],timeout=20)
        for r in self.c['resources']:
            items=self.containers(r)
            if not items: raise RuntimeError('Selector matches no containers: '+r['name'])
            for x in items:
                if x['HostConfig']['RestartPolicy']['Name'] not in ('always','unless-stopped'):
                    raise RuntimeError('Set restart policy in persistent Coolify/Compose config: '+r['name'])
        if not self.healthy(): raise RuntimeError('Fix resource health checks before activation')
        if not self.notify('setup-test','Setup alert test; verify receipt in your inbox/phone',force=True):
            raise RuntimeError('No alert destination accepted the test message')
        mode='monitor-only; updates remain disabled' if monitor_only else 'full maintenance'
        print(f'Preflight passed ({mode}). The test confirms delivery acceptance, not human receipt.')

    def upgrade(self):
        env=os.environ.copy(); env.update(DEBIAN_FRONTEND='noninteractive',NEEDRESTART_MODE='a')
        apt=['apt-get','-o','DPkg::Lock::Timeout=300','-o','Dpkg::Options::=--force-confdef','-o','Dpkg::Options::=--force-confold','-y','--no-remove']
        run(['apt-get','-o','DPkg::Lock::Timeout=300','update'],timeout=None,env=env)
        held=set(run(['apt-mark','showhold']).stdout.split())
        installed=[]
        for name in DOCKER:
            p=run(['dpkg-query','-W','-f=${Status}',name],check=False)
            if p.returncode==0 and p.stdout=='install ok installed': installed.append(name)
        version=select_version(run(['apt-cache','madison','docker-ce']).stdout,self.c['docker_major'])
        for name in ('docker-ce','docker-ce-cli'):
            current=run(['dpkg-query','-W','-f=${Version}',name]).stdout
            if run(['dpkg','--compare-versions',version,'lt',current],check=False).returncode==0:
                raise RuntimeError('Docker policy would downgrade installed packages; refusing')
        temporary=[n for n in installed if n not in held]
        # Persistent intent repairs temporary holds after a power loss too.
        self.s['temporary_docker_holds']=temporary; self.save()
        try:
            run(['apt-mark','hold',*installed])
            run([*apt,'upgrade','--with-new-pkgs'],timeout=None,env=env)
            blocked=set(held)
            if held.intersection({'docker-ce','docker-ce-cli','docker-ce-rootless-extras'}):
                blocked.update({'docker-ce','docker-ce-cli','docker-ce-rootless-extras'})
            update=[n for n in installed if n not in blocked]
            if update:
                run(['apt-mark','unhold',*update])
                specs=[n+'='+version if n in ('docker-ce','docker-ce-cli','docker-ce-rootless-extras') else n for n in update]
                run([*apt,'install','--only-upgrade',*specs],timeout=None,env=env)
        finally:
            self.repair_holds()

    def repair_holds(self):
        temporary=self.s.get('temporary_docker_holds',[])
        if temporary:
            run(['apt-mark','unhold',*temporary])
            self.s.pop('temporary_docker_holds',None); self.save()

    def maintenance(self):
        self.s['phase']='preflight'; self.s['maintenance_started']=time.time(); self.save()
        self.repair_holds(); self.disks(); self.backup_check()
        if not self.healthy(): raise RuntimeError('Apps unhealthy before maintenance; skip upgrades')
        atomic(self.root/'packages-before.json',{'packages':run(['dpkg-query','-W']).stdout,'time':time.time()})
        self.s['phase']='packages'; self.save()
        self.upgrade()
        self.s['pending_postboot']=True
        self.s['reboot_boot_id']=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.s['phase']='reboot'; self.save()
        run(['systemctl','reboot'])

    def boot(self):
        self.repair_holds()
        if self.wait_healthy(): self.finish_reboot()
        else: self.notify('postboot','Resources unhealthy after boot; recovery loop will try bounded repairs')

    def finish_reboot(self):
        boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if self.s.get('pending_postboot') and self.s.get('reboot_boot_id')!=boot_id:
            self.s.update(pending_postboot=False,last_maintenance=time.time(),phase='idle'); self.save()
            self.notify('maintenance','Maintenance and reboot completed successfully',resolved=True)
        self.notify('postboot','Resources recovered after boot',resolved=True)

    def monitor(self):
        for key,p in list(self.s.get('pending_alerts',{}).items()):
            self.notify(key,p['message'],p['resolved'])
        try:
            run(['docker','info'],timeout=20)
            self.s.pop('docker_failed_since',None)
            self.notify('docker','Docker recovered',resolved=True)
        except Exception:
            since=self.s.setdefault('docker_failed_since',time.time()); self.save()
            if self.throttle('docker-restart',21600):
                run(['systemctl','restart','docker'],timeout=180,check=False)
            elif time.time()-since>600 and self.c.get('recovery_reboot',True) and self.throttle('recovery-reboot',86400):
                self.notify('docker','Docker still unavailable; requesting recovery reboot (one/24h limit)')
                run(['systemctl','reboot'])
            self.notify('docker','Docker unavailable; bounded recovery is in progress')
            return
        misses=self.s.setdefault('misses',{})
        for r in self.c['resources']:
            name=r['name']
            if self.resource_ok(r):
                misses[name]=0; self.notify('resource-'+name,name+' recovered',resolved=True); continue
            misses[name]=misses.get(name,0)+1; self.save()
            if misses[name]<3: continue
            items=self.containers(r)
            if items and self.throttle('restart-'+name,3600):
                run(['docker','restart','--time','120',*[x['Id'] for x in items]],timeout=600,check=False)
                time.sleep(20)
                if self.resource_ok(r):
                    misses[name]=0; self.notify('resource-'+name,name+' recovered',resolved=True); continue
            self.notify('resource-'+name,name+' still unhealthy or missing after recovery; inspect Coolify')
        try:
            self.disks(); self.notify('disk','Disk/mount health recovered',resolved=True)
        except RuntimeError as e: self.notify('disk',str(e))
        if time.time()-self.s.get('last_backup_check',0)>21600:
            try:
                self.backup_check(); self.notify('backup','Provider backups recovered',resolved=True)
            except RuntimeError as e: self.notify('backup',str(e))
        if time.time()-self.s.get('last_maintenance',self.s.get('enabled_at',time.time()))>48*3600:
            self.notify('maintenance','No successful maintenance plus reboot health verification in 48h')
        if self.healthy(): self.finish_reboot()
        self.s['last_monitor']=time.time(); self.save()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['preflight','monitor','maintenance','boot','status','alert-test','failure'])
    parser.add_argument('unit',nargs='?',default='unknown')
    parser.add_argument('--monitor-only',action='store_true',help='Skip the backup gate for a monitoring-only preflight')
    args=parser.parse_args()
    if args.monitor_only and args.command!='preflight': parser.error('--monitor-only is only valid with preflight')
    if os.geteuid()!=0: raise SystemExit('Run with sudo/root')
    for p in (CONFIG,SECRETS):
        st=p.stat()
        if st.st_uid!=0 or st.st_mode&0o077: raise SystemExit('Configuration must be root-owned mode 0600: '+str(p))
    config=json.loads(CONFIG.read_text()); validate(config)
    secrets=json.loads(SECRETS.read_text())
    ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (ROOT/'lock').open('w') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another Hostcare job owns the lock; check service status before retrying')
            return 0 if args.command=='monitor' else 75
        care=Care(config,secrets)
        try:
            if args.command=='status':
                print(json.dumps({k:care.s.get(k) for k in ('phase','maintenance_started','last_monitor','last_maintenance','backup_created','last_backup_check','pending_postboot')},indent=2))
            elif args.command=='alert-test':
                if not care.notify('manual-test','Manual alert test',force=True): return 1
            elif args.command=='failure': care.notify('service-'+args.unit,'Service failed: '+args.unit)
            elif args.command=='preflight': care.preflight(monitor_only=args.monitor_only)
            else: getattr(care,args.command)()
        except Exception as e:
            message=str(e) if isinstance(e,RuntimeError) else 'Operation failed ('+type(e).__name__+'); inspect logs/configuration'
            care.notify(args.command,message); return 1
    return 0


if __name__=='__main__': sys.exit(main())
