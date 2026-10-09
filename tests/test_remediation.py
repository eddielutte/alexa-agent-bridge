"""Regression tests for delivery, dispatch and session edge cases: no credentials, live services or device actions."""
import copy
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_skill as f
from storage import Store, Sessions, Busy, Conflict, StoreUnavailable
import retail
import messaging


class AppRegressionTests(unittest.TestCase):
    def setUp(self):
        self.t = f.AppTests(); self.t.setUp()
        self.app, self.store = self.t.app, self.t.store
        self.req = {"requestId": "new", "timestamp": "1970-01-01T00:25:01Z"}
        self.calls = []
        self.app.dispatch = lambda *args: self.calls.append(args)

    def submit(self, row=None, req=None):
        return self.app.submit("owner", self.t.key, row or self.store.get(self.t.key)[0],
                               req or self.req, "test", {})

    def text(self, result):
        return result["response"]["outputSpeech"]["text"]

    def test_known_blocked_auth_does_not_create_job_or_dispatch(self):
        for state in ("sign_in_required", "account_mismatch"):
            with self.subTest(state=state):
                s, rev = self.store.get("retail-session")
                self.store.put("retail-session", {**s, "auth_state": state}, rev)
                reply = self.submit({"target": f.session()["devices"][0]})
                self.assertIn("not sent", self.text(reply))
                self.assertIsNone(self.store.get(self.t.key)[0])
        self.assertEqual(self.calls, [])

    def test_stale_auth_can_dispatch_for_automatic_renewal(self):
        self.t.pending()
        s, rev = self.store.get("retail-session")
        self.store.put("retail-session", {**s, "auth_state": "stale"}, rev)
        self.assertEqual(self.text(self.submit()), "Okay, one moment.")
        self.assertEqual(len(self.calls), 1)

    def test_pre_dispatch_store_failure_never_contacts_agent(self):
        with patch.object(self.store, "get", side_effect=StoreUnavailable):
            with self.assertRaises(Busy):
                self.submit({"target": f.session()["devices"][0]})
        self.assertEqual(self.calls, [])

    def test_pre_webhook_failure_is_terminal_but_uncertain_post_is_not(self):
        for error, expected in ((messaging.DispatchNotSent, "dispatch_failed"),
                                (retail.TransportError, "pending")):
            self.setUp(); self.t.pending()
            with patch.object(self.app, "dispatch", side_effect=error):
                reply = self.submit()
            job = self.store.get(self.t.key)[0]["pending"]
            self.assertEqual(job["state"], expected)
            self.assertEqual("token" in job, expected == "pending")
            self.assertIn("not sent" if expected == "dispatch_failed" else "could not confirm", self.text(reply))

    def test_nonduplicate_route_or_generation_conflict_requests_repeat(self):
        for field, value in (("target", {"serial": "bedroom", "type": "echo"}), ("generation", 1)):
            self.setUp(); self.t.pending()
            old, rev = self.store.get(self.t.key)
            self.store.put(self.t.key, {**old, field: value}, rev)
            self.assertIn("not sent", self.text(self.submit(old)))
            self.assertEqual(self.calls, [])

    def test_duplicate_ack_does_not_dispatch_again(self):
        self.t.pending(); self.submit()
        self.assertIn("already received", self.text(self.submit()))
        self.assertEqual(len(self.calls), 1)

    def test_cancel_order_uses_event_clock_despite_server_clock_skew(self):
        self.t.pending()
        self.app.clock = lambda: 1510  # server is ten seconds ahead
        self.app.handle(self.t.voice("AMAZON.CancelIntent"))  # event 1500
        self.assertIn("Okay", self.text(self.submit()))  # event 1501
        self.assertEqual(len(self.calls), 1)

    def test_late_precancel_request_cannot_restart_work(self):
        self.t.pending(); self.app.handle(self.t.voice("AMAZON.CancelIntent"))
        reply = self.submit(req={"requestId": "old", "timestamp": "1970-01-01T00:24:59Z"})
        self.assertIn("not sent", self.text(reply))
        self.assertEqual(self.calls, [])

    def test_cancel_after_claim_still_prevents_speech(self):
        data = self.t.pending(); original = self.store.mutate
        def mutate(key, change):
            result = original(key, change)
            if key == self.t.key and (result or {}).get("pending", {}).get("state") == "sending":
                self.store.mutate = original
                self.app.handle(self.t.voice("AMAZON.CancelIntent"))
            return result
        self.store.mutate = mutate
        self.app.handle(self.t.message(data))
        self.assertEqual(self.t.sent, [])

    def test_generation_changed_before_claim_prevents_speech(self):
        data = self.t.pending()
        real = self.app.sessions.ready
        def ready(**kwargs):
            result = real(**kwargs)
            row, rev = self.store.get(self.t.key)
            self.store.put(self.t.key, {**row, "generation": 1}, rev)
            return result
        self.app.sessions.ready = ready
        self.app.handle(self.t.message(data))
        self.assertEqual(self.t.sent, [])

    def test_read_failure_after_claim_records_no_send_without_replay(self):
        data = self.t.pending(); original_get = self.store.get; failed = [False]
        def get(key):
            row, rev = original_get(key)
            if key == self.t.key and row.get("pending", {}).get("state") == "sending" and not failed[0]:
                failed[0] = True
                raise StoreUnavailable()
            return row, rev
        self.store.get = get
        self.app.handle(self.t.message(data))
        self.app.handle(self.t.message(data))
        self.assertEqual(self.t.sent, [])
        self.assertEqual(original_get(self.t.key)[0]["pending"]["state"], "delivery_failed")

    def test_metadata_busy_does_not_mask_callback_transport_failure(self):
        data = self.t.pending()
        with patch.object(self.app.sessions, "ready", side_effect=retail.TransportError), \
                patch.object(self.store, "mutate", side_effect=Busy):
            with self.assertRaises(retail.TransportError):
                self.app.handle(self.t.message(data))

    def test_status_combines_last_outcome_and_authentication(self):
        self.t.pending(state="auth_required")
        state, rev = self.store.get("retail-session")
        self.store.put("retail-session", {**state, "auth_state": "sign_in_required"}, rev)
        text = self.text(self.app.handle(self.t.voice("ResultIntent")))
        self.assertIn("last answer was not sent", text)
        self.assertIn("renew cloud authentication", text)

    def test_control_exception_wording(self):
        for error, phrase in ((Busy, "busy"), (retail.RenewalIncomplete, "temporary"),
                              (retail.RegistrationRejected, "sign-in"),
                              (retail.IdentityMismatch, "account could not be verified")):
            with patch.object(self.app.sessions, "ready", side_effect=error):
                text = self.text(self.app.handle(self.t.voice("RenewIntent")))
            self.assertIn(phrase, text)

    def test_uncertain_proof_records_receipt_and_does_not_repeat(self):
        with patch.object(self.app, "speaker", side_effect=retail.UnconfirmedSpeech) as send:
            reply = self.app.handle(self.t.voice("ProofIntent"))
            self.app.handle(self.t.voice("ProofIntent"))
        self.assertIn("not confirm", self.text(reply))
        self.assertEqual(send.call_count, 1)
        receipts = self.store.rows["proofs"][0]["recent"]
        self.assertEqual([r["state"] for r in receipts], ["unconfirmed"])


class SessionRegressionTests(unittest.TestCase):
    def state(self, **changes):
        store = f.Memory(); state = {**f.session(), **changes}
        store.put("retail-session", state)
        return store, state

    def test_incomplete_renewal_is_retryable_and_not_signin(self):
        for error in (retail.AuthRequired, retail.RenewalIncomplete):
            store, _ = self.state(auth_state="stale")
            def renew(*args, **kwargs): raise error()
            with self.assertRaises(retail.TransportError):
                Sessions(store, lambda:1500, renew).ready()
            self.assertEqual(store.get("retail-session")[0]["auth_state"], "stale")

    def test_account_mismatch_stays_blocked_even_with_force(self):
        store, _ = self.state(auth_state="stale")
        def renew(*args, **kwargs): raise retail.IdentityMismatch()
        sessions = Sessions(store, lambda:1500, renew)
        with self.assertRaises(retail.IdentityMismatch): sessions.ready()
        self.assertEqual(store.get("retail-session")[0]["auth_state"], "account_mismatch")
        sessions.renewer = lambda *a, **k:self.fail("Mismatch must not retry")
        with self.assertRaises(retail.IdentityMismatch): sessions.ready(force=True)

    def test_age_only_contention_can_use_exact_recent_online_target(self):
        store, state = self.state(devices_refreshed_at=1190)
        store.put("session-lease", {"owner":"other", "until":1545})
        result = Sessions(store, lambda:1500).ready(target=state["devices"][0])
        self.assertEqual(result, state)
        self.assertEqual(store.get("retail-session")[0]["devices_refreshed_at"], 1190)

    def test_cache_grace_never_applies_to_required_checks(self):
        for case in ("offline", "missing", "too_old", "stale_auth", "pairing", "forced"):
            store, state = self.state(devices_refreshed_at=1190)
            if case == "offline": state["devices"][0]["online"] = False
            if case == "missing": state["devices"] = []
            if case == "too_old": state["devices_refreshed_at"] = 1140
            if case == "stale_auth": state["auth_state"] = "stale"
            _, rev = store.get("retail-session"); store.put("retail-session", state, rev)
            store.put("session-lease", {"owner":"other", "until":1545})
            with self.subTest(case=case), self.assertRaises(Busy):
                Sessions(store, lambda:1500).ready(target=f.session()["devices"][0],
                    refresh_devices=case=="pairing", force=case=="forced")

    def test_invalidation_busy_returns_failure_without_overwriting_new_state(self):
        store, state = self.state()
        with patch.object(store, "mutate", side_effect=Busy):
            self.assertFalse(Sessions(store).invalidate(state))
        self.assertEqual(store.get("retail-session")[0], state)

    def test_release_busy_does_not_hide_registration_rejection(self):
        store, _ = self.state(auth_state="stale"); original = store.mutate
        def mutate(key, change):
            if key == "session-lease" and store.get(key)[0]: raise Busy()
            return original(key, change)
        store.mutate = mutate
        def renew(*a, **k): raise retail.RegistrationRejected()
        with self.assertRaises(retail.RegistrationRejected): Sessions(store,lambda:1500,renew).ready()


class BoundaryTests(unittest.TestCase):
    def test_only_pre_webhook_failures_are_definite_not_sent(self):
        config={"webhook_url":"https://example.test/hook", "webhook_key":"FAKE", "client_id":"FAKE", "client_secret":"FAKE"}
        job={"id":"job", "token":"FAKE", "api_host":"api.eu.amazonalexa.com"}
        for stage in ("token", "webhook"):
            calls=[]
            def sender(method,url,*args):
                calls.append(url)
                if '/auth/o2/token' in url:
                    return (503,b'{}') if stage=="token" else (200,b'{"access_token":"FAKE"}')
                return 503,b'{}'
            with self.assertRaises(retail.TransportError) as caught:
                messaging.dispatch(config,"owner","key",job,"test",sender)
            self.assertEqual(isinstance(caught.exception,messaging.DispatchNotSent),stage=="token")
            self.assertEqual(len(calls),1 if stage=="token" else 2)

    def test_any_2xx_webhook_reply_confirms_and_nothing_else_does(self):
        config={"webhook_url":"https://example.test/hook", "webhook_key":"FAKE", "client_id":"FAKE", "client_secret":"FAKE"}
        job={"id":"job", "token":"FAKE", "api_host":"api.eu.amazonalexa.com"}
        for status in (200, 202, 204, 299, 301, 302, 400, 401, 404, 500, 503):
            calls=[]
            def sender(method,url,*args):
                calls.append(url)
                return (200,b'{"access_token":"FAKE"}') if '/auth/o2/token' in url else (status,b'')
            if 200 <= status < 300:
                messaging.dispatch(config,"owner","key",job,"test",sender)
            else:
                with self.assertRaises(retail.TransportError) as caught:
                    messaging.dispatch(config,"owner","key",job,"test",sender)
                self.assertNotIsInstance(caught.exception,messaging.DispatchNotSent)
            self.assertEqual(calls.count("https://example.test/hook"),1,status)

    def test_webhook_waits_four_seconds_and_token_is_reused_until_near_expiry(self):
        config = {"webhook_url": "https://example.test/hook", "webhook_key": "FAKE", "client_id": "FAKE", "client_secret": "FAKE"}
        job = {"id": "job", "token": "FAKE", "api_host": "api.eu.amazonalexa.com"}
        calls, clock = [], [1000]
        def sender(method, url, *args):
            calls.append((url, args[3:]))
            if "/auth/o2/token" in url:
                return 200, b'{"access_token":"FAKE-%d","expires_in":3600}' % len(calls)
            return 202, b""
        with patch.object(messaging.time, "time", lambda: clock[0]):
            for t in (1000, 1500, 1000 + 3600 - 299):
                clock[0] = t
                messaging.dispatch(config, "owner", "key", job, "test", sender)
        tokens = [u for u, _ in calls if "/auth/o2/token" in u]
        hooks = [extra for u, extra in calls if u == "https://example.test/hook"]
        self.assertEqual(len(tokens), 2)
        self.assertEqual(hooks, [(messaging.WEBHOOK_TIMEOUT,)] * 3)
        self.assertEqual(messaging.WEBHOOK_TIMEOUT, 4.0)
        other_calls = []
        def other(method, url, *args):
            other_calls.append(url)
            return (200, b'{"access_token":"OTHER","expires_in":3600}') if "/auth/o2/token" in url else (200, b"")
        messaging.dispatch(config, "owner", "key", job, "test", other)
        self.assertIn("https://api.amazon.com/auth/o2/token", other_calls)

    def test_bad_webhook_is_rejected_before_any_network(self):
        for url in ("http://example.test", "https://example.test:bad", "https://example.test/#fragment"):
            with self.assertRaises(messaging.DispatchNotSent):
                messaging.dispatch({"webhook_url":url,"webhook_key":"FAKE"},"u","d",{},"t",
                                   lambda *a:self.fail("Network called"))

    def test_incomplete_identity_is_distinct_from_mismatch(self):
        for identity, error in (({},retail.RenewalIncomplete), ({"id":"different"},retail.IdentityMismatch)):
            def transport(method,url,*args):
                if '/auth/token' in url:
                    return 200,[],b'{"response":{"tokens":{"cookies":{".amazon.co.uk":[{"Name":"csrf","Value":"FAKE"}]}}}}'
                return 200,[],json.dumps(identity).encode()
            with self.assertRaises(error): retail.renew(f.session(),transport,now=1500)

    def test_store_read_retry_and_write_no_retry(self):
        class ServiceError(Exception):
            response={"Error":{"Code":"InternalServerError"}}
        client=SimpleNamespace(get_item=None,put_item=None)
        calls=[]
        def get(**kw):
            calls.append(1)
            if len(calls)==1: raise ServiceError()
            return {}
        client.get_item=get
        self.assertEqual(Store(client,"table").get("key"),(None,None))
        self.assertEqual(len(calls),2)
        with patch.object(client,"put_item",side_effect=ServiceError) as put:
            with self.assertRaises(StoreUnavailable): Store(client,"table").put("key",{})
            self.assertEqual(put.call_count,1)

    def test_real_store_conditional_error_maps_to_conflict(self):
        class ConditionalError(Exception):
            response={"Error":{"Code":"ConditionalCheckFailedException"}}
        client=SimpleNamespace(put_item=None)
        with patch.object(client,"put_item",side_effect=ConditionalError):
            with self.assertRaises(Conflict): Store(client,"table").put("key",{})


if __name__ == "__main__":
    unittest.main()
