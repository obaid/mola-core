# Private Ubuntu browser proxy binding

The authenticated host create API accepts an optional `browser_proxy` object for `ubuntu-xfce:*` images:

```json
{"provider":"decodo","host":"gate.decodo.com","port":7000,"username":"<generated-proxy-username>","password":"<proxy-password>"}
```

Only validated Decodo hostnames and HTTP proxy credentials are accepted. The native runner installs a trusted Python bridge, Chrome policies and root-owned credentials into the disposable ext4 copy before boot. Existing Ubuntu images are supported without rebuilding their base. The bridge binds `127.0.0.1:18888`, drops privileges after reading its mode-0600 credentials and never falls back to direct connections. It resolves the gateway to public numeric addresses before connecting, adds authentication only to gateway requests and tunnels TLS without decrypting it. Chrome policies disable QUIC and non-proxied WebRTC UDP. Localhost is bypassed for local applications.

This configures Chrome, including normal agent launches. It is not an OS-wide VPN or an isolation boundary against a guest owner with sudo. Other applications keep normal egress. Country and session targeting come from the supplied Decodo endpoint/username; core does not rewrite them or guarantee the provider's country, session lifetime, IP reputation or site access.

Secrets stay in the private mode-0600 core registry and guest configuration; descriptions never include them. Operation fingerprints contain a hash of the full nested binding, preventing a changed-credential retry from replaying an unrelated saved result. Credentials do not enter the lifecycle journal.

On restore, the control plane's destination binding is reinstalled by reseeding. Forks without a proxy remove the source's active Chrome proxy files and service. Snapshot disks remain sensitive: deleted data can survive in filesystem free space. Cloud requires its existing sensitive-state acknowledgement. Destroyed machine tombstones clear the credential binding.

Validation includes HTTP forwarding, opaque CONNECT tunnels, authentication replacement and failure responses, durable host retry/fencing, real ext4 installation/removal and a disposable Ubuntu KVM boot/restart on Hetzner with invalid fixture credentials. A real successful Decodo session requires customer test credentials.
