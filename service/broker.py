#!/usr/bin/env python3
"""Muse Tesla connector broker: the only component that talks to Tesla's Fleet API.

Owns all Tesla credentials (client secret, OAuth token chain) in a private
store under this directory. Implements the refresh state machine documented in SECURITY.md: serialized refreshes under a file lock, fsync-before-use store
writes, one predecessor-token retry inside Tesla's 24-hour grace window on
an ambiguous refresh outcome, and a stop-and-reauthorize state otherwise.

CLI verbs: exchange-code, refresh, partner-check, vehicles, vehicle-data,
status, location, fleet-status, key-status, doctor.
Token values are never printed; outputs are data summaries and JSON payloads.
"""
import fcntl
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SERVICE = Path(__file__).resolve().parent
SECRETS = Path(os.environ.get("TESLA_SECRETS_DIR", SERVICE / "secrets"))
TOKEN_STORE = SECRETS / "tokens.json"
LOCK_FILE = SECRETS / ".tokens.lock"
def _load_config() -> dict:
    """Configuration comes from config.json next to this file (copy
    config.example.json and fill it in), with environment overrides:
    TESLA_CLIENT_ID, TESLA_VIN, TESLA_DOMAIN, TESLA_FLEET_BASE,
    TESLA_REDIRECT_URI. No personal values are baked into this file.
    The secret store location can be overridden with TESLA_SECRETS_DIR.
    """
    cfg = {}
    cfg_path = Path(os.environ.get("TESLA_CONFIG", SERVICE / "config.json"))
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    env_map = {
        "client_id": "TESLA_CLIENT_ID",
        "vin": "TESLA_VIN",
        "domain": "TESLA_DOMAIN",
        "fleet_base": "TESLA_FLEET_BASE",
        "redirect_uri": "TESLA_REDIRECT_URI",
    }
    for key, env_name in env_map.items():
        if os.environ.get(env_name):
            cfg[key] = os.environ[env_name]
    return cfg


CONFIG = _load_config()

CLIENT_ID = CONFIG.get("client_id", "")
TOKEN_URL = "https://auth.tesla.com/oauth2/v3/token"  # per Tesla's published OIDC metadata
FLEET_BASE = CONFIG.get("fleet_base", "https://fleet-api.prd.na.vn.cloud.tesla.com")
DOMAIN = CONFIG.get("domain", "example.com")
SCOPES = "openid offline_access vehicle_device_data vehicle_location vehicle_cmds"
GRACE_SECONDS = 24 * 3600


class BrokerError(Exception):
    pass


class ReauthorizeNeeded(BrokerError):
    pass


def client_secret() -> str:
    return (SECRETS / "client-secret.txt").read_text(encoding="utf-8").strip()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_store() -> dict:
    if not TOKEN_STORE.exists():
        raise ReauthorizeNeeded("no token store yet; run the S6 authorization first")
    return json.loads(TOKEN_STORE.read_text(encoding="utf-8"))


def save_store(store: dict) -> None:
    SECRETS.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = TOKEN_STORE.with_suffix(".json.tmp")
    payload = json.dumps(store, indent=2).encode("utf-8")
    with open(tmp, "wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.chmod(0o600)
    os.replace(tmp, TOKEN_STORE)
    _fsync_dir(SECRETS)


class refresh_lock:
    """Process-wide serialization for refreshes (see SECURITY.md)."""

    def __enter__(self):
        SECRETS.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._fh = open(LOCK_FILE, "w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()


def _post_form(url: str, params: dict) -> tuple[int, dict]:
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        try:
            body = json.loads(err.read())
        except Exception:
            body = {}
        return err.code, body


def _apply_token_response(store: dict, body: dict) -> dict:
    now = int(time.time())
    old_refresh = store.get("refresh_token")
    new_refresh = body.get("refresh_token")
    if new_refresh and old_refresh and new_refresh != old_refresh:
        store["predecessor_refresh_token"] = old_refresh
        store["predecessor_valid_until"] = now + GRACE_SECONDS
    if new_refresh:
        store["refresh_token"] = new_refresh
    store["access_token"] = body["access_token"]
    store["access_expires_at"] = now + int(body.get("expires_in", 28800))
    store["refresh_issued_at"] = now
    return store


def exchange_code(code: str) -> dict:
    """S6: trade the owner's authorization code for the first token chain."""
    status, body = _post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "client_secret": client_secret(),
            "code": code,
            "redirect_uri": CONFIG["redirect_uri"],
            "audience": FLEET_BASE,
        },
    )
    if status != 200 or "access_token" not in body:
        raise BrokerError(f"code exchange failed: http {status} {body.get('error', '')}")
    store = _apply_token_response({}, body)
    save_store(store)
    return {"exchanged": True, "scope": body.get("scope", SCOPES)}


def _refresh_once(refresh_token: str, with_secret: bool) -> tuple[int, dict]:
    params = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": refresh_token,
    }
    if with_secret:
        params["client_secret"] = client_secret()
        params["audience"] = FLEET_BASE
    return _post_form(TOKEN_URL, params)


def refresh(force: bool = False) -> dict:
    """Refresh the access token following the Section 6 state machine.

    Tesla's own refresh example sends no client secret; that shape is tried
    first and the result recorded in the store (see SECURITY.md).
    On an ambiguous outcome (network-level failure), exactly one retry with
    the predecessor token is allowed inside the grace window.
    """
    with refresh_lock():
        store = load_store()
        if not force and store.get("access_expires_at", 0) > int(time.time()) + 300:
            return {"refreshed": False, "reason": "access token still valid"}
        ambiguous = False
        body = {}
        status = 0
        try:
            status, body = _refresh_once(store["refresh_token"], with_secret=False)
            shape = "client_id+refresh_token only"
            if status != 200:
                status, body = _refresh_once(store["refresh_token"], with_secret=True)
                shape = "with client_secret+audience"
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            ambiguous = True
        if ambiguous or status == 0:
            pred = store.get("predecessor_refresh_token")
            until = store.get("predecessor_valid_until", 0)
            if pred and until > int(time.time()):
                try:
                    status, body = _refresh_once(pred, with_secret=False)
                    if status != 200:
                        status, body = _refresh_once(pred, with_secret=True)
                    shape = "predecessor token (grace window)"
                except (urllib.error.URLError, TimeoutError, ConnectionError):
                    raise ReauthorizeNeeded(
                        "refresh outcome ambiguous and predecessor retry unreachable; "
                        "stop and re-authorize (S6)"
                    )
            else:
                raise ReauthorizeNeeded(
                    "refresh outcome ambiguous and no predecessor in grace window; "
                    "stop and re-authorize (S6)"
                )
        if status == 401 or (status == 400 and body.get("error") == "login_required"):
            raise ReauthorizeNeeded("refresh token rejected (login_required); re-authorize (S6)")
        if status != 200 or "access_token" not in body:
            raise BrokerError(f"refresh failed: http {status} {body.get('error', '')}")
        store = _apply_token_response(store, body)
        store["refresh_shape"] = shape
        save_store(store)
        return {"refreshed": True, "request_shape": shape}


def access_token() -> str:
    store = load_store()
    if store.get("access_expires_at", 0) <= int(time.time()) + 60:
        refresh()
        store = load_store()
    return store["access_token"]


def fleet_get(path: str, retry_on_401: bool = True) -> tuple[int, dict]:
    req = urllib.request.Request(
        FLEET_BASE + path, headers={"Authorization": f"Bearer {access_token()}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        if err.code == 401 and retry_on_401:
            refresh(force=True)
            return fleet_get(path, retry_on_401=False)
        try:
            body = json.loads(err.read())
        except Exception:
            body = {}
        return err.code, body


def call_log(verb: str, outcome: str, detail: str = "") -> None:
    try:
        logs = SERVICE / "logs"
        logs.mkdir(exist_ok=True)
        entry = {"ts": int(time.time()), "verb": verb, "outcome": outcome, "detail": detail[:120]}
        with open(logs / "calls.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def fleet_post(path: str, payload: dict, retry_on_401: bool = True) -> tuple[int, dict]:
    req = urllib.request.Request(
        FLEET_BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access_token()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        if err.code == 401 and retry_on_401:
            refresh(force=True)
            return fleet_post(path, payload, retry_on_401=False)
        try:
            body = json.loads(err.read())
        except Exception:
            body = {}
        return err.code, body


def partner_token() -> str:
    status, body = _post_form(
        TOKEN_URL,
        {
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": client_secret(),
            "audience": FLEET_BASE,
            "scope": "openid vehicle_device_data vehicle_location vehicle_cmds",
        },
    )
    if status != 200:
        raise BrokerError(f"partner token failed: http {status} {body.get('error', '')}")
    return body["access_token"]


def pinned_vin() -> str:
    vin = CONFIG.get("vin")
    if not vin:
        raise BrokerError("no VIN pinned in config yet (set \"vin\" in config.json or TESLA_VIN)")
    return vin


def cmd_vehicles() -> dict:
    status, body = fleet_get("/api/1/vehicles")
    if status != 200:
        raise BrokerError(f"vehicles failed: http {status} {body.get('error', '')}")
    out = []
    for v in body.get("response", []):
        out.append(
            {
                "vin": v.get("vin"),
                "display_name": v.get("display_name"),
                "state": v.get("state"),
                "in_service": v.get("in_service"),
                "charge_port": None,
            }
        )
    return {"count": body.get("count", len(out)), "vehicles": out}


def cmd_vehicle_data(endpoints: str | None = None) -> dict:
    vin = pinned_vin()
    path = f"/api/1/vehicles/{vin}/vehicle_data"
    if endpoints:
        path += "?endpoints=" + urllib.parse.quote(endpoints)
    status, body = fleet_get(path)
    if status != 200:
        raise BrokerError(f"vehicle_data failed: http {status} {json.dumps(body)[:200]}")
    return body.get("response", {})


def cmd_status() -> dict:
    data = cmd_vehicle_data("charge_state;climate_state;vehicle_state;drive_state")
    charge = data.get("charge_state", {})
    climate = data.get("climate_state", {})
    vstate = data.get("vehicle_state", {})
    drive = data.get("drive_state", {})
    return {
        "vin_suffix": (data.get("vin") or "")[-6:],
        "state": data.get("state"),
        "battery_level_pct": charge.get("battery_level"),
        "charge_limit_pct": charge.get("charge_limit_soc"),
        "charging_state": charge.get("charging_state"),
        "charge_rate_mph": charge.get("charge_rate"),
        "minutes_to_full": charge.get("minutes_to_full_charge"),
        "inside_temp_c": climate.get("inside_temp"),
        "climate_on": climate.get("is_climate_on"),
        "odometer_mi": vstate.get("odometer"),
        "car_version": vstate.get("car_version"),
        "shift_state": drive.get("shift_state"),
        "speed_mph": drive.get("speed"),
        "timestamp_ms": data.get("timestamp"),
    }


def cmd_location() -> dict:
    data = cmd_vehicle_data("location_data;drive_state")
    loc = data.get("location_data", {})
    drive = data.get("drive_state", {})
    return {
        "vin_suffix": (data.get("vin") or "")[-6:],
        "state": data.get("state"),
        "latitude": loc.get("latitude", drive.get("latitude")),
        "longitude": loc.get("longitude", drive.get("longitude")),
        "heading": drive.get("heading"),
        "gps_as_of_s": drive.get("gps_as_of"),
        "native_type": drive.get("native_location_supported"),
        "timestamp_ms": data.get("timestamp"),
    }


def cmd_fleet_status() -> dict:
    vin = pinned_vin()
    status, body = fleet_post("/api/1/vehicles/fleet_status", {"vins": [vin]})
    if status != 200:
        raise BrokerError(f"fleet_status failed: http {status} {body.get('error', '')}")
    resp = body.get("response", {})
    return resp.get(vin, resp)


NAV_GUARD = SECRETS / "nav-guard.json"
DUP_WINDOW_SECONDS = 600
CONFIRM_TTL_SECONDS = 600


def _load_guard() -> dict:
    if NAV_GUARD.exists():
        return json.loads(NAV_GUARD.read_text(encoding="utf-8"))
    return {}


def _save_guard(guard: dict) -> None:
    SECRETS.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = NAV_GUARD.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(guard))
        fh.flush()
        os.fsync(fh.fileno())
    tmp.chmod(0o600)
    os.replace(tmp, NAV_GUARD)
    _fsync_dir(SECRETS)


def vehicle_state() -> str:
    status, body = fleet_get("/api/1/vehicles")
    if status != 200:
        raise BrokerError(f"vehicles failed: http {status}")
    vin = pinned_vin()
    for v in body.get("response", []):
        if v.get("vin") == vin:
            return v.get("state", "unknown")
    raise BrokerError("pinned VIN not in vehicle list")


def cmd_wake() -> dict:
    """Unsigned cloud wake (see SECURITY.md). One call, no silent retry."""
    vin = pinned_vin()
    status, body = fleet_post(f"/api/1/vehicles/{vin}/wake_up", {})
    call_log("wake", f"http {status}")
    if status != 200:
        raise BrokerError(f"wake failed: http {status} {json.dumps(body)[:160]}")
    resp = body.get("response", {})
    return {"result": resp.get("result"), "state": resp.get("state")}


def wait_online(max_seconds: int = 60) -> dict:
    """Bounded wait for the vehicle to come online; each poll is a billed call."""
    deadline = time.time() + max_seconds
    polls = 0
    state = vehicle_state()
    polls += 1
    while state != "online" and time.time() < deadline:
        time.sleep(10)
        state = vehicle_state()
        polls += 1
    call_log("wait_online", state, f"polls={polls}")
    return {"state": state, "polls": polls, "waited_s": max_seconds if state != "online" else None}


def _nav_payload(kind: str, text: str | None, lat: float | None, lon: float | None) -> dict:
    if kind == "gps":
        return {"latitude": lat, "longitude": lon, "order": 1}
    return {
        "type": "share_ext_content_raw",
        "locale": "en-US",
        "timestamp_ms": str(int(time.time() * 1000)),
        "value": {"android.intent.extra.TEXT": text},
    }


def cmd_nav_prepare(kind: str, text: str | None = None, lat: float | None = None, lon: float | None = None) -> dict:
    import secrets as _secrets

    canonical = text.strip() if kind == "text" else f"{lat},{lon}"
    guard = _load_guard()
    last = guard.get("last_send", {})
    duplicate = (
        last.get("canonical") == canonical
        and int(time.time()) - int(last.get("ts", 0)) < DUP_WINDOW_SECONDS
    )
    state = vehicle_state()
    token = _secrets.token_hex(8)
    guard["pending"] = {
        "token": token,
        "kind": kind,
        "canonical": canonical,
        "text": text,
        "lat": lat,
        "lon": lon,
        "expires": int(time.time()) + CONFIRM_TTL_SECONDS,
    }
    _save_guard(guard)
    call_log("nav_prepare", "ok", canonical)
    return {
        "destination": text if kind == "text" else canonical,
        "kind": kind,
        "vehicle_state": state,
        "needs_wake": state != "online",
        "wake_note": (
            "Vehicle is not online; confirming the send will wake it (one wake call, $0.02 list) "
            "and wait up to 60s for it to come online."
            if state != "online"
            else "Vehicle is online; no wake needed."
        ),
        "duplicate_within_10min": duplicate,
        "confirm_token": token,
        "confirm_within_seconds": CONFIRM_TTL_SECONDS,
    }


def cmd_nav_send(token: str, override_duplicate: bool = False) -> dict:
    guard = _load_guard()
    pending = guard.get("pending") or {}
    if not pending or pending.get("token") != token:
        raise BrokerError("confirmation token does not match a pending prepared send")
    if int(time.time()) > int(pending.get("expires", 0)):
        raise BrokerError("confirmation token expired; prepare again")
    canonical = pending["canonical"]
    last = guard.get("last_send", {})
    if (
        last.get("canonical") == canonical
        and int(time.time()) - int(last.get("ts", 0)) < DUP_WINDOW_SECONDS
        and not override_duplicate
    ):
        raise BrokerError("duplicate destination within 10 minutes; refused (design guard)")
    vin = pinned_vin()
    state = vehicle_state()
    woke = False
    if state != "online":
        cmd_wake()
        woke = True
        outcome = wait_online(60)
        if outcome["state"] != "online":
            call_log("nav_send", "vehicle_stayed_offline", canonical)
            raise BrokerError(f"vehicle did not come online within 60s (state {outcome['state']}); send not attempted")
    kind = pending["kind"]
    endpoint = "navigation_gps_request" if kind == "gps" else "navigation_request"
    payload = _nav_payload(kind, pending.get("text"), pending.get("lat"), pending.get("lon"))
    path = f"/api/1/vehicles/{vin}/command/{endpoint}"
    route = "direct-rest"
    if os.environ.get("TESLA_BROKER_TEST_TRANSPORT"):
        status, body = fleet_post(path, payload)
    elif proxy_ensure():
        status, body = proxy_post(path, payload)
        route = "via-local-proxy"
    else:
        # Declared fallback: navigation is a REST-class command (the proxy
        # itself passes it through unsigned), so a dead proxy degrades the
        # route, not the safety. The response reports which route was used.
        status, body = fleet_post(path, payload)
    resp = body.get("response", {}) if isinstance(body, dict) else {}
    result = resp.get("result")
    call_log("nav_send", f"http {status} result={result}", canonical)
    guard["last_send"] = {"canonical": canonical, "ts": int(time.time())}
    guard["pending"] = None
    _save_guard(guard)
    if status != 200 or result is not True:
        raise BrokerError(f"nav send not confirmed by vehicle: http {status} {json.dumps(body)[:200]}")
    return {
        "sent": True,
        "destination": pending.get("text") if kind == "text" else canonical,
        "kind": kind,
        "route": route,
        "woke_vehicle": woke,
        "vehicle_result": result,
        "reason": resp.get("reason", ""),
    }


PROXY_BASE = "https://127.0.0.1:4443"
PROXY_CERT = SERVICE / "proxy" / "tls-cert.pem"
PROXY_SCRIPT = SERVICE / "run-proxy.sh"


def proxy_health() -> bool:
    try:
        ctx = ssl.create_default_context(cafile=str(PROXY_CERT))
        req = urllib.request.Request(PROXY_BASE + "/health", method="GET")
        with urllib.request.urlopen(req, timeout=2, context=ctx) as resp:
            return resp.status == 200
    except Exception:
        return False


def proxy_ensure() -> bool:
    """Broker supervision duty: start the proxy if it is down."""
    if proxy_health():
        return True
    try:
        subprocess.Popen(
            [str(PROXY_SCRIPT)],
            stdout=open(SERVICE / "proxy" / "proxy.log", "ab"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception:
        return False
    for _ in range(10):
        time.sleep(0.5)
        if proxy_health():
            return True
    return False


def proxy_post(path: str, payload: dict) -> tuple[int, dict]:
    """POST a command through the local signing proxy with the pinned cert.

    Signed-class commands are signed by the proxy; REST-class commands (like
    navigation_request) are passed through to Fleet API by the proxy itself.
    """
    token = access_token()
    ctx = ssl.create_default_context(cafile=str(PROXY_CERT))
    req = urllib.request.Request(
        PROXY_BASE + path,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:200]}


def cmd_partner_check() -> dict:
    tok = partner_token()
    q = urllib.parse.urlencode({"domain": DOMAIN})
    req = urllib.request.Request(
        FLEET_BASE + "/api/1/partner_accounts/public_key?" + q,
        headers={"Authorization": f"Bearer {tok}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read())
    returned = (body.get("response") or {}).get("public_key", "")
    staged = _staged_point_hex()
    return {"domain": DOMAIN, "registered_key_matches": returned.lower() == staged.lower()}


def _staged_point_hex() -> str:
    import subprocess

    der = subprocess.run(
        ["openssl", "ec", "-pubin", "-in", str(SERVICE / "public-key-staging/com.tesla.3p.public-key.pem"), "-outform", "DER"],
        capture_output=True, check=True,
    ).stdout
    return der[-65:].hex()


def cmd_key_status() -> dict:
    out = {"partner_registration": cmd_partner_check()}
    try:
        fs = cmd_fleet_status()
        vin = pinned_vin()
        info = (fs.get("vehicle_info") or {}).get(vin, {})
        out["fleet_status"] = {
            "key_paired": vin in (fs.get("key_paired_vins") or []),
            "vehicle_command_protocol_required": info.get("vehicle_command_protocol_required"),
            "discounted_device_data": info.get("discounted_device_data"),
            "total_number_of_keys": info.get("total_number_of_keys"),
        }
    except BrokerError as err:
        out["fleet_status_error"] = str(err)
    out["pairing_note"] = (
        "Pairing state above comes from Tesla's fleet_status (key_paired_vins). "
        "A signed command actually executing is the end-to-end proof."
    )
    return out


def cmd_doctor() -> dict:
    checks = {}
    checks["client_secret_present"] = (SECRETS / "client-secret.txt").exists()
    try:
        import urllib.request as u

        with u.urlopen(f"https://{DOMAIN}/.well-known/appspecific/com.tesla.3p.public-key.pem", timeout=15) as r:
            checks["public_key_url_live"] = r.status == 200
    except Exception:
        checks["public_key_url_live"] = False
    try:
        checks["partner_registration"] = cmd_partner_check()
    except Exception as err:
        checks["partner_registration"] = {"error": str(err)}
    try:
        store = load_store()
        checks["token_store"] = {
            "present": True,
            "access_expires_at": store.get("access_expires_at"),
            "refresh_shape_last": store.get("refresh_shape"),
        }
    except ReauthorizeNeeded:
        checks["token_store"] = {"present": False}
    checks["vin_pinned"] = bool(CONFIG.get("vin"))
    checks["proxy_health"] = proxy_health()
    return checks


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    verb = argv[1]
    try:
        if verb == "exchange-code":
            result = exchange_code(argv[2])
        elif verb == "refresh":
            result = refresh(force="--force" in argv)
        elif verb == "wake":
            result = cmd_wake()
        elif verb == "nav-prepare":
            # nav-prepare <destination text>  |  nav-prepare --gps <lat> <lon>
            if len(argv) > 2 and argv[2] == "--gps":
                result = cmd_nav_prepare("gps", lat=float(argv[3]), lon=float(argv[4]))
            else:
                result = cmd_nav_prepare("text", text=" ".join(argv[2:]))
        elif verb == "nav-send":
            # Accept both forms: nav-send <token> and nav-send --confirm <token>
            _args = [a for a in argv[2:] if a != "--override-duplicate"]
            if _args and _args[0] == "--confirm":
                _args = _args[1:]
            result = cmd_nav_send(_args[0], override_duplicate="--override-duplicate" in argv)
        elif verb == "partner-check":
            result = cmd_partner_check()
        elif verb == "vehicles":
            result = cmd_vehicles()
        elif verb == "vehicle-data":
            result = cmd_vehicle_data(argv[2] if len(argv) > 2 else None)
        elif verb == "status":
            result = cmd_status()
        elif verb == "location":
            result = cmd_location()
        elif verb == "fleet-status":
            result = cmd_fleet_status()
        elif verb == "key-status":
            result = cmd_key_status()
        elif verb == "doctor":
            result = cmd_doctor()
        else:
            print(f"unknown verb: {verb}", file=sys.stderr)
            return 2
    except ReauthorizeNeeded as err:
        print(json.dumps({"error": "reauthorize_needed", "detail": str(err)}))
        return 3
    except BrokerError as err:
        print(json.dumps({"error": "broker_error", "detail": str(err)}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
