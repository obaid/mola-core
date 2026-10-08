import base64
import importlib.util
import socket
import sys
import threading
import tempfile
import pathlib
import json

spec = importlib.util.spec_from_file_location('browser_proxy', sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
config = {'provider': 'decodo', 'host': 'gate.decodo.com', 'port': 7000, 'username': 'test-user', 'password': 'private-fixture'}
auth = b'Proxy-Authorization: Basic ' + base64.b64encode(b'test-user:private-fixture')
requests = []


def connector(_):
    client, upstream = socket.socketpair()
    def serve():
        try:
            head = m.header(upstream)
            requests.append(head)
            assert auth in head
            assert b'not-my-credential' not in head
            if head.startswith(b'CONNECT'):
                upstream.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                data = upstream.recv(1024)
                # HTTPS application data remains an opaque tunnel.
                upstream.sendall(data)
            else:
                if head.startswith(b'POST'):
                    if b'Transfer-Encoding: chunked' in head:
                        assert m.line(upstream) == b'5\r\n'
                        assert upstream.recv(5) == b'hello'
                        assert m.line(upstream) == b'\r\n'
                        assert m.line(upstream) == b'0\r\n'
                        assert m.line(upstream) == b'\r\n'
                    else: assert upstream.recv(5) == b'hello'
                upstream.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello')
        finally: upstream.close()
    threading.Thread(target=serve, daemon=True).start()
    return client


temporary = tempfile.TemporaryDirectory(prefix='mola-proxy-meter-')
meter = m.Meter(temporary.name, boot_id='owned-fixture-boot')
server = m.Server(('127.0.0.1', 0), config, connector=connector, meter=meter)
threading.Thread(target=server.serve_forever, daemon=True).start()
address = server.server_address
with socket.create_connection(address, timeout=3) as client:
    client.sendall(b'GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\nProxy-Authorization: not-my-credential\r\n\r\n')
    response = m.header(client)
    assert b'200 OK' in response
    assert auth not in response
    assert client.recv(5) == b'hello'
with socket.create_connection(address, timeout=3) as client:
    client.sendall(b'CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n')
    assert b'200 Connection Established' in m.header(client)
    client.sendall(b'opaque-tls-record')
    assert client.recv(1024) == b'opaque-tls-record'
with socket.create_connection(address, timeout=3) as client:
    client.sendall(b'POST http://example.com/ HTTP/1.1\r\nHost: example.com\r\nContent-Length: 5\r\n\r\nhello')
    assert b'200 OK' in m.header(client)
    assert client.recv(5) == b'hello'
with socket.create_connection(address, timeout=3) as client:
    client.sendall(b'POST http://example.com/ HTTP/1.1\r\nHost: example.com\r\nTransfer-Encoding: chunked\r\nExpect: 100-continue\r\n\r\n')
    assert b'100 Continue' in m.header(client)
    client.sendall(b'5\r\nhello\r\n0\r\n\r\n')
    assert b'200 OK' in m.header(client)
    assert client.recv(5) == b'hello'
server.connector = lambda _: (_ for _ in ()).throw(OSError('private-fixture'))
with socket.create_connection(address, timeout=3) as client:
    client.sendall(b'CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n')
    response = m.header(client)
    assert b'502 Bad Gateway' in response and b'private-fixture' not in response
server.shutdown(); server.server_close()
assert len(requests) == 4
meter.add()
counters = json.loads((pathlib.Path(temporary.name) / 'counters.json').read_text())
assert counters['connections'] == 4 and counters['bytes_up'] > 0 and counters['bytes_down'] > 0
assert 'private-fixture' not in json.dumps(counters) and 'example.com' not in json.dumps(counters)
same = m.Meter(temporary.name, boot_id='owned-fixture-boot')
assert same.state == counters, 'service restart reset usage or epoch'
same.add(bytes_up=100)
next_boot = m.Meter(temporary.name, boot_id='new-owned-fixture-boot')
assert next_boot.state['counter_epoch'] != counters['counter_epoch'] and next_boot.state['bytes_up'] == counters['bytes_up'] + 100
temporary.cleanup()
print('HTTP forwarding, CONNECT tunnel, gateway-only authentication and fail-closed behavior passed')
