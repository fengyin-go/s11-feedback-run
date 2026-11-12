# Feedback 生成与复核契约

fbr_reviewer.py review 自动执行：指定模型生成 → 确定性检查 → 另一次指定模型语义复核 → 失败反馈给生成侧。最多 3 版，接口异常或材料变化立即停止。实跑不足时返回 needs_probes，调度侧补 probe 后再验收。两个模型阶段均为 gpt-6.1-sol / high，不会回退主会话模型。

## feedback.json 的内部字段

现有 score、feedback、issues、process、note 保留；新增三项仅供核对，不进入平台或结果表正文。

- results：至少一条 {sentence, requirement_ids, anchors}。sentence 是正文中说明最终行为的连续原文，requirement_ids 指向真实需求；anchors 至少含产物文件、缺失文件、运行日志、probe、演示或失败用例证据。不能只填需求编号或过程步骤。
- score_basis：{category, delivered, remaining, why_this_score, lowest_supported_score, lower_score_rejected, anchors}。category 为 delivery_issue、verification_gap 或 complete；其余分别解释实际完成部分、真实残余问题或验证边界、为何处于该档。lowest_supported_score 必须等于 score；lower_score_rejected 至少 8 字，解释为什么不能更低（1 分说明已最低）；anchors 必须真实有效。确证前提下选择最低合理档位。已纠正且无残余影响的过程错误不能单独支持降档。
- test_claims：正文有测试描述时逐处登记 {sentence, requirement_ids, behavior, source, output_excerpt, count, count_quote}，没有时为 []。sentence 要覆盖完整相关分句，不能只标出 make test 而漏掉后面的通过结论。behavior 说明检查的本题行为；output_excerpt 是该次执行真实输出中的连续摘录。

source 仅用以下一种：{"kind":"trace","step":6}、{"kind":"probe","id":"P1"}、{"kind":"tests_after"}、{"kind":"demo","index":1}。都指向当前事实包记录的对应输出，不能拿另一次执行补计数。只有源码、没有输出则补充定向 probe 或不写通过断言。

描述从未测试或末次改动后未重测时，允许 {"kind":"verification"}，output_excerpt 引用 facts.verification 中对应的实际字段（例如 "verified_after_last_edit": false）。该来源只能支持未执行/未重测的事实，不能支持通过状态或数量；语义复核仍需回查完整轨迹。不能为没有发生的测试编造输出。

正文提到数量时 count 为整数，count_quote 是同一次输出的计数原行；不报数量时 count 为 null，count_quote 为空。脚本检查摘录存在和数字出处，独立复核再检查该数字的含义、执行是否成功、测试与题目的关系，不能把日志里任意相同数字当测试数。

process 的 steps 仍指向真实工具步骤。没有任何工具步骤的特殊会话，允许 {sentence, steps:[], source:"conversation"}，独立复核须回查原始对话；有步骤时不得使用此例外。issues 只写真实功能/交付问题，不用未验证冒充缺陷。唯一限制是验证不足的 4 分可留空 issues，但须通过 verification_gap 条件及独立复核。

## 独立语义复核输出

复核返回 {checks:[{id,passed,reason}], claim_reviews:[...]}，七个 id 必须各出现一次，passed 是布尔值，reason 给出本题具体证据或需修正的原句，不接受只有符合要求。

| id | 核对内容 |
| --- | --- |
| result_and_process | 正文同时解释最终行为与真实过程；过程选择能说明结果或验证可信度 |
| task_specific_voice | 事实决定句序，读起来是具体判断，没有空泛赞美、装饰细节或机械褒贬配额 |
| test_relevance | 每处测试描述对应本题行为，执行来源、成功状态、计数及结论范围一致 |
| score_alignment | 分数、问题严重度、剩余工作一致，既拦无证据压分，也拦有证据却给高；纠正的过程失误不自动扣分；验证不足不伪装成缺陷 |
| evidence_fidelity | 结果和过程原句均由相应证据支持，风险不升级成已发生事故，没有虚假实跑 |
| scope_discipline | 只评价本轮约定和受影响兼容路径，不把可选增强、工程故障当缺项 |
| non_template | 与其他反馈比较，未复用开头、转折、收尾或仅替换技术名词和数字的框架 |

全部通过才写有效回执。质量文件绑定输入摘要、audit/feedback 摘要、生成规则摘要及指定模型参数，随交付导出。旧结果缺少该回执或基于旧规则生成时，阻止再次交付并提示重新验收；不静默改动已有评价或平台记录。

无 AI 痕迹在执行中指消除可观察的套话与模板、保持自然和具体；不使用不可验证的作者来源断言作为通过条件。词表只拦确定的空泛短语，语义复核承担自然表达判断，不靠扩大禁词表代替。

## 逐句回查，防止分析结论幻觉

claim_reviews 按脚本给出的 public_units 顺序覆盖全部正文，不能只登记有锚点的句子。每项为 {sentence, assertions, verdict, reason, later_check, sources:[{path,excerpt}]}。assertions 拆出该句每个可核实的事实和因果断言；reason 解释原文如何支持全部断言；later_check 说明后续是否修正及最终状态，不能只看首次报错。verdict 为 supported、contradicted 或 insufficient，只有全部 supported 才通过。复核者独立阅读原文，不采信生成侧自报的分析结论。每个过程不满意断言必须在这第二轮重新配对工具输入与返回，检查完整相关后续的重试、修正和最终影响；不只核查错误首次出现的片段。先用覆盖全部会话的索引定位，再定向读取原文，避免两轮重复通读无关大文件。索引不作为 sources；结果静态证据充分时无需为同一结论启动服务。

sources 引用本题原始会话、工具返回、命令日志、probe、题面、diff、录屏元数据或产物代码，保存相对路径与连续原文。脚本拒绝生成摘要、越界路径、符号链接和原文不存在的摘录；引用存在只能证明出处，不自动证明语义正确。语义复核必须检查所有断言是否被支持，以及正文是否漏掉已确证的重要过程/产物问题。

重点复查：调用是否真正成功、后面有没有重试并修好、当前文件是否仍有缺陷、基线是否本来如此、失败是否来自验收环境，以及局部输入能否支持所写影响范围。声称“未实现”须核对实际入口和调用链；声称“从未/没有再验证”须查完整相关后续会话；业务影响与因果需有机制或复现支持。找不到证据时删除或缩小断言，需实跑则返回 probe，不以常识或模型推断补齐。

逐句记录保存在 quality.json 和失败版本的 history 中，随正文、原始材料及规则摘要绑定。七项总体通过但逐句记录缺失、摘录伪造或仍有不确定断言，同样拒绝交付。该机制减少幻觉风险，不宣称脚本能自动证明任意语义结论。
