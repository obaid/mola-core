import hashlib, importlib.util, json, sys, unittest, signal, time
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('egress',sys.argv.pop(1)); module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
CERT=b'owned-test-certificate'
class Socket:
    def getpeercert(self,binary_form):return CERT
    def settimeout(self,value):pass
class Response:
    status=200
    body=b'{"country_code":"US","ip":"never-return"}'
    def getheader(self,name):return None
    def read1(self,count):data=self.body[:count];self.body=self.body[count:];return data
class Connection:
    def __init__(self,*args,**kwargs):self.sock=Socket();self.response=Response();self.calls=[];self.closed=False
    def set_tunnel(self,*args):self.calls.append(args)
    def connect(self):pass
    def request(self,*args,**kwargs):self.calls.append(args)
    def getresponse(self):return self.response
    def close(self):self.closed=True
class Tests(unittest.TestCase):
    def setUp(self):
        self.args={'checker_url':'https://checker.example/country','certificate_sha256':hashlib.sha256(CERT).hexdigest(),'configuration_id':'owned-config','expected_generation':1,'expected_boot_id':'boot'}
        self.binding={'generation':1,'boot_id':'boot'};self.config={'status':'configured','configuration_id':'owned-config'};self.connection=Connection()
    def run_probe(self,**kw):return module.probe(self.args,self.binding,connection_factory=lambda *a,**k:self.connection,observe=kw.get('observe',lambda:self.config.copy()),boot=kw.get('boot',lambda:'boot'),clock=kw.get('clock',lambda:1))
    def test_only_loopback_tunnel_and_metadata(self):
        def factory(host,port,**kw):self.assertEqual((host,port),('127.0.0.1',18888));self.assertTrue(kw['context'].check_hostname);self.assertEqual(kw['context'].verify_mode,2);return self.connection
        result=module.probe(self.args,self.binding,connection_factory=factory,observe=lambda:self.config.copy(),boot=lambda:'boot')
        self.assertEqual(result['country_code'],'US');self.assertNotIn('ip',result);self.assertEqual(self.connection.calls[0],('checker.example',443));self.assertTrue(self.connection.closed)
    def test_wrong_pin_no_http(self):
        self.args['certificate_sha256']='0'*64
        with self.assertRaises(module.ProbeError):self.run_probe()
        self.assertEqual(len(self.connection.calls),1)
    def test_redirect_refused(self):
        self.connection.response.status=302
        with self.assertRaises(module.ProbeError):self.run_probe()
    def test_oversize_refused(self):
        self.connection.response.body=b'x'*16385
        with self.assertRaises(module.ProbeError):self.run_probe()
    def test_boot_race(self):
        values=iter(['boot','changed'])
        with self.assertRaises(module.ProbeError):self.run_probe(boot=lambda:next(values))
    def test_config_race(self):
        values=iter([self.config,{'status':'configured','configuration_id':'changed'}])
        with self.assertRaises(module.ProbeError):self.run_probe(observe=lambda:next(values))
    def test_service_gone(self):
        def gone():raise module.ProbeError('inactive')
        values=iter([self.config,None])
        def observe():
            value=next(values)
            if value is None:gone()
            return value
        with self.assertRaises(module.ProbeError):self.run_probe(observe=observe)
    def test_deadline(self):
        values=iter([0]+[11]*20)
        with self.assertRaises(module.ProbeError):self.run_probe(clock=lambda:next(values))
    def test_invalid_urls(self):
        for url in ['http://checker.example/','https://user:pass@checker.example/','https://checker.example/?token=x','https://checker.example/#x','https://checker.example:444/']:
            self.args['checker_url']=url
            with self.assertRaises(module.ProbeError):self.run_probe()
    def test_bad_country(self):
        self.connection.response.body=b'{"country_code":"United States"}'
        with self.assertRaises(module.ProbeError):self.run_probe()
    def test_slow_drip_headers_have_absolute_deadline(self):
        def headers():
            while True:time.sleep(0.02)  # Every individual socket-like read succeeds.
        self.connection.getresponse=headers
        original_handler=signal.getsignal(signal.SIGALRM)
        started=time.monotonic()
        with self.assertRaises(module.ProbeError):self.run_probe()
        self.assertLess(time.monotonic()-started,11)
        self.assertTrue(self.connection.closed)
        self.assertEqual(signal.getsignal(signal.SIGALRM),original_handler)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL),(0.0,0.0))
    def test_unsafe_parent_ownership_or_write_permission_refused(self):
        from types import SimpleNamespace
        import stat
        for uid,mode in [(1,0o755),(0,0o775),(0,0o777)]:
            with patch.object(module.Path,'stat',return_value=SimpleNamespace(st_mode=stat.S_IFDIR|mode,st_uid=uid)):
                with self.assertRaises(module.ProbeError):module.current_configuration()
unittest.main()
