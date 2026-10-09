"""JSON records with optimistic concurrency on the Alexa-managed DynamoDB table."""
import copy
import json
import os
import secrets
import time


class Conflict(Exception):
    pass


class Busy(Exception):
    pass


class StoreUnavailable(Busy):
    pass


def transient_store_error(error):
    code = getattr(error, "response", {}).get("Error", {}).get("Code")
    return (code in {"ProvisionedThroughputExceededException", "ThrottlingException",
                    "RequestLimitExceeded", "InternalServerError", "ServiceUnavailable"}
            or type(error).__name__ in {"ReadTimeoutError", "ConnectTimeoutError",
                                       "EndpointConnectionError", "ConnectionClosedError"})


def metadata_failure(operation, error):
    print(json.dumps({"event": "metadata_write_failed", "operation": operation,
                      "category": type(error).__name__}))


class Store:
    def __init__(self, client=None, table=None):
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("dynamodb",
                region_name=os.environ["DYNAMODB_PERSISTENCE_REGION"],
                config=Config(connect_timeout=1, read_timeout=1,
                              retries={"max_attempts": 0}))
        self.client = client
        self.table = table or os.environ["DYNAMODB_PERSISTENCE_TABLE_NAME"]

    def get(self, key):
        # One bounded retry for an idempotent read. Never retry an uncertain write.
        for attempt in range(2):
            try:
                item = self.client.get_item(TableName=self.table, Key={"id": {"S": key}},
                                            ConsistentRead=True).get("Item")
                break
            except Exception as error:
                if not transient_store_error(error):
                    raise
                if attempt:
                    raise StoreUnavailable("Storage read unavailable") from None
        if not item:
            return None, None
        return json.loads(item["payload"]["S"]), item["revision"]["S"]

    def put(self, key, value, expected=None):
        revision = secrets.token_hex(16)
        kwargs = {}
        condition = "attribute_not_exists(id)"
        if expected is not None:
            condition = "revision = :old"
            kwargs["ExpressionAttributeValues"] = {":old": {"S": expected}}
        try:
            self.client.put_item(TableName=self.table,
                Item={"id": {"S": key}, "payload": {"S": json.dumps(value)},
                      "revision": {"S": revision}},
                ConditionExpression=condition, **kwargs)
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise Conflict() from None
            if transient_store_error(error):
                raise StoreUnavailable("Storage write unconfirmed") from None
            raise
        return revision

    def mutate(self, key, change):
        for _ in range(3):
            value, revision = self.get(key)
            updated = change(copy.deepcopy(value))
            try:
                self.put(key, updated, revision)
                return updated
            except Conflict:
                continue
        raise Busy("Concurrent operation; try again")


class Sessions:
    """Account-wide lease guards renewal across cold/concurrent Lambda workers."""
    def __init__(self, store, clock=time.time, renewer=None, discoverer=None):
        import retail
        self.store, self.clock = store, clock
        self.renewer = renewer or retail.renew
        self.discoverer = discoverer or retail.refresh_devices

    def invalidate(self, rejected, sign_in=False, mismatch=False):
        """Invalidate only the rejected authentication generation, not a newer one."""
        fields = ("auth_generation", "enrollment_id", "refresh_token", "cookies")
        def mark(current):
            if not current or any(current.get(k) != rejected.get(k) for k in fields):
                raise Conflict()
            # A late rejection must not downgrade a known interactive-auth failure.
            if mismatch:
                current["auth_state"] = "account_mismatch"
            elif current.get("auth_state") == "account_mismatch":
                return current
            elif sign_in or current.get("auth_state") != "sign_in_required":
                current["auth_state"] = "sign_in_required" if sign_in else "stale"
            return current
        try:
            self.store.mutate("retail-session", mark)
            return True
        except Conflict:
            return False
        except Busy as error:
            metadata_failure("invalidate_session", error)
            return False

    def ready(self, force=False, target=None, refresh_devices=False):
        import retail
        now = int(self.clock())
        def check(state):
            current_time = int(self.clock())
            if state and state.get("auth_state") == "account_mismatch":
                raise retail.IdentityMismatch("Repair account setup")
            if not state or (not force and state.get("auth_state") == "sign_in_required"):
                raise retail.RegistrationRejected("Set up the session")
            renewal = force or retail.needs_renewal(state, current_time)
            discovery = refresh_devices or (target is not None and retail.needs_devices(state, target, current_time))
            return renewal, discovery
        state, revision = self.store.get("retail-session")
        renewal, discovery = check(state)
        if not renewal and not discovery:
            return state
        lease_id = secrets.token_hex(16)

        def acquire(current):
            if current and current.get("until", 0) > now:
                raise Busy("Session renewal in progress")
            return {"owner": lease_id, "until": now + 45}
        try:
            lease = self.store.mutate("session-lease", acquire)
        except Busy:
            # Another worker may have finished since the first read. No sleeping
            # or unbounded polling on Alexa's request deadline.
            latest, _ = self.store.get("retail-session")
            renewal, discovery = check(latest)
            if not renewal and not discovery:
                return latest
            if (not force and not refresh_devices and target is not None and not renewal
                    and latest.get("devices_refreshed_at", 0) > self.clock() - 360):
                try:
                    retail.speech_payload(latest, target, "cache validation")
                except (retail.AuthRequired, retail.TargetUnavailable):
                    pass
                else:
                    # At most one minute beyond the normal five-minute age; no
                    # timestamp extension and no substitute target/pairing use.
                    print(json.dumps({"event": "device_cache_grace_used"}))
                    return latest
            raise
        try:
            # Re-read after acquiring; another worker may have just completed.
            state, revision = self.store.get("retail-session")
            renewal, discovery = check(state)
            if not renewal and not discovery:
                return state
            started = time.monotonic()
            phase = "auth_renewal" if renewal else "device_discovery"
            rejected_read = False
            try:
                if renewal:
                    renewed = self.renewer(copy.deepcopy(state), now=now)
                else:
                    try:
                        renewed = self.discoverer(copy.deepcopy(state), now=now)
                    except retail.AuthRequired:
                        # Safe GET rejection: one bounded renewal before any speech.
                        phase = "discovery_auth_recovery"
                        rejected_read = True
                        renewal = True
                        renewed = self.renewer(copy.deepcopy(state), now=now)
            except retail.IdentityMismatch:
                self.invalidate(state, mismatch=True)
                raise
            except retail.RegistrationRejected:
                self.invalidate(state, sign_in=True)
                raise
            except retail.AuthRequired:
                self.invalidate(state)
                raise retail.RenewalIncomplete("Renewal did not complete") from None
            except retail.TransportError:
                if rejected_read or renewal:
                    self.invalidate(state)
                raise
            finally:
                print(json.dumps({"event": "hosted_session_phase", "phase": phase,
                    "elapsed_ms": round((time.monotonic() - started) * 1000)}))
            if renewal:
                renewed["auth_generation"] = secrets.token_hex(16)
                renewed["auth_state"] = "ready"
            current, _ = self.store.get("session-lease")
            if current != lease or self.clock() >= lease["until"]:
                raise Busy("Renewal lease expired")
            self.store.put("retail-session", renewed, revision)
            return renewed
        finally:
            def release(current):
                if current and current.get("owner") == lease_id:
                    return {"owner": "", "until": 0}
                return current
            try:
                self.store.mutate("session-lease", release)
            except Busy as error:
                # The lease expires independently; don't mask the real outcome.
                metadata_failure("release_session_lease", error)
