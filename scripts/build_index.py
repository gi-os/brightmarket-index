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

import tempfile

import yaml

try:
    from pyaxmlparser import APK
except ImportError:  # keeps the script runnable without the optional dep
    APK = None

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


def version_code_from_tag(tag: str) -> int | None:
    """v1.3.18 -> 18. Only a fallback; see read_apk."""
    try:
        return int(tag.lstrip("v").rsplit(".", 1)[-1])
    except (ValueError, IndexError):
        return None


def read_apk(blob: bytes) -> tuple[int, str] | None:
    """Read the real (versionCode, applicationId) out of an APK.

    This is the authoritative source and the tag is not. The client compares
    the index's versionCode against PackageManager's longVersionCode, so it has
    to be the number actually compiled into the APK.

    Deriving it from the tag only works for repos whose CI stamps a monotonic
    run number. Across the wider portfolio tags look like `build-50`,
    `v1.2.0-build.21` and plain semver -- and for plain semver the trailing
    segment goes BACKWARDS on a minor bump (1.2.2 -> 2, then 1.3.0 -> 0), which
    would tell every user they were already up to date, permanently.
    """
    if APK is None:
        return None
    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".apk", delete=False) as f:
            f.write(blob)
            path = f.name
        apk = APK(path)
        code = int(apk.version_code)
        pkg = apk.package
        if not pkg:
            return None
        return code, pkg
    except Exception:
        return None
    finally:
        if path and os.path.exists(path):
            os.unlink(path)


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

        prev = previous.get(pkg, {})

        # One extra API call per app; cheap, and it means a developer adding a
        # screenshot sees it appear without touching apps.yml.
        try:
            repo_meta = get(f"{API}/repos/{repo}")
            screenshots = find_screenshots(repo, repo_meta.get("default_branch", "main"))
        except urllib.error.HTTPError:
            screenshots = prev.get("screenshots", [])

        # Download once: the same bytes give us the hash the client verifies and
        # the versionCode it compares against.
        try:
            blob = get_bytes(asset["browser_download_url"])
        except urllib.error.HTTPError as e:
            warn(f"{pkg}: could not download the APK ({e.code}) -- skipped")
            continue
        sha256 = hashlib.sha256(blob).hexdigest()

        parsed = read_apk(blob)
        if parsed is None:
            version_code = version_code_from_tag(latest_release["tag_name"])
            if version_code is None:
                warn(f"{pkg}: couldn't read versionCode from the APK or the tag -- skipped")
                continue
            warn(f"{pkg}: fell back to the tag for versionCode ({version_code})")
        else:
            version_code, apk_pkg = parsed
            # apps.yml is hand-written and the APK is not. If they disagree the
            # APK wins, because that is the identity Android will actually use --
            # and a mismatch means the client would compare against a package
            # that isn't installed and offer an eternal "update".
            if apk_pkg != pkg:
                warn(f"{pkg}: apps.yml disagrees with the APK ({apk_pkg}) -- using the APK")
                pkg = apk_pkg

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
                # Carried through so the UI can mark an app that is kept for
                # its history but shouldn't be installed fresh.
                "deprecated": bool(app.get("deprecated", False)),
                "supersededBy": app.get("supersededBy", ""),
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

    # A stable /apk link that always points at the newest BrightMarket build.
    #
    # GitHub's own "latest release" download URL needs the exact filename, which
    # changes every release, so a hand-written redirect would go stale. This is
    # regenerated on every index build (every release, plus the cron), so it
    # tracks whatever is current.
    market = next((a for a in out if a["pkg"] == "com.gios.brightmarket"), None)
    if market:
        apk_url = market["latest"]["apk"]
        version = market["latest"]["version"]
        apk_dir = os.path.join(root, "apk")
        os.makedirs(apk_dir, exist_ok=True)
        with open(os.path.join(apk_dir, "index.html"), "w") as f:
            f.write(
                "<!DOCTYPE html>\n"
                "<!-- Generated by scripts/build_index.py. Do not edit by hand. -->\n"
                '<html lang="en"><head><meta charset="utf-8" />\n'
                f'<meta http-equiv="refresh" content="0; url={apk_url}" />\n'
                f'<link rel="canonical" href="{apk_url}" />\n'
                "<title>BrightMarket " + version + "</title>\n"
                "<style>body{background:#000;color:#fff;font-family:sans-serif;"
                "padding:40px;text-align:center}a{color:#fff}</style></head>\n"
                "<body><p>Downloading BrightMarket " + version + "…</p>\n"
                # A plain link as well as the refresh: some in-app browsers
                # ignore meta refresh, and a blank page with no way forward is
                # the worst possible outcome for a download link on a phone.
                f'<p><a href="{apk_url}">Tap here if it doesn\'t start</a></p>\n'
                "</body></html>\n"
            )
        print(f"  /apk -> {version}")

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
