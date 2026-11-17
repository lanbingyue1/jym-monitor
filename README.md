# 交易猫新品监控（忍者必须死3 · 成品号）

定时监控交易猫（jiaoyimao.com）「忍者必须死3 → 成品号」分类，发现新上架商品后通过飞书群机器人和/或邮件推送。

运行在 GitHub Actions 上：**公开仓库 + 公开 `state` 分支**保存运行状态，零成本、无需自备服务器。公开仓库的 GitHub 托管 runner 分钟数免费不限；私有仓库每月只有 2000 分钟，不足以支撑每 5 分钟一次的调度。

> 本项目只负责发现新品并提醒：没有 Web 界面、不需要登录交易猫、不会自动下单或购买。

## 工作方式

一句话架构：GitHub Actions 定时触发 `python -m monitor`，站点适配器抓取并解析搜索结果页，流水线按「首次运行锚点 + 已见商品 ID」判断新品，经规则过滤后批量通知，最后通过 GitHub Contents API 把 `state.json` 写回 `state` 分支。

- **抓取**：优先普通 HTTP；被反爬页拦截时，降级到 Playwright 驱动的 Google Chrome（`channel="chrome"`）。GitHub 的 Ubuntu runner 已预装 Chrome，因此 workflow 不需要 `playwright install` 步骤。
- **翻页**：从第 1 页开始，遇到已见商品 ID 或达到 `max_pages`（默认 3）时停止，避免调度延迟期间漏掉新品。
- **首次运行**：登记当前存量商品，只发一条「监控已启动，当前 N 条商品」，不逐条推送。
- **判新规则**：未见过，且发布时间不早于首次运行时间（`anchor_at`）。这样被擦亮/重新推广的旧商品不会被误报；缺少发布时间的商品会保守提醒。
- **通知失败**：某个批次所有渠道都失败时，本次不会为该站点写入已见状态，下一轮会重试（至少一次语义，可能产生少量重复）。
- **状态提交**：有新商品 ID 或距上次提交超过 24 小时（心跳）时提交一次 `state.json`。每日心跳同时避免仓库因 60 天无活动导致定时任务被 GitHub 停用。
- **调度**：GitHub 的 `schedule` 是 best-effort，`*/5` 是最小间隔；5–15 分钟是目标而非硬性 SLA，负载高时可能延迟或跳过，下一轮会通过翻页补上。

## 项目结构

```
monitor/                        抓取、解析、判新、过滤、通知、状态读写
tests/                          离线单元测试（含真实页面 fixture）
config.yaml                     监控地址、过滤规则、通知渠道
.github/workflows/monitor.yml   GitHub Actions 定时任务
requirements.txt                Python 依赖
```

## 本地测试

在项目根目录执行（CI 使用 Python 3.12）：

```bash
pip install -r requirements.txt
python -m pytest tests -v
```

测试完全离线，使用保存的页面 fixture，不需要网络、Chrome 或任何密钥。

## 本地试跑（dry run）

```bash
python -m monitor --config config.yaml --dry-run
```

- 会真实抓取并解析，打印计数和「将要推送」的条目；**不发送通知、不写状态、不需要任何 Secrets 或 `GITHUB_TOKEN`**。
- 状态从环境变量 `STATE_FILE` 指定的本地文件读取，默认 `state.local.json`（已加入 `.gitignore`）。文件不存在时按「首次运行」处理：只统计存量，不会列出待推送条目。若要预览待推送列表，可把 `state` 分支的 `state.json` 复制为本地 `state.local.json`（不要提交）。
- 本地网络通常会被交易猫的反爬页拦截，这是预期行为；`--dry-run` 主要用于有可用网络时的解析与判新逻辑检查。抓取失败会记录在输出的 `errors` 中，命令仍以 0 退出（配置或状态文件本身读不了时才会返回 1）。正式的抓取链路以 GitHub Actions 的美国出口为准。
- `--config` 默认就是 `config.yaml`，可省略。

## 部署到 GitHub Actions

### 1. 创建公开仓库并推送

在 GitHub 新建一个**公开**仓库（例如 `jym-monitor`，不要勾选初始化 README），然后在本地项目根目录推送：

```bash
git remote add origin git@github.com:<你的用户名>/jym-monitor.git
git push -u origin main
```

如果默认分支不是 `main`，把最后一行换成实际分支名。仓库设为公开是刻意的取舍：换免费不限量的 Actions 分钟数；仓库中没有任何密钥，密钥只放 GitHub Secrets。

### 2. 创建 `state` 分支

在默认分支上执行：

```bash
git checkout --orphan state
echo '{"version": 1, "sites": {}, "notifiers": {}}' > state.json
git add state.json && git commit -m "init state"
git push origin state
git checkout main
```

注意：

- 最后一行 `git checkout main` 中的 `main` 是仓库默认分支；如果默认分支是 `master` 等，请替换为实际名称。
- 也可以在 GitHub 网页上直接新建 `state` 分支并创建 `state.json`，效果相同。
- `state.json` 只应存在于 `state` 分支。回到默认分支后，本地可能残留未跟踪的 `state.json`，请删除，不要提交到默认分支。
- 在 Windows PowerShell 中，`echo ... > state.json` 在旧版 PowerShell 下可能写成 UTF-16 编码；若如此，请改用编辑器保存为 UTF-8，或使用 PowerShell 7 / Git Bash。

### 3. 配置 Secrets

进入仓库 **Settings → Secrets and variables → Actions → New repository secret**，逐个添加。名称必须完全一致（区分大小写）。

| Secret | 何时需要 | 说明 |
| --- | --- | --- |
| `FEISHU_WEBHOOK` | `notify.channels` 含 `feishu` 时必填（当前默认启用） | 飞书群自定义机器人的 Webhook 地址 |
| `SMTP_HOST` | `notify.channels` 含 `email` 时必填 | SMTP 服务器地址，例如 QQ 邮箱为 `smtp.qq.com` |
| `SMTP_PORT` | `notify.channels` 含 `email` 时必填 | SMTP 端口；465 走 SSL，其它端口（如 587）走 STARTTLS |
| `SMTP_USER` | `notify.channels` 含 `email` 时必填 | 发件邮箱账号 |
| `SMTP_PASS` | `notify.channels` 含 `email` 时必填 | 邮箱密码或授权码（QQ 邮箱等通常必须使用授权码，而不是登录密码） |
| `MAIL_TO` | `notify.channels` 含 `email` 时必填 | 收件地址；多个地址用英文逗号分隔 |
| `HEALTHCHECK_PING_URL` | 可选 | 外部存活检测地址；仅在本次运行没有任何错误时 ping 一次 |
| `GITHUB_TOKEN` | 无需手动配置 | Actions 自动注入 `secrets.GITHUB_TOKEN`，用于通过 Contents API 读写 `state` 分支 |
| `GITHUB_REPOSITORY` | 无需手动配置 | workflow 自动传入 `github.repository`（`owner/repo`） |
| `STATE_BRANCH` | 无需手动配置 | workflow 固定为 `state`；本地手动真实运行时可用环境变量覆盖，默认也是 `state` |

配置 `HEALTHCHECK_PING_URL` 后，每次**没有任何错误**的运行结束时会 GET 一次该地址；外部服务（如 healthchecks.io）在约定时间内没收到 ping 就告警，可发现「整个 workflow 停跑」这种内部渠道无法告警的情况。ping 失败只会打印一行提示，不会让本次运行失败。

邮件渠道默认关闭。要启用邮件，编辑 `config.yaml`：

```yaml
notify:
  channels: [feishu, email]   # 飞书和邮件同时开启
  # channels: [email]         # 只开邮件
```

飞书和邮件可同时开启；任一渠道失败不影响其它渠道。所有已启用渠道都失败的批次会留给下一轮重试。

#### 飞书群机器人

在接收通知的群聊中：**群设置 → 群机器人 → 添加机器人 → 自定义机器人**，设置名称后复制 Webhook 地址填入 `FEISHU_WEBHOOK`。

安全设置保持默认即可；**不要启用「签名校验」**，当前实现没有做签名计算，启用后消息会被飞书拒绝。

#### 本地真实运行（可选）

设置 `GITHUB_TOKEN`、`GITHUB_REPOSITORY` 以及对应渠道的环境变量后执行 `python -m monitor --config config.yaml`。这会真实发送通知并写 `state` 分支；本地抓取通常会被反爬拦截，日常请优先使用 Actions 的 `Run workflow`，避免与定时任务并发。

### 4. 首次验证

打开仓库 **Actions → 左侧选择 `monitor` → Run workflow**（选择默认分支）→ 点击运行。

预期结果：

- workflow 运行成功（绿色），运行摘要（Step summary）显示抓取、新增、通知、耗时等计数，其中「首次运行」为「是」。
- 飞书和/或邮件收到一条「**监控已启动，当前 N 条商品**」。N 是本次抓取到的存量商品数（受 `max_pages` 限制），首次运行不会逐条推送存量。
- `state` 分支出现一次 `chore: update monitor state` 提交，`state.json` 中写入 `sites.jiaoyimao.anchor_at` 和 `seen`。

如果第一次进入 Actions 时提示启用 workflow，确认启用后再运行。

### 5. 二次验证

1. 在 GitHub 打开 `state` 分支的 `state.json`，找到 `sites.jiaoyimao.seen` 中最新的一条商品（最大的 `goodsId`），删除该键值对并提交（网页编辑即可）。
2. 等下一轮定时运行（通常 5–15 分钟），或手动执行一次 `Run workflow`。
3. 预期：收到该商品的推送**恰好一次**；之后再运行不会重复（该 ID 已被重新写入 `seen`）。

注意事项：

- 请删除最新的一条，确保它仍在列表前几页；删除过旧的商品可能已经滚出抓取范围，不会被看到。
- 当前 `config.yaml` 的过滤器为空，所以所有商品都会推送；如果你启用了过滤且该商品不满足条件，就不会收到推送。
- 该商品的发布时间必须不早于首次运行的 `anchor_at`，否则按判新规则不会被推送；最新商品通常都满足。

## 配置说明

`config.yaml` 的主要配置项：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `sites.jiaoyimao.urls` | 成品号分类地址 | 监控地址，可配置多个 |
| `sites.jiaoyimao.max_pages` | `3` | 每轮最多翻几页 |
| `sites.jiaoyimao.filters.price_min` / `price_max` | `null` | 价格区间（元）；`null` 表示不限。设置区间后价格未知的商品会被过滤 |
| `sites.jiaoyimao.filters.platforms` | `[]` | 平台白名单（如「安卓」「iOS」「双端」）；空表示全部 |
| `sites.jiaoyimao.filters.servers` | `[]` | 服务器白名单；空表示全部 |
| `sites.jiaoyimao.filters.include_keywords` | `[]` | 必须命中的关键词，匹配标题、描述和卖点；空表示不过滤 |
| `sites.jiaoyimao.filters.exclude_keywords` | `[]` | 排除关键词，优先级高于包含关键词 |
| `notify.channels` | `[feishu]` | 通知渠道，可组合 `feishu` / `email` |
| `notify.batch_size` | `10` | 单条通知最多包含几件商品，超出会拆批 |
| `alerts.consecutive_failures_before_alert` | `3` | 连续失败多少次后发一次异常告警 |
| `alerts.min_alert_interval_minutes` | `180` | 同类告警的最小间隔（分钟） |
| `bootstrap.notify_on_first_run` | `true` | 首次运行是否发送「监控已启动」消息 |

调度频率在 `.github/workflows/monitor.yml` 的 `cron` 中修改。GitHub 定时任务的最小间隔是 5 分钟；workflow 同时保留了 `workflow_dispatch` 手动触发，单次超时 10 分钟，并启用 `concurrency` 防止重叠运行。workflow 已声明 `contents: write` 权限，用于通过 GitHub Contents API 回写 `state` 分支。

## 常见问题

**本地运行出现验证码页 / 反爬惩罚页（punish）**

这是预期行为。交易猫通常会拦截普通本地网络出口的请求，HTTP 和 Chrome 兜底都可能拿到反爬页。`--dry-run` 只适合在能正常访问时做解析、判新等逻辑检查；正式的监控链路由 GitHub Actions 的美国出口运行，请不要据此判断程序损坏。

**Actions 运行了，但没有收到通知**

- 先看 Actions 的运行日志和 Step summary，搜索「错误」或「notifier」；通知失败通常会在日志中明确写出渠道名和原因。
- 核对 Secret 名称是否完全一致。启用邮件时 `SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASS`、`MAIL_TO` **五项都要配置**，缺一项会在启动时直接失败。
- 确认 `config.yaml` 的 `notify.channels` 包含预期渠道；当前默认只有 `feishu`，邮件需要手动加 `email`。
- 飞书：确认机器人仍在群里、Webhook 未失效，并关闭「签名校验」。
- 邮件：确认使用的是邮箱授权码而非登录密码，并检查垃圾邮件目录。

**定时运行延迟或被跳过**

GitHub 的 `schedule` 是 best-effort：`*/5` 只是最小间隔，平台负载高时会延迟甚至跳过某一轮。5–15 分钟是目标，不是 SLA。漏掉一轮不会丢商品：下一轮会从第 1 页翻到已见 ID，覆盖延迟期间的增量。需要立即检查时可手动执行 `Run workflow`。

**定时任务在 60 天后被自动停用**

GitHub 会在仓库连续 60 天无活动后自动停用定时 workflow。监控每天至少写一次状态心跳提交（`chore: update monitor state`），正常情况下能维持仓库活跃、避免触发该限制。如果仍被停用（例如 Actions 被手动关闭、仓库长期暂停），进入 **Actions → monitor → Enable workflow** 并手动运行一次即可恢复。

## 费用与隐私

- 公开仓库使用 GitHub 托管的标准 runner，Actions 分钟数免费；通知渠道使用飞书自定义机器人和你自己的 SMTP 服务，均无额外费用。
- `state` 分支是公开的，但其中只有商品 ID、运行时间、失败计数等运行状态，不包含账号、Cookie、密钥或个人信息。
- 通知密钥保存在 GitHub Secrets 中，不会写进仓库或日志。请不要把 Webhook、邮箱授权码等敏感信息提交到代码或 `config.yaml`。
