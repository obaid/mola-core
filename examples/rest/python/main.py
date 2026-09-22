#!/usr/bin/env python3
"""Minimal public REST API example. Uses only Python's standard library."""
import base64
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("MOLA_API", "http://127.0.0.1:4141/v1").rstrip("/")
TOKEN = os.environ.get("MOLA_TOKEN")
if not TOKEN:
    raise SystemExit("Set MOLA_TOKEN to the Mola operator token.")

def call(path, method="GET", body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": "Bearer " + TOKEN,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.load(response).get("data")
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"Mola returned HTTP {error.code}: {detail[:300]}") from error

def wait_for(machine_id, status, timeout=180):
    deadline, last = time.time() + timeout, "unknown"
    while time.time() < deadline:
        machine = call(f"/machines/{machine_id}")
        last = machine["status"]
        if last == status:
            return machine
        if last in ("failed", "error"):
            raise RuntimeError(f"Machine became {last} while waiting for {status}.")
        time.sleep(1)
    raise TimeoutError(f"Machine was still {last} after {timeout} seconds.")

machine = None
keep = "--keep" in sys.argv
try:
    machine = call("/machines", "POST", {"name": "python-rest-example", "vcpus": 1, "memory_mb": 2048, "disk_gb": 20})
    wait_for(machine["id"], "ready")
    action = lambda payload: call(f"/machines/{machine['id']}/actions", "POST", payload)
    print(action({"action": "exec", "command": "uname -a"})["stdout"].strip())
    action({"action": "write_file", "path": "~/mola-example.txt", "content": "hello from Mola\n"})
    restored = base64.b64decode(action({"action": "read_file", "path": "~/mola-example.txt"})["content_base64"]).decode()
    assert restored == "hello from Mola\n"
    screenshot = action({"action": "screenshot"})
    screenshot_path = Path(__file__).with_name("mola-example.png")
    screenshot_path.write_bytes(base64.b64decode(screenshot["image_base64"]))
    call(f"/machines/{machine['id']}/stop", "POST", {})
    wait_for(machine["id"], "stopped")
    print({"id": machine["id"], "file": restored, "screenshot": str(screenshot_path)})
finally:
    if machine and not keep:
        call(f"/machines/{machine['id']}", "DELETE")
