"""No desktop, package installation, customer profile or production connection."""
import importlib.util
import json
import pathlib
import tempfile
import sys
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('tools', sys.argv[1]); g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
with patch.object(g.Path, 'exists', return_value=True), patch.object(g.os, 'readlink', return_value='/opt/google/chrome/chrome'), patch.object(g.Path, 'read_bytes', return_value=b'/opt/google/chrome/chrome --user-data-dir=/home/dev/.local/share/mola/browser/profile --no-first-run\0'):
    assert g.profile_process(7, '/home/dev/.local/share/mola/browser/profile'), 'Chromium rewritten process title rejected'
    assert not g.profile_process(7, '/home/dev/.local/share/mola/browser/profile-other'), 'profile prefix matched another profile'

def rejects(callback, code=None):
    try: callback()
    except g.ToolError as error:
        assert code is None or error.code == code, (error.code, code)
    else: raise AssertionError('unsafe request accepted')

for url in ['https://user:password@example.com/', 'file:///tmp/profile', 'https://example.com/#secret', 'javascript:alert(1)']:
    rejects(lambda: g.web_url(url))
assert g.web_url('https://EXAMPLE.COM') == 'https://example.com/'
for crop in [{'x':-1,'y':0,'width':5,'height':5}, {'x':9,'y':0,'width':5,'height':5}, {'x':0,'y':0,'width':True,'height':5}]:
    rejects(lambda: g.crop_box(crop, 10, 10))
rejects(lambda: g.installer_spec({'kind':'apt','name':'demo','packages':['-oAPT::Pre-Invoke=payload']}), 'invalid_package_name')
rejects(lambda: g.installer_spec({'kind':'portable','name':'demo','url':'https://example.com/tool','version':'1'}), 'artifact_sha256_required')
rejects(lambda: g.loopback_http({'port':9222,'path':'/json/list'}), 'invalid_loopback_port')
rejects(lambda: g.loopback_http({'port':8080,'path':'//evil.test/'}), 'invalid_loopback_path')
rejects(lambda: g.view_input({'watch_only':True,'mode':'desktop','action':'click','x':1,'y':1}), 'viewer_watch_only')
rejects(lambda: g.view_input({'scope':{'mode':'window'},'input':{'mode':'desktop','action':'click'}}), 'viewer_input_scope_override')
class OriginFrame:
    def __init__(self,url):self.url=url
    def evaluate(self,expression):return {'url':self.url,'title':'fixture','width':800,'height':600}
g.browser_area(OriginFrame('https://example.com/path?state=one#route'),{'allowed_origin':'https://example.com:443'})
rejects(lambda:g.browser_area(OriginFrame('https://user@example.com/path#route'),{'allowed_origin':'https://example.com'}),'browser_view_origin_mismatch')
rejects(lambda:g.browser_area(OriginFrame('https://example.com:444/path'),{'allowed_origin':'https://example.com'}),'browser_view_origin_mismatch')

with tempfile.TemporaryDirectory() as temporary:
    home=pathlib.Path(temporary)
    with patch.object(g.Path, 'home', return_value=home):
        folder=g.state_dir()/'installs'; folder.mkdir(); source=pathlib.Path(sys.argv[1]).read_text(); g.__source__=source
        started=[]
        class Child: pid=777
        def launch(args, **kwargs): started.append((args,kwargs)); return Child()
        with patch.object(g.subprocess,'Popen',side_effect=launch):
            request={'kind':'script','name':'demo','script':'exit 0','operation_id':'install-once','timeout_seconds':10}
            assert g.apps('app_install',request)['status']=='queued'
            assert g.apps('app_install',request)['status']=='queued'
            assert len(started)==1
            rejects(lambda:g.apps('app_install',{**request,'script':'exit 1'}),'installer_operation_payload_mismatch')
        path=folder/'install-once.json'; job=g.read(path); job['created_at']-=20; g.save(path,job)
        assert g.apps('app_status',{'operation_id':'install-once'})['status']=='outcome_unknown'
        # Worker executes an actual isolated shell program; no installer output
        # or script text is returned from status, and a failure is terminal.
        g.install_worker(path)
        assert g.app_status('install-once')['status']=='completed'
        failed=folder/'failure.json'; g.save(failed,{'spec':{**job['spec'],'script':'printf secret-output; exit 3'},'status':'queued','created_at':0})
        g.install_worker(failed)
        status=g.app_status('failure'); assert status['status']=='failed' and 'secret-output' not in json.dumps(status)
        # Staged portable software is verified before switching the current
        # symlink; a broken version leaves the old executable/customer data.
        app=home/'.local/share/mola/apps/demo'; release=app/'releases/1'; release.mkdir(parents=True)
        binary=release/'app'; binary.write_text('#!/bin/sh\necho version-one\n'); binary.chmod(0o700)
        g.save(release/'release.json',{'version':'1','sha256':g.hashlib.sha256(binary.read_bytes()).hexdigest()})
        g.software('software_activate',{'name':'demo','version':'1'})
        other=app/'releases/2'; other.mkdir(); (other/'app').write_text('#!/bin/sh\nexit 4\n'); (other/'app').chmod(0o700)
        g.save(other/'release.json',{'version':'2','sha256':g.hashlib.sha256((other/'app').read_bytes()).hexdigest()})
        rejects(lambda:g.software('software_activate',{'name':'demo','version':'2'}),'guest_dependency_unavailable')
        assert (app/'current').resolve()==release.resolve()
        g.software('software_pin',{'name':'demo','version':'1'})
        rejects(lambda:g.software('software_activate',{'name':'demo','version':'2'}),'software_version_pinned')
        with patch.object(g,'targets',side_effect=g.ToolError('browser_not_running')), patch.object(g,'windows',return_value=[]):
            saved=g.sessions('session_save',{'apps':['demo']}); assert saved['manifest']['apps']==['demo']
        with patch.object(g,'apps',side_effect=g.ToolError('app_not_registered_or_ready')):
            assert g.sessions('session_restore',{})['errors']==['app_restore_unavailable']
        # Cropping is passed to the local capture program before any image is
        # base64 encoded; no full-frame request is returned after errors.
        calls=[]
        def output(args, **kwargs): calls.append(args); return b'cropped-png'
        with patch.object(g,'screen',return_value={'width':100,'height':100}), patch.object(g,'command',side_effect=output):
            g.capture({'mode':'desktop','crop':{'x':5,'y':6,'width':7,'height':8}})
        assert calls==[['import','-window','root','-crop','7x8+5+6','+repage','png:-']]
        calls=[]
        def display_command(args, **kwargs):
            calls.append(args)
            return 'VNC-0 connected 1280x800+0+0\n' if args==['xrandr','--query'] else ''
        with patch.object(g,'desktop_env'), patch.object(g,'command',side_effect=display_command), patch.object(g,'screen',return_value={'width':1440,'height':900}):
            assert g.geometry({'width':1440,'height':900})=={'width':1440,'height':900}
        assert calls[-1]==['xrandr','--output','VNC-0','--mode','MOLA-1440x900']
        assert any('--newmode' in call for call in calls)
        with patch.object(g,'desktop_env'), patch.object(g,'command',side_effect=display_command), patch.object(g,'screen',return_value={'width':1280,'height':800}):
            rejects(lambda:g.geometry({'width':1440,'height':900}),'geometry_change_not_applied')

class Browser:
    def __init__(self): self.calls=[]
    def evaluate(self, expression): self.calls.append(expression); return 'https://wrong.example/'
    def close(self): pass
browser=Browser()
with patch.object(g,'page',return_value=({}, {}, browser)):
    rejects(lambda:g.vault({'action':'browser_login','url':'https://example.com/login','password':'private'}),'vault_browser_url_mismatch')
assert browser.calls==['location.href'], 'wrong-origin vault dispatched secret'
assert g.dispatch({'kind':'vault-inject','action':'browser_login','url':'https://user:private@example.com','password':'private'})=={'ok':False,'status':400,'code':'invalid_browser_url'}
descriptor=g.os.open('/dev/null',g.os.O_RDWR)
class Typed: returncode=0
def anonymous_type(args,**options):
    assert 'private-memory-fixture' not in json.dumps(args)
    assert options['pass_fds']==(descriptor,) and args[-1]=='/proc/self/fd/'+str(descriptor)
    return Typed()
with patch.object(g,'command',side_effect=['0x1','WM_CLASS = "owned-app", "OwnedApp"']),patch.object(g.os,'memfd_create',return_value=descriptor,create=True),patch.object(g.tempfile,'TemporaryFile',side_effect=AssertionError('secret touched disk')),patch.object(g.subprocess,'run',side_effect=anonymous_type):
    assert g.vault({'action':'type_secret','value':'private-memory-fixture','expected_app':'OwnedApp'})=={'success':True}
print('guest tool isolation fixtures passed')
