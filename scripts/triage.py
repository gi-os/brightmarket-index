"""A second opinion on a submission, for the person reviewing it.

The validator already checks everything that is checkable: the package id matches the
APK, the signer is pinned, the release exists, the fields parse. What it cannot check is
whether the listing is *honest* -- whether the summary describes the thing in the repo,
whether the category is the one a reader would look under, whether anything about it is
spam.

Those are judgments, so they come from a System One model and they are **advisory**. A
probability must never reject a submission: the failure mode of an automatic refusal is a
real app turned away by a machine that will not explain itself, which is worse than a bad
listing reaching a human who is reading the PR anyway. Every answer here lands in the PR
body as a note; the decision stays where it was.
"""

import json
import os
import urllib.error
import urllib.request

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"

# The categories the catalogue actually uses. A submission naming something else is not
# wrong -- the field is free text -- but it is worth a reviewer's glance.
# Read off the catalogue, not invented. The model cannot choose an option that is not
# offered, so a made-up vocabulary would push every app into the nearest wrong box and
# report it confidently.
CATEGORIES = {
    "utilities": "Tools for the phone itself, its files, or its settings",
    "productivity": "Notes, tasks, time, documents, money",
    "media": "Photos, video, audio, or playing them back",
    "lifestyle": "Habits, food, weather, faith, the day-to-day",
    "health": "Body, exercise, sleep, medication",
    "hardware": "Talks to a device: a watch, a camera, a car, a sensor",
    "entertainment": "Something to pass the time that is not a game",
    "communication": "Messaging, mail, calls",
    "travel": "Maps, transit, navigation, places",
    "reading": "Books, articles, feeds, anything read at length",
    "games": "A game",
}


def questions(declared_category: str) -> dict:
    return {
        "is_a_real_app": {
            "type": "noul",
            "instructions": "Does this describe a real, working application for a phone?",
            "criteria": {
                "true": "A specific app with a described function",
                "false": "Spam, a placeholder, a test entry, or something that is not an app",
            },
        },
        "summary_matches_repo": {
            "type": "noul",
            "instructions": "Does `submission.summary` describe the same software that `repo_readme` describes?",
            "criteria": {
                "true": "They are plainly the same program",
                "false": "The summary describes something the README does not support, or there is no way to tell",
            },
        },
        "overclaims": {
            "type": "noul",
            "instructions": "Does `submission.summary` promise capabilities that `repo_readme` gives no sign of?",
            "criteria": {
                "true": "The summary claims features the project does not appear to have",
                "false": "The summary is at or below what the project actually does",
            },
        },
        "best_category": {
            "type": "choice",
            "instructions": (
                "Which catalogue category would a reader most expect to find this app under? "
                f"It was submitted under \"{declared_category or 'nothing'}\"."
            ),
            "criteria": dict(CATEGORIES),
        },
    }


def ask(state: dict, qs: dict, key: str) -> dict | None:
    body = json.dumps({"state": state, "model": MODEL, "questions": qs}).encode()
    req = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.load(resp)
    except Exception:
        return None


def note(summary: str, name: str, category: str, readme: str) -> str:
    """A Markdown block for the PR body, or "" when there is nothing to say.

    Silent on a clean submission. A reviewer who sees this section at all should be
    seeing it because something is worth a look.
    """
    key = os.environ.get("TYPESAFE_API_KEY", "")
    if not key or not summary:
        return ""

    state = {
        "submission": {"name": name, "category": category, "summary": summary},
        "repo_readme": readme[:2500] if readme else "(no README could be read)",
    }
    out = ask(state, questions(category), key)
    if not out or "answers" not in out:
        return ""

    a = out["answers"]
    flags = []

    if a["is_a_real_app"]["noul"] < 0.5:
        flags.append(
            f"- **This may not be an app.** Reads as a real application: "
            f"{a['is_a_real_app']['noul']:.0%}."
        )
    if readme and a["summary_matches_repo"]["noul"] < 0.5:
        flags.append(
            f"- **The summary and the README may describe different things.** Same software: "
            f"{a['summary_matches_repo']['noul']:.0%}."
        )
    if readme and a["overclaims"]["noul"] > 0.6:
        flags.append(
            f"- **The summary may claim more than the project does.** Overclaims: "
            f"{a['overclaims']['noul']:.0%}."
        )

    best = a["best_category"]
    # Only when the model is concentrated: a genuinely cross-category app spreads its
    # probability and has no business being second-guessed over it.
    if best["confidence"] >= 0.7 and category and best["choice"] != category:
        flags.append(
            f"- **Category.** Submitted as `{category}`, reads as `{best['choice']}` "
            f"({best['probabilities'][best['choice']]:.0%})."
        )

    if not flags:
        return ""
    return (
        "\n### Worth a look\n\n"
        + "\n".join(flags)
        + "\n\nJudgments, not checks — nothing here blocked the submission.\n"
    )
