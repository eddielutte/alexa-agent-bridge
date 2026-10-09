"""bridge CLI: network-free tests with a fake ASK CLI, fake Amazon hosts and a temporary BRIDGE_HOME."""
import contextlib
import io
import json
import os
import re
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import core  # noqa: E402

SKILL = "amzn1.ask.skill.00000000-0000-0000-0000-000000000000"
SECRETS = ("FAKE-SECRET", "FAKE-REFRESH", "FAKE-KEY", "FAKE-SETUP", "FAKE-CODE", "FAKE-GIT")


class FakeAsk:
    """Answers `ask smapi ...` like Amazon would; records calls and simulator utterances."""

    def __init__(self, permission="ALLOWED", credentials_missing=True, configured=False):
        self.calls, self.utterances = [], []
        self.permission, self.credentials_missing, self.configured = permission, credentials_missing, configured
        self.bodies = {}

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if args[1] != "smapi":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        name, opts = args[2], dict(zip(args[3::2], args[4::2]))
        for key, value in opts.items():
            if isinstance(value, str) and value.startswith("file:"):
                self.bodies[name] = json.loads(Path(value[5:]).read_text())
        reply = {
            "get-vendor-list": {"vendors": [{"id": "VENDOR", "name": "Owner"}]},
            "get-alexa-hosted-skill-user-permissions": {"permission": "newSkill", "status": self.permission,
                                                        "actionUrl": "https://example.test/captcha"},
            "create-skill-for-vendor": {"skillId": SKILL},
            "get-skill-status": {"hostedSkillProvisioning": {"lastUpdateRequest": {"status": "SUCCEEDED"}},
                                 "hostedSkillDeployment": {"lastUpdateRequest": {"status": "SUCCEEDED"}},
                                 "manifest": {"lastUpdateRequest": {"status": "SUCCEEDED"}},
                                 "interactionModel": {"en-GB": {"lastUpdateRequest": {"status": "SUCCEEDED"}}}},
            "get-skill-manifest": {"manifest": {"publishingInformation": {}, "apis": {"custom": {}}}},
            "simulate-skill": {"id": "SIM"},
        }.get(name, {})
        if name == "get-skill-credentials":
            if self.credentials_missing and "update-skill-manifest" not in [c[2] for c in self.calls if c[1] == "smapi"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="Request failed with status code 404")
            reply = {"skillMessagingCredentials": {"clientId": "CLIENT", "clientSecret": "FAKE-SECRET"}}
        if name == "simulate-skill":
            self.utterances.append(opts["--input-content"])
        if name == "get-skill-simulation":
            utterance = self.utterances[-1]
            card = None
            if (utterance.startswith("open") and not self.configured) or "repair" in utterance:
                card = {"type": "Simple", "content": json.dumps(
                    {"schema": 2, "skill_id": SKILL, "user_id": "USER", "setup_token": "FAKE-SETUP"})}
            text = ("Cloud authentication renewal succeeded." if "renew" in utterance else "Setup is saved.")
            reply = {"status": "SUCCESSFUL", "result": {"skillExecutionInfo": {"invocations": [
                {"invocationResponse": {"body": {"response": {"outputSpeech": {"text": text},
                                                              **({"card": card} if card else {})}}}}]}}}
        return SimpleNamespace(returncode=0, stdout="[Warn]: async\n" + json.dumps(reply), stderr="")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = patch.dict(os.environ, {"BRIDGE_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.out = io.StringIO()
        redirect = contextlib.redirect_stdout(self.out)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.state = core.load_state()

    def events(self):
        return [json.loads(line) for line in self.out.getvalue().splitlines() if line.startswith("{")]

    def assertNoSecrets(self):
        text = self.out.getvalue() + (Path(self.tmp.name) / "state.json").read_text()
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def chosen(self, **extra):
        core.choose(self.state, "nova a. i.", "gb", **{"agent": "Grokbot", **extra})
        self.state.update(profile="alexa-bridge", vendor_id="VENDOR")
        return self.state

    def test_choose_validates_and_derives_defaults(self):
        self.chosen()
        self.assertEqual(self.state["choices"]["display_name"], "Nova AI")
        self.assertEqual((self.state["choices"]["country"], self.state["choices"]["locale"]), ("GB", "en-GB"))
        for args, category in ((("nova", "GB"), "invocation_rejected"), (("kitchen helper", "XX"), "country_unknown"),
                               (("kitchen helper", "CA"), "choose_language")):
            with self.assertRaises(core.Stop) as stop:
                core.choose(self.state, *args)
            self.assertEqual(stop.exception.category, category)
        core.choose(self.state, "kitchen helper", "CA", locale="fr-CA")
        self.assertEqual(self.state["choices"]["locale"], "fr-CA")

    def test_choose_records_the_agent_name_and_keeps_it_on_rerun(self):
        self.chosen(agent=" Grokbot ")
        core.choose(self.state, "nova a. i.", "gb", test_echo="Kitchen")
        self.assertEqual(self.state["choices"]["agent_name"], "Grokbot")
        self.state["skill_id"] = SKILL
        self.assertEqual(core.render_config(self.state)["agent_name"], "Grokbot")
        with self.assertRaises(core.Stop) as stop:
            core.choose(self.state, "nova a. i.", "gb", agent="{name}")
        self.assertEqual(stop.exception.category, "agent_name_invalid")
        fresh = {}
        with self.assertRaises(core.Stop) as stop:
            core.choose(fresh, "nova a. i.", "gb")
        self.assertEqual(stop.exception.category, "choose_agent")
        self.assertNotIn("choices", fresh)

    def test_state_records_the_checkout_for_the_maintenance_skill(self):
        core.status(self.chosen())
        saved = json.loads((Path(self.tmp.name) / "state.json").read_text())
        self.assertEqual(saved["checkout"], str(core.ROOT))
        self.assertEqual(self.events()[-1]["checkout"], str(core.ROOT))

    def test_skills_follow_the_agent_skills_format_and_point_into_this_release(self):
        skills = sorted((core.ROOT / "skills").glob("*/SKILL.md"))
        self.assertEqual([p.parent.name for p in skills], ["alexa-bridge-maintenance", "alexa-bridge-setup"])
        for path in skills:
            text = path.read_text(encoding="utf-8")
            head, body = re.match(r"---\r?\n(.*?)\r?\n---\r?\n(.*)", text, re.S).groups()
            fields = dict(line.split(": ", 1) for line in head.splitlines())
            self.assertEqual(set(fields), {"name", "description"})
            self.assertEqual(fields["name"], path.parent.name)
            self.assertRegex(fields["name"], r"^[a-z0-9]+(-[a-z0-9]+)*$")
            self.assertTrue(0 < len(fields["description"]) <= 1024)
            self.assertNotIn("http", body)
            for name in re.findall(r"`(?:<work>/)?([A-Z-]+\.md)`", body):
                self.assertTrue((core.ROOT / name).exists(), name)

    def test_sign_in_address_can_come_from_a_hidden_prompt_or_a_file(self):
        url = "https://www.amazon.com/ap/maplanding?openid.oa2.authorization_code=FAKE-CODE"
        self.assertEqual(core.read_redirect("prompt", lambda _: " " + url + " "), url)
        path = Path(self.tmp.name) / "redirect.txt"
        path.write_text(url)
        self.assertEqual(core.read_redirect(str(path)), url)
        self.assertFalse(path.exists())
        self.assertNotIn("FAKE-CODE", self.out.getvalue())

    def test_choose_rerun_keeps_options_for_the_same_skill(self):
        core.choose(self.state, "kitchen helper", "CA", locale="fr-CA", display_name="Kitchen Pal", agent="Grokbot")
        core.choose(self.state, "kitchen helper", "CA", test_echo="Kitchen")
        choices = self.state["choices"]
        self.assertEqual((choices["locale"], choices["display_name"], choices["agent_name"], choices["test_echo"]),
                         ("fr-CA", "Kitchen Pal", "Grokbot", "Kitchen"))
        core.choose(self.state, "kitchen helper", "CA")
        self.assertEqual(self.state["choices"]["test_echo"], "Kitchen")
        core.choose(self.state, "pantry helper", "CA", locale="en-CA")
        self.assertEqual(self.state["choices"]["display_name"], "Pantry Helper")
        self.assertIsNone(self.state["choices"]["test_echo"])

    def test_dev_auth_takes_a_vendor_when_the_account_has_several(self):
        class TwoVendors(FakeAsk):
            def __call__(self, args, **kwargs):
                if args[1:3] == ["smapi", "get-vendor-list"]:
                    return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
                        {"vendors": [{"id": "VENDOR-A", "name": "Home"}, {"id": "VENDOR-B", "name": "Work"}]}))
                return super().__call__(args, **kwargs)
        with patch.object(core, "ensure_vendor_in_profile"):
            with self.assertRaises(core.Stop) as stop:
                core.dev_auth(self.state, runner=TwoVendors())
            self.assertEqual(stop.exception.category, "choose_vendor")
            self.assertIn("VENDOR-B", stop.exception.message)
            with self.assertRaises(core.Stop) as stop:
                core.dev_auth(self.state, runner=TwoVendors(), vendor="VENDOR-C")
            self.assertEqual(stop.exception.category, "vendor_unknown")
            core.dev_auth(self.state, runner=TwoVendors(), vendor="VENDOR-B")
        self.assertEqual(self.state["vendor_id"], "VENDOR-B")
        self.assertIn("dev_auth", self.state["phases"])

    def test_doctor_keeps_the_agents_country_notes(self):
        self.chosen()
        notes = Path(self.tmp.name) / "country-notes.json"
        notes.write_text(json.dumps({"GB": {"notes": ["Consent button was labelled Allow"]}}))
        core.doctor(self.state, fetch=lambda url: 302 if "alexa.amazon" in url else 404)
        record = json.loads(notes.read_text())["GB"]
        self.assertEqual(record["notes"], ["Consent button was labelled Allow"])
        self.assertTrue(all(record["ok"].values()))

    def test_renewal_failure_still_records_that_enrolment_was_sent(self):
        state = self.sign_in()
        state["skill_id"] = SKILL
        original = core.simulate
        def simulate(ask, skill_id, locale, utterance, sleep):
            speech, card = original(ask, skill_id, locale, utterance, sleep)
            return ("Renewal is not ready yet." if "renew" in utterance else speech), card
        sender = lambda method, url, *a: (200, b'{"access_token":"FAKE-TOKEN"}') if "auth/o2/token" in url else (202, b"")
        with patch.dict(os.environ, {"BRIDGE_WEBHOOK_URL": "https://example.test/hook", "BRIDGE_WEBHOOK_KEY": "FAKE-KEY"}), \
                patch.object(core, "simulate", simulate):
            with self.assertRaises(core.Stop) as stop:
                core.enrol(state, FakeAsk(), sleep=lambda s: None, sender=sender)
        self.assertEqual(stop.exception.category, "renewal_failed")
        self.assertEqual(state["phases"]["enrol"]["renewal"], "pending")
        core.status(state)
        self.assertNotEqual(self.events()[-1]["next"], "enrol")
        self.assertNoSecrets()

    def test_update_verifies_the_tag_against_the_installed_signers_before_checkout(self):
        self.chosen()
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            bad = "verify-tag" in args and args[-1] == "v9.9.9"
            return SimpleNamespace(returncode=1 if bad else 0, stdout="", stderr="")
        with patch.object(core, "deploy") as deploy:
            with self.assertRaises(core.Stop) as stop:
                core.update(self.state, "v9.9.9", runner=runner)
            self.assertEqual(stop.exception.category, "update_refused")
            self.assertFalse(any("checkout" in c for c in calls))
            deploy.assert_not_called()
            calls.clear()
            core.update(self.state, "v1.0.0", runner=runner)
            deploy.assert_called_once()
        verify = next(c for c in calls if "verify-tag" in c)
        self.assertIn("gpg.ssh.allowedSignersFile=" + core.SIGNERS.as_posix(), verify)
        self.assertLess(calls.index(verify), next(i for i, c in enumerate(calls) if "checkout" in c))
        self.assertTrue(core.SIGNERS.read_text().startswith("alexa-agent-bridge-release namespaces=\"git\" ssh-ed25519 "))

    def test_status_names_the_next_step(self):
        core.status(self.chosen())
        self.assertEqual(self.events()[-1]["next"], "preflight")

    def test_create_stops_for_first_time_captcha(self):
        with self.assertRaises(core.Stop) as stop:
            core.create(self.chosen(), FakeAsk(permission="NEW_USER_REGISTRATION_REQUIRED"), sleep=lambda s: None)
        self.assertEqual(stop.exception.category, "captcha")
        self.assertIn("example.test%2Fcaptcha%3Fvendor_id%3DVENDOR", stop.exception.message)
        self.assertNotIn("skill_id", self.state)

    def test_create_sends_country_region_and_is_resumable(self):
        ask = FakeAsk()
        core.create(self.chosen(), ask, sleep=lambda s: None)
        body = ask.bodies["create-skill-for-vendor"]
        self.assertEqual(body["hosting"]["alexaHosted"], {"runtime": "PYTHON_3_8", "region": "EU_WEST_1"})
        self.assertEqual(body["manifest"]["publishingInformation"]["locales"], {"en-GB": {"name": "Nova AI"}})
        self.assertEqual((body["vendorId"], self.state["skill_id"]), ("VENDOR", SKILL))
        self.assertFalse(list((Path(self.tmp.name) / "private").iterdir()))
        again = FakeAsk()
        core.create(self.state, again, sleep=lambda s: None)
        self.assertNotIn("create-skill-for-vendor", [c[2] for c in again.calls])

    def test_staging_is_idempotent_and_retires_old_files(self):
        state = self.chosen(test_echo="Office Echo")
        state["skill_id"] = SKILL
        with tempfile.TemporaryDirectory() as work:
            lambda_dir = Path(work) / "lambda"
            lambda_dir.mkdir()
            (lambda_dir / "carriers.json").write_text("{}")
            (lambda_dir / "utils.py").write_text("# template")
            self.assertTrue(core.stage_lambda(work, state))
            self.assertFalse(core.stage_lambda(work, state))
            config = json.loads((lambda_dir / "bridge_config.json").read_text())
            self.assertEqual((config["skill_id"], config["test_device_name"]), (SKILL, "Office Echo"))
            self.assertFalse((lambda_dir / "carriers.json").exists())
            self.assertTrue((lambda_dir / "utils.py").exists())
            self.assertTrue((lambda_dir / "lang_en.json").exists())

    def test_credentials_are_unlocked_by_manifest_toggle(self):
        ask = FakeAsk()
        self.chosen()
        client, secret = core.messaging_credentials(core.Ask("alexa-bridge", ask), SKILL, sleep=lambda s: None)
        self.assertEqual((client, secret), ("CLIENT", "FAKE-SECRET"))
        updates = [c for c in ask.calls if c[2] == "update-skill-manifest"]
        self.assertEqual(len(updates), 2)
        self.assertNotIn("FAKE-SECRET", self.out.getvalue())

    def sign_in(self, home_region="EU"):
        core.signin_start(self.chosen())
        url = self.events()[-1]["url"]
        self.assertTrue(url.startswith("https://www.amazon.com/ap/signin?"))
        redirect = Path(self.tmp.name) / "redirect.txt"
        redirect.write_text("https://www.amazon.com/ap/maplanding?openid.oa2.authorization_code=FAKE-CODE")
        def transport(method, url, headers=None, data=None, **kwargs):
            if url.endswith("/auth/register"):
                self.assertIn(b"FAKE-CODE", data)
                return 200, [], json.dumps({"response": {"success": {
                    "tokens": {"bearer": {"refresh_token": "FAKE-REFRESH"}},
                    "extensions": {"customer_info": {"home_region": home_region}}}}}).encode()
            if url.endswith("/auth/token"):
                return 200, [], json.dumps({"response": {"tokens": {"cookies": {
                    ".amazon.co.uk": [{"Name": "csrf", "Value": "C"}]}}}}).encode()
            if url.endswith("/api/users/me"):
                self.assertTrue(url.startswith("https://alexa.amazon.co.uk/"))
                return 200, [], b'{"id":"CUSTOMER"}'
            self.fail("unexpected " + url)
        renewed = lambda seed, transport: {**seed, "devices": [{"name": "Office Echo"}, {"name": "Kitchen Echo"}]}
        core.signin_finish(self.state, str(redirect), transport, renewed)
        self.assertFalse(redirect.exists())
        return self.state

    def test_signin_builds_a_country_seed_privately(self):
        state = self.sign_in()
        seed = core.read_private("seed.json")
        self.assertEqual((seed["domain"], seed["customer_id"], seed["locale"]), ("amazon.co.uk", "CUSTOMER", "en-GB"))
        self.assertEqual(state["echoes"], ["Kitchen Echo", "Office Echo"])
        self.assertIsNone(core.read_private("pkce.json"))
        self.assertNoSecrets()

    def test_enrol_sends_to_the_country_region_and_deletes_the_seed(self):
        state = self.sign_in()
        state["skill_id"] = SKILL
        sent = []
        def sender(method, url, headers, data, hosts):
            sent.append((url, json.loads(data) if url.endswith("/token") is False else data))
            return (200, b'{"access_token":"FAKE-TOKEN"}') if "auth/o2/token" in url else (202, b"")
        with patch.dict(os.environ, {"BRIDGE_WEBHOOK_URL": "https://example.test/hook",
                                     "BRIDGE_WEBHOOK_KEY": "FAKE-KEY"}):
            core.enrol(state, FakeAsk(), sleep=lambda s: None, sender=sender)
        url, body = sent[-1]
        self.assertTrue(url.startswith("https://api.eu.amazonalexa.com/v1/skillmessages/users/USER"))
        payload = json.loads(body["data"]["payload"])
        self.assertEqual((payload["webhook_url"], payload["test_device_name"]), ("https://example.test/hook", "Kitchen Echo"))
        self.assertEqual(payload["registration"]["refresh_token"], "FAKE-REFRESH")
        self.assertIsNone(core.read_private("seed.json"))
        self.assertIn("enrol", state["phases"])
        self.assertNoSecrets()

    def test_enrol_requires_routine_secrets(self):
        state = self.sign_in()
        state["skill_id"] = SKILL
        with patch.dict(os.environ, {"BRIDGE_WEBHOOK_URL": "http://insecure.test", "BRIDGE_WEBHOOK_KEY": "x"}):
            with self.assertRaises(core.Stop) as stop:
                core.enrol(state, FakeAsk(), sleep=lambda s: None)
        self.assertEqual(stop.exception.category, "webhook_missing")
        self.assertIsNotNone(core.read_private("seed.json"))

    def test_webhook_set_keeps_values_private(self):
        answers = iter(["https://example.test/hook", "FAKE-KEY"])
        core.webhook_set(lambda _: next(answers))
        self.assertEqual(core.read_private("webhook.json")["key"], "FAKE-KEY")
        self.assertEqual(self.events()[-1]["host"], "example.test")
        self.assertNotIn("FAKE-KEY", self.out.getvalue())

    def test_english_pack_passes_pack_check(self):
        core.pack_check("en")
        self.assertEqual(self.events()[-1]["problems"], [])
        with self.assertRaises(core.Stop):
            core.pack_check("xx")

    def test_pack_check_requires_the_agent_placeholder(self):
        root = Path(self.tmp.name) / "src"
        (root / "lambda").mkdir(parents=True); (root / "models").mkdir()
        pack = json.loads((core.LAMBDA / "lang_en.json").read_text(encoding="utf-8"))
        pack["strings"]["dispatch_failed"] = "Your request was not sent. Please try again."
        (root / "lambda" / "lang_en.json").write_text((core.LAMBDA / "lang_en.json").read_text(encoding="utf-8"), encoding="utf-8")
        (root / "lambda" / "lang_xx.json").write_text(json.dumps(pack), encoding="utf-8")
        (root / "models" / "xx.json").write_text((core.ROOT / "models" / "en.json").read_text(encoding="utf-8"), encoding="utf-8")
        with patch.object(core, "ROOT", root), patch.object(core, "LAMBDA", root / "lambda"):
            with self.assertRaises(core.Stop):
                core.pack_check("xx")
        self.assertIn("dispatch_failed", " ".join(self.events()[-1]["problems"]))

    def test_routine_text_lists_every_region(self):
        core.routine_text(self.chosen())
        text = self.out.getvalue()
        for host in ("api.amazonalexa.com", "api.eu.amazonalexa.com", "api.fe.amazonalexa.com"):
            self.assertIn(host, text)
        self.assertIn("Nova AI", text)
        self.assertIn('{"data": <the copied data plus answer>, "expiresAfterSeconds": 120}', text)

    def test_doctor_stops_before_anything_is_created(self):
        self.chosen()
        core.doctor(self.state, fetch=lambda url: 302 if "alexa.amazon" in url else 404)
        with self.assertRaises(core.Stop):
            core.doctor(self.state, fetch=lambda url: None)
        notes = json.loads((Path(self.tmp.name) / "country-notes.json").read_text())
        self.assertFalse(all(notes["GB"]["ok"].values()))

    def test_cli_reports_stops_with_exit_code_two(self):
        from bridge.__main__ import main
        self.assertEqual(main(["choose", "--name", "alexa", "--country", "GB"]), 2)
        self.assertEqual(self.events()[-1]["category"], "invocation_rejected")


if __name__ == "__main__":
    unittest.main()
