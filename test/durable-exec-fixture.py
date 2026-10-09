"""Owned temporary receipts and fake scope/launch only; no systemd/guest changes."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

source=Path(sys.argv[1]).read_text();spec=importlib.util.spec_from_file_location('durable',sys.argv[1]);g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
REAL_CAPABILITIES=g.capabilities
BOOT='e1019c8c-7c38-4844-a8d4-2d140a2c261f'
OP='3a3540e0-ec51-46e6-8d68-041b37894e7a'

class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='mola-durable-native-');self.addCleanup(self.temporary.cleanup)
        self.home=Path(self.temporary.name);self.addCleanup(patch.stopall)
        patch.object(g.Path,'home',return_value=self.home).start();patch.object(g,'boot_id',return_value=BOOT).start()
        patch.object(g,'capabilities',return_value={'supported':True,'max_timeout_seconds':900}).start()
        self.binding={'generation':1,'boot_id':BOOT}
        self.arguments={'operation_id':OP,'expected_generation':1,'expected_boot_id':BOOT,
                        'command':'printf fake-command-secret','timeout_seconds':900,'payload':{'token':'fake-payload-secret'},
                        'redact':['fake-command-secret'],'payload_digest':'a'*64}
        self.launches=[];self.inputs=[]
        def memory(value,*_):self.inputs.append(json.loads(value));return os.open('/dev/null',os.O_RDONLY)
        def launch(arguments,**kwargs):
            self.assertEqual(g.load(g.path_for(OP))['status'],'queued')
            self.assertNotIn('fake-command-secret',str(arguments));self.assertNotIn('fake-payload-secret',str(arguments))
            self.assertEqual(kwargs['stdout'],g.subprocess.DEVNULL);self.assertEqual(kwargs['stderr'],g.subprocess.DEVNULL)
            self.launches.append(arguments)
        self.memory=patch.object(g,'private_input',side_effect=memory);self.memory.start()
        patch.object(g.subprocess,'Popen',side_effect=launch).start()
    def accept(self):return g.submit(self.arguments,self.binding,source)
    def record(self,**fields):
        self.accept();path=g.path_for(OP);record=g.load(path);record.update(fields);g.save(path,record);return path,record
    def test_acceptance_contains_only_digest_and_binding_before_one_detached_launch(self):
        result=self.accept();self.assertEqual(result['status'],'queued');self.assertFalse(result['terminal'])
        self.assertEqual(self.inputs[0]['request']['arguments']['command'],self.arguments['command'])
        for path in g.folder().glob('*.json'):
            self.assertNotIn('fake-command-secret',path.read_text());self.assertNotIn('fake-payload-secret',path.read_text())
        self.assertIn('--property=KillMode=control-group',self.launches[0]);self.assertIn('--property=RuntimeMaxSec=915s',self.launches[0])
        self.assertEqual(self.accept()['status'],'queued');self.assertEqual(len(self.launches),1)
    def test_changed_digest_replay_refuses_and_never_relaunches(self):
        self.accept()
        with self.assertRaises(g.ExecError):g.submit({**self.arguments,'payload_digest':'b'*64},self.binding,source)
        self.assertEqual(len(self.launches),1)
    def test_expired_accepted_receipt_is_unknown_not_new_execution(self):
        path,record=self.record(accepted_at=time.time()-20)
        self.assertEqual(g.status(self.arguments)['status'],'outcome_unknown')
        self.assertEqual(self.accept()['status'],'outcome_unknown');self.assertEqual(len(self.launches),1)
    def test_disappeared_worker_is_unknown_even_when_scope_may_have_ended(self):
        self.record(status='running',worker_pid=42,worker_start='99',scope='/user.slice/unknown.service')
        with patch.object(g,'process_state',return_value=False),patch.object(g,'scope_gone',return_value=True):
            self.assertEqual(g.status(self.arguments)['status'],'outcome_unknown')
        self.assertEqual(self.accept()['status'],'outcome_unknown');self.assertEqual(len(self.launches),1)
    def test_finishing_is_not_terminal_until_original_scope_is_empty(self):
        path,record=self.record(status='finishing',result={'exit_code':0,'stdout':'safe','stderr':''})
        with patch.object(g,'scope_gone',return_value=False):
            result=g.status(self.arguments);self.assertEqual(result['status'],'running');self.assertNotIn('result',result)
        with patch.object(g,'scope_gone',return_value=True):
            result=g.status(self.arguments);self.assertEqual(result['status'],'completed');self.assertTrue(result['terminal'])
        with patch.object(g,'boot_id',return_value='other-boot'):
            self.assertEqual(g.status(self.arguments),result)
            self.assertEqual(g.submit(self.arguments,{'generation':2,'boot_id':'other-boot'},source),result)
    def test_terminal_failure_preserves_timeout_and_does_not_retry(self):
        self.record(status='finishing',result={'exit_code':124,'stdout':'','stderr':'','timed_out':True})
        with patch.object(g,'scope_gone',return_value=True):
            result=g.status(self.arguments);self.assertEqual(result['status'],'failed');self.assertTrue(result['result']['timed_out'])
        self.assertEqual(self.accept(),result);self.assertEqual(len(self.launches),1)
    def test_stale_actual_guest_boot_is_rejected_before_acceptance_and_launch(self):
        with patch.object(g,'boot_id',return_value='e1019c8c-7c38-4844-a8d4-2d140a2c261e'):
            with self.assertRaises(g.ExecError):self.accept()
        self.assertFalse(g.path_for(OP).exists());self.assertEqual(self.launches,[])
    def test_queued_cancel_fences_a_late_worker_before_any_command(self):
        self.accept();result=g.cancel(self.arguments)
        self.assertEqual(result['status'],'failed');self.assertTrue(result['result']['cancelled'])
        record=g.load(g.path_for(OP))
        with patch.object(g,'command_result') as command:
            with self.assertRaises(g.ExecError):g.worker({'arguments':self.arguments,'binding':self.binding,'attempt':record['attempt']})
            command.assert_not_called()
    def test_running_cancel_requires_positive_scope_termination(self):
        path,record=self.record(status='running',worker_pid=42,worker_start='99')
        record['scope']='/user.slice/user-'+str(os.getuid())+'.slice/user@'+str(os.getuid())+'.service/app.slice/'+record['unit'];g.save(path,record)
        with patch.object(g,'process_state',return_value=True),patch.object(g,'scope_gone',return_value=False),patch.object(g.subprocess,'run'):
            self.assertFalse(g.cancel(self.arguments)['terminal'])
        with patch.object(g,'scope_gone',return_value=True):
            result=g.status(self.arguments);self.assertEqual(result['status'],'failed');self.assertTrue(result['result']['cancelled'])
    def test_worker_persists_launch_fence_and_finishing_but_not_false_terminal(self):
        self.accept();record=g.load(g.path_for(OP));unit=record['unit'];original=g.Path.read_text
        def proc(path,*args,**kwargs):
            if str(path)=='/proc/'+str(os.getpid())+'/stat':return str(os.getpid())+' (worker) S '+' '.join(['0']*18+['99'])
            if str(path)=='/proc/self/cgroup':return '0::/user.slice/user-'+str(os.getuid())+'.slice/user@'+str(os.getuid())+'.service/app.slice/'+unit+'\n'
            return original(path,*args,**kwargs)
        with patch.object(g.Path,'read_text',new=proc),patch.object(g,'command_result',return_value={'exit_code':0,'stdout':'safe','stderr':''}) as command:
            g.worker({'arguments':self.arguments,'binding':self.binding,'attempt':record['attempt']});command.assert_called_once()
        record=g.load(g.path_for(OP));self.assertEqual(record['status'],'finishing');self.assertEqual(record['worker_start'],'99')
        self.assertNotIn('fake-command-secret',json.dumps(record));self.assertNotIn('fake-payload-secret',json.dumps(record))
    def test_worker_on_new_boot_never_launches(self):
        self.accept();record=g.load(g.path_for(OP))
        with patch.object(g,'boot_id',return_value='later-boot'),patch.object(g,'command_result') as command:
            with self.assertRaises(g.ExecError):g.worker({'arguments':self.arguments,'binding':self.binding,'attempt':record['attempt']})
            command.assert_not_called()
    def test_output_redaction_precedes_truncation_and_handles_credential_fields(self):
        definition=g.spec(self.arguments)
        raw=b'fake-command-secret fake-payload-secret password=other-secret\nAuthorization: Bearer fake-bearer-value\nCookie: session=fake-cookie-value\n'+b'x'*70000
        output=g.sanitized_output(raw,definition,False)
        self.assertNotIn('fake-command-secret',output);self.assertNotIn('fake-payload-secret',output);self.assertNotIn('other-secret',output)
        self.assertNotIn('fake-bearer-value',output);self.assertNotIn('fake-cookie-value',output)
        self.assertLessEqual(len(output.encode()),65536)
        self.assertEqual(g.sanitized_output(raw,definition,True),'[OUTPUT OMITTED: SIZE LIMIT]')
    def test_deeply_nested_payload_credentials_are_inspected_without_silent_skip(self):
        value={'password':'fake-deep-secret'}
        for _ in range(40):value={'nested':value}
        definition=g.spec({**self.arguments,'payload':value})
        self.assertNotIn('fake-deep-secret',g.sanitized_output(b'fake-deep-secret',definition,False))
    def test_payload_size_matches_one_mib_json_boundary(self):
        g.spec({**self.arguments,'payload':{'data':'x'*(1048576-11)}})
        with self.assertRaises(g.ExecError):g.spec({**self.arguments,'payload':{'data':'x'*1048576}})
    def test_uuid_positive_generation_and_timeout_boundaries(self):
        for fields in [{'operation_id':'not-uuid'},{'expected_generation':0},{'timeout_seconds':901},{'timeout_seconds':True}]:
            with self.assertRaises(g.ExecError):g.submit({**self.arguments,**fields},self.binding,source)
        self.assertEqual(self.launches,[])
    def test_unreadable_actual_boot_disables_capability_without_command(self):
        with patch.object(g,'boot_id',side_effect=FileNotFoundError()),patch.object(g.subprocess,'run') as run:
            self.assertFalse(REAL_CAPABILITIES()['supported']);run.assert_not_called()
    def test_missing_receipt_and_binding_mismatch_cannot_execute(self):
        with self.assertRaises(g.ExecError):g.status(self.arguments)
        self.accept()
        with self.assertRaises(g.ExecError):g.status({**self.arguments,'expected_generation':2})
        self.assertEqual(len(self.launches),1)
    def test_unicode_and_escaped_max_output_survives_terminal_reload(self):
        for text in ['😀' * 8192, '\\' * 32768, '\n' * 32768]:
            with self.subTest(kind=text[:1]):
                result={'exit_code':0,'stdout':text,'stderr':text}
                path,record=self.record(status='completed',result=result)
                self.assertLess(path.stat().st_size,262144)
                self.assertEqual(g.load(path)['result'],result)
                self.assertEqual(g.status(self.arguments)['result'],result)
                self.assertEqual(self.accept()['result'],result)
        self.assertEqual(len(self.launches),1)

unittest.main(argv=[sys.argv[0]])
