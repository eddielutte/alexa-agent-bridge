"""Config, countries, messaging regions, enrolment versioning and model generation."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import test_skill as f
import retail
import messaging
import lambda_function
from lambda_function import App, digest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import build_model


def country_session(code):
    row = retail.COUNTRIES[code]
    return {**f.session(), "domain": row["domain"]}


class ConfigTests(unittest.TestCase):
    def test_deployed_config_loads(self):
        self.assertEqual(lambda_function.SKILL_ID, lambda_function.CONFIG["skill_id"])
        self.assertEqual(lambda_function.DEFAULT_API_HOST, retail.messaging_host(lambda_function.CONFIG["country"]))

    def test_invalid_configs_are_refused(self):
        good = dict(lambda_function.CONFIG)
        for change in ({"schema": 2}, {"skill_id": "amzn1.ask.skill.x"}, {"display_name": " "},
                       {"country": "XX"}, {"locale": "en-US"}, {"language": "eng"},
                       {"test_device_name": None}, {"agent_name": 5}):
            with self.assertRaises(ValueError):
                lambda_function.load_config({**good, **change})
        self.assertEqual(lambda_function.load_config(good), good)

    def test_every_spoken_string_formats(self):
        for key in lambda_function.PACK["strings"]:
            self.assertTrue(lambda_function.say(key, device="Office Echo", guidance="x").strip())

    def test_agent_name_fills_spoken_strings_with_a_pack_fallback(self):
        for name, expected in (("Grokbot", "not sent to Grokbot."), ("", "not sent to your agent.")):
            with patch.dict(lambda_function.CONFIG, agent_name=name):
                self.assertIn(expected, lambda_function.say("dispatch_failed"))
        old = {k: v for k, v in lambda_function.CONFIG.items() if k != "agent_name"}
        self.assertEqual(lambda_function.load_config(old), old)


class CountryRetailTests(unittest.TestCase):
    """Run the retail invariants once per country row with fake hosts."""

    def test_country_table_is_consistent(self):
        for code, row in retail.COUNTRIES.items():
            self.assertIn(row["region"], retail.REGIONS, code)
            self.assertTrue(row["locales"], code)
            self.assertIn("alexa." + row["domain"], retail.ALLOWED_HOSTS)
        self.assertEqual(retail.messaging_host("AU"), "api.fe.amazonalexa.com")
        self.assertEqual(retail.messaging_host("US"), "api.amazonalexa.com")

    def test_renewal_uses_only_the_sessions_own_domain(self):
        for code, row in retail.COUNTRIES.items():
            domain, calls = row["domain"], []
            def transport(method, url, headers=None, data=None, **kwargs):
                calls.append((url, headers, data))
                if url.endswith("/auth/token"):
                    return 200, [], json.dumps({"response": {"tokens": {"cookies": {
                        "." + domain: [{"Name": "session-token", "Value": "NEW"}],
                        ".amazon.co.uk" if domain != "amazon.co.uk" else ".amazon.com":
                            [{"Name": "foreign", "Value": "no"}]}}}}).encode()
                if url.endswith("/api/users/me"):
                    return 200, [("Set-Cookie", "csrf=C; Path=/; Domain=." + domain)], b'{"id":"customer"}'
                return 200, [], b'{"devices":[]}'
            result = retail.renew(country_session(code), transport, now=1200)
            self.assertIn(b"domain=www." + domain.encode(), calls[0][2], code)
            self.assertTrue(all(u.startswith("https://alexa." + domain + "/") for u, _, _ in calls[1:]), code)
            self.assertEqual(calls[1][1]["Accept-Language"], row["locales"][0])
            self.assertNotIn("foreign", result["cookies"], code)
            self.assertEqual(result["cookies"]["csrf"], "C")

    def test_speech_targets_the_sessions_domain_and_locale(self):
        for code, row in retail.COUNTRIES.items():
            sent = []
            state = country_session(code)
            retail.speak_once(state, state["devices"][0], "hello",
                              lambda *a: sent.append(a) or (200, [], b""))
            self.assertEqual(sent[0][1], "https://alexa." + row["domain"] + "/api/behaviors/preview")
            node = json.loads(json.loads(sent[0][3])["sequenceJson"])["startNode"]["nodesToExecute"][0]
            self.assertEqual(node["operationPayload"]["locale"], row["locales"][0])

    def test_session_locale_overrides_country_default(self):
        self.assertEqual(retail.locale_for({**country_session("CA"), "locale": "fr-CA"}), "fr-CA")
        self.assertEqual(retail.locale_for({**country_session("CA"), "locale": "bad"}), "en-CA")

    def test_unknown_domain_is_rejected_before_network(self):
        with self.assertRaises(retail.RegistrationRejected):
            retail.renew({**f.session(), "domain": "amazon.evil.test"}, lambda *a: self.fail("Network"))
        with self.assertRaises(ValueError):
            retail.request("GET", "https://alexa.amazon.evil.test/api")


class MessagingRegionTests(unittest.TestCase):
    def test_api_host_accepts_only_known_regions(self):
        for host in ("api.amazonalexa.com", "api.eu.amazonalexa.com", "api.fe.amazonalexa.com"):
            self.assertEqual(messaging.api_host("https://" + host), host)
        for endpoint in ("https://api.fe.amazon.com", "http://api.amazonalexa.com",
                         "https://api.amazonalexa.com:8443", "https://api.amazonalexa.com/x",
                         "https://user@api.amazonalexa.com", None, 7):
            self.assertEqual(messaging.api_host(endpoint), "")

    def test_callback_url_follows_the_region(self):
        self.assertTrue(messaging.callback_url("u", "api.amazonalexa.com").startswith(
            "https://api.amazonalexa.com/v1/skillmessages/users/"))
        with self.assertRaises(ValueError):
            messaging.callback_url("u", "")

    def test_unknown_region_is_not_sent_before_any_network(self):
        config = {"webhook_url": "https://example.test/hook", "webhook_key": "FAKE",
                  "client_id": "FAKE", "client_secret": "FAKE"}
        with self.assertRaises(messaging.DispatchNotSent):
            messaging.dispatch(config, "u", "d", {"id": "j", "token": "t", "api_host": ""}, "q",
                               lambda *a: self.fail("Network called"))


class AppRegionAndEnrolmentTests(unittest.TestCase):
    def setUp(self):
        self.t = f.AppTests("test_valid_message_without_application_delivered_once")
        self.t.setUp()
        self.app, self.store = self.t.app, self.t.store
        self.store.put(self.t.key, {"target": f.session()["devices"][0]})
        config, revision = self.store.get("configuration")
        self.store.put("configuration", {**config, "webhook_url": "https://example.test/hook",
                                         "webhook_key": "FAKE"}, revision)
        self.dispatched = []
        self.app.dispatch = lambda config, user, key, job, question: self.dispatched.append(job)

    def ask(self, endpoint="absent"):
        event = self.t.voice("PleaseIntent", {"request": {"value": "say hello"}})
        if endpoint != "absent":
            event["context"]["System"]["apiEndpoint"] = endpoint
        return self.app.handle(event)["response"]["outputSpeech"]["text"]

    def test_request_region_is_saved_with_the_job(self):
        self.assertEqual(self.ask("https://api.amazonalexa.com"), "Okay, one moment.")
        self.assertEqual(self.dispatched[0]["api_host"], "api.amazonalexa.com")

    def test_missing_endpoint_uses_the_country_region(self):
        self.ask()
        self.assertEqual(self.dispatched[0]["api_host"], "api.eu.amazonalexa.com")

    def test_unknown_endpoint_is_not_sent_and_keeps_older_answer(self):
        older = {"id": "older", "state": "pending"}
        row, revision = self.store.get(self.t.key)
        self.store.put(self.t.key, {**row, "pending": older}, revision)
        for endpoint in ("https://api.evil.test", "https://api.amazonalexa.com:bad", "https://[::1"):
            self.assertIn("not sent", self.ask(endpoint))
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self.store.get(self.t.key)[0]["pending"], older)

    def card(self):
        return json.loads(self.app.setup_card("owner")["response"]["card"]["content"])

    def test_setup_card_is_versioned(self):
        card = self.card()
        self.assertEqual((card["schema"], card["country"]), (2, "GB"))

    def enrol(self, registration, **extra):
        card = self.card()
        payload = {"client_id": "FAKE", "client_secret": "FAKE", "registration": registration, **extra}
        self.app.enroll("owner", {"setup_token": card["setup_token"], "payload": json.dumps(payload)})
        return self.store.get("configuration")[0]

    def test_enrolment_from_another_country_is_refused(self):
        before = self.store.get("configuration")[0]
        self.assertEqual(self.enrol(country_session("US")), before)

    def test_enrolment_records_locale_and_test_device(self):
        config = self.enrol(f.session(), test_device_name="Kitchen Echo Show")
        self.assertEqual(config["test_device_name"], "Kitchen Echo Show")
        self.assertEqual(config["seed"]["locale"], "en-GB")
        self.assertEqual(self.enrol(f.session())["test_device_name"], lambda_function.CONFIG["test_device_name"])

    def test_proof_receipts_are_bounded(self):
        clock = iter(range(1500, 1600))
        for n in range(lambda_function.PROOF_LIMIT + 5):
            event = self.t.voice("ProofIntent")
            event["request"]["requestId"] = "proof-%d" % n
            self.app.handle(event)
        recent = self.store.get("proofs")[0]["recent"]
        self.assertEqual(len(recent), lambda_function.PROOF_LIMIT)
        self.assertEqual(recent[-1]["id"], digest("proof-%d" % (lambda_function.PROOF_LIMIT + 4)))
        self.assertFalse(any(k.startswith("proof:") for k in self.store.rows))


class ModelTests(unittest.TestCase):
    def test_generated_model_matches_the_deployed_model(self):
        path = ROOT / "interaction-model.json"
        if not path.exists():
            self.skipTest("no deployed model in this checkout")
        deployed = path.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
        invocation = json.loads(deployed)["interactionModel"]["languageModel"]["invocationName"]
        self.assertEqual(build_model.build("en", invocation), deployed)

    def test_owner_echo_names_replace_the_examples(self):
        model = json.loads(build_model.build("en", "nova a. i.", ["Kitchen Echo Show", "Office Echo", "Den"]))
        types = model["interactionModel"]["languageModel"]["types"]
        values = [t for t in types if t["name"] == "EchoDeviceName"][0]["values"]
        self.assertEqual(values, [{"name": {"value": "Den"}},
                                  {"name": {"value": "Kitchen Echo Show", "synonyms": ["Kitchen"]}},
                                  {"name": {"value": "Office Echo", "synonyms": ["Office"]}}])

    def test_invocation_rules(self):
        self.assertEqual(build_model.invocation_problems("nova a. i."), [])
        self.assertEqual(build_model.invocation_problems("kitchen helper"), [])
        for bad in ("nova", "Nova AI", "nova ai2", "ask nova", "nova echo", "the bot",
                    "nova to go", "nova a.i."):
            self.assertTrue(build_model.invocation_problems(bad), bad)
        with self.assertRaises(ValueError):
            build_model.build("en", "alexa helper")
        self.assertEqual(build_model.invocation_problems("küchen hilfe", "de"), [])
        self.assertTrue(build_model.invocation_problems("alexa hilfe", "de"))
        self.assertTrue(build_model.invocation_problems("hilfe2 bot", "de"))


if __name__ == "__main__":
    unittest.main()
