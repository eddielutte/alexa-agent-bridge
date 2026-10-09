"""Official callback transport and a single configured agent webhook."""
import hashlib
import http.client
import json
import time
import urllib.parse

from retail import REGIONS, TransportError, json_body

# Skill Messaging regions (NA, EU, FE); a callback may only target one of these.
MESSAGING_HOSTS = {row["messaging_host"] for row in REGIONS.values()}
# The agent must acknowledge within this; the token exchange keeps the 2 s default (IKI-85).
WEBHOOK_TIMEOUT = 4.0
# Skill Messaging token reused within a warm container: {(sender, client, secret digest): (token, refresh_at)}.
_TOKENS = {}


class DispatchNotSent(TransportError):
    """Failure before starting the webhook POST; the agent was not contacted."""
    pass


def send(method, url, headers, data, allowed_hosts, timeout=2.0):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in allowed_hosts
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.fragment):
        raise ValueError("Unsupported HTTPS destination")
    connection = http.client.HTTPSConnection(parsed.hostname, timeout=timeout)
    try:
        connection.request(method, parsed.path + ("?" + parsed.query if parsed.query else ""),
                           body=data, headers=headers)
        response = connection.getresponse()
        body = response.read(65537)
        if len(body) > 65536:
            raise TransportError("Response too large")
        return response.status, body
    except (OSError, http.client.HTTPException):
        raise TransportError("Connection failed; no retry") from None
    finally:
        connection.close()


def messaging_token(config, sender=send, lifetime=None):
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials", "scope": "alexa:skill_messaging",
        "client_id": config["client_id"], "client_secret": config["client_secret"]}).encode()
    status, result = sender("POST", "https://api.amazon.com/auth/o2/token",
        {"Content-Type": "application/x-www-form-urlencoded"}, body, {"api.amazon.com"})
    if status != 200:
        raise TransportError("Skill credential exchange rejected")
    result = json_body(result)
    if not isinstance(result.get("access_token"), str) or not result["access_token"]:
        raise TransportError("No skill messaging token")
    if lifetime is not None:
        expires = result.get("expires_in")
        lifetime.append(expires if isinstance(expires, int) and expires > 0 else 0)
    return result["access_token"]


def reusable_token(config, sender=send, now=None):
    """Reuse a token until 5 minutes before expiry, so the agent's callback window (120 s) always fits."""
    now = now or time.time
    key = (sender, config["client_id"], hashlib.sha256(config["client_secret"].encode("utf-8")).hexdigest())
    token, refresh_at = _TOKENS.get(key, (None, 0))
    if token and now() < refresh_at:
        return token
    lifetime = []
    token = messaging_token(config, sender, lifetime)
    _TOKENS.clear()
    _TOKENS[key] = (token, now() + lifetime[0] - 300)
    return token


def api_host(endpoint):
    """Host of a request's context.System.apiEndpoint, or "" when it is not a known messaging host."""
    try:
        parsed = urllib.parse.urlsplit(endpoint) if isinstance(endpoint, str) else None
        if (not parsed or parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username
                or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            return ""
    except ValueError:
        return ""
    return parsed.hostname if parsed.hostname in MESSAGING_HOSTS else ""


def callback_url(user_id, host):
    if host not in MESSAGING_HOSTS:
        raise ValueError("Unsupported Skill Messaging region")
    return "https://" + host + "/v1/skillmessages/users/" + urllib.parse.quote(user_id, safe="")


def send_message(user_id, token, data, sender=send, host=None):
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                             for k, v in data.items()):
        raise ValueError("Skill Messaging data must contain string values")
    if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > 6000:
        raise ValueError("Message exceeds bounded callback size")
    url = callback_url(user_id, host)
    status, _ = sender("POST", url,
        {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        json.dumps({"data": data, "expiresAfterSeconds": 120},
                   ensure_ascii=False).encode("utf-8"), {host})
    if status != 202:
        raise TransportError("Skill Messaging rejected the request")
    return "queued"


def prepare_dispatch(config, user_id, device_key, job, message, sender):
    # Only a user-configured, separate routine may be targeted.
    url = config.get("webhook_url", "")
    parsed = urllib.parse.urlsplit(url)
    if (not isinstance(config.get("webhook_key"), str) or not config["webhook_key"]
            or any(c in config["webhook_key"] for c in "\r\n") or not parsed.hostname):
        raise DispatchNotSent("Agent routine is not configured")
    if (parsed.scheme != "https" or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.fragment):
        raise DispatchNotSent("Unsupported webhook destination")
    reply_url = callback_url(user_id, job.get("api_host"))
    token = reusable_token(config, sender)
    payload = {"request_id": job["id"], "message": message,
        "reply": {"url": reply_url, "bearer_token": token,
                  "format": "alexa_skill_messaging",
                  "data": {"kind": "answer", "device_key": device_key,
                           "request_id": job["id"], "job_token": job["token"]},
                  "expires_after_seconds": 120, "max_answer_characters": 4000,
                  "max_data_utf8_bytes": 6000}}
    return url, {"Authorization": "Bearer " + config["webhook_key"], "Content-Type": "application/json"}, json.dumps(payload, ensure_ascii=False).encode("utf-8"), {parsed.hostname}


def dispatch(config, user_id, device_key, job, message, sender=send):
    try:
        url, headers, body, hosts = prepare_dispatch(config, user_id, device_key, job, message, sender)
    except (TransportError, ValueError, KeyError, TypeError, UnicodeError):
        raise DispatchNotSent("Request preparation failed; webhook not contacted") from None
    status, _ = sender("POST", url, headers, body, hosts, WEBHOOK_TIMEOUT)
    if not 200 <= status < 300:
        raise TransportError("Agent dispatch was not confirmed")
