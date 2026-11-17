// 把各个 Grafana 实例的登录 cookie（包括 HttpOnly 的 SSO 会话 cookie）推送给本地 grafana-connector 服务端。
// Grafana 实例不写死：从服务端的 GET /config 读取；读取这些域名 cookie 的权限在 popup 里由用户点按钮授予。
// 服务端没在跑时推送会失败，没关系：之后的定时推送 / 在 popup 里点"推送"会补上。
const SERVER = "http://127.0.0.1:7788";

let timer = null;

function toInstance({ name, url }) {
  const u = new URL(url);
  // getAll 用带路径的 url，子路径部署（https://host/arb-grafana）时 path=/arb-grafana 的 cookie 也能拿到
  return { name, url: `${url.replace(/\/+$/, "")}/`, host: u.hostname, origin: `${u.origin}/*` };
}

async function fetchConfig() {
  const r = await fetch(`${SERVER}/config`, { signal: AbortSignal.timeout(5000) });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const body = await r.json();
  const list = body.grafanas || [{ name: "", url: body.grafana_url }]; // 老版本服务端只有 grafana_url
  const instances = list.map(toInstance);
  await chrome.storage.local.set({ instances }); // 给 cookie 变化监听用
  return instances;
}

async function pushOne(inst, reason) {
  if (!(await chrome.permissions.contains({ origins: [inst.origin] }))) {
    return { ...inst, status: "auth", detail: `not allowed to read cookies of ${inst.host}` };
  }
  const cookies = await chrome.cookies.getAll({ url: inst.url });
  if (!cookies.length) {
    return { ...inst, status: "no-cookies", detail: `no cookies for ${inst.url}; open it and log in` };
  }
  let r;
  try {
    const q = new URLSearchParams({ reason });
    if (inst.name) q.set("grafana", inst.name);
    r = await fetch(`${SERVER}/cookies?${q}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cookies: cookies.map((c) => ({ name: c.name, value: c.value })) }),
      signal: AbortSignal.timeout(10000),
    });
  } catch (e) {
    return { ...inst, status: "off", detail: `server not reachable: ${e.name}: ${e.message}` };
  }
  const body = await r.json().catch(() => ({}));
  console.log("push", inst.name, reason, r.status, body);
  if (r.ok && body.ok !== false) {
    const who = body.user ? `logged in as ${body.user}` : "already up to date";
    return { ...inst, status: "ok", detail: `ok ${new Date().toLocaleTimeString()}: ${who}` };
  }
  return { ...inst, status: "error", detail: `HTTP ${r.status}: ${body.error || ""}` };
}

async function push(reason) {
  try {
    await setBadge("…", "#888", `pushing (${reason})`);
    let instances;
    try {
      instances = await fetchConfig();
    } catch (e) {
      const detail = `server not reachable: ${e.name}: ${e.message}`;
      await setBadge("off", "#888", detail);
      return { status: "off", detail, results: [] };
    }
    const results = [];
    for (const inst of instances) results.push(await pushOne(inst, reason));
    await chrome.storage.local.set({ last: Object.fromEntries(results.map((r) => [r.name || r.url, r])) });
    const w = worst(results);
    const [text, color] = BADGE[w] || ["", "#888"];
    await setBadge(text, color, results.map((r) => `${r.name || r.host}: ${r.detail}`).join("\n"));
    return { status: w, results };
  } catch (e) {
    await setBadge("err", "#d33", `${e.name}: ${e.message}`);
    return { status: "error", detail: `${e.name}: ${e.message}`, results: [] };
  }
}

const RANK = { error: 4, off: 3, auth: 2, "no-cookies": 1, ok: 0 };
const BADGE = { error: ["!", "#d33"], off: ["off", "#888"], auth: ["auth", "#d90"], "no-cookies": ["?", "#888"] };

function worst(results) {
  return results.reduce((w, r) => ((RANK[r.status] ?? 0) > (RANK[w] ?? 0) ? r.status : w), "ok");
}

async function setBadge(text, color, detail) {
  console.log("badge", text, detail);
  await chrome.action.setBadgeText({ text });
  if (text) await chrome.action.setBadgeBackgroundColor({ color });
  await chrome.action.setTitle({ title: `grafana-connector cookie sync\n${detail}` });
}

function hostMatch(instHost, cookieDomain) {
  const d = cookieDomain.replace(/^\./, "");
  return instHost === d || instHost.endsWith("." + d);
}

function schedule(reason) {
  clearTimeout(timer);
  timer = setTimeout(() => push(reason), 1000); // 登录时会连着改好几个 cookie，合并成一次推送
}

chrome.cookies.onChanged.addListener(async ({ cookie, removed }) => {
  if (removed) return;
  const { instances } = await chrome.storage.local.get("instances");
  // 推送所有实例：服务端会忽略 cookie 没变化的实例
  if (instances && instances.some((i) => hostMatch(i.host, cookie.domain))) schedule("changed");
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
