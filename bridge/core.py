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
from . import sshsig  # noqa: E402
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
    """Setup can't continue on its own. `category` is fixed; `message` explains it to the agent. `say`, when set,
    is what to tell the owner, in plain words; `do` is the agent's next action."""
    def __init__(self, category, message, say=None, do=None):
        super().__init__(message)
        self.category, self.message, self.say, self.do = category, message, say, do


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

def run(args, quiet=False, **kwargs):
    if args and args[0] == "git":  # never let planted replace refs change what a verified commit means
        kwargs.setdefault("env", dict(os.environ, GIT_NO_REPLACE_OBJECTS="1"))
    if quiet:  # no pipes to hold open, e.g. for a clipboard tool that forks a background owner
        return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              **kwargs)
    return subprocess.run(args, capture_output=True, **{"text": True, **kwargs})


_ASK_CHECKED = {}


def tools_dir():
    return home() / "tools"


def ask_exe(runner=run, install=True):
    """The setup tool's own ASK CLI under ~/.alexa-bridge/tools: no root or PATH changes, and reinstalled
    automatically when a computer update has removed it. An existing `ask` of the pinned version also works."""
    def pinned(path):
        if path not in _ASK_CHECKED:
            try:
                _ASK_CHECKED[path] = run([path, "--version"]).stdout.strip() == ASK_VERSION
            except OSError:
                _ASK_CHECKED[path] = False
        return _ASK_CHECKED[path]
    for path in (str(tools_dir() / "bin" / "ask"), str(tools_dir() / "ask.cmd"), shutil.which("ask")):
        if path and Path(path).exists() and pinned(path):
            return path
    npm = shutil.which("npm")
    if not (install and npm):
        return None
    emit("installing", tool="ask-cli", version=ASK_VERSION)
    result = runner([npm, "install", "-g", "--prefix", str(tools_dir()), "ask-cli@" + ASK_VERSION], timeout=600)
    if result.returncode != 0:
        raise Stop("ask_cli_install", "npm couldn't install ASK CLI %s into %s. Check the network and that npm "
                   "works, then run this step again." % (ASK_VERSION, tools_dir()))
    _ASK_CHECKED.clear()
    return ask_exe(runner, install=False)


class Ask:
    """Thin ASK CLI wrapper. Parses JSON from stdout and never echoes ASK output."""

    def __init__(self, profile, runner=run):
        self.profile, self.runner = profile, runner

    def call(self, *args, missing_ok=False):
        exe = ask_exe(self.runner, install=False) or "ask"
        result = self.runner([exe, "smapi", *args, "--profile", self.profile])
        text = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            if missing_ok and ("404" in text or "NOT_FOUND" in text.upper()):
                return None
            raise Failed("ask_" + args[0].replace("-", "_") + "_failed")
        start = (result.stdout or "").find("{")
        return json.loads(result.stdout[start:]) if start >= 0 else {}


SIGNED_OUT = ("profile", "configure", "unauthorized", "invalid_grant", "access token", "refresh token", "401")


def developer_signed_out(profile, runner=run):
    """True only when ASK CLI clearly says its sign-in is missing or rejected; a network blip isn't that."""
    exe = ask_exe(runner, install=False)
    if not exe:
        return False
    try:
        result = runner([exe, "smapi", "get-vendor-list", "--profile", profile])
    except OSError:
        return False
    text = ((result.stdout or "") + (result.stderr or "")).lower()
    return result.returncode != 0 and any(marker in text for marker in SIGNED_OUT)


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
    ask = ask_exe(runner, install=install and checks["npm"])
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
    if saved and saved.get("country") != country:
        if state.get("skill_id"):
            raise Stop("country_locked", "The skill was created for %s, and its hosting region and language can't "
                       "change. To use another country, remove the bridge (MAINTENANCE.md) and set up again."
                       % saved.get("country"))
        for phase in ("doctor", "signin", "test_echo"):  # these depend on the country
            state["phases"].pop(phase, None)
    if saved and saved.get("invocation") != invocation and "deploy" in state["phases"]:
        state["deploy_pending"] = "name"  # the voice model must learn the new name
    state["choices"] = {"invocation": invocation, "display_name": display_name, "country": country,
                        "locale": locale, "language": language, "test_echo": test_echo, "agent_name": agent,
                        "country_status": row["status"]}
    done(state, "choose", **state["choices"])


def choose_test_echo(state, name=None):
    """Pick the Echo that plays the connection test from the signed-in account's list (alone if there's one)."""
    need(state, "choices")
    echoes = state.get("echoes") or []
    if not echoes and "signin" in state.get("phases", {}):
        raise Stop("no_echoes", "The signed-in Amazon account lists no Echo. Once one is set up in the Alexa app, "
                   "run `python3 -m bridge signin check`, then setup.",
                   say="Your Amazon account doesn't show any Echo yet. Please set one up in the Alexa app (with "
                       "this same account), then tell me.",
                   do="python3 -m bridge signin check, then python3 -m bridge setup")
    if not echoes:
        raise Stop("signin_needed", "The Echo list comes from the Amazon sign-in. Do that first.")
    name = name or state["choices"].get("test_echo")
    match = [e for e in echoes if name and e.casefold() == name.strip().casefold()]
    if not match and len(echoes) == 1 and not name:
        match = echoes
    if not match:
        raise Stop("choose_test_echo", "Choose the test Echo from the signed-in account's list.",
                   say="Which Echo should I use for a quick sound test? Pick the one you'll be standing next to: "
                       + ", ".join(echoes) + ".",
                   do='python3 -m bridge choose --test-echo "<exact name from the list>"')
    state["choices"]["test_echo"] = match[0]
    done(state, "test_echo", test_echo=match[0])


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


def network_timings(transport=retail.request, clock=time.monotonic, tries=3):
    """Round trips to Amazon's token endpoint, made the way setup makes them (an empty form, refused).
    Milliseconds per try; None where the connection failed."""
    timings = []
    for _ in range(tries):
        start = clock()
        try:
            transport("POST", "https://api.amazon.com/auth/token",
                      {"User-Agent": retail.UA, "Content-Type": "application/x-www-form-urlencoded"}, b"",
                      timeout=SETUP_TIMEOUT)
            timings.append(int((clock() - start) * 1000))
        except retail.TransportError:
            timings.append(None)
    return timings


def doctor(state, fetch=probe, network=False, timer=network_timings):
    need(state, "choices")
    country = state["choices"]["country"]
    row = retail.COUNTRIES[country]
    if network:
        timings = timer()
        slow = any(t is None or t > SLOW_MS for t in timings)
        emit("network", host="api.amazon.com", timings_ms=timings, slow=slow,
             next="Setup allows %d s per Amazon call. Sign in when this computer is less busy." % SETUP_TIMEOUT
             if slow else None)
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


DEV_SIGNIN, DEV_SIGNIN_LIMIT = "dev-signin.json", 900
CONFIGURE_ANSWERS = (("browser", "used the browser to sign in", "y"), ("aws", "link your aws account", "n"),
                     ("overwrite", "overwrite", "y"))


def _plain(text):
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text).lower()


def configure_reply(text, answered):
    """The answer to an `ask configure` question in `text` not answered yet: (key, reply) or None.
    It confirms the browser sign-in the bridge itself opened, replaces only the bridge's own profile, and
    never links an AWS account."""
    plain = _plain(text)
    return next(((key, reply) for key, phrase, reply in CONFIGURE_ANSWERS if key not in answered and phrase in plain),
                None)


def unknown_question(text):
    """A finished-looking question (a line starting '?') that configure_reply doesn't know, such as a choice of
    developer organisation: those need the owner at a terminal."""
    return any(line.strip().startswith("?") for line in _plain(text).splitlines())


def drive_configure(profile, limit=DEV_SIGNIN_LIMIT, quiet_for=3):
    """`bridge _dev-signin`: run `ask configure` in a pseudo-terminal on the browser's display, answer its
    questions, and record only its progress (never its output) in dev-signin.json. Runs detached."""
    import pty
    import select
    home().mkdir(parents=True, exist_ok=True)
    status = home() / DEV_SIGNIN
    def note(stage):
        status.write_text(json.dumps({"pid": os.getpid(), "stage": stage, "at": int(time.time())}))
    exe, display = ask_exe(install=False), browser_display()
    if not exe:
        return note("failed")
    note("waiting_for_owner")
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.execvpe(exe, [exe, "configure", "--profile", profile], dict(os.environ, **({"DISPLAY": display}
                                                                                         if display else {})))
        finally:
            os._exit(127)
    seen, answered, end, last = "", set(), time.monotonic() + limit, time.monotonic()
    stage = None
    while time.monotonic() < end:
        if not select.select([fd], [], [], 1)[0]:
            if seen and time.monotonic() - last >= quiet_for and unknown_question(seen):
                stage = "needs_terminal"  # a question this tool doesn't answer, e.g. a choice of organisation
                break
            continue
        try:
            chunk = os.read(fd, 4096).decode("utf-8", "replace")
        except OSError:
            break
        if not chunk:
            break
        seen, last = (seen + chunk)[-4000:], time.monotonic()
        reply = configure_reply(seen, answered)
        if reply:
            os.write(fd, (reply[1] + "\r").encode())
            answered.add(reply[0])
            seen = ""
            note("finishing")
    else:
        stage = "failed"
    if stage:
        os.kill(pid, 15)
    _, code = os.waitpid(pid, 0)
    note(stage or ("done" if os.waitstatus_to_exitcode(code) == 0 else "failed"))


def _dev_signin_record():
    try:
        return json.loads((home() / DEV_SIGNIN).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _dev_signin_running():
    record = _dev_signin_record()
    if record.get("stage") in (None, "done", "failed", "needs_terminal"):
        return False
    if time.time() - record.get("at", 0) > DEV_SIGNIN_LIMIT + 60:  # stale, or a process ID reused after a restart
        return False
    try:
        os.kill(record["pid"], 0)
        return True
    except (OSError, KeyError, TypeError):
        return False


def start_dev_signin(profile):
    """Start the developer sign-in in the background, so a cut-off command doesn't end it. False if this
    computer can't (no pseudo-terminals, e.g. Windows) or ASK CLI asked something it can't answer: then a
    terminal is needed."""
    if os.name != "posix" or not ask_exe(install=False) or _dev_signin_record().get("stage") == "needs_terminal":
        return False
    if not _dev_signin_running():
        subprocess.Popen([sys.executable, "-B", "-m", "bridge", "_dev-signin", "--profile", profile], cwd=str(ROOT),
                         start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    return True


def dev_auth(state, profile=DEFAULT_PROFILE, runner=run, vendor=None, starter=start_dev_signin):
    ask = Ask(profile, runner)
    try:
        vendors = ask.call("get-vendor-list").get("vendors", [])
    except Failed:
        if starter(profile):
            raise Stop("developer_sign_in", "The Amazon developer sign-in has opened in your browser on display %s. "
                       "Hand control to the owner; the tool answers ASK CLI's questions itself. When they're done, "
                       "run this step again." % (browser_display() or "?"),
                       do="hand control (see your profile's Browser handover), then run python3 -m bridge setup",
                       say="Please sign in to the Amazon developer site on the page I've opened, with the same "
                           "Amazon account, and click Allow. Tell me when it says you can close the page.") from None
        raise Stop("developer_sign_in", "Sign in to the Amazon developer account: run `ask configure --profile %s` "
                   "in a terminal, complete the browser sign-in, answer 'y' to confirm you used the browser and "
                   "'n' to linking an AWS account, then run this step again." % profile) from None
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
    if (home() / DEV_SIGNIN).exists():
        (home() / DEV_SIGNIN).unlink()
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
    for attempt in range(attempts):
        status = read(ask.call("get-skill-status", "-s", skill_id))
        if status == "SUCCEEDED":
            return
        if status == "FAILED":
            raise Failed(what + "_failed")
        if attempt and attempt % 6 == 0:  # about every 30 s, so a long wait doesn't look like a hang
            emit("waiting", on=what, seconds=attempt * delay,
                 next="Amazon is still working. If this command gets cut off, run it again; it carries on.")
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
    state.pop("deploy_pending", None)
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
    pkce = {"serial": uuid.uuid4().hex.upper(), "verifier": b64(secrets.token_bytes(32))}
    write_private("pkce.json", pkce)
    url = signin_url(state, pkce)
    if open_browser:
        webbrowser.open(url)
    emit_signin_url(url)


def emit_signin_url(url):
    emit("signin_url", url=url, next="Open this page and hand control to the owner. When it lands on /ap/maplanding, "
         "copy the address bar to the clipboard and run: setup (or signin finish --from-clipboard / --from-file F)")


def signin_url(state, pkce):
    """Amazon's sign-in page for this pending sign-in; the same page again if it's needed again."""
    serial, verifier = pkce["serial"], pkce["verifier"]
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
    return "https://www.amazon.com/ap/signin?" + urllib.parse.urlencode(query)


# (read, clear) commands for desktops without tkinter; the first one installed is used.
CLIPBOARD_TOOLS = [(["xclip", "-o", "-selection", "clipboard"], ["xclip", "-i", "-selection", "clipboard"]),
                   (["xsel", "--clipboard", "--output"], ["xsel", "--clipboard", "--clear"]),
                   (["wl-paste", "--no-newline"], ["wl-copy", "--clear"]),
                   (["pbpaste"], ["pbcopy"])]


def _tk_clipboard():
    display = browser_display()
    try:
        import tkinter
        root = tkinter.Tk(screenName=display)
    except Exception:  # no tkinter, or no display for it
        return None
    try:
        root.withdraw()
        try:
            text = root.clipboard_get()
        except tkinter.TclError:  # empty
            text = ""
    finally:
        root.destroy()
    def clear():
        try:
            again = tkinter.Tk(screenName=display)
            again.withdraw()
            again.clipboard_clear()
            again.update()
            again.destroy()
        except Exception:  # best effort: the address has already been read
            pass
    return text, clear


BROWSERS = ("chrome", "chromium", "chromium-browse", "firefox", "firefox-esr")


def browser_display():
    """The X display the visible browser runs on, which can differ from this shell's; else $DISPLAY.
    Read from the browser process's own environment (same user, Linux /proc). BRIDGE_DISPLAY overrides it."""
    if os.environ.get("BRIDGE_DISPLAY"):
        return os.environ["BRIDGE_DISPLAY"]
    proc = Path("/proc")
    for entry in sorted(proc.glob("[0-9]*")) if proc.is_dir() else []:
        try:
            if (entry / "comm").read_text().strip() not in BROWSERS:
                continue
            found = [v[8:] for v in (entry / "environ").read_bytes().split(b"\0") if v.startswith(b"DISPLAY=")]
        except OSError:
            continue
        if found:
            return found[0].decode()
    return os.environ.get("DISPLAY")


def _x11():
    """libX11 through ctypes, and an open display; None without either."""
    import ctypes
    import ctypes.util
    display_name = browser_display()
    if not display_name:
        return None
    try:
        x = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
    except OSError:
        return None
    ulong, ptr = ctypes.c_ulong, ctypes.c_void_p
    x.XOpenDisplay.restype, x.XOpenDisplay.argtypes = ptr, [ctypes.c_char_p]
    x.XDefaultRootWindow.restype, x.XDefaultRootWindow.argtypes = ulong, [ptr]
    x.XCreateSimpleWindow.restype = ulong
    x.XCreateSimpleWindow.argtypes = [ptr, ulong, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
                                      ctypes.c_uint, ulong, ulong]
    x.XInternAtom.restype, x.XInternAtom.argtypes = ulong, [ptr, ctypes.c_char_p, ctypes.c_int]
    x.XConvertSelection.argtypes = [ptr, ulong, ulong, ulong, ulong, ulong]
    x.XSetSelectionOwner.argtypes = [ptr, ulong, ulong, ulong]
    x.XPending.argtypes, x.XNextEvent.argtypes = [ptr], [ptr, ptr]
    x.XGetWindowProperty.argtypes = [ptr, ulong, ulong, ctypes.c_long, ctypes.c_long, ctypes.c_int, ulong,
                                     ctypes.POINTER(ulong), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ulong),
                                     ctypes.POINTER(ulong), ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]
    for name in ("XFree", "XFlush", "XCloseDisplay"):
        getattr(x, name).argtypes = [ptr]
    x.XDestroyWindow.argtypes = [ptr, ulong]
    display = x.XOpenDisplay(display_name.encode())
    if not display:
        return None
    window = x.XCreateSimpleWindow(display, x.XDefaultRootWindow(display), 0, 0, 1, 1, 0, 0, 0)
    return ctypes, x, display, window


def _x11_clipboard(timeout=3.0):
    """The X11 CLIPBOARD as UTF-8, read with the standard library only (no xclip). (text, clear) or None."""
    opened = _x11()
    if not opened:
        return None
    ctypes, x, display, window = opened
    ulong = ctypes.c_ulong

    class Selection(ctypes.Structure):
        _fields_ = [("type", ctypes.c_int), ("serial", ulong), ("send_event", ctypes.c_int),
                    ("display", ctypes.c_void_p), ("requestor", ulong), ("selection", ulong), ("target", ulong),
                    ("property", ulong), ("time", ulong)]

    class Event(ctypes.Union):
        _fields_ = [("type", ctypes.c_int), ("xselection", Selection), ("pad", ctypes.c_long * 24)]

    clipboard = x.XInternAtom(display, b"CLIPBOARD", 0)
    try:
        target, prop = x.XInternAtom(display, b"UTF8_STRING", 0), x.XInternAtom(display, b"BRIDGE_CLIP", 0)
        x.XConvertSelection(display, clipboard, target, prop, window, 0)
        x.XFlush(display)
        event, deadline, text = Event(), time.monotonic() + timeout, ""
        while time.monotonic() < deadline:
            if not x.XPending(display):
                time.sleep(0.05)
                continue
            x.XNextEvent(display, ctypes.byref(event))
            if event.type != 31:  # SelectionNotify
                continue
            if event.xselection.property:
                kind, fmt, count, after = ulong(), ctypes.c_int(), ulong(), ulong()
                data = ctypes.POINTER(ctypes.c_ubyte)()
                x.XGetWindowProperty(display, window, prop, 0, 1 << 20, 1, 0, ctypes.byref(kind), ctypes.byref(fmt),
                                     ctypes.byref(count), ctypes.byref(after), ctypes.byref(data))
                if data:
                    text = ctypes.string_at(data, count.value).decode("utf-8", "replace")
                    x.XFree(data)
            break
    finally:
        x.XDestroyWindow(display, window)
        x.XCloseDisplay(display)

    def clear():  # owning the selection with a window that then closes leaves the clipboard empty
        try:
            again = _x11()
        except Exception:  # best effort: the address has already been read
            return
        if again:
            _, lib, shown, owner = again
            lib.XSetSelectionOwner(shown, lib.XInternAtom(shown, b"CLIPBOARD", 0), owner, 0)
            lib.XFlush(shown)
            lib.XDestroyWindow(shown, owner)
            lib.XCloseDisplay(shown)
    return text, clear


def _clipboard_once(runner):
    """(text, clear) from the first clipboard reader that works on this computer, or None if none does."""
    got = _tk_clipboard()
    if got:
        return got
    display = browser_display()
    env = dict(os.environ, DISPLAY=display) if display else None
    for read, clear in CLIPBOARD_TOOLS:
        if shutil.which(read[0]):
            try:
                result = runner(read, timeout=10, env=env)
            except (OSError, subprocess.SubprocessError):
                continue
            if result.returncode == 0:
                def wipe(clear=clear):
                    try:  # best effort, and without pipes: xclip keeps a background owner running
                        runner(clear, quiet=True, timeout=10, env=env)
                    except (OSError, subprocess.SubprocessError):
                        pass
                return result.stdout, wipe
    return _x11_clipboard()


def read_clipboard(runner=run, want=None, wait=0, sleep=time.sleep, clock=time.monotonic):
    """Clipboard text. With `want`, waits up to `wait` seconds for matching text and clears the clipboard only
    when it matches, so anything else the owner had copied is left alone. Clearing is best effort: a desktop
    that syncs its clipboard elsewhere (WSLg did, in testing) may put the text back. The sign-in code it holds
    works once and is used straight away."""
    end = clock() + wait
    while True:
        got = _clipboard_once(runner)
        if got is None:
            raise Stop("clipboard_unavailable", "No clipboard could be read here (tkinter, xclip, xsel, wl-paste, "
                       "pbpaste or an X display). Save the address to a private file and use --from-file, or "
                       "--from-prompt. The sign-in is still pending.")
        text, clear = got
        if text.strip() and (want is None or want(text)):
            clear()
            return text
        if clock() >= end:
            return text if want is None else ""
        sleep(2)


def read_redirect(source, prompt=None, clipboard=None):
    if source == "prompt":
        import getpass
        return (prompt or getpass.getpass)("Paste the full sign-in address (hidden): ").strip()
    if source == "clipboard":
        if clipboard:
            return clipboard().strip()
        return read_clipboard(want=lambda text: "/ap/maplanding" in text, wait=30).strip()
    path = Path(source)
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise Stop("signin_incomplete", "That file couldn't be read. Save the full /ap/maplanding address to a "
                   "private file and run signin finish again; the sign-in is still pending.") from None
    finally:
        if path.exists():
            path.unlink()


SETUP_TIMEOUT, SLOW_MS = 20.0, 5000


def amazon(method, url, headers=None, data=None, timeout=SETUP_TIMEOUT, retry=True):
    """The setup tool's Amazon calls: a slow computer gets 20 s and one retry after a connection failure.
    The hosted skill keeps retail.request's short default."""
    try:
        return retail.request(method, url, headers, data, timeout=timeout)
    except retail.TransportError:
        if not retry:
            raise
        return retail.request(method, url, headers, data, timeout=timeout)


def register(code, pkce, transport=amazon):
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
    try:  # never retried: the code works once, and a lost reply may still have registered
        status, _, raw = transport("POST", "https://api.amazon.com/auth/register",
                                   {"Content-Type": "application/json", "User-Agent": retail.UA},
                                   json.dumps(body).encode(), retry=False)
    except retail.TransportError:
        raise Stop("signin_unreachable", "Amazon couldn't be reached to finish the sign-in. Start the sign-in again; "
                   "if a spare \"AioAmazonDevices\" entry appears in the owner's Amazon devices list, they can remove "
                   "it.") from None
    if status != 200:
        raise Stop("signin_refused", "Amazon did not accept that sign-in (HTTP %d). Start the sign-in again." % status)
    return retail.json_body(raw)["response"]["success"]


def signin_finish(state, source, transport=amazon, renew=retail.renew, clipboard=None):
    need(state, "choices")
    pkce = read_private("pkce.json")
    if not pkce:
        raise Stop("signin_not_started", "Start the Amazon sign-in first (signin start).")
    # The pending sign-in is only used up once a code has been read; a bad source can simply be retried.
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(read_redirect(source, clipboard=clipboard)).query)
    code = (query.get("openid.oa2.authorization_code") or [None])[0]
    if not code:
        emit_signin_url(signin_url(state, pkce))  # in case the page was closed: the same sign-in, still pending
        raise Stop("signin_incomplete", "No sign-in address was found. When the browser is on the /ap/maplanding "
                   "page, copy its full address to the clipboard (or a private file), then run this step again. "
                   "The sign-in is still pending.",
                   do="copy the /ap/maplanding address bar to the clipboard, then run python3 -m bridge setup",
                   say="Has Amazon shown the “Sorry, we couldn't find that page” dog yet? If not, please finish "
                       "signing in and tell me when you see it.")
    drop_private("pkce.json")
    success = register(code, pkce, transport)
    domain = retail.COUNTRIES[state["choices"]["country"]]["domain"]
    # Kept from here on, so a failed check is repeated with `signin check`, not a new sign-in and registration.
    write_private("seed.json", {"schema": 1, "domain": domain, "customer_id": "pending",
                                "locale": state["choices"]["locale"],
                                "refresh_token": success["tokens"]["bearer"]["refresh_token"], "cookies": {},
                                "cookies_refreshed_at": 0, "devices": []})
    state["phases"].pop("signin", None)
    state["home_region"] = success.get("extensions", {}).get("customer_info", {}).get("home_region")
    save_state(state)
    signin_check(state, transport, renew)


def signin_check(state, transport=amazon, renew=retail.renew):
    """Identify the saved registration's account and check that it renews. Safe to repeat."""
    need(state, "choices")
    seed = read_private("seed.json")
    if not seed:
        raise Stop("signin_needed", "Sign in to Amazon first (signin start, then signin finish).")
    retry = Stop("signin_check_failed", "Amazon accepted the sign-in, but it couldn't be checked yet. It's saved: "
                 "run `python3 -m bridge signin check` in a minute. Don't sign in again.")
    country = retail.COUNTRIES[state["choices"]["country"]]
    if seed["domain"] != country["domain"]:
        drop_private("seed.json")
        raise Stop("signin_needed", "The country changed after signing in. Sign in to Amazon again.")
    try:
        if seed["customer_id"] == "pending":
            reply = retail.token_request(seed, "auth_cookies", transport)
            cookies = {}
            for cookie_domain, rows in reply.get("response", {}).get("tokens", {}).get("cookies", {}).items():
                if cookie_domain.lstrip(".") in (seed["domain"], "alexa." + seed["domain"]):
                    cookies.update({r["Name"]: r["Value"].strip('"') for r in rows if "Name" in r and "Value" in r})
            try:
                seed["customer_id"] = retail.retail_get(dict(seed, cookies=cookies), "/api/users/me", transport)["id"]
            except retail.AuthRequired:
                drop_private("seed.json")
                raise Stop("signin_wrong_country", "This Amazon account does not belong to %s. Choose the country "
                           "your Echos are registered in." % country["name"]) from None
            write_private("seed.json", seed)
        verified = renew(dict(seed), transport)
    except retail.AuthRequired:
        drop_private("seed.json")
        raise Stop("signin_refused", "Amazon no longer accepts this sign-in. Start the sign-in again.") from None
    except retail.TransportError:
        raise retry from None
    echoes = sorted(d["name"] for d in verified.get("devices", []) if d.get("name"))
    if "deploy" in state["phases"] and echoes != state.get("echoes"):
        state["deploy_pending"] = "echo names"  # the voice model must learn the new list
    state["echoes"] = echoes
    done(state, "signin", home_region=state.get("home_region"), echoes=echoes)


# --- Enrolment, routine text and tests ---------------------------------------------------------

def enrol(state, runner=run, sleep=time.sleep, sender=messaging.send):
    need(state, "choices", "profile", "skill_id")
    seed = read_private("seed.json")
    if not seed:
        raise Stop("signin_needed", "Sign in to Amazon first (signin start, then signin finish).")
    if "signin" not in state["phases"]:
        raise Stop("signin_check_failed", "The Amazon sign-in hasn't been checked yet. Run `python3 -m bridge "
                   "signin check` first.")
    url, key = webhook_values()
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
    emit("enrolment_checked", status_reply=status, renewal_reply=renewed)
    if renewed != "Cloud authentication renewal succeeded.":
        done(state, "enrol", test_echo=test_echo, renewal="pending")
        raise Stop("renewal_failed", "Enrolment was sent, but the cloud could not renew the Amazon sign-in yet. "
                   "Wait a minute and run: bridge test --status. Once it no longer reports a sign-in problem, "
                   "run setup again.", do="wait a minute, run python3 -m bridge test --status, then setup")
    done(state, "enrol", test_echo=test_echo)


def webhook_values():
    """The routine's URL and key, from the agent's secret store (environment) or `webhook-set`'s private file."""
    stored = read_private("webhook.json") or {}
    url = os.environ.get("BRIDGE_WEBHOOK_URL") or stored.get("url", "")
    key = os.environ.get("BRIDGE_WEBHOOK_KEY") or stored.get("key", "")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment or not key or any(c in key for c in "\r\n")):
        raise Stop("webhook_missing", "The routine's webhook URL and key aren't available, or the URL isn't HTTPS. "
                   "Create the routine (`routine-text`), then have the owner store its URL and key as the secrets "
                   "BRIDGE_WEBHOOK_URL and BRIDGE_WEBHOOK_KEY (see your profile), and run this step again.",
                   say="I've made the routine that will answer your questions. It has two connection codes that "
                       "only you can copy: click each link I've posted and paste the code into the matching secure "
                       "card. I never see them. Tell me when both are saved.",
                   do="give the owner the routine's URL and key links and your secret store's boxes (see your "
                      "profile's Connecting the routine), then run python3 -m bridge setup")
    return url, key


def host_hint(url):
    """Only the last two labels of the webhook's host: on some platforms the full host name is itself secret."""
    return ".".join((urllib.parse.urlsplit(url).hostname or "").split(".")[-2:])


def webhook_check(state):
    """Confirm the routine's URL and key are in place without revealing them: base domain and key length only."""
    url, key = webhook_values()
    done(state, "webhook", domain=host_hint(url), key_length=len(key))


def webhook_set(prompt=None):
    """Owner types the routine's URL and key into hidden prompts; they go to a 0600 file, never to output."""
    import getpass
    prompt = prompt or getpass.getpass
    url, key = prompt("Routine webhook URL (hidden): ").strip(), prompt("Routine webhook key (hidden): ").strip()
    if not url.startswith("https://") or not key:
        raise Stop("webhook_invalid", "That did not look like the routine's HTTPS webhook URL and key. Try again.")
    write_private("webhook.json", {"url": url, "key": key})
    emit("webhook_stored", domain=host_hint(url), key_length=len(key))


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
         say=None if status_only else "Did you hear all of this on %s: “This is %s speaking directly from the "
             "hosted service. This is the end of the cloud connection test.”?" % (
                 choices.get("test_echo") or "your Echo", choices["display_name"]))
    if not status_only:
        done(state, "test", reply=speech)


# The owner's part comes first, in one sitting: choices, both Amazon sign-ins, the test Echo and the routine's
# secrets. Deploy then runs once, with the Echo names already known; the owner returns only to listen.
PHASES = ("preflight", "choose", "doctor", "signin", "test_echo", "dev_auth", "create", "webhook", "deploy", "enrol",
          "test")
COMMANDS = {"choose": 'choose --name "<name>" --country <code> --agent "<agent name>"', "doctor": "doctor --network",
            "signin": "signin start", "signin finish": "signin finish --from-clipboard",
            "test_echo": 'choose --test-echo "<Echo name>"', "dev_auth": "dev-auth", "webhook": "webhook-check"}


def next_step(state):
    if state.get("deploy_pending"):
        return "deploy"
    step = next((p for p in PHASES if p not in state.get("phases", {})), None)
    if step == "signin" and (private_dir() / "seed.json").exists():
        return "signin check"
    if step == "signin" and (private_dir() / "pkce.json").exists():
        return "signin finish"
    return step


def command_for(step):
    return None if step is None else "python3 -m bridge " + COMMANDS.get(step, step)


def status(state):
    step = next_step(state)
    emit("status", choices=state.get("choices"), skill_id=state.get("skill_id"), checkout=str(ROOT),
         release=(state.get("release") or {}).get("tag"), deploy_pending=state.get("deploy_pending"),
         phases=sorted(state.get("phases", {})), echoes=state.get("echoes"), next=command_for(step))


def setup(state, runner=run, sleep=time.sleep, clipboard=None):
    """Run each next step until one needs the owner or setup is finished. Safe to run again at any point."""
    phases, profile = state.get("phases", {}), state.get("profile") or DEFAULT_PROFILE
    if "preflight" in phases and shutil.which("npm"):
        ask_exe(runner)  # a computer update may have removed it
    if "dev_auth" in phases and next_step(state) is not None and developer_signed_out(profile, runner):
        phases.pop("dev_auth")  # a reset removed the developer sign-in: dev_auth asks for it again
    for _ in range(len(PHASES) + 3):
        step = next_step(state)
        emit("setup_step", step=step)
        if step is None:
            choices = state["choices"]
            emit("setup_complete", say="You're all set. On each Echo you want to use, say: “Alexa, ask %s, please "
                 "tell me a short joke.” The first time, Alexa asks which Echo it is: say its name, and it answers "
                 "straight after." % choices["display_name"],
                 next="Save the maintenance skill and give the owner the cheat sheet (SETUP.md step 6).")
            return
        if step == "preflight":
            preflight(state, runner=runner)
        elif step == "choose":
            raise Stop("choose_needed", "The owner's choices are needed.", do=command_for("choose"),
                       say="Three quick questions, and your answers are the go-ahead. 1. Name: you'll say “Alexa, "
                           "ask <suggested name>, please…”. Keep it or pick another? 2. Which country's Amazon do "
                           "you use for your Echos? 3. Is the Amazon account in your Alexa app the one you'll sign "
                           "in with?")
        elif step == "doctor":
            doctor(state, network=True)
        elif step == "signin":
            signin_start(state)
            raise Stop("signin_handover", "Open the signin_url above in your browser and hand control to the owner.",
                       do="open signin_url, hand control (see your profile's Browser handover); afterwards copy "
                          "the address bar to the clipboard and run python3 -m bridge setup",
                       say="Please sign in to Amazon on the page I've opened, with the account your Echos use. "
                           "When Amazon shows “Sorry, we couldn't find that page” with a dog, that means it "
                           "worked: tell me and I'll take over.")
        elif step == "signin finish":
            signin_finish(state, "clipboard", clipboard=clipboard)
        elif step == "signin check":
            signin_check(state)
        elif step == "test_echo":
            choose_test_echo(state)
        elif step == "dev_auth":
            dev_auth(state, profile, runner=runner)
        elif step == "create":
            create(state, runner, sleep)
        elif step == "webhook":
            webhook_check(state)
        elif step in ("deploy", "enrol"):
            (deploy if step == "deploy" else enrol)(state, runner, sleep)
        elif step == "test":
            raise Stop("ready_to_listen", "Run the connection test when the owner is by the test Echo.",
                       do="python3 -m bridge test",
                       say="Almost done. Please stand next to %s and tell me when you're there; I'll play a short "
                           "test sentence on it." % state["choices"].get("test_echo"))


RELEASE_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
GIT_TIMEOUT, FETCH_TIMEOUT = 60, 300


def release_version(tag):
    match = RELEASE_TAG.fullmatch(tag or "")
    if not match:
        raise Stop("release_name_invalid", "Release tags look like v1.2.3; '%s' doesn't." % tag)
    return tuple(int(n) for n in match.groups())


def _git(runner, *args, timeout=GIT_TIMEOUT, **kwargs):
    try:
        return runner(["git", "-C", str(ROOT), *args], timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        raise sshsig.BadSignature("git_timeout") from None


def verify_release(tag, runner=run, signers=None):
    """Check a release tag's SSH signature in-process (no ssh-keygen needed) against allowed_signers.
    The signed tag must name this tag and point at the commit it resolves to. Returns that commit."""
    release_version(tag)
    signers = signers or SIGNERS
    ref = "refs/tags/" + tag
    try:
        size = _git(runner, "cat-file", "-s", ref)
        if size.returncode != 0 or not str(size.stdout).strip().isdigit():
            raise sshsig.BadSignature("not_a_tag")
        if int(str(size.stdout).strip()) > sshsig.MAX_TAG_BYTES:
            raise sshsig.BadSignature("too_large")  # checked before reading the object itself
        result = _git(runner, "cat-file", "tag", ref, text=False)
        commit = _git(runner, "rev-parse", "--verify", ref + "^{commit}")
        if result.returncode != 0 or commit.returncode != 0:
            raise sshsig.BadSignature("not_a_tag")
        raw = result.stdout if isinstance(result.stdout, bytes) else result.stdout.encode("utf-8")
        keys = sshsig.allowed_keys(Path(signers).read_text(encoding="utf-8"))
        payload, armoured = sshsig.split_signed_tag(raw)
        signer = sshsig.verify(payload, armoured, keys)
        sha = commit.stdout.strip()
        header = sshsig.tag_header(payload)
        if (header.get("object"), header.get("type"), header.get("tag")) != (sha, "commit", tag):
            raise sshsig.BadSignature("tag_mismatch")  # e.g. an older signed release re-published under a new name
        checks = ["built-in"]
        if shutil.which("ssh-keygen"):  # an independent second opinion where available; both must agree
            second = _git(runner, "-c", "gpg.ssh.allowedSignersFile=" + Path(signers).as_posix(),
                          "-c", "gpg.minTrustLevel=fully", "verify-tag", ref)
            if second.returncode != 0:
                raise sshsig.BadSignature("ssh_keygen_disagrees")
            checks.append("ssh-keygen")
    except (sshsig.BadSignature, OSError, ValueError) as error:
        raise Stop("release_unverified", "Release %s is not signed by a key in allowed_signers (%s). Don't run anything "
                   "from it; tell the owner." % (tag, error)) from None
    emit("release_verified", tag=tag, signer=signer, commit=sha, checks=checks)
    return sha


def _checkout_matches(sha, runner):
    """The working tree is exactly the verified commit: HEAD matches and no tracked file is changed."""
    head = _git(runner, "rev-parse", "--verify", "HEAD")
    changed = _git(runner, "status", "--porcelain", "--untracked-files=no")
    return (head.returncode == 0 and head.stdout.strip() == sha
            and changed.returncode == 0 and not changed.stdout.strip())


def verify_install(state, tag, runner=run):
    """`bridge verify`: check the tag, check that this checkout IS that release, and record it as installed."""
    try:
        sha = verify_release(tag, runner)
        matches = _checkout_matches(sha, runner)
    except sshsig.BadSignature as error:
        raise Stop("release_unverified", "Release %s couldn't be checked (%s). Tell the owner." % (tag, error)) from None
    if not matches:
        raise Stop("release_not_checked_out", "This checkout isn't release %s exactly. Run "
                   "`git checkout -q --detach refs/tags/%s`, then verify again. Don't run anything else from it." % (tag, tag))
    state["release"] = {"tag": tag, "commit": sha}
    save_state(state)
    emit("release_installed", tag=tag, commit=sha)


def _installed_release(state, runner):
    """The verified release that is installed now. Fails closed if it can't be established."""
    recorded = state.get("release") or {}
    if recorded.get("tag") and recorded.get("commit"):
        if not _checkout_matches(recorded["commit"], runner):
            raise Stop("release_baseline_unknown", "This checkout no longer matches the recorded release %s. Tell the "
                       "owner; nothing was deployed." % recorded["tag"])
        return recorded["tag"]
    # Older installs have no record: the tag on HEAD must itself be a verified release that matches HEAD.
    described = _git(runner, "describe", "--tags", "--exact-match", "--match", "v*", "HEAD")
    tag = described.stdout.strip() if described.returncode == 0 else ""
    if not RELEASE_TAG.fullmatch(tag):
        raise Stop("release_baseline_unknown", "The installed release can't be identified. Run `python3 -m bridge verify "
                   "--tag <installed tag>` first; nothing was deployed.")
    try:
        sha = verify_release(tag, runner)
    except Stop:
        raise Stop("release_baseline_unknown", "The installed release %s doesn't verify. Tell the owner; nothing was "
                   "deployed." % tag) from None
    if not _checkout_matches(sha, runner):
        raise Stop("release_baseline_unknown", "This checkout isn't release %s exactly. Tell the owner; nothing was "
                   "deployed." % tag)
    state["release"] = {"tag": tag, "commit": sha}
    save_state(state)
    return tag


def _fresh_deploy():
    """Deploy with the newly checked-out code in a new process, never with code loaded from the old release."""
    return subprocess.run([sys.executable, "-B", "-m", "bridge", "deploy"], cwd=str(ROOT)).returncode


def latest_release(runner=run):
    """The newest release-shaped tag on the repository. Only a name: it still has to verify before anything runs."""
    try:
        listed = _git(runner, "ls-remote", "--tags", "--refs", "origin", "v*", timeout=FETCH_TIMEOUT)
    except sshsig.BadSignature:
        listed = None
    tags = [line.rsplit("refs/tags/", 1)[-1].strip() for line in (listed.stdout if listed else "").splitlines()]
    tags = [t for t in tags if RELEASE_TAG.fullmatch(t)]
    if not listed or listed.returncode != 0 or not tags:
        raise Stop("release_list_failed", "The repository's releases couldn't be listed. Check the network and try "
                   "again; nothing was changed.")
    return max(tags, key=release_version)


def update(state, tag=None, runner=run, deployer=_fresh_deploy):
    latest = tag is None
    tag = tag or latest_release(runner)
    wanted = release_version(tag)
    lock = home() / "update.lock"
    home().mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise Stop("update_in_progress", "Another update is running. If none is, delete %s and try again." % lock) from None
    try:
        os.close(handle)
        try:
            current = _installed_release(state, runner)
            if latest and wanted <= release_version(current):
                emit("up_to_date", tag=current)
                return
            if wanted <= release_version(current):
                raise Stop("update_refused", "Release %s isn't newer than the installed %s. Nothing was deployed."
                           % (tag, current))
            # Fetch only this tag, replacing any local copy; whatever arrives must still verify below.
            _git(runner, "fetch", "-q", "--no-tags", "origin", "+refs/tags/%s:refs/tags/%s" % (tag, tag),
                 timeout=FETCH_TIMEOUT)
        except sshsig.BadSignature:
            raise Stop("update_refused", "Release %s couldn't be fetched in time. Nothing was deployed." % tag) from None
        try:
            # Checked with the signers file of the release already installed, before the new one is checked out.
            sha = verify_release(tag, runner)
        except Stop:
            raise Stop("update_refused", "Release %s is not signed by a key in the installed allowed_signers. "
                       "Nothing was deployed." % tag) from None
        try:
            checked_out = _git(runner, "checkout", "-q", "--detach", sha)
        except sshsig.BadSignature:
            checked_out = None
        if not checked_out or checked_out.returncode != 0:
            raise Stop("update_refused", "Release %s could not be checked out. Nothing was deployed." % tag)
        state["release"] = {"tag": tag, "commit": sha}
        state["deploy_pending"] = tag  # cleared by a deploy that finishes, so status shows an interrupted one
        save_state(state)
        emit("release_checked_out", tag=tag, commit=sha)
    finally:
        lock.unlink()
    code = deployer()
    if code == 2:
        raise Stop("deploy_stopped", "Release %s is checked out, but deploy stopped (see above). Run `python3 -m bridge "
                   "deploy` again once that's resolved." % tag)
    if code != 0:
        raise Failed("deploy_failed")


def uninstall(state, confirm, runner=run):
    if not confirm:
        raise Stop("confirm_uninstall", "This deletes the Alexa skill and its stored data. Run again with --yes to confirm.")
    if state.get("skill_id") and state.get("profile"):
        Ask(state["profile"], runner).call("delete-skill", "-s", state["skill_id"], missing_ok=True)
    shutil.rmtree(home(), ignore_errors=True)
    emit("uninstalled", next="Delete the agent's Alexa bridge routine and the two secrets that hold its address "
         "and key, if your agent can.")
