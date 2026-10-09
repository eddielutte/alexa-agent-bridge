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
    pre = sub.add_parser("preflight", help="check and install tools")
    pre.add_argument("--no-install", action="store_true")
    ch = sub.add_parser("choose", help="record the skill name, country and language")
    ch.add_argument("--name", required=True, help='invocation, e.g. "nova a. i."')
    ch.add_argument("--country", required=True, help="country code, e.g. GB")
    ch.add_argument("--display-name")
    ch.add_argument("--locale")
    ch.add_argument("--language", help="language pack, default en")
    ch.add_argument("--test-echo")
    ch.add_argument("--agent", help="the agent's spoken name, as the owner calls it")
    sub.add_parser("doctor", help="probe the country's Amazon services")
    auth = sub.add_parser("dev-auth", help="check the Amazon developer sign-in")
    auth.add_argument("--profile", default=core.DEFAULT_PROFILE)
    auth.add_argument("--vendor", help="developer organisation ID, when the account has several")
    sub.add_parser("create", help="create the Alexa-hosted skill")
    sub.add_parser("deploy", help="push code, build the voice model, enable testing")
    sign = sub.add_parser("signin", help="Amazon sign-in on Amazon's own page")
    sign.add_argument("step", choices=["start", "finish"])
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
    test = sub.add_parser("test", help="connection test (or --status) through the simulator")
    test.add_argument("--status", action="store_true")
    sub.add_parser("status", help="show progress and the next step")
    ver = sub.add_parser("verify", help="check a release tag's signature (built in; no ssh-keygen needed)")
    ver.add_argument("--tag", required=True)
    up = sub.add_parser("update", help="deploy a newer signed release")
    up.add_argument("--tag", required=True)
    up.add_argument("--allow-unsigned", action="store_true")
    rm = sub.add_parser("uninstall", help="delete the skill and local state")
    rm.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    state = core.load_state()
    try:
        if args.command == "preflight":
            core.preflight(state, install=not args.no_install)
        elif args.command == "choose":
            core.choose(state, args.name, args.country, args.display_name, args.locale, args.language, args.test_echo,
                        args.agent)
        elif args.command == "doctor":
            core.doctor(state)
        elif args.command == "dev-auth":
            core.dev_auth(state, args.profile, vendor=args.vendor)
        elif args.command == "create":
            core.create(state)
        elif args.command == "deploy":
            core.deploy(state)
        elif args.command == "signin" and args.step == "start":
            core.signin_start(state, args.open)
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
        elif args.command == "routine-text":
            core.routine_text(state)
        elif args.command == "test":
            core.connection_test(state, status_only=args.status)
        elif args.command == "status":
            core.status(state)
        elif args.command == "verify":
            core.verify_release(args.tag)
        elif args.command == "update":
            core.update(state, args.tag, args.allow_unsigned)
        elif args.command == "uninstall":
            core.uninstall(state, args.yes)
    except core.Stop as stop:
        core.emit("stopped", category=stop.category, message=stop.message)
        return 2
    except core.Failed as failure:
        core.emit("failed", category=failure.category)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
