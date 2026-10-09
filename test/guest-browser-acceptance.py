"""Real CDP acceptance against a NEW temporary profile, never user's Chrome.

Usage: python3 test/guest-browser-acceptance.py /path/to/chrome
The caller supplies an existing executable; this script never installs a browser.
"""
import http.server
import importlib.util
import json
import pathlib
import shutil
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch
import urllib.request

source=pathlib.Path(__file__).parent.parent/'runtime/guest_tools.py'
spec=importlib.util.spec_from_file_location('tools',source);g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        html=b'''<!doctype html><title>isolated CDP fixture</title><form><input autocomplete="username"><input type="password"><input id="otp"><button type="button" onclick="document.body.dataset.clicked='yes'">Submit</button></form><p>known browser marker</p>'''
        self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(html)));self.end_headers();self.wfile.write(html)

with tempfile.TemporaryDirectory(prefix='mola-browser-qualification-') as temporary:
    root=pathlib.Path(temporary);profile=root/'profile';profile.mkdir()
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(root/'key.pem'),'-out',str(root/'cert.pem'),'-days','1','-subj','/CN=localhost'],capture_output=True,check=True)
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(root/'cert.pem',root/'key.pem');server.socket=context.wrap_socket(server.socket,server_side=True)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    url='https://127.0.0.1:'+str(server.server_port)+'/login'
    # Certificate override is limited to this throwaway local fixture process.
    child=subprocess.Popen([sys.argv[1],'--headless=new','--user-data-dir='+str(profile),'--remote-debugging-address=127.0.0.1','--remote-debugging-port=0','--no-first-run','--no-default-browser-check','--ignore-certificate-errors','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader','--window-size=800,600',url],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    try:
        for _ in range(200):
            if (profile/'DevToolsActivePort').exists():break
            assert child.poll() is None,'disposable browser launch failed';time.sleep(.1)
        port=int((profile/'DevToolsActivePort').read_text().splitlines()[0])
        state={'profile':str(profile),'pid':child.pid,'mode':'full','current_tab':None}
        def pages():
            with urllib.request.urlopen('http://127.0.0.1:'+str(port)+'/json/list',timeout=5) as response:values=json.load(response)
            return state,[p for p in values if p['type']=='page']
        with patch.object(g,'targets',side_effect=pages),patch.object(g,'state_dir',return_value=root):
            for _ in range(100):
                snapshot=g.browser('browser_snapshot',{})
                if 'known browser marker' in snapshot['text']:break
                time.sleep(.1)
            assert snapshot['url']==url and snapshot['elements']
            inputs=[e for e in snapshot['elements'] if e['tag']=='input'];username=next(e for e in inputs if e['type']=='text')
            g.browser('browser_fill',{'snapshot_id':snapshot['snapshot_id'],'ref':username['ref'],'value':'ordinary-user'})
            _,_,cdp=g.page({})
            try: assert cdp.evaluate('document.querySelector("input").value')=='ordinary-user'
            finally:cdp.close()
            vault_result=g.vault({'action':'browser_login','url':url,'username':'isolated-user','password':'private-fixture-password','totp':'123456','totp_selector':'#otp'})
            assert vault_result=={'success':True} and 'private' not in json.dumps(vault_result)
            _,_,cdp=g.page({})
            try:
                assert cdp.evaluate('document.querySelector("input[type=password]").value==="private-fixture-password"') is True
                assert cdp.evaluate('document.querySelector("#otp").value==="123456"') is True
                webgl=cdp.evaluate('(()=>{const g=document.createElement("canvas").getContext("webgl");if(!g)return {available:false};const e=g.getExtension("WEBGL_debug_renderer_info");return {available:true,renderer:e?g.getParameter(e.UNMASKED_RENDERER_WEBGL):g.getParameter(g.RENDERER)};})()')
            finally:cdp.close()
            crop={'x':10,'y':20,'width':100,'height':80};capture=g.browser('browser_screenshot',{'crop':crop})
            raw=g.base64.b64decode(capture['image_base64']);assert raw[:8]==b'\x89PNG\r\n\x1a\n' and struct.unpack('!II',raw[16:24])==(100,80)
            tab=pages()[1][0]['id'];origin=url.split('/login')[0]
            scope={'mode':'browser','browser_tab':tab,'selector':'form','allowed_origin':origin}
            _,_,cdp=g.page({'tab_id':tab})
            try:cdp.evaluate('document.querySelector("input").focus()')
            finally:cdp.close()
            assert g.view_input({**scope,'action':'type','text':'-scoped'})=={'success':True}
            selector_frame=g.capture(scope);assert selector_frame['tab_id']==tab and selector_frame['url']==url
            selector_png=g.base64.b64decode(selector_frame['image_base64']);assert struct.unpack('!II',selector_png[16:24])==(selector_frame['geometry']['width'],selector_frame['geometry']['height'])
            _,_,cdp=g.page({'tab_id':tab})
            try:cdp.evaluate('document.querySelector("p").tabIndex=0;document.querySelector("p").focus();document.querySelector("form").style.display="none"')
            finally:cdp.close()
            assert g.capture(scope)=={'available':False,'reason':'target_not_visible','tab_id':tab}
            try:g.view_input({**scope,'action':'key','key':'Enter'})
            except g.ToolError:pass
            else:raise AssertionError('offscreen scoped input accepted')
            _,_,cdp=g.page({'tab_id':tab})
            try:cdp.evaluate('document.querySelector("form").style.display=""')
            finally:cdp.close()
            current=g.browser('browser_snapshot',{})
            try:g.browser('browser_click',{'snapshot_id':snapshot['snapshot_id'],'ref':username['ref']})
            except g.ToolError:pass
            else:raise AssertionError('stale browser reference accepted')
            try:g.vault({'action':'browser_login','url':url+'?different=1','password':'never-injected'})
            except g.ToolError as error:assert error.code=='vault_browser_url_mismatch'
            else:raise AssertionError('different exact URL accepted')
            print(json.dumps({'outcome':'passed','engine':'real Chrome CDP','structured_snapshot':True,'reference_fencing':True,'fill':True,'url_bound_password_totp':True,'cropped_png_dimensions':[100,80],'webgl':webgl,'profile':'disposable','signed_in_migration_qualification':False,'lightweight_qualification':False}))
    finally:
        child.terminate()
        try:child.wait(timeout=10)
        except subprocess.TimeoutExpired:child.kill();child.wait()
        server.shutdown();server.server_close();worker.join()
