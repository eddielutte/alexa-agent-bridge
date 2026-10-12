"""bridge: set up a personal Alexa skill that hands questions to your AI agent.

Run from the folder that contains `bridge`:  python3 -m bridge <command> [options]
Exit codes: 0 done, 2 stopped for the owner (see the "stopped" event), 1 failed.
"""
import argparse
import sys

from . import core


def main(argv=None):
    parser = argparse.ArgumentParser(prog="bridge", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="run every next step until the owner is needed (safe to repeat)")
    pre = sub.add_parser("preflight", help="check and install tools")
    pre.add_argument("--no-install", action="store_true")
    ch = sub.add_parser("choose", help="record the skill name, country and language, or (alone) the test Echo")
    ch.add_argument("--name", help='invocation, e.g. "nova a. i."')
    ch.add_argument("--country", help="country code, e.g. GB")
    ch.add_argument("--display-name")
    ch.add_argument("--locale")
    ch.add_argument("--language", help="language pack, default en")
    ch.add_argument("--test-echo")
    ch.add_argument("--agent", help="the agent's spoken name, as the owner calls it")
    doc = sub.add_parser("doctor", help="probe the country's Amazon services")
    doc.add_argument("--network", action="store_true", help="also time Amazon's sign-in endpoint from this computer")
    auth = sub.add_parser("dev-auth", help="check the Amazon developer sign-in")
    auth.add_argument("--profile", default=core.DEFAULT_PROFILE)
    auth.add_argument("--vendor", help="developer organisation ID, when the account has several")
    code_source = auth.add_mutually_exclusive_group()
    code_source.add_argument("--from-file", help="a private file holding the address of Amazon's code page")
    code_source.add_argument("--from-prompt", action="store_true", help="paste that address into a hidden prompt")
    sub.add_parser("create", help="create the Alexa-hosted skill")
    sub.add_parser("deploy", help="push code, build the voice model, enable testing")
    sign = sub.add_parser("signin", help="Amazon sign-in on Amazon's own page")
    sign.add_argument("step", choices=["start", "finish", "check"])
    sign.add_argument("--open", action="store_true", help="open the page in the default browser")
    source = sign.add_mutually_exclusive_group()
    source.add_argument("--from-file")
    source.add_argument("--from-clipboard", action="store_true")
    source.add_argument("--from-prompt", action="store_true", help="paste the address into a hidden prompt")
    sub.add_parser("enrol", help="send the sign-in and routine details to the skill")
    sub.add_parser("routine-text", help="print the agent routine instructions")
    pc = sub.add_parser("pack-check", help="check a drafted language pack against English")
    pc.add_argument("--language", required=True)
    sub.add_parser("webhook-set", help="type the routine's webhook URL and key into hidden prompts")
    sub.add_parser("webhook-check", help="confirm the routine's URL and key are stored (shows host and length only)")
    test = sub.add_parser("test", help="connection test (or --status) through the simulator")
    test.add_argument("--status", action="store_true")
    say = sub.add_parser("announce", help="say something on the owner's Echos with no spoken request (or --status)")
    say.add_argument("text", nargs="?", help="what to say, as plain text")
    say.add_argument("--to", action="append", default=[], help='an Echo name as the Alexa app shows it, or "all"; '
                     "repeat for several")
    say.add_argument("--style", choices=["announce", "speak"], default="announce",
                     help="announce: with a chime, as Alexa's own announcements; speak: no chime")
    say.add_argument("--volume", type=int, help="0-100 for this message only; the earlier volume comes back after")
    say.add_argument("--whisper", action="store_true")
    say.add_argument("--urgent", action="store_true", help="send during the owner's quiet hours (never Do Not Disturb)")
    say.add_argument("--no-wait", action="store_true", help="don't wait for the skill's report")
    say.add_argument("--status", action="store_true", help="the latest announcement's outcome and the settings")
    cfg = sub.add_parser("announce-config", help="turn announcements on or off, limit Echos, set quiet hours")
    onoff = cfg.add_mutually_exclusive_group()
    onoff.add_argument("--on", action="store_true")
    onoff.add_argument("--off", action="store_true")
    cfg.add_argument("--echos", help='Echo names separated by commas, or "all"')
    cfg.add_argument("--quiet", help="quiet hours, e.g. 22:00-07:00, or off")
    cfg.add_argument("--timezone", help="IANA time zone for quiet hours, e.g. Europe/London")
    cfg.add_argument("--new-key", action="store_true", help="hand the skill a fresh announcement key")
    sub.add_parser("status", help="show progress and the next step")
    ver = sub.add_parser("verify", help="check a release tag's signature (built in; no ssh-keygen needed)")
    ver.add_argument("--tag", help="the release to check (default: the release tag at this checkout's HEAD)")
    up = sub.add_parser("update", help="deploy a newer signed release")
    which = up.add_mutually_exclusive_group(required=True)
    which.add_argument("--tag")
    which.add_argument("--latest", action="store_true", help="the newest release, if it's newer than this one")
    rm = sub.add_parser("uninstall", help="delete the skill and local state")
    rm.add_argument("--yes", action="store_true")
    hidden = sub.add_parser("_dev-signin")  # started in the background by dev-auth
    hidden.add_argument("--profile", default=core.DEFAULT_PROFILE)
    args = parser.parse_args(argv)
    if args.command == "_dev-signin":
        core.drive_configure(args.profile)
        return 0
    state = core.load_state()
    try:
        if args.command == "setup":
            core.setup(state)
        elif args.command == "preflight":
            core.preflight(state, install=not args.no_install)
        elif args.command == "choose" and args.test_echo and not any(
                (args.name, args.country, args.display_name, args.locale, args.language, args.agent)):
            core.choose_test_echo(state, args.test_echo)
        elif args.command == "choose":
            if not (args.name and args.country):
                parser.error("choose needs --name and --country (or --test-echo alone)")
            core.choose(state, args.name, args.country, args.display_name, args.locale, args.language, args.test_echo,
                        args.agent)
        elif args.command == "doctor":
            core.doctor(state, network=args.network)
        elif args.command == "dev-auth":
            core.dev_auth(state, args.profile, vendor=args.vendor,
                          source="prompt" if args.from_prompt else args.from_file)
        elif args.command == "create":
            core.create(state)
        elif args.command == "deploy":
            core.deploy(state)
        elif args.command == "signin" and args.step == "start":
            core.signin_start(state, args.open)
        elif args.command == "signin" and args.step == "check":
            core.signin_check(state)
        elif args.command == "signin":
            if not (args.from_file or args.from_clipboard or args.from_prompt):
                parser.error("signin finish needs --from-file, --from-clipboard or --from-prompt")
            core.signin_finish(state, "clipboard" if args.from_clipboard else "prompt" if args.from_prompt
                               else args.from_file)
        elif args.command == "enrol":
            core.enrol(state)
        elif args.command == "pack-check":
            core.pack_check(args.language)
        elif args.command == "webhook-set":
            core.webhook_set()
        elif args.command == "webhook-check":
            core.webhook_check(state)
        elif args.command == "routine-text":
            core.routine_text(state)
        elif args.command == "test":
            core.connection_test(state, status_only=args.status)
        elif args.command == "announce" and args.status:
            core.announce_status(state)
        elif args.command == "announce":
            if not (args.text and args.to):
                parser.error('announce needs --to and the text, e.g. announce --to "Office Echo" "It\'s bedtime."')
            core.announce(state, args.to, args.text, args.style, args.volume, args.whisper, args.urgent,
                          wait=not args.no_wait)
        elif args.command == "announce-config":
            core.announce_config(state, enabled=True if args.on else False if args.off else None, echoes=args.echos,
                                 quiet=args.quiet, timezone=args.timezone, new_key=args.new_key)
        elif args.command == "status":
            core.status(state)
        elif args.command == "verify":
            core.verify_install(state, args.tag)
        elif args.command == "update":
            core.update(state, args.tag)  # no tag: --latest
        elif args.command == "uninstall":
            core.uninstall(state, args.yes)
    except core.Stop as stop:
        core.emit("stopped", category=stop.category, message=stop.message, say=stop.say, do=stop.do,
                  who="owner" if stop.say else "agent")
        return 2
    except core.Failed as failure:
        core.emit("failed", category=failure.category)
        return 1
    except Exception as error:  # JSON lines only: a traceback could carry an address, a token or a message
        core.emit("failed", category="unexpected_" + type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
