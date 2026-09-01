#!/usr/bin/env python3
"""Fill icons/ with one square PNG per catalogued app.

BrightMarket showed a list of names. Every other store shows the thing you are
about to install, and on a phone this size a 2-unit mark is still the fastest
way to find the row you meant.

Where an icon comes from, in order:

0. The app's own `icon:` declaration in apps/<pkg>.yml -- either a direct
   https:// image URL or a path inside the repo (the same `docs/icon.png`
   convention). This is the concrete, explainable path: a developer commits
   the mark and it shows up on the next refresh, no guessing by filename.
1. A file in the app's own repo -- docs/icon.png is the convention across the
   Bright* set, and F-Droid's fastlane path is the convention outside it. Free
   to check, always current, and the developer controls it by committing.
2. The APK itself, resolved through the resource table. Needed for everything
   that isn't ours: resource *names* are minified to `res/o-.png`, so nothing
   can be found by guessing filenames -- the manifest icon attribute has to be
   followed through the .arsc, and an adaptive icon one level further into its
   foreground and background layers.
3. Nothing. Eighteen of the fifty-nine declare no icon at all -- most SDK tools
   do not, because LightOS never asked them for one -- and that is a real
   answer, not a failure. The client draws a lettered tile for those, so a
   missing icon is a deliberate look rather than a hole in the row.

Why the files are normalised and committed here rather than linked in place:

* One host, one size, one format. A 512px PNG from a repo and a 48px WebP out
  of an APK are the same 192px PNG by the time the phone sees them, which is
  the difference between a list that scrolls and one that does not.
* The index builder cannot commit anything (contents: read, deliberately), and
  the site is deployed rather than committed -- so an icon generated during a
  build would exist for exactly one run. Committed, it survives.

Run by refresh-icons.yml on a schedule, or by hand:

    pip install pyyaml androguard pillow
    python3 scripts/extract_icons.py            # only what is missing
    python3 scripts/extract_icons.py --force    # re-fetch everything

APK downloads are skipped for apps that already have an icon, because every
download is +1 on that release public counter and the Popular sort is built out
of those numbers. Repo files are re-checked every run -- one request, no
counter.
"""

import argparse
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile

import yaml

from PIL import Image

# androguard logs every AXML chunk it reads. Silence it before it is imported.
try:
    from loguru import logger

    logger.remove()
except Exception:
    pass

try:
    from androguard.core.apk import APK
    from androguard.core.axml import AXMLPrinter
except ImportError:
    APK = None
    AXMLPrinter = None

API = "https://api.github.com"
TOKEN = os.environ.get("GH_TOKEN", "")
HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "brightmarket-icons"}
if TOKEN:
    HEADERS["Authorization"] = f"Bearer {TOKEN}"

PUBLISHED_INDEX = "https://brightmarket.gzl.dev/index-v1.json"

# The rendered size. 192px is a little over what a 2-grid-unit mark needs on the
# LP3 panel, so the same file still looks right in the detail header at four.
SIZE = 192

# Checked in order, on the repo default branch. docs/icon.png first: it is what
# tools/icon_mark.py writes across the Bright* set.
REPO_ICONS = (
    "docs/icon.png",
    "docs/icon.webp",
    "icon.png",
    ".github/icon.png",
    "app/src/main/ic_launcher-playstore.png",
    "fastlane/metadata/android/en-US/images/icon.png",
    "metadata/en-US/images/icon.png",
)


def log(msg):
    print(msg, flush=True)


def fetch(url, timeout=120):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "brightmarket-icons"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def default_branch(repo):
    try:
        req = urllib.request.Request(API + "/repos/" + repo, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r).get("default_branch") or "main"
    except Exception:
        # Not fatal: main is right for all but a couple of repos, and a wrong
        # guess only means this app falls through to its APK.
        return "main"


def from_explicit(repo, icon):
    """The app's own `icon:` declaration, when it has one.

    Two shapes, both already validated on the way in:
    * `https://…`  -- a direct image URL, fetched as-is.
    * `docs/icon.png` -- a path inside the app's repo, fetched from the
      default branch. Cheaper and more current than a URL someone has to
      keep re-uploading, and it is the same convention REPO_ICONS checks.
    """
    if not icon:
        return None
    branch = default_branch(repo)
    url = icon if icon.startswith(("http://", "https://")) else (
        "https://raw.githubusercontent.com/" + repo + "/" + branch + "/" + icon
    )
    blob = fetch(url, 60)
    if blob and len(blob) > 256:
        try:
            Image.open(io.BytesIO(blob)).verify()
        except Exception:
            return None
        return blob, icon
    return None


def from_repo(repo):
    branch = default_branch(repo)
    for path in REPO_ICONS:
        blob = fetch("https://raw.githubusercontent.com/" + repo + "/" + branch + "/" + path, 60)
        # A 404 body and an HTML error page both decode as an image about as
        # well as each other, so the bytes are verified rather than trusted.
        if blob and len(blob) > 256:
            try:
                Image.open(io.BytesIO(blob)).verify()
            except Exception:
                continue
            return blob, path
    return None


def parse_colour(value):
    """'#ff1a1a1a' or '#1a1a1a' -> RGBA."""
    m = re.fullmatch("#([0-9a-fA-F]{6}|[0-9a-fA-F]{8})", value.strip())
    if not m:
        return None
    h = m.group(1)
    if len(h) == 8:
        a, r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4, 6))
    else:
        a = 255
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return r, g, b, a


def from_apk(path):
    """The launcher icon, followed through the resource table.

    Resource *names* are minified by AAPT2, so there is nothing to match on:
    `res/o-.png` is a real launcher icon and
    `res/color/mtrl_chip_close_icon_tint.xml` is not. The manifest icon
    attribute is the only reliable entry point.

    An adaptive icon points at an XML with a background and a foreground layer.
    Both are followed and composited, and the result is cropped to the 72/108
    safe zone -- otherwise the artwork arrives at the size the launcher zooms
    into, which reads as a tiny mark floating in a large empty square.
    """
    if APK is None:
        return None
    apk = APK(path)
    res = apk.get_android_resources()
    zf = zipfile.ZipFile(path)
    names = set(zf.namelist())

    def files_for(rid):
        out = []
        try:
            for _cfg, val in res.get_resolved_res_configs(rid):
                s = val if isinstance(val, str) else str(val)
                if s:
                    out.append(s)
        except Exception:
            pass
        return out

    def bitmap(rid, depth=0):
        if depth > 3:
            return None
        vals = files_for(rid)
        # Densities are separate entries for one id; the biggest file is the
        # highest density, which is the one worth downscaling from.
        bmps = [v for v in vals if v.lower().endswith((".png", ".webp", ".jpg")) and v in names]
        if bmps:
            best = max(bmps, key=lambda v: zf.getinfo(v).file_size)
            try:
                return Image.open(io.BytesIO(zf.read(best))).convert("RGBA")
            except Exception:
                return None
        for xml in [v for v in vals if v.lower().endswith(".xml") and v in names]:
            try:
                doc = AXMLPrinter(zf.read(xml)).get_xml().decode()
            except Exception:
                continue
            layers = {}
            for layer in ("background", "foreground"):
                m = re.search("<" + layer + '[^>]*android:drawable="([^"]+)"', doc)
                if not m:
                    m = re.search("<" + layer + '[^>]*android:color="([^"]+)"', doc)
                if m:
                    layers[layer] = m.group(1)
            if not layers:
                # A plain bitmap wrapper, or an adaptive-icon written with
                # attributes instead of child elements.
                for ref in re.findall('android:(?:src|drawable)="@([0-9A-Fa-f]{8})"', doc):
                    got = bitmap(int(ref, 16), depth + 1)
                    if got:
                        return got
                continue
            canvas = None
            for layer in ("background", "foreground"):
                ref = layers.get(layer)
                if not ref:
                    continue
                img = None
                if ref.startswith("#"):
                    rgba = parse_colour(ref)
                    if rgba:
                        img = Image.new("RGBA", (SIZE, SIZE), rgba)
                elif ref.startswith("@"):
                    resolved = files_for(int(ref[1:], 16))
                    hexes = [v for v in resolved if v.startswith("#")]
                    rgba = parse_colour(hexes[0]) if hexes else None
                    if rgba:
                        img = Image.new("RGBA", (SIZE, SIZE), rgba)
                    else:
                        img = bitmap(int(ref[1:], 16), depth + 1)
                if img is None:
                    continue
                if canvas is None:
                    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
                canvas.alpha_composite(img.resize((SIZE, SIZE), Image.LANCZOS))
            if canvas is not None:
                # 72/108 of the layer is what a launcher actually shows.
                inset = round(SIZE * (108 - 72) / 216)
                return canvas.crop((inset, inset, SIZE - inset, SIZE - inset))
        return None

    for attr in ("icon", "roundIcon"):
        val = apk.get_attribute_value("application", attr)
        if not val or not val.startswith("@"):
            continue
        try:
            img = bitmap(int(val[1:], 16))
        except Exception:
            img = None
        if img is not None:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue(), "apk:" + attr
    return None


def normalise(blob):
    """One square opaque greyscale 192px PNG, whatever came in.

    Opaque on purpose. BrightMarket draws on black, and a lot of these icons are
    light artwork on nothing at all -- an alpha channel would render them
    invisible against the very background they were cut out of. Which ground to
    flatten onto is decided by the artwork: light marks get black behind them,
    dark marks get white, so nothing can come out as a blank square.

    Greyscale on purpose too, and converted here rather than filtered by each
    client. This phone is a black-and-white phone; a colour icon is not a
    brighter version of a grey one, it is the wrong thing in the row. Three apps
    ship the stock green Android robot and one ships an orange bird, and on a
    panel LightOS keeps in greyscale anyway those would only ever have been
    colour on the web catalogue and on the handful of screens BrightControl
    grants colour to. Doing it at the source means there is one answer, both
    clients get it, and nothing has to remember to strip saturation.
    """
    img = Image.open(io.BytesIO(blob)).convert("RGBA")

    # Squared before scaling, so a 184x192 icon is not stretched.
    if img.width != img.height:
        side = max(img.size)
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        square.alpha_composite(img, ((side - img.width) // 2, (side - img.height) // 2))
        img = square
    img = img.resize((SIZE, SIZE), Image.LANCZOS)

    alpha = img.getchannel("A")
    if alpha.getextrema()[0] < 250:
        grey = img.convert("L").tobytes()
        opacity = alpha.tobytes()
        lit = [p for p, a in zip(grey, opacity) if a > 16]
        mean = sum(lit) / len(lit) if lit else 0
        ground = (0, 0, 0, 255) if mean >= 110 else (255, 255, 255, 255)
        flat = Image.new("RGBA", img.size, ground)
        flat.alpha_composite(img)
        img = flat

    buf = io.BytesIO()
    # ITU-R 601-2 luma, which is what PIL's L conversion is -- a green robot and
    # a red one stop being different pictures, which is the point.
    img.convert("L").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def catalogue(root):
    entries = []
    directory = os.path.join(root, "apps")
    for name in sorted(os.listdir(directory)):
        if not name.endswith((".yml", ".yaml")):
            continue
        with open(os.path.join(directory, name)) as f:
            entry = yaml.safe_load(f)
        if entry and entry.get("pkg") and entry.get("repo"):
            entries.append(entry)
    return entries


def apk_urls():
    """pkg -> newest APK, from the published index.

    Read from the index rather than the Releases API: the index has already
    picked the right asset, refused the releases carrying two APKs, and
    corrected the catalogue pkg against what the APK actually says.
    """
    blob = fetch(PUBLISHED_INDEX, 60)
    if not blob:
        log("!! could not read the published index; only repo icons will be found")
        return {}
    return {
        a["pkg"]: a["latest"]["apk"]
        for a in json.loads(blob)["apps"]
        if a.get("latest", {}).get("apk")
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-fetch icons that already exist")
    ap.add_argument("--only", default="", help="one pkg, for debugging")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(root, "icons")
    os.makedirs(out_dir, exist_ok=True)
    sources_path = os.path.join(out_dir, "sources.json")
    sources = {}
    if os.path.exists(sources_path):
        with open(sources_path) as f:
            sources = json.load(f)

    apps = catalogue(root)
    if args.only:
        apps = [a for a in apps if a["pkg"] == args.only]
    urls = apk_urls()

    written = kept = missing = 0
    for app in apps:
        pkg, repo = app["pkg"], app["repo"]
        path = os.path.join(out_dir, pkg + ".png")
        have = os.path.exists(path)

        where = None
        blob = None
        # The app's own `icon:` declaration wins; the repo/APK lookups below
        # are the fallback for the apps that never declared one.
        got = from_explicit(repo, app.get("icon"))
        if not got:
            got = from_repo(repo)
        if got:
            blob, where = got
        elif have and not args.force:
            # Everything left needs the APK, and that costs a public download
            # counter. Not spent to confirm bytes we already have.
            log("==  " + pkg.ljust(36) + " kept (apk not re-downloaded)")
            kept += 1
            continue
        elif pkg in urls:
            blob = fetch(urls[pkg], 600)
            if not blob:
                log("ERR " + pkg.ljust(36) + " could not download the APK")
                missing += 1
                continue
            tmp = os.path.join("/tmp", pkg + ".apk")
            with open(tmp, "wb") as f:
                f.write(blob)
            try:
                got = from_apk(tmp)
            except Exception as e:
                log("ERR " + pkg.ljust(36) + " " + type(e).__name__ + " reading the APK: " + str(e))
                got = None
            finally:
                os.unlink(tmp)
            if not got:
                log("--  " + pkg.ljust(36) + " declares no icon")
                missing += 1
                continue
            blob, where = got
        else:
            log("--  " + pkg.ljust(36) + " not in the index yet")
            missing += 1
            continue

        try:
            png = normalise(blob)
        except Exception as e:
            log("ERR " + pkg.ljust(36) + " could not render " + str(where) + ": " + str(e))
            missing += 1
            continue

        # Byte-identical output is not rewritten, so a scheduled run that finds
        # nothing new produces no commit.
        if have and open(path, "rb").read() == png:
            log("==  " + pkg.ljust(36) + " unchanged (" + where + ")")
            kept += 1
        else:
            with open(path, "wb") as f:
                f.write(png)
            log("OK  " + pkg.ljust(36) + " " + where + " -> " + str(len(png)) + "b")
            written += 1
        sources[pkg] = where

    with open(sources_path, "w") as f:
        json.dump(dict(sorted(sources.items())), f, indent=1)
        f.write("\n")

    log("")
    log(str(written) + " written, " + str(kept) + " unchanged, " + str(missing)
        + " without an icon, " + str(len(apps)) + " apps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
