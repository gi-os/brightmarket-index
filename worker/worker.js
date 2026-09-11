// BrightMarket submission portal — Cloudflare Worker
//
// This is the ONLY server-side component in the whole marketplace. Everything else
// (the index, the client, the validator) is GitHub Pages + GitHub Actions. This Worker
// exists purely because GitHub's OAuth code->token exchange needs a client_secret held
// somewhere that isn't a browser -- that's it. It does not store anything, has no
// database, and holds no session state between requests.
//
// Two completely separate credentials are in play, and they must never merge:
//
//   1. The SUBMITTER's GitHub token (minted per-login, scope=read:user only).
//      Can read who they are and list THEIR OWN public repos. Cannot write
//      anything, anywhere, ever. Discarded the instant /exchange finishes --
//      never returned to the browser, never logged, never stored.
//
//   2. SUBMIT_PAT, a fine-grained PAT that belongs to gi-os, restricted to
//      exactly one repository (brightmarket-index) and exactly one permission
//      (Issues: write). This is the only credential capable of creating the
//      submission issue. The submitter's browser never sees it and never
//      needs it. If it ever leaked, the blast radius is "someone can open or
//      edit issues on brightmarket-index" -- nothing else in the account.
//
// Required Worker secrets (`wrangler secret put <name>`):
//   OAUTH_CLIENT_ID       -- from the GitHub OAuth App
//   OAUTH_CLIENT_SECRET   -- from the GitHub OAuth App (never leaves this Worker)
//   SESSION_SECRET        -- any random 32+ byte string, `openssl rand -hex 32`
//   SUBMIT_PAT            -- fine-grained PAT, brightmarket-index only, Issues:write only
//
// Required Worker vars (not secret, fine in wrangler.toml):
//   ALLOWED_ORIGIN         -- the exact origin the portal is served from, e.g. https://brightmarket.gzl.dev
//   SUBMIT_REPO            -- "gi-os/brightmarket-index"

import { DurableObject } from "cloudflare:workers";

const SESSION_TTL_SECONDS = 10 * 60; // the signed session is only good for 10 minutes

/**
 * Bumped whenever this file changes in a way that matters. /health reports it,
 * so whether a deploy actually landed is a question with an answer instead of
 * a guess -- which is what made the empty-repo-list bug take two rounds to
 * pin down.
 */
// Bumped on every change to this file. /health reports it, and the deploy workflow refuses to
// go green until the running worker answers with the string that is in the source -- which is
// the check that would have caught the ADB, Name and Summary fields shipping to a bundle that
// was never redeployed.
const VERSION = "7-pulse";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const cors = corsHeaders(env);

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: cors });
    }

    try {
      if (url.pathname === "/health") {
        return json({ ok: true, version: VERSION }, 200, cors);
      }
      if (url.pathname === "/exchange" && request.method === "POST") {
        return await handleExchange(request, env, cors);
      }
      if (url.pathname === "/submit" && request.method === "POST") {
        return await handleSubmit(request, env, cors);
      }
      if (url.pathname === "/manage" && request.method === "POST") {
        return await handleManage(request, env, cors);
      }
      if (url.pathname === "/edit" && request.method === "POST") {
        return await handleManageAction("edit", request, env, cors);
      }
      if (url.pathname === "/remove" && request.method === "POST") {
        return await handleManageAction("remove", request, env, cors);
      }
      // Anonymous install counting. See the Pulse section at the bottom of this
      // file: no identifier of any kind is sent, received or stored.
      if (url.pathname === "/pulse" && request.method === "POST") {
        return await handlePulse(request, env, cors);
      }
      if (url.pathname === "/pulse/summary.json" && request.method === "GET") {
        return await handlePulseSummary(request, env, cors);
      }
      return json({ error: "not found" }, 404, cors);
    } catch (err) {
      // Never leak internals (token fragments, stack traces) to the client.
      console.error(err);
      return json({ error: "internal error" }, 500, cors);
    }
  },
};

function corsHeaders(env) {
  return {
    "Access-Control-Allow-Origin": env.ALLOWED_ORIGIN, // exact origin, never "*"
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}

function json(body, status, cors) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...cors },
  });
}

// ---------------------------------------------------------------------------
// Step 1: exchange GitHub's one-time `code` for a user access token, look up
// who they are and which public repos they own, then hand the browser back a
// signed, stateless session -- never the token itself.
// ---------------------------------------------------------------------------
async function handleExchange(request, env, cors) {
  const { code, state } = await request.json();
  if (!code) return json({ error: "missing code" }, 400, cors);

  const tokenRes = await fetch("https://github.com/login/oauth/access_token", {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({
      client_id: env.OAUTH_CLIENT_ID,
      client_secret: env.OAUTH_CLIENT_SECRET, // the one place this secret is used
      code,
    }),
  });
  const tokenData = await tokenRes.json();
  const userToken = tokenData.access_token;
  if (!userToken) {
    return json({ error: "github rejected the code", detail: tokenData.error_description }, 400, cors);
  }

  const ghHeaders = {
    Authorization: `Bearer ${userToken}`,
    Accept: "application/vnd.github+json",
    "User-Agent": "brightmarket-portal",
  };

  const meRes = await fetch("https://api.github.com/user", { headers: ghHeaders });
  const me = await meRes.json();
  if (!me.login) return json({ error: "could not read github identity" }, 400, cors);

  // Deliberately the PUBLIC /users/{login}/repos endpoint, not /user/repos.
  // /user/repos requires the `repo` scope; with only `read:user` it returns an
  // empty list, which reads to the user as "you have no repos". This endpoint
  // needs no scope beyond identifying them, so the permission ask stays at
  // read:user -- and since it is keyed on THEIR login it can only ever return
  // repos they own, which is the ownership guarantee we actually want.
  //
  // type=owner excludes forks-of-others and org repos they merely collaborate
  // on. Paginated because 100 is a real ceiling for an active account.
  const repos = [];
  let reposError = null;
  for (let page = 1; page <= 5; page++) {
    const res = await fetch(
      `https://api.github.com/users/${encodeURIComponent(me.login)}/repos` +
        `?type=owner&per_page=100&sort=updated&page=${page}`,
      { headers: ghHeaders }
    );
    if (!res.ok) {
      // Surfaced to the page so a failure reads as a failure rather than as
      // "you have no repos", which is what it looked like before.
      reposError = `GitHub returned ${res.status} listing repositories`;
      break;
    }
    const batch = await res.json();
    if (!Array.isArray(batch) || batch.length === 0) break;
    for (const r of batch) {
      // Archived repos can't ship updates, so the validator would reject them
      // anyway -- filter here so they never appear as a choice.
      if (r.archived || r.private) continue;
      repos.push({
        full_name: r.full_name,
        name: r.name,
        description: r.description,
        updated_at: r.pushed_at || r.updated_at,
      });
    }
    if (batch.length < 100) break;
  }

  // The user token's job ends here. It is never written to a response, a log,
  // or a store of any kind -- it goes out of scope when this function returns.

  const session = await signSession(env, { login: me.login, repos: repos.map((r) => r.full_name) });
  return json(
    { login: me.login, repos, session, version: VERSION, reposError },
    200,
    cors
  );
}

// ---------------------------------------------------------------------------
// Step 2: the actual submission. Trusts nothing the browser says about
// ownership -- re-checks the requested repo against the SIGNED list from
// step 1 (so a request forged in devtools can't submit someone else's repo),
// then files the issue with SUBMIT_PAT, a credential the browser never sees.
// ---------------------------------------------------------------------------
async function handleSubmit(request, env, cors) {
  const { session, repo, category, name, summary, icon, adb } = await request.json();
  if (!session || !repo || !category) {
    return json({ error: "missing session, repo, or category" }, 400, cors);
  }

  let claims;
  try {
    claims = await verifySession(env, session);
  } catch {
    return json({ error: "session expired or invalid -- please sign in again" }, 401, cors);
  }

  if (!claims.repos.includes(repo)) {
    // Either a forged request, or the repo list is stale (they made it public
    // after logging in). Either way: no. They can just sign in again.
    return json({ error: "that repo isn't in your verified, owned repo list" }, 403, cors);
  }

  // One line each, newlines stripped. The validator reads these as `**Label:** value`
  // anchored to a line, so a value carrying its own newline could otherwise write a field
  // the submitter never chose -- a summary containing "**Category:** hardware", say.
  const oneLine = (v, max) => String(v ?? "").replace(/[\r\n]+/g, " ").trim().slice(0, max);

  const body = [
    `**Repo:** https://github.com/${repo}`,
    `**Category:** ${category}`,
    // Name and Summary were collected by the portal and then dropped here, so the
    // validator read two fields that were never written and every listing fell back to
    // the repo's own name and description.
    ...(oneLine(name, 40) ? [`**Name:** ${oneLine(name, 40)}`] : []),
    ...(oneLine(summary, 140) ? [`**Summary:** ${oneLine(summary, 140)}`] : []),
    // The concrete icon path: either a direct image URL or a path inside the
    // repo. Left blank the icon is looked up from the repo and the APK as
    // before, so this is strictly an upgrade for apps whose mark wouldn't
    // resolve on its own.
    ...(oneLine(icon, 300) ? [`**Icon:** ${oneLine(icon, 300)}`] : []),
    // Checked properly on the validator side, against the applicationId read out of the
    // APK. Carried verbatim here so the issue shows exactly what was asked for.
    ...(oneLine(adb, 600) ? [`**ADB:** ${oneLine(adb, 600)}`] : []),
    `**Submitted by:** @${claims.login} (verified via GitHub OAuth, ownership confirmed server-side)`,
  ].join("\n");

  const issueRes = await fetch(`https://api.github.com/repos/${env.SUBMIT_REPO}/issues`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.SUBMIT_PAT}`, // scoped ONLY to SUBMIT_REPO, Issues:write only
      Accept: "application/vnd.github+json",
      "User-Agent": "brightmarket-portal",
    },
    body: JSON.stringify({
      title: `Submit: ${repo}`,
      body,
      labels: ["submission"],
    }),
  });

  if (!issueRes.ok) {
    const detail = await issueRes.text();
    return json({ error: "failed to file the submission", detail }, 502, cors);
  }

  const issue = await issueRes.json();
  return json({ ok: true, issueUrl: issue.html_url }, 200, cors);
}

// ---------------------------------------------------------------------------
// Step 3: "Your apps". Returns the verified repo list carried in the session
// -- the same ownership-guaranteed list /submit trusts -- and lets the portal
// join it against the published catalogue client-side. No extra PAT scope is
// needed: this endpoint never reads the index repo, it only re-asserts what
// OAuth already proved.
// ---------------------------------------------------------------------------
async function handleManage(request, env, cors) {
  const { session } = await request.json();
  if (!session) return json({ error: "missing session" }, 400, cors);

  let claims;
  try {
    claims = await verifySession(env, session);
  } catch {
    return json({ error: "session expired or invalid -- please sign in again" }, 401, cors);
  }

  return json({ login: claims.login, repos: claims.repos }, 200, cors);
}

// ---------------------------------------------------------------------------
// Step 4: edit / remove. Same shape as /submit -- a verified session, then an
// issue filed with SUBMIT_PAT carrying an explicit `**Action:**` field so the
// validator routes it to its edit/remove branch instead of a fresh submission.
// The action field is read by parse_issue(); without it the validator would
// treat the request as a new listing for a repo that's already indexed and
// reject it.
// ---------------------------------------------------------------------------
async function handleManageAction(action, request, env, cors) {
  const { session, repo, name, summary, category } = await request.json();
  if (!session || !repo) {
    return json({ error: "missing session or repo" }, 400, cors);
  }

  let claims;
  try {
    claims = await verifySession(env, session);
  } catch {
    return json({ error: "session expired or invalid -- please sign in again" }, 401, cors);
  }

  if (!claims.repos.some((r) => r.toLowerCase() === repo.toLowerCase())) {
    return json({ error: "that repo isn't in your verified, owned repo list" }, 403, cors);
  }

  const oneLine = (v, max) => String(v ?? "").replace(/[\r\n]+/g, " ").trim().slice(0, max);

  const body = [
    `**Action:** ${action}`,
    `**Repo:** https://github.com/${repo}`,
    // Only the fields the submitter actually changed are sent, so the validator's
    // edit branch reports exactly what moved instead of "nothing differs".
    ...(oneLine(name, 40) ? [`**Name:** ${oneLine(name, 40)}`] : []),
    ...(oneLine(summary, 140) ? [`**Summary:** ${oneLine(summary, 140)}`] : []),
    // Category is already a validated pick from a bounded set, so truncating
    // it here could only corrupt it (e.g. "Hardware / integrations" cut to 20).
    ...(category ? [`**Category:** ${category.trim()}`] : []),
    `**Submitted by:** @${claims.login} (verified via GitHub OAuth, ownership confirmed server-side)`,
  ].join("\n");

  const issueRes = await fetch(`https://api.github.com/repos/${env.SUBMIT_REPO}/issues`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.SUBMIT_PAT}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "brightmarket-portal",
    },
    body: JSON.stringify({
      title: `${action === "remove" ? "Remove" : "Edit"}: ${repo}`,
      body,
      labels: ["submission"],
    }),
  });

  if (!issueRes.ok) {
    const detail = await issueRes.text();
    return json({ error: `failed to file the ${action} request`, detail }, 502, cors);
  }

  const issue = await issueRes.json();
  return json({ ok: true, issueUrl: issue.html_url }, 200, cors);
}

// ---------------------------------------------------------------------------
// Stateless session signing -- HMAC-SHA256 over a JSON payload, base64url'd.
// No KV, no database. Anyone can read the payload (it's not secret data --
// just a login name and a list of already-public repo names) but nobody
// without SESSION_SECRET can forge or extend one.
// ---------------------------------------------------------------------------
async function signSession(env, claims) {
  const payload = { ...claims, iat: Math.floor(Date.now() / 1000) };
  const payloadB64 = base64url(JSON.stringify(payload));
  const sig = await hmac(env.SESSION_SECRET, payloadB64);
  return `${payloadB64}.${sig}`;
}

async function verifySession(env, token) {
  const [payloadB64, sig] = token.split(".");
  if (!payloadB64 || !sig) throw new Error("malformed session");
  const expected = await hmac(env.SESSION_SECRET, payloadB64);
  if (!timingSafeEqual(sig, expected)) throw new Error("bad signature");
  const claims = JSON.parse(atob(payloadB64.replace(/-/g, "+").replace(/_/g, "/")));
  if (Date.now() / 1000 - claims.iat > SESSION_TTL_SECONDS) throw new Error("expired");
  return claims;
}

async function hmac(secret, message) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function base64url(str) {
  return btoa(str).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let result = 0;
  for (let i = 0; i < a.length; i++) result |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return result === 0;
}

// ---------------------------------------------------------------------------
// Pulse -- how many installs of each catalogue app exist, and on which version.
//
// GitHub's download counters cannot answer that. They count bytes leaving a
// release, so a mirror, a crawler or one person re-downloading all read the
// same as a person installing: Ritual drew 87 downloads a day for a month from
// something that was never a phone. This counts installs instead, and only ever
// installs made through BrightMarket -- an app sideloaded with adb or fetched by
// Obtainium is invisible here, so every number this produces is a floor.
//
// Nothing identifying is transmitted, because nothing identifying exists. The
// client sends TRANSITIONS, not states:
//
//   {"e": "<random per event>", "app": "com.x.y", "frm": "",     "dst": "1.2"}  installed
//   {"e": "...",                "app": "com.x.y", "frm": "?",    "dst": "1.2"}  already had it
//   {"e": "...",                "app": "com.x.y", "frm": "1.1",  "dst": "1.2"}  updated
//   {"e": "...",                "app": "com.x.y", "frm": "1.2",  "dst": ""   }  removed
//
// There is no install id, no device id, no secret, no hash of anything, and no
// IP is read anywhere in this file. Two events from the same phone have nothing
// in common, so no query here can group them -- not by accident and not on
// purpose. What is stored is a counter per (day, app, from, to) and nothing else.
//
// The live figure is then arithmetic, the same trick the download history uses:
// everyone who arrived at a version minus everyone who left it. Updates cancel
// (one arrival, one departure), so installs + already-had - removed is exactly
// the number of copies out there.
//
// `frm: "?"` is the one-time census. On first run the client cannot know whether
// it is looking at a fresh install or at someone who has had the app for months,
// so it asks the package manager -- firstInstallTime == lastUpdateTime means
// genuinely new -- and everyone else is counted once as pre-existing. Without
// that split the day this shipped would have looked like a thousand installs.
// ---------------------------------------------------------------------------

const PULSE_MAX_BYTES = 16 * 1024;
const PULSE_MAX_EVENTS = 300;
const PULSE_DEDUPE_DAYS = 7;
// Days kept at daily resolution. Older rows are FOLDED, never deleted -- see
// PulseStore.record().
const PULSE_DAILY_DAYS = 120;

const RE_EVENT_ID = /^[0-9a-f]{32}$/;
const RE_PKG = /^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$/;
const RE_VERSION = /^[A-Za-z0-9._+-]{1,24}$/;

const PULSE_INDEX = "https://brightmarket.gzl.dev/index-v1.json";

/**
 * The set of packages the catalogue actually lists.
 *
 * Anyone can POST here, so without this the table would accept any package name
 * at all and the numbers would be whatever the last person to find the endpoint
 * felt like. Cached at the edge for 15 minutes, which is also roughly how long a
 * newly listed app waits before its first event is accepted.
 *
 * A failed read returns null and the guard is skipped rather than rejecting
 * everything: losing a day of counting is worse than accepting a day of noise,
 * and the index being down is not the client's fault.
 */
async function pulseCatalogue() {
  try {
    const res = await fetch(PULSE_INDEX, { cf: { cacheTtl: 900, cacheEverything: true } });
    if (!res.ok) return null;
    const doc = await res.json();
    const apps = Array.isArray(doc) ? doc : doc.apps;
    if (!Array.isArray(apps)) return null;
    const set = new Set();
    for (const a of apps) if (a && typeof a.pkg === "string") set.add(a.pkg);
    return set.size ? set : null;
  } catch {
    return null;
  }
}

async function handlePulse(request, env, cors) {
  const raw = await request.text();
  if (raw.length > PULSE_MAX_BYTES) return json({ error: "too large" }, 413, cors);

  let doc;
  try {
    doc = JSON.parse(raw);
  } catch {
    return json({ error: "bad json" }, 400, cors);
  }
  const events = Array.isArray(doc && doc.events) ? doc.events : null;
  if (!events) return json({ error: "missing events" }, 400, cors);
  if (events.length > PULSE_MAX_EVENTS) return json({ error: "too many events" }, 413, cors);

  const known = await pulseCatalogue();

  // Anything malformed is dropped silently rather than failing the batch. A
  // client that cannot parse one of its own version strings should still be
  // able to report the other twenty apps on the phone.
  const clean = [];
  for (const ev of events) {
    if (!ev || typeof ev !== "object") continue;
    const e = String(ev.e == null ? "" : ev.e);
    const app = String(ev.app == null ? "" : ev.app);
    const frm = String(ev.frm == null ? "" : ev.frm);
    const dst = String(ev.dst == null ? "" : ev.dst);

    if (!RE_EVENT_ID.test(e)) continue;
    if (app.length > 128 || !RE_PKG.test(app)) continue;
    if (known && !known.has(app)) continue;
    if (frm !== "" && frm !== "?" && !RE_VERSION.test(frm)) continue;
    if (dst !== "" && !RE_VERSION.test(dst)) continue;
    // Neither a departure nor an arrival is not an event.
    if (frm === "" && dst === "") continue;
    clean.push({ e, app, frm, dst });
  }

  if (clean.length === 0) return new Response(null, { status: 204, headers: cors });

  const stub = env.PULSE.get(env.PULSE.idFromName("v1"));
  const kept = await stub.record(clean);
  // 204 either way: the client's job is done once the batch is accepted, and a
  // duplicate it retried after a dropped connection is a success, not an error.
  return new Response(null, { status: 204, headers: { ...cors, "X-Pulse-Kept": String(kept) } });
}

async function handlePulseSummary(request, env, cors) {
  const stub = env.PULSE.get(env.PULSE.idFromName("v1"));
  const body = await stub.summary();
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: {
      ...cors,
      "Content-Type": "application/json",
      // The index build reads this once a run; the page reads a copy baked into
      // the deployment. Five minutes is plenty and keeps a hot reload cheap.
      "Cache-Control": "public, max-age=300",
    },
  });
}

function pulseDay(ms) {
  return new Date(ms).toISOString().slice(0, 10);
}

/**
 * One Durable Object holds the whole table. Deliberately one: the traffic is a
 * few hundred rows a day, and a single object makes every write transactional
 * without a database to provision, an account id to look up, or a second
 * credential in CI.
 */
export class PulseStore extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec(
      `CREATE TABLE IF NOT EXISTS moves (
         day TEXT NOT NULL,
         app TEXT NOT NULL,
         frm TEXT NOT NULL,
         dst TEXT NOT NULL,
         n   INTEGER NOT NULL DEFAULT 0,
         PRIMARY KEY (day, app, frm, dst)
       )`
    );
    // Idempotency only. The client keeps an event in its outbox until this
    // accepts it, so a retry after a dropped connection carries the same id and
    // must not count twice. Swept weekly -- an id older than that cannot still
    // be in flight.
    this.sql.exec(`CREATE TABLE IF NOT EXISTS seen (e TEXT PRIMARY KEY, at INTEGER NOT NULL)`);
  }

  record(events) {
    const now = Date.now();
    const day = pulseDay(now);
    let kept = 0;

    for (const ev of events) {
      const dup = [...this.sql.exec("SELECT 1 FROM seen WHERE e = ?", ev.e)].length > 0;
      if (dup) continue;
      this.sql.exec("INSERT INTO seen (e, at) VALUES (?, ?)", ev.e, now);
      this.sql.exec(
        `INSERT INTO moves (day, app, frm, dst, n) VALUES (?, ?, ?, ?, 1)
         ON CONFLICT (day, app, frm, dst) DO UPDATE SET n = n + 1`,
        day,
        ev.app,
        ev.frm,
        ev.dst
      );
      kept++;
    }

    this.sql.exec("DELETE FROM seen WHERE at < ?", now - PULSE_DEDUPE_DAYS * 86400000);

    // Old days are folded into a single '*' bucket, never dropped. The install
    // count is arithmetic over every event ever recorded, so deleting a row
    // would silently subtract people who are still there -- the daily chart
    // loses its tail, the totals stay exact.
    const cut = pulseDay(now - PULSE_DAILY_DAYS * 86400000);
    const old = [
      ...this.sql.exec(
        `SELECT app, frm, dst, SUM(n) AS n FROM moves
         WHERE day < ? AND day != '*' GROUP BY app, frm, dst`,
        cut
      ),
    ];
    if (old.length) {
      for (const r of old) {
        this.sql.exec(
          `INSERT INTO moves (day, app, frm, dst, n) VALUES ('*', ?, ?, ?, ?)
           ON CONFLICT (day, app, frm, dst) DO UPDATE SET n = n + ?`,
          r.app,
          r.frm,
          r.dst,
          r.n,
          r.n
        );
      }
      this.sql.exec("DELETE FROM moves WHERE day < ? AND day != '*'", cut);
    }

    return kept;
  }

  summary() {
    const apps = {};
    const of = (name) =>
      (apps[name] ||= { installed: 0, installs: 0, existing: 0, removed: 0, versions: {}, daily: {} });

    for (const r of this.sql.exec("SELECT day, app, frm, dst, n FROM moves")) {
      const a = of(r.app);
      const n = Number(r.n) || 0;

      if (r.frm === "") a.installs += n;
      else if (r.frm === "?") a.existing += n;
      if (r.dst === "") a.removed += n;

      // Arrivals at a version, minus departures from it.
      if (r.dst !== "") a.versions[r.dst] = (a.versions[r.dst] || 0) + n;
      if (r.frm !== "" && r.frm !== "?") a.versions[r.frm] = (a.versions[r.frm] || 0) - n;

      if (r.day !== "*") {
        const d = (a.daily[r.day] ||= { installs: 0, removed: 0 });
        if (r.frm === "") d.installs += n;
        if (r.dst === "") d.removed += n;
      }
    }

    for (const a of Object.values(apps)) {
      // Updates contribute one arrival and one departure, so they cancel and
      // this is exactly the number of copies still out there.
      a.installed = a.installs + a.existing - a.removed;
      for (const [v, n] of Object.entries(a.versions)) if (n <= 0) delete a.versions[v];
    }

    return {
      format: 1,
      generated: new Date().toISOString(),
      // Said here so it travels with the data and not only on the page that
      // happens to draw it today.
      counts: "installs made through BrightMarket only; adb and Obtainium are invisible",
      apps,
    };
  }
}
