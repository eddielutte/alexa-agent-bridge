"""Build an interaction model from a language template and an invocation name.

  python build_model.py --language en --invocation "nova a. i." [--out interaction-model.json]
"""
import argparse
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
# English invocation rules (Amazon custom-skill guidance): lower-case words, numbers spelled out,
# acronyms as "a. i.", no wake words or launch/connecting words, and at least two words.
RULES = {"en": {
    "banned": {"alexa", "amazon", "echo", "computer", "ziggy", "skill", "app"},
    "launch": {"ask", "tell", "open", "launch", "begin", "load", "play", "resume", "run", "start",
               "talk", "use", "enable"},
    "connecting": {"to", "from", "by", "if", "and", "whether", "for", "in", "on", "of", "with",
                   "about", "at"},
    "articles": {"a", "an", "the"}}}
# Other languages: only the universal checks until a reviewed rule set is added here.
GENERIC = {"banned": {"alexa", "amazon", "echo", "computer", "ziggy"}, "launch": set(), "connecting": set(),
           "articles": set()}


def allowed(name):
    return bool(name) and all((c.isalpha() and c == c.lower()) or c in " .'" for c in name)


def invocation_problems(name, language="en"):
    rules = RULES.get(language, GENERIC)
    problems = []
    if not isinstance(name, str) or not allowed(name):
        return ["use lower-case letters, spaces, apostrophes and acronym full stops only (spell out numbers)"]
    words = name.split()
    letters = [w for w in words if re.fullmatch(r"[^\W\d_]\.", w)]
    if any("." in w and not re.fullmatch(r"[^\W\d_]\.", w) for w in words):
        problems.append('write acronyms as single letters with full stops, e.g. "a. i."')
    if len(words) < 2:
        problems.append("use at least two words")
    plain = [w.rstrip(".").replace("'s", "") for w in words]
    for word in plain:
        if word in rules["banned"]:
            problems.append('"%s" is not allowed in an invocation name' % word)
    if plain and plain[0] in rules["launch"]:
        problems.append('do not start with a launch word such as "%s"' % plain[0])
    if any(w in rules["connecting"] for w in plain):
        problems.append("avoid connecting words such as to, from, by, if or and")
    if len(words) == 2 and not letters and any(w in rules["articles"] for w in plain):
        problems.append("a two-word name cannot include an article")
    return problems


DEVICE_WORDS = {"echo", "show", "dot", "studio", "pop", "spot", "plus", "input", "flex"}


def echo_values(names):
    """Slot values for the owner's own Echo names, with the room words as a synonym."""
    values = []
    for name in sorted(set(n.strip() for n in names if isinstance(n, str) and n.strip())):
        room = " ".join(w for w in name.split() if w.lower() not in DEVICE_WORDS and not w.isdigit())
        value = {"value": name}
        if room and room.lower() != name.lower():
            value["synonyms"] = [room]
        values.append({"name": value})
    return values


def build(language, invocation, echoes=None):
    problems = invocation_problems(invocation, language)
    if problems:
        raise ValueError("; ".join(problems))
    template = json.loads((HERE / "models" / (language + ".json")).read_text(encoding="utf-8-sig"))
    if echoes:
        for slot_type in template["languageModel"].get("types", []):
            if slot_type["name"] == "EchoDeviceName":
                slot_type["values"] = echo_values(echoes)
    model = {"interactionModel": {"languageModel": {"invocationName": invocation,
                                                    **template["languageModel"]}}}
    return json.dumps(model, indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--language", default="en")
    parser.add_argument("--invocation", required=True)
    parser.add_argument("--out")
    args = parser.parse_args()
    text = build(args.language, args.invocation)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8", newline="\n")
    else:
        print(text, end="")
