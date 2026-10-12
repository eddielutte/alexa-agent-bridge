"""Private Alexa-hosted bridge skill. No local relay and no AudioPlayer."""
from datetime import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import time

import messaging
import retail
from storage import Store, Sessions, Conflict, Busy, metadata_failure

ACK = {"version": "1.0", "response": {}}
PROOF_LIMIT = 20
HELD_QUESTION_LIMIT = 1000
ANNOUNCE_LIMIT = 20  # bounded announcement receipts
ECHO_LIMIT = 20  # names in one announcement or in the owner's restriction


def load_json(name):
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name), encoding="utf-8-sig") as handle:
        return json.load(handle)


def load_config(config):
    """Deployment identity is data (bridge_config.json); refuse to run with an invalid one."""
    if (not isinstance(config, dict) or config.get("schema") != 1
            or not re.fullmatch(r"amzn1\.ask\.skill\.[0-9a-f-]{36}", str(config.get("skill_id", "")))
            or not isinstance(config.get("display_name"), str) or not config["display_name"].strip()
            or config.get("country") not in retail.COUNTRIES
            or config.get("locale") not in retail.COUNTRIES[config["country"]]["locales"]
            or not re.fullmatch(r"[a-z]{2}", str(config.get("language", "")))
            or not isinstance(config.get("test_device_name"), str)
            or not isinstance(config.get("agent_name", ""), str)):
        raise ValueError("bridge_config.json is invalid")
    return config


CONFIG = load_config(load_json("bridge_config.json"))
SKILL_ID = CONFIG["skill_id"]
PACK = load_json("lang_" + CONFIG["language"] + ".json")
DEFAULT_API_HOST = retail.messaging_host(CONFIG["country"])


def say(key, **values):
    return PACK["strings"][key].format(name=CONFIG["display_name"],
                                       agent=CONFIG.get("agent_name") or PACK["strings"]["agent_default"], **values)


class DuplicateRequest(Conflict):
    pass


def event_time(req):
    parsed = datetime.fromisoformat(req["timestamp"].replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Missing timezone")
    return parsed.timestamp()


def auth_guidance(state):
    auth = (state or {}).get("auth_state")
    if auth == "account_mismatch":
        return say("auth_mismatch")
    if auth == "sign_in_required":
        return say("auth_sign_in")
    if auth == "stale":
        return say("auth_stale")
    return ""


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def response(text, end=True, card=None):
    body = {"outputSpeech": {"type": "PlainText", "text": text},
            "shouldEndSession": end}
    if not end:
        body["reprompt"] = {"outputSpeech": {"type": "PlainText", "text": text}}
    if card:
        body["card"] = {"type": "Simple", "title": say("card_title"),
                        "content": json.dumps(card)}
    return {"version": "1.0", "response": body}


def normal_name(value):
    return re.sub(r" echo$", "", " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower())))


def test_device_name(payload):
    name = payload.get("test_device_name")
    return name if isinstance(name, str) and 0 < len(name) <= 100 else CONFIG["test_device_name"]


def key_hash(value):
    return value.lower() if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) else None


def echo_names(value):
    """A list of 1–20 short Echo names, or None."""
    if (not isinstance(value, list) or not 0 < len(value) <= ECHO_LIMIT
            or not all(isinstance(n, str) and normal_name(n) and len(n) <= 100 for n in value)):
        return None
    return value


def spoken_request(intent):
    value = intent.get("slots", {}).get("request", {}).get("value", "").strip()
    if not value:
        return None
    # Model supplies carrier prefixes; restore words removed from the query slot.
    prefix = PACK["carriers"].get(intent.get("name"))
    return None if prefix is None else ("" if intent["name"] == "PleaseIntent" else prefix) + value


class App:
    def __init__(self, store, sessions=None, clock=time.time, dispatch=messaging.dispatch,
                 speaker=retail.speak_once, deregisterer=retail.deregister, announcer=retail.announce_once,
                 dnd=retail.dnd_states, allowed=retail.announcements_allowed, volumes=retail.device_volumes):
        self.store, self.clock = store, clock
        self.sessions = sessions or Sessions(store, clock)
        self.dispatch, self.speaker, self.deregisterer = dispatch, speaker, deregisterer
        self.announcer, self.dnd, self.allowed, self.volumes = announcer, dnd, allowed, volumes

    def setup_card(self, user):
        token = secrets.token_urlsafe(24)
        self.store.mutate("setup:" + digest(user), lambda _: {
            "hash": digest(token), "expires": int(self.clock()) + 1800, "used": False})
        return response(say("setup_ready"), card={"schema": 2, "skill_id": SKILL_ID, "user_id": user,
                                                  "setup_token": token, "country": CONFIG["country"]})

    def status_card(self):
        """Read back through "check cloud status" (which, unlike the setup card, issues no new token): the last
        device-entry removal, tagged with the request it answers, for `bridge uninstall`, and the latest
        announcement receipt and settings, for `bridge announce --status` and `announce-config`."""
        removal, _ = self.store.get("deregistration")
        receipts, _ = self.store.get("announcements")
        config, _ = self.store.get("configuration")
        card = {}
        if removal:
            card.update(deregistration=removal.get("result"), **{"for": removal.get("for")})
        if receipts and receipts.get("recent"):
            card["announcement"] = receipts["recent"][-1]
        settings = (config or {}).get("announce")
        if settings:  # never the key's hash: whether one is set is enough
            card["announcements"] = {"enabled": settings.get("enabled", True), "echoes": settings.get("echoes", []),
                                     "key": bool(settings.get("key_hash"))}
        return {"skill_id": SKILL_ID, **card} if card else None

    def setup_challenge(self, user, message):
        """The owner's unused, unexpired setup challenge matching the message's token: (challenge, revision, key)."""
        token = message.get("setup_token", "")
        if not isinstance(token, str) or len(token) > 100:
            return None
        key = "setup:" + digest(user)
        challenge, revision = self.store.get(key)
        if (not challenge or challenge["expires"] < self.clock()
                or challenge.get("used") or not hmac.compare_digest(challenge["hash"], digest(token))):
            return None
        return challenge, revision, key

    def remove_registration(self, state, reason):
        """Best effort: ask Amazon to remove this registration's device entry. Never raises."""
        started = time.monotonic()
        try:
            result = self.deregisterer(state)
        except (retail.TransportError, retail.AuthRequired, ValueError, KeyError, TypeError):
            result = "failed"
        print(json.dumps({"event": "hosted_deregistration", "reason": reason, "result": result,
                          "elapsed_ms": round((time.monotonic() - started) * 1000)}))
        return result

    def deregister(self, user, message):
        """`bridge uninstall`: remove the skill's own Amazon device entry before the skill is deleted. Takes the
        same one-use, owner-bound setup token as enrolment. The result, tagged with that request, is read back
        from the "check cloud status" card."""
        found = self.setup_challenge(user, message)
        config, _ = self.store.get("configuration")
        if not found or not config or config["owner_id"] != user:
            return ACK
        challenge, revision, key = found
        challenge["used"] = True
        self.store.put(key, challenge, revision)
        result = "failed"
        try:
            old, _ = self.store.get("retail-session")
            self.ensure_seed(config)  # an enrolment cut off before this would otherwise leave its entry behind
            state, _ = self.store.get("retail-session")
            if old and state and old.get("refresh_token") != state.get("refresh_token"):
                self.remove_registration(old, "replaced")
            result = self.remove_registration(state, "uninstall") if state else "none"
            if state and result in ("removed", "already_invalid"):
                self.sessions.invalidate(state, sign_in=True)  # conditional; never overwrites a newer sign-in
        finally:
            marker = digest(message["setup_token"])[:16]
            self.store.mutate("deregistration", lambda _: {"result": result, "at": int(self.clock()), "for": marker})
        return ACK

    def enroll(self, user, message):
        found = self.setup_challenge(user, message)
        if not found:
            return ACK
        challenge, revision, key = found
        token = message["setup_token"]
        config, config_revision = self.store.get("configuration")
        if config and config["owner_id"] != user:
            return ACK
        try:
            payload = json.loads(message["payload"])
            seed = payload["registration"]
            retail.validate_session(seed)
            if seed["domain"] != retail.COUNTRIES[CONFIG["country"]]["domain"]:
                print(json.dumps({"event": "enrollment_refused", "category": "country_mismatch"}))
                return ACK
            seed["locale"] = CONFIG["locale"]
            seed["enrollment_id"] = digest(token)
            if not all(isinstance(payload.get(k), str) and payload[k]
                       for k in ("client_id", "client_secret")):
                return ACK
        except (KeyError, ValueError, TypeError, retail.AuthRequired):
            return ACK
        announce = (config or {}).get("announce")  # a sign-in repair keeps the owner's announcement choices
        if key_hash(payload.get("announce_key_hash")):
            announce = {"enabled": True, "echoes": [], **(announce or {}),
                        "key_hash": key_hash(payload["announce_key_hash"])}
        config = {"owner_id": user, "client_id": payload["client_id"],
                  "client_secret": payload["client_secret"], "seed": seed,
                  "test_device_name": test_device_name(payload), "setup_at": int(self.clock()),
                  "enrollment_id": digest(token),
                  "webhook_url": payload.get("webhook_url", ""),
                  "webhook_key": payload.get("webhook_key", "")}
        if announce:
            config["announce"] = announce
        previous, _ = self.store.get("retail-session")
        try:
            self.store.put("configuration", config, config_revision)
        except Conflict:
            return ACK
        self.ensure_seed(config)
        challenge["used"] = True
        self.store.put(key, challenge, revision)
        if previous and previous.get("refresh_token") != seed["refresh_token"]:
            self.remove_registration(previous, "replaced")  # a sign-in repair: the old device entry goes
        return ACK

    def ensure_seed(self, config):
        state, revision = self.store.get("retail-session")
        if state is None or state.get("enrollment_id") != config.get("enrollment_id"):
            self.store.put("retail-session", config["seed"], revision)

    def job_status(self, key, job_id, error, terminal=False):
        """Receipt metadata only; never overwrite a newer or already claimed job."""
        def update(current):
            job = (current or {}).get("pending")
            if not job or job.get("id") != job_id or job.get("state") != "pending":
                raise Conflict()
            job["last_error"] = error
            job["updated_at"] = int(self.clock())
            if terminal:
                job["state"] = error
                job.pop("token", None)
            return current
        try:
            self.store.mutate(key, update)
        except Conflict:
            pass
        except Busy as error:
            metadata_failure("job_status", error)

    def status(self, key):
        reply = self.status_reply(key)
        card = self.status_card()
        if card:
            reply["response"]["card"] = {"type": "Simple", "title": say("card_title"), "content": json.dumps(card)}
        return reply

    def status_reply(self, key):
        state, _ = self.store.get("retail-session")
        guidance = auth_guidance(state)
        def explain(text):
            return response((text + " " + guidance).strip())
        row, _ = self.store.get(key)
        job = (row or {}).get("pending") or {}
        outcome = job.get("state")
        if outcome == "pending":
            if job.get("expires", 0) <= self.clock():
                return explain(say("status_expired"))
            if job.get("last_error") == "dispatch_unconfirmed":
                return explain(say("status_dispatch_unconfirmed"))
            if job.get("last_error"):
                return explain(say("status_retrying"))
            return explain(say("status_waiting"))
        if outcome in ("target_unavailable", "auth_required", "dispatch_failed", "delivery_failed",
                       "unconfirmed", "sending", "accepted_by_amazon"):
            return explain(say("outcome_" + outcome))
        if guidance:
            return explain(say("status_nothing_waiting"))
        if not state or retail.needs_renewal(state, self.clock()):
            return response(say("status_renew_next"))
        return response(say("status_ok"))

    def speak(self, session, target, text):
        started = time.monotonic()
        try:
            return self.speaker(session, target, text)
        except retail.AuthRequired:
            self.sessions.invalidate(session)
            raise
        finally:
            print(json.dumps({"event": "hosted_speech_attempt",
                "elapsed_ms": round((time.monotonic() - started) * 1000)}))

    def handle(self, event, context=None):
        try:
            return self._handle(event, context)
        except (retail.AuthRequired, retail.TransportError, retail.UnconfirmedSpeech, Busy, Conflict) as error:
            if event.get("request", {}).get("type") == "Messaging.MessageReceived":
                raise
            if isinstance(error, retail.IdentityMismatch):
                text = auth_guidance({"auth_state": "account_mismatch"})
            elif isinstance(error, retail.RegistrationRejected):
                text = auth_guidance({"auth_state": "sign_in_required"})
            elif isinstance(error, retail.UnconfirmedSpeech):
                text = say("error_unconfirmed_speech")
            elif isinstance(error, (Busy, Conflict)):
                text = say("error_busy")
            else:
                text = say("error_temporary")
            return response(text)

    def _handle(self, event, context=None):
        system = event.get("context", {}).get("System", {})
        req = event.get("request", {})
        kind = req.get("type")
        app_id = system.get("application", {}).get("applicationId")
        # Messaging examples omit application. The hosted Lambda endpoint is
        # skill-restricted; messages additionally require owner and job/setup token.
        if app_id != SKILL_ID and not (kind == "Messaging.MessageReceived" and app_id is None):
            return ACK
        user = system.get("user", {}).get("userId")
        if not isinstance(user, str) or not user or len(user) > 1000:
            return ACK
        if kind == "SessionEndedRequest":
            return ACK
        config, _ = self.store.get("configuration")
        if kind == "Messaging.MessageReceived":
            message = req.get("message", {})
            if isinstance(message, str):
                try:
                    message = json.loads(message)
                except ValueError:
                    return ACK
            if not isinstance(message, dict):
                return ACK
            if message.get("kind") == "setup":
                return self.enroll(user, message)
            if message.get("kind") == "deregister":
                return self.deregister(user, message)
            if message.get("kind") == "announce_config":
                return self.announce_config(user, message)
            if not config or config["owner_id"] != user:
                return ACK
            self.ensure_seed(config)
            if message.get("kind") == "announce":
                return self.announce(config, message)
            return self.answer(user, message)
        if config is None:
            return self.setup_card(user) if kind == "LaunchRequest" else response(say("open_to_set_up"))
        if config["owner_id"] != user:
            return response(say("private"))
        self.ensure_seed(config)
        if kind == "LaunchRequest":
            return response(say("launch"), False)
        intent = req.get("intent", {})
        name = intent.get("name")
        device = system.get("device", {}).get("deviceId")
        if not device:
            return response(say("no_device"))
        key = "device:" + digest(user + "|" + device)
        if name in ("AMAZON.CancelIntent", "AMAZON.StopIntent", "LinkEchoIntent", "EchoNameIntent"):
            try:
                stamp = event_time(req)
                if abs(stamp - self.clock()) > 150:
                    raise ValueError()
            except (KeyError, ValueError, TypeError):
                return response(say("untimed"))
        if name in ("AMAZON.CancelIntent", "AMAZON.StopIntent"):
            def cancel(row):
                row = self.advance_control(row, stamp)
                return {**row, "pending": None, "pairing": None}
            self.store.mutate(key, cancel)
            return response(say("cancelled"))
        if name == "SetupIntent":
            return self.setup_card(user)
        if name in ("StatusIntent", "ResultIntent"):
            return self.status(key)
        if name == "RenewIntent":
            self.sessions.ready(force=True)
            return response(say("renewed"))
        if name == "ProofIntent":
            session = self.sessions.ready(refresh_devices=True)
            matches = [d for d in session.get("devices", [])
                       if d["name"] == config["test_device_name"] and d["online"]]
            device_name = config["test_device_name"]
            if len(matches) != 1:
                return response(say("test_target_unavailable", device=device_name))
            proof_id = digest(req.get("requestId", ""))
            def record(current):
                recent = (current or {}).get("recent", [])
                if any(row.get("id") == proof_id for row in recent):
                    raise DuplicateRequest()
                # Bounded receipts: Alexa only redelivers a request within seconds.
                return {"recent": (recent + [{"id": proof_id, "state": "sending",
                                              "at": int(self.clock())}])[-PROOF_LIMIT:]}
            try:
                self.store.mutate("proofs", record)
            except DuplicateRequest:
                return response(say("test_duplicate"))
            outcome = "unconfirmed"
            try:
                outcome = self.speak(session, matches[0], say("test_sentence"))
            except retail.AuthRequired:
                outcome = "auth_required"
                raise
            finally:
                def settle(current):
                    for row in (current or {}).get("recent", []):
                        if row.get("id") == proof_id:
                            row.update(state=outcome, at=int(self.clock()))
                            return current
                    raise Conflict()
                try:
                    self.store.mutate("proofs", settle)
                except (Busy, Conflict) as error:
                    metadata_failure("proof_outcome", error)
            return response(say("test_sent", device=device_name))
        if name == "AMAZON.HelpIntent":
            return response(say("help"), False)
        row, _ = self.store.get(key)
        row = row or {}
        session_id = event.get("session", {}).get("sessionId")
        if name == "LinkEchoIntent":
            self.start_pairing(key, session_id, stamp, relink=True)
            return response(say("which_echo"), False)
        if name == "EchoNameIntent":
            pairing = row.get("pairing") or {}
            if (pairing.get("expires", 0) <= self.clock() or
                    (pairing.get("session_id") and pairing["session_id"] != session_id)):
                return response(say("link_start"))
            value = intent.get("slots", {}).get("echoName", {}).get("value", "")
            session = self.sessions.ready(refresh_devices=True)
            matches = [d for d in session.get("devices", [])
                       if normal_name(d["name"]) == normal_name(value) and d["online"]]
            if len(matches) != 1:
                return response(say("say_echo_name"), False)
            target = matches[0]
            def link(current):
                if ((current or {}).get("pairing") != pairing or
                        pairing.get("expires", 0) <= self.clock()):
                    raise Conflict()
                current = self.advance_control(current, stamp)
                return {**current, "target": target, "pairing": None, "pending": None}
            try:
                self.store.mutate(key, link)
            except Conflict:
                return response(say("link_expired"))
            held = pairing.get("question")
            if not held or not config.get("webhook_url"):
                return response(say("linked"))
            # The question asked before linking is sent once, as part of this same event.
            row, _ = self.store.get(key)
            reply = self.submit(user, key, row or {}, req, held, config, system.get("apiEndpoint"), same_event=True)
            speech = reply["response"]["outputSpeech"]
            speech["text"] = say("linked_sending") + " " + speech["text"]
            return reply
        question = spoken_request(intent)
        if not question:
            return response(say("need_carrier"), False)
        if not row.get("target"):
            try:
                stamp = event_time(req)
                if abs(stamp - self.clock()) > 150:
                    raise ValueError()
            except (KeyError, ValueError, TypeError):
                return response(say("untimed"))
            held = question if len(question) <= HELD_QUESTION_LIMIT else None
            self.start_pairing(key, session_id, stamp, question=held)
            return response(say("which_echo_held" if held else "which_echo"), False)
        if not config.get("webhook_url"):
            return response(say("routine_missing"))
        return self.submit(user, key, row, req, question, config, system.get("apiEndpoint"))

    def advance_control(self, current, stamp):
        current = current or {}
        if stamp < current.get("event_time", 0):
            raise Conflict()
        current.pop("request_time", None)  # Retire the old mixed-clock watermark.
        return {**current, "event_time": stamp, "generation": current.get("generation", 0) + 1}

    def start_pairing(self, key, session_id, stamp, relink=False, question=None):
        pairing = {"id": secrets.token_hex(16), "expires": int(self.clock()) + 120,
                   "session_id": session_id}
        if question:
            pairing["question"] = question  # held only for this 120 s pairing; cleared on link or cancel
        def start(current):
            current = current or {}
            if not relink and current.get("target"):
                raise Conflict()
            current = self.advance_control(current, stamp)
            return {**current, "pairing": pairing, "pending": None}
        self.store.mutate(key, start)

    def submit(self, user, key, row, req, question, config, endpoint=None, same_event=False):
        job_id = digest(req["requestId"])
        try:
            stamp = event_time(req)
        except (KeyError, TypeError, ValueError):
            return response(say("submit_untimed"))
        if abs(stamp - self.clock()) > 150:
            return response(say("submit_expired"))
        # Local-state preflight only: don't start actions whose answer is already
        # known to be blocked. This isn't a guarantee against later service failure.
        state, _ = self.store.get("retail-session")
        if not state:
            return response(say("not_sent_repair"))
        if state.get("auth_state") in ("sign_in_required", "account_mismatch"):
            return response(say("not_sent_auth", guidance=auth_guidance(state)))
        api_host = DEFAULT_API_HOST if endpoint is None else messaging.api_host(endpoint)
        if not api_host:
            # Unknown answer region: refuse before saving, so an older pending answer survives.
            print(json.dumps({"event": "hosted_request_refused", "category": "unknown_api_endpoint"}))
            return response(say("dispatch_failed"))
        job = {"id": job_id, "token": secrets.token_urlsafe(24),
               "expires": int(self.clock()) + 180, "state": "pending",
               "target": row["target"], "generation": row.get("generation", 0),
               # The answer returns to this request's own Skill Messaging region.
               "api_host": api_host}
        def begin(current):
            current = current or {}
            if job_id in current.get("seen", []):
                raise DuplicateRequest()
            # A held question rides on the linking event itself, so it needs that exact event time.
            if ((stamp != current.get("event_time", 0) if same_event else stamp <= current.get("event_time", 0))
                    or current.get("generation", 0) != job["generation"]
                    or current.get("target") != job["target"]):
                raise Conflict()
            current.pop("request_time", None)
            return {**current, "pending": job, "event_time": stamp,
                    "seen": (current.get("seen", []) + [job_id])[-20:]}
        try:
            self.store.mutate(key, begin)
        except DuplicateRequest:
            return response(say("duplicate"))
        except Conflict:
            return response(say("changed"))
        # A failed/uncertain dispatch is never retried. A valid callback may
        # still arrive after a timeout; its nonce and saved target remain valid.
        started = time.monotonic()
        try:
            self.dispatch(config, user, key, job, question)
        except messaging.DispatchNotSent:
            self.job_status(key, job_id, "dispatch_failed", terminal=True)
            return response(say("dispatch_failed"))
        except retail.TransportError:
            self.job_status(key, job_id, "dispatch_unconfirmed")
            return response(say("dispatch_unconfirmed"))
        finally:
            print(json.dumps({"event": "hosted_agent_dispatch",
                "elapsed_ms": round((time.monotonic() - started) * 1000)}))
        return response(say("ack"))

    def answer(self, user, message):
        key, job_id = message.get("device_key"), message.get("request_id")
        if message.get("kind") != "answer" or not isinstance(key, str) or not re.fullmatch(r"device:[a-f0-9]{64}", key):
            return ACK
        row, _ = self.store.get(key)
        job = (row or {}).get("pending")
        token, text = message.get("job_token"), message.get("answer")
        if (not job or job.get("state") != "pending" or job.get("id") != job_id
                or job.get("expires", 0) <= self.clock()
                or not isinstance(token, str)
                or not hmac.compare_digest(job["token"], token)
                or not isinstance(text, str) or not text.strip() or len(text) > 4000
                or any(ord(c) < 32 and c not in "\n\r\t" for c in text)):
            return ACK
        # Authentication and text/target validation precede the irreversible claim.
        try:
            session = self.sessions.ready(target=job["target"])
            retail.speech_payload(session, job["target"], text)
        except retail.AuthRequired:
            self.job_status(key, job_id, "auth_required", terminal=True)
            return ACK
        except retail.TargetUnavailable as error:
            if error.reason == "missing":
                self.job_status(key, job_id, "target_unavailable", terminal=True)
                return ACK
            self.job_status(key, job_id, "target_offline")
            raise
        except (Busy, Conflict, retail.TransportError):
            self.job_status(key, job_id, "temporary_delivery_failure")
            raise
        def claim(current):
            pending = (current or {}).get("pending")
            if (not pending or pending.get("id") != job_id
                    or pending.get("state") != "pending"
                    or pending.get("generation", 0) != current.get("generation", 0)
                    or pending.get("expires", 0) <= self.clock()):
                raise Conflict()
            pending["state"] = "sending"
            pending.pop("last_error", None)
            return current
        try:
            self.store.mutate(key, claim)
        except Conflict:
            return ACK
        try:
            current, _ = self.store.get(key)
        except Busy as error:
            self.finish_job(key, job_id, "delivery_failed")
            metadata_failure("pre_speech_read", error)
            return ACK
        if not current or not current.get("pending") or current["pending"]["id"] != job_id:
            return ACK
        if (current["pending"].get("generation", 0) != current.get("generation", 0)
                or current["pending"].get("expires", 0) <= self.clock()):
            self.finish_job(key, job_id, "delivery_failed")
            return ACK
        outcome = "unconfirmed"
        try:
            outcome = self.speak(session, job["target"], text)
        except retail.AuthRequired:
            outcome = "auth_required"
        except retail.UnconfirmedSpeech:
            outcome = "unconfirmed"
        finally:
            self.finish_job(key, job_id, outcome)
        return ACK

    def announce_config(self, user, message):
        """Turn announcements on or off, limit them to some Echos, or hand over the announcement key's hash. Takes
        the same one-use, owner-bound setup token as enrolment."""
        found = self.setup_challenge(user, message)
        config, config_revision = self.store.get("configuration")
        if not found or not config or config["owner_id"] != user:
            return ACK
        try:
            change = json.loads(message["payload"])
            if not isinstance(change, dict):
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            return ACK
        announce = dict(config.get("announce") or {"enabled": True, "echoes": []})
        if "key_hash" in change:
            if not key_hash(change["key_hash"]):
                return ACK
            announce["key_hash"] = key_hash(change["key_hash"])
        if "enabled" in change:
            if not isinstance(change["enabled"], bool):
                return ACK
            announce["enabled"] = change["enabled"]
        if "echoes" in change:
            if change["echoes"] != [] and not echo_names(change["echoes"]):
                return ACK
            announce["echoes"] = change["echoes"]  # [] means every eligible Echo
        challenge, revision, key = found
        try:
            self.store.put("configuration", {**config, "announce": announce}, config_revision)
        except Conflict:
            return ACK
        challenge["used"] = True
        self.store.put(key, challenge, revision)
        return ACK

    def announce(self, config, message):
        """Speak the agent's own message on the named Echos, with no spoken request. The announcement key, not the
        skill-wide messaging token, authorises it; names resolve exactly or the whole message is refused; Echos
        with Do Not Disturb on (which Speak would ignore), or where an announcement would be silently dropped, are
        left out; one POST, never replayed."""
        settings = config.get("announce") or {}
        announce_id, offered = message.get("announce_id"), message.get("announce_key")
        if (not isinstance(announce_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", announce_id)
                or not settings.get("key_hash") or not isinstance(offered, str) or len(offered) > 200
                or not hmac.compare_digest(settings["key_hash"], digest(offered))):
            print(json.dumps({"event": "hosted_announcement", "outcome": "refused", "category": "key"}))
            return ACK
        receipts, _ = self.store.get("announcements")
        if any(r.get("id") == announce_id for r in (receipts or {}).get("recent", [])):
            return ACK  # a redelivered message: decided once already
        refuse = lambda reason: self.receipt(announce_id, "refused", reason=reason)
        if not settings.get("enabled", True):
            return refuse("off")
        text, style = message.get("text"), message.get("style", "announce")
        whisper, volume = message.get("whisper") == "1", message.get("volume", "")
        try:
            retail.check_text(text)
            names = json.loads(message.get("targets", ""))
            volume = None if volume == "" else int(volume)
        except (ValueError, TypeError):
            return refuse("invalid")
        if (style not in ("announce", "speak") or not (names == "all" or echo_names(names))
                or (volume is not None and not 0 <= volume <= 100)):
            return refuse("invalid")
        try:
            session = self.sessions.ready(refresh_devices=True)
        except retail.AuthRequired:
            return refuse("auth_required")
        allowed = [d for d in session.get("devices", [])
                   if not settings.get("echoes")
                   or normal_name(d["name"]) in {normal_name(n) for n in settings["echoes"]}]
        if names == "all":
            chosen = allowed
        else:
            chosen = []
            for name in names:
                matches = [d for d in allowed if normal_name(d["name"]) == normal_name(name)]
                if len(matches) != 1:
                    return refuse("unknown_echo")  # no fallback to another Echo
                if matches[0] not in chosen:
                    chosen.append(matches[0])
        if not chosen:
            return refuse("unknown_echo")
        echoes = {d["name"]: "offline" for d in chosen if not d["online"]}
        targets = [d for d in chosen if d["online"]]
        try:
            asleep = self.dnd(session)
        except retail.TransportError:
            asleep = None  # Speak would play through it, so unknown means not sent
        for d in list(targets):
            key = (d["serial"], d["type"])
            if asleep is None and style == "speak":
                echoes[d["name"]] = "do_not_disturb_unknown"
            elif asleep and asleep.get(key):
                echoes[d["name"]] = "do_not_disturb"
            elif style == "announce" and self.allowed(session, d) is False:
                echoes[d["name"]] = "announcements_off"
            else:
                continue
            targets.remove(d)
        levels = None
        if targets and volume is not None:
            try:
                levels = self.volumes(session)
            except retail.TransportError:
                levels = {}  # the volume stays as it is: never set without a level to restore
        if not targets:
            return self.receipt(announce_id, "skipped", echoes=echoes)
        try:
            payload = retail.announcement_payload(session, targets, text, style=style, whisper=whisper,
                                                  title=CONFIG["display_name"], volume=volume, volumes=levels)
        except retail.TargetUnavailable:
            raise retail.TransportError("Devices changed; let Amazon redeliver")  # unclaimed: safe to redeliver
        except ValueError:
            return refuse("invalid")
        try:
            self.receipt(announce_id, "sending", echoes=echoes, claim=True)
        except DuplicateRequest:
            return ACK
        started, outcome = time.monotonic(), "unconfirmed"
        try:
            outcome = self.announcer(session, targets, text, style=style, whisper=whisper,
                                     title=CONFIG["display_name"], volume=volume, volumes=levels)
        except retail.AuthRequired:
            outcome = "auth_required"
            self.sessions.invalidate(session)
        except retail.Throttled:
            outcome = "throttled"
        except retail.UnconfirmedSpeech:
            outcome = "unconfirmed"
        finally:
            for d in targets:
                echoes[d["name"]] = outcome
            print(json.dumps({"event": "hosted_announcement", "outcome": outcome, "echoes": len(targets),
                              "elapsed_ms": round((time.monotonic() - started) * 1000)}))
            try:
                self.receipt(announce_id, outcome, echoes=echoes, settle=True)
            except (Busy, Conflict) as error:
                metadata_failure("announcement_outcome", error)
        return ACK

    def receipt(self, announce_id, state, reason=None, echoes=None, claim=False, settle=False):
        """Bounded receipts with no text: id, state, the outcome for each Echo, time. A claim fails if the id was
        already recorded, so a redelivered message never speaks twice."""
        row = {"id": announce_id, "state": state, "at": int(self.clock()), "echoes": echoes or {}}
        if reason:
            row["reason"] = reason
        def record(current):
            recent = (current or {}).get("recent", [])
            mine = [r for r in recent if r.get("id") == announce_id]
            if settle:
                if not mine:
                    raise Conflict()
                mine[0].update(row)
                return current
            if mine:
                raise DuplicateRequest()
            return {"recent": (recent + [row])[-ANNOUNCE_LIMIT:]}
        try:
            self.store.mutate("announcements", record)
        except DuplicateRequest:
            if claim:
                raise
        except Busy as error:
            if claim or settle:
                raise
            metadata_failure("announcement_receipt", error)
        if not claim and not settle:
            print(json.dumps({"event": "hosted_announcement", "outcome": state, "category": reason or ""}))
        return ACK

    def finish_job(self, key, job_id, outcome):
        def finish(current):
            pending = (current or {}).get("pending")
            if not pending or pending.get("id") != job_id or pending.get("state") != "sending":
                raise Conflict()
            pending["state"] = outcome
            pending.pop("token", None)
            return current
        try:
            self.store.mutate(key, finish)
        except Conflict:
            pass
        except Busy as error:
            metadata_failure("delivery_outcome", error)


def lambda_handler(event, context):
    started = time.monotonic()
    try:
        return App(Store()).handle(event, context)
    except Exception as error:
        # Never print event, request text, credentials, URL or exception message.
        print(json.dumps({"event": "hosted_error", "category": type(error).__name__}))
        if event.get("request", {}).get("type") == "Messaging.MessageReceived":
            # Unclaimed transient work can be redelivered; claimed speech won't replay.
            raise RuntimeError("Callback could not complete") from None
        return response(say("error_needs_attention"))
    finally:
        print(json.dumps({"event": "hosted_invocation_complete",
                          "elapsed_ms": round((time.monotonic() - started) * 1000)}))
