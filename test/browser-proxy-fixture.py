import base64
import importlib.util
import socket
import sys
import threading

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


server = m.Server(('127.0.0.1', 0), config, connector=connector)
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
print('HTTP forwarding, CONNECT tunnel, gateway-only authentication and fail-closed behavior passed')
