# astrbot-wbhub-plugin

AstrBot 插件：在任意 AstrBot 消息平台（QQ 与 Telegram 等）发一条 `/wb`，查询 **WorkBuddy Hub** 单实例的用量报表 —— 默认 WorkBuddy 国内版当日，也可以选择国际版、CodeArts 或全渠道，以及最近 24 小时、近 3/7/30 天、本周、本月或全部时间。单渠道包括总 Token、总积分、累计 Token、每个账号的消耗与余额、按模型统计，以及**账号当前是否可用**（熔断 / 风控 / 限流 / 停单等）；全渠道只汇总 Token。

纯文本输出，不依赖文转图服务，不需要模型参与。

当前版本：**v1.3.11**。适配 Hub v2.1.1：持久封禁显示「已封禁，需手动启用」，详细与宽区间视图均保留，兼容旧版风控倒计时。`/wb 1` 改为**最近 24 小时滚动窗口**（不再是「今天 00:00 起」），与默认的当日视图彻底区分；CodeArts（`hw`）不再输出「0 积分」噪声。渠道选择见 v1.3.8：`/wb cn`、`/wb int`、`/wb hw`、`/wb all`；单独 `/wb all` 表示全渠道当日，国内版全部时间请用 `/wb cn all`。

- 仓库：<https://github.com/umeabc/astrbot-wbhub-plugin>
- 数据来源：WorkBuddy Hub 的 `/panel/login` + `/usage/analytics` + `/accounts` / `/codearts/accounts`（`X-Panel-Token` 认证）

> 非官方插件，与 WorkBuddy / CodeBuddy 官方无关。上游网关项目见 [ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)，接口契约以该项目源码为准。

---

## 功能

| 能力 | 说明 |
| --- | --- |
| `/wb [渠道] [参数]` | 输出用量报表（渠道 `cn`/`int`/`hw`/`all`，默认 `cn`；不填参数为当日，`0` 同默认、`1` 最近 24 小时、`3`/`7`/`30` 近 N 天、`week`/`month`/`all` 选区间，`help` 看帮助）。一次一条，过长则分页 |
| `/wbdiag` | 自诊断：打印插件看到的适配器、发送者 ID、群 ID、是否管理员、判定结果。**不受白名单限制**，被拒绝的人也能用它排查 |
| 账号状态 | 熔断 / 降级 / 冷却 / 单模型 429 / 额度用满 / 风控限制 / 凭证过期 / 停单 / 已禁用模型 / 连续失败，正常账号不显示任何多余字符 |
| 分页 | 账号或模型很多时按行分页成多条消息（`[WorkBuddy i/N]`），**不截断任何一条** |

## 安装

### 方式一：上传 zip（推荐）

从 [Releases](https://github.com/umeabc/astrbot-wbhub-plugin/releases) 下载 `astrbot_plugin_workbuddy_usage-*.zip`，在 AstrBot 面板「插件 → 安装插件（+）→ 从文件安装」上传，然后重载插件。

### 方式二：git clone

```bash
cd AstrBot/data/plugins
git clone https://github.com/umeabc/astrbot-wbhub-plugin.git
# 仓库根即插件目录时，把目录改名为 astrbot_plugin_workbuddy_usage
mv astrbot-wbhub-plugin astrbot_plugin_workbuddy_usage
```

依赖由 AstrBot 按 `requirements.txt` 自动安装：

```
aiohttp>=3.9,<4
tzdata>=2024.1
```

本插件不使用 `requests`（AstrBot 项目规则），只用 `aiohttp`。

## 配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `base_url` | `http://127.0.0.1:8788` | Hub 源站，只填协议+主机+端口（8788 是 Hub 默认端口）。**Hub 不在本机时要改成实际地址**；不可在聊天中覆盖 |
| `proxy` | （空） | 可选出站 HTTP/HTTPS 代理，如 `http://127.0.0.1:8080`；留空直连 |
| `panel_password` | `admin`（secret） | Hub 面板登录密码。`admin` 是 Hub 出厂默认值，改过密码就要同步改这里 |
| `panel_token` | （空，secret） | 可选，预获取的 `X-Panel-Token`；配了密码时 401 后会自动续期一次 |
| `timeout_seconds` | 15.0 | HTTP 总超时（1..120） |
| `default_timezone` | `Asia/Shanghai` | 计算区间起点（当日 0 点、本周一、本月 1 日）所用时区（IANA 名） |
| `allowed_platforms` | `[]` | 允许使用的消息平台（如 `telegram`、`aiocqhttp`）。留空 = 不限制平台 |
| `allowed_users` | `[]` | 允许查询的用户 ID（各平台的发送者 ID）。**空 = 拒绝所有非管理员** |
| `allowed_groups` | `[]` | 允许查询的群/频道 ID。群里非管理员要求「用户 ID」和「群 ID」**同时**命中 |
| `allowed_qq_users` / `allowed_qq_groups` | `[]` | **旧键名**，等价于 `allowed_users` / `allowed_groups`，保留向后兼容（与新键取并集） |
| `allow_admin` | `true` | AstrBot 管理员可绕过白名单（不能绕过平台限制） |
| `allow_private` | `true` | 是否允许私聊查询（管理员同样受此约束） |
| `cooldown_seconds` | 10 | 同一发送者的查询间隔（0..3600） |
| `max_message_chars` | 1800 | 单页字符上限（300..4000） |
| `max_lines_per_message` | 35 | 单页行数上限（5..100） |
| `max_response_bytes` | 5242880 | Hub JSON 响应上限（65536..52428800） |
| `update_notice` | `true` | 在 `/wb` 末尾提示网关有新版本（需网关能访问 GitHub） |
| `update_check_interval_hours` | 6 | 后台检查网关新版本的最小间隔（小时，1..168） |

## 用法

在允许的私聊或群聊里发送 `/wb`（前缀跟随 AstrBot 配置）。不填渠道默认为 **WorkBuddy 国内版**，不填区间参数就是**当日**报表。

### 渠道

| 渠道 | 统计范围 | 示例 |
| --- | --- | --- |
| `cn` | WorkBuddy 国内版账号与使用情况 | `/wb cn`、`/wb cn 0`、`/wb cn 7` |
| `int` | WorkBuddy 国际版账号与使用情况 | `/wb int`、`/wb int 1`、`/wb int week` |
| `hw` | CodeArts 账号与使用情况 | `/wb hw`、`/wb hw 3`、`/wb hw month` |
| `all` | 全渠道 Token 总数、各模型 Token 数；仅不填参数、`0`、`1` 时增加累计使用 Token | `/wb all`、`/wb all 30`、`/wb all all` |

四种渠道都支持下表中的区间参数。`/wb 0` 与 `/wb cn 0`、`/wb 7` 与 `/wb cn 7` 完全等价；国内版沿用 `WorkBuddy` 报表标题，国际版、CodeArts、全渠道分别使用 `WorkBuddy 国际版`、`CodeArts`、`WorkBuddy 全渠道`。

**`/wb all` 优先解释为渠道选择，即全渠道当日。** 旧版用它查询全部时间的写法需改成 `/wb cn all`；全渠道全部时间用 `/wb all all`。`/wb help` 可查看渠道说明和示例。

全渠道报表不显示积分、账号明细、余额或账号状态；需要这些信息时，分别查询 `cn`、`int`、`hw`。渠道选择不新增配置项，也不改变权限、冷却和分页规则。

CodeArts 的 usage 行**只记 Token**：实操中每条记录的 `credit` 都是 `0`，也没有 `credits` 对象（真机 2.0.6 验证）。所以 `hw` 只输出 Token —— 表头没有「已消耗积分」行，账号行是 `已消耗 3,333 Token`，模型行是 `3,333 Token`；**余额仍然显示**（CodeArts 的 `credits.remain` 由独立账号接口提供）。已删除但窗口内有 Token 消耗的账号也会保留。`cn`、`int` 的账号行继续沿用原有积分模板。

### 区间参数

| 参数 | 统计区间 | 输出样式 |
| --- | --- | --- |
| （不填） | 当日 00:00 起（`today` 等价） | 当日模板 |
| `0` | 同不填，逐字节一致 | 当日模板 |
| `1` | **最近 24 小时**（now−24h → now，含昨晚） | 区间模板（与当日模板不同的标题/文案） |
| `3` / `7` / `30` | 近 3 / 7 / 30 天（含今天） | 区间模板 |
| `week` | 本周（周一 00:00 起） | 区间模板 |
| `month` | 本月（1 日 00:00 起） | 区间模板 |
| `all`（放在渠道后） | 全部时间，如 `/wb cn all`、`/wb all all`（Hub 数据多时会慢一些） | 区间模板 |
| `help` | 参数帮助 | — |

单渠道的「当日模板」与「区间模板」的差别不只是标题（v1.3.4）：

| 视图 | 累计消耗 Token | 可用积分（所选渠道账号余额合计） | 账号剩余积分 | 429 / 风控等实时状态 |
| --- | --- | --- | --- | --- |
| `/wb`、`/wb 0`、`/wb 1` | 显示 | 显示 | 显示 | 显示 |
| `3`/`7`/`30`/`week`/`month`/`all` | 不显示 | 不显示 | 不显示 | 不显示 |

原因：这三项都是「此刻」的状态，和一段区间的消耗放在一起容易被读成同一时刻；宽窗口只保留与区间有关的消耗数据。

「实时状态」指**带恢复时间的那些**：上游风控限制、熔断、降级、冷却、单模型 429 / 额度用满，以及「近期429」历史兜底。**结构性状态**不受影响，宽窗口里照样显示：凭证过期、已停用、保留积分停单、当日限额停单、积分达限、已禁用模型、连续失败。

CodeArts 的 `cooling=true` 在不填参数、`0`、`1` 时只显示 `【冷却】`，宽窗口隐藏；上游 `until` 没有时区，插件不据此推算恢复时间。

`/wb 1` 是**滚动 24 小时**窗口，会跨两个日历日（例如 11-08 ~ 11-09），因此数字必然与 `/wb`（今天 00:00 起）不同；它同时保留累计 Token / 可用积分 / 账号余额 / 实时状态。

`/wb 7` 的示例输出（没有累计/余额/429，模型行同时给 Token 与积分）：

```
WorkBuddy 近 7 天用量
2026-10-26 ~ 2026-11-01
近 7 天已消耗 Token：128,904
近 7 天已消耗积分：412.6
账号（2）：
a1b2c3d4 [cn]：已消耗 38.19 积分
dead0000 [cn]【已删除】：已消耗 1.2 积分
使用模型统计：
deepseek-v4.1-flash：91,204,553 Token / 69.93 积分
```

> 已删除的账号不再只报「已删除 N 个账号」：**窗口内有过消耗**的已删除账号会以 `【已删除】` 单独成行（余额一栏省略），这样上面的合计能对得上。WorkBuddy 沿用按积分判断消耗的规则；CodeArts 只要有 Token 或积分消耗就保留。没有消耗的已删除账号不显示。

其它任何参数（如 `/wb 5`、`/wb 3 7`、`/wb xx 7`）只会回一句「参数错误：…。请输入 /wb help 查看正确参数填法。」——**不请求 Hub，也不占用查询冷却**。

当日报表示例：

```
WorkBuddy 当日用量
2026-10-21
当日已消耗 Token：22,421,758
当日已消耗积分：79.97
累计消耗 Token：2,091,356,813
可用积分：8467.76
账号（3）：
a1b2c3d4 [cn]：已消耗 38.19 积分，剩余 4288.97 积分
e5f6a7b8 [cn]：已消耗 34.64 积分，剩余 3478.61 积分
c9d0e1f2 [cn]【近期429（code 14003，当前未限制）】：已消耗 0 积分，剩余 528.18 积分
使用模型统计：
deepseek-v4.1-flash：91,204,553 Token / 69.93 积分
glm-5.3-flash：12,310,000 Token / 10.04 积分
```

- **账号只显示 UUID 前 8 位**（脱敏）；昵称不显示（在部分部署里昵称就是手机号）。
- **`剩余` 是该账号当前的积分余额**，与 `已消耗` 是两回事。
- 「使用模型统计」是**所选渠道内跨账号汇总**，`all` 则汇总全渠道。Hub 不提供「账号 × 模型」的积分分解，插件**不会伪造或按比例推算**。
- 累计 Token 取自同一响应体的 `summary.all_time`，与当日数据天然同一时刻。
- **可用积分**是**所选渠道所有账号** `credits.remain` 的合计（含当前没有消耗、因而不在列表里的账号）；一个余额都读不到时显示 `—`，不会假装是 0。CodeArts 的余额由独立账号接口补充。

### 网关更新提示（v1.2.0）

`/wb` 报表的**最后一行**（在「使用模型统计」和任何「警告：」之后）可能是：

```
网关有更新，最新版本号v1.7.25
```

- 只有当 Hub 的 `/updates` 报告了**确实比当前版本新**的版本号时才显示；网关已是最新（或版本数据缺失 / 无法解析 / `/updates` 请求失败）时**什么都不打印**，报表与旧版逐字节一致。
- 插件每次 `/wb` 都会读一次廉价的 `GET /updates`，并按 `update_check_interval_hours` 的**时间节流**在**后台**调用一次 `POST /updates/check`（只读 GitHub，不安装任何东西），结果在**下一次** `/wb` 中体现，本次回复不变。
  之所以按时间而不是"有没有数据"来节流：Hub 会把检查结果落盘，一旦拿到过版本号就不会再自动检查；若按"缺数据"触发，提示会永久停在旧版本上。
- 插件自己做节流的手动检查，因此 Hub 面板里的「更新检查」开关保持关闭即可；前提是 **Hub 所在机器能访问 GitHub**，否则检查只会静默失败，不影响报表。
- 关闭 `update_notice` 后，`GET /updates` 与后台检查都完全不发。

### 账号状态词表

正常账号**不显示任何状态内容**。出现下列情况时，会在 `[区域]` 之后以【】追加（内部的时间等细节仍用全角括号），多个状态用 `；` 连接：

| 状态 | 触发条件 | 含恢复时间 |
| --- | --- | --- |
| `凭证过期，需重新登录` | 凭证失效 | — |
| `已停用` | 账号被关闭 | — |
| `上游风控限制，MM-DD HH:MM 恢复` | 上游账号级风控（403 / code 11140），按小时计、重启不消失 | ✅ |
| `熔断，… 恢复` | 连续硬失败触发熔断 | ✅ |
| `降级，… 恢复` | 降级期 | ✅ |
| `冷却，… 恢复` | 通用冷却 | ✅ |
| `{模型}被429，… 恢复` | 该模型被上游临时限流 | ✅ |
| `{模型}额度用满，… 恢复` | 该模型额度耗尽（上游 code 6004），与临时限流区分 | ✅ |
| `保留积分停单` | 余额跌破保留阈值而暂停接单 | — |
| `当日限额停单` | 触发日 Token / 日积分护栏 | — |
| `积分达限（仅免费模型）` | 日积分护栏命中，仅剩免费模型可用 | — |
| `已禁用模型：a、b` | 运维为该账号手工禁用的模型（超过 6 个显示 `等 N 个`） | — |
| `连续失败 N 次（W 秒窗口）` | N ≥ 2 且尚未熔断，作为触发熔断前的预警 | — |
| `近期429（code N，当前未限制）` | 历史错误兜底：只剩陈旧的 `lastError`，当前**没有任何**限制 | — |

最后一条值得解释：Hub 的 `lastError` 是**历史记录**，冷却结束后仍会保留到下一次成功请求。所以一个账号可能"看起来被 429 了"，但实际上冷却早已过期、当前完全可用 —— 这时插件只会如实标注「当前未限制」，**不会给一个假的解封时间**。

### 多平台

- 白名单里填的是**该平台自己的**发送者 / 群 ID：Telegram 的数字用户 ID、QQ 的 QQ 号等互不通用。用 `/wbdiag` 在目标平台里发一条即可看到插件读到的 ID。
- 群聊 / 频道查询要求发送者与群**同时**命中 `allowed_users` 与 `allowed_groups`。
- `allowed_platforms` 可限制哪些平台允许使用（如只填 `["aiocqhttp"]` 则 Telegram 会被拒绝）；留空则不限制。

## 安全

- `panel_password` / `panel_token` 标记为 secret，仅在 WebUI 中掩码显示；配置文件中是**明文**存储。
- 这组凭据等价于 Hub 的**管理权限**（能读账号与用量），请妥善保护 AstrBot 的配置与备份。
- Hub 出厂面板密码是 `admin`，部署后应尽快修改，并同步更新插件配置。
- 插件默认使用 HTTP 明文访问 Hub，建议放在可信内网，或用 HTTPS 反向代理。
- 插件只做**只读**查询：`/panel/login`、`/usage/analytics`、`/accounts`、`/codearts/accounts`、`/panel/logout`，不会修改 Hub 的任何状态。

## 离线探测（不经过 QQ）

AstrBot 主机上可直接用自带 CLI 验证到 Hub 的链路：

```bash
# 默认指向 http://127.0.0.1:8788，密码回落到出厂默认 admin
python tools/probe_hub.py --timezone Asia/Shanghai

# Hub 在别的机器 / 改过密码时：
WB_HUB_PASSWORD='<面板密码>' python tools/probe_hub.py \
    --base-url http://192.0.2.10:8788 --timezone Asia/Shanghai
```

- 密码只从环境变量 `WB_HUB_PASSWORD` 读取（也可用 `WB_HUB_PANEL_TOKEN` 传预获取 token），**绝不接受命令行参数**，避免留在 shell 历史里。
- 会打印实际使用的时间窗口（起止时间 + 原始 epoch + 用的是 Hub 时钟还是本机时钟），便于核对。
- 退出码：`0` 正常 / `2` 传输失败 / `3` 认证失败 / `4` 格式不兼容。

## 实现要点

- **区间终点永远是 Hub 时钟的「现在」，区间起点按 `default_timezone` 切**：`3`/`7`/`30` 从 N-1 天前的 0 点起，`week` 从周一 0 点起，`month` 从 1 日 0 点起，`all` 不设下界。窗口时间戳直接发给 Hub 的 `range=custom`，所以不受 Hub 所在机器时区影响。
- **「当日」以 Hub 的时钟为准，而且只用"刚刚这次响应"的时钟**。Hub 的 HTTP 响应带标准 `Date` 头（UTC，无歧义），插件据此换算配置时区的日历日；即便 AstrBot 跑在 UTC 容器、或宿主机时钟漂移，窗口也不会错。每次 `/wb` 先请求账号接口（`hw` 为 `/codearts/accounts`，其它渠道为 `/accounts?realm=all`，顺带取得本次响应的 `Date`）再计算窗口；超过 5 秒的旧 `Date` 一律不参与计算 —— 否则窗口会停在上一次查询的时刻，把"当时的用量"当成"现在的用量"重复报出来（v1.3.2 修复）。账号接口不可用时退回本机时钟，再用 `/usage/analytics` 响应里的 `Date` 校正一次。
- 响应体按固定大小**分块读到 EOF** 再解析（aiohttp 的 `read(n)` 在拿到部分缓冲时就会返回，单次大 `read` 会偶发截断）。
- 积分以 `Decimal` 解析与聚合，聚合过程不取整，仅在显示时按 6 位小数四舍五入并去掉尾零。
- `/usage/analytics` 的 `realm` 分别传 `cn`、`intl`、`codearts`、`all`。`cn`/`int` 的账号从 `/accounts?realm=all` 取得后按 `realm` 筛选；CodeArts 独立调用 `/codearts/accounts`，补齐余额和没有用量的账号。
- **CodeArts 口径来自 Hub v1.10.9 源码**：`wb_codearts.py:382-383` 的账号对象固定 `realm="codearts"`、`product="codearts"`，但账号公开视图（488-515 行）没有返回这两个字段；插件根据独立接口补上 `realm`。`wb_proxy.py:24922-24931` 的 CodeArts 请求处理传入 `realm="codearts"`，1581 行将其写入 usage 行的 `realm` 字段，因此 `hw` 按该 `realm` 查询统计，而非按 `platform` 或模型名猜测。
- **全渠道只取一次 analytics**：直接使用 `summary.window`、`summary.all_time` 与 `models[]`，不分别请求三种渠道再相加，也不额外请求 CodeArts 账号接口。各渠道正常查询仍为 1 次账号请求 + 1 次 analytics 请求；登录、续期、时钟校正重试及原有更新提示请求另计。
- CodeArts 账号接口可能按账号查询上游信息（Hub 缓存 60 秒，单次超时 8 秒）；接口失败时记录日志并降级，历史用量仍可显示，余额和状态可能缺失。
- **已删除账号按行列出**：对应渠道账号接口里已经不存在的 uid，只要在查询窗口内有消耗，就保留成 `【已删除】` 行（没有余额），不再只打印一个数量——否则区间合计对不上账。

## 测试

```bash
python -m pytest tests -q
```

全部离线运行（本地 aiohttp stub + 源码形状夹具），**不访问任何真实网络、不需要可用的代理或 Hub**。

## 兼容性

- **AstrBot**：基于 4.16+ 的插件 API（`Star` / `filter.command` / `AstrBotConfig`）。已在真实实例上跑通命令路径。
- **WorkBuddy Hub**：契约按上游**官方正式发布 tag [v2.1.1](https://github.com/ardeyouxipianyi/workbuddy2api-hub/tree/v2.1.1)** 源码复核；此前已在运行 2.0.6 的运维实例上逐条验证（旧版本新增状态字段缺失时，对应状态不显示；`hw` 需要 Hub 支持 CodeArts 统计及账号接口）。v1.10.9 → 官方 tag v2.0.6 源码对照及 v2.0.8 → v2.1.1 增量复核（以下真机结果为此前部署的观测）：
  - `/usage/analytics`：`_new_analytics_stat()`、`_get_usage_analytics()`、`compute_usage_analytics()`、`range_window()` 与 v1.10.9 **逐字节相同**；v1.7.12 → 2.0.6 期间 payload 结构（`summary.window` / `summary.all_time` / `accounts[]` / `models[]` / `keys[]`）与 Stat 的 15 个计数字段没有改动；`range=custom` 仍按 epoch 秒解释，`since=0` 仍视为「无下界」。
  - `/accounts`：截至 v2.0.8 的 2.0.x 变更只在响应里**增加**字段 —— `Account.public()` 新增 `group` / `poolScope` / `hasDeviceToken`，`account_views()` 将 `poolScope` 规范化为 `text` / `media`（v2.0.2 的文体池分离）；顶层新增 `text_ready` / `media_ready`，`usable` 改为文本池就绪数（此前真机 2.0.6 部署未返回两个新增计数，仅有 `usable`；官方 tag 源码明确返回三者）。插件报表不使用 `usable` / `text_ready` / `media_ready` / `poolScope`，渲染字段（`cooldownFor` / `breakerFor` / `degradeFor` / `restrictedFor`（旧版）/ `accountBanned`（v2.1.0+）/ `modelCooldowns[].expiresAt|cappedAt` / `fails` / `failWindow` / `lastError` / `disabledModels` / 三种停单 / `tokenExpired` / `needRelogin` / `enabled` / `credits.remain`）除下述风控字段替换外均保留。
  - `/codearts/accounts`：响应仍是 `{provider, accounts, stats, models, base_url}`；账号行仍是 `uid` / `credits.remain` / `disabled`（插件把 `disabled` 归一成 `enabled`）等，**没有 `realm`**，插件按独立接口补 `realm="codearts"`。`CodeArtsAccount.view()` 与 v1.10.9 逐字节相同；新增的 `manual_disabled` 仅用于内部状态及持久化，不出现在公开视图中，手动停用仍通过 `disabled` 表达。它的 `until` 是无时区字符串，插件不据此推算恢复时间。
  - `/updates` / `/updates/check`：`_get_updates()`、`_route_update_check()` 及 `wb_updates` 的 `status()` / `check()` 与 v1.10.9 相同，插件消费的版本提示字段和手动检查契约不变。
  - 鉴权未变：`POST /panel/login` 换 token，之后一律用 `X-Panel-Token` 头（本插件从 v1.0 起就这么发的）；`/panel/login` / `/panel/logout` 所在的 `_handle_panel()` 及 token 校验函数与 v1.10.9 相同，退出仍撤销 token 并返回 `{ok: true}`。
  - 此前 v2.0.1～v2.0.8 的新增内容都在本插件不消费的层面：文体池分离与媒体（生图/生视频）能力、TLS 指纹（Go/uTLS）、设备 token、桌面端 chat 兼容头、Responses 历史合并、打包方式。媒体与 CodeArts 出口默认关闭；开启后产生的用量按普通 usage 行进入统计，模型名照常显示。
  - v2.0.6 → v2.0.8 源码复核：v2.0.7 的会话粘滞键默认开启、v2.0.7 / v2.0.8 的 `x-anthropic-billing-header` / `x-anthropic-billing-hdr` 清洗修改请求派发与内容处理；面板 `X-Robots-Tag` 和账号表列固定仅涉及 HTML 响应头与样式，对插件无影响。上述 analytics 四个函数、账号视图及顶层计数、CodeArts 视图、更新与登录/退出契约均未变，无需调整插件校验。
  - v2.0.8 → v2.1.1 本地官方 tag 源码逐函数复核：上述 analytics 四个函数、`account_views()`、`_get_accounts()`（含 `usable` / `text_ready` / `media_ready`）、`CodeArtsAccount.view()` / `_get_codearts_accounts()`、更新与登录/退出处理函数均未变；在插件消费的契约范围内，除以下封禁迁移外未发现其他破坏性变化。
  - **v2.0.9**：TLS 出站默认 auto、桥生命周期及请求清洗范围调整属于请求路径；`/health`、`/settings`、`/panel/status` 新增脱敏 `outbound_tls` 状态，本插件（含探测工具）不调用这三个端点，不影响所消费的面板 JSON 契约。
  - **v2.1.0**：`Account.public()` **移除 `restrictedFor`，改为 `accountBanned` + `bannedAt`**。403 / 11140 改为持久封禁，只能通过面板手动启用解除；`cooldownFor` / `inCooldown` 也不再包含旧风控截止时间。插件同时兼容两种字段：`accountBanned is True` 显示「已封禁，需手动启用」，在宽区间也显示；与 `enabled=False` 并存只显示更具体的封禁，与 `restrictedFor` 并存不显示旧风控恢复时间。未封禁时旧 Hub（v1.7.19～v2.0.8）的「上游风控限制，{time} 恢复」文案不变。`bannedAt` 是绝对 epoch 或 null，表示封禁发生时间，不是恢复期限，不渲染。批量治理、自动化页面拆分和导入任务变更不改变插件消费端点的其他契约。
  - **v2.1.1**：每日活跃自动模型回退、桌面/网页共用候选序列、免费模型目录补全及 DSH 视觉声明修正属于自动任务、请求路径或看板配置，不改变本插件消费的面板 JSON 契约。本次仅做本地源码与离线测试验证，未声称已在 v2.1.1 真机验收。
  - **CodeArts 只记 Token**：其 usage 行的 `credit` 恒为 `0`、且没有 `credits` 对象（真机 2.0.6 验证），因此 `hw` 报表不再显示「已消耗积分」和「/ 0 积分」，只显示 Token 与余额。
  - 另注：上游已声明该项目进入**低频维护**状态，WorkBuddy 可能随时收紧访问方式，网关可用性不作保证。
- 支持平台：任意 AstrBot 平台适配器（已在 aiocqhttp / qq_official 上验证；telegram 等纯文本平台同理）。

## 许可与致谢

MIT，见 [LICENSE](LICENSE)。

接口契约与字段语义来自 [workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)（MIT）的源码分析；本项目仅消费其公开的管理接口，不包含其代码。
