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


def parse_issue(body: str) -> tuple[str, str]:
    """Pull repo + category out of either the issue-form rendering or the
    portal's plain `**Repo:** <url>` format."""
    repo_m = re.search(r"github\.com/([\w.-]+/[\w.-]+)", body) or re.search(
        r"^\s*([\w.-]+/[\w.-]+)\s*$", body, re.M
    )
    cat_m = re.search(r"(reading|utilities|games|media|productivity|hardware)", body, re.I)
    if not repo_m:
        raise Reject("I couldn't find a repo in this issue. Give me `owner/name` or a GitHub URL.")
    if not cat_m:
        raise Reject(f"No category found. Pick one of: {', '.join(sorted(VALID_CATEGORIES))}.")
    return repo_m.group(1).removesuffix(".git"), cat_m.group(1).lower()


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
        repo, category = parse_issue(body)
        info = validate(repo)

        with open(apps_path) as f:
            doc = yaml.safe_load(f)
        existing = doc["apps"]

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
                "name": info["name"],
                "repo": repo,
                "category": category,
                "summary": info["summary"],
            }
        )
        with open(apps_path, "w") as f:
            yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False, allow_unicode=True)

        with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a") as f:
            f.write("status=pass\n")
            f.write(f"pkg={info['pkg']}\n")
            f.write(f"name={info['name']}\n")
        print(
            f"Validated **{info['name']}** (`{info['pkg']}`) — latest release "
            f"`{info['version']}`, one asset `{info['apk']}`."
        )
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
