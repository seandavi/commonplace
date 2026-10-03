# Security policy

## Security model

commonplace has no authentication of its own. Run the HTTP server only on a
network you trust: the deployment scripts in `deploy/` bind it to the
machine's Tailscale address, so only devices on your tailnet can reach it.
Anyone who can reach the port can read, write and forget every memory.

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
