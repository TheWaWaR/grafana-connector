// 列出所有 Grafana 实例和同步状态；按钮：申请读取这些域名 cookie 的权限（需要用户点击）并立即推送一次。
const SERVER = "http://127.0.0.1:7788";
const $ = (id) => document.getElementById(id);
let origins = null;

const CLS = { ok: "ok", auth: "warn", "no-cookies": "warn", off: "err", error: "err" };
const HINT = {
  auth: "需要授权读取该域名的 cookie，点下面的按钮。",
  "no-cookies": "Chrome 里还没登录这个 Grafana，先打开它登录。",
  off: "连不上本地服务端：在终端运行 loki_logs.py health 会自动拉起它。",
};

function render(results) {
  const box = $("list");
  box.textContent = "";
  for (const r of results) {
    const row = document.createElement("div");
    row.className = "row";
    const name = document.createElement("b");
    name.textContent = r.name || "(default)";
    const url = document.createElement("span");
    url.className = "muted";
    url.textContent = ` ${r.url || ""}`;
    const st = document.createElement("div");
    st.className = CLS[r.status] || "";
    st.textContent = [r.detail, HINT[r.status]].filter(Boolean).join("\n");
    row.append(name, url, st);
    box.append(row);
  }
}

function showError(status, detail) {
  $("list").textContent = "";
  $("status").className = CLS[status] || "";
  $("status").textContent = [detail, HINT[status]].filter(Boolean).join("\n");
}

async function init() {
  try {
    const r = await fetch(`${SERVER}/config`, { signal: AbortSignal.timeout(3000) });
    const body = await r.json();
    const list = body.grafanas || [{ name: "", url: body.grafana_url }];
    origins = [...new Set(list.map((g) => `${new URL(g.url).origin}/*`))];
    $("push").disabled = false;
    const { last = {} } = await chrome.storage.local.get("last");
    const results = [];
    for (const g of list) {
      const granted = await chrome.permissions.contains({ origins: [`${new URL(g.url).origin}/*`] });
      const prev = last[g.name || `${g.url.replace(/\/+$/, "")}/`];
      results.push(granted ? { ...(prev || { status: "", detail: "not pushed yet" }), ...g }
                           : { ...g, status: "auth", detail: "" });
    }
    $("status").textContent = "";
    render(results);
  } catch (e) {
    showError("off", `${e.name}: ${e.message}`);
    $("push").disabled = false; // 允许点按钮重试
  }
}

$("push").addEventListener("click", async () => {
  // permissions.request 必须在用户点击的同步上下文里调用，所以 origins 要提前准备好
  if (origins) {
    const ok = await chrome.permissions.request({ origins });
    if (!ok) return showError("auth", "没有授权，无法读取 cookie。");
  } else {
    await init();
    if (!origins) return;
  }
  $("status").className = "";
  $("status").textContent = "推送中…";
  const res = await chrome.runtime.sendMessage({ type: "push" });
  if (res.results.length) {
    $("status").textContent = "";
    render(res.results);
  } else {
    showError(res.status, res.detail);
  }
});

init();
