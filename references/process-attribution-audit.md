# 模型过程归因审计

Feedback 的过程句只描述被测模型在这次轨迹中做了什么、为什么没有成立、后来怎样修正，以及最终留下什么状态。验收 probe、录屏操作、当前任务 AI 的检查动作和产品最终现象可以支撑结果或问题句，但不能改写成模型行为。

## 五类必查错误

1. **基线归因错误**

   先看 `task.json.initial_commit` 对应文件，再判断某行为是基线已有，还是模型引入。不要把仓库原有契约写成“模型最初让接口……”。准确写法是“模型沿用了已有契约，但新页面继续按另一种请求体发送”。

2. **验收证据混入模型过程**

   `evidence/probes.jsonl`、录屏和当前任务 AI 的验收动作不在模型轨迹里。过程句不得出现“独立 probe”“边界 probe”“验收侧”等结果并把它接到模型主语链上。probe 只用于 `results`、`issues`、`score_basis` 和逐句来源。

3. **页面操作幻觉**

   只有原始轨迹出现 Playwright、浏览器、`page.click`、`locator` 等可核对的浏览器动作，才能写“模型点击/在页面操作”。仅有 typecheck、build、Vite 启动或代理 curl 时，只能写“模型加入页面代码”“模型通过代理接口检查”，最终页面现象另写为产物结果。

4. **时间线错误**

   “最初”“随后”“后来”“收尾”“最终”必须按完整步骤顺序核对。某错误发生在中段，后面仍有代码修改和验证时，不能写成“收尾时”。模型曾检查过某状态、但后续修改造成回归时，应写“检查过，后来修改后回归”，不能写“从未核对”。

5. **绝对化范围错误**

   “只运行了”“仅覆盖”“从未”“没有核对”必须先扫描全部主/子轨迹和获准续写。遗漏一项 typecheck、build、HTTP、Vite 代理或数据库调用都可能让该句失实；无法穷尽时改成具体范围，例如“没有进行浏览器级按钮操作”。

## 强制记录

`feedback.json.process` 每项必须先用 `evidence` 绑定真实工具步骤或助手文本：

```json
{
  "sentence": "正文里的完整过程句",
  "steps": [4, 6],
  "evidence": [
    {"kind": "tool", "step": 4, "field": "output", "excerpt": "FAILED (failures=1)"},
    {"kind": "tool", "step": 6, "field": "output", "excerpt": "Ran 14 tests in 0.52s OK"}
  ]
}
```

- `steps` 必须覆盖全部工具 evidence 的步骤；每个声明的步骤都至少有一条 evidence，不能只列一个无关 step 号。
- `field` 只能是 `tool`、`target`、`output`、`error`，摘录必须逐字落在该步骤对应的真实工具名、输入目标、输出尾部或错误文本中。
- 没有工具步骤的特殊会话才允许 `steps: []`，并必须使用 `{"kind":"assistant","index":1,"excerpt":"真实助手文本摘录"}`。
- 独立逐句复核时，过程句的 `claim_reviews.sources` 必须引用原始 `trace/projects/*.jsonl`，并逐条包含上述 evidence 的同一段摘录。只写一个存在的 step 号、引用另一段无关日志，不能通过。

`feedback.json.process_audit` 必须与 `process` 等长、同序、同句，并逐项记录：

```json
{
  "sentence": "与 process 完全一致的句子",
  "subject": "model",
  "verdict": "supported",
  "checks": {
    "full_trace": true,
    "baseline": true,
    "timeline": true,
    "source_boundary": true,
    "scope": true
  },
  "note": "说明回查了哪些原始步骤、基线或后续状态"
}
```

五项检查是语义判断，不能替代上面的逐句原文 evidence：

- `full_trace`：检查全量主轨迹和获准续写，而不是只看一条错误片段。
- `baseline`：需要因果或“谁引入”判断时，对照初始提交和最终 diff。
- `timeline`：需要“最初/随后/收尾/最终”判断时，回查前后步骤及后续修改。
- `source_boundary`：确认句子只写模型轨迹，不混入 probe、录屏或验收侧动作。
- `scope`：出现“只、仅、从未、没有”时，已扫描完整轨迹验证范围。

## 为什么这样绑定

旧方案只检查 step 号是否存在，文字含义仍可能借另一段日志绕过去。现在每个过程句至少有一条可定位到指定步骤输入/输出的连续摘录，并由复核轮再次引用同一摘录；这样能机械拦截 step 张冠李戴、把验收 probe 混入模型过程和只凭摘要写过程句。它仍不能自动证明因果或“从未”等语义，所以 `process_audit`、`claim_reviews.later_check` 和完整后续回查继续保留。

## 写作改正示例

- 错：模型最初只让动作接口接受嵌套 `values`。
- 对：模型沿用了仓库已有的嵌套 `values` 契约，但新页面继续发送扁平字段，真实 HTTP 联调因此失败。

- 错：收尾的浏览器操作仍使用扁平请求，点击启动后页面停在草稿。
- 对：模型保留的前端仍然发送扁平 `action`；最终页面联调收到空动作并停在草稿。

- 错：模型只运行了服务层脚本和前端 typecheck。
- 对：模型做了服务层脚本、typecheck、生产构建和 Vite 代理 curl，但没有在浏览器中实际点击权限编排按钮。

- 错：模型收尾只核对了代次，没有核对失效状态。
- 对：模型在刷新链中核对过模板和规则状态，但后续改动留下的最终实现仍让规则清单显示有效，两个入口继续不一致。

- 错：修正后两个边界 probe 通过。
- 对：模型重跑 10 项后端测试并通过；两个边界 probe 由验收侧另行通过，只作为结果证据。
