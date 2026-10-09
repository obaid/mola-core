"""Owned loopback CONNECT/TLS peer; no external network or paid provider."""
import hashlib, http.client, importlib.util, json, pathlib, socket, ssl, subprocess, sys, tempfile, threading, unittest
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('egress',sys.argv.pop(1)); module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='mola-egress-tls-');folder=pathlib.Path(cls.temp.name)
        cert=folder/'certificate.pem';key=folder/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
        cls.context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);cls.context.load_cert_chain(cert,key)
        cls.trust=ssl.create_default_context(cafile=str(cert))
        cls.pin=hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def probe(self, hostname='localhost', trusted=True, pin=None):
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(3)
        port=listener.getsockname()[1];seen=[]
        def peer():
            try:
                raw,_=listener.accept();raw.settimeout(3)
                with raw:
                    headers=b''
                    while not headers.endswith(b'\r\n\r\n'):headers+=raw.recv(1)
                    seen.append(headers)
                    raw.sendall(b'HTTP/1.0 200 Connection established\r\n\r\n')
                    with self.context.wrap_socket(raw,server_side=True) as tls:
                        request=b''
                        while not request.endswith(b'\r\n\r\n'):
                            part=tls.recv(4096)
                            if not part:return
                            request+=part
                        seen.append(request)
                        body=b'{"country_code":"US","ip":"discarded-owned-IP"}'
                        tls.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(body)).encode()+b'\r\nConnection: close\r\n\r\n'+body)
            except (OSError,ssl.SSLError):pass
        worker=threading.Thread(target=peer);worker.start()
        arguments={'checker_url':'https://'+hostname+'/country','certificate_sha256':pin or self.pin,'configuration_id':'test-config','expected_generation':1,'expected_boot_id':'boot'}
        def factory(host,proxyport,**kwargs):
            self.assertEqual((host,proxyport),('127.0.0.1',18888))
            return http.client.HTTPSConnection(host,port,**kwargs)
        try:
            if trusted:
                with patch.object(module.ssl,'create_default_context',return_value=self.trust):
                    result=module.probe(arguments,{'generation':1,'boot_id':'boot'},connection_factory=factory,observe=lambda:{'status':'configured','configuration_id':'test-config'},boot=lambda:'boot')
            else:
                result=module.probe(arguments,{'generation':1,'boot_id':'boot'},connection_factory=factory,observe=lambda:{'status':'configured','configuration_id':'test-config'},boot=lambda:'boot')
            self.assertIn(('CONNECT '+hostname+':443').encode(),seen[0]);self.assertIn(b'GET /country',seen[1])
            self.assertNotIn('ip',result)
            return result
        finally:
            listener.close();worker.join(timeout=4);self.assertFalse(worker.is_alive())
    def test_actual_connect_tls_ca_hostname_and_pin(self):self.assertEqual(self.probe()['country_code'],'US')
    def test_untrusted_ca_refused(self):
        with self.assertRaises(ssl.SSLCertVerificationError):self.probe(trusted=False)
    def test_wrong_hostname_refused(self):
        with self.assertRaises(ssl.SSLCertVerificationError):self.probe(hostname='different.example')
    def test_wrong_pin_refused_after_valid_tls(self):
        with self.assertRaises(module.ProbeError):self.probe(pin='0'*64)
unittest.main()
