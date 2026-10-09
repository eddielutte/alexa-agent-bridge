"""Heuristic tracked-file secret check. Reports paths and line numbers only, never contents."""
import re
import subprocess
import sys

PRIVATE = re.compile(r"(^|/)(\.env(\..+)?|session\.dpapi|.*\.pem)$|(^|/)(\.alexa-bridge|private|node_modules)/")
SECRET = re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(ghp_[A-Za-z0-9]{30,}|AKIA[A-Z0-9]{16}"
                    r"|Atzr\|[A-Za-z0-9_-]{20,}|amzn1\.application-oa2-client\.[a-f0-9]{32})\b|"
                    r"^\s*(BRIDGE_WEBHOOK_KEY|BRIDGE_WEBHOOK_URL)\s*=\s*\S+")

names = subprocess.run(["git", "ls-files", "-z"], capture_output=True, text=True, check=True).stdout.split("\0")
findings = []
for name in filter(None, names):
    if PRIVATE.search(name) and not name.endswith(".env.example"):
        findings.append((name, 0, "private_file_tracked"))
    try:
        lines = open(name, encoding="utf-8").read().splitlines()
    except (UnicodeDecodeError, OSError):
        continue
    findings += [(name, n, "possible_credential") for n, line in enumerate(lines, 1) if SECRET.search(line)]
for name, line, reason in findings:
    print("%s:%d %s" % (name, line, reason))
print("tracked files checked: %d, findings: %d (heuristic, not proof)" % (len(names) - 1, len(findings)))
sys.exit(1 if findings else 0)
