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
//   ALLOWED_ORIGIN         -- the exact GitHub Pages origin, e.g. https://gi-os.github.io
//   SUBMIT_REPO            -- "gi-os/brightmarket-index"

const SESSION_TTL_SECONDS = 10 * 60; // the signed session is only good for 10 minutes

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const cors = corsHeaders(env);

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: cors });
    }

    try {
      if (url.pathname === "/exchange" && request.method === "POST") {
        return await handleExchange(request, env, cors);
      }
      if (url.pathname === "/submit" && request.method === "POST") {
        return await handleSubmit(request, env, cors);
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
    "Access-Control-Allow-Methods": "POST, OPTIONS",
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

  // affiliation=owner is the whole point: this is what stops someone submitting
  // a repo that isn't theirs. type=public because BrightMarket only ever indexes
  // public releases -- there is no reason to ever touch a private repo here.
  const reposRes = await fetch(
    "https://api.github.com/user/repos?affiliation=owner&type=public&per_page=100&sort=updated",
    { headers: ghHeaders }
  );
  const reposData = await reposRes.json();
  const repos = Array.isArray(reposData)
    ? reposData.map((r) => ({ full_name: r.full_name, name: r.name, description: r.description }))
    : [];

  // The user token's job ends here. It is never written to a response, a log,
  // or a store of any kind -- it goes out of scope when this function returns.

  const session = await signSession(env, { login: me.login, repos: repos.map((r) => r.full_name) });
  return json({ login: me.login, repos, session }, 200, cors);
}

// ---------------------------------------------------------------------------
// Step 2: the actual submission. Trusts nothing the browser says about
// ownership -- re-checks the requested repo against the SIGNED list from
// step 1 (so a request forged in devtools can't submit someone else's repo),
// then files the issue with SUBMIT_PAT, a credential the browser never sees.
// ---------------------------------------------------------------------------
async function handleSubmit(request, env, cors) {
  const { session, repo, category } = await request.json();
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

  const body = [
    `**Repo:** https://github.com/${repo}`,
    `**Category:** ${category}`,
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
