# Security policy

## Security model

commonplace currently has no authentication of its own, so it needs a
private or otherwise secure network. A Tailscale tailnet is the easy way to
get one, and the scripts in `deploy/` bind the server to the machine's
Tailscale address. Beyond that, the only network requirement is that
clients can reach the server's address and port; anyone who can reach it
can read, write and forget every memory. A LAN behind a firewall, a
WireGuard or other VPN, an SSH tunnel to a server bound to `127.0.0.1`, or
a reverse proxy that adds TLS and authentication work too. Don't expose the
port to the internet.

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
