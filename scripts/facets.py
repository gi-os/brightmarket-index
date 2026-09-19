"""Ask Jev what each app is, once per change, and keep the answers in the index.

The catalogue has a name, a category and one sentence of prose per app. That is enough
for a person reading a list and not enough for the question people actually have before
they install something: will this work for me. Does it need an account. Does it need a
server I have to run. Is it any use with no signal.

Those are judgments, not facts in a YAML file, so they come from a System One model
(https://docs.typesafe.ai). Answers are typed and carry probabilities, so the site can
hold a threshold rather than a sentence, and "similar apps" can compare two distributions
instead of matching a category string.

Three things about the shape, each of which cost something to learn:

**The README is the state, not the summary.** Asked about BrightWay with only its
catalogue line -- which says the compass "keeps working with no key and no signal" -- the
model answered needs_account 0.09 and works_offline 0.71. Both wrong: it wants a Google
Routes key by QR and routing is a live service. The clause is about the compass; as state
it reads as the app. With the README included the same two questions answer 0.93 and 0.09.
The model was never the problem. A one-line summary is not enough evidence to judge from.

**One call, many questions.** Questions are evaluated in parallel and in isolation against
the same state, so asking six costs barely more than asking one and none of them can see
another's answer.

**A missing key is not a failure.** The whole catalogue costs about half a cent to enrich,
but a build that cannot reach the model must still produce an index. Previous answers are
carried forward, the rest go out without facets, and the run says so.

**Answers are carried in the published index, not in a file here.** The index is deployed
straight to Pages and never committed, so a cache file in the repo would be written by a
build and thrown away with the runner -- every build would then re-ask about all 82 apps.
Each app's facets travel with its entry and come back through load_previous, exactly as
firstSeen and the pinned signer do. The stamp they were computed under rides along with
them, which is what makes "has anything changed" answerable without fetching a thing.
"""

import concurrent.futures
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"

# Enough README to carry the operational facts, which live near the top: what it is, what
# it needs, how it is set up. Past a couple of thousand characters a README is build
# instructions and licence text.
README_CHARS = 2500

# Below this the model is spread across several options and the site says nothing rather
# than asserting the top one. Similarity still uses the full distribution -- a genuinely
# ambiguous app is genuinely near several others, and that is information.
PURPOSE_FLOOR = 0.6

QUESTIONS = {
    "needs_account": {
        "type": "noul",
        "instructions": "Does using this app require the owner to supply an account, an API key, or a subscription?",
        "criteria": {
            "true": "The owner must sign in, paste or scan a key, or hold a subscription before the main feature works",
            "false": "It is useful immediately after installing, with nothing to supply",
        },
    },
    "needs_server": {
        "type": "noul",
        "instructions": "Does this app depend on a server or another machine that the owner has to run themselves?",
        "criteria": {
            "true": "It needs a NAS, a home server, a desktop bridge or a self-hosted service to work",
            "false": "The phone is the only machine involved, apart from ordinary public internet services",
        },
    },
    "works_offline": {
        "type": "noul",
        "instructions": "Can the main thing this app does be done with no network connection?",
        "criteria": {
            "true": "The primary feature works fully offline",
            "false": "The primary feature needs a network, even if some smaller part of the app works offline",
        },
    },
    "purpose": {
        "type": "choice",
        "instructions": "What is the primary thing a person uses this app to do?",
        "criteria": {
            "capture": "Record something from the world: photos, video, audio, notes taken in the moment",
            "read": "Read or listen to material written elsewhere",
            "communicate": "Reach another person",
            "organise": "Track, plan or keep hold of the owner's own information",
            "control": "Change how the phone itself behaves",
            "play": "A game or a pastime",
            "reference": "Look something up",
            "other": "None of these fits",
        },
    },
    "replaces_phone_habit": {
        "type": "score",
        "instructions": "How much does this app replace a reason someone would otherwise pick up a smartphone?",
        "criteria": [
            "Not at all: a convenience on the phone you already carry",
            "Slightly: it covers a small part of one habit",
            "Substantially: it covers most of one common reason to reach for a smartphone",
            "Completely: someone could drop a whole smartphone app because of it",
        ],
    },
    "setup_effort": {
        "type": "score",
        "instructions": "How much work is there between installing this app and using it for the first time?",
        "criteria": [
            "None: open it and it works",
            "A little: one permission, one toggle, or a sign-in",
            "Real setup: adb commands, a key to obtain, or a service to configure",
            "A project: another machine to set up and keep running",
        ],
    },
}


def readme_for(repo: str, headers: dict) -> str:
    """The top of a repo's README, stripped to prose.

    Tries main then master. A missing README is ordinary -- some catalogue entries point
    at somebody else's project -- and the app is still enriched from its summary alone,
    with less to go on.
    """
    for branch in ("main", "master"):
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/README.md"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode("utf-8", "replace")
        except Exception:
            continue
        text = re.sub(r"```.*?```", " ", text, flags=re.S)          # code blocks say little here
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)           # images
        text = re.sub(r"<[^>]+>", " ", text)                        # badges and raw HTML
        return re.sub(r"\s+", " ", text).strip()[:README_CHARS]
    return ""


def stamp(app: dict, version: str) -> str:
    """Whether anything could have changed, decided without touching the network.

    The obvious key is the evidence itself -- summary plus README -- but computing it
    means fetching 82 READMEs on every build just to conclude that none of them moved.
    Measured: 6.6 seconds and 82 requests to do nothing.

    A release tag stands in for the README. An app whose catalogue entry is unchanged and
    which has not cut a release cannot have changed what it is, and if a README is edited
    without a release the facets are stale until the next one -- which is the right trade
    for a fact like "does this need a server".

    The questions are in the key too, so editing one invalidates every entry rather than
    leaving old answers sitting under a new meaning.
    """
    blob = json.dumps(
        [app.get("name", ""), app.get("category", ""), " ".join(str(app.get("summary", "")).split()),
         version, QUESTIONS],
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def ask(state: dict, key: str, tries: int = 4) -> dict | None:
    body = json.dumps({"state": state, "model": MODEL, "questions": QUESTIONS}).encode()
    for attempt in range(tries):
        req = urllib.request.Request(
            ENDPOINT,
            data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            # 429 and 529 are the documented back-off cases. Everything else is ours to fix
            # and retrying will not help.
            if exc.code in (429, 529) and attempt < tries - 1:
                time.sleep(2 ** attempt)
                continue
            return None
        except Exception:
            if attempt < tries - 1:
                time.sleep(2 ** attempt)
                continue
            return None
    return None


def shape(answers: dict) -> dict:
    """The answers the index carries, in the form the site consumes.

    The full purpose distribution is kept, not just the winner: "similar apps" compares
    distributions, and an app split evenly between read and reference really is close to
    both. The argmax is kept separately and only when the model is concentrated enough to
    be worth printing.
    """
    purpose = answers["purpose"]
    return {
        "needsAccount": round(answers["needs_account"]["noul"], 3),
        "needsServer": round(answers["needs_server"]["noul"], 3),
        "worksOffline": round(answers["works_offline"]["noul"], 3),
        "purpose": purpose["choice"] if purpose["confidence"] >= PURPOSE_FLOOR else "",
        "purposeMix": {k: round(v, 3) for k, v in purpose["probabilities"].items()},
        "replaces": round(answers["replaces_phone_habit"]["score"], 2),
        "setup": round(answers["setup_effort"]["score"], 2),
    }


def enrich(apps: list[dict], previous: dict[str, dict], headers: dict, warn,
           versions: dict[str, str] | None = None) -> dict[str, dict]:
    """Facets for every app, doing no work at all for the ones that cannot have changed.

    `previous` maps pkg to that app's entry in the last published index; its facets are
    reused when the stamp still matches. `versions` maps pkg to its current release tag,
    which build_index already holds from the Releases API.

    Returns pkg -> facets, each carrying the `fp` it was computed under so the next build
    can make the same comparison. Never raises: an index without facets is a worse index,
    not a failed build.
    """
    versions = versions or {}
    key = os.environ.get("TYPESAFE_API_KEY", "")

    known = {a["pkg"]: a for a in apps if a.get("pkg") and a.get("summary")}
    stamps = {p: stamp(a, versions.get(p, "")) for p, a in known.items()}

    kept = {}
    for pkg in known:
        old = (previous.get(pkg) or {}).get("facets") or {}
        if old.get("fp") == stamps[pkg]:
            kept[pkg] = old

    todo = [p for p in known if p not in kept]
    if not todo:
        return kept

    if not key:
        warn(f"No TYPESAFE_API_KEY, so {len(todo)} app(s) have no facets this build.")
        return kept

    # Only now, and only for these, is the evidence gathered.
    states = {}
    for pkg in todo:
        app = known[pkg]
        state = {
            "name": app.get("name", pkg),
            "category": app.get("category", ""),
            "summary": " ".join(str(app["summary"]).split()),
        }
        repo = app.get("repo", "")
        if repo:
            text = readme_for(repo, headers)
            if text:
                state["readme"] = text
        states[pkg] = state

    fresh = {}
    # Eight at a time finished the whole catalogue in about ten seconds; the limit that
    # matters is the service's, not ours, and `ask` backs off when it is reached.
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        futures = {pool.submit(ask, states[p], key): p for p in todo}
        for future in concurrent.futures.as_completed(futures):
            pkg = futures[future]
            out = future.result()
            if not out or "answers" not in out:
                warn(f"{pkg}: no facets, the model call did not return an answer")
                continue
            try:
                shaped = shape(out["answers"])
                shaped["fp"] = stamps[pkg]
                fresh[pkg] = shaped
            except Exception as exc:
                warn(f"{pkg}: facets unreadable ({type(exc).__name__})")

    # An app whose call failed keeps whatever it had, even though the stamp has moved on:
    # a stale facet is better than none, and the next build tries again.
    for pkg in todo:
        if pkg not in fresh:
            old = (previous.get(pkg) or {}).get("facets") or {}
            if old:
                kept[pkg] = old

    kept.update(fresh)
    return kept
