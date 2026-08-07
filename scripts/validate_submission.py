#!/usr/bin/env python3
"""Validate a submission issue and, if it passes, append it to apps.yml.

Run by .github/workflows/submit.yml on every issue labelled `submission`.
On pass it writes the new apps.yml and prints a PR body; on fail it prints the
reason, which the workflow posts back as an issue comment.

The checks exist for specific reasons, not as generic hygiene:

* exactly one .apk per release -- two assets is precisely how an updater ends up
  installing a debug build (different signing cert) over the release one, which
  surfaces to the user as an opaque "Failure: Invalid".
* tag shape -- versionCode is derived from the tag's trailing segment, and the
  client compares it against PackageManager to decide "update available". A repo
  that doesn't follow the convention would be indexed with a wrong version
  forever, which is worse than being rejected.
* applicationId uniqueness -- Android identifies an app by (applicationId, cert).
  Two index entries claiming one applicationId means the store would offer to
  "update" one app with a completely different one.
"""

import os
import re
import sys
import json
import zipfile
import io
import urllib.request
import urllib.error

import yaml

API = "https://api.github.com"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "brightmarket-validator",
}
if os.environ.get("GH_TOKEN"):
    HEADERS["Authorization"] = f"Bearer {os.environ['GH_TOKEN']}"

VALID_CATEGORIES = {"reading", "utilities", "games", "media", "productivity", "hardware"}


class Reject(Exception):
    pass


def get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def field(body: str, label: str) -> str:
    """Read one `**Label:** value` line written by the portal.

    Anchored to the start of a line and stopping at the end of it, because the
    portal strips newlines out of free text for exactly this reason -- a summary
    that could contain its own `**Category:**` line would otherwise be able to
    set a field the submitter never chose.
    """
    m = re.search(rf"^\s*\*\*{label}:\*\*\s*(.+?)\s*$", body, re.M | re.I)
    return m.group(1).strip() if m else ""


def parse_issue(body: str) -> dict:
    """Pull the request out of either the issue-form rendering or the portal's
    plain `**Repo:** <url>` format.

    Returns an action as well as a repo now: the portal can ask to edit or
    remove a listing, not only to add one, and all three arrive as issues on
    this repo so they go through the same checks and leave the same trail.
    """
    repo_m = re.search(r"github\.com/([\w.-]+/[\w.-]+)", body) or re.search(
        r"^\s*([\w.-]+/[\w.-]+)\s*$", body, re.M
    )
    if not repo_m:
        raise Reject("I couldn't find a repo in this issue. Give me `owner/name` or a GitHub URL.")

    action = (field(body, "Action") or "submit").lower()
    if action not in ("submit", "edit", "remove"):
        raise Reject(f"Unknown action `{action}`.")

    # Prefer the explicit field; fall back to finding the word anywhere, which
    # is what the hand-written issue form produces.
    category = field(body, "Category").lower()
    if category and category not in VALID_CATEGORIES:
        raise Reject(f"`{category}` isn't a category. Pick one of: {', '.join(sorted(VALID_CATEGORIES))}.")
    if not category:
        cat_m = re.search(r"(reading|utilities|games|media|productivity|hardware)", body, re.I)
        category = cat_m.group(1).lower() if cat_m else ""
    if action == "submit" and not category:
        raise Reject(f"No category found. Pick one of: {', '.join(sorted(VALID_CATEGORIES))}.")

    return {
        "action": action,
        "repo": repo_m.group(1).removesuffix(".git"),
        "category": category,
        "name": field(body, "Name"),
        "summary": field(body, "Summary"),
    }


def validate(repo: str) -> dict:
    try:
        meta = get(f"{API}/repos/{repo}")
    except urllib.error.HTTPError as e:
        raise Reject(f"Can't read `{repo}` — is it public? (GitHub said {e.code}.)")

    if meta.get("private"):
        raise Reject(f"`{repo}` is private. BrightMarket only indexes public repos.")
    if meta.get("archived"):
        raise Reject(f"`{repo}` is archived, so it can't ship updates.")

    releases = [r for r in get(f"{API}/repos/{repo}/releases?per_page=100")
                if not r["draft"] and not r["prerelease"]]
    if not releases:
        raise Reject(
            f"`{repo}` has no published release. Cut a release with one signed `.apk` "
            "attached, then reopen this issue."
        )

    latest = releases[0]
    apks = [a for a in latest["assets"] if a["name"].endswith(".apk")]
    if not apks:
        raise Reject(f"The latest release (`{latest['tag_name']}`) has no `.apk` attached.")
    if len(apks) > 1:
        names = ", ".join(f"`{a['name']}`" for a in apks)
        raise Reject(
            f"The latest release has {len(apks)} `.apk` assets ({names}). Attach exactly one — "
            "a debug APK alongside the release one is how an updater installs the wrong "
            "signature and users get `Failure: Invalid`."
        )

    try:
        int(latest["tag_name"].lstrip("v").rsplit(".", 1)[-1])
    except ValueError:
        raise Reject(
            f"Tag `{latest['tag_name']}` doesn't end in a number. BrightMarket derives "
            "versionCode from the tag's last segment (`v1.2.34` → `34`), and it has to "
            "increase every release or Android refuses the update."
        )

    # Read the applicationId straight out of the APK the user is actually shipping,
    # rather than trusting anything they typed. AndroidManifest.xml inside an APK is
    # binary XML, but the applicationId appears verbatim in the string pool, so a
    # targeted scan avoids needing aapt2 (which is x86_64-only and awkward in CI).
    req = urllib.request.Request(apks[0]["browser_download_url"], headers=HEADERS)
    with urllib.request.urlopen(req, timeout=300) as r:
        blob = r.read()
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            manifest = z.read("AndroidManifest.xml")
    except (zipfile.BadZipFile, KeyError):
        raise Reject("That `.apk` isn't a readable Android package.")

    # Parse the binary manifest properly. The old approach scanned the string
    # pool for dotted identifiers and took the most frequent one that wasn't
    # android./androidx./etc -- which is wrong for any app derived from sample
    # code. The first real outside submission was a whoBIRD fork built on the
    # TensorFlow Lite sound-classifier example, and
    # `org.tensorflow.lite.examples.soundclassifier` appears throughout it far
    # more often than its actual applicationId does. The count picked the
    # ancestor.
    #
    # That is not a cosmetic mistake: pkg is the key the published index is
    # stored under and the one the client compares against PackageManager, so a
    # wrong one means the app never registers as installed and its signing
    # certificate never pins.
    app_id = None
    try:
        import tempfile
        from pyaxmlparser import APK
        with tempfile.NamedTemporaryFile(suffix=".apk", delete=False) as f:
            f.write(blob)
            tmp = f.name
        try:
            app_id = APK(tmp).package or None
        finally:
            os.unlink(tmp)
    except Exception:
        app_id = None

    if not app_id:
        # Kept only as a fallback for the case where the parser itself fails.
        # Still a guess, and still capable of being wrong in the same way.
        text = manifest.decode("utf-16-le", errors="ignore") + manifest.decode("latin-1", errors="ignore")
        candidates = re.findall(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){2,}", text)
        ignore = ("android.", "androidx.", "com.google.", "kotlin.", "java.")
        pkgs = [c for c in candidates if not c.startswith(ignore)]
        if not pkgs:
            raise Reject("Couldn't read an applicationId out of that APK's manifest.")
        app_id = max(set(pkgs), key=pkgs.count)

    return {
        "pkg": app_id,
        "repo": repo,
        "name": meta["name"],
        "summary": (meta.get("description") or "").strip(),
        "version": latest["tag_name"],
        "apk": apks[0]["name"],
    }


def main() -> int:
    body = os.environ.get("ISSUE_BODY", "")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    apps_path = os.path.join(root, "apps.yml")

    try:
        req = parse_issue(body)
        repo, action = req["repo"], req["action"]

        with open(apps_path) as f:
            doc = yaml.safe_load(f)
        existing = doc["apps"]

        if action == "remove":
            match = next((a for a in existing if a["repo"].lower() == repo.lower()), None)
            if not match:
                raise Reject(f"`{repo}` isn't in BrightMarket, so there's nothing to remove.")
            existing.remove(match)
            summary = f"Removed **{match['name']}** (`{match['pkg']}`) at the owner's request."
            out_pkg, out_name = match["pkg"], match["name"]

        elif action == "edit":
            match = next((a for a in existing if a["repo"].lower() == repo.lower()), None)
            if not match:
                raise Reject(f"`{repo}` isn't in BrightMarket yet — submit it first.")
            changed = []
            for key in ("name", "summary", "category"):
                value = req.get(key)
                if value and value != match.get(key):
                    changed.append(f"{key}: `{match.get(key)}` → `{value}`")
                    match[key] = value
            if not changed:
                raise Reject("Nothing in that request differs from what's already listed.")
            summary = f"Updated **{match['name']}** — " + "; ".join(changed)
            out_pkg, out_name = match["pkg"], match["name"]

        else:
            # A new entry is the only action that has to touch the network: it
            # is the only one making a claim about a repo we have never checked.
            info = validate(repo)
            for a in existing:
                if a["repo"].lower() == repo.lower():
                    raise Reject(f"`{repo}` is already in BrightMarket.")
                if a["pkg"] == info["pkg"]:
                    raise Reject(
                        f"Package `{info['pkg']}` is already indexed as **{a['name']}** "
                        f"(`{a['repo']}`). Two apps can't share an applicationId."
                    )
            existing.append(
                {
                    "pkg": info["pkg"],
                    # What the submitter asked to be called, falling back to the
                    # repo's own name and description when they said nothing.
                    "name": req["name"] or info["name"],
                    "repo": repo,
                    "category": req["category"],
                    "summary": req["summary"] or info["summary"],
                }
            )
            summary = (
                f"Validated **{req['name'] or info['name']}** (`{info['pkg']}`) — latest "
                f"release `{info['version']}`, one asset `{info['apk']}`."
            )
            out_pkg, out_name = info["pkg"], req["name"] or info["name"]

        with open(apps_path, "w") as f:
            yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False, allow_unicode=True)

        with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a") as f:
            f.write("status=pass\n")
            f.write(f"action={action}\n")
            f.write(f"pkg={out_pkg}\n")
            f.write(f"name={out_name}\n")
        print(summary)
        return 0

    except Reject as e:
        with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a") as f:
            f.write("status=fail\n")
            # Multi-line safe: GitHub Actions heredoc form.
            f.write(f"reason<<EOF\n{e}\nEOF\n")
        print(str(e), file=sys.stderr)
        return 0  # the workflow reports this as a comment, not as a red run


if __name__ == "__main__":
    sys.exit(main())
