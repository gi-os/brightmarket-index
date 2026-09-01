# brightmarket-index

The database behind [BrightMarket](https://github.com/gi-os/BrightMarket). There is
no server: this repo *is* the backend.

- **`apps/`** — the curated list, one YAML file per app named after its
  applicationId. One file each rather than one list: every submission used to
  append to the end of a single `apps.yml`, so any two open at once modified the
  same line and whichever was merged second always conflicted. Separate files
  cannot collide, so merge order stops mattering.
- **`index-v1.json`** — rebuilt by CI from `apps/` + the GitHub Releases
  API, and served from GitHub Pages. This is the single file the app fetches.
- **`icons/`** — one 192px PNG per app, named after its applicationId, plus
  `sources.json` recording where each came from. Written by
  `scripts/extract_icons.py`, in priority order: an app's own `icon:` field in
  its YAML (a direct image URL or a repo path — see below), then a file in the
  app's own repo (`docs/icon.png` and the F-Droid fastlane path), then the
  APK's launcher icon resolved through its resource table. Committed rather
  than generated at build time, because the index workflow has `contents: read`
  and deploys instead of committing — an icon it wrote would live for exactly
  one run. Eighteen apps declare no icon anywhere, most of them SDK tools;
  those have no file here and no `icon` key in the index, and both clients draw
  the app's first letter instead.

  **The concrete, explainable way to get an icon in:** add an `icon:` line to
  the app's file in `apps/` — either a direct `https://` image URL or a path
  inside the repo like `docs/icon.png`. The next icon refresh fetches it, normalises
  it to the 192px PNG, and the app shows it. That is the whole answer to "why
  isn't my icon showing": if the mark isn't on the app's repo and the APK has
  no launcher icon, there is nothing to show, and the fix is one line in the
  listing. Any existing app file can be edited the same way — edit the YAML and
  open a PR (or use the portal's *Your apps* tab), and the change goes through
  the same refresh.
- **`submit.html`** — the submission portal (GitHub OAuth, read-only). Signs
  you in, lists only repos you own, and can submit, edit, and remove your own
  apps. Edit and remove file an issue with an explicit `**Action:**` field that
  `scripts/validate_submission.py` applies directly (no manual merge — ownership
  was already proved by OAuth).

## How an app gets in

Either open a [submission issue](../../issues/new?template=submit-app.yml), or use
the [portal](https://brightmarket.gzl.dev/submit.html), which signs
you in with GitHub and only lets you pick repos you actually own.

Either way `scripts/validate_submission.py` runs and checks the repo is public and
unarchived, has a published release with **exactly one** `.apk`, has a tag ending in
a number, and isn't already indexed under that applicationId. It reads the
applicationId out of the APK itself rather than trusting the submission. Pass opens
a PR; fail comments the reason and closes the issue so it can be reopened to retry.

Categories are `reading`, `utilities`, `games`, `media`, `productivity`, `hardware`,
`lifestyle`, `entertainment`, `communication`, `travel`, and `health`. Pick the
closest; the browse list groups by category, so it is worth matching how the app
actually gets used.

## Why the sorts need no analytics

`downloads` is summed from GitHub's own `download_count` across every release, so
"most downloaded" costs nothing and tracks nobody. `firstSeen` (New) and
`latest.published` (Updated) come from the index and the API. BrightMarket never
sees a user.

## Two things that must not be "tidied"

**`pkg` still reads `light*` for older apps.** That's the Android applicationId and
it is the app's permanent identity. Changing one isn't an update — it's a second app
installed alongside the first, and every user loses their data. `name` is what people
see and is free to differ.

**`firstSeen` and the signer pin are carried forward from the previous index,
never recomputed.** Recomputing would reset the New sort every hour, and would make
the signer pin meaningless — anyone able to push one release could clear it.

## An app is never dropped for an answer the API failed to give

Learned the hard way on 2026-08-17. The Releases API spent part of the afternoon returning
empty lists — HTTP 200, no error, just `[]` — and the builder read that as "this app has no
releases" and dropped it. An HTTP *error* had always kept the previous entry; a successful
empty response did not. So the published index fell 46 → 4 → 3 across three green runs, each
one carrying less history than the last, and because the index is deployed rather than
committed, the history went with it. Nothing failed, nothing was flagged, and a person had to
notice the store was nearly empty.

Two things hold that line now:

- **Carry forward, don't drop.** An app already in the index that suddenly reports no release
  keeps its previous entry, and so does one whose latest release is malformed. Stale is a far
  better failure than absent. The lookup is keyed on **repo, not pkg**, because before an APK
  has been downloaded the only pkg available is the catalogue's — the one this README already
  warns can be wrong.
- **Refuse to publish a collapse.** A build that indexes less than 75% of what the published
  index had exits non-zero and deploys nothing, so the site keeps serving the last good file
  and the run goes red. `ALLOW_SHRINK=1` for a deliberate mass removal.

Because the pins and dates live only in the published file, losing it loses them. The Pages
artifact of each run keeps for 24 hours, which is the window in which that is recoverable:
`recovery/` holds the last known-good index from that incident, and `build-index.yml` takes a
`seed_index` input that rebuilds history from such a file instead of from the live site. Both
exist for recovery and nothing else — a normal run must never set them.
