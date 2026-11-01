# Tesla (self-built Fleet API connector)

`custom.tesla` connects a Muse agent to the owner's own Tesla through the owner's own Tesla Fleet API developer application — not through Tessie or any third-party service. No third party ever holds the Tesla tokens: everything runs on the owner's machine.

## Layout and components

- `service/broker.py` — the only component that talks to Tesla. It owns all credentials and implements the OAuth refresh state machine. All configuration comes from `service/config.json` (copy `service/config.example.json`) or environment variables (`TESLA_CLIENT_ID`, `TESLA_VIN`, `TESLA_DOMAIN`, `TESLA_FLEET_BASE`, `TESLA_REDIRECT_URI`, `TESLA_SECRETS_DIR`). Nothing personal is hardcoded.
- `bin/tesla` — a thin wrapper over the broker CLI. This is the only interface an agent should use; never call Tesla endpoints directly.
- `service/run-proxy.sh` — starts Tesla's official `tesla-http-proxy` on `127.0.0.1:4443` with the local Fleet API signing key. The broker supervises it (starts it when needed for command sends). You must supply the `tesla-http-proxy` binary from Tesla's official vehicle-command repository (see README setup), via `TESLA_HTTP_PROXY_BIN` or your `PATH`.
- `service/setup_wizard.py` — a one-time, loopback-only, token-gated web page for the owner to paste the Tesla client secret into the local store once. The secret is never typed into chat and never logged.

## How authentication works here (deliberate design)

- Tesla's OAuth cannot be served by a generic static credential vault: Tesla's token endpoint accepts the client secret only as a POST form field (`client_secret_post`), and its refresh tokens are a rotating, single-use chain. The broker therefore self-manages authentication.
- Credentials live in `service/secrets/` (directory mode 700, files mode 600): the client secret (needed at setup and for partner operations) and `tokens.json` (access token, current refresh token, and the predecessor refresh token with its 24-hour grace deadline).
- The token endpoint is `https://auth.tesla.com/oauth2/v3/token` (per Tesla's published OIDC metadata). Do not post grants to the fleet-auth host; valid credentials are rejected there.
- Refresh rules: refreshes are serialized under a file lock; a new token store is fsynced to disk before the new access token is used; an ambiguous refresh outcome (for example a network failure mid-refresh) earns exactly one predecessor-token retry inside the grace window; a rejected chain stops with `reauthorize_needed` and recovery is a single owner re-authorization — never a silent retry loop.
- Scopes are exactly `openid offline_access vehicle_device_data vehicle_location vehicle_cmds`. Do not request broader scopes.
- Partner registration: Tesla requires a domain you control, with your Fleet API public key hosted at `https://<your-domain>/.well-known/appspecific/com.tesla.3p.public-key.pem`, registered with Tesla via the partner-accounts endpoint. The file the broker expects to compare against lives at `service/public-key-staging/com.tesla.3p.public-key.pem`.

## Command surface (`bin/tesla <verb>`)

Reads (no side effects):

- `tesla doctor` — health report: client secret present, public-key URL live, partner registration match, token store present, VIN pinned, local signing-proxy health. Run this first when anything seems wrong.
- `tesla status` — battery level, charge limit, charging state, cabin temperature, odometer, drive state and speed for the pinned vehicle.
- `tesla location` — latitude, longitude, heading, and GPS freshness. Location is live data: an asleep vehicle may return no location, and that absence must be reported as-is, never filled in from an old reading.
- `tesla vehicles` — the account's vehicle list.
- `tesla vehicle-data [endpoints]` — raw `vehicle_data` response (semicolon-separated endpoint list optional).
- `tesla fleet-status` — raw fleet status, including whether signed commands are required for the vehicle.
- `tesla key-status` — partner registration check plus Tesla-reported virtual-key pairing state (`key_paired_vins`).
- `tesla partner-check` — verifies Tesla's registered public key for your domain still matches your hosted key.

Token maintenance (normally automatic; reads refresh proactively):

- `tesla refresh [--force]` — run the refresh state machine once.
- `tesla exchange-code <code>` — recovery only: exchange a fresh authorization code from the owner's consent redirect. Codes expire within minutes. Never invent one; the owner produces it by signing in to Tesla in a browser.

Commands (the complete actuation surface — there is nothing else):

- `tesla wake` — one unsigned cloud wake call for the pinned vehicle. Never loop it.
- `tesla nav-prepare <destination text>` or `tesla nav-prepare --gps <lat> <lon>` — NO side effects. Returns the exact destination text, the vehicle state, whether confirming will wake the vehicle, a duplicate warning, and a one-time confirmation token (10-minute TTL).
- `tesla nav-send <token>` (also accepts `tesla nav-send --confirm <token>`) — executes one prepared send exactly once. If the vehicle was asleep it wakes it and waits up to 60 seconds, as disclosed at prepare time. The broker refuses a mismatched or expired token, and refuses an identical destination within 10 minutes (duplicate guard; `--override-duplicate` only on the owner's explicit say-so). Sends route through the local `tesla-http-proxy`; if the proxy cannot be started, navigation falls back to direct REST and the result reports `route: direct-rest` — it is never silent about the route.

Exit codes: `3` with `reauthorize_needed` means the token chain is dead — tell the owner the Tesla connection needs one re-authorization in the browser and walk through it with them; do not retry. `1` is a broker error; `2` is a usage error.

## Safety rules for the operating agent (non-negotiable)

1. The command surface is exactly wake + navigation. No lock, unlock, climate, charging, trunk, or drive verbs exist, and none may be improvised around this helper — not via the proxy, not via raw Fleet API calls.
2. Every navigation send is two-step: `nav-prepare`, show the owner the exact destination text the broker returns, and `nav-send` only after the owner confirms that exact text in the conversation, using the one-time token from that prepare. A prepared token is single-use and expires in 10 minutes; re-prepare instead of reusing.
3. Geocoding is fallible: a place name can resolve to a similarly named wrong address. Read the prepared destination back to the owner verbatim; if it looks wrong, stop and re-check rather than sending.
4. Never wake the vehicle autonomously. Wakes happen only inside an owner-confirmed nav-send (or one explicit `tesla wake` the owner asked for).
5. Fleet API usage is billed per request against the owner's developer account (a small monthly billing cap is part of setup). Do not poll the vehicle in a loop; batch questions into one `vehicle-data` call where possible.
6. Vehicle data comes from Tesla's servers; when the vehicle is asleep the response is cached and may be stale. When reporting a reading, say when it was taken (timestamps and GPS age are in the payloads) instead of presenting it as live.
7. Never print, paste, or log token values, the client secret, or the signing key. If a tool output contains one, treat it as compromised and tell the owner.

## Setup

Full setup (Tesla developer account, domain and public-key hosting, partner registration, virtual-key pairing in the car, billing cap, first authorization) is in `README.md`. The security model and its limits are in `SECURITY.md` — read it before proposing any change to the command surface.
