# 交易猫新品监控告警器 设计文档

日期：2026-11-16
状态：待批准

## 1. 背景与意图

用户在交易猫（jiaoyimao.com）上关注「忍者必须死3」的账号（成品号）交易，希望在有人发布新商品时尽快得到通知（标题、价格、链接），以便了解行情、有空时再看。第一版**不做筛选**：该分类下的所有新品都提醒；筛选规则后续通过配置文件增加。

用户明确的三条约束：

1. 提醒延迟 5–15 分钟可以接受（用于了解行情，不是秒抢）。
2. 必须免费，且不依赖用户自己的电脑常开。
3. 程序要有扩展口：通知渠道可插拔；站点抓取可插拔，以后加别的网站只写适配器。

## 2. 目标 / 非目标 / 反目标

### 目标

- 每 5–10 分钟检查一次交易猫「忍者必须死3 → 成品号」分类，发现未见过的商品就推送。
- 通知渠道可插拔：飞书群机器人 + 邮箱，配置开关。
- 站点适配器可插拔：新增网站 = 新增一个适配器模块 + 一段配置，不改核心流程。
- 去重可靠：同一商品只提醒一次；首次运行只登记存量，不轰炸。
- 运行状态可观测：抓取失败、被风控、连续异常时主动告警，而不是静默失败。
- 零服务器成本：跑在 GitHub Actions 免费额度内。

### 非目标（第一版不做）

- 不做自动购买、自动议价、自动联系卖家。
- 不登录交易猫账号（公开列表页已足够拿到所需数据）。
- 不破解/自动通过滑块验证（只做降级与告警）。
- 不保证秒级延迟。
- 不做网页界面、不做多用户。
- 不接入交易猫以外的站点，但接口按可扩展设计。

### 反目标（技术上能跑也算失败）

- 频繁误报或重复推送，导致用户不再看通知。
- 被风控拦截后静默失败，用户以为在监控其实早就没数据。
- 需要每隔几天维护一次选择器或参数。

## 3. 已验证的事实（2026-11-16 实测）

这些事实决定了下面的设计，不是假设：

| 验证项 | 结果 |
|---|---|
| 裸 HTTP（curl / requests，本地网络） | 被阿里 x5sec 风控拦截，返回滑块页 |
| 带浏览器 Cookie 的 requests（本地） | 仍被拦截 → Cookie 不是关键，客户端指纹才是 |
| 真实 Chrome（有头/无头，无 Cookie） | 正常加载，无验证码 |
| GitHub Actions（美国 IP 128.85.45.91）跑 requests | HTTP 200，无风控页，数据完整 |
| GitHub Actions 跑无头 Chrome | 正常，拿到 16 条商品 |
| 列表页数据来源 | 服务端渲染，`window.__INITIAL_DATA__` 内嵌完整数据 |
| 「最新发布」排序 | URL 参数 `rId=107`；列表中会混入推广/擦亮的老商品 |
| 商品唯一标识 | `goodsId`；前 13 位是上架时间的毫秒时间戳 |
| 每页条数 | 16 条 |

关键结论：

- GitHub Actions 的出口 IP 目前是干净的，**纯 HTTP 即可抓取**；浏览器只作为风控兜底。
- 判断「新品」必须靠 `goodsId` 去重，不能依赖排序位置。
- 本地开发环境反而会被风控拦截，开发时用保存的 HTML 做离线解析。

## 4. 方案选型

| 方案 | 结论 |
|---|---|
| **A. GitHub Actions + Python（公开仓库）** | **采用**。免费、无需自有机器、已验证可访问、部署最简单。 |
| B. Cloudflare Workers + Cron + KV | 不采用：出口 IP 同样在海外且不可控，无法运行真实浏览器兜底；中国网络访问 Cloudflare 不稳定。 |
| C. 国内免费云函数（阿里云 FC / 腾讯云 SCF） | 作为备选：有国内 IP，但需要实名与额外配置，且不再是「GitHub 免费服务器」。若 A 的 IP 突然被风控长期封禁，再迁移到 C，核心代码不变。 |

A 方案唯一的硬约束：**私有仓库每月只有 2000 分钟**，5 分钟一次（约 8640 分钟/月）不够；**公开仓库不限分钟**。因此正式仓库设为公开；代码中无敏感信息，通知 Webhook/邮箱密码放 GitHub Secrets，状态放独立分支。

用户已确认接受仓库与状态公开：`state` 分支中的已见商品 ID 与每次运行时间对外可见；其中不含密码、邮箱地址或 Webhook。

## 5. 架构总览

```
GitHub Actions（cron */5，workflow_dispatch 手动）
  └─ python -m monitor
       ├─ config.yaml                 # 站点 URL、开关、通知渠道
       ├─ state（state 分支 state.json）# 已见商品 ID、运行状态
       ├─ sites/
       │    └─ jiaoyimao.py           # SiteAdapter 实现：抓取 + 解析
       ├─ pipeline.py                 # 去重 → 过滤 → 通知 → 落状态
       └─ notify/
            ├─ feishu.py              # 飞书群机器人 Webhook
            └─ email.py               # SMTP
```

数据流：

1. 读取 `config.yaml` 与状态文件。
2. 对每个启用的站点调用适配器，拿到当前列表（`list[Listing]`）。
3. 与已见 `goodsId` 集合求差。
4. 走规则过滤（第一版为空，即全部通过）。
5. 批量推送新商品。
6. 推送成功后，把本次见到的所有 ID 写回状态；写入失败则下次重试（至少一次语义）。
7. 任何环节失败都按策略重试/降级/告警。

## 6. 组件设计

### 6.1 数据模型

```python
@dataclass(frozen=True)
class Listing:
    site: str                    # "jiaoyimao"
    item_id: str                 # goodsId
    title: str
    price: int | None            # 元
    url: str                     # 商品详情页
    published_at: datetime | None  # 由 goodsId 前缀推导
    extra: dict                  # server / platform / sell_points / category
```

### 6.2 站点适配器接口

```python
class SiteAdapter(Protocol):
    site_id: str
    def fetch(self) -> list[Listing]: ...
```

- 适配器只负责「取数 + 归一化」，不关心通知、状态、规则。
- 新增站点：在 `sites/` 下加模块，在 `sites/__init__.py` 注册，`config.yaml` 增加对应配置块。核心流程零改动。
- 第一版只实现 `jiaoyimao`。

### 6.3 交易猫适配器

- 取数：`requests` GET 配置中的列表页 URL，带真实浏览器请求头（UA、Accept-Language、Referer）。
- 风控判定：响应中出现 `x5secdata`、或跳转到 `/punish`、或缺少 `__INITIAL_DATA__`。
- 降级链：requests 失败 → 间隔 3 秒重试一次 → 仍失败则用 Playwright 无头 Chrome（系统 Chrome，`headless=True`，去除 `navigator.webdriver`）打开同一 URL，等待 6 秒后解析。
- 解析：
  - 定位 `window.__INITIAL_DATA__`。
  - 该对象是 JS 字面量而非严格 JSON（含 `undefined`），解析前用正则把 `:undefined` 替换为 `:null`，再用 `json.JSONDecoder().raw_decode` 截取第一个完整对象。
  - `pageInitialProps.initialData.goodsListData.goodsList` 中 `type == 8` 的 `data` 即商品；`type == 14` 是安全提示条，忽略。
  - 字段映射：`goodsId`、`price`、`title`、`detailUrl`（缺失时用 `https://www.jiaoyimao.com/jg{gameId}/{goodsId}.html`）、`serverName`、`newPlatformName`、`sellPoints`。
  - `published_at`：`goodsId[:13]` 按毫秒时间戳解析，并校验在合理区间（防止规则变化导致误判），非法则为 `None`。
- 分页：**向更早的页翻，直到遇到已见过的 `goodsId` 或达到 `max_pages`（默认 3）**。页码通过 URL 参数 `page=N` 拼接；每页之间间隔 1–2 秒。这样两次运行之间即使新增超过 16 条也不会漏；若整页都是新品（没有已见 ID），继续翻下一页。达到 `max_pages` 时整页仍全是新品，则记一次「新增量异常」告警（理论上该分类流速远低于此）。
- 监控 URL（第一版）：
  `https://www.jiaoyimao.com/jg1008016/f2161571-c2161572/o110/?rId=107&searchCondition={"filter_low_quality":{"conditionList":["1"]}}&enforcePlat=2&newPage=true`
  （`enforcePlat=2` 为安卓；以后要加 iOS/双端，只需在 `urls` 里追加对应 URL。）
- 注意：`rId=107`（最新发布）排序里仍会混入推广/擦亮的老商品，**不要**依赖顺序判断新品。

### 6.4 配置

`config.yaml`（配置随代码提交，公开仓库可见；不放密钥）：

```yaml
sites:
  jiaoyimao:
    enabled: true
    urls:
      - https://www.jiaoyimao.com/jg1008016/f2161571-c2161572/o110/?rId=107&searchCondition=...&enforcePlat=2&newPage=true
    max_pages: 1
    filters:                # 第一版全部为空 = 全量提醒
      price_min: null
      price_max: null
      platforms: []         # 例如 ["安卓", "iOS", "双端"]；空 = 全部
      servers: []
      include_keywords: []  # 匹配 title + description + sellPoints
      exclude_keywords: []
notify:
  channels: [feishu]        # 可选 feishu / email，可同时开启
  batch_size: 10
alerts:
  consecutive_failures_before_alert: 3
  min_alert_interval_minutes: 180
bootstrap:
  notify_on_first_run: true # 首次运行只发「监控已启动，当前 N 条」，不逐条推送
```

### 6.5 去重与状态

状态文件（`state` 分支上的 `state.json`）：

```json
{
  "version": 1,
  "last_commit_at": "2026-11-16T15:40:00Z",
  "sites": {
    "jiaoyimao": {
      "anchor_at": "2026-11-16T15:37:00Z",
      "seen": {"1794831310167680": "2026-11-16T15:37:00Z"},
      "last_run": "2026-11-16T15:45:00Z",
      "consecutive_failures": 0,
      "last_alert_at": null
    }
  }
}
```

- **「新品」的判定**：未见过 **且**（`published_at` 为 `None`，或 `published_at >= anchor_at`）。`anchor_at` 是首次运行时间，写入状态。这样被擦亮/推广的旧商品即使第一次出现在列表里，也不会被误报为新品；`published_at` 缺失时保守提醒。
- 存储：`state` 分支的 `state.json`，通过 GitHub Contents API 用 `GITHUB_TOKEN` 读写（workflow 需要 `contents: write`）。仓库初始化时创建该分支和初始文件。
- 清理：`seen` 按「最近 60 天」或「最多 5000 条」裁剪。由于 `anchor_at` 规则，即使清理后旧商品被重新推广，其 `published_at` 早于锚点，仍不会重复提醒。
- 写入时机：
  - 有新 ID 被登记（即使被过滤规则淘汰也要登记，避免以后反复评估）。
  - 或距 `last_commit_at` 超过 24 小时的**心跳提交**（维持仓库活跃，避免公开仓库 60 天无活动导致定时任务被停）。
- 推送失败时不写状态，下次重试 → 至少一次，不丢商品（可能重复一次，可接受）。

### 6.6 通知适配器

```python
class Notifier(Protocol):
    name: str
    def send(self, items: list[Listing]) -> None: ...
    def alert(self, text: str) -> None: ...
```

- **飞书**：群自定义机器人 Webhook，发送 interactive 卡片（`lark_md` 支持链接）。每条含标题（可点击）、价格、服务器/平台、上架时间。每批最多 `batch_size` 条，超出拆分。
- **邮箱**：SMTP（QQ 邮箱 465/SSL 或 587/STARTTLS），HTML 表格正文。
- 渠道失败不影响其他渠道；所有渠道都失败才视为本次通知失败。
- 单个渠道连续失败达到阈值时，通过其它正常渠道发一次告警（避免某个渠道长期死掉却没人发现）。
- 告警消息使用 `[监控异常]` 前缀，受 `min_alert_interval_minutes` 节流。

需要的 Secrets：`FEISHU_WEBHOOK`；`SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASS`、`MAIL_TO`。

### 6.7 调度与运行

- 触发：`schedule: cron "*/5 * * * *"` + `workflow_dispatch`（手动测试/补跑）。
- GitHub 的 `schedule` 是 best-effort：`*/5` 是最小间隔，负载高时会延迟甚至跳过。**5–15 分钟是目标而非硬性 SLA**；翻页策略（§6.3）保证延迟期间不丢新品。
- 可选外部存活信号：配置 `HEALTHCHECK_PING_URL`（healthchecks.io 等免费服务）后，每次成功运行 ping 一次；超过约定时间没收到 ping 由外部服务告警。这解决「整个 workflow 停跑、内部渠道无法告警」的盲区。
- `concurrency: {group: monitor, cancel-in-progress: false}` 防止重叠。
- 单次任务 `timeout-minutes: 10`；正常应在 60 秒内完成（带浏览器兜底时约 20 秒）。
- 依赖安装：`pip install requests playwright`；使用 runner 自带 Chrome，不下载浏览器。
- 失败策略：
  - 单站点抓取失败：本站点跳过，不写状态；记录 `consecutive_failures`。
  - 连续失败达到阈值：发一次告警（节流）。
  - 通知失败：退出码非 0（在 Actions 里标红），不写状态。
- 每次运行在 `GITHUB_STEP_SUMMARY` 写入摘要：抓取条数、新增条数、耗时、是否降级。

## 7. 失败模式与处理

| 失败 | 处理 |
|---|---|
| 被风控（punish） | requests 重试 → Chrome 兜底 → 仍失败计入连续失败并告警 |
| 页面结构变化 / 解析失败 | 抛错，计入连续失败并告警；不把「解析失败」当成「没有新品」 |
| GitHub IP 长期被封 | 告警后按预案迁移到国内云函数（代码不变，替换运行环境） |
| 定时任务被 GitHub 停用 | 每日心跳提交保持活跃；另可在告警渠道收到异常 |
| 状态写入失败 | 重试一次；失败则本次不标记已见，下次重复评估 |
| 通知渠道故障 | 其它渠道继续；全部失败则本次视为失败；单渠道连续失败另行告警 |
| 两次运行间新增超过翻页能覆盖的量 | 翻到 `max_pages` 仍整页全新时记一次「新增量异常」告警，不静默 |

## 8. 测试策略

- 单元测试（pytest，离线，用保存的真实页面 fixture）：
  - `undefined` 解析、商品提取、字段映射、时间戳推导。
  - 去重：首次启动 bootstrap、重复运行无新增、批量新增。
  - 规则过滤：价格/平台/服务器/关键词（为第二版预留）。
  - 通知载荷：飞书卡片 JSON、邮件 HTML（mock HTTP/SMTP）。
- `python -m monitor --dry-run`：真实抓取并打印「将要推送」的内容，不通知、不写状态。
- 集成验证：`workflow_dispatch` 手动跑一次，检查日志与摘要。
- 测试 fixture 从本次实验抓到的页面中脱敏保存（不含 Cookie）。

## 9. 初始化步骤（部署时执行）

1. 新建**公开**仓库 `jym-monitor`，提交代码与 `config.yaml`。
2. 创建 `state` 分支，放入初始 `state.json`（空状态）。
3. 在仓库 Secrets 中配置飞书 Webhook（及可选的 SMTP 参数）。
4. 启用 Actions，手动触发一次 `workflow_dispatch`：首次运行登记存量并发「监控已启动」。
5. 验证一次：手动往 `state.json` 删掉一条最新 ID，等下一轮看到该商品被推送。

## 10. 留给实现者的决定

- 飞书卡片的具体排版与字段顺序；邮件模板样式。
- 请求头细节、重试间隔、Chrome 等待时长的微调。
- 日志详细程度与错误信息措辞。
- `Listing.extra` 中保留哪些字段、如何命名。

## 11. 后续演进（不在第一版）

- 规则细化：价格区间、服务器、平台、角色/关键词（配置结构已预留）。
- 多站点：新增适配器即可。
- 通知渠道：Bark / Telegram / 企业微信，按 `Notifier` 接口扩展。
- 若 GitHub IP 不再干净：迁移到国内免费云函数，或加入代理。

## 12. 决策记录（设计评审后）

1. **5–15 分钟是目标，不是 SLA**：接受 GitHub 定时任务的延迟与跳过，靠翻页策略兜住“不漏”。
2. **翻页直到遇到已见 ID**，`max_pages` 默认 3；整页全新时告警。
3. **新品判定 = 未见 且 `published_at >= anchor_at`**，`anchor_at` 为首次运行时间；排除被擦亮/推广的旧品。
4. **`seen` 保留 60 天或 5000 条**；锚点规则保证清理后旧品也不会重复提醒。
5. **单渠道连续失败**通过其它健康渠道告警。
6. **可选 `HEALTHCHECK_PING_URL`** 作为外部存活信号，解决 workflow 整体停跑无法自救的盲区。
7. **通知成功后才写状态**，失败则下轮重试（至少一次语义，可能重复一次，不丢）。
8. **仓库公开、状态公开**（用户已确认）：换取免费无限分钟数；状态中只有商品 ID 与运行时间。
