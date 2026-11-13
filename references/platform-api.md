# SOLO2 平台接口

2026-11-11 已用本机 `gonghao` 凭据实际完成登录、`auth/me`、表单 schema 与自己的提交列表读取。接口固定为 `https://solo2.jzxhnh.com/api/v1`，不沿用旧凭据文件里的 `base_url`。正式附件上传和有效建单须由第一条合格交付验证；本地自测不代表生产质检通过。

## 命令和权限

下文脚本位于本 skill 的 `scripts/`，Python 始终加 `PYTHONDONTWRITEBYTECODE=1 python3 -B`，含中文的绝对路径必须加引号。

```bash
fbr_submit.py probe
fbr_batch.py --root "<root>" upload --batch b1110-01 --tasks s11-000123 --limit 1
# 用户明确授权本次平台提交后：
fbr_batch.py --root "<root>" upload --batch b1110-01 --tasks s11-000123 --limit 1 --execute --confirm SUBMIT
fbr_sheet.py --root "<root>" --batch b1110-01
```

`fbr_submit.py submit` 与 `fbr_batch.py upload` 是同一实现。默认只向平台发送登录请求和 GET，不上传、不建单；本地会刷新交付清单。已有绑定当前内容的回执时，默认演练也会 GET 回读并刷新本地回执的质检状态。

真实提交串行进行；所选任务先全部完成本地校验，再上传第一份文件。`--tasks` 逗号分隔，`--limit` 限制本次处理数。未传选择参数时检查整个批次；未就绪任务不被悄悄跳过。平台第一次提交会锁定该账号的领域，跨领域整批停止。首次提交属于哪个领域由用户确定，不由脚本替用户选择。

## 凭据和认证

默认账号为 `zouyonghong`，可用环境变量 `SOLO11_PLATFORM_ACCOUNT` 覆盖。优先读取 `~/.codex/coding-agent-satisfaction/solo-qa-accounts/<账号>.json`，不存在时才读取 `~/.codex/coding-agent-satisfaction/solo-qa.json`。两者均要求普通文件、权限 600、`username` 恰为所选账号，密码只读到内存。优先文件损坏或不安全时不能悄悄退回备用文件。禁止打印文件、密码、Cookie、CSRF 或完整登录响应；凭据不放工作目录、仓库、回执或交付目录。

登录：`POST /auth/login`，JSON 为 `username`、`password`。会话由内存 CookieJar 保存；写请求使用 `solo_qa_csrf` Cookie 解码后的值作为 `X-CSRF-Token`。不保存 Cookie 文件，不跟随跨 origin 重定向，不自动重试 POST。登录后 GET `/auth/me` 核对用户名和是否需要修改初始密码；需要改密码时停止。

## 实时 schema 与映射

每次读取 `GET /submissions/form-schema`，校验 19 个启用的输入字段及类型、必填、选项、领域与子方向联动、长度、格式和分数范围；建单前再次核对 fingerprint。未知字段、类型或校验规则停止提交，不能抛弃字段或自行猜值。指纹不写死在脚本里。

| API 字段 | 本地结果表 / 来源 |
| --- | --- |
| domain | 所属领域 |
| sub_direction | 子方向 |
| question_type | 任务类型；仅传输时 `泛Agent需求` → `泛 Agent 需求` |
| difficulty | 任务难度 |
| languages | 语言/框架 |
| harness | Harness |
| harness_version | Harness 版本，纯版本号 |
| os_platform | 操作系统 |
| repro_level | 环境可复现等级 |
| env_snapshot | 初始环境快照，40 位 SHA 的 GitHub commit 链接 |
| user_prompt | User Prompt 原文 |
| session_id | SessionID |
| turn_id | TurnID/PromptID |
| trace_file | 上传主会话得到的 `[{name,path,size}]` |
| artifact_snapshot | 产物快照，同仓库、40 位 SHA；本地已确认推送 |
| screencast | 上传视频得到的 URL 字符串 |
| score_delivery | 交付完整性，整数 1–5 |
| feedback | Feedback 原文 |
| other_issues | 平台“其他问题”，唯一非必填字段；只允许有证据的中断续写说明和“模型运行时读取了 apikey”，两者可同时存在，其他情况一律为空字符串，返修也相同 |

本地保留 21 列兼容表，不从表格反向读数据提交。`提交人` 和 `提交时间` 不在 API data 中：平台据登录账号和服务端时间生成。未提交时两项保持空白，不用本地身份或评价时间代替，也不阻塞输入字段检查。来源必须是当前仍通过 Feedback 闸门的原始记录和白名单投影；人工抽检退回的当前正文禁止提交，agent 不代登记人工抽检通过。

## 附件、建单和回读

1. 刷新 `results/<批次>/delivery-manifest.json`。正文、分数、证据、会话轮数和快照不合格时，上传前停止。用起跑 key 扫描交付源文件；缺 key 不能确认备注事实，停止导出。轨迹或证据命中起跑 key 时可交付，但备注必须自动包含“模型运行时读取了 apikey”，命中文件记入交付清单供核查。
2. 仅取本题清单内的主会话 `.jsonl` 和 `record/recording.mp4`，拒绝符号链接、跨题文件、空文件和摘要漂移。平台仅接受一个 JSONL，不能打包 ZIP 或把子会话当第二附件；子会话原样保留本地。
3. `POST /submissions/upload` 使用 multipart。轨迹只含 `file`；视频含 `file` 和 `kind=video`。轨迹响应须有 `name,path,size`，视频响应须有 `name,url`。同一打开文件先核对大小/SHA256，流式发送时再次核对 SHA256。2026-11-11 schema 的附件上限为 27 MB；schema 未给视频上限时采用当日页面组件的 500 MB，服务端可进一步拒绝。录屏时长/编码/分辨率仍遵守原运行规则。
4. `POST /submissions`，请求为 `{"data": {...19字段...}, "schema_fingerprint": "实时指纹"}`。上传后指纹变化则停止建单，保留已上传附件供重新演练后复用。
5. 用返回 ID 执行 `GET /submissions/<id>`，逐字段核对本次 data。只有一致才记 `confirmed`；保存原始平台状态，建单成功不代表自动质检通过。详情页为 `/app/submissions/<id>`。

## 回执和恢复

回执在 `batches/<批次>/<任务>/review/upload.json`，不是交付副本。它绑定固定平台、实际账号、当前所有 data、源交付摘要以及两份附件的路径/大小/SHA256。平台返回地址、记录 ID、状态与回读时间保存在回执，密码、Cookie、CSRF 不保存。

成功上传一项立即落盘；下一项上传失败可复用已确认那项。建单前先落 `creating`；连接中断、超时或返回缺 ID 时记 `uncertain`。再次调用先按本账号历史 SessionID 回查，能与原上传和所有字段一致对应才恢复确认。结果未知却查不到记录时必须停下核查，不能自动再 POST。已有相同 SessionID 且无匹配回执也不重复建单，不自动覆盖、返修或删除远端数据。

回执确认后重新生成结果表，才会采用平台媒体地址、所选平台账号与平台 `created_at`。本地正文/附件变化、旧占位回执或回读字段不一致，均不能继续显示有效已提交。若需要更新远端内容，另行分析平台返修锁定字段并取得用户授权；本版本不自动 PUT。

## 返修 PENDING_FIX

2026-11-12 已从平台前端代码和实际返修验证确认：返修保存使用 `PUT /submissions/{id}`，请求体为 `{"data": {...19字段...}, "schema_fingerprint": "...", "comment": "..."}`。页面只允许 `status=PENDING_FIX` 且 `editable=true` 的记录进入返修；`locked_fields` 内的字段和两份附件必须沿用平台当前值，不能通过返修更换会话或环境。

本地修复顺序：

```bash
fbr_reviewer.py review --batch b1110-01 --task s11-000123 --platform-repair --execute
fbr_reviewer.py prepare --batch b1110-01 --task s11-000123 --platform-repair --execute
# 修改 candidate / verification 后
fbr_reviewer.py finalize --batch b1110-01 --task s11-000123 \
  --candidate ... --verification ... --platform-repair --execute
fbr_batch.py repair --batch b1110-01 --tasks s11-000123
fbr_batch.py repair --batch b1110-01 --tasks s11-000123 \
  --comment "按平台打回原因重写 Feedback" --execute --confirm REPAIR
```

`repair` 只更新非锁定字段，当前实现会保留平台附件值。演练必须显示实际变化字段；真实执行后重新 GET 详情，确认版本递增、字段一致且 `qc_running=true` 或 `stage=QC_RUNNING/QC_WAITING`。如果 PUT 已成功但回执尚未落盘，再次运行会按当前详情识别 `already_applied`，不得重复 PUT。返修完成后轮询详情，只有 `QC_PASSED` 才算通过；`PENDING_FIX` 要继续按新的 `qc_summary` 和失败 `qc_checks` 处理。

平台 `other_issues` 不从本地 `feedback.note` 复制，只根据原始轨迹自动生成两类事实：模型自行中断后已输入“继续”的次数及首轮标识说明；当前轨迹或运行输出确实出现起跑 key 时写固定短句“模型运行时读取了 apikey”。两类都存在时用中文分号连接。两项都没有时显式发送 `""`；重跑、Git 历史、录屏问题、修复说明等仍保留在本地证据中。

## 2026-11-12 首条实际建单后的接口兼容

- 轨迹上传返回 `uploads/<存储键>.jsonl`，该值原样送入 data，不把存储键冒充可下载网址。只接受已核实的 uploads 命名空间，拒绝跨目录等非法值。
- 详情接口将 19 字段直接置于顶层；也兼容原 data 包裹格式，但必须字段齐全。语言/框架中的全角分号会被平台规范为半角分号，仅对此已核实的等价变化进行比较归一化。
- 详情中的轨迹条目把 path 替换为 HTTPS COS url。回读时核对单附件名称、大小，并真实下载计算 SHA256 与交付源比较；保留原始响应、原始上传存储键和远程内容核对证据，结果表使用经过核对的真实下载 URL。其他字段仍逐项精确核对。
- TLS 握手堆栈明确显示失败发生于 http.client.connect 和 ssl.do_handshake 时，归类 RequestNotSent，表明 HTTP 请求尚未发出。建单前发生此错误保留 uploaded 状态与附件；下次仍先回查同 SessionID 后继续。已获得记录 ID 后的回读失败或其他超时仍保留 uncertain，不能自动再建单。
