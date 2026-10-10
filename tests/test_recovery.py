"""Failure/recovery regressions using fake registration, clocks and transports."""
import copy
import unittest
from unittest.mock import patch

import test_skill as fixtures
from storage import Sessions, Busy, Conflict
import retail
import lambda_function


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.t = fixtures.AppTests()
        self.t.setUp()
        self.app, self.store = self.t.app, self.t.store

    def renew(self, state, now):
        return {**state, "cookies": {"csrf": "NEW"}, "cookies_refreshed_at": now,
                "devices_refreshed_at": now}

    def test_auth_rejection_renews_next_job_without_replaying_rejected_job(self):
        data = self.t.pending()
        calls = []
        def reject(*args):
            calls.append(args)
            raise retail.AuthRequired()
        self.app.speaker = reject
        self.app.handle(self.t.message(data))
        self.assertEqual(self.store.get(self.t.key)[0]["pending"]["state"], "auth_required")
        self.assertEqual(self.store.get("retail-session")[0]["auth_state"], "stale")
        self.app.handle(self.t.message(data))
        self.assertEqual(len(calls), 1)
        self.app.sessions.renewer = self.renew
        self.app.speaker = lambda *args: calls.append(args) or "accepted_by_amazon"
        row, rev = self.store.get(self.t.key)
        row["pending"] = {"id": "next", "token": "NEXT", "expires": 1600,
                          "state": "pending", "target": fixtures.session()["devices"][0]}
        self.store.put(self.t.key, row, rev)
        self.app.handle(self.t.message({**data, "request_id": "next", "job_token": "NEXT"}))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1][0]["cookies"]["csrf"], "NEW")
        self.assertEqual(self.store.get("retail-session")[0]["auth_state"], "ready")

    def test_late_auth_failure_cannot_invalidate_concurrent_renewal_or_enrollment(self):
        for field in ("auth_generation", "enrollment_id"):
            old, rev = self.store.get("retail-session")
            self.store.put("retail-session", {**old, field: "new-generation"}, rev)
            self.assertFalse(self.app.sessions.invalidate(old))
            self.assertNotIn("auth_state", self.store.get("retail-session")[0])

    def test_invalidation_rechecks_generation_after_write_conflict(self):
        old, _ = self.store.get("retail-session")
        original_put = self.store.put
        def raced_put(key, value, expected=None):
            self.store.put = original_put
            original_put(key, {**old, "auth_generation": "new"}, expected)
            raise Conflict()
        self.store.put = raced_put
        self.assertFalse(self.app.sessions.invalidate(old))
        self.assertEqual(self.store.get("retail-session")[0]["auth_generation"], "new")

    def test_revoked_registration_is_reported_without_repeated_renewal(self):
        data = self.t.pending()
        old, rev = self.store.get("retail-session")
        self.store.put("retail-session", {**old, "auth_state": "stale"}, rev)
        calls = []
        def revoked(*args, **kwargs):
            calls.append(1)
            raise retail.RegistrationRejected()
        self.app.sessions.renewer = revoked
        self.app.handle(self.t.message(data))
        self.assertEqual(self.store.get(self.t.key)[0]["pending"]["state"], "auth_required")
        with self.assertRaises(retail.AuthRequired):
            self.app.sessions.ready()
        self.assertEqual(len(calls), 1)
        self.assertIn("sign-in needs attention", self.app.status(self.t.key)["response"]["outputSpeech"]["text"])
        self.app.sessions.renewer = self.renew
        self.assertEqual(self.app.sessions.ready(force=True)["auth_state"], "ready")

    def test_offline_retry_then_online_delivers_once(self):
        data = self.t.pending()
        old, rev = self.store.get("retail-session")
        old["devices"][0]["online"] = False
        self.store.put("retail-session", old, rev)
        with self.assertRaises(retail.TargetUnavailable):
            self.app.handle(self.t.message(data))
        pending = self.store.get(self.t.key)[0]["pending"]
        self.assertEqual(pending["state"], "pending")
        self.assertEqual(pending["last_error"], "target_offline")
        self.assertEqual(self.t.sent, [])
        def discover(state, now):
            state["devices"][0]["online"] = True
            return {**state, "devices_refreshed_at": now}
        self.app.sessions.discoverer = discover
        self.app.handle(self.t.message(data))
        self.app.handle(self.t.message(data))
        self.assertEqual(len(self.t.sent), 1)

    def test_missing_target_never_falls_back_to_another_echo(self):
        data = self.t.pending()
        old, rev = self.store.get("retail-session")
        old["devices"] = [{**old["devices"][0], "serial": "bedroom"}]
        self.store.put("retail-session", old, rev)
        self.app.handle(self.t.message(data))
        self.assertEqual(self.store.get(self.t.key)[0]["pending"]["state"], "target_unavailable")
        self.assertEqual(self.t.sent, [])

    def test_busy_preclaim_can_be_redelivered_but_expired_job_cannot(self):
        data = self.t.pending()
        with patch.object(self.app.sessions, "ready", side_effect=Busy):
            with self.assertRaises(Busy):
                self.app.handle(self.t.message(data))
        job = self.store.get(self.t.key)[0]["pending"]
        self.assertEqual(job["state"], "pending")
        self.assertEqual(job["last_error"], "temporary_delivery_failure")
        self.app.clock = lambda: 1601
        self.app.handle(self.t.message(data))
        self.assertEqual(self.t.sent, [])
        self.assertIn("expired", self.app.status(self.t.key)["response"]["outputSpeech"]["text"])

    def test_transient_failure_redelivery_succeeds_once(self):
        data = self.t.pending()
        with patch.object(self.app.sessions, "ready", side_effect=retail.TransportError):
            with self.assertRaises(retail.TransportError):
                self.app.handle(self.t.message(data))
        self.app.handle(self.t.message(data))
        self.app.handle(self.t.message(data))
        self.assertEqual(len(self.t.sent), 1)
        self.assertNotIn("last_error", self.store.get(self.t.key)[0]["pending"])

    def test_late_failure_metadata_cannot_overwrite_newer_job(self):
        self.t.pending(id="newer")
        before = copy.deepcopy(self.store.rows)
        self.app.job_status(self.t.key, "old", "auth_required", terminal=True)
        self.assertEqual(before, self.store.rows)

    def test_lambda_propagates_unclaimed_callback_failure_for_redelivery(self):
        data = self.t.pending()
        with patch.object(lambda_function, "Store", return_value=self.store), \
                patch.object(lambda_function, "App", return_value=self.app), \
                patch.object(self.app.sessions, "ready", side_effect=Busy):
            with self.assertRaises(RuntimeError):
                lambda_function.lambda_handler(self.t.message(data), None)
        self.assertEqual(self.store.get(self.t.key)[0]["pending"]["state"], "pending")

    def test_pairing_requires_active_unexpired_prompt_in_same_session(self):
        voice = self.t.voice("EchoNameIntent", {"echoName": {"value": "Office"}})
        for pairing in (None, {"expires": 1499}, {"expires": 1600, "session_id": "other"}):
            row, rev = self.store.get(self.t.key)
            self.store.put(self.t.key, {"pairing": pairing}, rev)
            self.app.handle(voice)
            self.assertNotIn("target", self.store.get(self.t.key)[0])

    def test_active_pairing_links_exact_name_and_rejects_ambiguity(self):
        self.app.handle(self.t.voice("LinkEchoIntent"))
        old, rev = self.store.get("retail-session")
        old["devices"].append({**old["devices"][0], "serial": "other"})
        self.store.put("retail-session", old, rev)
        voice = self.t.voice("EchoNameIntent", {"echoName": {"value": "Office"}})
        self.app.handle(voice)
        self.assertNotIn("target", self.store.get(self.t.key)[0])
        old, rev = self.store.get("retail-session")
        old["devices"].pop()
        self.store.put("retail-session", old, rev)
        self.app.handle(voice)
        self.assertEqual(self.store.get(self.t.key)[0]["target"]["serial"], "office")

    def test_cancel_during_discovery_cannot_restore_pairing(self):
        self.app.handle(self.t.voice("LinkEchoIntent"))
        def discover(state, now):
            self.app.handle(self.t.voice("AMAZON.CancelIntent"))
            return state
        self.app.sessions.discoverer = discover
        self.app.handle(self.t.voice("EchoNameIntent", {"echoName": {"value": "Office"}}))
        self.assertNotIn("target", self.store.get(self.t.key)[0])


class SessionRecoveryTests(unittest.TestCase):
    def test_rejected_discovery_then_transport_failure_marks_stale_not_signin(self):
        store = fixtures.Memory(); store.put("retail-session", fixtures.session())
        def discover(*args, **kwargs): raise retail.AuthRequired()
        def renew(*args, **kwargs): raise retail.TransportError()
        with self.assertRaises(retail.TransportError):
            Sessions(store, lambda:1500, renew, discover).ready(refresh_devices=True)
        self.assertEqual(store.get("retail-session")[0]["auth_state"], "stale")
        self.assertEqual(store.get("session-lease")[0]["until"], 0)

    def test_expired_lease_cannot_commit_renewal(self):
        store = fixtures.Memory(); store.put("retail-session", fixtures.session())
        now = [11000]
        def renew(state, **kwargs):
            now[0] += 46
            return {**state, "cookies": {"csrf": "NEW"}}
        with self.assertRaises(Busy):
            Sessions(store, lambda:now[0], renew).ready()
        self.assertEqual(store.get("retail-session")[0], fixtures.session())

    def test_device_refresh_uses_one_get_without_token_exchange(self):
        calls = []
        def transport(method, url, *args):
            calls.append((method, url))
            return 200, [], b'{"devices":[]}'
        original = fixtures.session()
        updated = retail.refresh_devices(original, transport, now=1700)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "GET")
        self.assertEqual(updated["devices_refreshed_at"], 1700)
        self.assertEqual(original, fixtures.session())

    def test_discovery_auth_rejection_renews_once_before_speech(self):
        store = fixtures.Memory(); store.put("retail-session", fixtures.session())
        calls = []
        def discover(*args, **kwargs):
            calls.append("discover")
            raise retail.AuthRequired()
        def renew(state, now):
            calls.append("renew")
            return {**state, "cookies_refreshed_at": now}
        result = Sessions(store, lambda:1500, renew, discover).ready(refresh_devices=True)
        self.assertEqual(calls, ["discover", "renew"])
        self.assertEqual(result["auth_state"], "ready")

    def test_access_token_expiry_does_not_control_cookie_renewal(self):
        s = fixtures.session()
        s["access_expires"] = 1
        self.assertFalse(retail.needs_renewal(s, 1500))
        self.assertTrue(retail.needs_renewal(s, 1000 + retail.COOKIE_REFRESH_SECONDS))

    def test_stale_device_cache_refresh_does_not_renew_auth(self):
        store = fixtures.Memory(); store.put("retail-session", fixtures.session())
        calls = []
        def renew(*args, **kwargs): self.fail("Authentication renewed unnecessarily")
        def discover(state, now):
            calls.append(1)
            return {**state, "devices_refreshed_at": now}
        Sessions(store, lambda:1900, renew, discover).ready(target=fixtures.session()["devices"][0])
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
