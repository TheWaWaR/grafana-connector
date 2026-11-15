// 显示 Grafana 地址和同步状态；按钮：申请读取该域名 cookie 的权限（需要用户点击）并立即推送一次。
const SERVER = "http://127.0.0.1:7788";
const $ = (id) => document.getElementById(id);
let origin = null;

const HINT = {
  ok: ["ok", ""],
  auth: ["warn", "需要授权读取该域名的 cookie，点下面的按钮。"],
  "no-cookies": ["warn", "Chrome 里还没登录 Grafana，先打开上面的地址登录。"],
  off: ["err", "连不上本地服务端：在终端运行 loki_logs.py health 会自动拉起它。"],
  error: ["err", ""],
};

function show(res) {
  const [cls, hint] = HINT[res.status] || ["", ""];
  $("status").className = cls;
  $("status").textContent = [res.detail, hint].filter(Boolean).join("\n");
}

async function init() {
  try {
    const r = await fetch(`${SERVER}/config`, { signal: AbortSignal.timeout(3000) });
    const url = new URL((await r.json()).grafana_url);
    origin = `${url.origin}/*`;
    $("grafana").textContent = url.origin;
    $("push").disabled = false;
    const granted = await chrome.permissions.contains({ origins: [origin] });
    show(granted ? { status: "", detail: (await chrome.action.getTitle({})).split("\n").slice(1).join("\n") }
                 : { status: "auth", detail: "" });
  } catch (e) {
    $("grafana").textContent = "（未知）";
    show({ status: "off", detail: `${e.name}: ${e.message}` });
    $("push").disabled = false; // 允许点按钮重试
  }
}

$("push").addEventListener("click", async () => {
  // permissions.request 必须在用户点击的同步上下文里调用，所以 origin 要提前准备好
  if (origin) {
    const ok = await chrome.permissions.request({ origins: [origin] });
    if (!ok) return show({ status: "auth", detail: "没有授权，无法读取 cookie。" });
  } else {
    await init();
    if (!origin) return;
  }
  $("status").className = "";
  $("status").textContent = "推送中…";
  show(await chrome.runtime.sendMessage({ type: "push" }));
});

init();
