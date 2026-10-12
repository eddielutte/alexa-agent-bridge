"""Resumable, cross-platform setup for a personal Alexa-to-agent skill.

Every step is safe to re-run. Output is JSON lines with fixed event names; tokens, codes,
cookies, card contents, client secrets and webhook keys are never printed or logged.
Non-secret progress lives in <BRIDGE_HOME>/state.json; secrets only ever touch 0600 files
under <BRIDGE_HOME>/private/ and are deleted as soon as they have been used.

The Amazon sign-in and device registration follow the protocol used by aioamazondevices
(Apache-2.0); see NOTICE.
"""
import base64
from datetime import datetime
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
    """Owner-only from creation, and replaced in one step, so a reader never sees a partly written file."""
    path = private_dir() / name
    temporary = path.with_name(path.name + ".tmp")
    handle = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(json.dumps(data))
    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(str(temporary), str(path))
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


def done(state, phase, next=None, **facts):
    state["phases"][phase] = {"at": int(time.time()), **facts}
    save_state(state)
    emit("phase_done", phase=phase, **facts, **({"next": next} if next else {}))


# The agent's browser is shared with its other assistants and tools: tidy it once each page has done its job.
CLOSE_TAB = ("Close the browser tab that held this sign-in now that its address has been used, keeping at least one "
             "tab open so the browser stays open (SETUP.md step 3).")
TIDY_BROWSER = ("No more browser steps are needed. Tidy your browser: close every tab you opened for the bridge, "
                "leaving one blank tab, and sign the owner out of Amazon there with the plain Sign out "
                "(SETUP.md step 4.6).")


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


DEV_SIGNIN, DEV_SIGNIN_LIMIT, DEV_CODE = "dev-signin.json", 900, "dev-code.json"
CONFIGURE_ANSWERS = (("browser", "used the browser to sign in", "y"), ("aws", "link your aws account", "n"),
                     ("overwrite", "overwrite", "y"))
# `ask configure --no-browser` prints Amazon's sign-in page instead of opening a browser on a display it has to
# guess; after "Allow", Amazon sends the browser to this page with the code in its address (ASK CLI 2.30.7).
NO_BROWSER_PAGE, NO_BROWSER_HOST = "ask-cli-no-browser.html", "ask-cli-static-content.s3-us-west-2.amazonaws.com"
CODE_PROMPT = "please enter the authorization code"


def _unstyled(text):
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)


def _plain(text):
    return _unstyled(text).lower()


def configure_url(text):
    """The sign-in page `ask configure --no-browser` printed, once its whole line has arrived; else None."""
    match = re.search(r"paste the following url to your browser:\s*(https://\S+)[ \t]*\r?\n", _unstyled(text), re.I)
    return match.group(1) if match else None


def wants_code(text):
    return CODE_PROMPT in _plain(text)


def dev_code(text):
    """The authorisation code in the address of ASK CLI's own page after the developer sign-in, or None.
    Any other page is refused, so a planted address can't sign ASK CLI in to another account."""
    parts = urllib.parse.urlsplit((text or "").strip())
    if parts.scheme != "https" or parts.hostname != NO_BROWSER_HOST or not parts.path.endswith("/" + NO_BROWSER_PAGE):
        return None
    code = urllib.parse.parse_qs(parts.query).get("code", [""])[0]
    return code if re.fullmatch(r"[A-Za-z0-9_.\-]{8,512}", code) else None


def configure_reply(text, answered):
    """The answer to an `ask configure` question in `text` not answered yet: (key, reply) or None.
    It confirms the browser sign-in the bridge itself opened, replaces only the bridge's own profile, and
    never links an AWS account."""
    plain = _plain(text)
    return next(((key, reply) for key, phrase, reply in CONFIGURE_ANSWERS if key not in answered and phrase in plain),
                None)


KNOWN_PROMPTS = (CODE_PROMPT,) + tuple(phrase for _, phrase, _ in CONFIGURE_ANSWERS)


def unknown_question(text):
    """A question (a line starting '?') that this tool doesn't answer, such as a choice of developer
    organisation: those need the owner at a terminal. ASK CLI redraws its prompts as they're typed and once
    answered, sometimes before a quiet network call (seen on a real agent), so known prompts never count."""
    lines = _plain(text).replace("\r", "\n").split("\n")
    return any(line.strip().startswith("?") and not any(known in line for known in KNOWN_PROMPTS) for line in lines)


def drive_configure(profile, limit=DEV_SIGNIN_LIMIT, quiet_for=3, exe=None):
    """`bridge _dev-signin`: run `ask configure --no-browser` in a pseudo-terminal, record the sign-in page it
    prints for the agent to open, type in the code that `dev-auth` hands over in a private file, and answer the
    other questions. dev-signin.json records only progress and that page, never ASK CLI's other output."""
    import pty
    import select
    home().mkdir(parents=True, exist_ok=True)
    status = home() / DEV_SIGNIN
    def note(stage, **extra):
        status.write_text(json.dumps({"pid": os.getpid(), "stage": stage, "at": int(time.time()), **extra}))
    drop_private(DEV_CODE)
    exe = exe or ask_exe(install=False)
    if not exe:
        return note("failed")
    note("starting")
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.execvp(exe, [exe, "configure", "--no-browser", "--profile", profile])
        finally:
            os._exit(127)
    seen, answered, end, last = "", set(), time.monotonic() + limit, time.monotonic()
    stage, url, typed = None, None, False
    while time.monotonic() < end:
        if not select.select([fd], [], [], 1)[0]:
            if url and not typed and wants_code(seen):
                try:
                    code = (read_private(DEV_CODE, consume=True) or {}).get("code")
                except (ValueError, AttributeError):
                    code = None  # unreadable: dev-auth copies it again
                if code:
                    os.write(fd, (code + "\r").encode())
                    typed, seen = True, ""
                    note("finishing")
                continue
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
        if not url and configure_url(seen):
            url = configure_url(seen)
            note("waiting_for_owner", url=url)
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
    drop_private(DEV_CODE)
    note(stage or ("done" if os.WIFEXITED(code) and os.WEXITSTATUS(code) == 0 else "failed"))  # any Python 3


def _dev_signin_record():
    try:
        record = json.loads((home() / DEV_SIGNIN).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _dev_signin_running():
    record = _dev_signin_record()
    if record.get("stage") in (None, "done", "failed", "needs_terminal"):
        return False
    at = record.get("at", 0)
    if not isinstance(at, (int, float)) or time.time() - at > DEV_SIGNIN_LIMIT + 60:  # stale, or a reused process ID
        return False
    try:
        os.kill(record["pid"], 0)
        return True
    except (OSError, KeyError, TypeError):
        return False


def start_dev_signin(profile):
    """Start the developer sign-in in the background, so a cut-off command doesn't end it. False if this
    computer can't (no pseudo-terminals, e.g. Windows) or ASK CLI asked something it can't answer: then a
    terminal is needed (for an hour, so a later run can try again)."""
    record = _dev_signin_record()
    asked = record.get("stage") == "needs_terminal" and time.time() - (record.get("at") or 0) < 3600
    if os.name != "posix" or not ask_exe(install=False) or asked:
        return False
    if not _dev_signin_running():
        subprocess.Popen([sys.executable, "-B", "-m", "bridge", "_dev-signin", "--profile", profile], cwd=str(ROOT),
                         start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    return True


def hand_over_dev_code(source=None, clipboard=None, sleep=time.sleep, wait=90):
    """If the background developer sign-in is waiting for its code, take the code from the address of Amazon's
    page after the sign-in (the clipboard, or `source` as in `signin finish`), hand it over and wait for ASK CLI
    to finish. True if a code was handed over. ASK CLI saves the sign-in before anything else it asks, so the
    caller checks the sign-in again however ASK CLI ended."""
    if not _dev_signin_running() or not _dev_signin_record().get("url"):
        return False
    if source:
        text = read_redirect(source)
    else:
        text = clipboard() if clipboard else read_clipboard(want=dev_code, wait=5)
    code = dev_code(text or "")
    if not code:
        return False
    write_private(DEV_CODE, {"code": code})
    for _ in range(int(wait / 2)):
        if _dev_signin_record().get("stage") in ("done", "failed", "needs_terminal"):
            break
        sleep(2)
    return True


def dev_auth(state, profile=DEFAULT_PROFILE, runner=run, vendor=None, starter=start_dev_signin, clipboard=None,
             sleep=time.sleep, source=None):
    ask, started = Ask(profile, runner), time.monotonic()
    def vendor_list():
        try:
            return ask.call("get-vendor-list").get("vendors", [])
        except Failed:
            return None
    vendors, handed = vendor_list(), False
    if vendors is None:
        waiting = _dev_signin_running() and bool(_dev_signin_record().get("url"))  # the agent has had the page
        if not starter(profile):
            raise Stop("developer_sign_in", "Sign in to the Amazon developer account in a terminal: run `ask "
                       "configure --no-browser --profile %s`, open the page it prints, have the owner sign in and "
                       "click Allow, then paste the code= value from the address of the page Amazon shows next. "
                       "Answer 'n' to linking an AWS account, then run this step again." % profile)
        for _ in range(10):  # ASK CLI prints its page within a few seconds of starting
            if _dev_signin_record().get("url"):
                break
            sleep(1)
        if waiting or source:  # only once the agent has had the page: no code can be for it before then
            handed = hand_over_dev_code(source, clipboard, sleep)  # e.g. no clipboard: says so, rather than looping
        elif not clipboard:
            discard_stale(dev_code)  # an old, used code on the clipboard would otherwise pass for the new one
        if handed:
            vendors = vendor_list()
            if vendors is None:
                raise Stop("developer_code_refused", "ASK CLI didn't accept that code; it may have expired. Run "
                           "this step again for a fresh sign-in page, and copy the code page's address straight away.",
                           do="run python3 -m bridge setup, open the new dev_signin_url and hand control again",
                           say="Amazon didn't accept that developer sign-in, usually because the code took too "
                               "long. Could you sign in once more on the page I'm opening?")
    if vendors is None:
        url = _dev_signin_record().get("url")
        if url:
            emit("dev_signin_url", url=url)
            raise Stop("developer_sign_in", "Open the dev_signin_url above in a new tab of your own browser and hand "
                       "control to the owner. When Amazon shows a page whose address contains %s, copy the address "
                       "bar to the clipboard and run this step again (or save it to a private file and run `dev-auth "
                       "--from-file F`). The code in it expires within minutes." % NO_BROWSER_PAGE,
                       do="open dev_signin_url in a new tab, hand control (see your profile's Browser handover); "
                          "afterwards copy the address bar to the clipboard and run python3 -m bridge setup",
                       say="Please sign in to the Amazon developer site on the page I've opened, with the same "
                           "Amazon account, and click Allow. Tell me as soon as it shows the code page.")
        if _dev_signin_record().get("stage") == "failed":
            raise Stop("developer_sign_in_failed", "ASK CLI's sign-in ended without printing its page. Run "
                       "`python3 -m bridge preflight`, then this step again. If it fails again, run `ask configure "
                       "--no-browser --profile %s` in a terminal and report what it prints." % profile)
        raise Stop("developer_sign_in_starting", "The developer sign-in is still starting. Run this step "
                   "again in a few seconds.", do="wait ten seconds, then run python3 -m bridge setup")
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
    done(state, "dev_auth", next=CLOSE_TAB if handed else None, seconds=int(time.monotonic() - started))


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


def deploy(state, runner=run, sleep=time.sleep, clock=time.monotonic):
    need(state, "choices", "profile", "skill_id")
    started = clock()
    emit("deploy_started", note="Amazon builds the skill and its voice model; this usually takes 2 to 15 minutes")
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
    done(state, "deploy", invocation=choices["invocation"], locale=choices["locale"], seconds=int(clock() - started))


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


PENDING_SIGNIN_SECONDS = 600


def signin_start(state, open_browser=False, now=None):
    """Start the Amazon speech sign-in, or reuse one started in the last 10 minutes: a page already open for it
    then stays valid. A new one each time made Amazon refuse codes from pages opened earlier (IKI-93)."""
    need(state, "choices")
    now = time.time() if now is None else now
    pkce = read_private("pkce.json") or {}
    if not pkce.get("serial") or not 0 <= now - pkce.get("at", 0) < PENDING_SIGNIN_SECONDS:
        pkce = {"serial": uuid.uuid4().hex.upper(), "verifier": b64(secrets.token_bytes(32)), "at": int(now)}
        write_private("pkce.json", pkce)
        discard_stale(lambda text: "/ap/maplanding" in text)  # an address left from an earlier, used sign-in
    url = signin_url(state, pkce)
    if open_browser:
        webbrowser.open(url)
    emit_signin_url(url)


def emit_signin_url(url):
    emit("signin_url", url=url, next="Open this page in a new tab and hand control to the owner. When it lands on "
         "/ap/maplanding, copy the address bar to the clipboard straight away and run: setup (or signin finish "
         "--from-clipboard / --from-file F). If the dog page is already showing, don't reopen this page: copy that "
         "address instead.")


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


def _tk_clipboard(display):
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


def browser_displays():
    """Every X display a copied address may be on, likeliest first: BRIDGE_DISPLAY, each running browser's own
    display (read from /proc; one computer can run a browser per agent, each on its own display) and $DISPLAY.
    Guessing one display failed on a real agent (IKI-93), so the clipboard is read on each in turn."""
    found = [os.environ.get("BRIDGE_DISPLAY")]
    proc = Path("/proc")
    for entry in sorted(proc.glob("[0-9]*"), key=lambda p: int(p.name)) if proc.is_dir() else []:
        try:
            if (entry / "comm").read_text().strip() in BROWSERS:
                found += [v[8:].decode() for v in (entry / "environ").read_bytes().split(b"\0")
                          if v.startswith(b"DISPLAY=")]
        except (OSError, UnicodeError):
            continue
    found.append(os.environ.get("DISPLAY"))
    unique = []
    for display in found:
        if display and display not in unique:
            unique.append(display)
    return unique or [None]


def _x11(display_name):
    """libX11 through ctypes, and an open display; None without either."""
    import ctypes
    import ctypes.util
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


def _x11_clipboard(display_name, timeout=3.0):
    """The X11 CLIPBOARD as UTF-8, read with the standard library only (no xclip). (text, clear) or None."""
    opened = _x11(display_name)
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
            again = _x11(display_name)
        except Exception:  # best effort: the address has already been read
            return
        if again:
            _, lib, shown, owner = again
            lib.XSetSelectionOwner(shown, lib.XInternAtom(shown, b"CLIPBOARD", 0), owner, 0)
            lib.XFlush(shown)
            lib.XDestroyWindow(shown, owner)
            lib.XCloseDisplay(shown)
    return text, clear


def _clipboard_once(runner, display=None):
    """(text, clear) from the first clipboard reader that works on `display`, or None if none does."""
    got = _tk_clipboard(display)
    if got:
        return got
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
    return _x11_clipboard(display)


def read_clipboard(runner=run, want=None, wait=0, sleep=time.sleep, clock=time.monotonic):
    """Clipboard text. With `want`, waits up to `wait` seconds for matching text and clears the clipboard only
    when it matches, so anything else the owner had copied is left alone. Clearing is best effort: a desktop
    that syncs its clipboard elsewhere (WSLg did, in testing) may put the text back. The sign-in code it holds
    works once and is used straight away."""
    end = clock() + wait
    while True:
        readable = False
        for display in browser_displays():
            got = _clipboard_once(runner, display)
            if got is None:
                continue
            readable, (text, clear) = True, got
            if text.strip() and (want is None or want(text)):
                clear()
                return text
        if not readable:
            raise Stop("clipboard_unavailable", "No clipboard could be read here (tkinter, xclip, xsel, wl-paste, "
                       "pbpaste or an X display). Save the address to a private file and use --from-file, or "
                       "--from-prompt. The sign-in is still pending.")
        if clock() >= end:
            return ""
        sleep(2)


def discard_stale(want, runner=run):
    """Clear an earlier sign-in's address from any clipboard before a new sign-in page is shown. Its code has
    been used or has expired, yet it looks just like the new one: on a real agent, an address left on the
    clipboard from the day before was handed over and refused (IKI-93). Best effort."""
    try:
        read_clipboard(runner, want=want, wait=0)
    except Stop:
        pass


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
        raise Stop("signin_incomplete", "That file couldn't be read. Save the full address to a private file and "
                   "run the step again; the sign-in is still pending.") from None
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


MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def registration_name(display_name, now=None):
    """The label in the owner's Amazon devices list, e.g. "Sam's Nova AI bridge (10 Oct 2026, 09:41 UTC)".
    Amazon fills in the placeholders, adding "2nd", "3rd"… only for exact duplicates."""
    label = re.sub(r"[^\w .'-]", "", display_name or "").strip()[:30] or "Alexa"
    t = time.gmtime(now)
    return "%%FIRST_NAME%%'s%%DUPE_STRATEGY_1ST%%%s bridge (%d %s %d, %02d:%02d UTC)" % (
        label, t.tm_mday, MONTHS[t.tm_mon - 1], t.tm_year, t.tm_hour, t.tm_min)


def register(code, pkce, transport=amazon, name=None):
    body = {"requested_extensions": ["device_info", "customer_info"],
            "cookies": {"website_cookies": [], "domain": ".amazon.com"},
            "registration_data": {"domain": "Device", "app_version": APP_VERSION, "device_type": DEVICE_TYPE,
                                  "device_name": name or registration_name(""), "os_version": "18.5",
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
                   "if a spare \"… bridge (date, time)\" entry appears in the owner's Amazon devices list, they can "
                   "remove it.") from None
    if status != 200:
        raise Stop("signin_refused", "Amazon refused the sign-in code (HTTP %d). The code works once and expires "
                   "within minutes, and only for the page it came from. Run setup again for a fresh sign-in, and "
                   "copy the address as soon as the dog page appears." % status,
                   do="run python3 -m bridge setup, open its new signin_url, and copy the address straight away",
                   say="Amazon didn't accept that sign-in, usually because the code took too long to reach it. "
                       "Could you sign in once more on the page I'm opening? I'll be quicker this time.")
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
    success = register(code, pkce, transport, registration_name(state["choices"].get("display_name")))
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
    done(state, "signin", next=CLOSE_TAB, home_region=state.get("home_region"), echoes=echoes)


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
    announce_key = (read_private(ANNOUNCE_FILE) or {}).get("key") or secrets.token_urlsafe(32)
    payload = dict(config, registration=seed, webhook_url=url, webhook_key=key, test_device_name=test_echo,
                   announce_key_hash=hashlib.sha256(announce_key.encode("utf-8")).hexdigest())
    messaging.send_message(card["user_id"], token, {
        "kind": "setup", "setup_token": card["setup_token"],
        "payload": json.dumps(payload, separators=(",", ":"))}, sender, host=retail.messaging_host(choices["country"]))
    write_private(ANNOUNCE_FILE, {"key": announce_key, "user_id": card["user_id"]})  # announcements on by default
    del config, payload, token, secret, announce_key
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
    done(state, "enrol", next=TIDY_BROWSER, test_echo=test_echo)


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


WEBHOOK_PROBE_TIMEOUT = 15.0


def webhook_probe(url, key, sender=messaging.send):
    """POST a setup check that the routine ignores, as the skill would call it. The HTTP status, or None if the
    routine's host couldn't be reached. Old codes kept after a reinstall, for a deleted routine, passed every
    other check and the skill was connected to a routine that no longer answered (IKI-93). On a real agent,
    the right key gave 200 and a wrong one 401 (11 October 2026)."""
    body = json.dumps({"setup_check": True, "request_id": "setup-check"}).encode("utf-8")
    try:
        status, _ = sender("POST", url, {"Authorization": "Bearer " + key, "Content-Type": "application/json"}, body,
                           {urllib.parse.urlsplit(url).hostname}, WEBHOOK_PROBE_TIMEOUT)
    except (retail.TransportError, ValueError):
        return None
    return status


def webhook_check(state, sender=messaging.send):
    """Confirm the routine's URL and key are in place and that the routine answers, without revealing them:
    base domain and key length only."""
    url, key = webhook_values()
    status = webhook_probe(url, key, sender)
    if status is None:
        raise Stop("webhook_check_failed", "The routine's host (%s) couldn't be reached for a setup check. Check "
                   "this computer's connection and run this step again; the stored codes may be fine." % host_hint(url),
                   do="wait a minute, then run python3 -m bridge setup")
    if not 200 <= status < 300:
        # Grokbot answers 401 for a wrong key and for an address with no routine alike (11 October 2026).
        reason = ("the address or key was refused: one of them is wrong, or they belong to an older or deleted "
                  "routine" if status in (401, 403) else
                  "no routine answers at that address; it may be an older or deleted one" if status in (404, 410) else
                  "the routine didn't accept it")
        raise Stop("webhook_unreachable", "The routine didn't accept a setup check at %s (HTTP %d, key length %d): %s. "
                   "If you haven't created this install's routine yet, run `routine-text` and create it first. Then "
                   "have the owner store the current routine's URL and key again, and run this step again."
                   % (host_hint(url), status, len(key), reason),
                   say="Your routine didn't accept the connection codes I have. Please copy both codes for the "
                       "routine again, replacing what's stored. Tell me when both are saved.",
                   do="give the owner the routine's URL and key again (see your profile's Connecting the "
                      "routine), then run python3 -m bridge setup")
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
    state["routine_text_at"] = int(time.time())  # setup asks for this install's routine before checking codes
    save_state(state)
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


# --- Announcements: the agent speaks on chosen Echos with no spoken request ---------------------

ANNOUNCE_FILE = "announce.json"  # the announcement key and the owner's Skill Messaging user; the skill keeps a hash
ANNOUNCE_WAITS = (8, 5)  # the skill handles the message in its own invocation, as for enrolment
QUIET = re.compile(r"([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)")


def time_zone(name):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise Stop("announce_timezone", "This computer doesn't know the time zone %r: the name is wrong (use an "
                   "IANA name such as Europe/London with `announce-config --timezone`), or this computer has no "
                   "time-zone database (`python3 -m pip install --user tzdata` adds one)." % name) from None


def quiet_now(state, now=None, zone=None):
    """True inside the owner's quiet hours, which are kept and checked here: the skill's Python has no time zones."""
    settings = state.get("announce") or {}
    match = QUIET.fullmatch(settings.get("quiet") or "")
    if not match:
        return False
    country = retail.COUNTRIES[state["choices"]["country"]]
    name = settings.get("timezone") or country.get("timezone")
    if not name:
        raise Stop("announce_timezone", "%s has more than one time zone, so quiet hours need one: run "
                   "`announce-config --timezone <IANA name>`, e.g. America/New_York." % country["name"])
    local = datetime.fromtimestamp(time.time() if now is None else now, (zone or time_zone)(name))
    minute = local.hour * 60 + local.minute
    start, end = int(match[1]) * 60 + int(match[2]), int(match[3]) * 60 + int(match[4])
    return start <= minute < end if start <= end else (minute >= start or minute < end)


def send_to_skill(state, ask, data, user_id, sender, sleep):
    """One Skill Messaging message to the owner's skill, with credentials held in memory only."""
    client, secret = messaging_credentials(ask, state["skill_id"], sleep)
    token = messaging.messaging_token({"client_id": client, "client_secret": secret}, sender)
    messaging.send_message(user_id, token, data, sender, host=retail.messaging_host(state["choices"]["country"]))
    del client, secret, token


def announce_settings(state, ask, sender, sleep, change, new_key=False):
    """Change the skill's announcement settings with a one-use setup token, as enrolment does. With `new_key`, hand
    over a fresh announcement key (an install from before announcements, or a lost key file). Returns the settings
    the skill then reports."""
    card = skill_card(ask, state, sleep)
    if not card.get("setup_token") or not card.get("user_id"):
        raise Failed("setup_card_unavailable")
    stored = read_private(ANNOUNCE_FILE) or {}
    key = secrets.token_urlsafe(32) if new_key or not stored.get("key") else stored["key"]
    if new_key or not stored.get("key"):
        change = dict(change, key_hash=hashlib.sha256(key.encode("utf-8")).hexdigest())
    send_to_skill(state, ask, {"kind": "announce_config", "setup_token": card["setup_token"],
                               "payload": json.dumps(change, separators=(",", ":"))}, card["user_id"], sender, sleep)
    write_private(ANNOUNCE_FILE, {"key": key, "user_id": card["user_id"]})
    del key
    for wait in ANNOUNCE_WAITS:
        sleep(wait)
        settings = status_card(ask, state, sleep).get("announcements") or {}
        if all(settings.get(k) == v for k, v in change.items() if k != "key_hash") and settings.get("key"):
            return settings
    raise Stop("announce_config_unconfirmed", "The change was sent, but the skill hasn't confirmed it yet. Run "
               "`python3 -m bridge announce --status` in a minute to see the current settings.")


def announce_config(state, enabled=None, echoes=None, quiet=None, timezone=None, new_key=False, runner=run,
                    sleep=time.sleep, sender=messaging.send):
    need(state, "choices", "profile", "skill_id")
    if "enrol" not in state["phases"]:
        raise Stop("earlier_step_missing", "Finish setup first: announcements start working once the skill is enrolled.")
    local = dict(state.get("announce") or {})
    if quiet is not None:
        if quiet != "off" and not QUIET.fullmatch(quiet):
            raise Stop("announce_quiet_invalid", "Give quiet hours as HH:MM-HH:MM in 24-hour time, e.g. 22:00-07:00, "
                       "or off.")
        local["quiet"] = None if quiet == "off" else quiet
    if timezone is not None:
        time_zone(timezone)
        local["timezone"] = timezone
    state["announce"] = local
    if local.get("quiet"):
        quiet_now(state)  # the time zone must be known before relying on it
    save_state(state)
    change = {}
    if enabled is not None:
        change["enabled"] = enabled
    if echoes is not None:
        names = [n.strip() for n in echoes.split(",") if n.strip()]
        change["echoes"] = [] if [n.lower() for n in names] == ["all"] else names
        if not 0 <= len(change["echoes"]) <= 20 or any(len(n) > 100 for n in change["echoes"]):
            raise Stop("announce_echoes_invalid", "Name up to 20 Echos, separated by commas, or all.")
    settings = None
    if change or new_key:
        settings = announce_settings(state, Ask(state["profile"], runner), sender, sleep, change, new_key)
    emit("announce_config", quiet=local.get("quiet"), timezone=local.get("timezone"),
         **({"skill": settings} if settings else {}))


def announce(state, names, text, style="announce", volume=None, whisper=False, urgent=False, wait=True, runner=run,
             sleep=time.sleep, sender=messaging.send, now=None):
    """Ask the skill to say `text` on the named Echos (or "all") with no spoken request. The skill checks the key,
    resolves the names exactly and leaves out Echos where it would be silent or that have Do Not Disturb on.
    Never resent: a message may have been spoken even when its outcome is unknown."""
    need(state, "choices", "profile", "skill_id")
    if "enrol" not in state["phases"]:
        raise Stop("earlier_step_missing", "Finish setup first: announcements start working once the skill is enrolled.")
    try:
        retail.check_text(text)
    except ValueError:
        raise Stop("announce_invalid", "Give the announcement as plain text: up to 4,000 characters, with no control "
                   "characters.") from None
    names = "all" if [n.lower() for n in names] == ["all"] else names
    if not names or (names != "all" and (len(names) > 20 or any(not n.strip() or len(n) > 100 for n in names))):
        raise Stop("announce_invalid", "Name 1–20 Echos with --to, as the owner's Alexa app shows them, or --to all.")
    if volume is not None and not 0 <= volume <= 100:
        raise Stop("announce_invalid", "Give --volume as 0–100.")
    if not urgent and quiet_now(state, now):
        raise Stop("announce_quiet_hours", "It's within the owner's quiet hours (%s), so nothing was sent. Send it "
                   "later, or with --urgent only if it really can't wait; Do Not Disturb still applies."
                   % state["announce"]["quiet"], do="hold the announcement until the quiet hours end")
    ask = Ask(state["profile"], runner)
    stored = read_private(ANNOUNCE_FILE) or {}
    if not stored.get("key") or not stored.get("user_id"):
        announce_settings(state, ask, sender, sleep, {}, new_key=True)  # an install from before announcements
        stored = read_private(ANNOUNCE_FILE)
    announce_id = uuid.uuid4().hex
    data = {"kind": "announce", "announce_id": announce_id, "announce_key": stored["key"],
            "targets": json.dumps(names), "text": text, "style": style, "whisper": "1" if whisper else "0",
            "volume": "" if volume is None else str(volume)}
    try:
        send_to_skill(state, ask, data, stored["user_id"], sender, sleep)
    except ValueError:
        raise Stop("announce_invalid", "The announcement is too long for Amazon's 6,000-byte message limit. Shorten "
                   "it by rewriting, not by cutting it off.") from None
    finally:
        del data, stored
    emit("announce_queued", announce_id=announce_id, echoes=names, style=style)
    if wait:
        announce_result(state, ask, sleep, announce_id)


def announce_status(state, runner=run, sleep=time.sleep):
    need(state, "choices", "profile", "skill_id")
    announce_result(state, Ask(state["profile"], runner), sleep)


ANNOUNCE_STOPS = {
    "off": ("announce_off", "The owner has turned announcements off (`announce-config --on` turns them back on, if "
            "they ask)."),
    "unknown_echo": ("announce_refused", "The skill refused it: an Echo name didn't match exactly one of the owner's "
                     "Echos, or isn't one announcements may use. Nothing was said. Use the names exactly as the "
                     "owner's Alexa app shows them."),
    "invalid": ("announce_refused", "The skill refused the message as invalid. Nothing was said."),
    "auth_required": ("announce_auth", "The skill's Amazon sign-in needs repair (MAINTENANCE.md, \"sign-in needs "
                      "attention\"). Nothing was said."),
}


def announce_result(state, ask, sleep, announce_id=None):
    """Read the skill's receipt for this announcement (or the latest) and report it for each Echo."""
    for wait in (ANNOUNCE_WAITS if announce_id else (0,)):
        sleep(wait)
        card = status_card(ask, state, sleep)
        receipt = card.get("announcement") or {}
        if announce_id is None or (receipt.get("id") == announce_id and receipt.get("state") != "sending"):
            break
    else:
        emit("announce_pending", announce_id=announce_id, next="The skill hasn't reported on it yet. Don't send it "
             "again: run `python3 -m bridge announce --status` in a minute.")
        return
    if announce_id is None:
        emit("announce_status", announcement=receipt or None, settings=card.get("announcements"))
        return
    outcome = receipt.get("state")
    if outcome == "refused":
        category, message = ANNOUNCE_STOPS.get(receipt.get("reason"), ANNOUNCE_STOPS["invalid"])
        raise Stop(category, message)
    if outcome == "auth_required":
        raise Stop(*ANNOUNCE_STOPS["auth_required"])
    if outcome == "throttled":
        raise Stop("announce_throttled", "Amazon is limiting requests. It probably wasn't said; don't send it again "
                   "now, and space announcements out.", do="wait several minutes before any other announcement")
    emit("announced", announce_id=announce_id, outcome=outcome, echoes=receipt.get("echoes", {}),
         next="Amazon accepted it; that doesn't prove it was heard. Echos marked do_not_disturb, offline or "
              "announcements_off were left out. Never resend an announcement whose outcome is unconfirmed.")


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
                 next="Make sure the owner sees each request and answer in your conversation, setting a standing rule "
                      "if your platform returns the routine's notes to you (SETUP.md step 4.3). Then save the "
                      "maintenance skill, give the owner the cheat sheet, and check that your browser is tidy and "
                      "signed out of Amazon (SETUP.md step 4).")
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
            raise Stop("signin_handover", "Open the signin_url above in a new tab of your browser and hand control "
                       "to the owner.",
                       do="open signin_url in a new tab, hand control (see your profile's Browser handover); "
                          "afterwards copy the address bar to the clipboard and run python3 -m bridge setup",
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
            dev_auth(state, profile, runner=runner, clipboard=clipboard, sleep=sleep)
        elif step == "create":
            create(state, runner, sleep)
        elif step == "webhook":
            if not state.get("routine_text_at"):  # codes left from an earlier install would otherwise skip this
                raise Stop("routine_needed", "Create this install's routine first: run `python3 -m bridge "
                           "routine-text` and make the routine from it exactly (if you already have, running "
                           "routine-text again is enough). Then have the owner store its URL and key, even if older "
                           "ones are already stored, and run setup again.",
                           do="run python3 -m bridge routine-text, create the routine (see your profile's Connecting "
                              "the routine), give the owner its two codes, then run python3 -m bridge setup",
                           say="I'm making the routine that will answer your questions. It has two connection codes "
                               "that only you can copy; I'll show you where in a moment.")
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


def head_release_tag(runner=run):
    """The release tag at this checkout's HEAD (`v1.2.3`), or "" if there isn't exactly one name for it."""
    described = _git(runner, "describe", "--tags", "--exact-match", "--match", "v*", "HEAD")
    tag = described.stdout.strip() if described.returncode == 0 else ""
    return tag if RELEASE_TAG.fullmatch(tag) else ""


def verify_install(state, tag=None, runner=run):
    """`bridge verify`: check the tag, check that this checkout IS that release, and record it as installed.
    Without a tag, the release tag at HEAD is checked: the signature and checkout checks are the same."""
    if not tag:
        try:
            tag = head_release_tag(runner)
        except sshsig.BadSignature:
            tag = ""
        if not tag:
            raise Stop("release_not_checked_out", "This checkout isn't at a release tag. Run `git checkout -q "
                       "--detach refs/tags/<tag>` for the release you want, then verify again (or pass --tag).")
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
    tag = head_release_tag(runner)
    if not tag:
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


def skill_card(ask, state, sleep):
    """The installed skill's setup card (the owner's one-use token and Skill Messaging user), or {}."""
    choices = state["choices"]
    _, card = simulate(ask, state["skill_id"], choices["locale"],
                       "ask %s to repair cloud authentication" % choices["invocation"], sleep)
    try:
        card = json.loads(card or "")
    except ValueError:
        return {}
    return card if isinstance(card, dict) and card.get("skill_id") == state["skill_id"] else {}


REMOVAL_RESULTS = ("removed", "refused", "already_invalid", "none", "failed")


def status_card(ask, state, sleep):
    """The card from "check cloud status": the skill's last device-entry removal, tagged with its request."""
    choices = state["choices"]
    _, card = simulate(ask, state["skill_id"], choices["locale"],
                       "ask %s to check cloud status" % choices["invocation"], sleep)
    try:
        card = json.loads(card or "")
    except ValueError:
        return {}
    return card if isinstance(card, dict) and card.get("skill_id") == state["skill_id"] else {}


def skill_removal(state, runner, sleep, sender):
    """Ask the installed skill to remove its own device entry (only it holds that sign-in), and read back the
    result for this request only. Best effort: one of REMOVAL_RESULTS, or "unknown"."""
    try:
        ask = Ask(state["profile"], runner)
        card = skill_card(ask, state, sleep)
        if not card.get("setup_token") or not card.get("user_id"):
            return "unknown"
        request = hashlib.sha256(card["setup_token"].encode("utf-8")).hexdigest()[:16]
        client, secret = messaging_credentials(ask, state["skill_id"], sleep)
        token = messaging.messaging_token({"client_id": client, "client_secret": secret}, sender)
        messaging.send_message(card["user_id"], token, {"kind": "deregister", "setup_token": card["setup_token"]},
                               sender, host=retail.messaging_host(state["choices"]["country"]))
        del client, secret, token
        for wait in (8, 5, 5, 5):  # the skill handles the message in its own invocation, as for enrolment
            sleep(wait)
            got = status_card(ask, state, sleep)
            if got.get("for") == request and got.get("deregistration") in REMOVAL_RESULTS:
                return got["deregistration"]
    except Exception:  # best effort: an unexpected reply must never stop the uninstall
        pass
    return "unknown"


def remove_device_entry(state, runner=run, sleep=time.sleep, sender=messaging.send, transport=amazon):
    """Before the skill is deleted, remove the bridge's own entries from the owner's Amazon devices list: a sign-in
    made here but not yet sent to the skill, and the skill's own. "removed" or "none" when nothing is left that the
    bridge knows of; otherwise the first other result (see REMOVAL_RESULTS), or "unknown"."""
    results = []
    seed = read_private("seed.json")
    if seed:
        try:
            results.append(retail.deregister(seed, transport))
        except Exception:
            results.append("failed")
    if "enrol" in state.get("phases", {}) and state.get("skill_id") and state.get("profile") and state.get("choices"):
        results.append(skill_removal(state, runner, sleep, sender))
    return next((r for r in results if r not in ("removed", "none")), "removed" if results else "none")


def uninstall(state, confirm, runner=run, sleep=time.sleep, sender=messaging.send):
    if not confirm:
        raise Stop("confirm_uninstall", "This deletes the Alexa skill and its stored data. Run again with --yes to confirm.")
    device = remove_device_entry(state, runner, sleep, sender)
    if state.get("skill_id") and state.get("profile"):
        Ask(state["profile"], runner).call("delete-skill", "-s", state["skill_id"], missing_ok=True)
    shutil.rmtree(home(), ignore_errors=True)
    removed = device in ("removed", "none")
    emit("uninstalled", device_entry=device,
         next=("Delete the agent's Alexa bridge routine and the two secrets that hold its address and key, if your "
               "agent can." + ("" if removed else " The skill's entry in the owner's Amazon devices list couldn't be "
               "removed automatically: tell the owner to remove the one named like \"<first name>'s <skill name> "
               "bridge (date, time UTC)\" under Manage Content and Devices > Devices > Alexa.")))
