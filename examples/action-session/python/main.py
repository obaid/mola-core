"""Persistent action session example. Install: pip install websocket-client"""
import json
import os
import urllib.request
from websocket import create_connection

api = os.environ.get("MOLA_API", "http://127.0.0.1:4141/v1").rstrip("/")
token = os.environ["MOLA_TOKEN"]
machine = os.environ["MOLA_MACHINE"]
request = urllib.request.Request(
    f"{api}/machines/{machine}/session",
    method="POST",
    headers={"Authorization": f"Bearer {token}"},
)
with urllib.request.urlopen(request, timeout=30) as response:
    grant = json.load(response)["data"]

socket = create_connection(grant["session_url"], timeout=30, origin=None)
try:
    ready = json.loads(socket.recv())
    assert ready["type"] == "ready"
    for index in range(100):
        socket.send(json.dumps({
            "id": str(index),
            "op": "action",
            "action": {"action": "exec", "command": "true"},
        }))
        result = json.loads(socket.recv())
        if not result.get("ok"):
            raise RuntimeError(result["error"]["message"])
    print("100 actions completed over one session")
finally:
    socket.close()
