# Security

## Reporting a hole

Use GitHub's private report: this repo's **Security** tab, **Report a vulnerability**. Please don't open a public issue for it.

Say what you found, how to reproduce it, and what it lets someone do. We'll answer within a week. A fix goes out as a normal commit, and the report stays private until it's out.

## What's in scope

The code in this repo: the server in `backend/`, the web app in `web/`, the Chrome extension in `extension/`, and the Docker and Caddy files.

Your own server's setup (its firewall, its proxy, who has root) is yours to secure. The README's "Security notes" list what the app assumes.

## Known limits

- Marketplace keys and the Gmail app password are stored in plain text in `DATA/secrets/connections.json` (mode 600) on the server.
- The server trusts `X-Forwarded-For` from 127.0.0.1 only, and takes its last value. Run the proxy on the same machine.
- `BOXLOFT_DEV=1` turns off the Secure cookie flag and listens on every interface. It's for trying the app on a home network.
