# WorkBuddy 每日自动签到 · 多账号版（GitHub Actions）

> **这份文档只描述本 fork 当前实际在跑的多账号版。** 上游 `tslshuli/workbuddy-checkin-public` 的单账号文档（`WB_REFRESH_TOKEN` / `WB_ACCESS_TOKEN` / `sync_token_to_github.py`）在本仓库**已不适用**，照抄会 guard 失败。
>
> 改造基线：上游 `c05a581` → 本 fork `7438fbe`（2026-11-13）。原项目 MIT，LICENSE 保留。

**一句话**：每个账号用手机号收一次短信验证码，换到明文长效令牌（RT）；所有 RT 组装进一个 Secret `WB_ACCOUNTS_JSON`；GitHub Actions 每天北京时间 09:00 在云端逐账号签到。**Mac 不用开机、不用装客户端、不用常驻**。只有「加账号」和「令牌到期续期」需要本机跑一次命令。

---

## 0. 平时只用这几条命令（速查）

全部在仓库根目录执行：`cd ~/Codes/workbuddy-checkin-nobody`

| 我想做什么 | 命令 | 说明 |
|---|---|---|
| **加一个账号** | `python3 scripts/sms_login.py --alias account02` | 输手机号 → 收短信 → 输验证码；RT 自动写进 `accounts.json`（0600） |
| **让新账号生效**（加完号必做） | `python3 scripts/set_accounts_secret.py accounts.json --repo lxuanzheng/workbuddy-checkin-nobody` | 覆盖 Secret `WB_ACCOUNTS_JSON`，幂等，几秒钟 |
| **看看有哪些账号** | `python3 scripts/add_account.py --list --accounts accounts.json` | 只显示别名 + 脱敏指纹 |
| **离线校验配置** | `python3 scripts/multi_checkin.py --accounts-file accounts.json --validate` | 不联网、不签到、不回显令牌 |
| **手动验收（只读）** | Actions 页 **Run workflow** → 勾 `check_only` | 只查状态不领积分 |
| **手动跑一次正式签到** | Actions 页 **Run workflow** → 不勾 | 当日已签会自动跳过，不会重复领 |
| **删一个账号** | `python3 scripts/add_account.py --remove account02 --accounts accounts.json` | 删完记得重跑上面的写 Secret |
| **令牌到期（约 30 天）** | 重跑该账号的 `sms_login.py --alias 同名` → 再覆盖 Secret | 同名会覆盖旧 RT，不重复追加 |

> ⚠️ **只加账号、不覆盖 Secret = 白加**。Actions 读的是仓库里的 Secret，不是本机的 `accounts.json`。这是最容易忘的一步。

---

## 1. 这套东西怎么跑

```
本机（只在加号/续期时）
  手机号 + 短信验证码
      │  POST /v2/plugin/login/send-sms
      │  POST /v2/plugin/login/token  → 服务端直接下发明文 accessToken / refreshToken
      ▼
  scripts/sms_login.py  →  accounts.json（本机 0600，已 gitignore，绝不入库）
      │
      │  scripts/set_accounts_secret.py（令牌经 stdin 交给 gh，不进命令行）
      ▼
GitHub Secret: WB_ACCOUNTS_JSON   ← 唯一必填 Secret
      │
      │  cron '0 1 * * *'（UTC）= 北京时间 09:00；也可手动 workflow_dispatch
      ▼
云端 ubuntu-latest runner
  multi_checkin.py --validate        先离线校验配置
  multi_checkin.py --out result.json 逐账号起独立子进程 → workbuddy_checkin.py
      │                               （RT → 换 accessToken → 查状态 → 必要时领取）
      ▼
  summarize_result.py → Actions 运行摘要（白名单字段，无凭据、无原始响应）
```

| 关键事实 | 值 |
|---|---|
| 唯一必填 Secret | `WB_ACCOUNTS_JSON`，内容形如 `{"accounts":[{"name":"account01","refresh_token":"eyJ…"}]}`（模板见 `accounts.example.json`） |
| 定时 | 每天 UTC 01:00 / 北京 09:00；GitHub 整点任务常有数分钟~数十分钟延迟，属正常 |
| 单机依赖 | 本机只要 Python 3（纯标准库，不需要 requests）+ GitHub CLI `gh`；**不依赖 WorkBuddy 桌面客户端** |
| 单账号失败 | 独立子进程隔离，不影响其余账号；最终按退出码决定是否让整个 run 失败 |
| 幂等 | 当日已签到返回 `code=10001`，脚本跳过，不会重复领取 |
| RT 轮换 | 服务端每次刷新会换发新 RT，但**旧值不立即作废**（本 fork 已用真实 RT 连刷两次实测 `old_token_reusable=true`），因此 Actions 不需要回写 Secret |
| 多账号 | 逐账号串行，每号最多 180 秒；job 总超时 20 分钟，账号很多时按需要调大 workflow 的 `timeout-minutes` |

**为什么不读本机登录态**：客户端 5.6.2+ 起 `workbuddy-desktop.info` 是 AES-256-GCM 加密信封，密钥只驻留客户端进程内存（且相关解密分支是 Windows-only），macOS 上取不到明文。短信登录由服务端直接下发 RT，整条死结被绕开，Linux runner 也能用。

---

## 2. 一次性初始化

> **本 fork 已完成这一节，可跳过。** 只有换机器、重建仓库、或把这套搬到别的账号时才需要照做。

1. **Fork / 导入仓库**，建议设为 **Private**（Actions 日志虽已脱敏，公开没有额外好处）。
2. **启用 Actions**：仓库 `Actions` 页 → 允许工作流；`Settings → Actions → General` 保持最小权限（workflow 已声明 `permissions: contents: read`）。
3. **准备 `gh`**：本机安装 [GitHub CLI](https://cli.github.com/) 并 `gh auth login`（需要 `repo` 权限；若要改 workflow 文件还需 `workflow` scope）。写 Secret 时脚本通过 `gh secret set` 的 stdin 传入，令牌不出现在命令行 / `ps` / shell history。
4. **导入第一个账号**：按 §3.1 跑 `sms_login.py`。
5. **写 Secret**：按 §3.2 跑 `set_accounts_secret.py`。
6. **首次验收**：Actions 页手动 **Run workflow** 勾 `check_only`，确认 `成功 1/1`；再跑一次不勾的正式签到。
7. （可选）配置失败通知 Secret，见 §3.6。

---

## 3. 日常操作

### 3.1 加一个账号

在仓库根目录（**必须**，否则 `accounts.json` 会落到 `.gitignore` 覆盖不到的地方）：

```bash
cd ~/Codes/workbuddy-checkin-nobody
python3 scripts/sms_login.py --alias account02
```

交互过程：输手机号 → 手机收到 6 位验证码（约 300 秒有效）→ 输验证码 → 脚本自动用该 RT 试刷新一次，成功后以 0600 写入 `accounts.json`。

**也可以拆成两步**（让助手代跑、或验证码在别处输入）：

```bash
python3 scripts/sms_login.py --alias account02 --phone '<你的手机号>' --send-only
# 收到短信后：
python3 scripts/sms_login.py --alias account02 --code 123456
# 验证码也可从 stdin 读： --code -  （配合管道，不进命令行）
```

输出只含脱敏信息，长这样：

| 字段 | 含义 |
|---|---|
| `action: added` / `updated` | 新增 / 同名覆盖 |
| `masked` | RT 脱敏指纹（`eyJhbG…(len=698)`），**不含可用片段** |
| `refresh_probe.ok: true` | 该 RT 能被刷新端点消费，云端可用 |
| `refresh_probe.rotated: true` | 服务端确实会轮换（正常，见 §1） |
| `total` / `file_mode: 0600` / `state_cleared` | 当前账号数、文件权限、待验证状态已清 |

**别名规则**：仅字母/数字/`_`/`-`，≤32 字符，不可重复。建议 `account01`、`account02`… 依次递增。

**脚本会拒绝的东西**：网页授权码 `WBAUTH1:`、加密信封 `$wbEncrypted`、短效令牌、长度/格式不对的字符串——这些填进 Secret 必然 401。

### 3.2 更新 Secret（每次加号 / 续期后必做）

```bash
cd ~/Codes/workbuddy-checkin-nobody

# 0) 本机 gh 若不在 PATH，先补上；写 Secret 需要 GH_TOKEN 或已 gh auth login
export PATH="$HOME/.local/bin:$PATH"
export GH_TOKEN=$(cat ~/.config/wb-checkin-gh-token)   # 0600 文件；也可改用 gh auth login

# 1) 离线校验 + 只预览（不写入）
python3 scripts/multi_checkin.py --accounts-file accounts.json --validate
python3 scripts/set_accounts_secret.py accounts.json \
  --repo lxuanzheng/workbuddy-checkin-nobody --dry-run

# 2) 正式覆盖写入（幂等）
python3 scripts/set_accounts_secret.py accounts.json \
  --repo lxuanzheng/workbuddy-checkin-nobody

# 3) 远端确认（只列名，不显示值）
gh secret list --repo lxuanzheng/workbuddy-checkin-nobody
```

`set_accounts_secret.py` 会把本地 JSON 经 stdin 交给 `gh secret set WB_ACCOUNTS_JSON`，覆盖旧值；`--repo` 不填则取当前目录 git remote。

### 3.3 查看 / 删除账号

```bash
python3 scripts/add_account.py --list  --accounts accounts.json
python3 scripts/add_account.py --remove account02 --accounts accounts.json
```

删完**必须重跑 §3.2**，否则云端仍按旧 Secret 跑已删的号。

### 3.4 手动触发与验收

网页：`Actions` → `WorkBuddy 每日自动签到（多账号）` → **Run workflow**

| `check_only` | 行为 |
|---|---|
| ✅ 勾选 | 只查状态，不领积分。首次配置、加完号、改完 Secret 后用它验收 |
| ❌ 不勾 | 正式签到；当日已签自动跳过 |

命令行等价：

```bash
gh workflow run checkin.yml --repo lxuanzheng/workbuddy-checkin-nobody -f check_only=true
RUN_ID=$(gh run list --workflow=checkin.yml --repo lxuanzheng/workbuddy-checkin-nobody \
         --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN_ID" --repo lxuanzheng/workbuddy-checkin-nobody --interval 5
gh run view  "$RUN_ID" --repo lxuanzheng/workbuddy-checkin-nobody --json conclusion,url
```

**看结果**：run 页面底部的 **Summary** 是多账号汇总表（每号 status / 结论字段），`成功 N/N` 表示全部成功。注意日志里回显的 `ARGS="--check-only"` 是 GitHub 打印的**脚本源码**，不是执行结果，别据此判断有没有领。

> 定时触发（`event=schedule`）时 `check_only` 判断必然为假，**一定走正式领取**，不用担心每天只在查询。

### 3.5 令牌到期续期

长效令牌约 **30 天**有效（以令牌自身 `exp` 为准；上游 README 写 60 天偏乐观）。到期表现：Actions 开始报「长效令牌刷新失败 / 401」。

**保守做法：每月重导一次，或配好失败通知后按需处理。**

```bash
python3 scripts/sms_login.py --alias account01      # 同名覆盖旧 RT
python3 scripts/sms_login.py --alias account01 --check --accounts accounts.json   # 可选：连刷两次验证轮换
python3 scripts/set_accounts_secret.py accounts.json --repo lxuanzheng/workbuddy-checkin-nobody
```

`--check` 只读、不签到、不改配置，输出 `old_token_reusable` 用来确认旧值仍可复用（到期前通常都是 true）。

> 短信登录**可能让该账号其他端的会话失效**（上游会作废整个会话，最后登录的才有效）。桌面客户端掉线重登一次即可，不是故障。

### 3.6 改时间 / 配通知

- **改签到时间**：编辑 `.github/workflows/checkin.yml` 的 `cron`（**UTC**）。北京 09:00 = `'0 1 * * *'`。
- **失败通知**（可选，Secrets 里添加任意一个即生效；默认仅失败提醒）：

| Secret | 用途 |
|---|---|
| `WECOM_WEBHOOK` | 企业微信群机器人 Webhook |
| `PUSHPLUS_TOKEN` | PushPlus 个人微信推送 |
| `BARK_URL` | iOS Bark，形如 `https://api.day.app/<KEY>/` |
| `SERVERCHAN_SENDKEY` | Server酱 / 方糖 SendKey |
| `SUCCESS_NOTIFY` | 填 `true` 时成功也播报 |

全留空则静默运行，只能到 Actions 页看结果（GitHub 默认会发失败邮件）。

---

## 4. 文件说明

| 路径 | 作用 |
|---|---|
| `.github/workflows/checkin.yml` | 定时 + 手动触发；注入 `WB_ACCOUNTS_JSON`；离线 guard → 签到 → 摘要 |
| `scripts/sms_login.py` | **新增**。短信登录换明文 RT，写入 `accounts.json`（0600、脱敏、同号覆盖、不进日志） |
| `scripts/multi_checkin.py` | **新增**。多账号编排：逐账号独立子进程，失败隔离，结果写 0600 文件而非 stdout |
| `scripts/set_accounts_secret.py` | **新增**。`accounts.json` → `WB_ACCOUNTS_JSON` Secret（stdin，支持 `--dry-run`） |
| `scripts/add_account.py` | **新增**。账号增 / 删 / 列（脱敏）；支持剪贴板、文件、stdin 输入 |
| `scripts/summarize_result.py` | 改：增多账号汇总表，向下兼容单账号结果 |
| `scripts/workbuddy_checkin.py` | 上游原脚本，未改：单个账号的查状态 / 领取 / 刷新 RT / 推送 |
| `scripts/gen_notify_config.py`、`scripts/copy_token_to_clipboard.py`、`scripts/sync_token_to_github.py` | 上游遗留工具，**本方案不用**（后者依赖本机登录态，macOS 5.6.2+ 解不出） |
| `accounts.example.json` | 配置模板（占位符，**不含真实令牌**） |
| `accounts.json` | 本机真实配置，**已 gitignore，绝不提交** |
| `.sms-state.json` | 短信登录待验证状态（只存别名与手机号，0600，已 gitignore） |
| `security_audit.py` | 部署前自检：凭据泄露 / 危险代码 / 敏感文件 |

`.gitignore` 已挡住 `accounts.json`、`.sms-state.json`、`result.json`、`*.log`、`workbuddy-desktop.info`、`notify_config.json` 等。改完可跑 `git status --short` 与 `python3 security_audit.py` 复核。

> **关于 `security_audit.py` 的一条预期告警**：本机一旦导入过账号，就会存在含真实 RT 的 `accounts.json`。该脚本扫描的是**目录里所有文件**（不按 `.gitignore` 过滤），因此会报：
> ```
> [2] 凭据泄露扫描
>     [严重] accounts.json    JWT 格式 token
> 结论：泄露风险 有
> ```
> 这是**正常的、预期的**，说明本机凭据确实存在且被保护着，不代表会泄露。判断是否安全以 `git status --short` / `git ls-files` 为准：只要 `accounts.json` 没出现在其中，就不会进仓库（`[4] 敏感文件误入库` 一栏应为「通过」）。**不要为了消掉这条告警去删 `accounts.json`**——删了就得重新收短信导号。

---

## 5. 结果解读与排错

单账号结果字段（来自原签到脚本，经白名单压缩）：

| status | action | 含义 |
|---|---|---|
| `ok` | `clicked` | 本次领取成功，看 `points` / `balance` |
| `ok` | `skip_already_signed` | 今日已签到，正常跳过 |
| `ok` | `skip_check_only` | 只读模式，看 `detail.today_signed` |
| `error` | `failed` | 失败，看 `msg` |

| 现象 | 原因 | 处理 |
|---|---|---|
| `未配置凭据` / guard 失败 | Secret 没写或名字不对 | 只认 `WB_ACCOUNTS_JSON`；按 §3.2 重写 |
| `成功 0/N`、`长效令牌刷新失败` / 401 | RT 过期或填错形态 | 重跑 `sms_login.py` 该别名 → 覆盖 Secret（§3.5） |
| 加了账号但云端没跑它 | 忘了覆盖 Secret | §3.2 |
| 报「加密信封无法解密」 | 把 `workbuddy-desktop.info` 的值塞进了配置 | 改用短信登录取明文 RT；`add_account.py` 会直接拒收 |
| `phone is required` / 验证码错误 | 手机号格式或验证码过期（约 300 秒） | 重发一次，`--send-only` + `--code` 两步走 |
| 桌面客户端掉线 | 短信登录作废旧会话 | 客户端重新登录一次 |
| 404 / 域名错误 | `domain` 填错 | 删掉该字段用默认，或填 `www.codebuddy.cn` |
| Actions 一直不自动跑 | fork 默认可能禁用 scheduled workflow | Actions 页确认已启用；也可先手动 Run 一次 |
| 定时没在 09:00 准点 | GitHub 排队延迟 | 正常，数十分钟内都算预期 |

本机排查辅助：

```bash
python3 scripts/multi_checkin.py --accounts-file accounts.json --validate       # 离线配置校验
python3 scripts/multi_checkin.py --accounts-file accounts.json --check-only     # 本机逐账号只读查询
python3 scripts/sms_login.py --alias account01 --check --accounts accounts.json # RT 连刷验证
python3 security_audit.py                                                       # 凭据/危险代码自检
python3 scripts/workbuddy_checkin.py --diagnose                                  # 上游环境自检（读本机登录态，本方案不用）
```

---

## 6. 安全边界与红线

| 层 | 措施 |
|---|---|
| 本机 | `accounts.json` / `.sms-state.json` / `result.json` 均 0600，且被 `.gitignore` 排除 |
| 传输 | 令牌经 `gh secret set` 的 **stdin** 写入，不进命令行、`ps`、shell history、对话 |
| 云端 | Secret 加密存储；`permissions: contents: read`；仅 `schedule` + `workflow_dispatch`，无 `pull_request_target` |
| 日志 | 输出层白名单压缩 + 摘要层白名单 + 长度 ≥80 的长串一律 `<REDACTED>`；完整接口响应永不进 stdout / 日志 / artifact |
| 代码 | `security_audit.py` 扫描硬编码凭据、加密信封、危险代码 |

**切勿**：

- ❌ 把 `accounts.json`、令牌、验证码、含 `session` Cookie 的 curl 贴进对话 / Issue / commit。
- ❌ 把 `workbuddy-desktop.info` 或其内容复制进仓库（5.6.2+ 下既不可用也不该外传）。
- ❌ 把仓库设成 public 后又放宽 Actions 权限；本 fork 建议 **Private**。
- ❌ 用代理轮换 IP / 改设备指纹去绕风控。多账号自动化本身已有平台条款与风控风险，多个同类项目 README 都自行标明；账号数量克制，别贪。
- ❌ 把用于部署的 PAT 长期留着：临时生成、用完立即在 https://github.com/settings/tokens **Revoke**，并删除本机保存它的 0600 文件。

凭据疑似泄露时：网页端退出登录再重登（使旧会话失效）→ 重跑 `sms_login.py` 覆盖所有账号 RT → 重写 Secret → 撤销相关 PAT。

---

## 7. 本机环境备忘（这台 Mac，2026-11-13 核实）

| 项 | 值 / 说明 |
|---|---|
| 本 fork | `lxuanzheng/workbuddy-checkin-nobody`（`main`） |
| 本机 clone | `~/Codes/workbuddy-checkin-nobody` |
| `gh` 二进制 | `~/.local/bin/gh`（v2.104.0，从 Homebrew bottle 提取，**不在 PATH**）→ 用前 `export PATH="$HOME/.local/bin:$PATH"` |
| 写 Secret 的认证 | `export GH_TOKEN=$(cat ~/.config/wb-checkin-gh-token)`（该文件 0600）；或改成一次 `gh auth login` |
| Python | 系统 `python3`，脚本全部只用标准库 |
| 已跑通 | 云端 `check_only` 与正式签到两次 run 均 `成功 1/1`；`today_checked_in=true`、连签 36 天、余额 3600 分 |
| 当前账号 | **`account01` 一个**（手机号尾号 8491）。加号时用 `account02`、`account03`… |
| 待办清理 | 部署用的临时 PAT 待 Revoke + 删除上述 0600 文件；曾在对话中贴出的网页 `session` Cookie 建议退出重登使其失效 |

> 这节是**状态快照**，会过期；操作知识以 §0–§6 为准。

---

## 8. 常见问题

**Q：Mac 关机 / 不装 WorkBuddy 客户端还能签吗？**
A：能。签到完全在 GitHub 云端 runner 上跑，本机只在「加账号」和「令牌续期」时需要联网一次。

**Q：一个账号要收几次短信？**
A：首次一次，之后约每 30 天续期一次。日常每天自动签到不再需要手机。

**Q：加完号要不要改 workflow / 代码？**
A：不用。账号数量与 workflow 无关，只覆盖 `WB_ACCOUNTS_JSON` 这一个 Secret。

**Q：今天已经手动签过了，Actions 再跑会重复领吗？**
A：不会，接口幂等，返回「今日已签」自动跳过。

**Q：为什么 RT 会轮换却不用回写 Secret？**
A：实测连刷两次旧值仍可用（`old_token_reusable=true`），服务端换发的新 RT 不会立即作废旧值。因此 Actions 每天用同一份 Secret 即可。

**Q：把 refresh token 换成网页 Cookie 行不行？**
A：不行。网页端是 Cookie 会话体系，与插件网关的 `X-Refresh-Token` 是两套认证；且 Cookie 约 7 天过期、绑定 UA、云端出口 IP 不同，不适合 Actions。

**Q：能改签到时间吗？**
A：能，改 `.github/workflows/checkin.yml` 的 `cron`（UTC），改 workflow 文件的推送需要 PAT 带 `workflow` scope。

---

## 9. 改造记录

| 提交 | 变更 |
|---|---|
| `c05a581` | 上游 `tslshuli/workbuddy-checkin-public` 单账号基线 |
| `977ff7e` | 多账号改造：`multi_checkin.py` / `add_account.py` / `set_accounts_secret.py` / `accounts.example.json`；workflow 改注入 `WB_ACCOUNTS_JSON` + 离线 guard；摘要层兼容多账号 |
| `7438fbe` | 弃用无效的「浏览器 Console 取令牌」路线，新增 `sms_login.py`（官方短信登录换明文 RT），删除错误产物 `templates/export-refresh-token.js` |
| 本次 | README 整体重写为多账号版使用教程，移除过时的单账号配置说明 |

上游未改动的能力：签到主脚本、状态查询、四类通知推送、日志脱敏、`security_audit.py`、MIT 许可。

---

## 许可证

基于 [MIT 许可证](LICENSE) 开源。
