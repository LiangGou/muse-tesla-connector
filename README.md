# Muse Tesla Connector — Free

**Connect your Tesla to your Muse agent through Tesla's official Fleet API. No subscription, no third-party service holding your tokens — everything runs on your own machine, under a developer app you own.**

Third-party Tesla dashboards and agent integrations typically charge a monthly subscription *per car* for the privilege of holding your Tesla credentials on their servers. This project is the do-it-yourself alternative: a small local broker that speaks to Tesla's official Fleet API with your own developer application, exposes a deliberately tiny command surface to your Muse agent, and keeps every secret on your disk.

> Status: working reference implementation, built and used daily by its author with a Muse agent. Python 3.10+, no third-party Python dependencies (standard library only). The signing proxy is Tesla's official `tesla-http-proxy` from the [vehicle-command](https://github.com/teslamotors/vehicle-command) repository, which you build or download yourself — it is not vendored here.

## What it does

- **Reads**: battery and charging state, cabin temperature, odometer, drive state, vehicle location (with GPS freshness), raw vehicle data, fleet/key status, and a `doctor` health check.
- **Commands — exactly two**: wake the car, and send a navigation destination. Navigation is a two-step, owner-confirmed flow: the agent prepares the exact destination, shows it to you, and only sends after you confirm, using a one-time token.
- **Nothing else.** There are no lock, climate, charging, trunk, or drive commands — not hidden, not planned. The broker cannot express them (see [SECURITY.md](SECURITY.md)).

## Why it exists

On a drive, the author asked his Muse agent to order a coffee ahead and send the destination to his car. Muse could do neither: it had no access to the car. The available third-party connector worked through a hosted service that wanted a monthly fee per car and custody of the Tesla tokens. So instead, in one voice-driven session, Muse and its owner registered a Tesla developer app, set up the domain and signing key, and wrote this connector. Total new recurring cost: $0 (Tesla's Fleet API is pay-per-use with a small monthly discount that comfortably covers personal use; see Costs). The subscription quote is what this project replaces.

## Architecture

```mermaid
flowchart TB
    Owner(["Owner"])
    Agent(["Muse agent"])

    subgraph Local["Your machine (trust boundary)"]
        CLI["bin/tesla<br/>connector CLI"]
        Broker["service/broker.py<br/>OAuth token chain, refresh state machine,<br/>nav prepare/send guard"]
        Proxy["tesla-http-proxy<br/>127.0.0.1:4443, signs commands<br/>with the local Fleet API key"]
        Store[("service/secrets/<br/>client secret + token store, mode 600")]
        Keys[("service/keys/<br/>Fleet API signing key (private half never leaves)")]
    end

    TeslaAPI[["Tesla Fleet API<br/>auth.tesla.com + fleet-api"]]
    Car[["Your Tesla<br/>virtual key paired"]]

    Owner -->|"confirms exact destination<br/>in conversation"| Agent
    Agent -->|"CLI verbs: reads, wake,<br/>nav-prepare / nav-send"| CLI
    CLI --> Broker
    Broker -->|"reads + unsigned calls<br/>HTTPS + OAuth bearer"| TeslaAPI
    Broker -->|"command sends<br/>via local proxy"| Proxy
    Proxy -->|"signed command<br/>HTTPS"| TeslaAPI
    TeslaAPI --> Car
    Broker -.->|"reads/writes tokens"| Store
    Proxy -.->|"signs with"| Keys
```

**Read flow**: agent → `bin/tesla` → broker (refreshes the OAuth token if needed) → Fleet API → summarized JSON back to the agent.

**Navigation flow**: agent runs `nav-prepare` (no side effects; returns the exact destination, vehicle state, and a one-time confirmation token) → owner confirms that exact text in conversation → agent runs `nav-send <token>` → broker wakes the car if needed, then sends once through the local signing proxy → the vehicle acknowledges the result. A duplicate destination within 10 minutes is refused by a broker-side guard.

## Safety model (summary)

The full model, stated exactly, is in [SECURITY.md](SECURITY.md). The short version:

- **Minimal by construction**: the broker's command vocabulary is wake + navigation. Adding a verb means editing the broker — there is no generic "send any command" path for an agent to abuse.
- **Human confirmation for every send**: a fresh one-time token, bound to the exact destination text shown to the owner, valid for 10 minutes, single-use.
- **Secrets stay home**: OAuth tokens and the client secret live in a local mode-600 store. The Fleet API signing key is generated locally; only its public half is ever uploaded (to a domain you control, as Tesla requires).
- **You hold the kill switches**: remove the virtual key in the car, revoke the app's consent at Tesla, or rely on the billing cap in your Tesla developer portal — each independently severs or bounds the connection.

## Prerequisites

1. A Tesla account and a Tesla vehicle (Fleet API access is region-dependent; this implementation targets the North America fleet API host by default and the base URL is configurable).
2. A **Tesla developer account** with an application (developer.tesla.com). Free to create.
3. A **domain you control** that can serve one static file over HTTPS — Tesla's partner registration requires your Fleet API public key at `https://<domain>/.well-known/appspecific/com.tesla.3p.public-key.pem`. GitHub Pages works well for this.
4. **Billing on file** in the Tesla developer portal, with a small monthly cap you choose (Tesla bills Fleet API per request; see Costs).
5. Tesla's official `tesla-http-proxy` binary, built from the [vehicle-command](https://github.com/teslamotors/vehicle-command) repo (Go) or from its releases.
6. A Muse setup that supports custom connectors, to load `CONNECTOR.md` (optional — the CLI works standalone too).

## Setup

1. **Create the Tesla developer app.** At developer.tesla.com, create an application. Request exactly these scopes: `openid offline_access vehicle_device_data vehicle_location vehicle_cmds`. Note the client ID and client secret. Set a small monthly billing cap in the portal (the author's is $10).
2. **Generate the Fleet API key pair** using the `tesla-keygen` tool from the vehicle-command repo. Keep the private key at `service/keys/tesla-fleet-private-key.pem` (mode 600, gitignored). **Back it up only encrypted** (for example `openssl enc`) with a passphrase you store in a password manager — if you lose this key you must re-pair. Place the public key where the broker can compare it: `service/public-key-staging/com.tesla.3p.public-key.pem`.
3. **Host the public key.** Publish the public key file at `https://<your-domain>/.well-known/appspecific/com.tesla.3p.public-key.pem` and confirm the URL loads.
4. **Configure the connector.** Copy `service/config.example.json` to `service/config.json` and fill in your client ID, VIN, domain, redirect URI, and (if outside North America) fleet API base URL. Or use the `TESLA_*` environment variables listed in `service/broker.py`.
5. **Store the client secret.** Run `python3 service/setup_wizard.py`, open the printed loopback URL, and paste the client secret. It lands in `service/secrets/client-secret.txt` (mode 600) and the wizard exits. The secret is never typed into chat.
6. **Register your domain with Tesla (partner registration).** With the broker's partner token flow (`tesla partner-check` exercises the same credentials), POST your domain to Tesla's `partner_accounts` endpoint per Tesla's Fleet API docs, then confirm with `tesla partner-check` that the registered key matches your hosted key.
7. **Authorize the app (one browser sign-in).** Build the authorize URL for your app with the scopes above and your redirect URI, sign in as the vehicle owner, and approve. Exchange the returned code once: `tesla exchange-code <code>`. The broker now owns the refresh-token chain.
8. **Pair the virtual key in the car.** Tesla's flow sends you a link (`https://tesla.com/_ak/<your-domain>`) to open in the Tesla app / in the car, which pairs your public key as a virtual key for command signing. Verify with `tesla key-status` — Tesla reports your VIN under `key_paired_vins`.
9. **Build/place `tesla-http-proxy`** and set `TESLA_HTTP_PROXY_BIN` to its path (or add it to `PATH`). The broker starts it on demand via `service/run-proxy.sh`.
10. **Verify.** Run `tesla doctor` (all checks green), `tesla status`, `tesla location`. Then do one careful end-to-end navigation test while you are at the car: `tesla nav-prepare <a nearby address>`, read the destination back, confirm, `tesla nav-send <token>`, and watch the destination arrive on the car's screen.
11. **Rehearse the kill switch** (see SECURITY.md): once, remove the virtual key in the car, confirm commands fail, then re-pair. Do this *before* you rely on the connector, not after an incident.

If you use a Muse agent, point it at this repo's `CONNECTOR.md` (and `connector.yaml`) as a custom connector — that document contains the operating and safety rules the agent must follow.

## Usage

```console
$ tesla doctor          # health of secret, key hosting, registration, tokens, proxy
$ tesla status          # battery, charging, temperature, odometer, drive state
$ tesla location        # lat/lon, heading, GPS freshness
$ tesla nav-prepare "2001 Geary Blvd, San Francisco"
# -> { "destination": "...", "vehicle_state": "online", "confirm_token": "...", ... }
$ tesla nav-send <token>   # only after the owner confirms the exact destination
```

## Costs

Tesla Fleet API is pay-per-use (for example, vehicle data requests are billed per request, command/wake calls at a higher per-call rate) against your developer account, and Tesla provides a small monthly discount (historically $10) that ordinary personal use stays within. The billing cap from setup step 1 bounds the worst case. There is no other recurring cost: no subscription, no hosted service.

## Limitations

- One pinned vehicle per broker instance (the account's other vehicles are listable; pin a different VIN in config to switch).
- Asleep vehicles return cached data and may return no location; reads report freshness rather than pretending to be live.
- Navigation destinations are sent as text (or raw GPS coordinates); the car's own search/geocoding resolves them, and place names can resolve to a similarly named wrong address — which is exactly why the prepare step shows the exact text for confirmation.
- Fleet API behavior, pricing, and regional availability are Tesla's and can change; the connector targets the documented North America endpoints by default.
- A fully compromised host defeats local protections. This connector reduces third-party and agent risk; it does not make a compromised machine safe. See SECURITY.md.

## License

MIT — see [LICENSE](LICENSE).
