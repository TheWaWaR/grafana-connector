// 把 Grafana 的登录 cookie（包括 HttpOnly 的 SSO 会话 cookie）推送给本地 loki_logs 服务端。
// Grafana 地址不写死：从服务端的 GET /config 读取；读取该域名 cookie 的权限在 popup 里由用户点按钮授予。
// 服务端没在跑时推送会失败，没关系：之后的定时推送 / 在 popup 里点"推送"会补上。
const SERVER = "http://127.0.0.1:7788";

let timer = null;

async function fetchConfig() {
  const r = await fetch(`${SERVER}/config`, { signal: AbortSignal.timeout(5000) });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const u = new URL((await r.json()).grafana_url);
  const cfg = { url: `${u.origin}/`, host: u.hostname, origin: `${u.origin}/*` };
  await chrome.storage.local.set({ cfg }); // 给 cookie 变化监听用
  return cfg;
}

async function push(reason) {
  try {
    await badge("…", "#888", `pushing (${reason})`);
    let cfg;
    try {
      cfg = await fetchConfig();
    } catch (e) {
      return await badge("off", "#888", `server not reachable: ${e.name}: ${e.message}`, "off");
    }
    if (!(await chrome.permissions.contains({ origins: [cfg.origin] }))) {
      return await badge("auth", "#d90", `click the extension icon and allow access to ${cfg.host}`, "auth", cfg);
    }
    const cookies = await chrome.cookies.getAll({ url: cfg.url });
    if (!cookies.length) {
      return await badge("?", "#888", `no cookies for ${cfg.url}; open it and log in`, "no-cookies", cfg);
    }
    let r;
    try {
      r = await fetch(`${SERVER}/cookies?reason=${reason}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cookies: cookies.map((c) => ({ name: c.name, value: c.value })) }),
        signal: AbortSignal.timeout(5000),
      });
    } catch (e) {
      return await badge("off", "#888", `server not reachable: ${e.name}: ${e.message}`, "off", cfg);
    }
    const body = await r.json().catch(() => ({}));
    console.log("push", reason, r.status, body);
    if (r.ok && body.ok !== false) {
      const who = body.user ? `logged in as ${body.user}` : "already up to date";
      return await badge("", "#888", `ok ${new Date().toLocaleTimeString()}: ${who}`, "ok", cfg);
    }
    return await badge("!", "#d33", `HTTP ${r.status}: ${body.error || ""}`, "error", cfg);
  } catch (e) {
    return await badge("err", "#d33", `${e.name}: ${e.message}`, "error");
  }
}

async function badge(text, color, detail, status = "", cfg = null) {
  console.log("badge", text, detail);
  await chrome.action.setBadgeText({ text });
  if (text) await chrome.action.setBadgeBackgroundColor({ color });
  await chrome.action.setTitle({ title: `loki_logs cookie sync\n${detail}` });
  return { status, detail, grafana: cfg && cfg.url };
}

function schedule(reason) {
  clearTimeout(timer);
  timer = setTimeout(() => push(reason), 1000); // 登录时会连着改好几个 cookie，合并成一次推送
}

chrome.cookies.onChanged.addListener(async ({ cookie, removed }) => {
  if (removed) return;
  const { cfg } = await chrome.storage.local.get("cfg");
  if (!cfg) return;
  const d = cookie.domain.replace(/^\./, "");
  if (cfg.host === d || cfg.host.endsWith("." + d)) schedule("changed");
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "push") {
    push("manual").then(sendResponse);
    return true; // 异步 sendResponse
  }
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("sync", { periodInMinutes: 10 });
  push("installed");
});
chrome.runtime.onStartup.addListener(() => push("startup"));
chrome.alarms.onAlarm.addListener(() => push("alarm"));
