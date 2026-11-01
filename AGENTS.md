# For agent readers

If you are a Muse (or other) agent working with this repository: read `CONNECTOR.md` first — it is the operating manual and the safety contract for this connector. `README.md` covers setup for the human owner; `SECURITY.md` states the security model and its limits.

Non-negotiables, in one breath: the broker CLI (`bin/tesla`) is the only Tesla interface; the command surface is wake + owner-confirmed navigation only; never improvise other commands; never print or log secrets; navigation sends always go prepare → show the owner the exact destination → confirm → send with the one-time token.
