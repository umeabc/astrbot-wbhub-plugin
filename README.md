# astrbot_plugin_workbuddy_usage

AstrBot 插件：在任意 AstrBot 消息平台（QQ 与 Telegram 等）发一条 `/wb`，查询 **WorkBuddy Hub** 单实例的当日用量报表 —— 包括总 Token、总积分、累计 Token、每个账号的消耗与余额、按模型统计，以及**账号当前是否可用**（熔断 / 风控 / 限流 / 停单等）。

纯文本输出，不依赖文转图服务，不需要模型参与。

- 仓库：<https://github.com/umeabc/astrbot-wbhub-plugin>
- 数据来源：WorkBuddy Hub 的 `/panel/login` + `/usage/analytics` + `/accounts`（`X-Panel-Token` 认证）

> 非官方插件，与 WorkBuddy / CodeBuddy 官方无关。上游网关项目见 [ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)，接口契约以该项目源码为准。

---

## 功能

| 能力 | 说明 |
| --- | --- |
| `/wb` | 输出当日用量报表（一次一条，过长则分页） |
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
| `default_timezone` | `Asia/Shanghai` | 计算「当日 0 点」所用时区（IANA 名） |
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

## 用法

在允许的私聊或群聊里发送 `/wb`（前缀跟随 AstrBot 配置）。示例输出：

```
WorkBuddy 当日用量
2026-10-21
当日已消耗 Token：22,421,758
当日已消耗积分：79.97
累计消耗 Token：2,091,356,813
账号（3）：
a1b2c3d4 [cn]：已消耗 38.19 积分，剩余 4288.97 积分
e5f6a7b8 [cn]：已消耗 34.64 积分，剩余 3478.61 积分
c9d0e1f2 [intl]【近期429（code 14003，当前未限制）】：已消耗 0 积分，剩余 528.18 积分
使用模型统计：
deepseek-v4.1-flash：69.93 积分
glm-5.3-flash：10.04 积分
```

- **账号只显示 UUID 前 8 位**（脱敏）；昵称不显示（在部分部署里昵称就是手机号）。
- **`剩余` 是该账号当前的积分余额**，与 `已消耗` 是两回事。
- 「使用模型统计」是**全实例跨账号汇总**。Hub 不提供「账号 × 模型」的积分分解，插件**不会伪造或按比例推算**。
- 累计 Token 取自同一响应体的 `summary.all_time`，与当日数据天然同一时刻。

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
- 插件只做**只读**查询：`/panel/login`、`/usage/analytics`、`/accounts`、`/panel/logout`，不会修改 Hub 的任何状态。

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

- **「当日」以 Hub 的时钟为准**。Hub 的 HTTP 响应带标准 `Date` 头（UTC，无歧义），插件据此换算配置时区的日历日；即便 AstrBot 跑在 UTC 容器、或宿主机时钟漂移，窗口也不会错。
- 响应体按固定大小**分块读到 EOF** 再解析（aiohttp 的 `read(n)` 在拿到部分缓冲时就会返回，单次大 `read` 会偶发截断）。
- 积分以 `Decimal` 解析与聚合，聚合过程不取整，仅在显示时按 6 位小数四舍五入并去掉尾零。
- `/accounts` 必须带 `realm=all`，否则 Hub 只返回当前区域的账号。

## 测试

```bash
python -m pytest tests -q
```

全部离线运行（本地 aiohttp stub + 源码形状夹具），**不访问任何真实网络、不需要可用的代理或 Hub**。

## 兼容性

- **AstrBot**：基于 4.16+ 的插件 API（`Star` / `filter.command` / `AstrBotConfig`）。已在真实实例上跑通命令路径。
- **WorkBuddy Hub**：契约按 **v1.7.19** 校验（旧版本同样可用：新增字段缺失时对应状态只是不显示）。已核对 `/usage/analytics` 在 v1.7.12 → v1.7.19 之间**没有变更**。
- 支持平台：任意 AstrBot 平台适配器（已在 aiocqhttp / qq_official 上验证；telegram 等纯文本平台同理）。

## 许可与致谢

MIT，见 [LICENSE](LICENSE)。

接口契约与字段语义来自 [workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)（MIT）的源码分析；本项目仅消费其公开的管理接口，不包含其代码。
