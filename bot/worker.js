// Telegram command bot for site-monitor: starts the GitHub Actions checks on request.
//
// Telegram → this Worker (webhook) → GitHub workflow_dispatch → report arrives in Telegram.
// Obeys one chat only, verifies Telegram's secret header, and can only start the read-only checks.
//
// Secrets (wrangler secret put): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WEBHOOK_SECRET, GITHUB_TOKEN
// KV binding: COOLDOWN

const REPO = "eddywong888/site-monitor";
const COMMANDS = {
  status: { workflow: "uptime-report.yml", label: "Uptime & response report", eta: "about 1 min", sites: false, cooldown: 120 },
  seo:    { workflow: "seo-speed.yml",     label: "SEO & speed check",        eta: "2–3 min per site", sites: true, cooldown: 1800 },
  links:  { workflow: "broken-links.yml",  label: "Broken-link crawl",        eta: "about 20 min per site", sites: true, cooldown: 3600 },
};
const HELP = `<b>Site monitor commands</b>
/status — uptime &amp; response times now
/seo 2 — SEO &amp; speed for site 2 (blank = all)
/links 3 — broken-link crawl for site 3 (blank = all)
/runs — last 5 checks and their result
/help — this list

Sites are picked by number: 1 2 3 4.`;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/setup" && request.method === "POST") return setup(request, env, url);
    if (url.pathname !== "/telegram" || request.method !== "POST") return new Response("Not found", { status: 404 });
    if (!safeEqual(request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "", env.WEBHOOK_SECRET)) {
      return new Response("Forbidden", { status: 403 });
    }
    const update = await request.json().catch(() => null);
    const msg = update?.message;
    // Anyone else who finds the bot gets silence, not an error that confirms it exists.
    if (!msg?.text || String(msg.chat?.id) !== String(env.TELEGRAM_CHAT_ID)) return new Response("ok");
    try {
      await reply(env, await handle(msg.text.trim(), env));
    } catch (e) {
      await reply(env, `⚠️ Bot error: ${escapeHtml(String(e.message || e)).slice(0, 300)}`);
    }
    return new Response("ok"); // always 200, so Telegram doesn't retry the same command
  },
};

async function handle(text, env) {
  const [rawCmd, ...args] = text.split(/\s+/);
  const cmd = rawCmd.toLowerCase().replace(/^\//, "").replace(/@.*$/, "");
  if (cmd === "help" || cmd === "start") return HELP;
  if (cmd === "runs") return recentRuns(env);
  const spec = COMMANDS[cmd];
  if (!spec) return `Unknown command. ${HELP}`;

  const sites = args.join(" ");
  if (!/^[1-9]( [1-9])*$|^$/.test(sites)) return "Pick sites by number only, e.g. <code>/seo 2</code> or <code>/links 1 3</code>.";
  if (sites && !spec.sites) return `/${cmd} always covers all sites — no number needed.`;

  // Cooldown per command + site list: crawls load the 1GB servers.
  const key = `${cmd}:${sites || "all"}`;
  const last = Number(await env.COOLDOWN.get(key)) || 0;
  const wait = last + spec.cooldown * 1000 - Date.now();
  if (wait > 0) return `⏳ ${spec.label} for ${sites ? "site " + sites : "all sites"} was started recently. Try again in ${Math.ceil(wait / 60000)} min.`;

  const res = await github(env, `/actions/workflows/${spec.workflow}/dispatches`, {
    method: "POST",
    body: JSON.stringify({ ref: "main", inputs: spec.sites && sites ? { sites } : {} }),
  });
  if (res.status === 401) return "⚠️ GitHub token rejected (expired or revoked). Make a new one and run <code>wrangler secret put GITHUB_TOKEN</code>.";
  if (res.status !== 204) return `⚠️ GitHub refused to start the check (HTTP ${res.status}).`;
  await env.COOLDOWN.put(key, String(Date.now()), { expirationTtl: Math.max(spec.cooldown, 60) });
  return `▶️ ${spec.label} started for ${sites ? "site " + sites : "all sites"}. Report here in ${spec.eta}.`;
}

async function recentRuns(env) {
  const res = await github(env, "/actions/runs?per_page=5&exclude_pull_requests=true");
  if (res.status === 401) return "⚠️ GitHub token rejected (expired or revoked).";
  if (!res.ok) return `⚠️ Couldn't read runs (HTTP ${res.status}).`;
  const { workflow_runs: runs = [] } = await res.json();
  const icon = (r) => r.status !== "completed" ? "⏳" : r.conclusion === "success" ? "✅" : "❌";
  const myt = (t) => new Date(Date.parse(t) + 8 * 3600e3).toISOString().slice(5, 16).replace("T", " ");
  return "<b>Last 5 checks</b>\n" + runs.map((r) =>
    `${icon(r)} ${escapeHtml(r.name)} · ${myt(r.created_at)} MYT · <a href="${r.html_url}">log</a>`).join("\n");
}

// One-off: points Telegram at this Worker and sets the command menu. Needs the webhook secret.
async function setup(request, env, url) {
  if (!safeEqual(request.headers.get("X-Setup-Key") || "", env.WEBHOOK_SECRET)) return new Response("Forbidden", { status: 403 });
  const hook = await telegram(env, "setWebhook", {
    url: `${url.origin}/telegram`, secret_token: env.WEBHOOK_SECRET,
    allowed_updates: ["message"], drop_pending_updates: true,
  });
  const menu = await telegram(env, "setMyCommands", { commands: [
    { command: "status", description: "Uptime & response times now" },
    { command: "seo", description: "SEO & speed (add a site number)" },
    { command: "links", description: "Broken-link crawl (add a site number)" },
    { command: "runs", description: "Last 5 checks" },
    { command: "help", description: "Command list" },
  ] });
  return Response.json({ webhook: hook.description || hook.ok, menu: menu.ok });
}

function github(env, path, init = {}) {
  return fetch(`https://api.github.com/repos/${REPO}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`, Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "site-monitor-bot",
    },
  });
}

async function telegram(env, method, body) {
  const res = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  return res.json();
}

function reply(env, text) {
  return telegram(env, "sendMessage", {
    chat_id: env.TELEGRAM_CHAT_ID, text, parse_mode: "HTML", disable_web_page_preview: true,
  });
}

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Constant-time compare, so the secret can't be guessed from response timing.
function safeEqual(a, b) {
  if (!b || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
