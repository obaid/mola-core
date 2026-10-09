"""Owned temporary disk/receipt fixture. Never starts QEMU, SSH or freezes a filesystem."""
import importlib.util
import json
import pathlib
import sys
import tempfile
import time
import unittest
import subprocess
import ast
import io
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('live_checkpoint', sys.argv.pop(1))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
ID = '11111111-1111-4111-8111-111111111111'
BOOT = '22222222-2222-4222-8222-222222222222'


class Guest:
    def __init__(self, runner, journal):
        self.runner, self.journal = runner, journal
        self.process = type('Process', (), {'pid': 20002})()
    def receive(self, phase):
        self.runner.events.append('guest_'+phase)
        return {'phase': phase, 'nonce': self.journal['nonce'], 'boot_id': BOOT}
    def command(self, command, phase):
        self.runner.events.append(command)
        if command == 'freeze':
            self.runner.frozen = True
            if self.runner.fail == 'freeze': raise OSError('private upstream output')
        else:
            if self.runner.fail == 'thaw': raise OSError('private upstream output')
            self.runner.frozen = False
        return self.receive(phase)
    def close(self): self.runner.events.append('guest_close')


class Services:
    def __init__(self, runner): self.runner = runner
    def identity(self, pid): return {'pid': pid, 'boot_id': BOOT, 'start_ticks': '99', 'state': 'S'}
    def alive(self, identity): return identity['pid'] == 20001 and not self.runner.qemu_dead
    def status(self, identity): return 'unknown' if self.runner.identity_unknown else ('same' if self.alive(identity) else 'exited')
    def start_watchdog(self, path):
        self.runner.events.append('watchdog')
        self.runner.watchdog_journal = path
    def guest(self, runner, data, journal):
        self.runner.events.append('guest_open')
        return Guest(runner, journal)
    def read_released(self, runner, data, journal): return self.runner.released_proof


class Runner:
    def __init__(self, root):
        self.root = pathlib.Path(root); self.sockets = self.root/'sockets'
        self.config = {}; self.events = []; self.state = 'running'; self.frozen = False
        self.fail = None; self.qemu_dead = False; self.released_proof = False; self.identity_unknown = False
        self._checkpoint_services = Services(self)
        self.directory = self.root/ID; self.directory.mkdir()
        (self.directory/'machine.json').write_text(json.dumps(self.metadata(ID)))
        (self.directory/'running.marker').write_text(json.dumps(self._checkpoint_services.identity(20001)))
        self.payload = {'operation_id': 'checkpoint-one', 'generation': 3, 'snapshot_id': 'snapshot-one'}
        self.receipt = self.operation_path(ID, 'checkpoint', 'checkpoint-one')
        m.save(self.receipt, {'status': 'pending', 'incarnation': 'fixture', 'payload': self.payload})
    def metadata(self, identifier): return {'id': identifier, 'managed_by': 'mola-native-v1', 'ssh_port': 12345, 'storage_incarnation': 'fixture'}
    def folder(self, identifier): return self.directory
    def storage_incarnation(self, identifier): return 'fixture'
    def operation_path(self, identifier, verb, operation): return self.root/(verb+'-'+operation+'.json')
    def storage_path(self, identifier, snapshot): return self.root/snapshot
    def snapshot_manifest(self, identifier, snapshot): return json.loads((self.root/snapshot/'manifest.json').read_text())
    def qmp(self, data, command):
        if command == 'query-status': return {'status': self.state}
        self.events.append(command)
        if command == 'cont' and self.fail == 'resume': raise OSError('private upstream output')
        self.state = 'paused' if command == 'stop' else 'running'
        return {}
    def _snapshot_contents(self, identifier, payload, guard, publish):
        assert publish is False and self.frozen and self.state == 'paused'
        self.events.append('copy'); guard()
        if self.fail == 'copy': raise OSError('private disk output')
        if self.fail == 'watchdog':
            path = self.watchdog_journal
            m.save(path.with_name(path.name+'.abort'), {'nonce': json.loads(path.read_text())['nonce']})
            self.state = 'running'; self.state = 'paused'  # Re-pausing cannot rescue mixed bytes.
        if self.fail == 'deadline':
            with patch.object(m.time, 'monotonic', return_value=time.monotonic()+1000): guard()
        guard()
        folder = self.storage_path(identifier, payload['snapshot_id']); folder.mkdir()
        (folder/'disk.gz.partial').write_bytes(b'owned staged data')
        return {'id': payload['snapshot_id'], 'sha256': 'fixture', 'format': 'mola-raw-gzip-v1'}
    def _publish_snapshot(self, identifier, payload, manifest, guard):
        guard()
        assert self.state == 'running' and not self.frozen
        self.events.append('publish')
        folder = self.storage_path(identifier, payload['snapshot_id'])
        folder.mkdir(exist_ok=True)
        (folder/'manifest.json').write_text(json.dumps(manifest))
        return manifest
    def run(self): return m.checkpoint(self, ID, {**self.payload, '_receipt_path': self.receipt})


class Checkpoints(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mola-checkpoint-fixture-')
        self.addCleanup(self.temporary.cleanup)
        self.runner = Runner(self.temporary.name)
    def journal(self): return self.runner.receipt.with_suffix('.checkpoint-state')
    def test_success_guardians_precede_freeze_and_publish_follows_resume_thaw(self):
        result = self.runner.run()
        self.assertEqual(self.runner.events, ['watchdog', 'guest_open', 'guest_ready', 'freeze', 'guest_frozen', 'stop', 'copy', 'cont', 'thaw', 'guest_released', 'publish', 'guest_close'])
        self.assertEqual(result['checkpoint_operation_id'], 'checkpoint-one')
        self.assertEqual(result['consistency'], 'filesystem')
        self.assertTrue(result['running_resumed']); self.assertTrue(result['filesystem_thawed'])
        self.assertEqual(json.loads(self.journal().read_text())['phase'], 'sealed')
    def test_sealed_replay_does_not_freeze_or_copy(self):
        original = self.runner.run(); self.runner.events.clear()
        self.assertEqual(self.runner.run(), original)
        self.assertEqual(self.runner.events, ['publish'])
    def test_secret_free_journal(self):
        self.runner.run()
        journal = json.loads(self.journal().read_text())
        self.assertNotIn('config', journal); self.assertNotIn('payload', journal)
        self.assertEqual(self.journal().stat().st_mode & 0o777, 0o600)
        self.assertNotIn('guest_key', self.journal().read_text())
    def test_copy_error_safely_resumes_and_aborts(self):
        self.runner.fail = 'copy'
        with self.assertRaises(m.CheckpointAborted): self.runner.run()
        self.assertFalse(self.runner.frozen); self.assertEqual(self.runner.state, 'running')
        self.assertNotIn('publish', self.runner.events)
    def test_freeze_error_is_recovered_before_terminal_failure(self):
        self.runner.fail = 'freeze'
        with self.assertRaises(m.CheckpointAborted): self.runner.run()
        self.assertFalse(self.runner.frozen)
    def test_watchdog_resume_repause_fences_every_copy(self):
        self.runner.fail = 'watchdog'
        with self.assertRaises(m.CheckpointAborted): self.runner.run()
        self.assertNotIn('publish', self.runner.events)
    def test_copy_deadline_never_publishes(self):
        self.runner.fail = 'deadline'
        with self.assertRaises(m.CheckpointAborted): self.runner.run()
        self.assertNotIn('publish', self.runner.events)
    def test_resume_uncertainty_stays_pending(self):
        self.runner.fail = 'resume'
        with self.assertRaises(m.CheckpointRecoveryPending): self.runner.run()
        self.assertEqual(json.loads(self.runner.receipt.read_text())['status'], 'pending')
        self.assertNotIn('publish', self.runner.events)
    def test_thaw_uncertainty_stays_pending(self):
        self.runner.fail = 'thaw'
        with self.assertRaises(m.CheckpointRecoveryPending): self.runner.run()
        self.assertNotIn('publish', self.runner.events)
    def test_restart_recovers_unknown_original_without_recopy(self):
        self.runner.fail = 'thaw'
        with self.assertRaises(m.CheckpointRecoveryPending): self.runner.run()
        self.runner.fail = None; self.runner.events.clear()
        with self.assertRaises(m.CheckpointRecoveryPending): self.runner.run()
        self.assertNotIn('freeze', self.runner.events); self.assertNotIn('copy', self.runner.events)
        self.runner.released_proof = True
        with self.assertRaises(m.CheckpointAborted): self.runner.run()
        self.assertNotIn('publish', self.runner.events)
    def test_recovery_of_exited_original_qemu_never_controls_new_guest(self):
        self.runner.fail = 'thaw'
        with self.assertRaises(m.CheckpointRecoveryPending): self.runner.run()
        self.runner.qemu_dead = True; self.runner.events.clear()
        with self.assertRaises(m.CheckpointAborted): self.runner.run()
        self.assertEqual(self.runner.events, [])
    def test_released_stage_survives_worker_restart_without_recopy(self):
        original = self.runner.run(); journal = json.loads(self.journal().read_text())
        journal['phase'] = 'released'; m.save(self.journal(), journal); self.runner.events.clear()
        self.assertEqual(self.runner.run(), original); self.assertEqual(self.runner.events, ['publish'])
    def test_occupied_snapshot_cannot_alias_an_old_manifest(self):
        folder = self.runner.storage_path(ID, 'snapshot-one'); folder.mkdir()
        (folder/'manifest.json').write_text(json.dumps({'id': 'snapshot-one', 'checkpoint_operation_id': 'other'}))
        with self.assertRaises(ValueError): self.runner.run()
        self.assertNotIn('freeze', self.runner.events)
    def test_receipt_payload_and_incarnation_binding(self):
        self.runner.payload['generation'] = 4
        with self.assertRaises(ValueError): self.runner.run()
        self.assertNotIn('freeze', self.runner.events)
    def test_symlink_journal_rejected_without_touching_target(self):
        target = self.runner.root/'unrelated'; target.write_text('preserve')
        self.journal().symlink_to(target)
        with self.assertRaises(ValueError): self.runner.run()
        self.assertEqual(target.read_text(), 'preserve')
    def test_no_durable_receipt_no_freeze(self):
        with self.assertRaises(ValueError): m.checkpoint(self.runner, ID, self.runner.payload)
        self.assertEqual(self.runner.events, [])
    def test_static_guest_source_compiles_without_running(self):
        compile(m.GUEST_HELPER, '<guest-checkpoint>', 'exec'); compile(m.GUEST_READ, '<guest-status>', 'exec')
    def test_pre_freeze_restart_can_abort_without_waiting_for_thaw(self):
        self.runner.run(); journal = json.loads(self.journal().read_text())
        journal['phase'] = 'preparing'; m.save(self.journal(), journal)
        with self.assertRaises(m.CheckpointAborted): self.runner.run()
    def test_uncertain_process_identity_cannot_release_pending_admission(self):
        self.runner.fail = 'thaw'
        with self.assertRaises(m.CheckpointRecoveryPending): self.runner.run()
        self.runner.identity_unknown = True; self.runner.events.clear()
        with self.assertRaises(m.CheckpointRecoveryPending): self.runner.run()
        self.assertEqual(self.runner.events, [])
    def test_process_io_errors_are_unknown_not_exit_proof(self):
        with patch.object(m, 'process_identity', side_effect=PermissionError()):
            self.assertEqual(m.identity_status({'pid': 123}), 'unknown')
        with patch.object(m, 'process_identity', side_effect=FileNotFoundError(2, 'gone', '/proc/123/stat')):
            self.assertEqual(m.identity_status({'pid': 123}), 'exited')
    def test_detached_watchdog_invalidation_precedes_resume_and_retries_uncertainty(self):
        self.runner.run(); journal = json.loads(self.journal().read_text())
        journal['phase'] = 'copying'; journal['monotonic_deadline'] = time.monotonic()-1
        m.save(self.journal(), journal)
        commands = []
        abort = self.journal().with_name(self.journal().name+'.abort')
        def wire(item, command):
            self.assertTrue(abort.exists())
            commands.append(command)
            if len(commands) < 3: raise OSError('temporary management outage')
            return {'status': 'running'}
        with patch.object(m, 'identity_status', return_value='same'), patch.object(m, 'same_process', return_value=False), \
             patch.object(m, 'qmp', side_effect=wire), patch.object(m.time, 'sleep', return_value=None):
            self.assertEqual(m.watchdog(self.journal()), 0)
        self.assertEqual(commands, ['cont', 'cont', 'cont', 'query-status'])
    def test_watchdog_does_not_resume_reused_or_unrelated_qemu(self):
        self.runner.run(); journal = json.loads(self.journal().read_text())
        journal['phase'] = 'copying'; journal['monotonic_deadline'] = time.monotonic()-1
        m.save(self.journal(), journal)
        with patch.object(m, 'identity_status', return_value='different'), patch.object(m, 'same_process', return_value=False), \
             patch.object(m, 'qmp', side_effect=AssertionError('unrelated VM')):
            self.assertEqual(m.watchdog(self.journal()), 0)
    def test_released_watchdog_cannot_abort_seal(self):
        self.runner.run()
        with patch.object(m, 'qmp', side_effect=AssertionError('released VM')):
            self.assertEqual(m.watchdog(self.journal()), 0)
        self.assertFalse(self.journal().with_name(self.journal().name+'.abort').exists())
    def test_watchdog_subprocess_has_no_inherited_lifecycle_descriptors(self):
        with patch.object(m.subprocess, 'Popen') as launch:
            m.Services().start_watchdog(self.journal())
        arguments = launch.call_args.kwargs
        self.assertTrue(arguments['close_fds']); self.assertTrue(arguments['start_new_session'])
        self.assertNotIn('pass_fds', arguments)
        self.assertEqual(arguments['stderr'], subprocess.DEVNULL)
    def test_abort_marker_after_released_stage_prevents_publication(self):
        self.runner.run(); journal = json.loads(self.journal().read_text())
        journal['phase'] = 'released'; m.save(self.journal(), journal)
        m.save(self.journal().with_name(self.journal().name+'.abort'), {'nonce': journal['nonce']})
        self.runner.events.clear(); self.runner.released_proof = True
        with self.assertRaises(m.CheckpointAborted): self.runner.run()
        self.assertNotIn('publish', self.runner.events)
    def test_control_close_error_cannot_overwrite_pending_recovery(self):
        self.runner.fail = 'thaw'
        with patch.object(Guest, 'close', side_effect=BrokenPipeError()):
            with self.assertRaises(m.CheckpointRecoveryPending): self.runner.run()
    def test_ssh_host_alias_binds_enrolled_machine_identity(self):
        key = self.runner.root/'guest-key'; key.write_text('fixture-only')
        known = self.runner.root/'known-hosts'; known.write_text('fixture-only')
        config = {'guest_key': str(key), 'guest_known_hosts': str(known)}
        command = m.ssh_command(config, self.runner.metadata(ID), 'print(1)')
        self.assertIn('-oHostKeyAlias=mola-'+ID, command)
        self.assertIn('-oStrictHostKeyChecking=yes', command)
        persistent = m.ssh_command(config, self.runner.metadata(ID), 'print(1)', persistent=True)
        self.assertIn('-oServerAliveInterval=0', persistent); self.assertIn('-oTCPKeepAlive=no', persistent)
        self.assertNotIn('-oServerAliveInterval=2', persistent)
        with self.assertRaises(ValueError): m.ssh_command(config, {**self.runner.metadata(ID), 'id': 'different\nidentity'}, '')
    def test_guest_guardian_parent_identity_distinguishes_reused_pid_and_unknown(self):
        # Execute the production helper's pure identity function with fake /proc
        # reads; no guardian, signal, SSH or filesystem-freeze syscall runs.
        tree = ast.parse(m.GUEST_HELPER)
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'parent_identity')
        module = ast.Module(body=[function], type_ignores=[])
        env = {'parent': 123, 'parent_ticks': '99', 'boot': BOOT}
        exec(compile(module, '<guest-identity-fixture>', 'exec'), env)
        def proc(ticks, state='S'):
            return '123 (fixture) '+ ' '.join([state]+['0']*18+[ticks])
        def original(path): return io.StringIO(BOOT if path.endswith('boot_id') else proc('99'))
        with patch('builtins.open', side_effect=original): self.assertEqual(env['parent_identity'](), 'same')
        with patch('builtins.open', side_effect=lambda path: io.StringIO(BOOT if path.endswith('boot_id') else proc('100'))):
            self.assertEqual(env['parent_identity'](), 'different')
        with patch('builtins.open', side_effect=PermissionError()): self.assertEqual(env['parent_identity'](), 'unknown')
    def test_host_signal_binds_pidfd_and_rechecks_identity_before_signal(self):
        with patch.object(m.os, 'pidfd_open', return_value=57, create=True), \
             patch.object(m.signal, 'pidfd_send_signal', create=True) as send, patch.object(m.os, 'close') as close:
            with patch.object(m, 'same_process', return_value=False):
                self.assertFalse(m.signal_bound({'pid': 123}, m.signal.SIGTERM)); send.assert_not_called()
            with patch.object(m, 'same_process', return_value=True):
                self.assertTrue(m.signal_bound({'pid': 123}, m.signal.SIGTERM))
                send.assert_called_once_with(57, m.signal.SIGTERM, None, 0)
            self.assertEqual(close.call_count, 2)


if __name__ == '__main__': unittest.main()
