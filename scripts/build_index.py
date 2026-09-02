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
import html
import hashlib
import datetime
import urllib.request
import urllib.error
import urllib.parse

import tempfile

import yaml

from apk_assets import pick_apk

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
    """The newest prerelease with one unambiguous .apk, hashed and read.

    Returns None when there is no usable prerelease, and also when the newest
    prerelease is older than nothing at all -- the caller drops the key entirely
    in that case rather than publishing an empty object, so a client can test
    for presence.

    Re-uses the carried-forward entry when the tag hasn't moved. Without that
    this would download every app's nightly on every run, which at a fifteen
    minute schedule is a lot of bandwidth to confirm nothing changed.
    """
    candidates = [(r, pick_apk(r)[0]) for r in prereleases]
    candidates = [(r, asset) for r, asset in candidates if asset]
    if not candidates:
        return None

    release, asset = candidates[0]

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

SITE = "https://brightmarket.gzl.dev"


def icon_url(root: str, pkg: str) -> str:
    """The published icon for a package, or "" if it has none.

    Icons are committed under icons/ by scripts/extract_icons.py and served by
    Pages alongside this file, so the only question here is whether the file
    exists. Deliberately not generated during a build: this workflow has
    contents: read and deploys rather than commits, so anything it wrote would
    live for exactly one run.

    An absolute URL rather than a path, because the client is an Android app
    holding one string, not a browser with a base URL.
    """
    return f"{SITE}/icons/{pkg}.png" if os.path.exists(
        os.path.join(root, "icons", f"{pkg}.png")
    ) else ""


def slugify(name: str) -> str:
    """A short, typeable key for an app: BrightNotebook -> brightnotebook."""
    parts = "".join(c if c.isalnum() else "-" for c in name.lower()).split("-")
    return "-".join(p for p in parts if p)


def share_page(entry: dict) -> str:
    """The static page behind /app/<key>/.

    A redirect carrying preview tags. The redirect is what a person gets: the
    catalogue, opened with this app in the hero. The tags are what a chat
    client, a forum or a crawler gets, and they are the whole reason this is a
    file per app instead of a query string alone -- a preview is read out of the
    document, and browse.html is one document for the entire catalogue, so it
    can only ever describe the catalogue.

    Written on every index build like /apk, and never committed: the workflow
    has contents: read and deploys the tree it just built, so a committed copy
    would only ever be a stale one.
    """
    pkg = entry["pkg"]
    target = f"{SITE}/browse.html?app={pkg}"
    canonical = f"{SITE}/app/{pkg}/"
    name = html.escape(entry["name"])
    summary = html.escape(entry.get("summary", ""))
    version = html.escape(str(entry["latest"]["version"]))
    apk = html.escape(entry["latest"]["apk"])
    repo = html.escape(entry.get("repo", ""))
    # Falls back to the site mark rather than omitting the tag: a preview with
    # no image is a preview some clients decline to render at all.
    image = html.escape(entry.get("icon") or f"{SITE}/favicon-512.png")
    # json.dumps for the script, so a quote in nothing here can ever break out.
    return (
        "<!DOCTYPE html>\n"
        "<!-- Generated by scripts/build_index.py. Do not edit by hand. -->\n"
        '<html lang="en"><head><meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        f"<title>{name} — BrightMarket</title>\n"
        f'<meta name="description" content="{summary}" />\n'
        f'<link rel="canonical" href="{canonical}" />\n'
        '<meta property="og:type" content="website" />\n'
        '<meta property="og:site_name" content="BrightMarket" />\n'
        f'<meta property="og:title" content="{name} for the Light Phone III" />\n'
        f'<meta property="og:description" content="{summary}" />\n'
        f'<meta property="og:image" content="{image}" />\n'
        f'<meta property="og:url" content="{canonical}" />\n'
        '<meta name="twitter:card" content="summary" />\n'
        f'<meta name="twitter:title" content="{name} for the Light Phone III" />\n'
        f'<meta name="twitter:description" content="{summary}" />\n'
        f'<meta name="twitter:image" content="{image}" />\n'
        # After the tags, so a client that stops reading at the refresh has
        # already seen everything it needs to draw a preview.
        f'<meta http-equiv="refresh" content="0; url={target}" />\n'
        '<link rel="icon" href="/favicon.svg" type="image/svg+xml" />\n'
        '<link rel="alternate icon" href="/favicon.ico" />\n'
        '<meta name="theme-color" content="#000000" />\n'
        "<style>body{background:#000;color:#fff;font-family:ui-sans-serif,system-ui,sans-serif;"
        "padding:40px;text-align:center;line-height:1.6}a{color:#fff}"
        "h1{font-size:22px;margin:0 0 6px}p{margin:6px 0;color:#b4b4b8}"
        "img{width:96px;height:96px;border-radius:14px;border:2px solid #fff}</style>\n"
        f"<script>location.replace({json.dumps(target)});</script>\n"
        "</head>\n"
        f'<body><img src="{image}" alt="" />\n'
        f"<h1>{name}</h1>\n"
        f"<p>{summary}</p>\n"
        # A plain link as well as the refresh and the script: an in-app browser
        # that honours neither still has a way forward instead of a black page.
        f'<p>Opening BrightMarket… <a href="{target}">continue</a></p>\n'
        f'<p><a href="{apk}">Download {name} {version} directly</a>'
        f' · <a href="https://github.com/{repo}">Source</a></p>\n'
        "</body></html>\n"
    )


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


PUBLISHED_HISTORY = f"{SITE}/history-v1.json"
HISTORY_DAYS = 400


def load_history() -> dict:
    """The per-day download snapshots behind /stats.html.

    Shape: {"days": {"2026-09-02": {"<pkg>": <lifetime downloads>, ...}, ...}}. One
    entry per app per UTC day; the last build of the day wins, so a day's number is
    "where the counter stood at the end of that day". Everything on the stats page
    -- the top-20 lines, the movers -- is a difference between two of these snapshots.

    Two more sections exist only for "+21 today":

    * "open": the first snapshot taken on the current day, per release. Kept so the
      marker has a baseline on the day the history starts (and on the first day after
      a gap) instead of reading +0 until midnight.
    * "releases": per-release counts for the last few days. A lifetime total is a sum
      over releases, and it goes DOWN when old releases are pruned -- BrightControl
      read 358 then 351 on the same afternoon. Differencing totals would then hide a
      real day's gains behind the drop, so "today" is summed per release instead:
      a tag that vanished simply drops out, a new tag counts in full. Nothing is counted client-side and nothing about visitors is
    recorded: the source is still GitHub's own download_count, just remembered.

    Like the index, this lives only in the deployment, so it is read back from the
    live site. A 404 is the one normal start (no history has ever been published);
    any other failure is fatal for the same reason load_previous is -- carrying on
    would publish a history file with one day in it over one with months.
    """
    if os.environ.get("ALLOW_EMPTY_HISTORY") == "1":
        return {"days": {}, "open": {}, "releases": {}}
    url = f"{PUBLISHED_HISTORY}?t={int(datetime.datetime.now().timestamp())}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "brightmarket-index"})
        with urllib.request.urlopen(req, timeout=60) as r:
            doc = json.load(r)
        days = doc.get("days") or {}
        if not isinstance(days, dict):
            raise ValueError("days is not an object")
        return {"days": days, "open": doc.get("open") or {}, "releases": doc.get("releases") or {}}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            warn("no published history yet; starting the download history today")
            return {"days": {}, "open": {}, "releases": {}}
        raise SystemExit(f"FATAL: could not read {PUBLISHED_HISTORY} ({e}); refusing to publish "
                         "a history that would overwrite the real one")
    except Exception as e:
        raise SystemExit(f"FATAL: could not read {PUBLISHED_HISTORY} ({e}); refusing to publish "
                         "a history that would overwrite the real one")


def get_all_releases(repo: str) -> list:
    """Every release, not the first hundred.

    A single page silently capped the download total of any app with more than 100
    releases, and -- worse -- made it FALL with each new release, as the oldest one
    dropped off the page and took its downloads with it. BrightControl had 210
    releases and read 358 when GitHub's own total was around 725. Capped at ten
    pages, which is a thousand releases, so one runaway repo cannot eat the rate
    limit.
    """
    out: list = []
    for page in range(1, 11):
        chunk = get(f"{API}/repos/{repo}/releases?per_page=100&page={page}")
        out.extend(chunk)
        if len(chunk) < 100:
            break
    return out


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    index_path = os.path.join(root, "index-v1.json")

    apps = load_catalogue(root)

    previous, previous_by_repo = load_previous(index_path)
    # UTC, so the day boundary is the same one the history snapshots use.
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    history = load_history()
    out = []

    for app in apps:
        pkg, repo = app["pkg"], app["repo"]
        # Keyed on repo, which is known now; pkg isn't trustworthy until the APK
        # has been read. Two things below need it.
        carried = previous_by_repo.get(repo.lower())
        try:
            releases = get_all_releases(repo)
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
        # (release, chosen asset). pick_apk resolves a release that carries a debug
        # build alongside the release one instead of discarding it -- the same call
        # the validator makes, from the same module, so a submission that was
        # admitted cannot then be skipped here forever.
        apk_releases = [(r, pick_apk(r)[0]) for r in published]
        apk_releases = [(r, asset) for r, asset in apk_releases if asset]

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

        # Counted over the chosen asset of each release, not over every .apk on it.
        # A repo publishing a debug build every time used to have both downloads
        # summed into its total, which flattered it in the Popular sort against a
        # repo shipping one file.
        downloads = sum(asset["download_count"] for _, asset in apk_releases)

        latest_release, asset = apk_releases[0]

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
                # Per release, for the history baseline. Popped before the index is written.
                "_perRelease": {r["tag_name"]: asset["download_count"] for r, asset in apk_releases},
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

    # The icon, stamped over every entry at the end rather than inside the loop.
    #
    # Several paths above append a *carried* entry -- an app whose releases could
    # not be read, or whose signer no longer matches -- and those carry last
    # run's fields. Setting the icon there too would mean a newly added icon did
    # not appear until the app happened to release, which for the apps that get
    # carried is exactly the case that never happens.
    for entry in out:
        url = icon_url(root, entry["pkg"])
        if url:
            entry["icon"] = url
        else:
            # Eighteen of these declare no icon anywhere. Absent, not empty:
            # clients test for the key and draw a lettered tile instead.
            entry.pop("icon", None)

    # Today's snapshot, then "+N today" on every entry.
    #
    # The baseline is per release (see load_history): the last day before today that
    # has one, else the first snapshot taken today. On an app's very first day in the
    # index there is nothing to compare against and it reads 0, which is honest.
    days = history["days"]
    rel = history["releases"]
    opened = history["open"]
    per_release = {a["pkg"]: a.pop("_perRelease", None) for a in out}
    # A carried entry (releases unreadable this run) keeps whatever it had.
    for pkg in list(per_release):
        if per_release[pkg] is None:
            per_release[pkg] = (rel.get(today) or {}).get(pkg) or (opened.get("counts") or {}).get(pkg) or {}
    days[today] = {a["pkg"]: a["downloads"] for a in out}
    rel[today] = per_release
    if opened.get("day") != today:
        opened = {"day": today, "counts": per_release}
        # One-time recovery, like SEED_INDEX: a day's opening snapshot recovered from a
        # run log, for the day the history was first switched on. Totals only, so they
        # are wrapped as one pseudo-release and differenced as sums.
        seed = os.path.join(root, "recovery", f"open-{today}.json")
        if os.path.exists(seed):
            with open(seed) as f:
                seeded = json.load(f)
            if seeded.get("day") == today and isinstance(seeded.get("counts"), dict):
                opened["counts"] = {
                    p: (c if isinstance(c, dict) else {"*total*": c}) for p, c in seeded["counts"].items()
                }
                warn(f"opening snapshot for {today} seeded from {seed}")
    prior = [d for d in sorted(rel, reverse=True) if d < today]
    base_day = prior[0] if prior else None
    for a in out:
        pkg = a["pkg"]
        now = per_release.get(pkg) or {}
        base = (rel[base_day].get(pkg) if base_day else None) or opened["counts"].get(pkg) or {}
        if "*total*" in base:
            a["downloadsToday"] = max(0, a["downloads"] - base["*total*"])
        else:
            a["downloadsToday"] = sum(max(0, n - base.get(tag, 0)) for tag, n in now.items())
    for stale in sorted(days)[:-HISTORY_DAYS]:
        del days[stale]
    # Per-release detail is only needed for the baseline; a few days covers a gap.
    for stale in sorted(rel)[:-4]:
        del rel[stale]
    with open(os.path.join(root, "history-v1.json"), "w") as f:
        json.dump(
            {"format": 1,
             "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
             "days": {d: days[d] for d in sorted(days)},
             "open": opened,
             "releases": {d: rel[d] for d in sorted(rel)}},
            f, separators=(",", ":"),
        )
    print(f"  /history-v1.json -> {len(days)} day(s), +{sum(a['downloadsToday'] for a in out)} today")

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

    # A page per app, so an app is something you can link to.
    #
    # /app/<applicationId>/ always exists; /app/<name-slug>/ exists as well when
    # that slug belongs to exactly one app. Both land on the catalogue with the
    # app already in the hero, which is what browse.html's ?app= does with the
    # key it is given.
    app_root = os.path.join(root, "app")
    os.makedirs(app_root, exist_ok=True)
    by_slug: dict[str, list[str]] = {}
    for entry in out:
        by_slug.setdefault(slugify(entry["name"]), []).append(entry["pkg"])
    pages = 0
    for entry in out:
        keys = [entry["pkg"]]
        slug = slugify(entry["name"])
        # Only when it is unambiguous. Two apps sharing a slug would each
        # overwrite the other's page and whichever built last would win, which
        # is a link that quietly points at the wrong app.
        if slug and slug != entry["pkg"] and len(by_slug.get(slug, [])) == 1:
            keys.append(slug)
        page = share_page(entry)
        for key in keys:
            directory = os.path.join(app_root, key)
            os.makedirs(directory, exist_ok=True)
            with open(os.path.join(directory, "index.html"), "w") as f:
                f.write(page)
            pages += 1
    print(f"  /app -> {pages} share page(s) for {len(out)} apps")

    with open(index_path, "w") as f:
        json.dump(doc, f, indent=1)
    with gzip.open(index_path + ".gz", "wt") as f:
        json.dump(doc, f)

    with_icons = sum(1 for a in out if a.get("icon"))
    print(
        f"indexed {len(out)}/{len(apps)} apps, {with_icons} with an icon, "
        f"{len(warnings)} warning(s)"
    )
    for a in out:
        shots = len(a.get("screenshots", []))
        print(
            f"  {a['name']:24} v{a['latest']['version']:12} "
            f"{a['downloads']:>6} downloads  {shots} screenshot(s)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
