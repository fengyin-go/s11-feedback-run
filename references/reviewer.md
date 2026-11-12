# 独立验收模型

用户指定：验收轨迹分析、需求完成判断、评分和评价描述统一使用 `gpt-6.1-sol`，推理强度 `high`，包括返修。主会话可继续使用 CC Switch 配置的 DeepSeek；主会话只调度、收集与实跑，不生成评价结论或修改指定模型的评价。

## 配置

本机文件：`/Users/fengyin/.codex/solo11/reviewer.env`。其他机器默认 `~/.codex/solo11/reviewer.env`；用入口全局参数 `--env-file /绝对路径` 可覆盖，放在 `probe` 或 `review` 前。

```dotenv
base_url=
apikey=
```

`base_url` 填网关的 OpenAI Responses API 根地址，例如服务方要求的 `https://网关/v1`，不填 `/responses` 路径；脚本保留给出的路径，不自动加删 `/v1`。`apikey` 填验收专用 key，不加引号。文件权限必须为 600。不把 key 填进 SKILL.md、settings.json、命令行、题目仓库或消息。

跑题用的 `gateway.env` 和验收用的 `reviewer.env` 分别配置，不自动复用。镜像中 `auto_model/urm` 的运行配置仍由原跑题入口负责。

网关需要支持 Responses API、`gpt-6.1-sol` 这个精确模型标识和 `reasoning.effort=high`，以及验收读取文件所需的工具调用。如果网关使用另一个模型标识，先向用户确认映射，不能自行改为其他模型。请求参数与回执不能证明第三方网关实际调用的上游模型。

## 连通性与运行

下列命令省略 `python3 <skill>/scripts/` 前缀，在批次根目录运行：

```bash
fbr_reviewer.py probe                           # 演练，不发请求
fbr_reviewer.py probe --execute                 # 用独立网关发一次小请求，消耗少量额度
fbr_feedback.py card --batch b1110-01
fbr_reviewer.py review --batch b1110-01 --task s11-000123 --execute
fbr_feedback.py gate --batch b1110-01
```

入口要求本机安装 Codex CLI，采用临时 `CODEX_HOME`，显式指定 provider、模型、high 和 Responses 协议；忽略用户配置及规则，关闭项目指令和联网搜索，使用只读沙箱及临时会话。验收 key 只放子进程环境，排除在模型执行的 shell 环境外。不修改全局 Codex 配置、CC Switch 或本次主会话模型。

调用失败、超时、模型不支持或格式不合法时停止，不回退 DeepSeek 或其他模型。配置未填写时不发送请求。`probe` 的 OK 只证明一次文本响应成功；第一道真题仍需确认完整材料读取、工具调用、评价结构及证据闸门都通过。

如果返回 `needs_probes`，根据 `review/probe-requests.json` 使用 `fbr_feedback.py probe` 在现有任务镜像的产物提交上真实执行，再生成 card 并重新验收。不要在宿主机直接执行被审项目代码。

模型先检查 diff、最终代码与实际调用链；静态证据足以确证需求缺口或 bug 时，不为同一结论启动服务。仅对运行依赖、证据不足或需验证通过的行为申请最小定向 probe；复用现有有效证据，交付录屏仍需保留。

入口生成 review/trace-index.jsonl，覆盖首轮、子会话及全部获准续写的每条消息，保留原始路径、行号、工具 ID 和结果关联。模型先浏览完整索引，再定向读取原始消息和相关日志；大消息解码后按字段分段读，不能用截断输出判断事实。索引和 facts 只导航，不作事实出处。负面缺失断言需检查完整相关后续范围，不能以关键词未命中证明未发生。评价综合全部过程和最终产物；填表标识仍取第一轮。它输出 audit、feedback 和 needs_probes。生成提示每次加载当前评分、写作、证据契约及样本校准规则，不依赖旧素材卡里的结构。

入口先执行确定性检查：结果和过程原句、需求关联、实际日志摘录、测试数量出处、评分上限及查重。通过后，再启动独立的 `gpt-6.1-sol / high` 调用，逐句拆出全部事实断言，回查原始材料、后续修正和最终产物，保存连续原文摘录；覆盖缺失、摘录不存在或结论不确定均拒绝采用。随后核对七项语义与其他正文的组织方式。复核失败时，把具体原因交回指定模型修复；标准入口每轮 3 版后，调用 `fbr_recover.py --batch … --task … --execute` 继续，保留每次生成和独立复核的请求、输出及轮次错误，不采纳失败候选。恢复只调整调度与来源/格式提示，不修改评分或证据政策，也不改写已通过记录的回执。调用异常需排除配置或网关故障后恢复；缺证据则请求最小 probe，不以降低标准解决。原提示词工程运行的重试限制不受此规则影响。详见 [feedback-quality.md](feedback-quality.md)。

全部通过后才保存评价、`review/model-review.json` 和 `review/quality.json`。两份回执绑定模型/强度、输入、输出和规则摘要，后者另含逐句原文复查、后续状态、七项复核依据及修改记录。gate、导出和结果表继续检查这些绑定；证据、规则或评价发生变化后，必须重新调用指定模型。旧任务不会被自动改稿或回写平台，但缺少当前有效复核时不能再次交付。

闸门错误及人工退回原因通过 `review/gate.json` 和 `review/spotcheck.json` 提供给下一次指定模型验收；不让主会话润色后重新盖回执。人审抽检不由模型代登记。平台其他问题字段仍仅填中断续写说明，其余情况为空字符串，返修也相同。
