#!/usr/bin/env python3
"""Rebuild index-v1.json from apps/ plus the GitHub Releases API.

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

# See the collapse guard at the end of main(). A floor rather than a fixed count so it scales
# with the catalogue, and a minimum so that the first few submissions are not fighting it.
SHRINK_FLOOR = 0.75
SHRINK_MIN = 10


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


def signer_of(apk) -> str:
    """SHA-256 of the APK's signing certificate, or "" if it can't be read.

    Android identifies an app by (applicationId, signing certificate), so this
    is the only thing that distinguishes the real app from a rebuild by someone
    else under the same package name. Pinned on first sight and compared on
    every run after: a change means the app has changed hands, which is exactly
    the event nobody would otherwise notice.

    v3 first, then v2, then the old JAR signature -- newest scheme wins because
    that is the one Android verifies against. Failing to read one is not treated
    as a mismatch; "" means "unknown", and unknown never trips the alarm.
    """
    for reader in ("get_certificates_der_v3", "get_certificates_der_v2"):
        try:
            ders = getattr(apk, reader)() or []
            if ders:
                return hashlib.sha256(ders[0]).hexdigest()
        except Exception:
            pass
    try:
        names = apk.get_signature_names() or []
        if names:
            return hashlib.sha256(apk.get_certificate_der(names[0])).hexdigest()
    except Exception:
        pass
    return ""


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
        return code, pkg, signer_of(apk)
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


def build_preview(pkg: str, prereleases: list, carried: dict | None) -> dict | None:
    """The newest prerelease with exactly one .apk, hashed and read.

    Returns None when there is no usable prerelease, and also when the newest
    prerelease is older than nothing at all -- the caller drops the key entirely
    in that case rather than publishing an empty object, so a client can test
    for presence.

    Re-uses the carried-forward entry when the tag hasn't moved. Without that
    this would download every app's nightly on every run, which at a fifteen
    minute schedule is a lot of bandwidth to confirm nothing changed.
    """
    candidates = [
        (r, [a for a in r["assets"] if a["name"].endswith(".apk")])
        for r in prereleases
    ]
    candidates = [(r, assets) for r, assets in candidates if len(assets) == 1]
    if not candidates:
        return None

    release, assets = candidates[0]
    asset = assets[0]

    if carried and carried.get("version") == release["tag_name"].lstrip("v"):
        return carried

    try:
        blob = get_bytes(asset["browser_download_url"])
    except urllib.error.HTTPError as e:
        warn(f"{pkg}: could not download the nightly ({e.code}) -- keeping the previous one")
        return carried

    parsed = read_apk(blob)
    if parsed is None:
        warn(f"{pkg}: couldn't read the nightly APK -- ignoring it")
        return carried
    version_code, _apk_pkg, _signer = parsed

    return {
        "version": release["tag_name"].lstrip("v"),
        "versionCode": version_code,
        "apk": asset["browser_download_url"],
        "size": asset["size"],
        "sha256": hashlib.sha256(blob).hexdigest(),
        "published": release["published_at"],
        "notes": (release["body"] or "")[:2000],
    }


def load_catalogue(root: str) -> list[dict]:
    """Every app, one YAML file each, under apps/.

    This was a single apps.yml holding one list. Every submission appended to the
    end of it, so any two submissions open at the same time modified the same
    line and the second to be merged always conflicted -- through no fault of
    either author. One file per app means two submissions cannot touch the same
    bytes, so they cannot conflict, and no amount of merge order matters.

    Filenames are the applicationId, which the validator already refuses to
    duplicate, so uniqueness comes for free.

    Order here is irrelevant: every client sorts by name, date or downloads.
    Sorted anyway, so a rebuild that changes nothing produces no diff.
    """
    directory = os.path.join(root, "apps")
    apps = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith((".yml", ".yaml")):
            continue
        with open(os.path.join(directory, name)) as f:
            entry = yaml.safe_load(f)
        if not entry or "pkg" not in entry:
            warn(f"apps/{name}: no pkg, skipped")
            continue
        apps.append(entry)
    return apps


PUBLISHED_INDEX = "https://brightmarket.gzl.dev/index-v1.json"


def load_previous(path: str) -> tuple[dict, dict]:
    """The last published index, keyed by pkg and also by repo.

    By repo as well because the pkg in a catalogue file can be wrong -- it is only
    authoritative once read out of the APK -- and the repo is the one key that
    is known before anything is downloaded. That is what makes it possible to
    ask "is this the same release as last time?" without fetching it first.

    Read from the LIVE SITE, not from the working tree. The index is no longer
    committed -- it is deployed straight to Pages -- so the published file is the
    only record of firstSeen, the pinned signing certificates, and the hashes
    that let an unchanged release skip its download.

    A failure here is fatal on purpose. Carrying on with an empty history would
    silently reset every firstSeen, re-pin every signer against whatever is being
    served right now, and re-download every APK. The first two are security
    properties; quietly rebuilding them from scratch is precisely what a pinned
    certificate exists to prevent. A stale site for fifteen minutes is the far
    better failure.
    """
    apps = None

    # One-time recovery hatch. When the published index has lost history -- as it did on
    # 2026-08-17, when a Releases API degradation emptied it down to three apps and took every
    # firstSeen with it -- there is nowhere left to carry those fields forward from, because the
    # published file is the only place they live. Pointing this at a recovered copy (the Pages
    # artifact of the last good run keeps for a day) restores them in a single run.
    #
    # Deliberately explicit and deliberately loud: it overrides the live index, which is the one
    # thing that must never happen by accident. Passed per-run through workflow_dispatch, never
    # set as a repository variable.
    seed = os.environ.get("SEED_INDEX", "").strip()
    if seed:
        with open(seed) as f:
            apps = json.load(f)["apps"]
        warn(f"history seeded from {seed} ({len(apps)} apps), NOT from the published index")
        return (
            {a["pkg"]: a for a in apps},
            {a["repo"].lower(): a for a in apps if a.get("repo")},
        )

    try:
        req = urllib.request.Request(
            f"{PUBLISHED_INDEX}?t={int(datetime.datetime.now().timestamp())}",
            headers={"User-Agent": "brightmarket-index"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            apps = json.load(r)["apps"]
    except Exception as e:
        # Local runs and the very first deploy have no published index yet; a
        # file on disk is an acceptable stand-in for those, and only those.
        try:
            with open(path) as f:
                apps = json.load(f)["apps"]
            warn(f"couldn't read the published index ({e}); used the local copy")
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            if os.environ.get("ALLOW_EMPTY_HISTORY") == "1":
                warn("no previous index anywhere; starting fresh because ALLOW_EMPTY_HISTORY=1")
                return {}, {}
            raise SystemExit(
                f"FATAL: could not read the previous index from {PUBLISHED_INDEX} ({e}) "
                "and no local copy exists. Refusing to publish an index with no "
                "history: it would reset every firstSeen and re-pin every signing "
                "certificate. Set ALLOW_EMPTY_HISTORY=1 only for a genuine first run."
            )
    return (
        {a["pkg"]: a for a in apps},
        {a["repo"].lower(): a for a in apps if a.get("repo")},
    )


def unchanged(prev: dict | None, asset: dict) -> bool:
    """True when the previous entry already describes this exact asset.

    Every run downloaded every APK in full, purely to recompute a hash of bytes
    that had not moved. That is tens of gigabytes a day at a fifteen minute
    schedule -- and, measurably, +2 on each app's GitHub download counter per
    run, which would have made the Popular sort a measure of how long an app had
    been listed rather than how many people wanted it.
    """
    if not prev:
        return False
    latest = prev.get("latest") or {}
    return (
        latest.get("apk") == asset["browser_download_url"]
        and latest.get("size") == asset["size"]
        and bool(latest.get("sha256"))
        and latest.get("versionCode") is not None
    )


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    index_path = os.path.join(root, "index-v1.json")

    apps = load_catalogue(root)

    previous, previous_by_repo = load_previous(index_path)
    today = datetime.date.today().isoformat()
    out = []

    for app in apps:
        pkg, repo = app["pkg"], app["repo"]
        # Keyed on repo, which is known now; pkg isn't trustworthy until the APK
        # has been read. Two things below need it.
        carried = previous_by_repo.get(repo.lower())
        try:
            releases = get(f"{API}/repos/{repo}/releases?per_page=100")
        except urllib.error.HTTPError as e:
            warn(f"{pkg}: cannot read releases for {repo} ({e.code}) -- keeping previous entry")
            # carried first, for the same reason as the empty-list branch below: the catalogue's
            # pkg can be wrong, and repo is the only key that is trustworthy before a download.
            keep = carried or previous.get(pkg)
            if keep:
                out.append(keep)
            continue

        # Drafts and prereleases are never the default. A prerelease is now a
        # deliberate channel rather than a mistake -- BrightMarket publishes a
        # nightly on every push -- so the newest one is recorded separately,
        # below, for the people who have asked to be on it. Everyone else never
        # sees it.
        published = [r for r in releases if not r["draft"] and not r["prerelease"]]
        prereleases = [r for r in releases if not r["draft"] and r["prerelease"]]
        apk_releases = [
            (r, [a for a in r["assets"] if a["name"].endswith(".apk")]) for r in published
        ]
        apk_releases = [(r, assets) for r, assets in apk_releases if assets]

        if not apk_releases:
            # An app already in the index does not quietly stop having releases. When the API
            # says it has none, that is far more often the API than the developer -- so the
            # previous entry is kept and the listing survives, stale at worst.
            #
            # This asymmetry is what wiped the store on 2026-08-17. An HTTP error here already
            # kept the previous entry (see above), but a *successful* response with an empty
            # list was treated as "this app genuinely has no release" and dropped it. During a
            # Releases API degradation every app got exactly that, so the published index fell
            # 46 -> 4 -> 3 over a few hourly runs, each one carrying less history than the last.
            # "The API told me nothing" and "there is nothing" are not the same answer.
            # Keyed on repo first, not pkg. Nothing has been downloaded at this point, so the
            # only pkg available is the catalogue's -- and that is exactly the one the file
            # warns can be wrong. com.thelightphone.sdk is listed under a pkg its APK does not
            # have, so a pkg-keyed lookup silently fails to find it and drops it anyway.
            keep = carried or previous.get(pkg)
            if keep:
                warn(
                    f"{pkg}: {repo} reports no published release with an .apk -- "
                    "keeping previous entry"
                )
                out.append(keep)
            else:
                warn(f"{pkg}: {repo} has no published release with an .apk asset -- skipped")
            continue

        downloads = sum(a["download_count"] for _, assets in apk_releases for a in assets)

        latest_release, latest_assets = apk_releases[0]
        if len(latest_assets) > 1:
            # Two APKs on one release is exactly how an updater ends up installing
            # a debug build with the wrong signature over the release one.
            #
            # The bad release is refused, but the app keeps its last good listing rather than
            # disappearing from the store: the mistake is in one release, not in the app, and
            # removing it would also take its firstSeen and its signer pin with it.
            warn(f"{pkg}: latest release has {len(latest_assets)} .apk assets -- skipped")
            keep = carried or previous.get(pkg)
            if keep:
                out.append(keep)
            continue
        asset = latest_assets[0]

        # Deliberately NOT `previous.get(pkg)` yet: pkg here is still whatever
        # the catalogue claims, and it can be wrong. Looked up after the APK has had
        # its say, below.
        prev: dict = {}

        # Hunting for screenshots costs a /repos call for the default branch plus
        # one /contents call per candidate directory -- two to four requests per
        # app, which was most of this job's API traffic. And it was being spent
        # every fifteen minutes on something a developer changes maybe monthly,
        # while the download counts everyone assumes are the expensive part ride
        # along free on the releases call we have to make regardless.
        #
        # So the screenshots are what gets checked once a day, not the counts.
        shots_checked = (carried or {}).get("shotsChecked", "")
        if carried is not None and shots_checked == today:
            screenshots = carried.get("screenshots", [])
        else:
            try:
                repo_meta = get(f"{API}/repos/{repo}")
                screenshots = find_screenshots(repo, repo_meta.get("default_branch", "main"))
                shots_checked = today
            except urllib.error.HTTPError:
                # Unknown, not empty. Falls back to the carried list below rather
                # than blanking a listing because one request failed.
                screenshots = None

        # Nothing to learn from bytes that haven't moved. Keyed on repo because
        # the pkg in the catalogue may be wrong and the APK is what settles it -- and
        # that is the thing we are trying not to download.
        if unchanged(carried, asset):
            prev_latest = carried["latest"]
            sha256 = prev_latest["sha256"]
            version_code = prev_latest["versionCode"]
            pkg = carried["pkg"]
            signer = carried.get("signer", "")
            parsed = None
            skipped_download = True
        else:
            skipped_download = False
            # Download once: the same bytes give us the hash the client verifies
            # and the versionCode it compares against.
            try:
                blob = get_bytes(asset["browser_download_url"])
            except urllib.error.HTTPError as e:
                warn(f"{pkg}: could not download the APK ({e.code}) -- skipped")
                continue
            sha256 = hashlib.sha256(blob).hexdigest()
            parsed = read_apk(blob)
            signer = ""

        if not skipped_download and parsed is None:
            version_code = version_code_from_tag(latest_release["tag_name"])
            if version_code is None:
                warn(f"{pkg}: couldn't read versionCode from the APK or the tag -- skipped")
                continue
            warn(f"{pkg}: fell back to the tag for versionCode ({version_code})")
        elif not skipped_download:
            version_code, apk_pkg, signer = parsed
            # The catalogue entry is hand-written and the APK is not. If they disagree the
            # APK wins, because that is the identity Android will actually use --
            # and a mismatch means the client would compare against a package
            # that isn't installed and offer an eternal "update".
            if apk_pkg != pkg:
                warn(f"{pkg}: the catalogue disagrees with the APK ({apk_pkg}) -- using the APK")
                pkg = apk_pkg

        # Now that pkg is the one Android will use, and therefore the one the
        # previous index is keyed by. Doing this earlier meant an app whose
        # catalogue pkg was wrong looked new on every single run: firstSeen reset
        # daily, and -- the part that actually matters -- the signing
        # certificate never pinned, because there was never a previous entry to
        # pin it against. The app would have looked exactly as trustworthy as
        # the rest while carrying none of the guarantee.
        prev = previous.get(pkg, {})
        if screenshots is None:
            screenshots = prev.get("screenshots", [])

        # The nightly channel. Same treatment as the stable entry -- the APK is
        # downloaded, hashed and read for its real versionCode -- because a
        # nightly installs the same way and deserves the same check. Carried
        # forward untouched when the newest prerelease hasn't changed, so this
        # doesn't re-download tens of megabytes every fifteen minutes.
        preview = build_preview(pkg, prereleases, prev.get("preview"))

        # The pin. A first sighting records whatever the APK is signed with; a
        # later run that disagrees keeps the previous entry and shouts, rather
        # than quietly publishing a new hash for an app that is no longer the
        # same app. Refusing to update is the safe direction: the worst case is
        # a stale listing, which is visible, instead of a silent handover, which
        # is not.
        pinned = prev.get("signer", "")
        if pinned and signer and signer != pinned:
            warn(
                f"{pkg}: SIGNING CERTIFICATE CHANGED "
                f"({pinned[:16]}... -> {signer[:16]}...) -- keeping the previous "
                f"entry. If this is intentional, clear `signer` for {pkg} in the "
                f"published index."
            )
            out.append(prev)
            continue
        signer = signer or pinned

        out.append(
            {
                "pkg": pkg,
                "name": app["name"],
                "repo": repo,
                "category": app.get("category", "utilities"),
                "summary": app.get("summary", ""),
                # The community tool this app forks, as owner/repo. Empty for
                # original apps; the UI shows a "fork of" link when present.
                "upstream": app.get("upstream", ""),
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
                # ADB setup this app needs, as its README writes it. Carried
                # through verbatim: BrightMarket shows the same words to the
                # person approving them, and BrightControl re-parses every line
                # and rebuilds the command pinned to this package rather than
                # running what it was handed. Hand-maintained, like name and
                # summary -- the builder cannot read a README and know which
                # lines are setup and which are examples.
                **({"adb": [str(c) for c in app["adb"] if str(c).strip()]}
                   if app.get("adb") else {}),
                "screenshots": screenshots,
                "shotsChecked": shots_checked,
                "downloads": downloads,
                # Carried forward, never recomputed -- see module docstring.
                "firstSeen": prev.get("firstSeen") or today,
                "signer": signer,
                # Absent for every app that publishes no prereleases, which is
                # most of them. Clients treat a missing preview as "no nightly".
                **({"preview": preview} if preview else {}),
            }
        )

    # Refuse to publish a collapse.
    #
    # The carry-forward above should make this unreachable, which is the point of having it: it
    # is the backstop for the next failure nobody predicted, not for the one that already
    # happened. On 2026-08-17 the index shrank 46 -> 4 -> 3 across three green runs and nothing
    # said a word; the store was broken for hours and a person had to notice. A failed run leaves
    # the last good site deployed and puts a red mark in the Actions list, which is the outcome
    # that was wanted all along -- the same reasoning load_previous already applies to missing
    # history: a stale site for fifteen minutes is the far better failure.
    #
    # A removal takes one app at a time, so the floor only has to clear ordinary churn. Set
    # ALLOW_SHRINK=1 for a deliberate mass removal.
    if len(previous) >= SHRINK_MIN and len(out) < len(previous) * SHRINK_FLOOR:
        lost = sorted(set(previous) - {a["pkg"] for a in out})
        if os.environ.get("ALLOW_SHRINK") != "1":
            raise SystemExit(
                f"FATAL: this build indexed {len(out)} apps, down from {len(previous)} in the "
                f"published index -- more than the {int((1 - SHRINK_FLOOR) * 100)}% drop this "
                f"refuses to publish.\n"
                f"Missing: {', '.join(lost)}\n"
                "Nothing has been deployed, so the site still serves the previous index. Read "
                "the warnings above: an upstream outage is the usual cause and the next run "
                "normally recovers on its own. Set ALLOW_SHRINK=1 for a deliberate removal."
            )
        warn(f"publishing a {len(previous)} -> {len(out)} shrink because ALLOW_SHRINK=1")

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
                # Root-relative, because this page lives one level down.
                '<link rel="icon" href="/favicon.svg" type="image/svg+xml" />\n'
                '<link rel="alternate icon" href="/favicon.ico" />\n'
                '<meta name="theme-color" content="#000000" />\n'
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
