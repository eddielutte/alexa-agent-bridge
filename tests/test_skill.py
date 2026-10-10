"""Focused invariant tests; no credentials/network/cloud resources."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambda"))
import retail
import messaging
from storage import Conflict, Busy, Store, Sessions
from lambda_function import App, SKILL_ID, digest


class Memory:
    def __init__(self):
        self.rows = {}
        self.version = 0

    def get(self, key):
        value = self.rows.get(key)
        return (copy.deepcopy(value[0]), value[1]) if value else (None, None)

    def put(self, key, value, expected=None):
        _, version = self.get(key)
        if version != expected:
            raise Conflict()
        self.version += 1
        self.rows[key] = (copy.deepcopy(value), str(self.version))
        return str(self.version)

    mutate = Store.mutate


def session():
    return {"schema": 1, "domain": "amazon.co.uk", "customer_id": "customer",
            "refresh_token": "FAKE-refresh", "cookies": {"csrf": "FAKE-csrf"},
            "access_expires": 10000, "cookies_refreshed_at": 1000,
            "devices_refreshed_at": 1500,
            "devices": [{"serial": "office", "type": "echo", "name": "Office Echo",
                         "online": True}]}


class RetailTests(unittest.TestCase):
    def test_payload_is_text_and_exact_single_target(self):
        payload = retail.speech_payload(session(), session()["devices"][0], '<audio src="x"/> & hello')
        seq = json.loads(payload["sequenceJson"])
        node = seq["startNode"]["nodesToExecute"][0]
        self.assertEqual(node["type"], "Alexa.Speak")
        self.assertEqual(len(node["operationPayload"]["target"]["devices"]), 1)
        self.assertIn("&lt;audio", node["operationPayload"]["textToSpeak"])

    def test_unknown_offline_or_overlong_never_dispatch(self):
        for target, text in [({"serial": "bedroom", "type": "echo"}, "hello"),
                             (session()["devices"][0], "x" * 4001),
                             (session()["devices"][0], "bad\x00")]:
            with self.assertRaises(ValueError):
                retail.speak_once(session(), target, text, lambda *a: self.fail("Network called"))

    def test_a_failed_or_uncertain_send_is_one_attempt(self):
        def raises(*args):
            raise retail.TransportError()
        cases = [(raises, retail.UnconfirmedSpeech)]
        cases += [(lambda *a, s=status: (s, [], b""), retail.UnconfirmedSpeech) for status in (302, 429, 500, 503)]
        cases += [(lambda *a, s=status: (s, [], b""), retail.AuthRequired) for status in (401, 403)]
        for reply, error in cases:
            calls = []
            def send(*args):
                calls.append(args)
                return reply(*args)
            with self.subTest(error=error.__name__), self.assertRaises(error):
                retail.speak_once(session(), session()["devices"][0], "hello", send)
            self.assertEqual(len(calls), 1)

    def test_credential_headers_reject_newlines(self):
        state = session()
        state["cookies"]["bad"] = "x\r\nInjected: yes"
        with self.assertRaises(retail.AuthRequired):
            retail.headers_for(state)

    def test_only_single_echo_devices_eligible(self):
        base = {"serialNumber": "one", "deviceType": "echo", "accountName": "Office",
                "capabilities": ["AUDIO_PLAYER"], "online": True}
        rows = [base, {**base, "deviceFamily": "WHA"},
                {**base, "clusterMembers": ["two"]}, {**base, "capabilities": []}]
        self.assertEqual(len(retail.eligible_devices({"devices": rows})), 1)

    def test_full_renewal_verified_without_mutating_original(self):
        original = session()
        original["cookies"] = {"csrf": "STALE"}
        calls = []
        def transport(method, url, headers=None, data=None, **kwargs):
            calls.append(url)
            if url.endswith("/auth/token"):
                if b"requested_token_type=access_token" in data:
                    return 200, [], b'{"access_token":"NEW","expires_in":3600}'
                return 200, [], json.dumps({"response": {"tokens": {"cookies": {
                    ".amazon.co.uk": [{"Name": "session-token", "Value": '"NEW-cookie"'}],
                    ".evil.example": [{"Name": "evil", "Value": "no"}]}}}}).encode()
            if url.endswith("/api/users/me"):
                return 200, [("Set-Cookie", "csrf=NEW-csrf; Path=/; Domain=.amazon.co.uk")], b'{"id":"customer"}'
            return 200, [], b'{"devices":[]}'
        result = retail.renew(original, transport, now=1200)
        self.assertEqual(result["cookies"]["csrf"], "NEW-csrf")
        self.assertNotIn("evil", result["cookies"])
        self.assertNotIn("access_expires", result)
        self.assertNotIn("access_token", result)
        self.assertEqual(original["cookies"], {"csrf": "STALE"})
        self.assertEqual(len(calls), 3)

    def test_partial_renewal_failure_preserves_registration(self):
        original = session()
        def fail(*args):
            return 503, [], b""
        with self.assertRaises(retail.TransportError):
            retail.renew(original, fail, now=1200)
        self.assertEqual(original, session())


class SessionTests(unittest.TestCase):
    def test_fresh_state_needs_no_renewal(self):
        store = Memory()
        store.put("retail-session", session())
        result = Sessions(store, lambda: 1500, lambda *_a, **_k: self.fail()).ready()
        self.assertEqual(result, session())

    def test_stale_state_renewed_persisted_and_lease_released(self):
        store = Memory()
        store.put("retail-session", session())
        def renew(state, now):
            state["cookies"] = {"csrf": "NEW"}
            state["access_expires"] = now + 3600
            state["cookies_refreshed_at"] = now
            return state
        Sessions(store, lambda: 11000, renew).ready()
        self.assertEqual(store.get("retail-session")[0]["cookies"]["csrf"], "NEW")
        self.assertEqual(store.get("session-lease")[0]["until"], 0)

    def test_other_worker_blocks_refresh(self):
        store = Memory()
        store.put("retail-session", session())
        store.put("session-lease", {"owner": "other", "until": 12000})
        with self.assertRaises(Busy):
            Sessions(store, lambda: 11000).ready()

    def test_failed_refresh_keeps_original_and_releases(self):
        store = Memory()
        store.put("retail-session", session())
        def renew(*args, **kwargs):
            raise retail.RegistrationRejected()
        with self.assertRaises(retail.AuthRequired):
            Sessions(store, lambda: 11000, renew).ready()
        self.assertEqual(store.get("retail-session")[0], {**session(), "auth_state": "sign_in_required"})
        self.assertEqual(store.get("session-lease")[0]["until"], 0)


class MessagingTests(unittest.TestCase):
    def test_payload_enforces_string_values_and_utf8_size(self):
        for data in ({"nested": {}}, {"answer": "界" * 2100}):
            with self.assertRaises(ValueError):
                messaging.send_message("user", "FAKE", data, lambda *_: self.fail())

    def test_real_contract_shape(self):
        calls = []
        def sender(*args):
            calls.append(args)
            return 202, b""
        self.assertEqual(messaging.send_message("amzn1.ask.user.test", "FAKE",
            {"kind": "test"}, sender, host="api.eu.amazonalexa.com"), "queued")
        body = json.loads(calls[0][3])
        self.assertEqual(body, {"data": {"kind": "test"}, "expiresAfterSeconds": 120})


class AppTests(unittest.TestCase):
    def setUp(self):
        self.store = Memory()
        self.store.put("configuration", {"owner_id": "owner", "seed": session(),
                                        "test_device_name": "Office Echo"})
        self.store.put("retail-session", session())
        self.sent = []
        sessions = Sessions(self.store, lambda: 1500,
            discoverer=lambda state, now: {**state, "devices_refreshed_at": now})
        self.app = App(self.store, sessions=sessions, clock=lambda: 1500,
            speaker=lambda *args: self.sent.append(args) or "accepted_by_amazon")
        self.key = "device:" + digest("owner|device")

    def voice(self, name, slots=None, user="owner", device="device"):
        return {"context": {"System": {"application": {"applicationId": SKILL_ID},
                 "user": {"userId": user}, "device": {"deviceId": device}}},
                "request": {"type": "IntentRequest", "requestId": "voice-1",
                            "timestamp": "1970-01-01T00:25:00Z",
                            "intent": {"name": name, "slots": slots or {}}}}

    def pending(self, **changes):
        job = {"id": "job", "token": "SECRET", "expires": 1600,
               "state": "pending", "target": session()["devices"][0], **changes}
        self.store.put(self.key, {"target": job["target"], "pending": job})
        return {"kind": "answer", "device_key": self.key, "request_id": "job",
                "job_token": "SECRET", "answer": "Complete answer."}

    def message(self, data, user="owner"):
        return {"context": {"System": {"user": {"userId": user}}},
                "request": {"type": "Messaging.MessageReceived", "message": data}}

    def held(self, webhook=True):
        config, rev = self.store.get("configuration")
        self.store.put("configuration", {**config, "webhook_url": "https://example.test/hook" if webhook else ""}, rev)
        self.dispatched = []
        self.app.dispatch = lambda config, user, key, job, question: self.dispatched.append(question)
        return lambda value: self.app.handle(self.voice("PleaseIntent", {"request": {"value": value}}))

    def text(self, reply):
        return reply["response"]["outputSpeech"]["text"]

    def test_first_question_on_unlinked_echo_is_sent_once_after_linking(self):
        ask = self.held()
        self.assertIn("once it's linked", self.text(ask("tell me a joke")))
        self.assertEqual(self.dispatched, [])
        name = self.voice("EchoNameIntent", {"echoName": {"value": "Office"}})
        self.assertEqual(self.text(self.app.handle(name)), "This Echo is linked. Okay, one moment.")
        self.assertEqual(self.dispatched, ["tell me a joke"])
        row = self.store.get(self.key)[0]
        self.assertIsNone(row["pairing"])
        self.assertEqual((row["target"]["serial"], row["pending"]["state"]), ("office", "pending"))
        self.app.handle(name)
        self.assertEqual(self.dispatched, ["tell me a joke"])

    def test_held_question_is_discarded_by_cancel_expiry_or_a_newer_question(self):
        name = self.voice("EchoNameIntent", {"echoName": {"value": "Office"}})
        ask = self.held()
        ask("first question")
        self.app.handle(self.voice("AMAZON.CancelIntent"))
        self.app.handle(name)
        self.assertEqual(self.dispatched, [])
        self.store.rows.pop(self.key)
        ask("second question")
        self.app.clock = lambda: 1630
        self.app.handle(name)
        self.assertEqual(self.dispatched, [])
        self.assertNotIn("target", self.store.get(self.key)[0])
        self.store.rows.pop(self.key)
        self.app.clock = lambda: 1500
        ask("older question")
        ask("newer question")
        self.app.handle(name)
        self.assertEqual(self.dispatched, ["newer question"])

    def test_relink_holds_nothing_and_missing_routine_only_links(self):
        ask = self.held(webhook=False)
        ask("tell me a joke")
        reply = self.app.handle(self.voice("EchoNameIntent", {"echoName": {"value": "Office"}}))
        self.assertEqual(self.text(reply), "This Echo is linked. Please ask your question.")
        self.held()
        self.app.handle(self.voice("LinkEchoIntent"))
        self.assertNotIn("question", self.store.get(self.key)[0]["pairing"])
        self.app.handle(self.voice("EchoNameIntent", {"echoName": {"value": "Office"}}))
        self.assertEqual(self.dispatched, [])

    def test_valid_message_without_application_delivered_once(self):
        data = self.pending()
        self.app.handle(self.message(data))
        self.app.handle(self.message(data))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1]["serial"], "office")

    def test_wrong_owner_never_speaks(self):
        data = self.pending()
        self.app.handle(self.message(data, "other"))
        self.assertEqual(self.sent, [])

    def test_invalid_nonce_never_speaks(self):
        data = self.pending()
        data["job_token"] = "WRONG"
        self.app.handle(self.message(data))
        self.assertEqual(self.sent, [])

    def test_expired_job_never_speaks(self):
        data = self.pending(expires=1499)
        self.app.handle(self.message(data))
        self.assertEqual(self.sent, [])

    def test_cancelled_job_never_speaks(self):
        data = self.pending()
        self.app.handle(self.voice("AMAZON.CancelIntent"))
        self.app.handle(self.message(data))
        self.assertEqual(self.sent, [])

    def test_superseded_job_never_speaks(self):
        data = self.pending(id="newer-job")
        self.app.handle(self.message(data))
        self.assertEqual(self.sent, [])

    def test_failed_speech_not_replayed(self):
        data = self.pending()
        calls = []
        def speak(*args):
            calls.append(args)
            raise retail.UnconfirmedSpeech()
        self.app.speaker = speak
        self.app.handle(self.message(data))
        self.app.handle(self.message(data))
        self.assertEqual(len(calls), 1)

    def test_pairing_does_not_default_on_unknown_name(self):
        self.app.handle(self.voice("LinkEchoIntent"))  # an active prompt, so only the name decides
        self.app.handle(self.voice("EchoNameIntent", {"echoName": {"value": "not an echo"}}))
        self.assertNotIn("target", self.store.get(self.key)[0])
        self.assertEqual(self.sent, [])

    def test_relink_invalidates_pending_answer(self):
        data = self.pending()
        self.app.handle(self.voice("LinkEchoIntent"))
        self.app.handle(self.voice("EchoNameIntent", {"echoName": {"value": "Office"}}))
        self.app.handle(self.message(data))
        self.assertEqual(self.sent, [])
        self.assertIsNone(self.store.get(self.key)[0]["pending"])

    def test_enrollment_is_owner_bound_and_one_use(self):
        store = Memory()
        app = App(store, clock=lambda: 1500)
        card = json.loads(app.setup_card("owner")["response"]["card"]["content"])
        payload = {"registration": session(), "client_id": "FAKE-ID", "client_secret": "FAKE-secret"}
        data = {"kind": "setup", "setup_token": card["setup_token"], "payload": json.dumps(payload)}
        app.enroll("owner", {**data, "setup_token": "wrong"})
        app.enroll("other", data)
        self.assertIsNone(store.get("configuration")[0])
        app.enroll("owner", data)
        before = copy.deepcopy(store.rows)
        app.enroll("owner", data)
        self.assertEqual(before, store.rows)
        self.assertEqual(store.get("configuration")[0]["owner_id"], "owner")

    def test_reenrollment_changes_only_owners_seed(self):
        card = json.loads(self.app.setup_card("owner")["response"]["card"]["content"])
        new = session()
        new["refresh_token"] = "FAKE-new"
        self.app.enroll("owner", {"setup_token": card["setup_token"], "payload": json.dumps({
            "registration": new, "client_id": "FAKE-ID", "client_secret": "FAKE-secret"})})
        self.assertEqual(self.store.get("retail-session")[0]["refresh_token"], "FAKE-new")

    def test_wrong_skill_no_effect(self):
        event = self.voice("ProofIntent")
        event["context"]["System"]["application"]["applicationId"] = "production"
        self.app.handle(event)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
