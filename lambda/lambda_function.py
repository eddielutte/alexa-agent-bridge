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


def spoken_request(intent):
    value = intent.get("slots", {}).get("request", {}).get("value", "").strip()
    if not value:
        return None
    # Model supplies carrier prefixes; restore words removed from the query slot.
    prefix = PACK["carriers"].get(intent.get("name"))
    return None if prefix is None else ("" if intent["name"] == "PleaseIntent" else prefix) + value


class App:
    def __init__(self, store, sessions=None, clock=time.time, dispatch=messaging.dispatch,
                 speaker=retail.speak_once):
        self.store, self.clock = store, clock
        self.sessions = sessions or Sessions(store, clock)
        self.dispatch, self.speaker = dispatch, speaker

    def setup_card(self, user):
        token = secrets.token_urlsafe(24)
        self.store.mutate("setup:" + digest(user), lambda _: {
            "hash": digest(token), "expires": int(self.clock()) + 1800, "used": False})
        return response(say("setup_ready"), card={"schema": 2, "skill_id": SKILL_ID, "user_id": user,
                                                  "setup_token": token, "country": CONFIG["country"]})

    def enroll(self, user, message):
        token = message.get("setup_token", "")
        if not isinstance(token, str) or len(token) > 100:
            return ACK
        key = "setup:" + digest(user)
        challenge, revision = self.store.get(key)
        if (not challenge or challenge["expires"] < self.clock()
                or challenge.get("used") or not hmac.compare_digest(challenge["hash"], digest(token))):
            return ACK
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
        config = {"owner_id": user, "client_id": payload["client_id"],
                  "client_secret": payload["client_secret"], "seed": seed,
                  "test_device_name": test_device_name(payload), "setup_at": int(self.clock()),
                  "enrollment_id": digest(token),
                  "webhook_url": payload.get("webhook_url", ""),
                  "webhook_key": payload.get("webhook_key", "")}
        try:
            self.store.put("configuration", config, config_revision)
        except Conflict:
            return ACK
        self.ensure_seed(config)
        challenge["used"] = True
        self.store.put(key, challenge, revision)
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
            if not config or config["owner_id"] != user:
                return ACK
            self.ensure_seed(config)
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
