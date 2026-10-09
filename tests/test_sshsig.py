"""Built-in SSH signature checks for release tags: no ssh-keygen, network or real keys needed."""
import contextlib
import io
import json
import os
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
    def test_rfc8032_vector_and_tampering(self):
        public = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        signature = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33"
                                  "bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
        self.assertTrue(sshsig.ed25519_verify(public, b"", signature))
        self.assertFalse(sshsig.ed25519_verify(public, b"x", signature))
        self.assertFalse(sshsig.ed25519_verify(public, b"", signature[:63] + b"\x00"))


class SshsigTests(unittest.TestCase):
    key = sshsig.public_key_blob(FIXTURE_KEY)

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


class VerifyReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.dict(os.environ, {"BRIDGE_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.signers = Path(self.tmp.name) / "allowed_signers"
        self.signers.write_text('fixture namespaces="git" ' + FIXTURE_KEY + "\n")
        self.out = io.StringIO()
        redirect = contextlib.redirect_stdout(self.out)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def runner(self, signed=True):
        def run(args, **kwargs):
            if "cat-file" in args:
                return SimpleNamespace(returncode=0, stdout=TAG if signed else PAYLOAD.decode(), stderr="")
            return SimpleNamespace(returncode=0, stdout="0123456789abcdef0123456789abcdef01234567\n", stderr="")
        return run

    def test_verify_reports_signer_and_commit_without_ssh_keygen(self):
        with patch("shutil.which", return_value=None):
            self.assertEqual(core.verify_release("v9.9.9", self.runner(), self.signers), FIXTURE_FPR)
        event = json.loads(self.out.getvalue().splitlines()[-1])
        self.assertEqual((event["event"], event["signer"]), ("release_verified", FIXTURE_FPR))

    def test_unsigned_or_unknown_signer_stops(self):
        for runner, signers in ((self.runner(signed=False), self.signers), (self.runner(), core.SIGNERS)):
            with self.assertRaises(core.Stop) as stop:
                core.verify_release("v9.9.9", runner, signers)
            self.assertEqual(stop.exception.category, "release_unverified")

    def test_update_checks_the_signature_before_checkout_and_never_deploys_a_bad_tag(self):
        calls = []
        def runner(signed):
            inner = self.runner(signed)
            def run(args, **kwargs):
                calls.append(args)
                return inner(args, **kwargs)
            return run
        with patch.object(core, "SIGNERS", self.signers), patch.object(core, "deploy") as deploy:
            with self.assertRaises(core.Stop) as stop:
                core.update({"phases": {}}, "v9.9.9", runner=runner(False))
            self.assertEqual(stop.exception.category, "update_refused")
            self.assertFalse(any("checkout" in c for c in calls))
            deploy.assert_not_called()
            calls.clear()
            core.update({"phases": {}}, "v9.9.9", runner=runner(True))
            deploy.assert_called_once()
        order = [c[3] for c in calls]
        self.assertLess(order.index("cat-file"), order.index("checkout"))


if __name__ == "__main__":
    unittest.main()
