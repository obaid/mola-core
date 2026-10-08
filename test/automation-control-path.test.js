import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
test('long operator state paths use ordinary SSH instead of invalid Unix control sockets', () => {
  const source = fileURLToPath(new URL('../runtime/automation.py', import.meta.url));
  const fixture = `import importlib.util,json,pathlib,subprocess,sys,tempfile
from unittest.mock import patch
s=importlib.util.spec_from_file_location('a',sys.argv[1]);a=importlib.util.module_from_spec(s);s.loader.exec_module(a)
with tempfile.TemporaryDirectory() as root:
 target={'id':'machine','session_key':'boot','ssh_host':'127.0.0.1','ssh_port':2222,'control_dir':str(pathlib.Path(root)/('long-state-'*12)),'known_hosts':'known-hosts','ssh_key':'private-key'}
 calls=[]
 def run(args,**kwargs):calls.append(args);return subprocess.CompletedProcess(args,0,stdout='{"exit_code":0,"stdout":"ok"}',stderr='')
 with patch.object(a.subprocess,'run',side_effect=run):value,timing=a.Worker()._ssh(target,{'action':'exec','command':'printf ok'})
 assert value['exit_code']==0 and timing['ssh_reused'] is False
 assert 'ControlMaster=no' in calls[0] and not any(arg.startswith('ControlPath=') for arg in calls[0])
 print('long control socket fallback passed')`;
  const result = spawnSync('python3', ['-c', fixture, source], { encoding: 'utf8', timeout: 10_000 });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /fallback passed/);
});
