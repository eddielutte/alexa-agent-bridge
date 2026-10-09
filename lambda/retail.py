"""Small Python 3.8-compatible native speech adapter; no AudioPlayer.
Protocol reference: aioamazondevices 16.3.1 (Apache-2.0); see NOTICE.
Only refresh, identity/device discovery and one targeted speech POST are supported.
"""
import copy
import html
import http.client
import http.cookies
import json
import os
import re
import time
import urllib.parse

MAX_TEXT = 4000
# Refresh cookies roughly hourly; long-idle behaviour is still unverified.
# Cookie lifetime is independent of the unused retail access token.
COOKIE_REFRESH_SECONDS = 3540
DEVICE_REFRESH_SECONDS = 300


def _load(name):
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name), encoding="utf-8-sig") as handle:
        return json.load(handle)


# Country data, not code: domains, locales and regions come from countries.json.
COUNTRY_TABLE = _load("countries.json")
COUNTRIES, REGIONS = COUNTRY_TABLE["countries"], COUNTRY_TABLE["regions"]
DOMAINS = {row["domain"] for row in COUNTRIES.values()}
ALLOWED_HOSTS = {"api.amazon.com"} | {"alexa." + domain for domain in DOMAINS}
UA = "AmazonWebView/AmazonAlexa/2.2.663733.0/iOS/18.5/iPhone"


class AuthRequired(Exception):
    pass


class RegistrationRejected(AuthRequired):
    pass


class IdentityMismatch(AuthRequired):
    pass


class TransportError(Exception):
    pass


class RenewalIncomplete(TransportError):
    pass


class UnconfirmedSpeech(Exception):
    pass


class TargetUnavailable(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__("Target is unavailable")


def request(method, url, headers=None, data=None, timeout=2.0):
    """One HTTPS attempt; no redirects, retries, environment proxy, or logging."""
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.fragment):
        raise ValueError("Unsupported endpoint")
    connection = http.client.HTTPSConnection(parsed.hostname, timeout=timeout)
    try:
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        connection.request(method, path, body=data, headers=headers or {})
        response = connection.getresponse()
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise TransportError("Response too large")
        return response.status, response.getheaders(), body
    except (OSError, http.client.HTTPException):
        raise TransportError("Amazon connection failed") from None
    finally:
        connection.close()


def messaging_host(country):
    return REGIONS[COUNTRIES[country]["region"]]["messaging_host"]


def locale_for(session):
    locale = session.get("locale")
    if isinstance(locale, str) and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", locale):
        return locale
    return next(row["locales"][0] for row in COUNTRIES.values() if row["domain"] == session["domain"])


def json_body(body):
    try:
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError, UnicodeError):
        raise TransportError("Unexpected Amazon response") from None


def collect_cookies(headers, cookies, domain):
    # Only called for the session's own allowlisted retail origin.
    result = dict(cookies)
    for name, value in headers:
        if name.lower() == "set-cookie":
            parsed = http.cookies.SimpleCookie()
            parsed.load(value)
            for key, morsel in parsed.items():
                cookie_domain = morsel["domain"].lstrip(".")
                if cookie_domain and cookie_domain not in (domain, "alexa." + domain):
                    continue
                if morsel["path"] and not "/api/".startswith(morsel["path"]):
                    continue
                if morsel["max-age"] == "0":
                    result.pop(key, None)
                else:
                    result[key] = morsel.value
    return result


def headers_for(session):
    cookies = session.get("cookies", {})
    if not isinstance(cookies, dict):
        raise AuthRequired("Missing cookies")
    if any(not isinstance(k, str) or not isinstance(v, str) or
           any(c in k + v for c in "\r\n;") for k, v in cookies.items()):
        raise AuthRequired("Invalid cookies")
    headers = {"User-Agent": UA, "Accept": "application/json",
               "Accept-Language": locale_for(session),
               "Cookie": "; ".join(k + "=" + v for k, v in cookies.items())}
    if cookies.get("csrf"):
        headers["csrf"] = cookies["csrf"]
    return headers


def retail_get(session, path, transport=request):
    status, headers, body = transport("GET", "https://alexa." + session["domain"] + path,
                                     headers_for(session))
    if status in (301, 302, 303, 307, 308, 401, 403):
        raise AuthRequired("Amazon sign-in needs renewal")
    if status != 200:
        raise TransportError("Amazon read was not accepted")
    session["cookies"] = collect_cookies(headers, session.get("cookies", {}), session["domain"])
    return json_body(body)


def validate_session(session):
    if (session.get("schema") != 1 or session.get("domain") not in DOMAINS
            or not isinstance(session.get("refresh_token"), str)
            or not session["refresh_token"] or
            not isinstance(session.get("customer_id"), str)
            or not session["customer_id"]):
        raise RegistrationRejected("Invalid registration")


def token_request(session, token_type, transport):
    form = {"app_name": "AioAmazonDevices", "app_version": "2.2.663733.0",
            "di.sdk.version": "6.12.4", "source_token": session["refresh_token"],
            "package_name": "com.amazon.echo", "di.hw.version": "iPhone",
            "platform": "iOS", "requested_token_type": token_type,
            "source_token_type": "refresh_token", "di.os.name": "iOS",
            "di.os.version": "18.5", "current_version": "6.12.4",
            "previous_version": "6.12.4", "domain": "www." + session["domain"]}
    status, _, body = transport(
        "POST", "https://api.amazon.com/auth/token",
        {"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"},
        urllib.parse.urlencode(form).encode())
    if status != 200:
        if status in (400, 401, 403):
            # Retain saved registration; classification does not erase it.
            raise RegistrationRejected("Registration needs interactive attention")
        raise TransportError("Amazon renewal temporarily failed")
    return json_body(body)


def renew(session, transport=request, now=None):
    """Return a new verified state. Never partially mutate the durable original."""
    validate_session(session)
    now = int(time.time() if now is None else now)
    candidate = copy.deepcopy(session)
    # Speech authenticates with cookies/CSRF, never this access token.
    candidate.pop("access_token", None)
    candidate.pop("access_expires", None)
    cookie_reply = token_request(candidate, "auth_cookies", transport)
    domains = cookie_reply.get("response", {}).get("tokens", {}).get("cookies", {})
    cookies = {}
    for domain, rows in domains.items():
        if domain.lstrip(".") not in (candidate["domain"], "alexa." + candidate["domain"]):
            continue
        for row in rows:
            name, value = row.get("Name"), row.get("Value")
            if isinstance(name, str) and isinstance(value, str):
                cookies[name] = value.strip('"')
    if not cookies:
        raise TransportError("Missing account cookies")
    candidate["cookies"] = cookies
    identity = retail_get(candidate, "/api/users/me", transport)
    if not identity.get("id"):
        raise RenewalIncomplete("No account identity returned")
    if identity.get("id") != candidate["customer_id"]:
        raise IdentityMismatch("Amazon account mismatch")
    # Discovery normally also issues a fresh CSRF cookie.
    candidate["devices"] = eligible_devices(
        retail_get(candidate, "/api/devices-v2/device?cached=false", transport))
    if not candidate["cookies"].get("csrf"):
        raise RenewalIncomplete("No fresh CSRF cookie")
    candidate["cookies_refreshed_at"] = now
    candidate["devices_refreshed_at"] = now
    candidate["auth_state"] = "ready"
    return candidate


def refresh_devices(session, transport=request, now=None):
    """Discover independently of cookie renewal; failed reads preserve saved state."""
    validate_session(session)
    candidate = copy.deepcopy(session)
    candidate["devices"] = eligible_devices(
        retail_get(candidate, "/api/devices-v2/device?cached=false", transport))
    candidate["devices_refreshed_at"] = int(time.time() if now is None else now)
    return candidate


def eligible_devices(reply):
    devices = []
    for row in reply.get("devices", []):
        serial = row.get("serialNumber")
        cluster = row.get("clusterMembers") or []
        if (not serial or row.get("deviceFamily") == "WHA"
                or (set(cluster) - {serial})
                or "AUDIO_PLAYER" not in row.get("capabilities", [])):
            continue
        devices.append({"serial": serial, "type": row.get("deviceType"),
                        "name": row.get("accountName"), "online": bool(row.get("online"))})
    return devices


def needs_renewal(session, now=None):
    now = int(time.time() if now is None else now)
    return (not session.get("cookies", {}).get("csrf")
            or session.get("auth_state") in ("stale", "sign_in_required")
            or int(session.get("cookies_refreshed_at", 0)) <= now - COOKIE_REFRESH_SECONDS)


def needs_devices(session, target, now):
    matches = [d for d in session.get("devices", [])
               if d.get("serial") == target.get("serial")
               and d.get("type") == target.get("type") and d.get("online")]
    return (len(matches) != 1 or
            session.get("devices_refreshed_at", 0) <= now - DEVICE_REFRESH_SECONDS)


def speech_payload(session, target, text):
    validate_session(session)
    if (not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT
            or any(ord(c) < 32 and c not in "\n\r\t" for c in text)):
        raise ValueError("Invalid speech text")
    matches = [d for d in session.get("devices", [])
               if d.get("serial") == target.get("serial")
               and d.get("type") == target.get("type")]
    if len(matches) != 1:
        raise TargetUnavailable("missing")
    if not matches[0].get("online"):
        raise TargetUnavailable("offline")
    customer = session["customer_id"]
    operation = {"deviceType": target["type"], "deviceSerialNumber": target["serial"],
                 "locale": locale_for(session), "customerId": customer,
                 "textToSpeak": html.escape(text, quote=False),
                 "target": {"customerId": customer, "devices": [
                     {"deviceSerialNumber": target["serial"], "deviceTypeId": target["type"]}]},
                 "skillId": "amzn1.ask.1p.saysomething"}
    sequence = {"@type": "com.amazon.alexa.behaviors.model.Sequence",
                "startNode": {"@type": "com.amazon.alexa.behaviors.model.SerialNode",
                              "nodesToExecute": [{
                                  "@type": "com.amazon.alexa.behaviors.model.OpaquePayloadOperationNode",
                                  "type": "Alexa.Speak", "operationPayload": operation}]}}
    return {"behaviorId": "PREVIEW", "status": "ENABLED",
            "sequenceJson": json.dumps(sequence, separators=(",", ":"))}


def speak_once(session, target, text, transport=request):
    payload = speech_payload(session, target, text)
    headers = headers_for(session)
    if not headers.get("csrf"):
        raise AuthRequired("Missing CSRF before speech")
    headers["Content-Type"] = "application/json; charset=utf-8"
    try:
        status, _, _ = transport(
            "POST", "https://alexa." + session["domain"] + "/api/behaviors/preview", headers,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except TransportError:
        raise UnconfirmedSpeech("Speech outcome unknown; no retry") from None
    if status in (401, 403):
        raise AuthRequired("Speech rejected; do not replay this job")
    if status != 200:
        raise UnconfirmedSpeech("Speech not confirmed; no retry")
    return "accepted_by_amazon"
