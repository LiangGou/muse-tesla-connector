# Security model

This connector gives an AI agent a live path to a physical car. The model below is stated exactly, including where it is weak. Read it before deploying, and before proposing any change to the command surface.

## What the broker can express — by construction, not by policy

Beyond reads, the broker's entire actuation vocabulary is two operations:

1. **Wake** — one unsigned cloud wake call (`wake_up`).
2. **Navigation send** — send one destination (free text, or GPS coordinates) to the car's navigation (`navigation_request` / `navigation_gps_request`).

There is no code path for locks, unlock, drive, climate, charging, trunk/frunk, horn, or any other Fleet API command. This is a property of the broker source, not a configuration flag and not an instruction the agent is trusted to obey: an agent (or anyone) driving the broker CLI cannot form another command, because the broker does not contain one. Extending the surface means deliberately editing `service/broker.py` — treat any such change as a security decision, not a feature.

## The navigation confirmation gate

A navigation send cannot happen in one step:

- `nav-prepare` performs no side effects. It records the exact destination (canonical text, or `lat,lon`), checks whether the same destination was sent within the last 10 minutes (duplicate guard), reads vehicle state, and returns a one-time confirmation token (128-bit random, 10-minute TTL) bound to that pending destination.
- The operating procedure (in `CONNECTOR.md`, which the agent loads) requires showing the owner the exact destination text and receiving the owner's confirmation in conversation before `nav-send` is called.
- `nav-send <token>` executes exactly once, only if the token matches the pending prepare and has not expired. It wakes the vehicle first if needed (disclosed at prepare time), and refuses an identical destination inside the duplicate window unless the owner explicitly overrides.
- Sends are executed through the local `tesla-http-proxy` so the command is signed with the local Fleet API key. If the proxy cannot be started, the send falls back to direct REST (navigation is a REST-class command — Tesla's own proxy passes it through unsigned) and the result reports `route: direct-rest`. The fallback degrades the route, never the confirmation gate, and is never silent.

Be honest about what this gate is: it binds *the agent's* action to an owner-confirmed destination. It protects against agent error, hallucinated or mis-geocoded destinations, and prompt-injection attempts that reach the agent, because in each case a human reads the destination before anything is sent. It does not protect against a human confirming a destination they did not read.

## Secrets

- OAuth tokens and the client secret exist only in `service/secrets/` (directory mode 700, files mode 600, written with fsync-before-use semantics). Token values are never printed by any broker verb.
- The refresh token is a rotating single-use chain: the broker serializes refreshes under a file lock, keeps the predecessor token only inside Tesla's 24-hour grace window for exactly one ambiguous-outcome retry, and otherwise stops and requires re-authorization rather than looping.
- The Fleet API signing key is generated locally (using Tesla's own tooling). Only the public half ever leaves the machine (hosted on your domain, as Tesla's partner registration requires). Back the private key up **only in encrypted form**, with the passphrase held in a password manager — never in the repo, never in chat, never next to the key.
- The setup wizard that receives the client secret binds to `127.0.0.1` only, serves one token-gated URL, stores once, and exits. The secret is never typed into an agent conversation.
- The signing proxy binds `127.0.0.1` only, with its own self-signed TLS certificate; the broker pins that certificate.

## Kill switches the owner holds (Tesla-side, independent of this code)

1. **Remove the virtual key in the car** (Settings → Locks → Keys): signed commands fail immediately. This is the fastest, most complete switch.
2. **Revoke the app's consent** in your Tesla account: the OAuth token chain dies; the broker stops with `reauthorize_needed`.
3. **Billing cap** in the Tesla developer portal: bounds financial exposure from API usage even if everything else fails.

Rehearse at least the first one before you need it: pair the key, send a test destination, remove the key, confirm sends fail, re-pair. In the original deployment this drill was still **unrehearsed** at the time this package was published — that gap is recorded here deliberately, as a reminder that an untested kill switch is a hope, not a control.

## Known limitations

- **A fully compromised host defeats local protections.** File permissions and loopback binding assume the machine itself is trustworthy. An attacker (or a malicious process) with your user account can read the secret store and the signing key. Mitigations belong to your OS hygiene, not this connector.
- **The agent is trusted within its surface.** The confirmation gate assumes the conversation's human confirmation is genuine. If an attacker can impersonate the owner to the agent, the gate adds friction, not proof.
- **Reads are cached when the car sleeps.** Tesla serves the last known data; the connector reports timestamps and GPS age, but a consumer of the data must still pay attention to them.
- **Tesla can change the platform.** Pricing, scopes, endpoints, and the virtual-key system are Tesla's; a platform change can break or alter any property above.

## Reporting

If you find a way for the broker to express a command outside the two verbs above, or to complete a navigation send without a valid confirmation token, that is a vulnerability in the core claim of this project — please open an issue describing it.
