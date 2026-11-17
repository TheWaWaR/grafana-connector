---
name: grafana-connector
description: Query and troubleshoot service logs in Loki (LogQL) and Prometheus-type metrics (PromQL) through one or more team Grafana instances via a local grafana-connector server, using the grafana MCP tools or a bundled CLI. Use when the user asks to check, search, or investigate a service's logs, errors, trace_ids, or behavior in the test/stage/prod environment, wants to run or debug a PromQL / Grafana dashboard query, or mentions Loki, Grafana logs, LogQL or PromQL.
---

# Grafana 查询：Loki 日志 / PromQL 指标

所有查询都通过本机的 grafana-connector 服务端（127.0.0.1:7788）走 Grafana，登录态由服务端持有，你不需要也不应该自己登录 Grafana。服务端没开时，CLI / MCP 会自动在后台拉起它。

可以配置多个 Grafana 实例（`health` 会列出每个实例的名字、地址和登录状态）。所有命令都可以用 `--grafana <name>`（MCP 参数 `grafana`）选实例，不写就用默认实例；名字、完整 URL 或名字的唯一前缀都行。用户给的 Grafana 地址还没配置时，执行 `<skill-dir>/scripts/grafana_connector.py config add <name> <url>`（服务端自动加载，不用重启），再让用户在 Chrome 里点扩展图标授权并登录该 Grafana。

两种调用方式，能力完全一样：

- **MCP 工具**（优先）：如果当前环境里有 `grafana` MCP 工具（`grafana_health`、`grafana_datasources`、`loki_labels`、`loki_label_values`、`loki_series`、`loki_query`、`prom_query`），直接用它们，参数和下面的 CLI 一一对应（`--grafana` → `grafana`，`--ds` → `ds`，`--max-line-len` → `max_line_len`，`--asc` → `direction="forward"`，`--instant` → `instant=true`）。在 Codex 这类 shell 默认处于无网络沙箱的环境里，只能用 MCP。
- **CLI**：`<skill-dir>/scripts/grafana_connector.py`，`<skill-dir>` 是本 SKILL.md 所在的目录。调用时写完整的绝对路径；不要用 shell 变量包装命令（zsh 不会对 `$VAR` 分词）。

```bash
<skill-dir>/scripts/grafana_connector.py health   # 或 MCP 的 grafana_health：确认服务端在跑、已登录
```

## 先处理 health 的结果

- `ok: [<name>] ... as <email>`：这个实例可以查了（每个实例一行，`FAIL:` 的实例按下面的错误处理）。
- `failed to start grafana-connector server`：自动启动失败，错误里带了日志尾部。常见原因是缺 `requests`，让用户按提示执行 `python3 -m pip install requests`。
- `cannot reach grafana-connector server ... sandbox`：当前 shell 没有网络权限，改用 MCP 工具；没有 MCP 工具就告诉用户需要注册 MCP（见仓库 README）。
- `Grafana URL is not configured`：还没配置 Grafana，问用户要地址，然后执行 `<skill-dir>/scripts/grafana_connector.py config add <name> <url> --default`（写入 `~/.config/grafana-connector/config.toml`），再重新 health。
- `HTTP 401` / `login expired`：登录态失效，**停下来让用户在 Chrome 里打开错误信息里给出的 Grafana 地址重新登录**（cookie 同步扩展会自动推送给服务端），用户确认后再 health。不要尝试其它登录方式，也不要让用户把 cookie 贴到聊天里。
- `no cookies yet`：服务端从没收到过 cookie，多半是扩展还没装，按下面的步骤引导用户。

### 首次设置 / cookie 同步排障（需要用户在 Chrome 里操作，你做不了）

0. 配置 Grafana 实例：`<skill-dir>/scripts/grafana_connector.py config add <name> <url> [--default]`。
1. 安装扩展：`chrome://extensions` → 打开右上角"开发者模式" → "加载已解压的扩展程序" → 选 `<skill-dir>/chrome_ext/grafana_connector_cookie_sync`（告诉用户展开后的绝对路径）。
2. 点工具栏上的扩展图标，在弹出的小窗里点「授权并推送所有 Grafana 的 cookie」，授权扩展读取这些 Grafana 域名的 cookie（新加实例后要再点一次）。
3. 在 Chrome 里打开 Grafana 并登录，扩展会自动推送；之后想手动推一次也是点扩展图标里的按钮。
4. 扩展图标角标含义（点开小窗可看详细信息）：无角标 = 推送成功；`auth` = 还没授权读取 Grafana 域名的 cookie；`off` = 连不上服务端（先确认服务端在跑）；`?` = Chrome 里没有 grafana 的 cookie（还没登录）；`!` / `err` = 服务端拒绝或出错。
5. 刚装上或改过扩展后推送没反应时，在 `chrome://extensions` 里点扩展卡片上的刷新按钮重新加载。
6. 兜底：用户在 DevTools → Network → 任一 grafana 请求 → Request Headers 里复制 `Cookie` 的值，自己在终端执行 `pbpaste | <skill-dir>/scripts/grafana_connector.py set-cookie --grafana <name>`。

## 先确认要查什么

动手前确认这几项，用户没说清楚的**问用户，不要猜**：

- **服务**：用哪个 label 定位（通常是 `app`，也可能是 `namespace` / `service_name` / `container`）。用户只给了模糊的名字时，用 `values app` 找候选，有多个匹配就列出来让用户选。不要在没有服务限定的情况下扫整个 namespace。
- **环境（数据源）**：**没有默认值**，所有查询都必须带 `--ds`。用户没说查哪个环境时，用 `grafana_connector.py ds` 列出数据源让用户选，不要自己挑一个。回复里写明查的是哪个环境，查生产时要特别说明。
- **时间范围**：用户没说时默认最近 1h，并在回复里写明。
- **关注点**：报错、某个 trace_id / 请求 id、某类事件、延迟等。

## 命令

| 命令 | 作用 |
|---|---|
| `grafana_connector.py ds [--type prometheus\|all]` | 列出数据源（默认只列 Loki） |
| `grafana_connector.py labels --ds <ds> --since 1h` | 有数据的 label 名（只有这些能写在 `{}` 里） |
| `grafana_connector.py values app --ds <ds> --since 1h` | label 取值；`--query '{namespace="<ns>"}'` 限定范围 |
| `grafana_connector.py series '{app="<app>"}' --ds <ds>` | 匹配的 stream（看有哪些 pod / region 等） |
| `grafana_connector.py query '<LogQL>' --ds <ds> [opts]` | 查日志或指标 |
| `grafana_connector.py prom '<PromQL>' --ds <ds> [--instant] [--step 5m]` | Prometheus 类数据源的 PromQL（走 Grafana `/api/ds/query`，和 dashboard 一样） |

通用参数：`--grafana`（选 Grafana 实例，默认实例可省略）、`--ds`（必填；完整名字、uid 或唯一前缀都行，如 `--ds prod`、`--ds test`）、`--since 30m`（默认 1h）、`--start` / `--end`（`2h` = now-2h、ISO 时间如 `2026-01-02T15:00`（本地时区）或带 `+08:00`/`Z`、Unix 时间戳）、`--json`。
`query` 额外参数：`--limit N`（默认 100，最大 5000）、`--asc`（从旧到新，默认最新在前）、`--step 5m`（指标查询）、`--max-line-len 300`（截断长行，省 token）。

输出格式：首行是数据源和时间范围；`# N lines (hit limit ...)` 表示被 limit 截断了；`# common labels` 是所有行共有的标签，每行只额外显示有差异的标签（如 pod）。

## 排查流程

下面的 `<ds>`、`<app>`、`<field>`、`<value>` 都要换成用户指定的环境、服务和实际日志里的字段。

1. **看格式**：先拉几行，确认日志是 logfmt（`key=value ...`）、JSON 还是纯文本，以及有哪些字段：
   ```bash
   grafana_connector.py query '{app="<app>"}' --ds <ds> --since 15m --limit 5
   ```
2. **先看量，再看行**：用指标查询了解分布，避免拉一大堆原始日志：
   ```bash
   grafana_connector.py query 'sum by (detected_level) (count_over_time({app="<app>"} [5m]))' --ds <ds> --since 2h --step 5m
   grafana_connector.py query 'sum by (<field>) (count_over_time({app="<app>"} | logfmt [5m]))' --ds <ds> --since 2h --step 5m
   ```
3. **过滤到具体日志**：小 limit、窄时间窗，加 `--max-line-len`：
   ```bash
   grafana_connector.py query '{app="<app>"} | detected_level=~"warn|error"' --ds <ds> --since 1h --limit 50 --max-line-len 400
   grafana_connector.py query '{app="<app>"} |= "<keyword>"' --ds <ds> --since 30m --limit 30
   grafana_connector.py query '{app="<app>"} | logfmt | <field>="<value>"' --ds <ds> --since 1h
   grafana_connector.py query '{app="<app>"} | json | <field>="<value>"' --ds <ds> --since 1h
   grafana_connector.py query '{app="<app>"} |= "<trace_id>"' --ds <ds> --since 1d    # 按 trace_id / 请求 id 串起整条链路
   ```
4. **看上下文**：找到关键行后，用它的时间戳前后几十秒 `--start ... --end ... --asc` 拉同一 pod 的日志。
5. 结论里引用具体日志行（时间 + 关键字段），并说明查询的服务、数据源和时间范围。

## LogQL 速查

- stream 选择器必须至少有一个非空匹配，只能用索引标签：`{app="<app>"}`、`{namespace="<ns>", app=~"<prefix>-.*"}`
- `detected_level`（info/warn/error，不依赖日志格式）是 structured metadata，要放在管道里过滤：`{app="<app>"} | detected_level=~"warn|error"`。如果某服务的 `detected_level` 全是 `unknown`，改用解析出的级别字段（`| logfmt | level=~"(?i)warn|error"`）或行过滤（`|~ "(?i)error"`）
- 行过滤：`|= "a"` 包含，`!= "a"` 不包含，`|~ "re"` 正则，`!~ "re"`
- 解析：logfmt 用 `| logfmt`，JSON 用 `| json`，之后可按字段过滤：`| level="ERROR"`、`| <field> > 500`（数值比较）
- 指标：`count_over_time(<log query> [5m])`、`rate(...)`、`sum by (<label>) (...)`、`topk(10, ...)`、`quantile_over_time(0.99, {app="<app>"} | logfmt | unwrap <numeric_field> [5m])`
- 查询报错（`parse error ...`）会原样返回，按提示修正 LogQL 即可。

## 注意

- 时间范围从小开始（15m–1h），确有需要再放大；大范围先用指标查询。
- 不要把大段原始日志原样贴给用户，提炼关键行和统计结果。

## PromQL

- 先用 `ds --type prometheus` 找数据源；排查 dashboard 时用 dashboard 里同一个数据源。
- 看当前值用 `--instant`；看趋势时控制点数（时间范围 / step），例如 6h 配 `--step 10m`。
- 把复杂表达式拆开逐段查（各个子表达式、`or` / `on() group_left` 两侧），定位是哪一步结果不对。
