"""Announcements: the agent speaks on chosen Echos with no spoken request. No network or cloud resources."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambda"))
import retail
from storage import Sessions
from lambda_function import App, CONFIG, digest
from test_skill import Memory

KEY = "FAKE-announce-key"


def session():
    devices = [{"serial": "office", "type": "echo", "name": "Office Echo", "online": True},
               {"serial": "kitchen", "type": "show", "name": "Kitchen Echo Show", "online": True},
               {"serial": "garage", "type": "dot", "name": "Garage Echo Dot", "online": False}]
    return {"schema": 1, "domain": "amazon.co.uk", "customer_id": "customer", "refresh_token": "FAKE-refresh",
            "cookies": {"csrf": "FAKE-csrf"}, "cookies_refreshed_at": 1000, "devices_refreshed_at": 1500,
            "devices": devices}


def nodes(payload):
    return json.loads(payload["sequenceJson"])["startNode"]["nodesToExecute"]


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.office, self.kitchen = session()["devices"][:2]

    def test_announce_is_one_text_node_for_every_target_with_a_long_expiry(self):
        payload = retail.announcement_payload(session(), [self.office, self.kitchen], "Bins tonight & tomorrow",
                                              title="Nova AI")
        [node] = nodes(payload)
        operation = node["operationPayload"]
        self.assertEqual(node["type"], "AlexaAnnouncement")
        self.assertEqual(operation["expireAfter"], "PT60S")
        self.assertEqual(operation["content"][0]["speak"], {"type": "text", "value": "Bins tonight & tomorrow"})
        self.assertEqual(operation["content"][0]["display"]["title"], "Nova AI")
        self.assertEqual([d["deviceSerialNumber"] for d in operation["target"]["devices"]], ["office", "kitchen"])

    def test_a_whispered_announcement_is_ssml_after_a_chime_and_markup_stays_text(self):
        payload = retail.announcement_payload(session(), [self.office], '<audio src="x"/> bedtime', whisper=True)
        sound, announcement = nodes(payload)
        self.assertEqual((sound["type"], sound["operationPayload"]["soundStringId"]), ("Alexa.Sound", retail.CHIME))
        speak = announcement["operationPayload"]["content"][0]["speak"]
        self.assertEqual(speak["type"], "ssml")
        self.assertIn('<amazon:effect name="whispered">&lt;audio', speak["value"])
        self.assertNotIn("<audio", speak["value"])

    def test_speak_is_one_node_per_echo_in_parallel_and_can_whisper(self):
        [parallel] = nodes(retail.announcement_payload(session(), [self.office, self.kitchen], "Hi", style="speak",
                                                       whisper=True))
        self.assertTrue(parallel["@type"].endswith("ParallelNode"))
        for node in parallel["nodesToExecute"]:
            self.assertEqual(node["type"], "Alexa.Speak")
            self.assertEqual(len(node["operationPayload"]["target"]["devices"]), 1)
            self.assertTrue(node["operationPayload"]["textToSpeak"].startswith("<speak>"))

    def test_volume_changes_only_where_the_level_is_known_and_is_put_back(self):
        levels = {("office", "echo"): 25}
        first, speech, last = nodes(retail.announcement_payload(
            session(), [self.office, self.kitchen], "Hi", volume=50, volumes=levels))
        self.assertEqual((first["type"], first["operationPayload"]["value"]), ("Alexa.DeviceControls.Volume", 50))
        self.assertEqual((last["operationPayload"]["deviceSerialNumber"], last["operationPayload"]["value"]),
                         ("office", 25))
        self.assertEqual(speech["type"], "AlexaAnnouncement")
        self.assertEqual(len(nodes(retail.announcement_payload(session(), [self.office], "Hi", volume=50,
                                                               volumes={}))), 1)

    def test_unknown_offline_duplicate_or_bad_text_never_builds(self):
        garage = session()["devices"][2]
        for targets, text in (([{"serial": "x", "type": "echo"}], "Hi"), ([garage], "Hi"),
                              ([self.office, self.office], "Hi"), ([self.office], "x" * 4001),
                              ([self.office], "bad\x00"), ([], "Hi")):
            with self.subTest(text=text[:5]), self.assertRaises(ValueError):
                retail.announcement_payload(session(), targets, text)

    def test_one_post_and_429_is_throttled(self):
        for status, error in ((429, retail.Throttled), (407, retail.AuthRequired), (500, retail.UnconfirmedSpeech)):
            calls = []
            with self.subTest(status=status), self.assertRaises(error):
                retail.announce_once(session(), [self.office], "Hi", lambda *a: calls.append(a) or (status, [], b""))
            self.assertEqual(len(calls), 1)

    def test_device_state_reads(self):
        def transport(method, url, headers=None, data=None, **kwargs):
            if "dnd" in url:
                return 200, [], b'{"doNotDisturbDeviceStatusList":[{"deviceSerialNumber":"office",' \
                                b'"deviceType":"echo","enabled":true}]}'
            if "allDeviceVolumes" in url:
                return 200, [], b'{"volumes":[{"dsn":"kitchen","deviceType":"show","speakerVolume":43}]}'
            states = [{"devicePreference": "communications", "state": "ON"},
                      {"devicePreference": "announcements", "state": "OFF" if "office" in url else "ON"}]
            self.assertTrue(url.startswith("https://" + retail.COMMS_HOST + "/devicesTypes/"))
            return 200, [], json.dumps({"devicePermissionsPreferences": states}).encode()
        self.assertEqual(retail.dnd_states(session(), transport), {("office", "echo"): True})
        self.assertEqual(retail.device_volumes(session(), transport), {("kitchen", "show"): 43})
        self.assertIs(retail.announcements_allowed(session(), self.office, transport), False)
        self.assertIs(retail.announcements_allowed(session(), self.kitchen, transport), True)
        self.assertIsNone(retail.announcements_allowed(session(), self.office, lambda *a: (503, [], b"<html>")))
        self.assertIsNone(retail.announcements_allowed(session(), {"serial": "../x", "type": "echo"}, transport))


class AnnounceTests(unittest.TestCase):
    def setUp(self):
        self.store = Memory()
        self.store.put("configuration", {"owner_id": "owner", "seed": session(), "test_device_name": "Office Echo",
                                        "announce": {"enabled": True, "echoes": [], "key_hash": digest(KEY)}})
        self.store.put("retail-session", session())
        self.sent, self.asleep, self.blocked, self.levels = [], {}, set(), {}
        sessions = Sessions(self.store, lambda: 1500, discoverer=lambda state, now: {**state, "devices_refreshed_at": now})
        self.app = App(self.store, sessions=sessions, clock=lambda: 1500,
                       speaker=lambda *a: self.fail("answer path used"),
                       announcer=lambda session, targets, text, **o: self.sent.append(
                           ([t["name"] for t in targets], text, o)) or "accepted_by_amazon",
                       dnd=lambda session: dict(self.asleep),
                       allowed=lambda session, d: d["serial"] not in self.blocked,
                       volumes=lambda session: dict(self.levels))
        self.count = 0

    def announce(self, names, text="It's bedtime.", user="owner", **fields):
        self.count += 1
        data = {"kind": "announce", "announce_id": fields.pop("announce_id", "announce-%04d" % self.count),
                "announce_key": KEY, "targets": json.dumps(names), "text": text, **fields}
        self.app.handle({"context": {"System": {"user": {"userId": user}}},
                         "request": {"type": "Messaging.MessageReceived", "message": data}})
        return data

    def last(self):
        return (self.store.get("announcements")[0] or {"recent": [{}]})["recent"][-1]

    def test_announces_once_on_the_named_echos(self):
        data = self.announce(["office", "Kitchen Echo Show"])
        self.assertEqual(self.sent, [(["Office Echo", "Kitchen Echo Show"], "It's bedtime.",
                                      {"style": "announce", "whisper": False, "title": CONFIG["display_name"],
                                       "volume": None, "volumes": None})])
        self.assertEqual(self.last()["state"], "accepted_by_amazon")
        self.assertEqual(self.last()["echoes"], {"Office Echo": "accepted_by_amazon",
                                                 "Kitchen Echo Show": "accepted_by_amazon"})
        self.assertNotIn("It's bedtime", json.dumps(self.store.rows))  # receipts carry no text
        reads = []
        self.app.dnd = lambda session: reads.append(1) or {}
        self.app.handle({"context": {"System": {"user": {"userId": "owner"}}},
                         "request": {"type": "Messaging.MessageReceived", "message": data}})
        self.assertEqual(len(self.sent), 1)  # a redelivered message never speaks twice
        self.assertEqual(reads, [])  # and is settled from its receipt, without reading the Echos again

    def test_wrong_owner_key_or_missing_key_never_speaks(self):
        self.announce(["office"], user="other")
        self.announce(["office"], announce_key="WRONG")
        config, revision = self.store.get("configuration")
        self.store.put("configuration", {**config, "announce": {"enabled": True}}, revision)
        self.announce(["office"])
        self.assertEqual(self.sent, [])
        self.assertIsNone(self.store.get("announcements")[0])

    def test_off_unknown_ambiguous_or_restricted_names_refuse_the_whole_message(self):
        self.announce(["office", "garage"])
        self.assertEqual((self.last()["state"], self.last()["reason"]), ("refused", "unknown_echo"))
        config, revision = self.store.get("configuration")
        self.store.put("configuration", {**config, "announce": {**config["announce"], "echoes": ["Kitchen Echo Show"]}},
                       revision)
        self.announce(["office"])
        self.assertEqual(self.last()["reason"], "unknown_echo")
        self.announce("all")
        self.assertEqual(self.sent[-1][0], ["Kitchen Echo Show"])
        config, revision = self.store.get("configuration")
        self.store.put("configuration", {**config, "announce": {**config["announce"], "enabled": False}}, revision)
        self.announce(["kitchen echo show"])
        self.assertEqual((self.last()["state"], self.last()["reason"]), ("refused", "off"))
        self.assertEqual(len(self.sent), 1)

    def test_invalid_fields_are_refused(self):
        for fields in ({"text": "x" * 4001}, {"text": "bad\x00"}, {"style": "shout"}, {"volume": "101"},
                       {"volume": "loud"}, {"targets": "office"}, {"targets": json.dumps([])}):
            self.announce(["office"], **fields)
            self.assertEqual(self.last()["reason"], "invalid")
        self.announce(["office"], announce_id="short")
        self.assertEqual(self.sent, [])

    def test_do_not_disturb_offline_and_announcements_off_are_skipped_and_recorded(self):
        self.asleep = {("office", "echo"): True}
        self.announce(["office", "kitchen echo show", "garage echo dot"])
        self.assertEqual(self.sent[-1][0], ["Kitchen Echo Show"])
        self.assertEqual(self.last()["echoes"], {"Office Echo": "do_not_disturb", "Garage Echo Dot": "offline",
                                                 "Kitchen Echo Show": "accepted_by_amazon"})
        self.asleep, self.blocked = {}, {"kitchen"}
        self.announce(["Kitchen Echo Show"])
        self.assertEqual((self.last()["state"], self.last()["echoes"]), ("skipped", {"Kitchen Echo Show": "announcements_off"}))
        self.announce(["Kitchen Echo Show"], style="speak")  # Speak isn't silenced by that setting
        self.assertEqual(self.sent[-1][0], ["Kitchen Echo Show"])

    def test_speak_is_not_sent_when_do_not_disturb_cannot_be_read(self):
        def unreadable(session):
            raise retail.TransportError()
        self.app.dnd = unreadable
        self.announce(["office"], style="speak")
        self.assertEqual(self.last()["echoes"], {"Office Echo": "do_not_disturb_unknown"})
        self.announce(["office"])  # Amazon itself holds an announcement for Do Not Disturb
        self.assertEqual(len(self.sent), 1)

    def test_volume_and_whisper_reach_the_payload(self):
        self.levels = {("office", "echo"): 25}
        self.announce(["office"], volume="50", whisper="1", style="speak")
        options = self.sent[-1][2]
        self.assertEqual((options["volume"], options["volumes"], options["whisper"], options["style"]),
                         (50, {("office", "echo"): 25}, True, "speak"))

    def test_failed_throttled_or_rejected_sends_are_recorded_and_never_replayed(self):
        for error, state in ((retail.UnconfirmedSpeech(), "unconfirmed"), (retail.Throttled(), "throttled"),
                             (retail.AuthRequired(), "auth_required")):
            calls = []
            def announcer(*args, error=error, **kwargs):
                calls.append(args)
                raise error
            self.app.announcer = announcer
            data = self.announce(["office"])
            self.app.handle({"context": {"System": {"user": {"userId": "owner"}}},
                             "request": {"type": "Messaging.MessageReceived", "message": data}})
            self.assertEqual((len(calls), self.last()["state"]), (1, state))

    def test_status_card_carries_the_latest_receipt(self):
        self.announce(["office"])
        card = json.loads(self.app.status("device:x")["response"]["card"]["content"])
        self.assertEqual((card["announcement"]["state"], card["announcement"]["echoes"]),
                         ("accepted_by_amazon", {"Office Echo": "accepted_by_amazon"}))
        self.assertEqual(card["announcements"], {"enabled": True, "echoes": [], "key": True})
        self.assertNotIn(digest(KEY), json.dumps(card))  # the settings, never the key's hash

    def test_receipts_are_bounded(self):
        for _ in range(25):
            self.announce(["office"])
        self.assertEqual(len(self.store.get("announcements")[0]["recent"]), 20)


class AnnounceConfigTests(unittest.TestCase):
    def setUp(self):
        self.store = Memory()
        self.store.put("configuration", {"owner_id": "owner", "seed": session(), "test_device_name": "Office Echo"})
        self.app = App(self.store, clock=lambda: 1500)

    def change(self, payload, user="owner", token=None):
        token = token or json.loads(self.app.setup_card(user)["response"]["card"]["content"])["setup_token"]
        self.app.handle({"context": {"System": {"user": {"userId": user}}},
                         "request": {"type": "Messaging.MessageReceived", "message": {
                             "kind": "announce_config", "setup_token": token, "payload": json.dumps(payload)}}})
        return token

    def settings(self):
        return self.store.get("configuration")[0].get("announce")

    def test_key_hand_over_turns_announcements_on_and_changes_are_one_use_and_owner_bound(self):
        token = self.change({"key_hash": digest(KEY)})
        self.assertEqual(self.settings(), {"enabled": True, "echoes": [], "key_hash": digest(KEY)})
        self.change({"enabled": False}, token=token)  # used up
        self.change({"enabled": False}, user="other")
        self.assertTrue(self.settings()["enabled"])
        self.change({"enabled": False, "echoes": ["Office Echo"]})
        self.assertEqual(self.settings(), {"enabled": False, "echoes": ["Office Echo"], "key_hash": digest(KEY)})
        for bad in ({"key_hash": "short"}, {"enabled": "no"}, {"echoes": "Office"}, {"echoes": [""]}):
            self.change(bad)
        self.assertEqual(self.settings(), {"enabled": False, "echoes": ["Office Echo"], "key_hash": digest(KEY)})

    def test_enrolment_sets_the_key_and_a_repair_keeps_the_owners_choices(self):
        def enrol(**extra):
            card = json.loads(self.app.setup_card("owner")["response"]["card"]["content"])
            self.app.enroll("owner", {"setup_token": card["setup_token"], "payload": json.dumps({
                "registration": session(), "client_id": "FAKE-ID", "client_secret": "FAKE-secret", **extra})})
        enrol(announce_key_hash=digest(KEY))
        self.assertEqual(self.settings(), {"enabled": True, "echoes": [], "key_hash": digest(KEY)})
        self.change({"enabled": False})
        enrol(announce_key_hash=digest("FAKE-newer"))
        self.assertEqual(self.settings(), {"enabled": False, "echoes": [], "key_hash": digest("FAKE-newer")})
        enrol()  # an older setup tool: the announcement settings stay
        self.assertEqual(self.settings()["key_hash"], digest("FAKE-newer"))


if __name__ == "__main__":
    unittest.main()
