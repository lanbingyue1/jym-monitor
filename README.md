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
- **详情补全**：只对即将推送的新商品抓一次详情页，补上「安全报告」（账号安全等级与各项认证/保障）。详情页抓取失败只记为警告，商品仍按搜索页信息推送。
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

在默认分支上执行。先确保工作区干净且代码已提交，因为 `git rm -rf .` 会删除工作区中的已跟踪文件（随后 `git checkout main` 会把它们恢复）：

```bash
git checkout --orphan state
git rm -rf .
echo '{"version": 1, "sites": {}, "notifiers": {}}' > state.json
git add state.json && git commit -m "init state"
git push origin state
git checkout main
```

注意：

- `git checkout --orphan state` 不会清空暂存区和工作区；必须用 `git rm -rf .` 把项目文件从 `state` 分支的提交中移除，否则 `state` 分支会包含整个项目。执行后 `state` 分支中只有 `state.json`。
- 最后一行 `git checkout main` 中的 `main` 是仓库默认分支；如果默认分支是 `master` 等，请替换为实际名称。
- 也可以在 GitHub 网页上直接新建 `state` 分支并创建 `state.json`。网页创建的分支会从默认分支带过来整个项目的文件，而不是只有 `state.json`；监控只读写 `state.json`，这些多余文件不影响运行，也可以在 `state` 分支中把它们删除。
- `state.json` 只存在于 `state` 分支；切回默认分支后它会被移除，默认分支不应提交 `state.json`。
- 在 Windows PowerShell 中，`echo ... > state.json` 在旧版 PowerShell 下可能写成 UTF-16 编码；若如此，请改用编辑器保存为 UTF-8，或使用 PowerShell 7 / Git Bash。

### 3. 配置 Secrets

进入仓库 **Settings → Secrets and variables → Actions → New repository secret**，逐个添加。名称必须完全一致（区分大小写）。

| Secret | 何时需要 | 说明 |
| --- | --- | --- |
| `FEISHU_WEBHOOK` | `notify.channels` 含 `feishu` 时必填（当前默认启用） | 飞书群自定义机器人的 Webhook 地址 |
| `FEISHU_APP_ID` | 可选，与 `FEISHU_APP_SECRET` 成对出现 | 自建飞书应用的 App ID；配置后图片直接显示在卡片里，否则只显示图片链接 |
| `FEISHU_APP_SECRET` | 可选，与 `FEISHU_APP_ID` 成对出现 | 自建飞书应用的 App Secret |
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

#### 让图片直接显示在卡片里（可选）

飞书卡片里的图片必须用 `img_key` 引用，而只有**自建应用**能上传图片并拿到 `img_key`，群机器人 Webhook 本身做不到。因此：

- **不配置应用**：卡片里每个商品附一行「图片：图1 图2 …」，点开即可查看，零额外配置。
- **配置应用**：图片（最多前 9 张）直接内联显示在卡片里，其余仍以链接给出。

配置步骤：

1. 打开 [飞书开放平台](https://open.feishu.cn/app)，创建一个**企业自建应用**（个人版飞书也可以先免费创建一个团队）。
2. **凭证与基础信息**：复制 `App ID` 和 `App Secret`。
3. **添加应用能力**：「添加应用能力」→ 启用「机器人」。飞书的上传图片接口要求应用具备机器人能力，否则返回 `400 / 234007`。
4. **权限管理**：添加「上传图片或文件资源」（`im:resource`），然后**创建版本并发布**（企业内自建应用需要管理员审核；你自己是管理员时直接通过）。
5. **启用应用**：确保应用处于「已启用」状态。
6. 在 GitHub 仓库添加 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET` 两个 Secrets。

应用不需要加入群聊，图片上传后由现有的群机器人 Webhook 引用。任一图片上传失败都会在 Actions 日志里打印「警告」，并自动降级为链接，不影响整条通知。

## 推送内容

每条新商品按以下顺序展示（缺失的字段会自动省略或显示「未知」）：

- **标题（可点击）**：完整标题，包含「【忍阶】…」和卖家写的 JYM 编号
- **价格 · 账号 · 平台 · 服务器**：例如 `¥50 · 账号：小米帐号 · 平台：安卓 · 服务器：和之国`
- **上架时间**：北京时间 `YYYY-MM-DD HH:MM`
- **特点**：搜索结果里的角色/皮肤等卖点标签
- **卖家说**：卖家填写的商品描述（可能为空）
- **安全报告**：账号安全等级（如 `SSS 100%`）以及「卖家信用认证 / 交易安全保障 / 售后安全保障」的各项状态
- **图片**：交易猫提供的验号截图与游戏截图（内联或链接，见上一节）

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

先理解判新规则：`is_new` 要求商品**不在 `seen` 中**，且发布时间**不早于 `anchor_at`**。程序用 `goodsId` 的前 13 位（毫秒时间戳）推导发布时间。首次运行会登记所有存量商品，它们的发布时间通常都早于首次运行写入的 `anchor_at`，所以直接删除其中任意一条通常不会触发推送。

动手修改前，先停用定时任务：**Actions → monitor → Disable workflow**。验证完成后再重新启用（Enable workflow）；否则一轮已经开始或正在排队的运行可能在你的手动编辑之后重新 PUT 它内存中的旧状态，把手动修改覆盖掉。

**做法 A：删除一条监控启动后上架的商品（推荐）**

1. 在 `state` 分支的 `state.json` 中查看 `sites.jiaoyimao.anchor_at`，从 `sites.jiaoyimao.seen` 找一条 `goodsId` 前 13 位时间戳**晚于** `anchor_at` 的商品（即监控启动后真实上架、并已推送过的商品）。如果还没有这样的商品，先等监控跑出至少一条新商品，或使用做法 B。
2. 删除该商品的 `goodsId` 键值对并提交（网页编辑即可）。
3. 等下一轮定时运行（通常 5–15 分钟），或手动执行一次 `Run workflow`。
4. 预期：收到该商品的推送**恰好一次**；之后再运行不会重复（该 ID 已被重新写入 `seen`）。

**做法 B：立即验证（临时调整 `anchor_at`）**

1. 从 `seen` 中选一条最新、且仍在前几页的商品，记下其 `goodsId`。
2. 把 `goodsId` 前 13 位当作毫秒时间戳，将 `state.json` 中的 `anchor_at` 临时改到该时间之前（例如提前 1 分钟），并从 `seen` 中删除该 `goodsId` 的键值对，提交。
   - 换算示例（把数字换成你的前 13 位）：`python -c "import datetime; print(datetime.datetime.fromtimestamp(1793434810016/1000, datetime.timezone.utc).isoformat())"`
3. 等下一轮或手动执行一次 `Run workflow`；预期该商品推送**恰好一次**，之后再运行不重复。
4. 验证完成后把 `anchor_at` 恢复为原值，避免改变后续的判新起点。

注意事项：

- 被删除的商品必须仍在抓取范围内（列表前几页）；删除过旧的商品可能已经滚出范围，不会被看到。
- 当前 `config.yaml` 的过滤器为空，所以所有商品都会推送；如果你启用了过滤且该商品不满足条件，就不会收到推送。
- 做法 B 临时提前 `anchor_at` 后，如果还有其它未登记的商品，也可能一并推送；首次运行的商品都已在 `seen` 中，正常只会推送你删除的那一条。

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

- 先看 Actions 的运行日志和 Step summary，搜索「错误」「警告」或「notifier」；通知失败通常会在日志中明确写出渠道名和原因。详情页补全失败只记「警告」，不影响推送和运行状态。
- 卡片里图片显示为链接而不是图片：说明没有配置 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`，或应用未发布、缺少 `im:resource` 权限。日志中的「警告: 飞书图片 … 上传失败」会写明原因。
- 日志出现「`feishu image upload returned HTTP 400 (code 234007`」：应用没有启用「机器人」能力。开放平台 → 应用 →「添加应用能力」启用机器人，然后重新发布版本。
- 日志出现「`feishu app auth returned code 21001`」：这是飞书侧的安全策略拦截，不是代码或凭据格式问题。检查应用是否已发布并启用、权限是否审核通过、安全设置里是否启用了 IP 白名单（Actions 出口 IP 不固定）；企业账号还需管理员在数据安全策略中放行该应用。此情况会自动降级为图片链接，通知本身不受影响。
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
- 图片优先从交易猫 CDN 直连下载；GitHub 的海外出口偶尔连不上时，会回退到公共图片代理 `wsrv.nl` 代取。传给代理的只有公开的商品图片 URL，不包含任何密钥或个人信息。
