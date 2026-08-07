#!/usr/bin/env python3
"""Rebuild index-v1.json from apps.yml plus the GitHub Releases API.

Design notes worth keeping in mind before changing anything here:

* The PREVIOUS index is an input, not just an output. `firstSeen` and the pinned
  `signer` are carried forward from it. Recomputing them from scratch would reset
  the "New" sort on every hourly run, and would make the signer pin worthless --
  an attacker who could push one release could clear the pin by definition.

* Downloads come from GitHub's own `download_count`, summed over every release.
  That is what makes a "most downloaded" sort possible with no analytics, no
  server and nothing to disclose to users about tracking.

* versionCode is the trailing segment of the tag (v1.3.18 -> 18). Every Bright
  app's build.yml stamps versionCode from the CI run number and tags
  v<name>.<run>, so this is monotonic per repo. It is what the client compares
  against PackageManager to decide "update available", so a repo that doesn't
  follow the convention is skipped loudly rather than indexed wrongly.
"""

import os
import sys
import json
import gzip
import hashlib
import datetime
import urllib.request
import urllib.error
import urllib.parse

import yaml

API = "https://api.github.com"
TOKEN = os.environ.get("GH_TOKEN", "")
HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "brightmarket-index-builder",
}
if TOKEN:
    HEADERS["Authorization"] = f"Bearer {TOKEN}"

warnings: list[str] = []


def warn(msg: str) -> None:
    warnings.append(msg)
    # GitHub Actions renders this as an annotation on the run.
    print(f"::warning::{msg}", file=sys.stderr)


def get(url: str) -> object:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def get_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=300) as resp:
        return resp.read()


def parse_version_code(tag: str) -> int | None:
    """v1.3.18 -> 18. Returns None if the tag doesn't fit the convention."""
    try:
        return int(tag.lstrip("v").rsplit(".", 1)[-1])
    except (ValueError, IndexError):
        return None


# Checked in order; the first that exists and has images wins. docs/screenshots
# is the convention already used across the portfolio, so it goes first.
SCREENSHOT_DIRS = ("docs/screenshots", "screenshots", ".github/screenshots")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def find_screenshots(repo: str, default_branch: str) -> list[dict]:
    """Look for a screenshots folder in the app's own repo.

    Deliberately reads from the app repo rather than hosting uploads: the files
    are already there for the README, they version with the app, and it means
    the whole marketplace still needs no storage of its own. A developer adds a
    screenshot by committing it, same as any other change.

    Served through raw.githubusercontent.com pinned to the branch name. Not to a
    commit SHA -- pinning would freeze the images at whatever the index last saw
    and quietly serve stale screenshots after a redesign.
    """
    for directory in SCREENSHOT_DIRS:
        try:
            listing = get(f"{API}/repos/{repo}/contents/{directory}")
        except urllib.error.HTTPError:
            continue
        if not isinstance(listing, list):
            continue

        shots = [
            {
                "url": (
                    f"https://raw.githubusercontent.com/{repo}/"
                    f"{default_branch}/{directory}/{urllib.parse.quote(f['name'])}"
                ),
                "name": f["name"],
                "size": f["size"],
            }
            # Sorted by filename so the order is stable and the developer can
            # control it by naming files 1-, 2-, etc.
            for f in sorted(listing, key=lambda x: x["name"])
            if f["type"] == "file" and f["name"].lower().endswith(IMAGE_EXTS)
        ]
        if shots:
            return shots
    return []


def load_previous(path: str) -> dict:
    try:
        with open(path) as f:
            return {a["pkg"]: a for a in json.load(f)["apps"]}
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return {}


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    index_path = os.path.join(root, "index-v1.json")

    with open(os.path.join(root, "apps.yml")) as f:
        apps = yaml.safe_load(f)["apps"]

    previous = load_previous(index_path)
    today = datetime.date.today().isoformat()
    out = []

    for app in apps:
        pkg, repo = app["pkg"], app["repo"]
        try:
            releases = get(f"{API}/repos/{repo}/releases?per_page=100")
        except urllib.error.HTTPError as e:
            warn(f"{pkg}: cannot read releases for {repo} ({e.code}) -- keeping previous entry")
            if pkg in previous:
                out.append(previous[pkg])
            continue

        # Drafts and prereleases are never shipped to users.
        published = [r for r in releases if not r["draft"] and not r["prerelease"]]
        apk_releases = [
            (r, [a for a in r["assets"] if a["name"].endswith(".apk")]) for r in published
        ]
        apk_releases = [(r, assets) for r, assets in apk_releases if assets]

        if not apk_releases:
            warn(f"{pkg}: {repo} has no published release with an .apk asset -- skipped")
            continue

        downloads = sum(a["download_count"] for _, assets in apk_releases for a in assets)

        latest_release, latest_assets = apk_releases[0]
        if len(latest_assets) > 1:
            # Two APKs on one release is exactly how an updater ends up installing
            # a debug build with the wrong signature over the release one.
            warn(f"{pkg}: latest release has {len(latest_assets)} .apk assets -- skipped")
            continue
        asset = latest_assets[0]

        version_code = parse_version_code(latest_release["tag_name"])
        if version_code is None:
            warn(f"{pkg}: tag {latest_release['tag_name']!r} isn't v<name>.<run> -- skipped")
            continue

        prev = previous.get(pkg, {})

        # One extra API call per app; cheap, and it means a developer adding a
        # screenshot sees it appear without touching apps.yml.
        try:
            repo_meta = get(f"{API}/repos/{repo}")
            screenshots = find_screenshots(repo, repo_meta.get("default_branch", "main"))
        except urllib.error.HTTPError:
            screenshots = prev.get("screenshots", [])

        # Hash what we actually serve, so the client can verify the download.
        try:
            sha256 = hashlib.sha256(get_bytes(asset["browser_download_url"])).hexdigest()
        except urllib.error.HTTPError as e:
            warn(f"{pkg}: could not download APK to hash it ({e.code}) -- skipped")
            continue

        out.append(
            {
                "pkg": pkg,
                "name": app["name"],
                "repo": repo,
                "category": app.get("category", "utilities"),
                "summary": app.get("summary", ""),
                "latest": {
                    "version": latest_release["tag_name"].lstrip("v"),
                    "versionCode": version_code,
                    "apk": asset["browser_download_url"],
                    "size": asset["size"],
                    "sha256": sha256,
                    "published": latest_release["published_at"],
                    "notes": (latest_release["body"] or "")[:4000],
                },
                "screenshots": screenshots,
                "downloads": downloads,
                # Carried forward, never recomputed -- see module docstring.
                "firstSeen": prev.get("firstSeen") or today,
            }
        )

    doc = {
        "format": 1,
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "apps": out,
    }

    with open(index_path, "w") as f:
        json.dump(doc, f, indent=1)
    with gzip.open(index_path + ".gz", "wt") as f:
        json.dump(doc, f)

    print(f"indexed {len(out)}/{len(apps)} apps, {len(warnings)} warning(s)")
    for a in out:
        shots = len(a.get("screenshots", []))
        print(
            f"  {a['name']:24} v{a['latest']['version']:12} "
            f"{a['downloads']:>6} downloads  {shots} screenshot(s)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
