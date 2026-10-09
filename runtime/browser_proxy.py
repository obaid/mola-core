"""Loopback-only HTTP/CONNECT bridge for Chrome's authenticated upstream proxy.

No direct fallback, credential logging, or TLS interception. HTTPS travels in
an opaque CONNECT tunnel; authentication is sent only to the Decodo gateway.
"""
import base64
import ipaddress
import json
import os
from pathlib import Path
import re
import select
import socket
import socketserver
import threading
import secrets
import time


class Meter:
    """Observed proxy wire bytes; never credentials, URLs, or billing truth."""
    def __init__(self, folder, boot_id=None):
        self.folder = Path(folder)
        if self.folder.is_symlink(): raise ValueError('Unsafe proxy meter directory')
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.folder / 'counters.json'
        if self.path.is_symlink(): raise ValueError('Unsafe proxy meter file')
        self.guard = threading.Lock()
        self.unflushed, self.last_flush = 0, time.monotonic()
        boot_id = boot_id or Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {'bytes_up': 0, 'bytes_down': 0, 'connections': 0}
        if self.state.get('boot_id') != boot_id:
            self.state.update(boot_id=boot_id, counter_epoch=secrets.token_hex(16))
        self.add()

    def add(self, defer=False, **increments):
        with self.guard:
            for key,value in increments.items():
                if key not in ('bytes_up', 'bytes_down', 'connections') or type(value) is not int or value < 0: raise ValueError('Invalid proxy counter')
                self.state[key] += value
                self.unflushed += value
            if defer and self.unflushed < 65536 and time.monotonic() - self.last_flush < 1: return
            temporary = self.path.with_suffix('.new')
            with temporary.open('w') as stream:
                json.dump(self.state, stream); stream.flush(); os.fsync(stream.fileno())
            temporary.chmod(0o600); temporary.replace(self.path)
            descriptor = os.open(self.folder, os.O_RDONLY)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
            self.unflushed, self.last_flush = 0, time.monotonic()


class CountingSocket:
    def __init__(self, stream, meter): self.stream, self.meter = stream, meter
    def fileno(self): return self.stream.fileno()
    def settimeout(self, timeout): return self.stream.settimeout(timeout)
    def sendall(self, data):
        self.stream.sendall(data); self.meter.add(defer=True, bytes_up=len(data))
    def recv(self, size):
        data = self.stream.recv(size)
        if data: self.meter.add(defer=True, bytes_down=len(data))
        return data
    def close(self):
        try: self.meter.add()
        finally: self.stream.close()

HOST = re.compile(r'^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+decodo\.com$')


def validate(config):
    if not isinstance(config, dict) or set(config) != {'provider', 'host', 'port', 'username', 'password'}:
        raise ValueError('Invalid proxy configuration')
    if config['provider'] != 'decodo' or not isinstance(config['host'], str) or len(config['host']) > 253 or not HOST.fullmatch(config['host']):
        raise ValueError('Invalid proxy endpoint')
    if type(config['port']) is not int or not 1 <= config['port'] <= 65535:
        raise ValueError('Invalid proxy port')
    for key in ['username', 'password']:
        value = config[key]
        if not isinstance(value, str) or not 1 <= len(value) <= 512 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('Invalid proxy authentication')
    if ':' in config['username']: raise ValueError('Invalid proxy username')
    return config


def connect(config):
    addresses = socket.getaddrinfo(config['host'], config['port'], type=socket.SOCK_STREAM)
    # Pin the numeric address after checking it: DNS cannot redirect credentials
    # into the host/guest private network between validation and connection.
    for family, kind, protocol, _, address in addresses:
        if not ipaddress.ip_address(address[0]).is_global: continue
        upstream = socket.socket(family, kind, protocol)
        upstream.settimeout(15)
        try:
            upstream.connect(address)
            return upstream
        except OSError:
            upstream.close()
    raise OSError('Proxy unavailable')


def header(stream):
    result = bytearray()
    while not result.endswith(b'\r\n\r\n'):
        chunk = stream.recv(1)
        if not chunk or len(result) >= 65536: raise OSError('Invalid proxy request')
        result.extend(chunk)
    return bytes(result)


def exact_copy(source, target, remaining):
    while remaining:
        data = source.recv(min(65536, remaining))
        if not data: raise OSError('Incomplete request body')
        target.sendall(data)
        remaining -= len(data)


def line(stream):
    value = bytearray()
    while not value.endswith(b'\r\n'):
        chunk = stream.recv(1)
        if not chunk or len(value) >= 8192: raise OSError('Invalid body framing')
        value.extend(chunk)
    return bytes(value)


def copy_body(source, target, fields):
    lengths = [v for k, v in fields if k.lower() == 'content-length']
    encoding = [v for k, v in fields if k.lower() == 'transfer-encoding']
    if len(lengths) > 1 or len(encoding) > 1 or (lengths and encoding): raise ValueError('Ambiguous body framing')
    if lengths:
        if not re.fullmatch(r'[0-9]{1,15}', lengths[0]): raise ValueError('Invalid body length')
        exact_copy(source, target, int(lengths[0]))
    elif encoding:
        if encoding[0].lower() != 'chunked': raise ValueError('Unsupported body encoding')
        while True:
            chunk_header = line(source)
            size_text = chunk_header.split(b';', 1)[0].strip()
            if not re.fullmatch(b'[0-9a-fA-F]{1,15}', size_text): raise ValueError('Invalid chunk')
            size = int(size_text, 16)
            target.sendall(chunk_header)
            if size == 0:
                # Chrome does not send trailers. Refuse them rather than
                # letting a second authentication/header set reach the proxy.
                if line(source) != b'\r\n': raise ValueError('Unsupported trailers')
                target.sendall(b'\r\n')
                break
            exact_copy(source, target, size)
            if line(source) != b'\r\n': raise ValueError('Invalid chunk end')
            target.sendall(b'\r\n')


def relay(left, right):
    while True:
        readable, _, _ = select.select([left, right], [], [], 120)
        if not readable: return
        for source in readable:
            data = source.recv(65536)
            if not data: return
            (right if source is left else left).sendall(data)


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        upstream = None
        connected = False
        try:
            self.request.settimeout(15)
            lines = header(self.request).decode('iso-8859-1').split('\r\n')
            method, target, version = lines[0].split(' ')
            if version not in ['HTTP/1.0', 'HTTP/1.1']: raise ValueError('Invalid HTTP version')
            if method == 'CONNECT':
                if not re.fullmatch(r'(?:[A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\]):[0-9]{1,5}', target): raise ValueError('Invalid tunnel')
            elif not target.startswith('http://'): raise ValueError('Absolute HTTP target required')
            fields = []
            for line in lines[1:-2]:
                key, value = line.split(':', 1)
                if not re.fullmatch(r'[!#$%&\'*+.^_`|~0-9A-Za-z-]+', key): raise ValueError('Invalid header')
                if key.lower() not in ['proxy-authorization', 'proxy-connection', 'connection', 'expect']:
                    fields.append((key, value.strip()))
            upstream = self.server.connector(self.server.config)
            if self.server.meter:
                self.server.meter.add(connections=1)
                upstream = CountingSocket(upstream, self.server.meter)
            credential = base64.b64encode((self.server.config['username'] + ':' + self.server.config['password']).encode()).decode()
            fields += [('Proxy-Authorization', 'Basic ' + credential)]
            if method != 'CONNECT': fields.append(('Connection', 'close'))
            wire = lines[0] + '\r\n' + ''.join(key + ': ' + value + '\r\n' for key, value in fields) + '\r\n'
            upstream.sendall(wire.encode('iso-8859-1'))
            if method != 'CONNECT':
                if any(value.lower().startswith('expect:') for value in lines[1:-2]):
                    self.request.sendall(b'HTTP/1.1 100 Continue\r\n\r\n')
                # Send the body before waiting for the upstream response: HTTP
                # POST/PUT otherwise deadlock while each side waits for bytes.
                copy_body(self.request, upstream, fields)
            response = header(upstream)
            while response.split(b' ', 2)[1] in [b'100', b'102', b'103']:
                response = header(upstream)
            code = response.split(b' ', 2)[1]
            if code == b'407' or (method == 'CONNECT' and code != b'200'): raise OSError('Proxy refused connection')
            self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n' if method == 'CONNECT' else response)
            connected = True
            self.request.settimeout(120)
            upstream.settimeout(120)
            relay(self.request, upstream)
        except (OSError, ValueError, UnicodeError, IndexError):
            if not connected:
                try: self.request.sendall(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                except OSError: pass
        finally:
            if upstream is not None: upstream.close()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, config, connector=connect, meter=None):
        self.config = validate(config)
        self.connector = connector
        self.meter = meter
        self.slots = threading.BoundedSemaphore(128)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # socketserver's default traceback can include application context.
        pass


if __name__ == '__main__':
    config = validate(json.loads(Path('/etc/mola/browser-proxy.json').read_text()))
    meter_folder = Path('/var/lib/mola/browser-proxy')
    if meter_folder.is_symlink(): raise ValueError('Unsafe proxy meter directory')
    meter_folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.geteuid() == 0:
        os.chown(meter_folder, 65534, 65534)
        for path in meter_folder.iterdir():
            if path.is_symlink(): raise ValueError('Unsafe proxy meter file')
            if path.is_file(): os.chown(path, 65534, 65534)
    server = Server(('127.0.0.1', 18888), config, meter=Meter(meter_folder))
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(65534)
        os.setuid(65534)
    server.serve_forever()
