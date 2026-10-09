"""Session service owned temporary files; no browser, desktop or guest is used."""
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('session_agent', sys.argv.pop(1))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


class Tools:
    def __init__(self):
        self.mode = 'full'; self.browser_fail = False; self.windows_fail = False
        self.pages = [{'id': 'tab-one', 'url': 'https://example.com/owned'}]
        self.frames = [{'id': '0x123', 'x': 0, 'y': 20, 'width': 800, 'height': 600, 'title': 'private-title', 'wm_class': ['fixture', 'Fixture']}]
        self.restores = 0; self.restore_success = True
    def sessions(self, tool, arguments):
        assert tool == 'session_restore'; self.restores += 1
        return {'restored': self.restore_success}
    def desktop_env(self): pass
    def screen(self): return {'width': 1280, 'height': 720}
    def targets(self):
        if self.browser_fail: raise RuntimeError('private upstream browser error')
        return {'mode': self.mode, 'current_tab': 'tab-one'}, self.pages
    def windows(self):
        if self.windows_fail: raise RuntimeError('private upstream desktop error')
        return self.frames
    def web_url(self, value):
        parsed = urllib.parse.urlsplit(value)
        if parsed.username or parsed.password or parsed.fragment: raise ValueError('private URL')
        return value


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mola-session-fixture-')
        self.addCleanup(self.temporary.cleanup)
        self.home = pathlib.Path(self.temporary.name)
        self.folder = m.private_state(self.home); self.tools = Tools()
        self.config = self.folder/'session-agent-config.json'
    def capture(self): return m.capture(self.tools, self.folder, self.config, clock=lambda: 123456)
    def manifest(self): return m.read_private(self.folder/'session.json')
    def test_complete_private_manifest_and_secret_free_status(self):
        status = self.capture(); manifest = self.manifest()
        self.assertEqual(manifest['tabs'], [{'url': 'https://example.com/owned', 'active': True}])
        self.assertNotIn('title', manifest['windows'][0]); self.assertIn('wm_class', manifest['windows'][0])
        self.assertNotIn('url', json.dumps(status)); self.assertNotIn('private-title', json.dumps(status))
        self.assertEqual((self.folder/'session.json').stat().st_mode & 0o777, 0o600)
    def test_restart_keeps_last_good_manifest_when_probes_unavailable(self):
        self.capture(); original = (self.folder/'session.json').read_bytes()
        self.tools.browser_fail = self.tools.windows_fail = True
        status = self.capture()
        self.assertEqual((self.folder/'session.json').read_bytes(), original)
        self.assertFalse(status['browser_available']); self.assertFalse(status['windows_available'])
    def test_partial_probe_failure_preserves_only_unavailable_component(self):
        self.capture(); self.tools.browser_fail = True; self.tools.frames = []
        self.capture(); manifest = self.manifest()
        self.assertEqual(manifest['tabs'][0]['url'], 'https://example.com/owned')
        self.assertEqual(manifest['windows'], [])
    def test_valid_empty_browser_result_can_clear_tabs(self):
        self.capture(); self.tools.pages = []; self.capture()
        self.assertEqual(self.manifest()['tabs'], [])
    def test_lightweight_mode_preserved(self):
        self.tools.mode = 'lightweight'; self.capture()
        self.assertEqual(self.manifest()['browser_mode'], 'lightweight')
    def test_absent_config_preserves_saved_apps(self):
        self.capture(); prior = self.manifest(); prior['apps'] = ['fixture']; m.atomic(self.folder/'session.json', prior)
        self.capture(); self.assertEqual(self.manifest()['apps'], ['fixture'])
    def test_registered_apps_config_updates_selection(self):
        m.atomic(self.folder/'apps.json', {'fixture': {'executable': '/fixture'}})
        m.atomic(self.config, {'apps': ['fixture']})
        status = self.capture(); self.assertTrue(status['apps_config_valid'])
        self.assertEqual(self.manifest()['apps'], ['fixture'])
    def test_unknown_app_config_cannot_replace_existing_selection(self):
        self.capture(); prior = self.manifest(); prior['apps'] = ['fixture']; m.atomic(self.folder/'session.json', prior)
        m.atomic(self.config, {'apps': ['unregistered']})
        self.assertFalse(self.capture()['apps_config_valid']); self.assertEqual(self.manifest()['apps'], ['fixture'])
    def test_credentials_and_fragment_urls_not_captured(self):
        self.tools.pages += [{'id': 'secret', 'url': 'https://user:password@example.com/'}, {'id': 'fragment', 'url': 'https://example.com/#token'}]
        self.capture(); self.assertEqual(len(self.manifest()['tabs']), 1)
    def test_oversized_or_malformed_probe_retains_prior(self):
        self.capture(); self.tools.pages *= 101; self.tools.frames[0]['width'] = 0
        status = self.capture(); self.assertFalse(status['browser_available']); self.assertFalse(status['windows_available'])
        self.assertEqual(len(self.manifest()['tabs']), 1)
    def test_symlink_manifest_does_not_overwrite_unrelated_file(self):
        target = self.home/'preserve-source'; target.write_text('preserve')
        (self.folder/'session.json').symlink_to(target)
        with self.assertRaises(ValueError): self.capture()
        self.assertEqual(target.read_text(), 'preserve')
    def test_crashed_staging_file_never_promoted(self):
        self.capture(); before = self.manifest(); stale = self.folder/'session.json.dead-worker.new'
        stale.write_text('{"schema":1,"private":"wrong"}')
        self.tools.browser_fail = self.tools.windows_fail = True
        self.capture(); self.assertEqual(self.manifest(), before)
        self.assertTrue(stale.exists())
    def test_failed_atomic_replace_preserves_published_manifest(self):
        self.capture(); before = (self.folder/'session.json').read_bytes()
        with patch.object(m.os, 'replace', side_effect=OSError('fixture interruption')):
            with self.assertRaises(OSError): m.atomic(self.folder/'session.json', {'replacement': True})
        self.assertEqual((self.folder/'session.json').read_bytes(), before)
        self.assertFalse(list(self.folder.glob('*.new')))
    def test_source_and_profile_files_never_mutated(self):
        source = self.home/'guest_tools.py'; profile = self.home/'ChromeProfile'; profile.mkdir()
        source.write_text('source'); (profile/'customer').write_text('profile')
        self.capture(); self.assertEqual(source.read_text(), 'source'); self.assertEqual((profile/'customer').read_text(), 'profile')
    def test_single_cycle_and_interval_config_bound(self):
        self.assertEqual(m.run(self.tools, self.folder, 30, self.config, once=True), 0)
        for interval in [29, 301, True]:
            with self.assertRaises(ValueError): m.run(self.tools, self.folder, interval, self.config, once=True)
        with self.assertRaises(ValueError): m.run(self.tools, self.folder, 60, self.home/'arbitrary-config', once=True)
    def test_an_existing_process_lock_prevents_duplicate_cycle(self):
        fd = m.os.open(self.folder/'session-agent.lock', m.os.O_RDWR | m.os.O_CREAT, 0o600)
        try:
            m.fcntl.flock(fd, m.fcntl.LOCK_EX | m.fcntl.LOCK_NB)
            self.assertEqual(m.run(self.tools, self.folder, 60, self.config, once=True), 0)
            self.assertFalse((self.folder/'session.json').exists())
        finally: m.os.close(fd)
    def test_unmanaged_source_path_cannot_be_loaded(self):
        with self.assertRaises(ValueError): m.load_tools(self.home/'customer-source.py')
    def test_corrupt_prior_manifest_remains_untouched(self):
        m.atomic(self.folder/'session.json', {'schema': 'invalid'})
        with self.assertRaises(ValueError): self.capture()
        self.assertEqual(m.read_private(self.folder/'session.json'), {'schema': 'invalid'})
    def test_restore_precedes_capture_and_same_boot_crash_does_not_repeat_launch(self):
        self.capture(); boot = '11111111-1111-4111-8111-111111111111'
        with patch.object(m, 'boot_identity', return_value=boot):
            m.run(self.tools, self.folder, 60, self.config, once=True, restore=True)
            m.run(self.tools, self.folder, 60, self.config, once=True, restore=True)
        self.assertEqual(self.tools.restores, 1)
        self.assertEqual(m.read_private(self.folder/'session-agent-status.json')['restore_boot_id'], boot)
    def test_new_boot_gets_a_new_restore_attempt(self):
        self.capture()
        m.restore_first(self.tools, self.folder, '11111111-1111-4111-8111-111111111111')
        m.restore_first(self.tools, self.folder, '22222222-2222-4222-8222-222222222222')
        self.assertEqual(self.tools.restores, 2)
    def test_failed_restore_preserves_manifest_and_blocks_same_boot_retry(self):
        self.capture(); prior = (self.folder/'session.json').read_bytes(); self.tools.restore_success = False
        boot = '11111111-1111-4111-8111-111111111111'
        with self.assertRaises(ValueError): m.restore_first(self.tools, self.folder, boot)
        self.tools.restore_success = True
        with self.assertRaises(ValueError): m.restore_first(self.tools, self.folder, boot)
        self.assertEqual(self.tools.restores, 1)
        self.assertEqual((self.folder/'session.json').read_bytes(), prior)
    def test_durable_submission_precedes_restore_side_effect(self):
        self.capture(); boot = '11111111-1111-4111-8111-111111111111'
        def lost_ack(*args):
            progress = m.read_private(self.folder/'session-agent-status.json')
            self.assertEqual(progress['restore_submitted_boot_id'], boot)
            self.assertNotIn('restore_boot_id', progress)
            raise OSError('private lost acknowledgement')
        with patch.object(self.tools, 'sessions', side_effect=lost_ack):
            with self.assertRaises(ValueError): m.restore_first(self.tools, self.folder, boot)
        with self.assertRaises(ValueError): m.restore_first(self.tools, self.folder, boot)
        self.assertEqual(self.tools.restores, 0)
    def test_no_manifest_boot_enables_observation_without_launch(self):
        boot = '11111111-1111-4111-8111-111111111111'
        m.restore_first(self.tools, self.folder, boot)
        self.assertEqual(self.tools.restores, 0)
        self.assertEqual(m.read_private(self.folder/'session-agent-status.json')['restore_boot_id'], boot)
    def test_standard_group_writable_xdg_ancestors_keep_private_state_leaf(self):
        (self.home/'.local').chmod(0o775); (self.home/'.local/state').chmod(0o775)
        self.assertEqual(m.private_state(self.home), self.folder)
        self.capture(); self.assertEqual(self.folder.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.folder/'session.json').stat().st_mode & 0o777, 0o600)
    def test_world_writable_xdg_ancestor_is_rejected(self):
        (self.home/'.local').chmod(0o777)
        with self.assertRaises(ValueError): m.private_state(self.home)
    def test_unready_desktop_never_writes_restore_intent_or_launches(self):
        self.capture(); before = m.read_private(self.folder/'session-agent-status.json')
        with patch.object(m, 'wait_desktop', side_effect=ValueError('not ready')):
            with self.assertRaises(ValueError): m.restore_first(self.tools, self.folder, '11111111-1111-4111-8111-111111111111')
        self.assertEqual(m.read_private(self.folder/'session-agent-status.json'), before)
        self.assertEqual(self.tools.restores, 0)
        m.restore_first(self.tools, self.folder, '11111111-1111-4111-8111-111111111111')
        self.assertEqual(self.tools.restores, 1)
    def test_desktop_readiness_retries_safely_and_bounds_wait(self):
        calls = [0]
        def pending():
            calls[0] += 1
            if calls[0] < 3: raise OSError('not ready')
            return {'width': 1280, 'height': 720}
        with patch.object(self.tools, 'screen', side_effect=pending):
            m.wait_desktop(self.tools, sleep=lambda seconds: None)
        self.assertEqual(calls[0], 3)
        with patch.object(self.tools, 'screen', return_value={'width': 0, 'height': 0}):
            with self.assertRaises(ValueError): m.wait_desktop(self.tools, timeout=0)


if __name__ == '__main__': unittest.main()
