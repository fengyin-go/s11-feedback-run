# 轨迹分析与当前任务 AI 验收

只有轨迹分析使用 `reviewer.env`。过程证据核查、需求验收、评分、Feedback 撰写、过程与产物断言逐句复核、门闸修复由推进任务的当前 AI 完成，所有其他操作使用推进任务的当前 AI，不读取这份配置，不指定固定模型或推理强度，不冒称独立模型或审核人完成。

## 轨迹配置

本机文件 `/Users/fengyin/.codex/solo11/reviewer.env`，权限 600、非符号链接；其他机器默认 `~/.codex/solo11/reviewer.env`。入口全局参数 `--env-file /绝对路径` 可覆盖。

```dotenv
base_url=
apikey=
model=gpt-6.1-sol
reasoning_effort=high
```

前两项必填，模型及强度未填写时沿用上面的默认值。base_url 是 HTTPS Responses API 根地址，不带 `/responses`、查询参数或口令；路径保持原样。key 不加引号，不出现在命令行、仓库、日志或消息中。轨迹 key 与跑题的 gateway.env 不复用。

仅 `review` 轨迹分析及其 `probe` 连通性检查读取此配置。使用临时 CODEX_HOME、只读沙箱、隔离子进程环境，不修改 CC Switch 或全局配置。调用失败保留原始轨迹和错误，不回退其他模型，不声称完成。请求回执不证明第三方网关实际的上游路由。

## 操作

下列命令省略 `python3 <skill>/scripts/`：

```bash
fbr_feedback.py card --batch b1110-01
fbr_evidence.py ingest --task /绝对路径/batches/b1110-01/s11-000123
fbr_reviewer.py review --batch b1110-01 --task s11-000123 --execute
# 返回 needs_task_agent：当前任务 AI 开始产物验收、评分、写作和复核
fbr_reviewer.py finalize --batch b1110-01 --task s11-000123 --candidate /绝对路径/candidate.json --verification /绝对路径/verification.json --execute
```

`review` 先由 `fbr_evidence.py` 对每份原始轨迹做一次全量流式摄取，建立 SHA、行偏移、事件图和 `trace-check.json`；reviewer 读取 `trace/review-pack.json`，不直接整读 `trace/projects/*.jsonl`。模型需要写入结论的原文，只能通过精确行号和 block 定向获取。报告必须覆盖每份原始轨迹，含连续原文出处；事件包、facts 和模型自述只作导航。分析不负责产物评分或写 Feedback。保存 `review/trace-analysis.json` 后，生成 `review/task-agent-work.json`，返回 `needs_task_agent`。滚动调度保留此待办，当前任务 AI 必须主动处理，不能把这个返回视为完成。

当前任务 AI 先读 `review/task-agent-brief.md`，再按需求查询相关 diff hunk、最终代码及调用链，复用已有录屏和实跑证据。不要整份读取 `facts.json`、`diff.patch`、原始轨迹或 `task-agent-history.json`。静态证据充分时无需重复启动；缺证据时按既有 probe 入口在任务镜像中补跑，不能直接在宿主机运行被审项目。补证后刷新 card/brief；若轨迹报告仍有效，可用 `prepare --batch … --task … --execute` 仅刷新检查点，不调用外部模型。

默认作者输入是 compact `review/decision-input.json`，完整结构见 `review/candidate-schema.json`。当前 AI 只写需求语义、正文、claims、score_basis 和 quality_checks；`fbr_reviewer.py build` 展开为正式 candidate/verification。唯一轨迹摘录可自动补 locator，歧义位置必须失败。手工完整 candidate 仍可兼容，verification 须包含：

```json
{
  "requirements": [{"id": "R1", "verdict": "做到", "user_text": "用户可见结果", "anchors": []}],
  "score": 4,
  "feedback": "完整正文",
  "claims": [{"sentence": "正文原句", "assertions": [], "verdict": "supported",
              "reason": "原文依据", "later_check": "后续和最终状态",
              "sources": [{"path": "trace/projects/x.jsonl", "excerpt": "连续原文"}]}],
  "quality_checks": {"result_and_process": {"passed": true, "reason": "具体理由"}},
  "process": [{"sentence": "过程句", "evidence": [], "audit": {"checks": {}, "note": ""}}],
  "results": [], "issues": [], "test_claims": [], "score_basis": {},
  "needs_probes": []
}
```

脚本只补齐唯一来源 locator、需求状态、claim_reviews/process_audit 和 verification；不会填需求结论、分数、正文或语义理由。

- `task_agent: "current_task_agent"`，如实表明复核者。
- `binding`：检查点中的 binding 原样引用；它绑定证据、规则及轨迹分析摘要。
- `candidate_sha`：`fbrlib.content_hash(candidate)`，不是文件原始字节摘要。
- `checks`：feedback-quality.md 的七项语义判断，每项具体事实依据。
- `claim_reviews`：按 `fbr_quality.public_units(feedback.feedback)` 顺序逐句列出 sentence、assertions、verdict、reason、later_check、sources。原始轨迹 source 必须为 `{path,start_line,end_line,block,excerpt}`；脚本按精确区间校验，不得只写文件路径或 step。逐句复核必须真的回查原始来源、后续修正和最终影响，摘录必须逐字存在。过程句的来源必须包含原始轨迹 JSONL，并逐条包含 `process[].evidence` 的同一摘录；另找一段存在的日志不能替代该句证据。

当前 AI 写完候选后另做复核轮，发现矛盾或证据不足先修候选、补证，不把它标成 supported。正文每句必须覆盖。过程断言结合轨迹报告与原始来源，产物断言检查代码、实际日志及基线，不用生成报告代替原始出处。评分标准和证据要求保持不变。

正文先对照提示词说明功能完成度，已完成什么、未完成或仍有问题什么；全部做到时直接陈述实际完成的能力，不写“未发现其他明确未完成功能”这类空泛完成度句。之后再用最多 3 句写真实过程，只保留影响结论的一到三个事件。叙述要从证据提炼行为和结果，不把取证过程写进交付评价。不要写“轨迹显示”“录屏中”“素材卡里”“原始会话表明”或“证据显示”；改写成模型实际执行了什么、产物表现如何、哪些范围尚未覆盖。过程段至少一句必须是明确的模型行为句，但全文只在第一次点明主体时写一次“模型”，后续用“它”承接或直接省略主语，不能反复写“模型随后”“模型最后”。原始过程里出现首测失败、错误断言、验证器写错或返工时，先写模型当时的做法、失败结果、修正动作和最终影响；例如“模型先用并发 curl 校验重复提交，却把多段响应拼在一起解析，报出 JSONDecodeError；改成五个独立线程分别读取后，才确认五次提交共用同一批次”。不能只列脚本通过、构建完成和未覆盖边界，也不能用“额外边界没有问题”代替过程分析。

每个 `process` 句先写真实 `steps` 和 `evidence`：工具 evidence 的 step 必须属于该句 steps，field 只能是 tool/target/output/error，excerpt 必须落在该步骤的真实字段中；没有工具的特殊会话使用 assistant evidence。之后还要在同序的 `process_audit` 中完成五项归因检查：`full_trace`、`baseline`、`timeline`、`source_boundary`、`scope`。验收 probe、录屏和当前 AI 的页面点击只能支撑结果或问题，不能接到模型主语链上；若轨迹中没有浏览器动作，过程句也不能写模型“点击”或“页面操作”。基线已有行为、后续修改造成的回归、绝对化范围表述和“收尾”等时序表述分别按 [process-attribution-audit.md](process-attribution-audit.md) 回查。

评分与过程描述必须同向。5 分要求正文也呈现出接近一次性完美跑通；如果正文如实写了错误验证、错判、核心实现返工，或关键竞争没有真正复核，即使最后修好，也最高 4 分，并用 process_issue 说明最终交付可用但过程不完美。不要为了保 5 分删除这些真实经历。



`finalize` 不加载配置、不调用模型；默认在通过后主动推送快照并回读，明确本地模式加 `--no-publish`。它核对绑定、证据契约、评分上限、查重、七项语义记录和逐句原文。失败保存候选、复核和错误到 task-agent-history.json，返回 repairing，由当前 AI 继续修复；不会采用失败候选。通过后保存 task-agent-v1 回执、归档旧版、执行门闸并立即刷新本地表。文件来源变化或规则变化会使回执失效，必须重新核实；不自动改稿或提交平台；发布失败保留验收结果，修复后补发布即可。人工 spotcheck 由真实审核人完成。

`task-agent-history.json` 只保存每次尝试的摘要和内容寻址引用，完整 candidate/verification 在 `review/task-agent-history/objects/` 中各存一份；相同内容不会重复追加。`review/metrics.json` 记录 reviewer 的 token usage、prompt/response 字节、轨迹包大小和证据访问统计。
