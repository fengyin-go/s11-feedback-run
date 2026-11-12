# 运行、收集、快照、录屏的规则

本期是单轮 pointwise：题面只输入一次；模型自行中断时，同题可在同一容器、同一会话只发送“继续”，累计最多 2 次。评价综合所有续写内容，填表题面、SessionID 与 PromptID 仍以第一轮为准，不追加需求或解法。下面每一条都有脚本在执行，写在这里是为了出问题时知道该怎么判断。

## 镜像与模型基线

- 镜像：`adminfather/benzhi-claude-code2:20261110`。
- Claude Code：`2.1.319`。
- Intel 与 Apple 芯片使用同一镜像名、同一组命令；Docker 自动选择架构，不传 `--platform`。
- 网关 `https://llm2.jzxhnh.com`、模型 `auto_model/urm` 和 Git 环境均已配置在镜像内。
- `_config/settings.json` 若有旧的 `image`，需更新为上述值；拉取后用 `docker image inspect --format '{{.Id}}' adminfather/benzhi-claude-code2:20261110` 取本机 Id 配置 `image_id`。Id 与架构有关，不复用其他架构的 Id。冻结题目需由出题侧重新交接，不手改旧题的摘要。

## 预检（R0）

`fbr_run.py preflight` 逐项比对，有一项不符就不要开跑：

| 项 | 要求 | 不符时 |
| --- | --- | --- |
| 镜像摘要 | 本机镜像的 Id 等于 `settings.json` 的 `image_id` | 重新拉官方镜像，不要自己 build |
| context 上限 | 镜像环境变量 `CLAUDE_CODE_MAX_CONTEXT_TOKENS` 等于 1000000 | 这个值不许改。镜像里不是这个数就停下问管理员，不要用 `-e` 覆盖 |
| Claude Code 版本 | 镜像里 `claude --version` 等于 `harness_version` | 以镜像为准去核对 settings，不要在容器里升级 |
| key 文件 | 存在、权限 600、有 `apikey=…` 一行 | 见下面「key」 |
| 本机录屏能力 | 依赖可导入且真实 PTY、ffmpeg、Playwright 启动与截图检查通过（`fbr_record.py --doctor`） | 修好再跑 |

通过项也会显示实际值。版本检查使用唯一命名的无 key 容器，不加 `--rm`，容器名写在检查结果里并留存供排查。

## 起跑前（每题）

`fbr_run.py run` 不带 `--execute` 时只做这些检查并列出没准备好的题：

- 任务仓库已发布，`task.json` 里有初始环境快照地址。
- `task.json` 的镜像、Harness 版本和当前配置一致。
- workspace 的 HEAD 就是初始提交，`git status --ignored` 完全干净。
- workspace 的路径不带逗号（`--mount` 用逗号分隔字段）。
- 没有未核验的同名容器。已有 current 记录时先 `recover`，核对 PID 起始身份、题面/基线/仓库摘要、容器归属标签和 ID。活 worker 继续等；归属不符或无匹配退出回执则保留现场阻塞，不能直接删掉重跑。
- `--limit` 必须是正整数；首题验证用 `--limit 1`，实际工作线程数也不会超过待运行题数。

## 容器怎么起

```
docker run -i -d --init --restart=no --cap-drop ALL --security-opt no-new-privileges \
  --name <任务编号> --label solo11.owner=<任务绝对路径摘要> --mount type=bind,src=<workspace>,dst=/workspace \
  --env-file <key 文件> --entrypoint sleep <镜像> infinity
docker exec -i <任务编号> /usr/local/bin/entrypoint.sh print < run/prompt.txt
```

- 新容器用 sleep 保活；首轮通过 `docker exec` 执行镜像原入口脚本，仍执行空会话检查和全部运行参数（禁用斜杠命令、禁用记忆、空 MCP、固定六个工具、流式 JSON 输出）。只有明确检测到模型自行中断且有有效 SessionID，脚本才在同一容器用相同参数加 `--resume <首轮SessionID>`，stdin 严格为“继续”。正常结束、任务做错或一行没改都不触发续写。
- 只挂这道题的 workspace。不挂用户目录、不挂 Claude 或 Codex 的配置、不挂别的题。
- 不加 `--rm`：容器退出后轨迹还在容器里，要等导出。
- 同一个容器只跑同一道题；中断续写是这道题的例外操作，不是新题。工程异常重跑仍须先导出旧容器，再停止、删除并新建容器。
- 起跑前把 workspace 权限放开给容器里的用户（只动 workspace 自己，不动上级目录）。
- 起跑前将 key 的 SHA256 指纹写入 `run/key-identity.json`，不保存 key 原文。收集时要求 key 文件仍可用、权限仍为 600、指纹与起跑一致；缺失、换 key、没有起跑指纹都拒绝收集，原件与容器保留。

## 什么情况重跑

按 2026-11-11 用户补充口径：模型自行中断先在原会话输入“继续”，累计最多 2 次；网络和进程工程异常、零代码改动需要重跑。续写用完仍中断时保留证据和容器并停止。`run.max_continuations` 可配置为 0、1、2，不能超过 2。具体判定：

| 现象 | 处理 |
| --- | --- |
| 超时或真实卡住 | 按预算归档；60 分钟执行上限，近期有进展可宽限一次 15 分钟；同题累计 75 分钟、批次 7 小时，耗尽则 deferred，恢复不清零预算 |
| 流式输出里没有 `result` 事件 | stderr 像网关报错或退出码非 0 时重跑；否则记「无故中断」，存在有效 SessionID 且字段正常时发送“继续”，没有可恢复会话才重跑 |
| `result` 事件 `is_error` 为真 | 网关报错重跑；无故中断且会话有效时按“继续”处理 |
| 退出码非 0 | 重跑 |
| 正常退出但最终回答是空的 | 按无故中断处理，可恢复会话中发送“继续” |
| 回答以 `API Error` 开头 | 工程异常重跑 |
| 正常结束且有代码改动 | 有效；改错或答非所问不靠重跑刷结果 |
| 正常结束但零代码改动 | 无效，归档、恢复基线、新建容器重跑；包含首次最多 3 次；仅文档/缓存/日志或改后还原不算代码改动 |
| 401/403 鉴权失败或实际模型不符 | 停止，不作为普通工程重试；鉴权失败暂停同凭据的新轮 |

连首次最多 3 次。3 次都不行，运行阶段记失败，这题换掉，不要手动再试到成功。重跑次数和原因保留在内部 run/attempts.json；备注只允许轨迹核实的中断后“继续”说明。

不算数的那次：流式输出、stderr、轨迹、当时的改动（`diff.patch`）整份归档到 `run/attempt-N/`，然后 workspace 恢复到初始提交（`reset --hard` + `clean -fdx`）。归档时轨迹导不出来就停下，容器留着，先解决导出的问题。

## 收集（R2）

- `docker cp <任务编号>:/home/node/.claude/projects <任务目录>/trace/projects`，导出后核对，再 `docker rm`（不带 `-f`）。要保留容器排查问题就加 `--keep-container`。
- SessionID 取主会话文件名（也是每行的 `sessionId`），要和流式输出 `system/init` 里的一致。
- TurnID/PromptID 始终取第一轮 user 消息上的 `promptId`，不取续写的 ID。
- 主会话首条输入必须逐字等于题面原文；其后只允许最多 2 条逐字为“继续”的输入，并与运行阶段的中断记录、SessionID 和次数一致。无记录的“继续”、额外建议或跨会话恢复均拒绝。
- `run/continuations.json` 记录中断次数；每次续写流另存 `run/continue-<尝试>-<次数>.jsonl`，同时合并进入 `run/stream.jsonl`。主会话原始轨迹保留全部轮次，评价和取证遍历整个会话。
- 子代理的会话（`isSidechain`）不算轮次。
- 流式输出、stderr、导出的主会话或子会话任何地方含 key 原文，立即判失败，原始文件与容器保留；不替换、不删除、不自动重跑，也不继续快照、取证或提交。失败运行归档的轨迹也执行同一检查；已命中的任务会保持阻塞，等待人工处理。

## key

- 文件路径可在 `_config/settings.json` 通过 `"key_file": "~/.codex/solo11/gateway.env"` 配置（填写路径，不填 key）。默认绝对路径在本机为 `/Users/fengyin/.codex/solo11/gateway.env`。
- 用户自行编辑该文件，把 API key 填到 `apikey=` 后面，例如 `apikey=你的API_KEY`，单行、无引号；文件权限为 600，只用 `--env-file` 传。
- 未填写 key 时保留 `apikey=` 空值，预检应拒绝起跑。不要让 agent 回显文件内容，也不要把 key 填进 skill 或 settings。
- 不写进命令行参数、`settings.json`、仓库、任务目录、聊天记录。检测到泄露时只报告固定错误信息，不显示 key 或带 key 的原文片段；原始证据保持不变。
- 这个 key 是任务网关的，只用来跑题。打分、写 Feedback、实跑边界情况都不用它，录屏和取证的容器里也不带它。

## 产物快照（R3）

任务文档的口径：模型自己提交了多次就填最后一个，不要 squash；初始环境快照必须是它的祖先提交。

1. 恢复 workspace 的 git 配置：`.git/config` 换回起跑前的备份（`run/git-config.before`），新出现的钩子挪走。没有备份就拒绝继续。之后宿主机上的 git 调用都带 `core.hooksPath=/dev/null`、`core.fsmonitor=false`。
   只接受独立 `.git` 目录；`.git` 链接或文件、`commondir`、`gitdir`、`worktrees`、`config.worktree`（包括悬空符号链接）均拒绝在宿主机执行 git，防止 Git 从别处加载未备份的配置。
2. 扫描要进提交的文件：
   - 文件名像密钥的（`.env`、`*.pem`、`id_rsa`、`credentials*` 这一类）；
   - 内容里有网关 key 原文，或 token 形态的字符串；
   - 单个文件超过 50MB。
   命中就停，列出文件。确实是仓库本来就该带的样例（比如测试用的假证书），在 `review/secret-waivers.json` 写明路径和理由后重跑。真的是密钥就不要自己动手删改，那是在改模型的产物；把情况报给人决定这题怎么处理。
3. 工作区里嵌套的 git 仓库不进快照，路径记进内部 `excluded`，不进入备注。
4. 模型没提交的改动由宿主机补一个提交收走，作者用 `settings.json` 里的 `git_user_name` / `git_user_email`。模型自己的提交一个不动。
5. 初始提交不是最终提交的祖先（模型 rebase、reset 或重建了仓库）时，用最终的文件树在初始提交上新建一个提交，`history_rewritten` 记真。
6. 新运行必须先通过零代码改动检查；零改动不能进入新一轮评分交付。快照中的 `no_change` 仅保留对历史材料和独立快照工具的兼容，不能替代运行有效性门禁。

推送（`fbr_snapshot.py push`）：

- 默认演练，只说明会推什么。真推要 `--execute --confirm PUBLISH`，并且必须是用户明确要求的。
- 只做快进推送，不强推。远端 main 不是本地产物提交的祖先就报错停下。
- token 从 `gh auth token --user <账号>` 取，通过 `GIT_CONFIG_*` 环境变量传给 git，不写进 `.git/config`，不出现在任何输出里。
- 推完回读远端 main 的 SHA，并确认仓库是 public。两样都对上才把 `pushed` 记真，写出 `https://github.com/<账号>/<仓库>/commit/<40 位 SHA>`。

## 录屏（R4）

Web 工程必须录真实服务启动再到页面访问和功能操作；构建/语法检查不能替代。复用 [cc-gsb-create](../../cc-gsb-create/references/recording.md) 的真实 PTY + 页面方案。启动段目标至少 10 秒、硬下限 8 秒；服务就绪后页面段至少 8 秒。setup 失败属于准备失败，产物启动失败如实录报错，不重试刷成功。原计划为 `business_demo_status=pending_implementation` 的构建占位会直接拒绝。

页面成功打开后，按题面和实际界面选一条能展示业务结果的操作路线。多页面项目优先覆盖 3–4 个相关页面：本题核心页面优先，再选关联列表、详情、配置或执行结果页；页面不够时按实际数量操作，不把同一页面反复刷新、分页或开关弹窗算成多个页面。单页面项目可以操作多个功能状态，不虚构不存在的入口。

每个选中的页面至少展示一次与本题有关的实际动作及可观察结果，例如检索后列表变化、打开详情核对字段、提交配置后查看保存状态、执行任务后查看回执。已有数据能支持时可演示创建→列表→详情→结果的关联流程；不要只切换菜单或停在空白页。遇到真实错误保留现场，仍可继续演示不依赖失败步骤的其他页面；不要为凑满页数修改产物、绕过前置条件或编造成功结果。

准备方案前先只读核实真实路由、导航与 selector，优先通过页面内导航切换。操作后等待结果元素出现，关键状态使用 `pause_ms: 1000`–`2000` 留出阅读时间；该字段逐动作配置，默认 300 毫秒，允许 0–10000 的整数，实际停留受剩余录制预算约束。动作记录包含当时 URL 和实际停留毫秒数，SPA 同 URL 的不同视图仍按真实内容判断，不仅凭 URL 数统计页数。页面多时通常给页面段约 30–60 秒，总片仍不超过 90 秒；开录前精简重复操作、预留加载和错误等待时间，不能靠截断末尾页面满足时长。

用户授权修订演示时使用 `review/recording-plan.override.json`：`source_sha256` 是原冻结 plan 的 `fbrlib.content_hash`，`authorization` 为用户授权原文，`plan` 为实测入口、URL、页面 selector 和动作。不可改冻结交接文件。覆盖方案和摘要写入 recording.json，更新录屏使下游取证与评价失效。


任务文档的口径：不超过 90 秒、720p、mp4；跑不起来也要录。

- 环境：任务镜像的新容器，挂的是产物提交的干净克隆（不是 workspace 本身），不带 key。
- 准备命令（`recording-plan.json` 的 `setup_cmd`）开录前跑完，不进画面。失败自动重来，最多 3 次；仍失败就把 `setup_ok` 记假并保存失败现场，本次录屏无效，不作为产物启动失败，也不写备注。
- 演示命令逐条在伪终端里跑，单条默认限 60 秒。画面是终端的真实输出，不剪辑、不加字幕、不重排。
- 命令报错就录报错。不为了画面好看换命令、不补环境、不重录到成功。`--redo` 用于录屏本身损坏（文件损坏、分辨率不对），或用户明确授权扩充演示内容后按覆盖方案重录；不能因产物报错而重录到成功。
- 成片：1280×720、H.264、mp4、最多 90 秒。截断 `cut` 或必需命令未执行 `skipped` 均判本次录屏失败，不能以成功录像交付。
- 录完会回读视频的宽、高、时长，不合规格记进 `problems`，录屏阶段判失败。
- 支持 `mode: "web"`：同一视频先显示真实 PTY 启动输出至少 8 秒，再显示页面及明确动作至少 8 秒。仅发布到 `127.0.0.1` 的本次临时端口；启动命令用 `{host}` / `{port}` 接收地址和端口。不改命令迎合结果。以下仅示意多页方案结构，路由、selector 和预期文本必须换成实际核实的值：

```json
{
  "mode": "web", "setup_cmd": "make setup",
  "serve_cmd": "npm run dev -- --host {host} --port {port}",
  "url": "http://127.0.0.1:{port}/", "ready_selector": "#app", "startup_timeout": 45,
  "steps": [
    {"action": "fill", "selector": "input[name=q]", "value": "demo"},
    {"action": "click", "selector": "button[type=submit]"},
    {"action": "expect_text", "selector": "#results", "text": "demo", "pause_ms": 1500},
    {"action": "click", "selector": "#results a[data-detail]"},
    {"action": "wait_for", "selector": "#detail", "pause_ms": 1500},
    {"action": "click", "selector": "nav a[href='/settings']"},
    {"action": "fill", "selector": "input[name=label]", "value": "demo-label"},
    {"action": "click", "selector": "button[data-save]"},
    {"action": "expect_text", "selector": "#save-status", "text": "已保存", "pause_ms": 2000}
  ]
}
```

- 页面动作还支持 `select`（原生下拉框的字符串 `value`）、`press`（`key`）、`wait_for`。原生下拉框使用 `select`，避免键盘模拟在不同浏览器环境中未提交选择。启动未就绪、页面错误、动作失败分别留证，不假定页面可用。
- 每次录制位于 `record/attempts/<UUID>/`。候选视频规格合格且步骤完整才替换正式文件；失败重录保留此前合格视频及其状态。
- setup 的网络/超时环境失败与原因不明会阻塞评价交付；只有具体编译等产物失败证据才可进入缺陷判断，不能把基础设施失败压到模型评分上。

干净克隆等临时产物默认落在工作目录的 `_verification/runtime-scratch/`；可通过 `FBR_SCRATCH` 指向本次验证目录。取证与实跑命令使用的容器不加 `--rm`，步骤结果记录 `container_name`，不自动删除容器。

## 运行记录与交付目录

代码仍在 `batches/<批次>/<任务>/workspace`；run、trace、record、evidence、review 保持为该目录的兄弟运行记录。最终交付由 `fbr_batch.py export` 按白名单投影到 `results/<批次>/`，不复制代码或 Git 配置，不改变运行阶段和原始证据。最终表、预览及上传计划都引用交付层文件；清单包含逐文件 SHA256，可复核来源。

## 首次真跑要核对的事

脚本是按文档和镜像入口脚本写的，自测用的是造出来的轨迹。第一道真题跑完后逐项对一遍，对不上就保留证据并汇报；修改脚本需有明确授权，不能跳过闸门：

1. `preflight` 里 context 上限那一项：镜像里实际的值是什么。
2. `run/stream.jsonl`：有没有 `system/init` 和 `result` 两类事件，字段名是否是 `session_id`、`claude_code_version`、`is_error`、`result`。
3. `trace/projects/` 下的目录结构，主会话文件是否叫 `<SessionID>.jsonl`，user 消息上有没有 `promptId`。
4. `trace/ids.json` 的 `user_turns` 等于 1 加有记录的续写次数（最多 3 条输入），`prompt_id` 为首轮 ID，`cli_version` 为纯版本号；续写时确认 SessionID 不变。
5. 容器里模型建的文件，宿主机上能不能读、能不能提交（权限和属主）。
6. `record/recording.mp4` 能正常播放，画面是 1280×720，字体和中文显示正常。
7. `evidence/facts.json` 的步骤数和轨迹里的工具调用数对得上，测试用例的通过和失败数解析对了。


## S11 单份 main 交接

`s11-gsb-prompt-create` 新批次先完成 `prepare_handoff.py`；运行脚本只用该批 task.json 的原文题面和同级 workspace。每题 origin 保留基准提交，workspace 在 main 的相同初始提交起跑，不创建 A/B 容器。origin 的预检依赖不挂到跑题容器，workspace 必须连忽略文件也没有。工程异常重跑与获准“继续”仍属于同一道题的记录，不能当成第二侧产物。产物推同一 main，初始快照使用 commit SHA。

## 共享调度与证据版本

参数与控制命令见 [runtime-flow.md](runtime-flow.md)。同凭据跨批次使用同一额度池；独立评价使用另一凭据，不占跑题额度。任务锁覆盖运行、取证、录屏、快照与评价，已有平台单冻结生产证据。

测试命令的完整输出保存在 `evidence/command-logs/`，事实卡中的尾部只用于展示。初始测试缓存绑定初始提交、镜像 ID、架构、setup/test 命令和超时、执行脚本摘要；身份或原始日志不匹配就重跑。facts 同时绑定当前轨迹、视频、录屏元数据与产物提交，来源变化不能沿用旧评价。
