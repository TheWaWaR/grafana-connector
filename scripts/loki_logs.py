#!/usr/bin/env python3
"""
通过 Grafana 查询 Loki 日志（以及 Prometheus/PromQL 指标）的 C/S 小工具，方便 AI（以及人）排查问题。
支持多个 Grafana 实例，每个实例有自己的名字、地址和登录 cookie。

Grafana 前面通常有 SSO / AWS Verified Access 之类的认证，脚本没法自己完成登录，
服务端复用 Chrome 里已登录的 cookie（如 AWSVAAuthSessionCookie / grafana_session，HttpOnly），
每个实例的 cookie 存在 ~/.config/loki_logs/cookies/<name>.txt（0600；老的 default 实例用 cookie.txt）。

Grafana 实例（必填）写在 ~/.config/loki_logs/config.toml（路径可用 LOKI_LOGS_CONFIG 覆盖）:
    default = "sptt"                        # 不指定 --grafana 时用哪个；只有一个实例时可省略
    [grafana.sptt]
    url = "https://grafana.sptt.dev"
    [grafana.arb]
    url = "https://admin.example.com/arb-grafana"   # 支持带子路径的 Grafana
  用 `loki_logs.py config add <name> <url> [--default]` 写入。老配置里的顶层 grafana_url 视为实例 "default"；
  环境变量 LOKI_LOGS_GRAFANA_URL / serve --grafana-url 会覆盖实例 "default" 的地址。

cookie 的来源:
  1. Chrome 扩展（推荐）: 仓库里的 chrome_ext/loki_logs_cookie_sync/，在 chrome://extensions 打开开发者模式后
     "加载已解压的扩展程序"。它从本地服务端读取所有 Grafana 实例（点扩展图标授权读取这些域名的 cookie），
     监听 cookie 变化并 POST 给本地服务端；在 Chrome 里登录/刷新 Grafana 后自动同步。
  2. 手动: Chrome DevTools → Network → 任一 grafana 请求 → Request Headers → 复制 Cookie 的值，
     然后 `pbpaste | python3 loki_logs.py set-cookie --grafana <name>`。
  3. `serve --source chrome` 直接读本机 Chrome 的 cookie 库（会弹钥匙串授权，需要 cryptography）。

结构:
  serve        常驻在本机 127.0.0.1 的服务端，持有登录态，代理所有 Loki 查询（依赖 requests）。
               Grafana token 需要轮换时自动轮换并写回 cookie 文件；登录态彻底失效时返回错误，
               提示在 Chrome 里重新打开 Grafana 登录。
  mcp          stdio MCP server，给 Claude Code 等 AI 用（内部调用本地服务端）。
  其它子命令    CLI 客户端（只依赖标准库），通过 HTTP 调用本地服务端。

用法:
    python3 loki_logs.py config add sptt https://grafana.sptt.dev --default  # 每个 Grafana 一次
    python3 loki_logs.py config add arb https://admin.example.com/arb-grafana
    python3 loki_logs.py config                                              # 查看当前配置
    python3 loki_logs.py config default arb / config remove arb
    python3 loki_logs.py serve                       # 一般不用手动跑，客户端 / MCP 会自动拉起
    pbpaste | python3 loki_logs.py set-cookie --grafana arb
    python3 loki_logs.py serve --source chrome --profile "Profile 1"

    python3 loki_logs.py health                              # 所有实例的登录状态
    python3 loki_logs.py --grafana arb ds --type all         # 下面的命令都可以用 --grafana 选实例（默认 default）
    python3 loki_logs.py ds                                  # 列出 Loki 数据源；其它命令都必须用 --ds 指定
    python3 loki_logs.py labels --ds test --since 6h
    python3 loki_logs.py values app --ds test
    python3 loki_logs.py values pod --ds prod --query '{app="my-service"}'
    python3 loki_logs.py series '{app="my-service"}' --ds prod
    python3 loki_logs.py query '{app="my-service"} |= "error"' --ds test --since 30m --limit 50
    python3 loki_logs.py query '{app="my-service"} | logfmt | level="ERROR"' --ds "PROD Logs" \\
        --start 2026-01-02T15:00 --end 2026-01-02T15:30
    python3 loki_logs.py query 'sum by (detected_level) (count_over_time({app="my-service"} [5m]))' --ds test --since 6h
    python3 loki_logs.py query '{app="my-service"}' --ds prod --since 15m --asc --json

    # PromQL（Prometheus 类型的数据源，经 Grafana /api/ds/query，数据源上配的 custom query parameters 也会带上）
    python3 loki_logs.py --grafana arb prom 'up' --ds greptimedb-promql --instant
    python3 loki_logs.py --grafana arb prom 'sum by(job)(rate(x_total[5m]))' --ds greptimedb --since 2h --step 5m

    # 可选：注册为 Claude Code 的 MCP server（先把 serve 跑起来）
    claude mcp add -s user loki -- python3 ~/.claude/skills/loki-logs/scripts/loki_logs.py mcp

--ds 没有默认值（避免查错环境），可以写完整名字、uid 或唯一前缀（prod / test）。

环境变量:
    LOKI_LOGS_SERVER   客户端/MCP 连接的服务端地址，默认 http://127.0.0.1:7788
    LOKI_LOGS_GRAFANA_URL  实例 "default" 的 Grafana 地址（覆盖配置文件）
    LOKI_LOGS_CONFIG       配置文件路径，默认 ~/.config/loki_logs/config.toml
    LOKI_LOGS_NO_AUTOSTART 设了就不自动拉起服务端

时间参数 --since / --start / --end:
    30m / 2h / 7d 这类相对时间（--start/--end 表示 now - x，--since 表示 end 往前的时长），
    now，Unix 时间戳（秒/毫秒/纳秒），ISO 时间 2026-11-13T22:00[:00][+08:00]（不带时区按本地时间）。
    不给 --start 时，start = end - since（since 默认 1h）。
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_SERVER = os.environ.get("LOKI_LOGS_SERVER", "http://127.0.0.1:7788")
CONFIG_DIR = os.path.expanduser("~/.config/loki_logs")
CONFIG_FILE = os.environ.get("LOKI_LOGS_CONFIG") or os.path.join(CONFIG_DIR, "config.toml")
_CFG_DIR = os.path.dirname(os.path.abspath(CONFIG_FILE))  # cookie 跟配置文件放在一起
LEGACY_COOKIE_FILE = os.path.join(_CFG_DIR, "cookie.txt")  # 实例 "default" 的 cookie（兼容老版本）
COOKIE_DIR = os.path.join(_CFG_DIR, "cookies")
LEGACY_NAME = "default"
PROM_TYPES = ("prometheus",)
DEFAULT_LIMIT = 100
MAX_LIMIT = 5000
SCRIPT = os.path.abspath(__file__)


class BadRequest(Exception):
    pass


class AuthError(Exception):
    pass


class UpstreamError(Exception):
    def __init__(self, status, msg):
        super().__init__(f"upstream HTTP {status}: {msg}")
        self.status = status


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- config

def load_config():
    try:
        with open(CONFIG_FILE, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"error: bad config file {CONFIG_FILE}: {e}")


_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def try_normalize_url(url):
    u = urllib.parse.urlparse((url or "").strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    return f"{u.scheme}://{u.netloc}{u.path.rstrip('/')}"


def normalize_grafana_url(url):
    n = try_normalize_url(url)
    if not n:
        sys.exit(f"error: bad Grafana URL {url!r}, expected e.g. https://grafana.example.com")
    return n


def config_instances(cfg):
    """配置文件里的实例 {name: url}（老的顶层 grafana_url 视为实例 "default"）。"""
    inst = {}
    for name, v in (cfg.get("grafana") or {}).items():
        url = v.get("url") if isinstance(v, dict) else v
        if url:
            inst[name] = normalize_grafana_url(url)
    if cfg.get("grafana_url") and LEGACY_NAME not in inst:
        inst[LEGACY_NAME] = normalize_grafana_url(cfg["grafana_url"])
    return inst


def resolve_instances(cli_url=None):
    """返回 ({name: url}, default_name)。--grafana-url / LOKI_LOGS_GRAFANA_URL 覆盖实例 "default"。都没有就报错退出。"""
    cfg = load_config()
    inst = config_instances(cfg)
    override = cli_url or os.environ.get("LOKI_LOGS_GRAFANA_URL")
    if override:
        inst[LEGACY_NAME] = normalize_grafana_url(override)
    if not inst:
        sys.exit("error: Grafana URL is not configured. Add one with:\n"
                 f"  python3 {SCRIPT} config add <name> https://grafana.example.com --default   (writes {CONFIG_FILE})\n"
                 "  or: export LOKI_LOGS_GRAFANA_URL=https://grafana.example.com")
    default = cfg.get("default")
    if default not in inst:
        default = LEGACY_NAME if LEGACY_NAME in inst else (next(iter(inst)) if len(inst) == 1 else None)
    return inst, default


def cookie_file_for(name):
    return LEGACY_COOKIE_FILE if name == LEGACY_NAME else os.path.join(COOKIE_DIR, f"{name}.txt")


def write_config(cfg):
    """写回配置：顶层字符串键 + [grafana.<name>] 表。老的顶层 grafana_url 迁移成实例 "default"。"""
    cfg = dict(cfg)
    inst = config_instances(cfg)
    cfg.pop("grafana_url", None)
    os.makedirs(os.path.dirname(CONFIG_FILE), mode=0o700, exist_ok=True)
    lines = [f"{k} = {json.dumps(str(v))}" for k, v in cfg.items() if k != "grafana" and isinstance(v, str)]
    for name, url in inst.items():
        lines += ["", f"[grafana.{json.dumps(name)}]", f"url = {json.dumps(url)}"]
    with open(CONFIG_FILE, "w") as f:
        f.write("\n".join(lines).lstrip("\n") + "\n")


# ---------------------------------------------------------------- time

_REL = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h|d|w)$")
_UNIT = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration_ns(s):
    m = _REL.match(s.strip())
    if not m:
        raise BadRequest(f"bad duration {s!r}, expected e.g. 30m / 2h / 7d")
    return int(float(m.group(1)) * _UNIT[m.group(2)] * 1e9)


def parse_time_ns(s, now_ns):
    s = (s or "").strip()
    if not s or s == "now":
        return now_ns
    if _REL.match(s):
        return now_ns - parse_duration_ns(s)
    if re.fullmatch(r"\d+(\.\d+)?", s):
        digits = len(s.split(".")[0])
        v = float(s)
        if digits >= 18:
            return int(s.split(".")[0])
        if digits >= 15:
            return int(v * 1e3)
        if digits >= 12:
            return int(v * 1e6)
        return int(v * 1e9)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise BadRequest(f"bad time {s!r}")
    if dt.tzinfo is None:
        dt = dt.astimezone()  # 按本地时区
    return int(dt.timestamp() * 1e9)


def resolve_range(p):
    now = time.time_ns()
    end = parse_time_ns(p.get("end"), now)
    if p.get("start"):
        start = parse_time_ns(p["start"], now)
    else:
        start = end - parse_duration_ns(p.get("since") or "1h")
    if start >= end:
        raise BadRequest("start must be before end")
    return start, end


def fmt_ts(ns):
    return datetime.fromtimestamp(ns / 1e9).astimezone().isoformat(timespec="milliseconds")


# ---------------------------------------------------------------- Chrome cookies (macOS)

BROWSERS = {
    "chrome": ("Google/Chrome", "Chrome Safe Storage"),
    "brave": ("BraveSoftware/Brave-Browser", "Brave Safe Storage"),
    "edge": ("Microsoft Edge", "Microsoft Edge Safe Storage"),
    "arc": ("Arc/User Data", "Arc Safe Storage"),
    "chromium": ("Chromium", "Chromium Safe Storage"),
}


class ChromeCookies:
    """读取 macOS 上 Chromium 系浏览器的 cookie（v10 AES-128-CBC，key 存在钥匙串里）。"""

    def __init__(self, browser="chrome", profile=None):
        sub, self.keychain_service = BROWSERS[browser]
        self.browser = browser
        self.base = os.path.expanduser(f"~/Library/Application Support/{sub}")
        self.profile = profile
        self._key = None

    def _aes_key(self):
        if self._key is None:
            out = subprocess.run(
                ["security", "find-generic-password", "-w", "-s", self.keychain_service],
                capture_output=True, text=True,
            )
            if out.returncode != 0:
                raise AuthError(f"cannot read '{self.keychain_service}' from keychain "
                               f"(exit {out.returncode}, 128 = denied/cancelled): {out.stderr.strip()}")
            self._key = hashlib.pbkdf2_hmac("sha1", out.stdout.strip().encode(), b"saltysalt", 1003, 16)
        return self._key

    def _decrypt(self, enc, db_version):
        if enc[:3] not in (b"v10", b"v11"):
            return enc.decode("utf-8")
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        d = Cipher(algorithms.AES(self._aes_key()), modes.CBC(b" " * 16)).decryptor()
        pt = d.update(enc[3:]) + d.finalize()
        pt = pt[: -pt[-1]]
        if db_version >= 24:  # 新版 Chrome 在明文前加了 sha256(host_key)
            pt = pt[32:]
        return pt.decode("utf-8")

    def _profiles(self):
        if self.profile:
            return [self.profile]
        if not os.path.isdir(self.base):
            raise AuthError(f"{self.browser} data dir not found: {self.base}")
        return sorted(d for d in os.listdir(self.base) if d == "Default" or d.startswith("Profile "))

    @staticmethod
    def _host_match(host_key, host):
        if host_key.startswith("."):
            return host == host_key[1:] or host.endswith(host_key)
        return host == host_key

    def _rows(self, profile, host):
        path = os.path.join(self.base, profile, "Cookies")
        if not os.path.exists(path):
            return 0, []
        tmp = tempfile.mkdtemp(prefix="loki_logs_")
        try:
            dst = os.path.join(tmp, "Cookies")
            shutil.copy2(path, dst)  # Chrome 运行时会锁库，拷一份再读
            for ext in ("-wal", "-journal"):
                if os.path.exists(path + ext):
                    shutil.copy2(path + ext, dst + ext)
            con = sqlite3.connect(dst)
            try:
                row = con.execute("SELECT value FROM meta WHERE key='version'").fetchone()
                rows = con.execute(
                    "SELECT host_key, name, value, CAST(encrypted_value AS BLOB), path, expires_utc, is_secure, last_access_utc "
                    "FROM cookies"
                ).fetchall()
            finally:
                con.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        now_chrome = int((time.time() + 11644473600) * 1e6)  # Chrome 时间从 1601-01-01 起算，单位微秒
        rows = [r for r in rows if self._host_match(r[0], host) and (r[5] == 0 or r[5] > now_chrome)]
        return int(row[0]) if row else 0, rows

    def load(self, host):
        """返回 (profile, [(name, value, domain, path, secure)])，多个 profile 时选最近用过的。"""
        best = None
        for p in self._profiles():
            ver, rows = self._rows(p, host)
            if not rows:
                continue
            score = (any(r[1].startswith(("AWSVAAuthSessionCookie", "AWSELBAuthSessionCookie")) for r in rows), max(r[7] for r in rows))
            if best is None or score > best[0]:
                best = (score, p, ver, rows)
        if best is None:
            raise AuthError(f"no cookies for {host} in {self.browser} ({self.base}); open https://{host} there and log in")
        _, profile, ver, rows = best
        cookies = [(r[1], r[2] or self._decrypt(r[3], ver), r[0], r[4], bool(r[6])) for r in rows]
        return profile, cookies

    def hint(self, url):
        return f"Ask the user to open {url} in {self.browser} and log in again, then retry."


def parse_cookie_header(text):
    text = text.strip()
    if text.lower().startswith("cookie:"):
        text = text[7:]
    out = {}
    for part in re.split(r"[;\n]", text):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def write_cookie_file(path, cookies):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("; ".join(f"{k}={v}" for k, v in cookies.items()) + "\n")
    os.replace(tmp, path)


class FileCookies:
    """cookie 存在一个文件里（Cookie 请求头格式），由 Chrome 扩展推送 / set-cookie 命令写入。"""

    browser = "cookie file"

    def __init__(self, path):
        self.path = path

    def load(self, host):
        try:
            with open(self.path) as f:
                cookies = parse_cookie_header(f.read())
        except FileNotFoundError:
            cookies = {}
        if not cookies:
            raise AuthError(f"no cookies yet in {self.path}. {self.hint(f'https://{host}')}")
        return self.path, [(k, v, host, "/", True) for k, v in cookies.items()]

    def save(self, cookies):
        write_cookie_file(self.path, cookies)

    def hint(self, url):
        return (f"Ask the user to open {url} in Chrome (with the loki_logs cookie-sync extension installed) "
                f"and log in again, or paste the Cookie header via `loki_logs.py set-cookie`; then retry.")


# ---------------------------------------------------------------- Grafana / Loki

class Grafana:
    def __init__(self, name, url, cookie_src):
        import requests

        self.name = name
        self.url = url.rstrip("/")
        self.host = urllib.parse.urlparse(self.url).hostname
        self.src = cookie_src
        self.s = requests.Session()
        self.s.trust_env = False
        self.s.headers["User-Agent"] = "loki-logs/1.0"
        self.lock = threading.Lock()
        self.profile = None
        self.loaded_at = 0
        self.last_pushed = None
        self._ds_cache = (0.0, None)

    def reload_cookies(self):
        with self.lock:
            profile, cookies = self.src.load(self.host)
            self.s.cookies.clear()
            https = self.url.startswith("https://")
            for name, value, domain, path, secure in cookies:  # http 的 Grafana 不能带 secure，否则不会发送
                self.s.cookies.set(name, value, domain=domain, path=path, secure=secure and https)
            self.profile, self.loaded_at = profile, time.time()
            log(f"[{self.name}] loaded {len(cookies)} cookies for {self.host} from {self.src.browser} {profile!r}: "
                f"{', '.join(sorted({c[0] for c in cookies}))}")

    def _persist(self):
        if hasattr(self.src, "save"):
            self.src.save({c.name: c.value for c in self.s.cookies})

    def accept_pushed(self, cookies):
        """Chrome 扩展推来的 cookie。浏览器侧没变化（定时推送）时忽略，免得覆盖服务端自己轮换过的 token。"""
        with self.lock:
            if cookies == self.last_pushed:
                return False
            self.last_pushed = cookies
        self.src.save(cookies)
        self.reload_cookies()
        self._ds_cache = (0.0, None)
        return True

    def _send(self, method, path, **kw):
        return self.s.request(method, self.url + path, allow_redirects=False, timeout=120, **kw)

    @staticmethod
    def _needs_rotate(r):
        return r.status_code == 401 and "session.token.rotate" in r.text

    @staticmethod
    def _auth_failed(r):
        if r.status_code == 401:
            return True
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location", "")
            return any(k in loc for k in ("accounts.google.com", "/oauth2/", "/login"))
        return False

    def _send_with_rotate(self, method, path, **kw):
        r = self._send(method, path, **kw)
        if self._needs_rotate(r):
            rr = self._send("POST", "/api/user/auth-tokens/rotate")
            log(f"[{self.name}] grafana session token rotated: HTTP {rr.status_code}")
            if rr.status_code < 300:
                self._persist()
            r = self._send(method, path, **kw)
        return r

    def request(self, method, path, **kw):
        if not self.loaded_at:
            self.reload_cookies()
        r = self._send_with_rotate(method, path, **kw)
        if self._auth_failed(r):
            log(f"[{self.name}] auth failed (HTTP {r.status_code}), reloading cookies from {self.src.browser}")
            self.reload_cookies()
            r = self._send_with_rotate(method, path, **kw)
        if self._auth_failed(r):
            raise AuthError(f"Grafana {self.name!r} login expired (HTTP {r.status_code}). {self.src.hint(self.url)}")
        return r

    def get_json(self, path, params=None):
        r = self.request("GET", path, params=params)
        if r.status_code >= 300:
            raise UpstreamError(r.status_code, r.text.strip()[:1000])
        try:
            return r.json()
        except ValueError:
            raise UpstreamError(r.status_code, f"non-JSON response: {r.text[:200]}")

    def datasources(self, types=("loki",)):
        """types=None 表示所有类型。"""
        ts, cached = self._ds_cache
        if cached is None or time.time() - ts >= 300:
            try:
                items = self.get_json("/api/datasources")
            except UpstreamError as e:
                if e.status != 403:
                    raise
                items = list(self.get_json("/api/frontend/settings").get("datasources", {}).values())
            cached = sorted(({"name": d["name"], "uid": d["uid"], "type": d.get("type", ""),
                              "isDefault": bool(d.get("isDefault"))} for d in items if d.get("uid")),
                            key=lambda d: d["name"])
            self._ds_cache = (time.time(), cached)
        return [d for d in cached if types is None or d["type"] in types]

    def resolve_ds(self, key, types=("loki",), kind="Loki"):
        dss = self.datasources(types)
        if not dss:
            raise UpstreamError(404, f"no {kind} datasource visible in Grafana {self.name!r}")
        names = ", ".join(d["name"] for d in dss)
        # 不设默认数据源：查错环境比报错更糟，必须显式指定
        if not key:
            raise BadRequest(f"no datasource specified; pass --ds / ds (one of: {names})")
        k = key.strip().lower()
        for d in dss:
            if d["uid"].lower() == k or d["name"].lower() == k:
                return d
        # 唯一前缀匹配，方便手敲：--ds prod / --ds test
        hits = [d for d in dss if d["name"].lower().startswith(k) or d["uid"].lower().startswith(k)]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise BadRequest(f"ambiguous datasource {key!r}: matches {', '.join(d['name'] for d in hits)}")
        raise BadRequest(f"unknown datasource {key!r}; available: {names}")

    def loki(self, ds, sub, params, headers=None):
        path = f"/api/datasources/proxy/uid/{ds['uid']}/loki/api/v1/{sub}"
        r = self.request("GET", path, params=params, headers=headers)
        if r.status_code >= 300:
            raise UpstreamError(r.status_code, r.text.strip()[:1000])
        try:
            res = r.json()
        except ValueError:
            raise UpstreamError(r.status_code, f"non-JSON response: {r.text[:200]}")
        if res.get("status") != "success":
            raise UpstreamError(502, json.dumps(res)[:1000])
        return res.get("data")

    def ds_query(self, ds, query, start_ns, end_ns):
        """Grafana 统一查询接口 /api/ds/query（和前端一样，数据源的 custom query parameters 等由 Grafana 处理）。"""
        body = {"queries": [{"refId": "A", "datasource": {"type": ds["type"], "uid": ds["uid"]}, **query}],
                "from": str(start_ns // 10**6), "to": str(end_ns // 10**6)}
        r = self.request("POST", "/api/ds/query", params={"ds_type": ds["type"]}, json=body)
        try:
            res = r.json()
        except ValueError:
            raise UpstreamError(r.status_code, f"non-JSON response: {r.text[:200]}")
        a = (res.get("results") or {}).get("A") or {}
        if a.get("error"):
            raise UpstreamError(a.get("status") or r.status_code, a["error"][:1000])
        if r.status_code >= 300:
            raise UpstreamError(r.status_code, json.dumps(res, ensure_ascii=False)[:1000])
        return a.get("frames") or []


class Hub:
    """服务端持有的所有 Grafana 实例。"""

    def __init__(self, cli_url=None, chrome=None):
        self.cli_url, self.chrome = cli_url, chrome
        self.instances, self.default = {}, None  # {name: Grafana}
        self.lock = threading.Lock()
        self.mtime = None
        self.refresh(force=True)

    def _config_mtime(self):
        try:
            return os.stat(CONFIG_FILE).st_mtime_ns
        except OSError:
            return 0

    def refresh(self, force=False):
        """配置文件变了就重新加载实例（地址没变的实例保留登录态），新增实例不用重启服务端。"""
        m = self._config_mtime()
        if not force and m == self.mtime:
            return
        with self.lock:
            self.mtime = m
            try:
                urls, default = resolve_instances(self.cli_url)
            except SystemExit as e:
                if force:
                    raise
                log(f"WARNING: config reload failed, keeping old instances: {e}")
                return
            old = self.instances
            self.instances = {
                n: old[n] if n in old and old[n].url == u
                else Grafana(n, u, self.chrome or FileCookies(cookie_file_for(n)))
                for n, u in urls.items()}
            self.default = default
            if not force:
                log(f"config reloaded: {', '.join(f'{n}={g.url}' for n, g in self.instances.items())}, "
                    f"default={default}")

    def select(self, key=None):
        """按名字 / URL / host / 名字的唯一前缀选实例；不给就用默认实例。"""
        names = ", ".join(self.instances)
        if not key:
            if self.default:
                return self.instances[self.default]
            raise BadRequest(f"several Grafana instances configured and no default; pass --grafana / grafana "
                             f"(one of: {names})")
        k = key.strip()
        if k in self.instances:
            return self.instances[k]
        url = try_normalize_url(k)
        for g in self.instances.values():
            if url == g.url or k.lower() == g.host:
                return g
        hits = [g for n, g in self.instances.items() if n.lower().startswith(k.lower())]
        if len(hits) == 1:
            return hits[0]
        raise BadRequest(f"unknown Grafana instance {key!r}; configured: {names} "
                         f"(add one with `loki_logs.py config add <name> <url>`)")


def normalize_streams(result, direction):
    """合并所有 stream，按时间排序。标签 = stream 标签 + structured metadata；
    | logfmt / | json 解析出的字段（categorize-labels 里的 parsed）本来就在日志行里，不再重复输出。"""
    entries = []
    for s in result:
        for v in s["values"]:
            labels = dict(s["stream"])
            if len(v) > 2 and isinstance(v[2], dict):
                labels.update(v[2].get("structuredMetadata") or {})
            entries.append({"ts": int(v[0]), "labels": labels, "line": v[1]})
    if not entries:
        return {"common_labels": {}, "entries": []}
    common = dict(entries[0]["labels"])
    for e in entries[1:]:
        common = {k: v for k, v in common.items() if e["labels"].get(k) == v}
    for e in entries:
        e["labels"] = {k: v for k, v in e["labels"].items() if k not in common}
    entries.sort(key=lambda e: e["ts"], reverse=(direction == "backward"))
    return {"common_labels": common, "entries": entries}


def health_of(g):
    u = g.get_json("/api/user")
    return {"ok": True, "name": g.name, "grafana": g.url, "user": u.get("email") or u.get("login"),
            "browser": g.src.browser, "profile": g.profile}


def api_health(hub, p):
    """给了 grafana 就只查这一个（失败直接报错）；否则查所有实例，各自报告。"""
    if p.get("grafana"):
        return {"instances": [health_of(hub.select(p["grafana"]))], "default": hub.default}
    out = []
    for g in hub.instances.values():
        try:
            out.append(health_of(g))
        except (AuthError, UpstreamError) as e:
            out.append({"ok": False, "name": g.name, "grafana": g.url, "error": str(e)})
        except Exception as e:  # 网络错误等
            out.append({"ok": False, "name": g.name, "grafana": g.url, "error": f"{type(e).__name__}: {e}"})
    return {"instances": out, "default": hub.default}


def api_datasources(hub, p):
    g = hub.select(p.get("grafana"))
    t = (p.get("type") or "loki").strip().lower()
    return {"grafana": g.name, "datasources": g.datasources(None if t == "all" else tuple(t.split(",")))}


def api_labels(hub, p):
    g = hub.select(p.get("grafana"))
    ds = g.resolve_ds(p.get("ds"))
    start, end = resolve_range(p)
    return {"datasource": ds["name"], "labels": g.loki(ds, "labels", {"start": start, "end": end}) or []}


def api_label_values(hub, p):
    if not p.get("name"):
        raise BadRequest("missing 'name'")
    g = hub.select(p.get("grafana"))
    ds = g.resolve_ds(p.get("ds"))
    start, end = resolve_range(p)
    params = {"start": start, "end": end}
    if p.get("query"):
        params["query"] = p["query"]
    sub = f"label/{urllib.parse.quote(p['name'], safe='')}/values"
    return {"datasource": ds["name"], "name": p["name"], "values": g.loki(ds, sub, params) or []}


def api_series(hub, p):
    if not p.get("match"):
        raise BadRequest("missing 'match'")
    g = hub.select(p.get("grafana"))
    ds = g.resolve_ds(p.get("ds"))
    start, end = resolve_range(p)
    data = g.loki(ds, "series", [("match[]", p["match"]), ("start", start), ("end", end)])
    return {"datasource": ds["name"], "series": data or []}


def api_query(hub, p):
    if not p.get("q"):
        raise BadRequest("missing 'q' (LogQL)")
    g = hub.select(p.get("grafana"))
    ds = g.resolve_ds(p.get("ds"))
    start, end = resolve_range(p)
    try:
        limit = max(1, min(int(p.get("limit") or DEFAULT_LIMIT), MAX_LIMIT))
    except ValueError:
        raise BadRequest("bad limit")
    direction = "forward" if p.get("direction") == "forward" else "backward"
    params = {"query": p["q"], "start": start, "end": end, "limit": limit, "direction": direction}
    if p.get("step"):
        params["step"] = p["step"]
    data = g.loki(ds, "query_range", params, headers={"X-Loki-Response-Encoding-Flags": "categorize-labels"})
    out = {"grafana": g.name, "datasource": ds["name"], "start": start, "end": end, "limit": limit,
           "direction": direction, "resultType": data["resultType"]}
    if data["resultType"] == "streams":
        out.update(normalize_streams(data["result"], direction))
    else:
        out["series"] = [{"labels": r.get("metric", {}), "values": r.get("values") or [r["value"]]}
                         for r in data["result"]]
    summary = (data.get("stats") or {}).get("summary") or {}
    out["stats"] = {k: summary[k] for k in ("totalBytesProcessed", "execTime") if k in summary}
    return out


def prom_frames_to_series(frames):
    series = []
    for f in frames:
        fields = (f.get("schema") or {}).get("fields") or []
        values = (f.get("data") or {}).get("values") or []
        ti = next((i for i, fd in enumerate(fields) if fd.get("type") == "time"), None)
        if ti is None or ti >= len(values):
            continue
        for i, fd in enumerate(fields):
            if i == ti or fd.get("type") != "number" or i >= len(values):
                continue
            pts = [[t / 1000, v] for t, v in zip(values[ti], values[i]) if v is not None]
            series.append({"labels": fd.get("labels") or {}, "values": pts})
    return series


def api_prom(hub, p):
    if not p.get("q"):
        raise BadRequest("missing 'q' (PromQL)")
    g = hub.select(p.get("grafana"))
    ds = g.resolve_ds(p.get("ds"), PROM_TYPES, "Prometheus")
    start, end = resolve_range(p)
    instant = str(p.get("instant") or "").lower() in ("1", "true", "yes")
    span = (end - start) / 1e9
    step = parse_duration_ns(p["step"]) / 1e9 if p.get("step") else max(15.0, span / 200)
    step = max(step, 1.0, span / 11000)  # Prometheus 单次最多 11000 个点
    query = {"expr": p["q"], "range": not instant, "instant": instant, "format": "time_series",
             "interval": f"{int(step)}s", "intervalMs": int(step * 1000), "maxDataPoints": int(span / step) + 1}
    series = prom_frames_to_series(g.ds_query(ds, query, start, end))
    if instant:
        for s in series:
            s["values"] = s["values"][-1:]
    return {"grafana": g.name, "datasource": ds["name"], "start": start, "end": end,
            "step": None if instant else int(step), "instant": instant, "series": series}


def api_config(hub, p):
    d = hub.instances.get(hub.default) if hub.default else None
    return {"grafana_url": d.url if d else next(iter(hub.instances.values())).url,  # 兼容老版本扩展
            "default": hub.default,
            "grafanas": [{"name": n, "url": g.url} for n, g in hub.instances.items()]}


def api_reload(hub, p):
    gs = [hub.select(p["grafana"])] if p.get("grafana") else list(hub.instances.values())
    for g in gs:
        try:
            g.reload_cookies()
        except AuthError as e:
            log(f"[{g.name}] reload: {e}")
    return api_health(hub, p)


def api_push_cookies(hub, p):
    # 只接受 Chrome 扩展发来的（网页发起的请求 Origin 是 http(s)://...）
    if not p.get("_origin", "").startswith("chrome-extension://"):
        raise BadRequest("cookies can only be pushed by the browser extension")
    g = hub.select(p.get("grafana"))
    if not hasattr(g.src, "save"):
        raise BadRequest("server is not using a cookie file (started with --source chrome)")
    try:
        items = json.loads(p.get("_body") or "{}").get("cookies") or []
        cookies = {c["name"]: c["value"] for c in items}
    except (ValueError, TypeError, KeyError, AttributeError):
        raise BadRequest("bad body, expected {\"cookies\": [{\"name\": ..., \"value\": ...}]}")
    if not cookies:
        raise BadRequest("no cookies")
    if not g.accept_pushed(cookies):
        return {"ok": True, "name": g.name, "updated": False}
    log(f"[{g.name}] cookies pushed by extension ({p.get('reason') or '?'})")
    try:
        return {"updated": True, **health_of(g)}
    except (AuthError, UpstreamError) as e:
        return {"ok": False, "name": g.name, "updated": True, "error": str(e)}


ROUTES = {
    ("GET", "/prom"): api_prom,
    ("GET", "/health"): api_health,
    ("GET", "/datasources"): api_datasources,
    ("GET", "/labels"): api_labels,
    ("GET", "/label_values"): api_label_values,
    ("GET", "/series"): api_series,
    ("GET", "/query"): api_query,
    ("GET", "/config"): api_config,
    ("POST", "/reload"): api_reload,
    ("POST", "/cookies"): api_push_cookies,
}


class Handler(BaseHTTPRequestHandler):
    hub = None
    allowed_hosts = set()

    def _handle(self, method):
        u = urllib.parse.urlparse(self.path)
        # 防 DNS rebinding：只接受 localhost 形式的 Host 头
        if self.headers.get("Host", "") not in self.allowed_hosts:
            return self._reply(403, {"error": "forbidden host"})
        route = ROUTES.get((method, u.path))
        if route is None:
            return self._reply(404, {"error": f"no route {method} {u.path}"})
        p = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items() if not k.startswith("_")}
        p["_origin"] = self.headers.get("Origin", "")
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            p["_body"] = self.rfile.read(min(n, 1 << 20)).decode("utf-8", "replace")
        try:
            self.hub.refresh()
            self._reply(200, route(self.hub, p))
        except BadRequest as e:
            self._reply(400, {"error": str(e)})
        except AuthError as e:
            self._reply(401, {"error": str(e)})
        except UpstreamError as e:
            self._reply(502, {"error": str(e)})
        except Exception as e:
            log(f"error: {e!r}")
            self._reply(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _reply(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        log(f"{self.command} {urllib.parse.unquote(self.path)[:300]} -> {args[1] if len(args) > 1 else ''}")


def run_server(args):
    hub = Hub(args.grafana_url, ChromeCookies(args.browser, args.profile) if args.source == "chrome" else None)
    for h in api_health(hub, {})["instances"]:
        if h["ok"]:
            log(f"[{h['name']}] logged in to {h['grafana']} as {h['user']}")
        else:
            log(f"[{h['name']}] WARNING: initial login check failed: {h['error']}")
    Handler.hub = hub
    Handler.allowed_hosts = {f"{h}:{args.port}" for h in ("127.0.0.1", "localhost")}
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    log(f"listening on http://127.0.0.1:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- client

class ClientError(Exception):
    pass


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 不走 http_proxy


SERVER_LOG = os.path.join(CONFIG_DIR, "server.log")


def start_server(server):
    """本地服务端没开时在后台拉起来（脱离当前进程组），等端口可连后返回。"""
    port = urllib.parse.urlparse(server).port or 7788
    os.makedirs(os.path.dirname(SERVER_LOG), mode=0o700, exist_ok=True)
    with open(SERVER_LOG, "ab") as logf:
        proc = subprocess.Popen([sys.executable, SCRIPT, "serve", "--port", str(port)],
                                stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True)
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            if proc.poll() is not None:  # 起不来（缺 requests、端口被占……）
                break
            time.sleep(0.2)
    try:
        with open(SERVER_LOG, errors="replace") as f:
            tail = "".join(f.readlines()[-6:]).strip()
    except OSError:
        tail = ""
    if "Grafana URL is not configured" in tail:  # 把配置提示原样给出来，AI 照着问用户要地址
        raise ClientError(tail[tail.index("Grafana URL is not configured"):])
    hint = (f" Missing dependency, run: {sys.executable} -m pip install requests."
            if "No module named 'requests'" in tail else "")
    raise ClientError(f"failed to start loki_logs server ({sys.executable} {SCRIPT} serve).{hint} "
                      f"Log {SERVER_LOG}:\n{tail}")


def call(path, params=None, method="GET", server=None, autostart=True):
    server = (server or DEFAULT_SERVER).rstrip("/")
    q = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
    req = urllib.request.Request(f"{server}{path}{'?' + q if q else ''}", method=method)
    try:
        with _opener.open(req, timeout=180) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(body)["error"]
        except (ValueError, KeyError, TypeError):
            pass
        raise ClientError(f"HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        local = urllib.parse.urlparse(server).hostname in ("127.0.0.1", "localhost")
        if (autostart and local and isinstance(e.reason, ConnectionRefusedError)
                and not os.environ.get("LOKI_LOGS_NO_AUTOSTART")):
            start_server(server)
            return call(path, params, method, server, autostart=False)
        raise ClientError(
            f"cannot reach loki_logs server at {server} ({e.reason}). Start it with: python3 {SCRIPT} serve. "
            f"If you are running inside a sandbox without network access (e.g. Codex), use the loki MCP tools instead.")


def fmt_labels(d):
    return "{" + ", ".join(f'{k}="{v}"' for k, v in sorted(d.items())) + "}"


def render_prom(res):
    head = f"# grafana={res['grafana']} datasource={res['datasource']} "
    if res["instant"]:
        out = [head + f"instant at {fmt_ts(res['end'])}", f"# {len(res['series'])} series"]
        for s in res["series"]:
            v = s["values"][-1][1] if s["values"] else None
            out.append(f"{fmt_labels(s['labels'])}  {v}")
        return "\n".join(out)
    out = [head + f"range=[{fmt_ts(res['start'])}, {fmt_ts(res['end'])}] step={res['step']}s",
           f"# {len(res['series'])} series"]
    for s in res["series"]:
        out.append(fmt_labels(s["labels"]))
        for ts, v in s["values"]:
            out.append(f"  {fmt_ts(int(ts * 1e9))}  {v}")
    return "\n".join(out)


def render_health(res):
    out = []
    for h in res["instances"]:
        mark = " (default)" if h["name"] == res.get("default") else ""
        if h["ok"]:
            out.append(f"ok: [{h['name']}{mark}] {h['grafana']} as {h['user']} "
                       f"(cookies from {h['browser']}: {h['profile']})")
        else:
            out.append(f"FAIL: [{h['name']}{mark}] {h['grafana']}: {h['error']}")
    return "\n".join(out)


def render_query(res, max_line_len=0):
    out = [f"# grafana={res['grafana']} datasource={res['datasource']} "
           f"range=[{fmt_ts(res['start'])}, {fmt_ts(res['end'])}] direction={res['direction']}"]
    if res["resultType"] == "streams":
        n = len(res["entries"])
        note = (f" (hit limit {res['limit']}, there may be more: narrow the time range/filter or raise limit)"
                if n >= res["limit"] else "")
        out.append(f"# {n} lines{note}")
        if res["common_labels"]:
            out.append(f"# common labels: {fmt_labels(res['common_labels'])}")
        for e in res["entries"]:
            line = e["line"].rstrip("\n")
            if max_line_len and len(line) > max_line_len:
                line = f"{line[:max_line_len]}...[+{len(line) - max_line_len} chars]"
            lab = f"{fmt_labels(e['labels'])} " if e["labels"] else ""
            out.append(f"{fmt_ts(e['ts'])} {lab}{line}")
    else:
        out.append(f"# {res['resultType']}: {len(res['series'])} series")
        for s in res["series"]:
            out.append(fmt_labels(s["labels"]))
            for ts, v in s["values"]:
                out.append(f"  {fmt_ts(int(float(ts) * 1e9))}  {v}")
    return "\n".join(out)


def render(kind, res, max_line_len=0):
    if kind in ("health", "reload"):
        return render_health(res)
    if kind == "ds":
        return (f"# grafana={res['grafana']}\n" + "\n".join(
            f"{d['name']}  type={d['type']}  uid={d['uid']}" for d in res["datasources"])) if res["datasources"] \
            else f"# grafana={res['grafana']}\n(no datasources of this type; try type=all)"
    if kind == "prom":
        return render_prom(res)
    if kind == "labels":
        return "\n".join(res["labels"]) or "(no labels)"
    if kind == "values":
        return "\n".join(res["values"]) or "(no values)"
    if kind == "series":
        return "\n".join(fmt_labels(s) for s in res["series"]) or "(no series)"
    return render_query(res, max_line_len)


def range_params(a):
    return {"grafana": a.get("grafana"), "ds": a.get("ds"), "since": a.get("since"),
            "start": a.get("start"), "end": a.get("end")}


def client_request(kind, a):
    """kind + 参数 dict → (path, params, method)，CLI 和 MCP 共用。"""
    g = {"grafana": a.get("grafana")}
    if kind == "health":
        return "/health", g, "GET"
    if kind == "reload":
        return "/reload", g, "POST"
    if kind == "ds":
        return "/datasources", {**g, "type": a.get("type")}, "GET"
    if kind == "prom":
        return "/prom", {**range_params(a), "q": a.get("query"), "step": a.get("step"),
                         "instant": "1" if a.get("instant") else None}, "GET"
    if kind == "labels":
        return "/labels", range_params(a), "GET"
    if kind == "values":
        return "/label_values", {**range_params(a), "name": a.get("name"), "query": a.get("query")}, "GET"
    if kind == "series":
        return "/series", {**range_params(a), "match": a.get("match")}, "GET"
    if kind == "query":
        return "/query", {**range_params(a), "q": a.get("query"), "limit": a.get("limit"),
                          "direction": a.get("direction"), "step": a.get("step")}, "GET"
    raise ValueError(kind)


# ---------------------------------------------------------------- MCP (stdio, JSON-RPC 2.0)

_GRAFANA_PROP = {
    "grafana": {"type": "string", "description": "Grafana instance name (see loki_health), URL or unique name prefix. "
                                                 "Optional: defaults to the configured default instance."},
}

_RANGE_PROPS = {
    **_GRAFANA_PROP,
    "ds": {"type": "string", "description": "Datasource (environment) name, uid or unique prefix, e.g. \"PROD Logs\" / \"test\". "
                                            "Required, there is no default: use the environment the user asked for; "
                                            "if they did not say, ask them (list options with loki_datasources; type=prometheus for prom_query)."},
    "since": {"type": "string", "description": "Look-back window ending at `end`, e.g. 15m, 1h, 2d. Default 1h. Ignored if `start` is set."},
    "start": {"type": "string", "description": "Range start: relative (2h = now-2h), unix ts, or ISO time like 2026-11-13T22:00:00+08:00 (no tz = server local time)."},
    "end": {"type": "string", "description": "Range end, same formats as start. Default now."},
}

MCP_INSTRUCTIONS = """\
Query Loki logs (and Prometheus/PromQL metrics via prom_query) in the team Grafana through a local loki_logs server (auto-started on demand).
Several Grafana instances can be configured; loki_health lists them with their names, URLs and login state. Every tool takes an optional `grafana` (instance name) and uses the default instance otherwise; when the user names a Grafana host/URL, pass the matching instance.

Before querying, make sure you know (ask the user instead of guessing):
1. Service: usually the `app` label. If the name is vague, list candidates with loki_label_values(name="app", ds=...) and ask. Never query a whole namespace without a service filter.
2. Environment (`ds`): required, there is NO default. If the user did not say, list options with loki_datasources and ask. Always state which environment you queried; call out PROD explicitly.
3. Time range: default the last 1h if unspecified, and state it.

Workflow:
- If unsure about login state, call loki_health first.
- Pull ~5 lines to learn the log format (logfmt `key=value`, JSON, or plain text) and its fields.
- Look at volume before lines: e.g. sum by (detected_level) (count_over_time({app="<app>"} [5m])) with step.
- Then fetch lines with a small limit, a narrow time range and max_line_len.
- `detected_level` is structured metadata: filter it in the pipeline (`{app="<app>"} | detected_level=~"warn|error"`), not inside {}. If it is all `unknown`, use `| logfmt | level=~"(?i)warn|error"` or `|~ "(?i)error"`.
- Follow a trace / request id with `{app="<app>"} |= "<id>"` over a wider range.
- Summarize key lines (timestamp + key fields) and counts; do not dump raw logs.

If the server fails to start because the Grafana URL is not configured, or the user wants a Grafana that is not configured yet, show the user the command (`loki_logs.py config add <name> <url>`) and ask for their Grafana URL; the server picks up config changes without a restart.
If a tool says the Grafana login expired or there are no cookies yet: stop and ask the user to open the Grafana URL from the error message in Chrome and log in (the loki_logs cookie-sync extension pushes cookies to the server automatically; for a newly added instance the user first clicks the extension icon and grants access). Never ask the user to paste cookies into the chat.
"""

MCP_TOOLS = [
    {
        "name": "loki_health",
        "description": "List the configured Grafana instances and check that the local loki_logs server is running and "
                       "logged in to each (starts it if needed). On a login error, ask the user to log in to that Grafana in Chrome.",
        "inputSchema": {"type": "object", "properties": {**_GRAFANA_PROP}},
        "kind": "health",
    },
    {
        "name": "loki_datasources",
        "description": "List datasources in a Grafana instance: Loki ones (environments) by default, "
                       "type=\"prometheus\" for PromQL ones, type=\"all\" for every type.",
        "inputSchema": {"type": "object", "properties": {
            **_GRAFANA_PROP,
            "type": {"type": "string", "description": "loki (default), prometheus, all, or comma-separated Grafana plugin types"}}},
        "kind": "ds",
    },
    {
        "name": "prom_query",
        "description": (
            "Run a PromQL query against a Prometheus-type datasource (Prometheus, GreptimeDB/VictoriaMetrics PromQL, ...) "
            "via Grafana's /api/ds/query, the same path dashboards use. Use instant=true for the current value; "
            "for range queries pick a step so that the number of points stays small (range/step), e.g. 6h with step 10m."
        ),
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "PromQL expression"},
            "instant": {"type": "boolean", "description": "Instant query at `end` (default false = range query)"},
            "step": {"type": "string", "description": "Range query step, e.g. 1m (default: range/200, at least 15s)"},
            **_RANGE_PROPS}, "required": ["query", "ds"]},
        "kind": "prom",
    },
    {
        "name": "loki_labels",
        "description": "List Loki label names that have data in the time range.",
        "inputSchema": {"type": "object", "properties": {**_RANGE_PROPS}, "required": ["ds"]},
        "kind": "labels",
    },
    {
        "name": "loki_label_values",
        "description": "List values of a Loki label (e.g. app, namespace, pod), optionally restricted by a stream selector.",
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Label name, e.g. app"},
            "query": {"type": "string", "description": 'Optional stream selector to restrict values, e.g. {namespace="my-namespace"}'},
            **_RANGE_PROPS}, "required": ["name", "ds"]},
        "kind": "values",
    },
    {
        "name": "loki_series",
        "description": "List the distinct label sets (streams) matching a stream selector.",
        "inputSchema": {"type": "object", "properties": {
            "match": {"type": "string", "description": 'Stream selector, e.g. {app="my-service"}'},
            **_RANGE_PROPS}, "required": ["match", "ds"]},
        "kind": "series",
    },
    {
        "name": "loki_query",
        "description": (
            "Run a LogQL query against Loki via Grafana. Log queries return log lines (newest first by default); "
            "metric queries (count_over_time, rate, sum by ...) return time series. "
            "Tips: start with a narrow time range and a small limit; filter with |= \"text\", |~ \"regex\", "
            "parse logfmt lines with | logfmt and filter fields like | logfmt | level=\"ERROR\". "
            'Always scope the selector to the service the user asked about; if unclear, list candidates with '
            'loki_label_values(name="app") and ask. Example: {app="my-service"} | detected_level=~"warn|error"'
        ),
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "LogQL expression"},
            "limit": {"type": "integer", "description": f"Max log lines (default {DEFAULT_LIMIT}, max {MAX_LIMIT})"},
            "direction": {"type": "string", "enum": ["backward", "forward"], "description": "backward = newest first (default)"},
            "step": {"type": "string", "description": "Step for metric queries, e.g. 1m"},
            "max_line_len": {"type": "integer", "description": "Truncate each log line to this many chars (default 1000, 0 = no limit)"},
            **_RANGE_PROPS}, "required": ["query", "ds"]},
        "kind": "query",
    },
]


def mcp_call(name, args):
    tool = next((t for t in MCP_TOOLS if t["name"] == name), None)
    if tool is None:
        return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
    try:
        path, params, method = client_request(tool["kind"], args)
        res = call(path, params, method)
        text = render(tool["kind"], res, int(args.get("max_line_len", 1000) or 0))
        return {"content": [{"type": "text", "text": text}]}
    except ClientError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}


def run_mcp():
    def send(obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    # 全部是只读查询：标上 readOnlyHint，客户端（如 Codex 的 writes 审批模式）可以据此免确认
    tools = [{**{k: v for k, v in t.items() if k != "kind"},
              "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}}
             for t in MCP_TOOLS]
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        mid, method = msg.get("id"), msg.get("method")
        if mid is None or method is None:  # 通知或对端的响应，不回复
            continue
        params = msg.get("params") or {}
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "loki-logs", "version": "1.2.0"},
                    "instructions": MCP_INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": tools}
            elif method == "tools/call":
                result = mcp_call(params.get("name"), params.get("arguments") or {})
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})
                continue
            send({"jsonrpc": "2.0", "id": mid, "result": result})
        except Exception as e:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}})


# ---------------------------------------------------------------- CLI

def cmd_config(args):
    cfg = load_config()
    changed = None
    if args.grafana_url:  # 老用法：等价于 config add default <url>
        args.action, args.name, args.url = "add", LEGACY_NAME, args.grafana_url
    if args.action == "add":
        if not args.name or not args.url:
            sys.exit("usage: config add <name> <url> [--default]")
        if not _NAME.match(args.name):
            sys.exit(f"error: bad instance name {args.name!r} (letters, digits, _ . -)")
        url = normalize_grafana_url(args.url)
        inst = config_instances(cfg)  # 先把老的 grafana_url 迁移进来
        inst[args.name] = url
        cfg["grafana"] = {n: {"url": u} for n, u in inst.items()}
        cfg.pop("grafana_url", None)
        if args.default or len(inst) == 1:
            cfg["default"] = args.name
        changed = f"added {args.name} = {url}"
    elif args.action == "remove":
        inst = config_instances(cfg)
        if args.name not in inst:
            sys.exit(f"error: no instance {args.name!r}; configured: {', '.join(inst) or '(none)'}")
        del inst[args.name]
        cfg["grafana"] = {n: {"url": u} for n, u in inst.items()}
        cfg.pop("grafana_url", None)
        if cfg.get("default") == args.name:
            cfg.pop("default")
        changed = f"removed {args.name}"
    elif args.action == "default":
        inst = config_instances(cfg)
        if args.name not in inst:
            sys.exit(f"error: no instance {args.name!r}; configured: {', '.join(inst) or '(none)'}")
        cfg["default"] = args.name
        changed = f"default = {args.name}"
    if changed:
        write_config(cfg)
        print(f"{changed} (wrote {CONFIG_FILE}; the running server picks it up automatically)")
        cfg = load_config()
    inst = config_instances(cfg)
    default = cfg.get("default") if cfg.get("default") in inst else None
    print(f"config file: {CONFIG_FILE}")
    if not inst:
        print("no Grafana instance configured; add one with: config add <name> <url> --default")
    for n, u in inst.items():
        mark = " (default)" if n == default else ""
        has = "yes" if os.path.exists(cookie_file_for(n)) else "no"
        print(f"  {n}{mark}: {u}  cookies={cookie_file_for(n)} (exists: {has})")
    env = os.environ.get("LOKI_LOGS_GRAFANA_URL")
    if env:
        print(f"LOKI_LOGS_GRAFANA_URL (overrides instance {LEGACY_NAME!r}): {env}")


def main():
    global DEFAULT_SERVER
    ap = argparse.ArgumentParser(description="Query Loki logs / PromQL metrics through Grafana (client/server).",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--server", default=DEFAULT_SERVER, help=f"server address (default {DEFAULT_SERVER})")
    ap.add_argument("--grafana", help="Grafana instance name / URL (default: the configured default)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def sp_(name, **kw):  # 子命令里也可以写 --grafana
        p = sub.add_parser(name, **kw)
        p.add_argument("--grafana", default=argparse.SUPPRESS, help="Grafana instance name / URL")
        return p

    sp = sub.add_parser("serve", help="run the local server")
    sp.add_argument("--port", type=int, default=urllib.parse.urlparse(DEFAULT_SERVER).port or 7788)
    sp.add_argument("--grafana-url", help=f"override the URL of instance {LEGACY_NAME!r} (and LOKI_LOGS_GRAFANA_URL)")
    sp.add_argument("--source", choices=["file", "chrome"], default="file",
                    help="file: cookie files fed by the extension / set-cookie (default); chrome: read Chrome's cookie DB")
    sp.add_argument("--browser", choices=sorted(BROWSERS), default="chrome", help="for --source chrome")
    sp.add_argument("--profile", help='for --source chrome: profile dir, e.g. "Default" / "Profile 1" (default: auto)')

    sub.add_parser("mcp", help="run as a stdio MCP server")
    sp_("health", help="check login status of all (or one) Grafana instances")
    sp_("reload", help="force the server to re-read cookies")
    p = sub.add_parser("config", help=f"show or edit Grafana instances ({CONFIG_FILE})")
    p.add_argument("action", nargs="?", choices=["show", "add", "remove", "default"], default="show")
    p.add_argument("name", nargs="?")
    p.add_argument("url", nargs="?")
    p.add_argument("--default", action="store_true", help="with add: make it the default instance")
    p.add_argument("--grafana-url", help=argparse.SUPPRESS)  # 老用法
    p = sp_("set-cookie", help="read a Cookie header value from stdin into an instance's cookie file")
    p = sp_("ds", help="list datasources (Loki by default)")
    p.add_argument("--type", help="loki (default), prometheus, all, or comma-separated plugin types")

    def add_range(p):
        p.add_argument("--ds", help="datasource name, uid or unique prefix, e.g. prod / test (required)")
        p.add_argument("--since", help="look-back window, e.g. 30m / 2h / 1d (default 1h)")
        p.add_argument("--start")
        p.add_argument("--end")
        p.add_argument("--json", action="store_true", help="print raw JSON")

    add_range(sp_("labels", help="list label names"))
    p = sp_("values", help="list values of a label")
    p.add_argument("name")
    p.add_argument("--query", help="stream selector to restrict values")
    add_range(p)
    p = sp_("series", help="list streams matching a selector")
    p.add_argument("match")
    add_range(p)
    p = sp_("query", help="run a LogQL query")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    p.add_argument("--asc", action="store_true", help="oldest first (direction=forward)")
    p.add_argument("--step", help="step for metric queries, e.g. 1m")
    p.add_argument("--max-line-len", type=int, default=0, help="truncate long lines (0 = no limit)")
    add_range(p)
    p = sp_("prom", help="run a PromQL query on a Prometheus-type datasource")
    p.add_argument("query")
    p.add_argument("--instant", action="store_true", help="instant query at --end (default: range query)")
    p.add_argument("--step", help="range query step, e.g. 1m (default: range/200, at least 15s)")
    add_range(p)

    args = ap.parse_args()
    if args.cmd == "serve":
        return run_server(args)
    if args.cmd == "mcp":
        DEFAULT_SERVER = args.server
        return run_mcp()
    if args.cmd == "config":
        return cmd_config(args)

    if args.cmd == "set-cookie":
        cookies = parse_cookie_header(sys.stdin.read())
        if not cookies:
            sys.exit("error: no cookies on stdin (expected the Cookie header value, e.g. `pbpaste | ... set-cookie`)")
        urls, default = resolve_instances()
        name = args.grafana or default
        if name not in urls:
            sys.exit(f"error: pass --grafana <name>, one of: {', '.join(urls)}")
        path = cookie_file_for(name)
        write_cookie_file(path, cookies)
        print(f"wrote {len(cookies)} cookies ({', '.join(sorted(cookies))}) to {path}")
        try:
            print(render("health", call("/reload", {"grafana": name}, method="POST", server=args.server)))
        except ClientError as e:
            print(f"server reload: {e}")
        return

    a = vars(args)
    if args.cmd == "query":
        a["direction"] = "forward" if args.asc else "backward"
    try:
        path, params, method = client_request(args.cmd, a)
        res = call(path, params, method, server=args.server)
    except ClientError as e:
        sys.exit(f"error: {e}")
    if a.get("json"):
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(render(args.cmd, res, a.get("max_line_len") or 0))


if __name__ == "__main__":
    main()
