# Chromium CDP through a Mola tunnel

This example launches Chromium inside an existing ready Mola computer, requests
a machine-scoped tunnel to port 9222, bridges that WebSocket to a local TCP
port, and connects Playwright over CDP. It loads a page, clicks a button, checks
the resulting DOM, then closes the browser, tunnel, bridge, and guest Chromium.

```sh
cd examples/chromium-cdp/playwright
npm install
MOLA_API=http://127.0.0.1:4141/v1 MOLA_TOKEN=... MOLA_MACHINE=... npm start
```

Playwright is confined to this example package. Mola retains only its generic
port-tunnel implementation.
