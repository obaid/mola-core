"""Private pinned country checker through the managed loopback proxy only."""
import hashlib
import hmac
import http.client
import json
import os
import re
import ssl
import stat
import signal
import threading
from contextlib import contextmanager
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit


class ProbeError(Exception):
    pass


def require(value):
    if not value:
        raise ProbeError('network_egress_unverified')


@contextmanager
def absolute_deadline():
    # This module is executed in an isolated Linux main-thread process. Socket
    # timeouts alone do not bound a peer that drips CONNECT or HTTP headers.
    require(threading.current_thread() is threading.main_thread())
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    def expired(signum, frame):
        raise ProbeError('network_egress_unverified')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, min(10, previous_timer[0]) if previous_timer[0] > 0 else 10)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, max(0.000001, previous_timer[0] - (time.monotonic() - started)), previous_timer[1])


def policy(arguments, binding):
    require(isinstance(arguments, dict) and isinstance(binding, dict))
    url = arguments.get('checker_url')
    pin = arguments.get('certificate_sha256')
    require(isinstance(url, str) and len(url) <= 2048 and isinstance(pin, str) and re.fullmatch(r'[0-9a-f]{64}', pin))
    parsed = urlsplit(url)
    require(parsed.scheme == 'https' and parsed.hostname and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment and parsed.port in [None, 443]
            and re.fullmatch(r'[A-Za-z0-9.-]{1,253}', parsed.hostname))
    require(type(arguments.get('expected_generation')) is int and arguments['expected_generation'] >= 1
            and arguments['expected_generation'] == binding.get('generation'))
    require(isinstance(arguments.get('expected_boot_id'), str) and arguments['expected_boot_id'] == binding.get('boot_id'))
    require(isinstance(arguments.get('configuration_id'), str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', arguments['configuration_id']))
    checker = hashlib.sha256((url + '\0' + pin).encode()).hexdigest()
    return parsed, pin, checker


def kernel_boot():
    value = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    require(re.fullmatch(r'[0-9a-f-]{36}', value))
    return value


def current_configuration():
    path = '/etc/mola/browser-proxy-binding.json'
    for parent in Path(path).parents:
        info = parent.stat(follow_symlinks=False)
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o077 and info.st_size <= 16384)
        with os.fdopen(fd, 'r', closefd=False) as stream:
            data = json.load(stream)
    finally:
        os.close(fd)
    require(data.get('status') == 'configured')
    require(subprocess.run(['systemctl', 'is-active', '--quiet', 'mola-browser-proxy.service'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1).returncode == 0)
    return data


def _probe(arguments, binding, connection_factory=None, observe=current_configuration, boot=kernel_boot, clock=time.monotonic):
    parsed, pin, checker = policy(arguments, binding)
    start = clock()
    first = observe()
    require(first.get('configuration_id') == arguments['configuration_id'] and boot() == binding['boot_id'])
    context = ssl.create_default_context()  # Required CA and hostname validation in addition to pinning.
    factory = connection_factory or http.client.HTTPSConnection
    connection = factory('127.0.0.1', 18888, timeout=8, context=context)
    try:
        connection.set_tunnel(parsed.hostname, 443)
        connection.connect()
        certificate = connection.sock.getpeercert(binary_form=True)
        require(certificate and hmac.compare_digest(hashlib.sha256(certificate).hexdigest(), pin))
        tls_socket = connection.sock
        tls_socket.settimeout(max(0.01, 8 - (clock() - start)))
        connection.request('GET', parsed.path or '/', headers={'Accept': 'application/json', 'Connection': 'close'})
        require(clock() - start < 8)
        tls_socket.settimeout(8 - (clock() - start))
        response = connection.getresponse()
        require(response.status == 200)  # No redirects or retry/direct fallback.
        length = response.getheader('Content-Length')
        require(length is None or length.isdigit() and int(length) <= 16384)
        chunks = []; size = 0
        while size <= 16384:
            remaining = 8 - (clock() - start)
            require(remaining > 0)
            chunk = response.read1(min(1024, 16385 - size))
            if not chunk: break
            chunks.append(chunk); size += len(chunk)
        data = b''.join(chunks)
        require(len(data) <= 16384)
        value = json.loads(data)
        require(isinstance(value, dict) and isinstance(value.get('country_code'), str) and re.fullmatch(r'[A-Z]{2}', value['country_code']))
        last = observe()
        require(last == first and boot() == binding['boot_id'] and clock() - start <= 10)
        return {'configuration_id': arguments['configuration_id'], 'runtime_generation': binding['generation'],
                'boot_id': binding['boot_id'], 'checker_id': checker, 'country_code': value['country_code'],
                'service_active': True, 'proxy_only': True, 'tls_verified': True, 'certificate_pinned': True}
    finally:
        connection.close()


def probe(arguments, binding, **options):
    with absolute_deadline():
        return _probe(arguments, binding, **options)


def main():
    import sys
    try:
        request = json.load(sys.stdin)
        result = probe(request['arguments'], request['binding'])
        print(json.dumps({'ok': True, 'result': result}))
    except Exception:
        print(json.dumps({'ok': False, 'status': 503, 'code': 'network_egress_unverified'}))


if __name__ == '__main__':
    main()
