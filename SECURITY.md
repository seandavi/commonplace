# Security policy

## Security model

commonplace has no authentication of its own. The only network requirement
is that clients can reach the server's address and port; anyone who can
reach it can read, write and forget every memory. So bind the server to a
network only your machines can reach. A Tailscale tailnet is the common
choice, and the scripts in `deploy/` bind the server to the machine's
Tailscale address. A LAN behind a firewall, a WireGuard or other VPN, an
SSH tunnel to a server bound to `127.0.0.1`, or a reverse proxy that adds
TLS and authentication work too. Don't expose the port to the internet.

Memories are text that other agents load into their context. Treat them as
untrusted data: the server instructions and the session index tell agents
never to follow instructions found in a memory, and `history` and `export`
show who wrote what. Never store secrets, credentials or tokens in
commonplace.

## Supported versions

Only the latest commit on `main` is supported.

## Reporting a vulnerability

Report privately through GitHub: open
https://github.com/seandavi/commonplace/security and choose "Report a
vulnerability". Please don't open a public issue for security problems.
