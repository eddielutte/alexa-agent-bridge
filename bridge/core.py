"""Resumable, cross-platform setup for a personal Alexa-to-agent skill.

Every step is safe to re-run. Output is JSON lines with fixed event names; tokens, codes,
cookies, card contents, client secrets and webhook keys are never printed or logged.
Non-secret progress lives in <BRIDGE_HOME>/state.json; secrets only ever touch 0600 files
under <BRIDGE_HOME>/private/ and are deleted as soon as they have been used.

The Amazon sign-in and device registration follow the protocol used by aioamazondevices
(Apache-2.0); see NOTICE.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
LAMBDA = ROOT / "lambda"
SIGNERS = ROOT / "allowed_signers"  # release-signing keys trusted by the installed release
for path in (str(LAMBDA), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)
import build_model  # noqa: E402
import messaging  # noqa: E402
import retail  # noqa: E402

ASK_VERSION = "2.30.7"
DEFAULT_PROFILE = "alexa-bridge"
LAMBDA_FILES = ["lambda_function.py", "storage.py", "retail.py", "messaging.py", "countries.json",
                "requirements.txt"]
RETIRED_FILES = ["carriers.json"]
DEVICE_TYPE, APP_NAME, APP_VERSION = "A2IVLV5VM2W81", "AioAmazonDevices", "2.2.663733.0"
PERMISSION_HINTS = {
    "RATE_EXCEEDED": "Amazon is rate-limiting hosted-skill creation. Wait a few minutes and run this step again.",
    "RESOURCE_LIMIT_EXCEEDED": "This developer account has reached its Alexa-hosted skill limit. Delete an unused hosted skill first."}


class Stop(Exception):
    """The owner must act. `category` is fixed; `message` is plain language for them."""
    def __init__(self, category, message):
        super().__init__(message)
        self.category, self.message = category, message


class Failed(Exception):
    def __init__(self, category):
        super().__init__(category)
        self.category = category


def home():
    return Path(os.environ.get("BRIDGE_HOME") or Path.home() / ".alexa-bridge")


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def private_dir():
    folder = home() / "private"
    folder.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(home(), 0o700)
        os.chmod(folder, 0o700)
    return folder


def write_private(name, data):
    path = private_dir() / name
    path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def read_private(name, consume=False):
    path = private_dir() / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    finally:
        if consume:
            path.unlink()


def drop_private(name):
    path = private_dir() / name
    if path.exists():
        path.unlink()


def load_state():
    path = home() / "state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"phases": {}}


def save_state(state):
    state["checkout"] = str(ROOT)  # read by the maintenance skill; holds no secrets
    home().mkdir(parents=True, exist_ok=True)
    path = home() / "state.json"
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def done(state, phase, **facts):
    state["phases"][phase] = {"at": int(time.time()), **facts}
    save_state(state)
    emit("phase_done", phase=phase, **facts)


def need(state, *keys):
    missing = [k for k in keys if not state.get(k)]
    if missing:
        raise Stop("earlier_step_missing", "Run the earlier setup step first (missing: %s)." % ", ".join(missing))


# --- ASK CLI -----------------------------------------------------------------------------------

def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, **kwargs)


class Ask:
    """Thin ASK CLI wrapper. Parses JSON from stdout and never echoes ASK output."""

    def __init__(self, profile, runner=run):
        self.profile, self.runner = profile, runner

    def call(self, *args, missing_ok=False):
        exe = shutil.which("ask") or "ask"
        result = self.runner([exe, "smapi", *args, "--profile", self.profile])
        text = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            if missing_ok and ("404" in text or "NOT_FOUND" in text.upper()):
                return None
            raise Failed("ask_" + args[0].replace("-", "_") + "_failed")
        start = (result.stdout or "").find("{")
        return json.loads(result.stdout[start:]) if start >= 0 else {}


def ensure_vendor_in_profile(profile, vendor_id):
    """ASK CLI drops vendorId from create bodies and injects the profile's vendor_id."""
    path = Path.home() / ".ask" / "cli_config"
    config = json.loads(path.read_text(encoding="utf-8"))
    entry = config.get("profiles", {}).get(profile)
    if entry is None:
        raise Stop("developer_sign_in", "The ASK CLI profile is missing. Run: ask configure --profile " + profile)
    if entry.get("vendor_id") != vendor_id:
        entry["vendor_id"] = vendor_id
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")


# --- Phases ------------------------------------------------------------------------------------

def preflight(state, install=True, runner=run):
    checks = {"python": sys.version_info >= (3, 9)}
    for tool in ("node", "npm", "git"):
        checks[tool] = bool(shutil.which(tool))
    if not shutil.which("ask") and install and checks["npm"]:
        result = runner([shutil.which("npm"), "install", "-g", "ask-cli@" + ASK_VERSION])
        if result.returncode != 0:
            raise Stop("ask_cli_install", "Installing ASK CLI failed. Make sure npm can install global packages "
                       "without root (for example: npm config set prefix ~/.local) and run preflight again.")
    ask = shutil.which("ask")
    version = run([ask, "--version"]).stdout.strip() if ask else ""
    checks["ask_cli"] = version == ASK_VERSION
    try:
        socket.create_connection(("api.amazon.com", 443), timeout=5).close()
        checks["network"] = True
    except OSError:
        checks["network"] = False
    emit("preflight", system=platform.system(), checks=checks, ask_cli_version=version or None)
    failing = [k for k, ok in checks.items() if not ok]
    if failing:
        raise Stop("preflight", "These tools are missing or not working: %s. Install them and run preflight again."
                   % ", ".join(failing))
    done(state, "preflight")


def choose(state, invocation, country, display_name=None, locale=None, language=None, test_echo=None, agent=None):
    country = country.upper()
    saved = state.get("choices") or {}
    if (saved.get("invocation"), saved.get("country")) == (invocation, country):  # a re-run keeps omitted options
        locale, language = locale or saved.get("locale"), language or saved.get("language")
        display_name = display_name or saved.get("display_name")
        test_echo = saved.get("test_echo") if test_echo is None else test_echo
    language = language or "en"
    if country not in retail.COUNTRIES:
        raise Stop("country_unknown", "That country is not in the country table yet: " + ", ".join(sorted(retail.COUNTRIES)))
    row = retail.COUNTRIES[country]
    if len(row["locales"]) > 1 and not locale:
        raise Stop("choose_language", "%s has more than one Alexa language (%s). Choose one."
                   % (row["name"], ", ".join(row["locales"])))
    locale = locale or row["locales"][0]
    if locale not in row["locales"]:
        raise Stop("locale_unknown", "Use one of: " + ", ".join(row["locales"]))
    if not (ROOT / "models" / (language + ".json")).exists() or not (LAMBDA / ("lang_" + language + ".json")).exists():
        raise Stop("language_pack_missing", "There is no language pack for '%s' yet. Draft one first." % language)
    if agent is None:
        agent = state.get("choices", {}).get("agent_name", "")
    agent = agent.strip()
    problems = build_model.invocation_problems(invocation, language)
    if problems:
        raise Stop("invocation_rejected", "That skill name will not work with Alexa: " + "; ".join(problems) + ".")
    if not agent:
        raise Stop("choose_agent", "Add --agent with the spoken name the owner calls you by (your agent profile gives it).")
    if len(agent) > 40 or any(c in agent for c in "{}\r\n"):
        raise Stop("agent_name_invalid", "Use a short spoken name for the agent, up to 40 characters, without braces or line breaks.")
    display_name = display_name or " ".join(w.rstrip(".").upper() if w.endswith(".") else w.capitalize()
                                            for w in invocation.split()).replace(" A I", " AI")
    state["choices"] = {"invocation": invocation, "display_name": display_name, "country": country,
                        "locale": locale, "language": language, "test_echo": test_echo, "agent_name": agent,
                        "country_status": row["status"]}
    done(state, "choose", **state["choices"])


def probe(url, timeout=8):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    try:
        return urllib.request.build_opener(NoRedirect).open(url, timeout=timeout).status
    except urllib.error.HTTPError as error:
        return error.code
    except (OSError, ValueError):
        return None


def doctor(state, fetch=probe):
    need(state, "choices")
    country = state["choices"]["country"]
    row = retail.COUNTRIES[country]
    results = {"retail_host": fetch("https://alexa." + row["domain"] + "/api/devices-v2/device"),
               "messaging_host": fetch("https://" + retail.messaging_host(country) + "/v1/skillmessages/users/x"),
               "sign_in_page": fetch("https://www.amazon.com/ap/signin")}
    verdict = {"retail_host": results["retail_host"] in (301, 302, 401, 403),
               "messaging_host": results["messaging_host"] in (401, 403, 404),
               "sign_in_page": results["sign_in_page"] is not None}
    notes = home() / "country-notes.json"
    record = json.loads(notes.read_text(encoding="utf-8")) if notes.exists() else {}
    kept = {"notes": record[country]["notes"]} if record.get(country, {}).get("notes") else {}
    record[country] = dict(kept, at=int(time.time()), status=row["status"], probes=results, ok=verdict)
    home().mkdir(parents=True, exist_ok=True)
    notes.write_text(json.dumps(record, indent=2), encoding="utf-8")
    emit("doctor", country=country, country_status=row["status"], probes=results, ok=verdict)
    if not all(verdict.values()):
        raise Stop("country_probe_failed", "Amazon's services for %s did not answer as expected. "
                   "Setup has stopped before anything was created; the details are in country-notes.json." % row["name"])
    done(state, "doctor")


def dev_auth(state, profile=DEFAULT_PROFILE, runner=run, vendor=None):
    ask = Ask(profile, runner)
    try:
        vendors = ask.call("get-vendor-list").get("vendors", [])
    except Failed:
        raise Stop("developer_sign_in", "Sign in to the Amazon developer account: run `ask configure --profile %s`, "
                   "complete the browser sign-in and consent, answer 'n' to linking an AWS account, then run "
                   "this step again." % profile) from None
    if not vendors:
        raise Stop("developer_profile", "This Amazon account has no Alexa developer profile yet. Finish registering at "
                   "https://developer.amazon.com/ (accept the agreement, complete the profile), then run this step again.")
    if vendor:
        if vendor not in [v.get("id") for v in vendors]:
            raise Stop("vendor_unknown", "That organisation ID is not on this account. Use one of: %s."
                       % ", ".join("%s (%s)" % (v.get("name", "?"), v.get("id")) for v in vendors))
        state["vendor_id"] = vendor
    if len(vendors) > 1 and not state.get("vendor_id"):
        raise Stop("choose_vendor", "This account belongs to several developer organisations: %s. Ask the owner "
                   "which one, then run dev-auth again with --vendor <ID>."
                   % ", ".join("%s (%s)" % (v.get("name", "?"), v.get("id")) for v in vendors))
    vendor_id = state.get("vendor_id") or vendors[0]["id"]
    ensure_vendor_in_profile(profile, vendor_id)
    state.update(profile=profile, vendor_id=vendor_id)
    done(state, "dev_auth")


def create(state, runner=run, sleep=time.sleep):
    need(state, "choices", "profile", "vendor_id")
    ask, choices = Ask(state["profile"], runner), state["choices"]
    if state.get("skill_id") and ask.call("get-skill-status", "-s", state["skill_id"], missing_ok=True):
        emit("skill_exists", skill_id=state["skill_id"])
        return done(state, "create", skill_id=state["skill_id"])
    permission = ask.call("get-alexa-hosted-skill-user-permissions", "--hosted-skill-permission-type", "newSkill")
    status = permission.get("status")
    if status == "NEW_USER_REGISTRATION_REQUIRED":
        url = permission.get("actionUrl", "")
        login = "https://www.amazon.com/ap/signin?" + urllib.parse.urlencode({
            "openid.ns": "http://specs.openid.net/auth/2.0", "openid.mode": "checkid_setup",
            "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
            "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
            "openid.assoc_handle": "amzn_dante_us", "openid.pape.max_auth_age": "7200",
            "openid.return_to": url + "?vendor_id=" + state["vendor_id"] + "&redirect_url=http://127.0.0.1:9090/captcha"})
        raise Stop("captcha", "Amazon needs a one-time check before your first Alexa-hosted skill. Open this page, "
                   "sign in to the developer account and solve the CAPTCHA, then run this step again: " + login)
    if status != "ALLOWED":
        raise Stop("hosted_permission", PERMISSION_HINTS.get(status, "Amazon did not allow a new hosted skill (%s)." % status))
    body = {"vendorId": state["vendor_id"],
            "manifest": {"publishingInformation": {"locales": {choices["locale"]: {"name": choices["display_name"]}}},
                         "apis": {"custom": {}}},
            "hosting": {"alexaHosted": {"runtime": "PYTHON_3_8",
                                        "region": retail.COUNTRIES[choices["country"]]["region"]}}}
    path = private_dir() / "create.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    try:
        skill_id = ask.call("create-skill-for-vendor", "--manifest", "file:" + str(path))["skillId"]
    finally:
        path.unlink()
    state["skill_id"] = skill_id
    save_state(state)
    wait_for(ask, skill_id, lambda s: s.get("hostedSkillProvisioning", {}).get("lastUpdateRequest", {}).get("status"),
             "provisioning", sleep)
    done(state, "create", skill_id=skill_id)


def wait_for(ask, skill_id, read, what, sleep, attempts=60, delay=5):
    for _ in range(attempts):
        status = read(ask.call("get-skill-status", "-s", skill_id))
        if status == "SUCCEEDED":
            return
        if status == "FAILED":
            raise Failed(what + "_failed")
        sleep(delay)
    raise Failed(what + "_timeout")


def render_config(state):
    choices = state["choices"]
    return {"schema": 1, "skill_id": state["skill_id"], "display_name": choices["display_name"],
            "country": choices["country"], "locale": choices["locale"], "language": choices["language"],
            "test_device_name": choices.get("test_echo") or "", "agent_name": choices.get("agent_name") or ""}


def stage_lambda(target, state):
    """Copy the deployable files into a hosted repo checkout; return whether anything changed."""
    lambda_dir = Path(target) / "lambda"
    language = state["choices"]["language"]
    files = {name: (LAMBDA / name).read_bytes() for name in LAMBDA_FILES + ["lang_" + language + ".json"]}
    files["bridge_config.json"] = (json.dumps(render_config(state), indent=2) + "\n").encode("utf-8")
    changed = False
    for name, data in files.items():
        path = lambda_dir / name
        data = data.replace(b"\r\n", b"\n")
        # Compare ignoring line endings: a checkout may use CRLF while the repository stores LF.
        if not path.exists() or path.read_bytes().replace(b"\r\n", b"\n") != data:
            path.write_bytes(data)
            changed = True
    for name in RETIRED_FILES:
        if (lambda_dir / name).exists():
            (lambda_dir / name).unlink()
            changed = True
    return changed


def hosted_git(ask, skill_id, runner=run):
    repo = ask.call("get-alexa-hosted-skill-metadata", "--skill-id", skill_id)["alexaHosted"]["repository"]["url"]
    cred = ask.call("generate-credentials-for-alexa-hosted-skill", "--skill-id", skill_id,
                    "--repository-url", repo, "--repository-type", "GIT")["repositoryCredentials"]
    tmp = Path(tempfile.mkdtemp(prefix="bridge-"))
    store = tmp / "cred"
    store.write_text("username=%s\npassword=%s\n" % (cred["username"], cred["password"]))
    os.chmod(store, stat.S_IRUSR | stat.S_IWUSR)
    helper = "!f() { cat '%s'; }; f" % store.as_posix()
    def git(*args):
        result = runner(["git", "-c", "credential.helper=", "-c", "credential.helper=" + helper, *args])
        if result.returncode != 0:
            raise Failed("git_" + next(a for a in args if a in ("clone", "add", "commit", "push", "diff")) + "_failed")
        return result.stdout
    return repo, tmp, git


def deploy(state, runner=run, sleep=time.sleep):
    need(state, "choices", "profile", "skill_id")
    ask, choices, skill_id = Ask(state["profile"], runner), state["choices"], state["skill_id"]
    repo, tmp, git = hosted_git(ask, skill_id, runner)
    try:
        work = tmp / "repo"
        git("clone", "-q", repo, str(work))
        staged = False
        if stage_lambda(work, state):
            git("-C", str(work), "add", "-A")
            staged = bool(git("-C", str(work), "diff", "--cached", "--name-only").strip())
        if staged:
            git("-C", str(work), "-c", "user.name=alexa-bridge", "-c", "user.email=bridge@invalid",
                "commit", "-q", "-m", "Deploy Alexa bridge")
            git("-C", str(work), "push", "-q", "origin", "HEAD:master")
            sleep(15)
            wait_for(ask, skill_id, lambda s: s.get("hostedSkillDeployment", {}).get("lastUpdateRequest", {}).get("status"),
                     "deployment", sleep)
            emit("code_deployed")
        else:
            emit("code_unchanged")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    model = private_dir() / "model.json"
    model.write_text(build_model.build(choices["language"], choices["invocation"], state.get("echoes")),
                     encoding="utf-8")
    try:
        ask.call("set-interaction-model", "-s", skill_id, "-g", "development", "-l", choices["locale"],
                 "--interaction-model", "file:" + str(model))
    finally:
        model.unlink()
    wait_for(ask, skill_id, lambda s: s.get("interactionModel", {}).get(choices["locale"], {})
             .get("lastUpdateRequest", {}).get("status"), "model_build", sleep)
    ask.call("set-skill-enablement", "-s", skill_id, "-g", "development")
    done(state, "deploy", invocation=choices["invocation"], locale=choices["locale"])


def messaging_credentials(ask, skill_id, sleep=time.sleep):
    """Return (client_id, client_secret) in memory; unlock them via a manifest toggle on 404."""
    reply = ask.call("get-skill-credentials", "-s", skill_id, missing_ok=True)
    if reply is None:
        manifest = ask.call("get-skill-manifest", "-s", skill_id, "-g", "development")
        original = manifest["manifest"].get("permissions")
        for permissions in ([{"name": "alexa::alerts:timers:skill:readwrite"}], original):
            changed = json.loads(json.dumps(manifest))
            if permissions:
                changed["manifest"]["permissions"] = permissions
            else:
                changed["manifest"].pop("permissions", None)
            path = private_dir() / "manifest.json"
            path.write_text(json.dumps(changed), encoding="utf-8")
            try:
                ask.call("update-skill-manifest", "-s", skill_id, "-g", "development", "--manifest", "file:" + str(path))
            finally:
                path.unlink()
            wait_for(ask, skill_id, lambda s: s.get("manifest", {}).get("lastUpdateRequest", {}).get("status"),
                     "manifest", sleep)
        reply = ask.call("get-skill-credentials", "-s", skill_id, missing_ok=True)
    creds = (reply or {}).get("skillMessagingCredentials") or {}
    if not creds.get("clientId") or not creds.get("clientSecret"):
        raise Stop("messaging_credentials", "Amazon has not issued Skill Messaging credentials for this skill. In the "
                   "developer console, open the skill's Permissions page, switch any permission on and off again, "
                   "then run this step again.")
    return creds["clientId"], creds["clientSecret"]


def simulate(ask, skill_id, locale, utterance, sleep=time.sleep):
    sim = ask.call("simulate-skill", "-s", skill_id, "-g", "development", "--input-content", utterance,
                   "--device-locale", locale, "--session-mode", "FORCE_NEW_SESSION")
    for _ in range(30):
        sleep(2)
        result = ask.call("get-skill-simulation", "-s", skill_id, "-i", sim["id"], "-g", "development")
        if result.get("status") != "IN_PROGRESS":
            break
    invocations = ((result.get("result") or {}).get("skillExecutionInfo") or {}).get("invocations") or []
    body = invocations[0].get("invocationResponse", {}).get("body", {}) if invocations else {}
    speech = body.get("response", {}).get("outputSpeech", {}).get("text")
    card = body.get("response", {}).get("card") or {}
    return speech, card.get("content")


# --- Amazon sign-in (PKCE on Amazon's global page) -----------------------------------------------

def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def client_id(serial):
    return (serial + "#" + DEVICE_TYPE).encode().hex()


def signin_start(state, open_browser=False):
    need(state, "choices")
    serial, verifier = uuid.uuid4().hex.upper(), b64(secrets.token_bytes(32))
    write_private("pkce.json", {"serial": serial, "verifier": verifier})
    query = {"openid.return_to": "https://www.amazon.com/ap/maplanding", "openid.oa2.code_challenge_method": "S256",
             "openid.assoc_handle": "amzn_dp_project_dee_ios",
             "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
             "pageId": "amzn_dp_project_dee_ios", "accountStatusPolicy": "P1",
             "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select", "openid.mode": "checkid_setup",
             "openid.ns.oa2": "http://www.amazon.com/ap/ext/oauth/2", "openid.oa2.client_id": "device:" + client_id(serial),
             "language": state["choices"]["locale"].replace("-", "_"),
             "openid.ns.pape": "http://specs.openid.net/extensions/pape/1.0",
             "openid.oa2.code_challenge": b64(hashlib.sha256(verifier.encode()).digest()),
             "openid.oa2.scope": "device_auth_access", "openid.ns": "http://specs.openid.net/auth/2.0",
             "openid.pape.max_auth_age": "0", "openid.oa2.response_type": "code"}
    url = "https://www.amazon.com/ap/signin?" + urllib.parse.urlencode(query)
    if open_browser:
        webbrowser.open(url)
    emit("signin_url", url=url, next="Sign in on this Amazon page. When it lands on /ap/maplanding, save the full "
         "address to a private file and run: signin finish --from-file <file>")


def read_redirect(source, prompt=None):
    if source == "prompt":
        import getpass
        return (prompt or getpass.getpass)("Paste the full sign-in address (hidden): ").strip()
    if source == "clipboard":
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        text = root.clipboard_get()
        root.clipboard_clear()
        root.update()
        root.destroy()
        return text.strip()
    path = Path(source)
    try:
        return path.read_text(encoding="utf-8").strip()
    finally:
        path.unlink()


def register(code, pkce, transport=retail.request):
    body = {"requested_extensions": ["device_info", "customer_info"],
            "cookies": {"website_cookies": [], "domain": ".amazon.com"},
            "registration_data": {"domain": "Device", "app_version": APP_VERSION, "device_type": DEVICE_TYPE,
                                  "device_name": "%FIRST_NAME%'s%DUPE_STRATEGY_1ST%" + APP_NAME, "os_version": "18.5",
                                  "device_serial": pkce["serial"], "device_model": "iPhone", "app_name": APP_NAME,
                                  "software_version": "35602678"},
            "auth_data": {"use_global_authentication": "true", "client_id": client_id(pkce["serial"]),
                          "authorization_code": code, "code_verifier": pkce["verifier"], "code_algorithm": "SHA-256",
                          "client_domain": "DeviceLegacy"},
            "user_context_map": {"frc": b64(secrets.token_bytes(313))},
            "requested_token_type": ["bearer", "mac_dms", "website_cookies", "store_authentication_cookie"]}
    status, _, raw = transport("POST", "https://api.amazon.com/auth/register",
                               {"Content-Type": "application/json", "User-Agent": retail.UA},
                               json.dumps(body).encode(), timeout=20.0)
    if status != 200:
        raise Stop("signin_refused", "Amazon did not accept that sign-in (HTTP %d). Start the sign-in again." % status)
    return retail.json_body(raw)["response"]["success"]


def signin_finish(state, source, transport=retail.request, renew=retail.renew):
    need(state, "choices")
    pkce = read_private("pkce.json", consume=True)
    if not pkce:
        raise Stop("signin_not_started", "Start the Amazon sign-in first (signin start).")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(read_redirect(source)).query)
    code = (query.get("openid.oa2.authorization_code") or [None])[0]
    if not code:
        raise Stop("signin_incomplete", "That address has no sign-in code. Finish signing in, then copy the full "
                   "address of the /ap/maplanding page.")
    success = register(code, pkce, transport)
    domain = retail.COUNTRIES[state["choices"]["country"]]["domain"]
    seed = {"schema": 1, "domain": domain, "customer_id": "pending", "locale": state["choices"]["locale"],
            "refresh_token": success["tokens"]["bearer"]["refresh_token"], "cookies": {},
            "cookies_refreshed_at": 0, "devices": []}
    reply = retail.token_request(seed, "auth_cookies", transport)
    cookies = {}
    for cookie_domain, rows in reply.get("response", {}).get("tokens", {}).get("cookies", {}).items():
        if cookie_domain.lstrip(".") in (domain, "alexa." + domain):
            cookies.update({r["Name"]: r["Value"].strip('"') for r in rows if "Name" in r and "Value" in r})
    try:
        seed["customer_id"] = retail.retail_get(dict(seed, cookies=cookies), "/api/users/me", transport)["id"]
    except retail.AuthRequired:
        raise Stop("signin_wrong_country", "This Amazon account does not belong to %s. Choose the country your "
                   "Echos are registered in." % retail.COUNTRIES[state["choices"]["country"]]["name"]) from None
    verified = renew(dict(seed), transport)
    write_private("seed.json", seed)
    echoes = sorted(d["name"] for d in verified.get("devices", []) if d.get("name"))
    state["echoes"] = echoes
    region = success.get("extensions", {}).get("customer_info", {}).get("home_region")
    done(state, "signin", home_region=region, echoes=echoes)


# --- Enrolment, routine text and tests ---------------------------------------------------------

def enrol(state, runner=run, sleep=time.sleep, sender=messaging.send):
    need(state, "choices", "profile", "skill_id")
    seed = read_private("seed.json")
    if not seed:
        raise Stop("signin_needed", "Sign in to Amazon first (signin start, then signin finish).")
    stored = read_private("webhook.json") or {}
    url = os.environ.get("BRIDGE_WEBHOOK_URL") or stored.get("url", "")
    key = os.environ.get("BRIDGE_WEBHOOK_KEY") or stored.get("key", "")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment or not key or any(c in key for c in "\r\n")):
        raise Stop("webhook_missing", "The agent routine's webhook URL and key are not available. Store them as the "
                   "secrets BRIDGE_WEBHOOK_URL and BRIDGE_WEBHOOK_KEY (or run `webhook-set` in your own terminal), "
                   "then run this step again.")
    choices = state["choices"]
    test_echo = choices.get("test_echo") or (state.get("echoes") or [""])[0]
    if not test_echo:
        raise Stop("choose_test_echo", "Choose which Echo should play the connection test.")
    ask = Ask(state["profile"], runner)
    client, secret = messaging_credentials(ask, state["skill_id"], sleep)
    invocation = choices["invocation"]
    speech, card = simulate(ask, state["skill_id"], choices["locale"], "open " + invocation, sleep)
    if not card:
        speech, card = simulate(ask, state["skill_id"], choices["locale"],
                                "ask %s to repair cloud authentication" % invocation, sleep)
    try:
        card = json.loads(card or "")
    except ValueError:
        card = {}
    if card.get("skill_id") != state["skill_id"] or not card.get("setup_token") or not card.get("user_id"):
        raise Failed("setup_card_unavailable")
    config = {"client_id": client, "client_secret": secret}
    token = messaging.messaging_token(config, sender)
    payload = dict(config, registration=seed, webhook_url=url, webhook_key=key, test_device_name=test_echo)
    messaging.send_message(card["user_id"], token, {
        "kind": "setup", "setup_token": card["setup_token"],
        "payload": json.dumps(payload, separators=(",", ":"))}, sender, host=retail.messaging_host(choices["country"]))
    del config, payload, token, secret
    sleep(8)
    status, _ = simulate(ask, state["skill_id"], choices["locale"], "ask %s to check cloud status" % invocation, sleep)
    renewed, _ = simulate(ask, state["skill_id"], choices["locale"], "ask %s to renew cloud authentication" % invocation, sleep)
    drop_private("seed.json")
    drop_private("webhook.json")
    emit("enrolment_checked", status_reply=status, renewal_reply=renewed,
         next="Run deploy once more so Alexa learns your Echo names, then run test.")
    if renewed != "Cloud authentication renewal succeeded.":
        done(state, "enrol", test_echo=test_echo, renewal="pending")
        raise Stop("renewal_failed", "Enrolment was sent, but the cloud could not renew the Amazon sign-in yet. "
                   "Wait a minute and run: bridge test --status. Once it no longer reports a sign-in problem, "
                   "carry on with deploy and test.")
    done(state, "enrol", test_echo=test_echo)


def webhook_set(prompt=None):
    """Owner types the routine's URL and key into hidden prompts; they go to a 0600 file, never to output."""
    import getpass
    prompt = prompt or getpass.getpass
    url, key = prompt("Routine webhook URL (hidden): ").strip(), prompt("Routine webhook key (hidden): ").strip()
    if not url.startswith("https://") or not key:
        raise Stop("webhook_invalid", "That did not look like the routine's HTTPS webhook URL and key. Try again.")
    write_private("webhook.json", {"url": url, "key": key})
    emit("webhook_stored", host=urllib.parse.urlsplit(url).hostname, key_length=len(key))


def pack_check(language):
    """Compare a drafted language pack and model template with the reviewed English ones."""
    english = json.loads((LAMBDA / "lang_en.json").read_text(encoding="utf-8"))
    try:
        pack = json.loads((LAMBDA / ("lang_" + language + ".json")).read_text(encoding="utf-8"))
        model = json.loads((ROOT / "models" / (language + ".json")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Stop("pack_unreadable", "lang_%s.json or models/%s.json is missing or is not valid JSON." % (language, language))
    problems = []
    missing = sorted(set(english["strings"]) - set(pack.get("strings", {})))
    if missing:
        problems.append("missing strings: " + ", ".join(missing))
    for key, text in pack.get("strings", {}).items():
        expected = set(re.findall(r"{(\w+)}", english["strings"].get(key, "")))
        if set(re.findall(r"{(\w+)}", text)) != expected:
            problems.append("string %s must keep the placeholders %s" % (key, sorted(expected) or "none"))
    intents = {i["name"]: i for i in model.get("languageModel", {}).get("intents", [])}
    for name, prefix in pack.get("carriers", {}).items():
        samples = intents.get(name, {}).get("samples", [])
        if not samples or not any(s.endswith("{request}") for s in samples):
            problems.append("carrier %s has no '{request}' sample in the model" % name)
    if pack.get("status") != "proven" and pack.get("status") != "untested":
        problems.append('status must be "untested" until a real request has passed')
    emit("pack_check", language=language, status=pack.get("status"), problems=problems)
    if problems:
        raise Stop("pack_problems", "The %s language pack needs fixes before use." % language)


def routine_text(state):
    need(state, "choices")
    template = (ROOT / "routine-template.md").read_text(encoding="utf-8")
    hosts = sorted(messaging.MESSAGING_HOSTS)
    text = template.format(display_name=state["choices"]["display_name"], language=state["choices"]["locale"],
                           hosts=", ".join("`" + h + "`" for h in hosts), max_chars=4000, max_bytes=6000, expiry=120)
    print(text)


def connection_test(state, runner=run, sleep=time.sleep, status_only=False):
    need(state, "choices", "profile", "skill_id")
    ask, choices = Ask(state["profile"], runner), state["choices"]
    utterance = "ask %s to %s" % (choices["invocation"], "check cloud status" if status_only else "run the connection test")
    speech, _ = simulate(ask, state["skill_id"], choices["locale"], utterance, sleep)
    emit("skill_reply", utterance=utterance, reply=speech,
         next=None if status_only else "Listen to the test Echo and confirm the whole sentence was heard.")
    if not status_only:
        done(state, "test", reply=speech)


def status(state):
    emit("status", choices=state.get("choices"), skill_id=state.get("skill_id"), checkout=str(ROOT),
         phases=sorted(state.get("phases", {})), echoes=state.get("echoes"),
         next=next((p for p in ("preflight", "choose", "doctor", "dev_auth", "create", "deploy", "signin", "enrol", "test")
                    if p not in state.get("phases", {})), None))


def update(state, tag, allow_unsigned=False, runner=run, sleep=time.sleep):
    repo = ROOT  # git finds the enclosing repository from here
    # Verified with the signers file of the release already installed, before the new one is checked out.
    verify = ["-c", "gpg.ssh.allowedSignersFile=" + SIGNERS.as_posix(), "verify-tag", tag]
    for args in (["fetch", "--tags", "-q"], verify, ["checkout", "-q", tag]):
        if args is verify and allow_unsigned:
            continue
        if runner(["git", "-C", str(repo), *args]).returncode != 0:
            raise Stop("update_refused", "Release %s could not be fetched or its signature could not be verified. "
                       "Nothing was deployed." % tag)
    emit("release_checked_out", tag=tag)
    deploy(state, runner, sleep)


def uninstall(state, confirm, runner=run):
    if not confirm:
        raise Stop("confirm_uninstall", "This deletes the Alexa skill and its stored data. Run again with --yes to confirm.")
    if state.get("skill_id") and state.get("profile"):
        Ask(state["profile"], runner).call("delete-skill", "-s", state["skill_id"], missing_ok=True)
    shutil.rmtree(home(), ignore_errors=True)
    emit("uninstalled", next="Delete the agent's Alexa bridge routine and its BRIDGE_WEBHOOK_URL and "
         "BRIDGE_WEBHOOK_KEY secrets.")
