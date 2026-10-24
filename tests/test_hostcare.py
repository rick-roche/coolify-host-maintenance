import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('hostcare',Path(__file__).resolve().parents[1]/'src/hostcare.py')
h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)

def result(stdout='',code=0): return subprocess.CompletedProcess([],code,stdout,'')


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.config={'host_name':'test','hetzner_server_id':123,'hetzner_server_name':'test','docker_major':29,'backup_max_age_hours':48,'min_free_gib':5,'disk_paths':[self.tmp.name],'resources':[{'name':'app','filters':['label=x=y']}],'required_mounts':[]}
        self.c=h.Care(self.config,{'hetzner_read_token':'secret','webhook_url':'https://example.invalid/webhook'},Path(self.tmp.name))
    def tearDown(self): self.tmp.cleanup()

    def test_installer_accepts_supported_ubuntu_lts_releases(self):
        script=Path(__file__).resolve().parents[1]/'scripts/platform.sh'
        for release in ('22.04','24.04','26.04'):
            with self.subTest(release=release):
                result=subprocess.run(['bash','-c',f'source "{script}"; hostcare_supported_os ubuntu {release}'],check=False)
                self.assertEqual(result.returncode,0)

    def test_installer_rejects_unsupported_os_releases(self):
        script=Path(__file__).resolve().parents[1]/'scripts/platform.sh'
        for os_id,release in (('ubuntu','25.10'),('debian','26.04')):
            with self.subTest(os_id=os_id,release=release):
                result=subprocess.run(['bash','-c',f'source "{script}"; hostcare_supported_os {os_id} {release}'],check=False)
                self.assertNotEqual(result.returncode,0)

    def test_monitor_timer_repeats_every_sixty_minutes(self):
        installer=Path(__file__).resolve().parents[1]/'scripts/install.sh'
        text=installer.read_text()
        self.assertIn('OnUnitInactiveSec=60min',text)

    def api(self,**server):
        s={'name':'test','backup_window':'00-04','volumes':[]};s.update(server)
        return {'server':s}
    def image_response(self,images): return {'images':images,'meta':{'pagination':{'next_page':None}}}
    def fresh_image(self,**kwargs):
        v={'bound_to':123,'status':'available','created':h.dt.datetime.now(h.dt.timezone.utc).isoformat()};v.update(kwargs);return v

    def test_fresh_backup_accepted(self):
        with patch.object(h,'request',side_effect=[self.api(),self.image_response([self.fresh_image()])]):
            self.assertTrue(self.c.backup_check())
        self.assertIn('last_backup_check',self.c.s)
        self.assertEqual(self.c.s['backup_status'],'verified')

    def test_allow_missing_policy_accepts_disabled_backups_and_records_status(self):
        self.config['backup_policy']='allow_missing'
        with patch.object(h,'request',return_value=self.api(backup_window=None)) as api:
            self.assertFalse(self.c.backup_check())
        api.assert_called_once()
        self.assertEqual(self.c.s['backup_status'],'missing_allowed')
        self.assertIn('last_backup_check',self.c.s)
        self.assertNotIn('backup_created',self.c.s)

    def test_allow_missing_policy_accepts_stale_backup(self):
        self.config['backup_policy']='allow_missing'
        stale=self.fresh_image(created='2000-01-01T00:00:00Z')
        with patch.object(h,'request',side_effect=[self.api(),self.image_response([stale])]):
            self.assertFalse(self.c.backup_check())
        self.assertEqual(self.c.s['backup_status'],'missing_allowed')

    def test_allow_missing_policy_still_checks_server_identity_and_volumes(self):
        self.config['backup_policy']='allow_missing'
        with patch.object(h,'request',return_value=self.api(name='wrong')):
            with self.assertRaisesRegex(RuntimeError,'mismatch'):self.c.backup_check()
        with patch.object(h,'request',return_value=self.api(volumes=[99])):
            with self.assertRaisesRegex(RuntimeError,'Volumes'):self.c.backup_check()

    def test_allow_missing_policy_still_requires_api_and_token(self):
        self.config['backup_policy']='allow_missing'
        with patch.object(h,'request',side_effect=RuntimeError('API unavailable')):
            with self.assertRaisesRegex(RuntimeError,'API unavailable'):self.c.backup_check()
        self.c.secret['hetzner_read_token']=''
        with patch.object(h,'request') as api:
            with self.assertRaisesRegex(RuntimeError,'read-only Hetzner API token'):self.c.backup_check()
        api.assert_not_called()

    def test_allow_missing_backup_state_does_not_notify(self):
        self.config['backup_policy']='allow_missing'
        with patch.object(h,'run',return_value=result()),patch.object(self.c,'resource_ok',return_value=True), \
             patch.object(self.c,'disks'),patch.object(self.c,'backup_check',return_value=False), \
             patch.object(self.c,'notify') as notify,patch.object(self.c,'finish_reboot'):
            self.c.monitor()
        self.assertFalse(any(call.args[0]=='backup' for call in notify.call_args_list))

    def test_allow_missing_clears_queued_routine_backup_notice_without_retrying_it(self):
        self.config['backup_policy']='allow_missing'
        message='No fresh provider backup; maintenance continues under backup_policy=allow_missing'
        self.c.s['pending_alerts']={'backup':{'message':message,'resolved':False}}
        self.c.s['alerts']={'backup':time.time()}
        with patch.object(h,'run',return_value=result()),patch.object(self.c,'resource_ok',return_value=True), \
             patch.object(self.c,'disks'),patch.object(self.c,'backup_check',return_value=False), \
             patch.object(self.c,'notify') as notify,patch.object(self.c,'finish_reboot'):
            self.c.monitor()
        self.assertNotIn('backup',self.c.s['pending_alerts'])
        self.assertNotIn('backup',self.c.s['alerts'])
        self.assertFalse(any(call.args[0]=='backup' for call in notify.call_args_list))

    def test_allow_missing_still_notifies_on_backup_api_errors(self):
        self.config['backup_policy']='allow_missing'
        with patch.object(h,'run',return_value=result()),patch.object(self.c,'resource_ok',return_value=True), \
             patch.object(self.c,'disks'),patch.object(self.c,'backup_check',side_effect=RuntimeError('Hetzner API request failed')), \
             patch.object(self.c,'notify') as notify,patch.object(self.c,'finish_reboot'):
            self.c.monitor()
        self.assertTrue(any(call.args[:2]==('backup','Hetzner API request failed') for call in notify.call_args_list))

    def test_unknown_backup_policy_is_rejected(self):
        self.config['backup_policy']='ignore'
        with self.assertRaisesRegex(RuntimeError,'backup_policy'):h.validate(self.config)
    def test_disabled_backups_refused(self):
        with patch.object(h,'request',return_value=self.api(backup_window=None)):
            with self.assertRaisesRegex(RuntimeError,'Enable Hetzner'):self.c.backup_check()
    def test_attached_volumes_refused(self):
        with patch.object(h,'request',return_value=self.api(volumes=[99])):
            with self.assertRaisesRegex(RuntimeError,'Volumes'):self.c.backup_check()
    def test_wrong_server_refused(self):
        with patch.object(h,'request',return_value=self.api(name='other')):
            with self.assertRaisesRegex(RuntimeError,'mismatch'):self.c.backup_check()
    def test_unrelated_backup_cannot_satisfy_gate(self):
        with patch.object(h,'request',side_effect=[self.api(),self.image_response([self.fresh_image(bound_to=999)])]):
            with self.assertRaisesRegex(RuntimeError,'fresh'):self.c.backup_check()
    def test_stale_backup_refused(self):
        with patch.object(h,'request',side_effect=[self.api(),self.image_response([self.fresh_image(created='2000-01-01T00:00:00Z')])]):
            with self.assertRaisesRegex(RuntimeError,'fresh'):self.c.backup_check()
    def test_backup_pagination(self):
        first={'images':[],'meta':{'pagination':{'next_page':2}}}
        with patch.object(h,'request',side_effect=[self.api(),first,self.image_response([self.fresh_image()])]) as api:self.c.backup_check()
        self.assertIn('page=2',api.call_args.args[0])
    def test_backup_failure_prevents_upgrade_and_reboot(self):
        with patch.object(self.c,'backup_check',side_effect=RuntimeError('bad backup')),patch.object(self.c,'upgrade') as upgrade,patch.object(h,'run') as run:
            with self.assertRaises(RuntimeError):self.c.maintenance()
        upgrade.assert_not_called();run.assert_not_called()

    def test_allow_missing_backup_policy_allows_maintenance(self):
        self.config['backup_policy']='allow_missing'
        with patch.object(self.c,'repair_holds'),patch.object(self.c,'disks'), \
             patch.object(h,'request',return_value=self.api(backup_window=None)),patch.object(self.c,'healthy',return_value=True), \
             patch.object(self.c,'upgrade') as upgrade,patch.object(h,'run',return_value=result()), \
             patch.object(h.Path,'read_text',return_value='boot-id'):
            self.c.maintenance()
        upgrade.assert_called_once_with()
        self.assertTrue(self.c.s['pending_postboot'])
        self.assertEqual(self.c.s['backup_status'],'missing_allowed')

    def test_allow_missing_backup_policy_allows_full_preflight(self):
        self.config['backup_policy']='allow_missing'
        container={'HostConfig':{'RestartPolicy':{'Name':'unless-stopped'}}}
        with patch.object(self.c,'disks'),patch.object(self.c,'backup_check',return_value=False) as backup, \
             patch.object(self.c,'containers',return_value=[container]),patch.object(self.c,'healthy',return_value=True), \
             patch.object(self.c,'notify',return_value=True),patch.object(h,'run'):
            self.c.preflight()
        backup.assert_called_once_with()

    def test_monitor_only_preflight_skips_backup_gate_but_checks_readiness(self):
        container={'HostConfig':{'RestartPolicy':{'Name':'unless-stopped'}}}
        with patch.object(self.c,'disks') as disks, patch.object(self.c,'backup_check') as backup, \
             patch.object(self.c,'containers',return_value=[container]), patch.object(self.c,'healthy',return_value=True), \
             patch.object(self.c,'notify',return_value=True), patch.object(h,'run'):
            self.c.preflight(monitor_only=True)
        disks.assert_called_once_with()
        backup.assert_not_called()
    def test_bad_health_prevents_upgrade(self):
        with patch.object(self.c,'backup_check'),patch.object(self.c,'healthy',return_value=False),patch.object(self.c,'upgrade') as upgrade:
            with self.assertRaisesRegex(RuntimeError,'unhealthy'):self.c.maintenance()
        upgrade.assert_not_called()
    def test_restart_budget_persists_across_reboot(self):
        self.assertTrue(self.c.throttle('reboot',86400))
        new=h.Care(self.config,{},Path(self.tmp.name))
        self.assertFalse(new.throttle('reboot',86400))
    def test_missing_mount_blocks_updates(self):
        self.config['required_mounts']=['/mnt/data']
        with patch.object(h,'run',return_value=result(code=1)):
            with self.assertRaisesRegex(RuntimeError,'missing'):self.c.disks()
    def test_alert_delivery_failure_queues(self):
        with patch.object(h.urllib.request,'urlopen',side_effect=OSError('offline')):
            self.assertFalse(self.c.notify('x','failure'))
        self.assertIn('x',self.c.s['pending_alerts']);self.assertNotIn('x',self.c.s['alerts'])

    def test_discord_webhook_sends_discord_user_agent(self):
        from unittest.mock import MagicMock
        self.c.secret['webhook_url']='https://discord.com/api/webhooks/123/secret'
        response=MagicMock();response.__enter__.return_value.status=204
        with patch.object(h.urllib.request,'urlopen',return_value=response) as send:
            self.assertTrue(self.c.notify('discord-test','delivery test',force=True))
        request=send.call_args.args[0]
        self.assertEqual(request.get_header('User-agent'),
                         'DiscordBot (https://github.com/rick-roche/coolify-host-maintenance, 1.0)')
    def test_recovery_drops_unsent_stale_alert(self):
        self.c.s['pending_alerts']={'x':{'message':'failure','resolved':False}}
        self.c.notify('x','recovered',True)
        self.assertNotIn('x',self.c.s['pending_alerts'])
    def test_docker_versions_stay_on_approved_major(self):
        text='docker-ce | 5:30.0.0-1 | stable\ndocker-ce | 5:29.9.0-1 | stable'
        self.assertEqual(h.select_version(text,29),'5:29.9.0-1')
        with self.assertRaises(RuntimeError):h.select_version(text,28)
    def test_temporary_holds_repaired_after_interruption(self):
        self.c.s['temporary_docker_holds']=['docker-ce','docker-ce-cli'];self.c.save()
        new=h.Care(self.config,{},Path(self.tmp.name))
        with patch.object(h,'run',return_value=result()) as cmd:new.repair_holds()
        self.assertEqual(cmd.call_args.args[0],['apt-mark','unhold','docker-ce','docker-ce-cli'])
        self.assertNotIn('temporary_docker_holds',new.s)
    def test_failed_unhold_keeps_recovery_intent(self):
        self.c.s['temporary_docker_holds']=['docker-ce'];self.c.save()
        with patch.object(h,'run',side_effect=RuntimeError('apt locked')):
            with self.assertRaises(RuntimeError):self.c.repair_holds()
        self.assertEqual(self.c.s['temporary_docker_holds'],['docker-ce'])
    def test_https_probe_bypasses_cache_and_never_disables_tls(self):
        p={'url':'https://app.example/health','resolve':'app.example:443:127.0.0.1','body_regex':'ok'}
        with patch.object(h,'run',return_value=result('ok\n200')) as cmd:self.assertTrue(self.c.probe(p))
        args=cmd.call_args.args[0]
        self.assertIn('--resolve',args);self.assertNotIn('-k',args);self.assertNotIn('--insecure',args)
    def test_redirect_not_healthy(self):
        with patch.object(h,'run',return_value=result('redirect\n302')):
            self.assertFalse(self.c.probe({'url':'https://app.example/health'}))
    def test_duplicate_resource_names_refused(self):
        self.config['resources']*=2
        with self.assertRaisesRegex(RuntimeError,'unique'):h.validate(self.config)
    def test_reboot_success_requires_changed_boot_id(self):
        boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.c.s.update(pending_postboot=True,reboot_boot_id=boot_id)
        self.c.finish_reboot();self.assertTrue(self.c.s['pending_postboot'])
        self.c.s['reboot_boot_id']='previous-boot'
        self.c.finish_reboot();self.assertFalse(self.c.s['pending_postboot'])
        self.assertGreater(self.c.s['last_maintenance'],time.time()-5)

    def upgrade_fake(self,held='',fail_upgrade=False):
        def fake(args,**kwargs):
            if args[:2]==['apt-mark','showhold']:return result(held)
            if args[0]=='dpkg-query':
                return result('install ok installed' if args[2]=='-f=${Status}' else '5:29.1.0-1')
            if args[:2]==['apt-cache','madison']:return result('docker-ce | 5:29.2.0-1 | stable')
            if args[:2]==['dpkg','--compare-versions']:return result(code=1)
            if fail_upgrade and args[0]=='apt-get' and 'upgrade' in args:raise RuntimeError('upgrade failed')
            return result()
        return fake

    def test_failed_upgrade_restores_only_our_holds(self):
        with patch.object(h,'run',side_effect=self.upgrade_fake('containerd.io',True)) as cmd:
            with self.assertRaisesRegex(RuntimeError,'upgrade failed'):self.c.upgrade()
        unholds=[x.args[0] for x in cmd.call_args_list if x.args[0][:2]==['apt-mark','unhold']]
        self.assertTrue(unholds)
        self.assertNotIn('containerd.io',unholds[-1])
        self.assertNotIn('temporary_docker_holds',self.c.s)

    def test_original_cli_hold_blocks_engine_and_rootless_upgrade(self):
        with patch.object(h,'run',side_effect=self.upgrade_fake('docker-ce-cli')) as cmd:self.c.upgrade()
        installs=[x.args[0] for x in cmd.call_args_list if x.args[0][0]=='apt-get' and 'install' in x.args[0]]
        self.assertEqual(len(installs),1)
        self.assertNotIn('docker-ce=5:29.2.0-1',installs[0])
        self.assertNotIn('docker-ce-rootless-extras=5:29.2.0-1',installs[0])
        unholds=[x.args[0] for x in cmd.call_args_list if x.args[0][:2]==['apt-mark','unhold']]
        self.assertTrue(all('docker-ce-cli' not in args for args in unholds))


if __name__=='__main__':unittest.main()
