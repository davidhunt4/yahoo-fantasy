"""Generate a matchup recap with an LLM (via the Vercel AI Gateway) from the files produced by timeline.py.

Usage: python generate.py WEEK TEAM_KEY [--model ID] [--dry-run] [--rebuild]

The system prompt (prompts/system.md plus any example recaps in prompts/examples/) holds all instructions
and the data dictionary; the user message holds only data, followed by the task.
"""
import argparse
import glob
import json
import os
import re
import sys

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

DEFAULT_MODEL = os.getenv("RECAP_MODEL", "openai/gpt-5.1-thinking")
GATEWAY_URL = os.getenv("AI_GATEWAY_BASE_URL", "https://ai-gateway.vercel.sh/v1")
HERE = os.path.dirname(os.path.abspath(__file__))
STOP = {"The", "A", "An", "But", "And", "Or", "So", "It", "He", "She", "They", "This", "That", "When", "While", "With", "In", "On", "At",
        "By", "For", "From", "If", "As", "Of", "To", "Not", "No", "Yes", "Then", "Still", "Even", "Just", "Only", "After", "Before"}
ALLOWED_CAPS = {"Thursday", "Friday", "Saturday", "Sunday", "Monday", "Tuesday", "Wednesday", "Night", "Football", "NFL", "TNF", "MNF",
                "January", "February", "September", "October", "November", "December", "Week", "Yahoo", "Sleeper"}


# ---- prompt assembly -------------------------------------------------------------------------------

def load_system():
    text = open(os.path.join(HERE, "prompts", "system.md")).read()
    examples = []
    for p in sorted(glob.glob(os.path.join(HERE, "prompts", "examples", "*"))):
        if not p.endswith("README.md") and os.path.isfile(p):
            examples.append(open(p).read().strip())
    if examples:
        text += ("\n# Example recaps (for voice and structure only; their matchups are not part of this one)\n\n"
                 + "\n\n".join(f"<example_recap>\n{e}\n</example_recap>" for e in examples) + "\n")
    return text


def load_data(stem):
    data = json.load(open(f"{stem}.json"))
    tables = open(f"{stem}_tables.txt").read()
    checkpoints, activities = tables.split("\n\nACTIVITIES\n")
    return data, checkpoints.replace("SLOT CHECKPOINTS\n", "", 1).strip(), activities.strip()


def build_user_message(data, checkpoints, activities):
    a, b = list(data["starting_score"])
    return (f"<matchup_data>\n{json.dumps(data, indent=1)}\n</matchup_data>\n\n"
            f"<slot_checkpoints>\n{checkpoints}\n</slot_checkpoints>\n\n"
            f"<activities>\n{activities}\n</activities>\n\n"
            f"Write the recap for week {data['week']}: {a} vs {b}, following the instructions you were given.")


# ---- model call ------------------------------------------------------------------------------------

def call_model(system, user, model):
    """The gateway speaks the OpenAI chat-completions API, so any model in its catalog works by changing the id."""
    key = os.getenv("AI_GATEWAY_API_KEY")
    if not key:
        sys.exit("AI_GATEWAY_API_KEY is not set. Put it in .env (see .env.example).")
    client = OpenAI(api_key=key, base_url=GATEWAY_URL, timeout=900)  # reasoning models can take a few minutes
    return client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )


# ---- output parsing and checks ---------------------------------------------------------------------

def parse(text):
    recap = re.search(r"<recap>(.*?)</recap>", text, re.S)
    evidence = re.search(r"<evidence>(.*?)</evidence>", text, re.S)
    return (recap.group(1).strip() if recap else None), (evidence.group(1).strip() if evidence else None)


def numbers_in(text):
    return {round(float(n), 2) for n in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", text)}


def check(recap, evidence, data, checkpoints, activities):
    """Cheap automatic checks. Returns a list of warnings for a human to look at; none of them prove an error."""
    warnings = []
    corpus = json.dumps(data) + "\n" + checkpoints + "\n" + activities
    valid_ids = set(re.findall(r"^([AS]\d+) \|", checkpoints + "\n" + activities, re.M))

    words = len(recap.split())
    if not 150 <= words <= 450:
        warnings.append(f"length is {words} words (target is roughly 250-350)")

    cited = set(re.findall(r"\b([AS]\d+)\b", evidence or ""))
    if not evidence:
        warnings.append("no evidence block returned")
    for bad in sorted(cited - valid_ids):
        warnings.append(f"evidence cites {bad}, which is not a row in the tables")

    known = numbers_in(corpus)
    unseen = sorted(n for n in numbers_in(recap) if n not in known and abs(n) not in known)
    if unseen:
        warnings.append(f"numbers in the recap that appear nowhere in the data (fine only if simple arithmetic): {unseen}")

    low = corpus.lower()
    unknown_names = set()
    for m in re.finditer(r"(?<![.!?\"“]\s)(?<!\n)(?<!^)\b([A-Z][\w'’.\-]+(?:[ ]+[A-Z][\w'’.\-]+)*)", recap):
        phrase = m.group(1)
        toks = [t for t in (x.strip(".,;:!?'’") for x in phrase.split()) if t and t not in STOP and t not in ALLOWED_CAPS]
        if any(t.lower() not in low for t in toks):
            unknown_names.add(phrase)
    if unknown_names:
        warnings.append(f"capitalized names/terms in the recap not found in the data: {sorted(unknown_names)}")

    tags = set(re.findall(r"\b([A-Z]{2,5}) = ", checkpoints))
    leaked = [t for t in tags if re.search(rf"\b{t}\b", recap)] + [t for t in ("far_above", "far_below", "vs_projection", "Team A", "Team B") if t in recap]
    if leaked:
        warnings.append(f"recap leaks table labels or field names: {leaked}")
    return warnings


# ---- main ------------------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("week", type=int)
    ap.add_argument("team_key")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true", help="assemble the prompt and report its size without calling the API")
    ap.add_argument("--rebuild", action="store_true", help="rebuild the data files from Yahoo/nflverse first")
    args = ap.parse_args()

    from timeline import find_matchup, write_outputs
    keys, _ = find_matchup(args.team_key, args.week)
    stem = f"timeline_wk{args.week}_{'_vs_'.join(k.split('.t.')[1] for k in keys)}"
    if args.rebuild or not os.path.exists(f"{stem}_tables.txt"):
        write_outputs(args.week, args.team_key)

    data, checkpoints, activities = load_data(stem)
    system, user = load_system(), build_user_message(data, checkpoints, activities)
    print(f"system prompt ~{len(system) // 4:,} tokens, user message ~{len(user) // 4:,} tokens (rough: characters / 4)")
    if args.dry_run:
        return

    response = call_model(system, user, args.model)
    choice = response.choices[0]
    text = choice.message.content or ""
    if choice.finish_reason == "content_filter":
        sys.exit("The model's content filter stopped this request.")
    recap, evidence = parse(text)
    if not recap:
        sys.exit(f"No <recap> block in the response (finish_reason={choice.finish_reason}):\n{text}")
    warnings = check(recap, evidence, data, checkpoints, activities)
    if choice.finish_reason == "length":
        warnings.append("response hit the output token limit and may be cut off")

    u = response.usage
    reasoning = getattr(getattr(u, "completion_tokens_details", None), "reasoning_tokens", None)
    cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", None)
    out = os.path.join(HERE, "recaps", f"{stem.replace('timeline_', '')}.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(f"{recap}\n\n---\n\n## Evidence\n\n{evidence or '(none)'}\n\n## Automatic checks\n\n"
                + ("\n".join(f"- {w}" for w in warnings) or "- no warnings") + "\n\n## Run details\n\n"
                + f"- model: {response.model}\n- finish_reason: {choice.finish_reason}\n"
                + f"- tokens: prompt {u.prompt_tokens} (cached {cached}), completion {u.completion_tokens} (reasoning {reasoning})\n")
    print(f"\n{recap}\n\nwrote {out}")
    for w in warnings:
        print("WARNING:", w)


if __name__ == "__main__":
    main()
