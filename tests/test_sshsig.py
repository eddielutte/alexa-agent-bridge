"""Built-in SSH signature checks for release tags: no ssh-keygen, network or real keys needed."""
import base64
import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import core, sshsig  # noqa: E402

# Made once with `ssh-keygen -Y sign -n git` and a throwaway key whose private half was deleted.
FIXTURE_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHfxMYjH22J749mWNpEPorCoCG9g+U3ckq6AHsn2Klqc fixture"
FIXTURE_FPR = "SHA256:IqwhhO4AyWSkLV+/VJ/HWxSc1sK2fZG+aV7xfK24+e0"
PAYLOAD = (b"object 0123456789abcdef0123456789abcdef01234567\ntype commit\ntag v9.9.9\n"
           b"tagger Fixture <fixture@example.test> 1700000000 +0000\n\nfixture release\n")
SIGNATURE = """-----BEGIN SSH SIGNATURE-----
U1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgd/ExiMfbYnvj2ZY2kQ+isKgIb2
D5TdySroAeyfYqWpwAAAADZ2l0AAAAAAAAAAZzaGE1MTIAAABTAAAAC3NzaC1lZDI1NTE5
AAAAQNC47ctWIWSo9rv6Htz1DDoWsE6g0UOLgaszIMGqKyFsxMCc/hS8SyMCFko0hcclMv
/q+LaWRg9rupnwEDbvug0=
-----END SSH SIGNATURE-----
"""
TAG = PAYLOAD.decode() + SIGNATURE


class Ed25519Tests(unittest.TestCase):
    def test_wycheproof_vectors(self):
        """Google's Wycheproof Ed25519 vectors (Apache-2.0), which include RFC 8032's: every valid case passes,
        every invalid case fails."""
        data = json.loads((Path(__file__).parent / "data" / "wycheproof_ed25519_test.json").read_text(encoding="utf-8"))
        seen = 0
        for group in data["testGroups"]:
            public = bytes.fromhex(group["publicKey"]["pk"])
            for case in group["tests"]:
                seen += 1
                with self.subTest(tcId=case["tcId"], comment=case["comment"]):
                    got = sshsig.ed25519_verify(public, bytes.fromhex(case["msg"]), bytes.fromhex(case["sig"]))
                    self.assertEqual(got, case["result"] == "valid")
        self.assertEqual(seen, data["numberOfTests"])


class SshsigTests(unittest.TestCase):
    key = sshsig.public_key_blob(FIXTURE_KEY)

    def test_shipped_release_key_matches_the_published_fingerprint(self):
        keys = sshsig.allowed_keys(core.SIGNERS.read_text(encoding="utf-8"))
        self.assertEqual(len(keys), 1)
        published = sshsig.fingerprint(keys[0])
        for doc in ("SETUP.md", "README.md", "CONTRIBUTING.md"):  # public/ in this repository, the root once exported
            path = core.ROOT / "public" / doc if (core.ROOT / "public" / doc).exists() else core.ROOT / doc
            self.assertIn(published, path.read_text(encoding="utf-8"), doc)

    def test_ssh_keygen_signature_verifies_with_the_same_fingerprint(self):
        payload, armoured = sshsig.split_signed_tag(TAG.encode())
        self.assertEqual(payload, PAYLOAD)
        self.assertEqual(sshsig.verify(payload, armoured, [self.key]), FIXTURE_FPR)

    def test_tampering_wrong_key_wrong_namespace_and_unsigned_are_refused(self):
        other = sshsig.public_key_blob((Path(core.ROOT) / "allowed_signers").read_text())
        for args, reason in (((PAYLOAD + b"x", SIGNATURE, [self.key]), "bad_signature"),
                             ((PAYLOAD, SIGNATURE, [other]), "unknown_key"),
                             ((PAYLOAD, SIGNATURE, [self.key], "file"), "wrong_namespace")):
            with self.assertRaises(sshsig.BadSignature) as caught:
                sshsig.verify(*args)
            self.assertEqual(str(caught.exception), reason)
        with self.assertRaises(sshsig.BadSignature):
            sshsig.split_signed_tag(PAYLOAD)


def armour(blob):
    body = base64.b64encode(blob).decode()
    return "\n".join([sshsig.BEGIN] + [body[i:i + 70] for i in range(0, len(body), 70)] + [sshsig.END]) + "\n"


class ContainerTests(unittest.TestCase):
    key = sshsig.public_key_blob(FIXTURE_KEY)

    def test_only_one_exact_signature_block_ending_the_tag(self):
        for raw in (TAG + "extra text\n", TAG + SIGNATURE, PAYLOAD.decode() + "x" + SIGNATURE):
            with self.assertRaises(sshsig.BadSignature):
                sshsig.split_signed_tag(raw.encode())

    def test_trailing_bytes_inside_the_signature_are_refused(self):
        blob = base64.b64decode("".join(SIGNATURE.strip().split("\n")[1:-1]))
        with self.assertRaises(sshsig.BadSignature) as caught:
            sshsig.verify(PAYLOAD, armour(blob + b"\x00"), [self.key])
        self.assertEqual(str(caught.exception), "trailing_data")
        with self.assertRaises(sshsig.BadSignature):
            sshsig.verify(PAYLOAD, SIGNATURE.replace(sshsig.BEGIN, "-----BEGIN SSH SIGNATURE----- "), [self.key])

    def test_allowed_signers_parsing_fails_closed(self):
        key = FIXTURE_KEY.split()[1]
        self.assertEqual(sshsig.allowed_keys('  # p ssh-ed25519 %s\np namespaces="git" ssh-ed25519 %s c\n' % (key, key)),
                         [self.key])
        def string(value):
            return len(value).to_bytes(4, "big") + value
        identity = base64.b64encode(string(b"ssh-ed25519") + string(b"\x01" + b"\x00" * 31)).decode()
        for line, reason in (('p valid-before="20200101" ssh-ed25519 %s' % key, "unsupported_signers_option"),
                             ("!*,fixture ssh-ed25519 %s" % key, "unsupported_principal"),
                             ("p ssh-rsa AAAA", "unsupported_key"),
                             ("p ssh-ed25519 %s" % identity, "degenerate_key")):
            with self.assertRaises(sshsig.BadSignature) as caught:
                sshsig.allowed_keys(line)
            self.assertEqual(str(caught.exception), reason)
        with self.assertRaises(ValueError):
            sshsig.allowed_keys("p ssh-ed25519 not*base64")


class VerifyReleaseTests(unittest.TestCase):
    COMMIT = "0123456789abcdef0123456789abcdef01234567"
    OLD = "fedcba9876543210fedcba9876543210fedcba98"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.dict(os.environ, {"BRIDGE_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        which = patch("shutil.which", return_value=None)
        which.start()
        self.addCleanup(which.stop)
        self.signers = Path(self.tmp.name) / "allowed_signers"
        self.signers.write_text('fixture namespaces="git" ' + FIXTURE_KEY + "\n")
        for target in ("SIGNERS",):
            p = patch.object(core, target, self.signers)
            p.start()
            self.addCleanup(p.stop)
        self.out = io.StringIO()
        redirect = contextlib.redirect_stdout(self.out)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.calls = []
        self.deploys = []

    def runner(self, signed=True, commit=None, head=None, changes="", described=None, size=None, verify_tag_ok=True,
               timeout_on=None):
        def run(args, **kwargs):
            self.calls.append(args)
            if timeout_on and timeout_on in args:
                raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))
            done = lambda out="", code=0: SimpleNamespace(returncode=code, stdout=out, stderr="")
            if "cat-file" in args and "-s" in args:
                return done("%d\n" % (size if size is not None else len(TAG)))
            if "cat-file" in args:
                return done((TAG if signed else PAYLOAD.decode()).encode())
            if "rev-parse" in args:
                return done((head or self.COMMIT) + "\n") if "HEAD" in args else done((commit or self.COMMIT) + "\n")
            if "describe" in args:
                return done(described + "\n") if described else done("", 128)
            if "status" in args:
                return done(changes)
            if "verify-tag" in args:
                return done(code=0 if verify_tag_ok else 1)
            return done()
        return run

    def deployer(self, code=0):
        def deploy():
            self.deploys.append(code)
            return code
        return deploy

    def events(self):
        return [json.loads(line) for line in self.out.getvalue().splitlines() if line.startswith("{")]

    def stop(self, category, function, *args, **kwargs):
        with self.assertRaises(core.Stop) as stop:
            function(*args, **kwargs)
        self.assertEqual(stop.exception.category, category)
        return stop.exception

    def state(self, tag="v0.1.1", commit=None):
        return {"phases": {}, "release": {"tag": tag, "commit": commit or self.OLD}} if tag else {"phases": {}}

    # verify_release

    def test_verify_reports_signer_and_commit_without_ssh_keygen(self):
        self.assertEqual(core.verify_release("v9.9.9", self.runner()), self.COMMIT)
        event = self.events()[-1]
        self.assertEqual((event["event"], event["signer"], event["checks"]), ("release_verified", FIXTURE_FPR, ["built-in"]))
        self.assertTrue(all("refs/tags/v9.9.9" in " ".join(c) for c in self.calls if "cat-file" in c))

    def test_oversized_tags_are_refused_before_reading(self):
        error = self.stop("release_unverified", core.verify_release, "v9.9.9", self.runner(size=2000000))
        self.assertIn("too_large", error.message)
        self.assertFalse(any("cat-file" in c and "tag" in c for c in self.calls))

    def test_unsigned_or_unknown_signer_stops(self):
        self.stop("release_unverified", core.verify_release, "v9.9.9", self.runner(signed=False))
        self.stop("release_unverified", core.verify_release, "v9.9.9", self.runner(), Path(core.ROOT) / "allowed_signers")

    def test_a_signed_tag_under_another_name_or_commit_is_refused(self):
        self.assertIn("tag_mismatch", self.stop("release_unverified", core.verify_release, "v9.9.10", self.runner()).message)
        self.assertIn("tag_mismatch", self.stop("release_unverified", core.verify_release, "v9.9.9",
                                                self.runner(commit="f" * 40)).message)

    def test_only_release_shaped_tag_names_are_accepted(self):
        for name in ("--upload-pack=x", "main", "v1.2", "v1.2.3-rc1", ""):
            self.stop("release_name_invalid", core.verify_release, name, self.runner())
        self.assertEqual(self.calls, [])

    def test_ssh_keygen_is_a_second_opinion_that_must_agree(self):
        with patch("shutil.which", return_value="/usr/bin/ssh-keygen"):
            core.verify_release("v9.9.9", self.runner())
            self.assertEqual(self.events()[-1]["checks"], ["built-in", "ssh-keygen"])
            self.assertIn("gpg.minTrustLevel=fully", next(c for c in self.calls if "verify-tag" in c))
            error = self.stop("release_unverified", core.verify_release, "v9.9.9", self.runner(verify_tag_ok=False))
        self.assertIn("ssh_keygen_disagrees", error.message)

    def test_git_commands_ignore_replace_refs(self):
        with patch("subprocess.run") as fake:
            core.run(["git", "status"])
            core.run(["node", "--version"])
        self.assertEqual(fake.call_args_list[0].kwargs["env"]["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertNotIn("env", fake.call_args_list[1].kwargs)

    # bridge verify (first install)

    def test_verify_install_requires_the_checkout_to_be_that_release_and_records_it(self):
        state = {"phases": {}}
        self.stop("release_not_checked_out", core.verify_install, state, "v9.9.9", self.runner(head=self.OLD))
        self.stop("release_not_checked_out", core.verify_install, state, "v9.9.9", self.runner(changes=" M bridge/core.py\n"))
        self.assertNotIn("release", state)
        core.verify_install(state, "v9.9.9", self.runner())
        self.assertEqual(state["release"], {"tag": "v9.9.9", "commit": self.COMMIT})
        saved = json.loads((Path(self.tmp.name) / "state.json").read_text())
        self.assertEqual(saved["release"]["tag"], "v9.9.9")

    # bridge update

    def test_update_verifies_then_deploys_the_verified_commit_in_a_fresh_process(self):
        state = self.state()
        self.stop("update_refused", core.update, state, "v9.9.9", runner=self.runner(signed=False, head=self.OLD),
                  deployer=self.deployer())
        self.assertFalse(any("checkout" in c for c in self.calls))
        self.assertEqual(self.deploys, [])
        self.calls.clear()
        core.update(state, "v9.9.9", runner=self.runner(head=self.OLD), deployer=self.deployer())
        self.assertEqual(self.deploys, [0])
        self.assertIn(["git", "-C", str(core.ROOT), "checkout", "-q", "--detach", self.COMMIT], self.calls)
        fetch = next(c for c in self.calls if "fetch" in c)
        self.assertIn("--no-tags", fetch)
        self.assertIn("+refs/tags/v9.9.9:refs/tags/v9.9.9", fetch)
        self.assertEqual(state["release"], {"tag": "v9.9.9", "commit": self.COMMIT})
        self.assertFalse((Path(self.tmp.name) / "update.lock").exists())

    def test_update_refuses_downgrades_and_a_changed_checkout(self):
        for installed in ("v9.9.9", "v10.0.0"):
            error = self.stop("update_refused", core.update, self.state(installed), "v9.9.9",
                              runner=self.runner(head=self.OLD), deployer=self.deployer())
            self.assertIn("isn't newer", error.message)
        self.stop("release_baseline_unknown", core.update, self.state(), "v9.9.9",
                  runner=self.runner(head=self.OLD, changes=" M lambda/retail.py\n"), deployer=self.deployer())
        self.stop("release_baseline_unknown", core.update, self.state(), "v9.9.9",
                  runner=self.runner(head="a" * 40), deployer=self.deployer())
        self.assertEqual(self.deploys, [])

    def test_unrecorded_install_needs_a_verified_tag_on_head(self):
        self.stop("release_baseline_unknown", core.update, self.state(None), "v9.9.9", runner=self.runner(),
                  deployer=self.deployer())
        self.stop("release_baseline_unknown", core.update, self.state(None), "v9.9.9",
                  runner=self.runner(described="v0.0.0"), deployer=self.deployer())
        state = self.state(None)
        error = self.stop("update_refused", core.update, state, "v9.9.9", runner=self.runner(described="v9.9.9"),
                          deployer=self.deployer())
        self.assertIn("isn't newer", error.message)
        self.assertEqual(state["release"], {"tag": "v9.9.9", "commit": self.COMMIT})

    def test_only_one_update_at_a_time_and_timeouts_fail_closed(self):
        lock = Path(self.tmp.name) / "update.lock"
        lock.write_text("")
        self.stop("update_in_progress", core.update, self.state(), "v9.9.9", runner=self.runner(head=self.OLD),
                  deployer=self.deployer())
        lock.unlink()
        self.stop("update_refused", core.update, self.state(), "v9.9.9", runner=self.runner(head=self.OLD, timeout_on="fetch"),
                  deployer=self.deployer())
        self.assertFalse(lock.exists())
        self.assertEqual(self.deploys, [])

    def test_a_stopped_or_failed_deploy_is_reported(self):
        self.stop("deploy_stopped", core.update, self.state(), "v9.9.9", runner=self.runner(head=self.OLD),
                  deployer=self.deployer(2))
        saved = json.loads((Path(self.tmp.name) / "state.json").read_text())
        self.assertEqual((saved["release"]["tag"], saved["deploy_pending"]), ("v9.9.9", "v9.9.9"))
        with self.assertRaises(core.Failed):
            core.update(self.state(), "v9.9.9", runner=self.runner(head=self.OLD), deployer=self.deployer(1))

    def test_latest_picks_the_newest_release_and_still_verifies_it(self):
        listing = "".join("%s\trefs/tags/%s\n" % ("0" * 40, t) for t in ("v0.1.2", "v9.9.9", "v10.0.0-rc1", "main"))
        def with_listing(inner):
            def run(args, **kwargs):
                if "ls-remote" in args:
                    return SimpleNamespace(returncode=0, stdout=listing, stderr="")
                return inner(args, **kwargs)
            return run
        self.assertEqual(core.latest_release(with_listing(self.runner())), "v9.9.9")
        core.update(self.state(), runner=with_listing(self.runner(head=self.OLD)), deployer=self.deployer())
        self.assertEqual(self.deploys, [0])
        core.update(self.state("v9.9.9", self.COMMIT), runner=with_listing(self.runner()), deployer=self.deployer())
        self.assertEqual((self.events()[-1]["event"], self.deploys), ("up_to_date", [0]))
        self.stop("update_refused", core.update, self.state(), runner=with_listing(self.runner(signed=False, head=self.OLD)),
                  deployer=self.deployer())


if __name__ == "__main__":
    unittest.main()
