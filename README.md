# grafana-connector

让 Claude Code、Codex、omp（oh-my-pi）能直接查团队 Grafana 上的 Loki 日志（例如 TEST Logs / PROD Logs 数据源），以及 Prometheus 类数据源的 PromQL 指标。支持同时配置多个 Grafana（每个实例单独登录）。仓库本身就是一个 [Agent Skill](https://agentskills.io)（`grafana-connector`），同时自带一个 MCP server。

Grafana 前面通常有 SSO（比如 AWS Verified Access + Google 登录），脚本自己没法登录，所以复用你 Chrome 里的登录态：

```
Chrome（已登录 Grafana）
  └─ cookie 同步扩展 ──POST cookie──▶ grafana-connector serve（127.0.0.1:7788，持有登录态）──▶ Grafana ─▶ Loki / Prometheus
                                              ▲
                   MCP / CLI（Claude Code、Codex、omp 的各个 session 共用）
```

- 登录态只放在本机服务端（每个 Grafana 实例一个文件 `~/.config/grafana-connector/cookies/<name>.txt`，老的单实例配置仍用 `cookie.txt`，权限 600），所有 AI session 共用，不用各自登录。
- 服务端不用手动启动：MCP / CLI 第一次调用时发现它没开，会自动在后台拉起（日志在 `~/.config/grafana-connector/server.log`）。
- Chrome 里重新登录或 cookie 刷新时，扩展会自动推送最新 cookie，服务端不用重启。

## 依赖

- macOS + Chrome（其它 Chromium 系浏览器也可以加载扩展）
- Python ≥ 3.11，服务端需要 `requests`：`python3 -m pip install requests`
- CLI 客户端和 MCP 只用标准库

## 安装

1. 克隆到 `~/.agents/skills`（Codex 和 omp 的用户级 skills 目录），再给 Claude Code 建一个软链接：
   ```bash
   git clone <repo-url> ~/.agents/skills/grafana-connector
   ```
   ```bash
   ln -s ~/.agents/skills/grafana-connector ~/.claude/skills/grafana-connector
   ```
2. 配置 Grafana 实例（写入 `~/.config/grafana-connector/config.toml`，路径可用 `GRAFANA_CONNECTOR_CONFIG` 改）。每个 Grafana 起一个名字，支持带子路径的地址：
   ```bash
   ~/.agents/skills/grafana-connector/scripts/grafana_connector.py config add main https://grafana.example.com --default
   ```
   ```bash
   ~/.agents/skills/grafana-connector/scripts/grafana_connector.py config add arb https://admin.example.com/arb-grafana
   ```
   `config` 查看所有实例，`config default <name>` 改默认实例，`config remove <name>` 删除。服务端会自动加载配置变化，不用重启。
   老配置（顶层 `grafana_url`）继续可用，视为名为 `default` 的实例；环境变量 `GRAFANA_CONNECTOR_GRAFANA_URL` / `serve --grafana-url` 会覆盖这个实例的地址。
3. 注册 MCP server：
   - **Codex（必须）**：Codex 的 shell 默认在无网络沙箱里，CLI 连不上本地服务端，只能走 MCP。
     ```bash
     codex mcp add grafana -- python3 ~/.agents/skills/grafana-connector/scripts/grafana_connector.py mcp
     ```
     Codex 默认每次调用 MCP 工具都要你确认（`codex exec` 非交互模式下会直接被拒）。loki 的工具全是只读查询，并且都带了 `readOnlyHint` 标注，如果不想每次确认，可以在 `~/.codex/config.toml` 的 `[mcp_servers.grafana]` 下加一行（`writes` = 只对非只读工具询问）：
     ```toml
     default_tools_approval_mode = "writes"
     ```
   - **Claude Code（可选）**：不注册也能通过 skill 调用 CLI。
     ```bash
     claude mcp add -s user grafana -- python3 ~/.agents/skills/grafana-connector/scripts/grafana_connector.py mcp
     ```
   - **omp**：不需要，skill 会直接调用 CLI。
4. 安装 Chrome 扩展：打开 `chrome://extensions` → 打开右上角「开发者模式」→「加载已解压的扩展程序」→ 选 `~/.agents/skills/grafana-connector/chrome_ext/grafana_connector_cookie_sync`（选择框里按 `⌘⇧G` 输入路径，或按 `⌘⇧.` 显示隐藏目录）。
5. 先验证一次（会顺便拉起服务端，此时还没有 cookie，报 `no cookies yet` 是正常的）：
   ```bash
   ~/.agents/skills/grafana-connector/scripts/grafana_connector.py health
   ```
6. 点工具栏上的扩展图标，在弹出的小窗里点「授权并推送所有 Grafana 的 cookie」，允许扩展读取这些 Grafana 域名的 cookie（扩展从本地服务端读取实例列表，只申请这些域名的权限）。以后新加实例，再点一次这个按钮授权即可。
7. 在 Chrome 里打开 Grafana 并登录，扩展会自动推送 cookie。再验证：
   ```bash
   ~/.agents/skills/grafana-connector/scripts/grafana_connector.py health
   ```
   每个实例输出一行 `ok: [<name>] <Grafana 地址> as <你的邮箱>` 就装好了。

扩展图标角标（多个实例时显示最差的那个，点开小窗看每个实例的状态）：无角标 = 同步正常；`auth` = 还没授权读取 Grafana 域名的 cookie（点图标里的按钮）；`off` = 连不上服务端；`?` = Chrome 里还没登录 Grafana；`!` / `err` = 服务端拒绝或出错（点开图标看详情）。刚装上推送没反应时，在 `chrome://extensions` 里点扩展卡片上的刷新按钮。

## 使用

在 Claude Code / Codex / omp 里直接说，**写清楚服务名、环境（test / prod）和时间范围**，例如「查一下 test 环境 `<你的服务>` 最近半小时的 error 日志，按错误类型统计」，AI 会自动加载 `grafana-connector` skill（Codex 里也可以用 `$grafana-connector` 显式调用）。没说清楚查哪个服务时，AI 会先列出候选让你确认。

也可以自己在命令行用：

```bash
alias grafana=~/.agents/skills/grafana-connector/scripts/grafana_connector.py   # 可以加进 ~/.zshrc
grafana ds                                                  # 数据源；其它命令都要用 --ds 指定环境，没有默认值
grafana values app --ds test --since 1h                     # 有哪些服务
grafana query '{app="<app>"}' --ds test --since 15m --limit 5  # 先看几行，确认日志格式
grafana query '{app="<app>"} | detected_level=~"warn|error"' --ds test --since 1h
grafana query '{app="<app>"} |= "<keyword>"' --ds test --since 30m --limit 50
grafana query '{app="<app>"} | logfmt | <field>="<value>"' --ds test --since 3h
grafana query 'sum by (<field>) (count_over_time({app="<app>"} | logfmt [5m]))' --ds test --since 2h --step 5m
grafana query '{app="<app>"}' --ds prod --since 15m
grafana health                                              # 所有 Grafana 实例的登录状态
grafana --grafana arb ds --type prometheus                  # --grafana 选实例（名字、URL 或名字前缀），不写用默认实例
grafana --grafana arb prom 'sum by(job)(up)' --ds greptimedb --instant
grafana --grafana arb prom 'rate(x_total[5m])' --ds greptimedb --since 6h --step 10m
```

PromQL 查询走 Grafana 的 `/api/ds/query`（和 dashboard 一样），数据源上配置的 custom query parameters（比如 GreptimeDB 的 `db=...`）由 Grafana 自动带上。

环境：`--ds` 必填，写完整名字（`"PROD Logs"`）、uid 或唯一前缀（`prod` / `test`）都行；故意不设默认值，免得查错环境。

时间参数：`--since 30m`（默认 1h），或 `--start` / `--end`，支持 `2h`（= now-2h）、`2026-11-13T22:00`（本地时区）、带时区的 ISO 时间、Unix 时间戳。完整参数见 `grafana_connector.py --help`。

没装扩展时的兜底：在 DevTools → Network → 任一 grafana 请求 → Request Headers 里复制 `Cookie` 的值，然后：

```bash
pbpaste | ~/.agents/skills/grafana-connector/scripts/grafana_connector.py set-cookie --grafana <name>
```

## 安全

- 服务端只监听 `127.0.0.1`，并校验 `Host` 头（防 DNS rebinding）。
- `POST /cookies` 只接受来自 Chrome 扩展的请求（`Origin: chrome-extension://…`），网页无法往服务端塞 cookie。
- 扩展默认只有访问 `127.0.0.1:7788` 的权限；读取 Grafana 域名 cookie 的权限要你在小窗里点按钮授予，而且只授予已配置实例的域名。cookie 只会发给 `127.0.0.1:7788`。
- cookie 等同于你的 Grafana 登录凭证，不要贴到聊天、issue 或日志里。

## 从 loki-logs 升级

这个项目以前叫 `loki-logs`（脚本 `loki_logs.py`、MCP server `loki`）。升级后：

- 配置目录 `~/.config/loki_logs` 第一次运行时自动搬到 `~/.config/grafana-connector`（配置、cookie 都保留）；`LOKI_LOGS_*` 环境变量仍然有效。
- MCP 工具 `loki_health` / `loki_datasources` 改名为 `grafana_health` / `grafana_datasources`，其它工具名不变。
- 需要手动做的：
  ```bash
  mv ~/.agents/skills/loki-logs ~/.agents/skills/grafana-connector   # 如果是软链接，改成指向新目录
  ```
  ```bash
  rm ~/.claude/skills/loki-logs && ln -s ~/.agents/skills/grafana-connector ~/.claude/skills/grafana-connector
  ```
  ```bash
  claude mcp remove -s user loki && claude mcp add -s user grafana -- python3 ~/.agents/skills/grafana-connector/scripts/grafana_connector.py mcp
  ```
  Codex 把 `~/.codex/config.toml` 里的 `[mcp_servers.loki]` 改成 `[mcp_servers.grafana]`，路径换成新的脚本。
  ```bash
  pkill -f "loki_logs.py serve"
  ```
  Chrome 扩展目录也改名了：在 `chrome://extensions` 删掉旧的 loki_logs cookie sync，重新加载 `chrome_ext/grafana_connector_cookie_sync`，再点图标授权一次。

## 更新

```bash
git -C ~/.agents/skills/grafana-connector pull
```

更新后如果扩展有改动，在 `chrome://extensions` 里点刷新；改了服务端代码时，执行 `pkill -f "grafana_connector.py serve"` 停掉即可，下次调用会自动用新代码拉起（只改配置不用重启）。
