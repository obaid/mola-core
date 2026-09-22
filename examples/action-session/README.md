# Persistent action sessions

The Node example sends 100 correlated actions over one WebSocket, then performs
a SHA-256-verified binary file round trip and binary screenshot. It surfaces
server backpressure codes, times every request, and never retries a request
whose result is uncertain.

```sh
export MOLA_API=http://127.0.0.1:4141/v1
export MOLA_TOKEN=...
export MOLA_MACHINE=...
node examples/action-session/node/index.mjs
```

A session URL can be redeemed once, expires, and is tied to one machine boot.
A close with code 4001 means the machine lifecycle changed or the session
expired. Obtain a new grant. Reconnect only for future actions; never replay an
action that timed out or lost its connection unless it is independently known
to be idempotent.

The Python version uses `websocket-client` and demonstrates the same ordered
request-id contract without adding a production dependency.
