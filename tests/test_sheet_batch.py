"""fbr_sheet（结果表/平台回执衔接）与 fbr_batch（进度、抽检预览、抽检登记）。

不联网、不起容器、不推送：「已推送」是直接改 review/snapshot.json 摆出来的。
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from openpyxl import load_workbook

from fixtures import BATCH, OWNER, PROMPT
from flow import TEXT_A, TEXT_B, FlowCase, feedback, run_main
from test_record_facts import BAD, GOOD, SID

import fbrlib as lib
import fbr_batch
import fbr_feedback
import fbr_sheet
import fbr_delivery

TRACE = f"batches/{BATCH}/s11-000001/trace/projects/-workspace/{SID}.jsonl"
VIDEO = f"batches/{BATCH}/s11-000001/record/recording.mp4"
DELIVERY_TRACE = TRACE.replace("batches/", "results/", 1)
DELIVERY_VIDEO = VIDEO.replace("batches/", "results/", 1)
NOT_PUSHED = "缺产物快照；缺运行录屏；产物快照还没推送"


class SheetCase(FlowCase):
    def complete(self, task_dir) -> str:
        """把推送、录屏文件、运行起止时间补齐，返回产物快照链接。"""
        path = task_dir / "review" / "snapshot.json"
        snapshot = lib.load_json(path)
        link = f"https://github.com/{OWNER}/{task_dir.name}-clipcut/commit/{snapshot['commit']}"
        snapshot.update(pushed=True, artifact_snapshot=link)
        lib.save_json(path, snapshot)
        (task_dir / "record" / "recording.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
        state = lib.load_state(task_dir)
        state["stages"]["run"].update(started_at="2026-11-10T14:00:00+0800", ended_at="2026-11-10T15:30:00+0800",
                                      attempts=1)
        lib.save_json(task_dir / "state.json", state)
        self.refresh_source_fixture(task_dir)
        self.review_receipt(task_dir)
        return link

    def row(self, task_dir):
        return fbr_sheet.build_row(self.root, task_dir, self.cfg)

    def patch_json(self, path, **changes) -> None:
        doc = lib.load_json(path, default={})
        doc.update(changes)
        lib.save_json(path, doc)


class Columns(unittest.TestCase):
    def test_public_columns_match_the_sheet(self):
        self.assertEqual(fbr_sheet.PUBLIC_COLUMNS, [
            "User Prompt", "所属领域", "子方向", "任务类型", "任务难度", "语言/框架", "Harness", "Harness 版本", "操作系统",
            "环境可复现等级", "初始环境快照", "SessionID", "TurnID/PromptID", "轨迹文件", "产物快照", "运行录屏",
            "交付完整性", "Feedback", "备注", "提交人", "提交时间"])
        self.assertEqual(fbr_sheet.INTERNAL_COLUMNS, [
            "任务编号", "批次", "底座仓库", "任务仓库", "运行开始", "运行结束", "提交截止", "尝试次数", "证据路径",
            "闸门状态", "抽检状态", "数据状态", "上传状态"])
        self.assertFalse(set(fbr_sheet.PUBLIC_COLUMNS) & set(fbr_sheet.INTERNAL_COLUMNS))

    def test_options(self):
        self.assertEqual(fbr_sheet.OPTIONS["所属领域"], ["安全", "游戏、图形与多媒体", "数据工程"])
        self.assertEqual(fbr_sheet.OPTIONS["子方向"], ["身份认证", "网络与通信安全", "系统安全", "漏洞检测与攻防", "其他",
                                                  "游戏开发", "图形渲染", "音视频处理", "图像处理", "数据采集、爬虫与 RPA",
                                                  "数仓与数据湖", "数据分析与数据应用"])
        self.assertTrue({"漏洞检测与攻防", "音视频处理", "图形渲染", "数据采集、爬虫与 RPA", "其他"}
                        <= set(fbr_sheet.OPTIONS["子方向"]))
        self.assertEqual(len(fbr_sheet.OPTIONS["任务类型"]), 11)
        self.assertIn("泛Agent需求", fbr_sheet.OPTIONS["任务类型"])
        self.assertEqual(fbr_sheet.OPTIONS["任务难度"], ["困难", "地狱"])
        self.assertEqual(fbr_sheet.OPTIONS["环境可复现等级"], ["无外部依赖", "有外部依赖，未容器化", "已容器化，可一键起环境"])
        self.assertTrue(set(fbr_sheet.OPTIONS) <= set(fbr_sheet.PUBLIC_COLUMNS))

    def test_times(self):
        self.assertEqual(fbr_sheet.show_time("2026-11-10T15:04:05+0800"), "2026-11-10 15:04")
        self.assertEqual(fbr_sheet.show_time("2026-11-10T15:04:05.123456+08:00"), "2026-11-10 15:04")
        self.assertEqual(fbr_sheet.show_time("2026-11-10T15:04:05.123456Z"), "2026-11-10 15:04")
        self.assertEqual(fbr_sheet.show_time("2026-11-10T15:04:05"), "2026-11-10 15:04")
        self.assertEqual(fbr_sheet.show_time("昨天"), "")
        self.assertEqual(fbr_sheet.show_time(""), "")
        self.assertEqual(fbr_sheet.deadline("2026-11-10T15:00:00+0800", "20:00"), "2026-11-10 20:00")
        self.assertEqual(fbr_sheet.deadline("2026-11-10T20:00:00+0800", "20:00"), "2026-11-10 20:00")
        self.assertEqual(fbr_sheet.deadline("2026-11-10T21:00:00+0800", "20:00"), "2026-11-11 20:00")
        self.assertEqual(fbr_sheet.deadline("2026-11-30T23:10:00+0800", "18:30"), "2026-12-01 18:30")
        self.assertEqual(fbr_sheet.deadline("", "20:00"), "")

    def test_note_excludes_all_engineering_facts(self):
        self.assertEqual(fbr_sheet.build_note({"note": "人工说明"},
            {"history_rewritten": True, "no_change": True, "excluded": ["vendor/x"]},
            {"setup_ok": False, "steps": [{"cut": True}]},
            [{"verdict": "retry", "reason": "网关错误"}, {"verdict": "ok"}]), "")

    def test_explicit_platform_aliases_only(self):
        self.assertEqual(fbr_sheet.canonical_field("操作系统", "Linux arm64"), "MacOS/Linux")
        self.assertEqual(fbr_sheet.canonical_field("子方向", "数据采集、爬虫与RPA"), "数据采集、爬虫与 RPA")
        self.assertEqual(fbr_sheet.canonical_field("子方向", "不明方向"), "不明方向")

    def test_row_problems(self):
        link = "https://github.com/demo-owner/s11-000001-clipcut/commit/" + "a" * 40
        row = {name: "x" for name in fbr_sheet.PUBLIC_COLUMNS}
        row.update({"所属领域": "安全", "子方向": "其他", "任务类型": "Bugfix", "任务难度": "地狱", "操作系统": "MacOS/Linux",
                    "环境可复现等级": "无外部依赖", "初始环境快照": link, "产物快照": link, "Harness 版本": "2.1.319",
                    "交付完整性": 4, "备注": "", "抽检状态": "通过"})
        problems = fbr_sheet.row_problems
        self.assertEqual(problems(row, {"user_turns": 1}, {"pushed": True}), [])
        self.assertEqual(problems(row, {}, {}), [])
        self.assertEqual(problems({**row, "Feedback": "", "提交人": None}, {}, {}), ["缺Feedback"])
        self.assertEqual(problems({**row, "任务难度": "中等", "子方向": "音频"}, {}, {}),
                         ["子方向的取值不在选项里：音频", "任务难度的取值不在选项里：中等"])
        self.assertEqual(problems({**row, "操作系统": "macOS"}, {}, {}), ["操作系统的取值不在选项里：macOS"])
        self.assertEqual(problems({**row, "初始环境快照": link[:-1], "产物快照": link.replace("/commit/", "/tree/")}, {}, {}),
                         ["初始环境快照不是提交链接", "产物快照不是提交链接"])
        self.assertEqual(problems({**row, "Harness 版本": "2.1.319 (Claude Code)"}, {}, {}), ["Harness 版本只填纯版本号"])
        self.assertEqual(problems(row, {"user_turns": 2}, {}), ["轨迹里有 2 轮用户输入"])
        self.assertEqual(problems(row, {}, {"pushed": False, "commit": "abc"}), ["产物快照还没推送"])
        self.assertEqual(problems({**row, "抽检状态": "退回"}, {}, {}), ["抽检被退回"])


class BuildRow(SheetCase):
    def test_not_gated(self):
        self.assertEqual(self.row(self.written()), (None, "Feedback 还没过闸门"))

    def test_full_row(self):
        task_dir = self.gated()
        link = self.complete(task_dir)
        row, reason = self.row(task_dir)
        self.assertEqual(reason, "")
        self.assertEqual(list(row), [*fbr_sheet.PUBLIC_COLUMNS[:-2], "提交人", "提交时间",
                                     *[c for c in fbr_sheet.INTERNAL_COLUMNS if c != "数据状态"], "数据状态"])
        self.assertEqual(row.pop("提交时间"), "")
        initial = lib.load_task(task_dir)["initial_commit"]
        self.assertEqual(row, {
            "User Prompt": PROMPT, "所属领域": "游戏、图形与多媒体", "子方向": "音视频处理", "任务类型": "功能开发",
            "任务难度": "困难", "语言/框架": "Python / ffmpeg", "Harness": "Claude Code", "Harness 版本": "2.1.319",
            "操作系统": "MacOS/Linux", "环境可复现等级": "已容器化，可一键起环境",
            "初始环境快照": f"https://github.com/{OWNER}/s11-000001-clipcut/commit/{initial}",
            "SessionID": SID, "TurnID/PromptID": "prm-0001", "轨迹文件": TRACE, "产物快照": link, "运行录屏": VIDEO,
            "交付完整性": 3, "Feedback": TEXT_A, "备注": "", "提交人": "",
            "任务编号": "s11-000001", "批次": BATCH, "底座仓库": "s11r001-clipcut",
            "任务仓库": f"{OWNER}/s11-000001-clipcut", "运行开始": "2026-11-10 14:00", "运行结束": "2026-11-10 15:30",
            "提交截止": "2026-11-10 20:00", "尝试次数": 1, "证据路径": f"batches/{BATCH}/s11-000001/evidence",
            "闸门状态": "通过", "抽检状态": "未抽检", "上传状态": "未提交", "数据状态": "可提交"})

    def test_not_pushed_and_no_video(self):
        row, _ = self.row(self.gated())
        self.assertEqual((row["产物快照"], row["运行录屏"], row["数据状态"]), ("", "", NOT_PUSHED))
        self.assertEqual((row["运行开始"], row["运行结束"], row["提交截止"], row["尝试次数"]), ("", "", "", ""))

    def test_changes_after_the_gate_void_it(self):
        task_dir = self.gated()
        self.put(task_dir, feedback_doc=feedback(TEXT_B))
        self.assertEqual(self.row(task_dir), (None, "过闸门之后正文改过，要重新过一遍"))
        self.put(task_dir, feedback_doc=feedback(TEXT_A, score=2))
        self.assertEqual(self.row(task_dir), (None, "过闸门之后分数改过，要重新过一遍"))
        self.put(task_dir, feedback_doc=feedback())
        self.assertIsNotNone(self.row(task_dir)[0])

    def test_spotcheck_counts_only_for_this_text(self):
        task_dir = self.gated()
        spot = task_dir / "review" / "spotcheck.json"
        lib.save_json(spot, {"state": "通过", "by": "测试员", "feedback_sha": fbr_feedback.sha(TEXT_B)})
        self.assertEqual(self.row(task_dir)[0]["抽检状态"], "未抽检")
        self.patch_json(spot, feedback_sha=fbr_feedback.sha(TEXT_A))
        self.assertEqual(self.row(task_dir)[0]["抽检状态"], "通过")
        self.patch_json(spot, state="退回")
        row, _ = self.row(task_dir)
        self.assertEqual((row["抽检状态"], row["数据状态"]), ("退回", NOT_PUSHED + "；抽检被退回"))

    def test_other_sources(self):
        task_dir = self.gated()
        self.complete(task_dir)
        self.patch_json(task_dir / "trace" / "ids.json", cli_version="2.1.320", user_turns=2)
        self.patch_json(task_dir / "review" / "upload.json", state="已上传")
        self.patch_json(task_dir / "review" / "feedback.json", note="模型中途自己装过一次依赖")
        self.review_receipt(task_dir)
        self.patch_json(task_dir / "review" / "snapshot.json", history_rewritten=True)
        lib.save_json(task_dir / "run" / "attempts.json", [{"verdict": "retry", "reason": "网关返回 529"}, {"verdict": "ok"}])
        row, _ = self.row(task_dir)
        self.assertEqual((row["Harness 版本"], row["上传状态"], row["尝试次数"]), ("2.1.320", "回执已失效", 1))
        self.assertEqual(row["备注"], "")
        self.assertEqual(row["数据状态"], "轨迹里有 2 轮用户输入")
        self.patch_json(task_dir / "trace" / "ids.json", cli_version="v2.1.320 (Claude Code)", user_turns=1,
                        session_file="projects/-workspace/missing.jsonl")
        self.review_receipt(task_dir)
        row, _ = self.row(task_dir)
        self.assertEqual((row["轨迹文件"], row["数据状态"]), ("", "缺轨迹文件；Harness 版本只填纯版本号"))


class ReceiptRow(SheetCase):
    @staticmethod
    def receipt(**changes):
        status = {"valid": True, "state": "已提交", "trace": "https://media.example.invalid/trace.jsonl",
                  "video": "https://media.example.invalid/recording.mp4", "submitter": "platform-user",
                  "submitted_at": "2026-11-11T16:08:09+0800"}
        status.update(changes)
        return status

    def manifest(self, task_dir):
        manifest = {"schema_version": 1, "batch_id": BATCH, "tasks": [{"task_id": task_dir.name}], "excluded": []}
        lib.save_json(fbr_delivery.delivery_dir(self.root, BATCH) / "delivery-manifest.json", manifest)
        return manifest

    def test_valid_receipt_fills_remote_media_actual_account_and_time(self):
        task_dir = self.gated()
        self.complete(task_dir)
        self.cfg["submitter"] = ""
        manifest = self.manifest(task_dir)
        local_rows = []

        def receipt_status(root, task, row, current_manifest):
            self.assertEqual((root, task, current_manifest), (self.root, task_dir, manifest))
            local_rows.append(dict(row))
            return self.receipt()

        with mock.patch("fbr_submit.receipt_status", side_effect=receipt_status) as check:
            row, reason = fbr_sheet.build_row(self.root, task_dir, self.cfg, delivery_manifest=manifest)
        check.assert_called_once()
        self.assertEqual(reason, "")
        self.assertEqual(local_rows[0]["提交人"], "")
        self.assertEqual(row["轨迹文件"], self.receipt()["trace"])
        self.assertEqual(row["运行录屏"], self.receipt()["video"])
        self.assertEqual((row["提交人"], row["提交时间"], row["上传状态"], row["数据状态"]),
                         ("platform-user", "2026-11-11 16:08", "已提交", "可提交"))

    def test_build_row_loads_current_manifest_for_receipt(self):
        task_dir = self.gated()
        self.complete(task_dir)
        manifest = self.manifest(task_dir)
        with mock.patch("fbr_submit.receipt_status", return_value=self.receipt()) as check:
            row, _ = self.row(task_dir)
        self.assertEqual(check.call_args.args[3], manifest)
        self.assertEqual(row["上传状态"], "已提交")

    def test_invalid_receipt_keeps_local_values_for_body_or_attachment_drift(self):
        task_dir = self.gated()
        self.complete(task_dir)
        self.manifest(task_dir)
        invalid = self.receipt(valid=False, state="回执已失效")
        for changed in ("body", "attachment"):
            with self.subTest(changed=changed):
                if changed == "body":
                    self.put(task_dir, feedback_doc=feedback(TEXT_B))
                    self.assertTrue(fbr_feedback.gate_batch(self.root, BATCH, self.cfg)[0][0]["state"] == "ok")
                else:
                    (task_dir / "record" / "recording.mp4").write_bytes(b"new attachment bytes")
                with mock.patch("fbr_submit.receipt_status", return_value=invalid) as check:
                    row, reason = self.row(task_dir)
                if changed == "attachment":
                    self.assertIsNone(row)
                    self.assertIn("证据变化", reason)
                    check.assert_not_called()
                    continue
                self.assertEqual(reason, "")
                self.assertEqual(check.call_args.args[2]["Feedback"], TEXT_B)
                self.assertEqual((row["轨迹文件"], row["运行录屏"], row["提交人"], row["上传状态"]),
                                 (TRACE, VIDEO, "", "回执已失效"))
                self.assertNotEqual(row["提交时间"], "2026-11-11 16:08")

    def test_legacy_string_receipt_without_manifest_is_not_trusted(self):
        task_dir = self.gated()
        self.complete(task_dir)
        lib.save_json(task_dir / "review" / "upload.json", self.receipt(state="已上传"))
        row, _ = self.row(task_dir)
        self.assertEqual((row["轨迹文件"], row["运行录屏"], row["提交人"]), (TRACE, VIDEO, ""))
        self.assertEqual(row["上传状态"], "回执已失效")

    def test_local_missing_submitter_remains_visible_before_platform_submission(self):
        task_dir = self.gated()
        self.complete(task_dir)
        self.cfg["submitter"] = ""
        with mock.patch("fbr_submit.receipt_status", return_value={"valid": False, "state": "未提交"}):
            row, _ = self.row(task_dir)
        self.assertEqual((row["提交人"], row["数据状态"], row["上传状态"]), ("", "可提交", "未提交"))


class WriteSheet(SheetCase):
    def test_sheet(self):
        self.complete(self.gated(1, TEXT_A))
        self.gated(2, TEXT_B)
        self.prepared(3)
        self.written(4, TEXT_A, score=5)
        fbr_feedback.gate_batch(self.root, BATCH, self.cfg, "s11-000004")
        self.make_task(5, repo=False, image="")
        out = fbr_sheet.write_sheet(self.root, BATCH, self.cfg)
        target = self.root / "results" / BATCH / "result.xlsx"
        self.assertEqual(out, {"path": target, "tasks": 5, "rows": 2, "ready": 1, "pending": [("s11-000002", NOT_PUSHED)]})

        book = load_workbook(target)
        self.assertEqual(book.sheetnames, ["结果", "进度", "必填字段核查", "字段修订核对"])
        sheet, board = book["结果"], book["进度"]
        columns = fbr_sheet.PUBLIC_COLUMNS + fbr_sheet.INTERNAL_COLUMNS
        self.assertEqual([c.value for c in sheet[1]], columns)
        self.assertEqual((sheet.freeze_panes, board.freeze_panes, sheet.max_row), ("B2", "B2", 3))
        self.assertTrue(all(c.font.bold for c in sheet[1]))
        first = dict(zip(columns, (c.value for c in sheet[2])))
        second = dict(zip(columns, (c.value for c in sheet[3])))
        self.assertEqual((first["任务编号"], first["User Prompt"], first["交付完整性"], first["Feedback"], first["数据状态"]),
                         ("s11-000001", PROMPT, 3, TEXT_A, "可提交"))
        self.assertEqual((first["轨迹文件"], first["运行录屏"], first["提交人"], first["备注"]),
                         (DELIVERY_TRACE, DELIVERY_VIDEO, None, None))
        self.assertEqual((second["任务编号"], second["Feedback"], second["产物快照"], second["数据状态"]),
                         ("s11-000002", TEXT_B, None, NOT_PUSHED))
        self.assertTrue(sheet["R2"].alignment.wrap_text)

        self.assertEqual([c.value for c in board[1]],
                         ["任务编号", "运行", "收集", "提交", "录屏", "取证", "Feedback", "已推送", "卡在哪"])
        lines = [[c.value for c in line] for line in board.iter_rows(min_row=2)]
        self.assertEqual(lines[0], ["s11-000001", "✓", "✓", "✓", "✓", "✓", "✓", "✓", None])
        self.assertEqual(lines[1], ["s11-000002", "✓", "✓", "✓", "✓", "✓", "✓", None, NOT_PUSHED])
        self.assertEqual(lines[2], ["s11-000003", "✓", "✓", "✓", "✓", None, None, None, "Feedback 还没过闸门"])
        self.assertEqual(lines[3][:8], ["s11-000004", "✓", "✓", "✓", "✓", "✓", "✗", None])
        self.assertTrue(lines[3][8].startswith("Feedback："))
        self.assertIn("5 分不写缺口：issues 应当为空", lines[3][8])
        self.assertEqual(lines[4], ["s11-000005", None, None, None, None, None, None, None, "s11-000005/task.json 缺少 image"])

    def test_regenerated_from_source(self):
        task_dir = self.gated()
        fbr_sheet.write_sheet(self.root, BATCH, self.cfg)
        self.put(task_dir, feedback_doc=feedback(TEXT_B))                # 过闸门之后又改了正文
        out = fbr_sheet.write_sheet(self.root, BATCH, self.cfg)
        self.assertEqual((out["rows"], out["ready"], out["pending"]), (0, 0, []))
        book = load_workbook(out["path"])
        self.assertEqual(book["结果"].max_row, 1)
        self.assertEqual(book["进度"]["I2"].value, "过闸门之后正文改过，要重新过一遍")

    def test_main(self):
        self.complete(self.gated(1, TEXT_A))
        self.gated(2, TEXT_B)
        code, out, err = run_main(fbr_sheet, "--root", str(self.root), "--batch", BATCH)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), [str(self.root / "results" / BATCH / "result.xlsx"),
                                            "批次共 2 题，进结果页 2 题，其中可提交 1 题", f"  s11-000002  {NOT_PUSHED}"])
        code, out, err = run_main(fbr_sheet, "--root", str(self.root), "--batch", "b1111-09")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("错误：批次不存在：", err)


class Status(SheetCase):
    def printed(self) -> list[str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fbr_batch.print_status(fbr_batch.status(self.root, BATCH), BATCH)
        return out.getvalue().splitlines()

    def test_next_stage(self):
        task_dir = self.make_task(repo=False)
        self.assertEqual(fbr_batch.next_stage(task_dir), "run")
        self.mark(task_dir, "collect")
        self.assertEqual(fbr_batch.next_stage(task_dir), "commit")
        lib.set_stage(task_dir, "commit", "dry")                         # 演练不算做完
        self.assertEqual(fbr_batch.next_stage(task_dir), "commit")
        self.mark(task_dir, "feedback")
        self.assertIsNone(fbr_batch.next_stage(task_dir))

    def test_status(self):
        self.gated(1)
        lib.set_stage(self.make_task(2, repo=False), "run", "failed", reason="网关返回 529")
        self.prepared(3)
        self.written(4, TEXT_B, score=5)
        fbr_feedback.gate_batch(self.root, BATCH, self.cfg, "s11-000004")
        rows = fbr_batch.status(self.root, BATCH)
        self.assertEqual(rows[:3], [
            {"task_id": "s11-000001", "next": None, "failed": False, "detail": "", "pushed": False, "committed": True},
            {"task_id": "s11-000002", "next": "run", "failed": True, "detail": "网关返回 529", "pushed": False,
             "committed": False},
            {"task_id": "s11-000003", "next": "facts", "failed": False, "detail": "", "pushed": False, "committed": True}])
        self.assertEqual((rows[3]["next"], rows[3]["failed"]), ("feedback", True))
        self.assertIn("；", rows[3]["detail"])
        lines = self.printed()
        self.assertEqual(lines[:3], ["s11-000001  都做完了  产物快照未推送", "s11-000002  等运行（上次没过：网关返回 529）",
                                     "s11-000003  等取证  产物快照未推送"])
        self.assertTrue(lines[3].startswith("s11-000004  等Feedback（上次没过："))
        self.assertTrue(lines[3].endswith("）  产物快照未推送"))
        self.assertEqual(lines[4:], [
            "—— 都做完了 1 题，等运行 1 题，等取证 1 题，等Feedback 1 题",
            f"下一步（运行）：fbr_run.py run --batch {BATCH} --execute",
            f"下一步（取证）：fbr_facts.py --batch {BATCH}",
            f"下一步（Feedback）：fbr_feedback.py card --batch {BATCH}，按 references/reviewer.md 调用指定验收模型后 "
            f"fbr_feedback.py gate --batch {BATCH}",
            f"待推送：fbr_snapshot.py push --batch {BATCH}（演练；真推要用户明确同意后加 --execute --confirm PUBLISH）"])

    def test_all_done(self):
        self.complete(self.gated())
        self.assertEqual(self.printed(), ["s11-000001  都做完了", "—— 都做完了 1 题", f"下一步：fbr_sheet.py --batch {BATCH}"])


class Preview(SheetCase):
    def page(self, wanted=None) -> str:
        target = fbr_batch.preview(self.root, BATCH, wanted)
        self.assertEqual(target, self.root / "results" / BATCH / "preview.html")
        return target.read_text(encoding="utf-8")

    def test_nothing_to_show(self):
        self.prepared()
        with self.assertRaisesRegex(lib.FbrError, "还没有当前有效且已过 Feedback 闸门的任务，没什么可看的"):
            fbr_batch.preview(self.root, BATCH)
        self.assertFalse((self.root / "results" / BATCH / "preview.html").exists())

    def test_gated_task(self):
        self.complete(self.gated())
        self.prepared(2)                                                 # 没取证的不上页
        text = self.page()
        for piece in (
            f"<title>{BATCH} 抽检预览</title>", '<nav><a href="#s11-000001">s11-000001</a></nav>',
            '<section id="s11-000001"><h2>s11-000001<span class="score">3 分</span></h2>',
            "<p>游戏、图形与多媒体 / 音视频处理 / 功能开发 / 困难 / Python / ffmpeg</p>",
            '<p>闸门：<b class="ok">通过</b>　抽检：未抽检', f'<div class="fb">{TEXT_A}</div>',
            "<tr><td>R1</td><td>pack 支持 --segment 分片</td><td>做到</td><td></td></tr>",
            "<tr><td>R2</td><td>一小时以上的输入能处理</td><td>有问题（未实现）</td>"
            "<td>一小时以上的输入直接抛 RuntimeError，代码里只有 TODO</td></tr>",
            '<p class="bad">虚假成功（轻微）：回答里说新增了 docs/usage.md，产物里没有这个文件</p>',
            '<video controls preload="none" src="s11-000001/record/recording.mp4"></video>',
            '<p>产物测试：<b class="ok">通过</b>；新增失败 无；模型最后一次改动后没有再验证</p>',
            "<p>2. <code>make test</code> → 退出码 2</p><pre>FAILED</pre>",
            "改了 src/clipcut/packer.py，新增 src/clipcut/segment.py 和 docs/usage.md。",
            "A src/clipcut/segment.py", "</main></html>",
        ):
            self.assertIn(piece, text)
        self.assertNotIn("s11-000002", text)
        self.assertNotIn("还没有录屏", text)

    def test_legacy_section_escaping_and_unwritten_not_delivered(self):
        task_dir = self.evidenced(tests=[GOOD, BAD])
        self.patch_json(task_dir / "task.json", prompt='把 <video> 标签的 "src" & poster 补上')
        text = fbr_batch.task_section(self.root, task_dir, task_dir.parent)
        with self.assertRaisesRegex(lib.FbrError, "当前有效"):
            self.page()
        for piece in ('<span class="score">未打分 分</span>', '<b class="bad">还没过闸门</b>',
                      '<div class="fb">（还没写）</div>', "<p>还没有录屏</p>",
                      "<pre>把 &lt;video&gt; 标签的 &quot;src&quot; &amp; poster 补上</pre>",
                      "<tr><td>R3</td><td>非法参数报错退出</td><td></td><td></td></tr>",
                      '<b class="bad">不通过</b>；新增失败 test_long (tests.test_packer.T)；', "FAILED (failures=1)"):
            self.assertIn(piece, text)
        self.assertNotIn("<video", text)
        self.assertNotIn("虚假成功", text)

    def test_failed_gate_and_wanted(self):
        self.written(1, TEXT_A, score=5)
        task_dir = self.gated(2, TEXT_B)
        fbr_feedback.gate_batch(self.root, BATCH, self.cfg, "s11-000001")
        fbr_batch.spotcheck(self.root, BATCH, task_dir.name, "通过", "测试员", "看过 <diff>")
        text = self.page()
        self.assertNotIn('id="s11-000001"', text)
        self.assertNotIn('<b class="bad">没过</b>', text)
        self.assertIn("抽检：通过 测试员 看过 &lt;diff&gt;</p>", text)
        text = self.page("s11-000002")
        self.assertIn('<section id="s11-000002">', text)
        self.assertNotIn("s11-000001", text)


class Spotcheck(SheetCase):
    def check(self, state="通过", by="测试员", note="", task="s11-000001"):
        return fbr_batch.spotcheck(self.root, BATCH, task, state, by, note)

    def test_arguments(self):
        task_dir = self.gated()
        for kwargs, message in (({"state": "不行"}, "--state 只能是 通过 或 退回"),
                                ({"by": "  "}, "抽检是人看过之后的结论，要用 --by 写上是谁看的"),
                                ({"state": "退回", "note": " "}, "退回要用 --note 写清楚原因"),
                                ({"task": "s11-000009"}, "批次里没有这些任务：s11-000009")):
            with self.assertRaisesRegex(lib.FbrError, message):
                self.check(**kwargs)
        self.assertFalse((task_dir / "review" / "spotcheck.json").exists())

    def test_needs_a_gated_feedback(self):
        task_dir = self.written()
        with self.assertRaisesRegex(lib.FbrError, "s11-000001：feedback 阶段还没通过，不能往下做"):
            self.check()
        self.assertFalse((task_dir / "review" / "spotcheck.json").exists())

    def test_pass(self):
        task_dir = self.gated()
        row = self.check(by=" 测试员 ", note=" 改动和正文对得上 ")
        self.assertEqual({k: v for k, v in row.items() if k != "at"},
                         {"task_id": "s11-000001", "state": "通过", "by": "测试员", "note": "改动和正文对得上",
                          "feedback_sha": fbr_feedback.sha(TEXT_A)})
        self.assertEqual(lib.load_json(task_dir / "review" / "spotcheck.json"), row)
        self.assertEqual(self.stage(task_dir, "feedback")["state"], "ok")
        self.assertEqual(self.row(task_dir)[0]["抽检状态"], "通过")

    def test_reject_sends_it_back(self):
        task_dir = self.gated()
        self.check("退回", note="第 4 步之后的描述不准")
        stage = self.stage(task_dir, "feedback")
        self.assertEqual((stage["state"], stage["problems"]), ("failed", ["抽检退回：第 4 步之后的描述不准"]))
        self.assertEqual(self.row(task_dir), (None, "Feedback 还没过闸门"))
        self.assertEqual(fbr_batch.status(self.root, BATCH)[0]["detail"], "抽检退回：第 4 步之后的描述不准")
        row, = fbr_feedback.gate_batch(self.root, BATCH, self.cfg)[0]          # 正文没改，重过闸门也不放行
        self.assertEqual((row["state"], row["errors"]), ("failed", ["抽检退回过（第 4 步之后的描述不准），正文还没改"]))
        self.put(task_dir, feedback_doc=feedback(TEXT_B))
        self.assertEqual(fbr_feedback.gate_batch(self.root, BATCH, self.cfg)[0][0]["state"], "ok")
        self.assertEqual(self.row(task_dir)[0]["抽检状态"], "未抽检")              # 上次看的是旧正文，要重新抽检


class Upload(SheetCase):
    def test_plan_only_lists_files(self):
        first = self.gated(1, TEXT_A)
        self.complete(first)
        second = self.gated(2, TEXT_B)
        self.patch_json(second / "trace" / "ids.json", session_file="")
        self.refresh_source_fixture(second)
        self.review_receipt(second)
        self.written(3)                                                  # 没过闸门的不列
        self.assertEqual(fbr_batch.upload_plan(self.root, BATCH), [
            {"task_id": "s11-000001", "trace": DELIVERY_TRACE, "video": DELIVERY_VIDEO},
            {"task_id": "s11-000002", "trace": "", "video": ""}])
        self.assertFalse((first / "review" / "upload.json").exists())      # 只是列清单，不记成已上传
        self.assertEqual(self.row(first)[0]["上传状态"], "未提交")


class Main(SheetCase):
    def run_cli(self, *argv):
        return run_main(fbr_batch, "--root", str(self.root), *argv)

    def test_commands(self):
        task_dir = self.gated()
        self.complete(task_dir)
        code, out, err = self.run_cli("status", "--batch", BATCH)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[0], "s11-000001  都做完了")
        code, out, _ = self.run_cli("preview", "--batch", BATCH, "--tasks", task_dir.name)
        self.assertEqual((code, out.strip()), (0, str(self.root / "results" / BATCH / "preview.html")))
        planned = {"execute": False, "tasks": [{"task_id": task_dir.name, "state": "dry"}]}
        with mock.patch("fbr_submit.run_batch", return_value=planned) as submit:
            code, out, err = self.run_cli("upload", "--batch", BATCH)
        submit.assert_called_once_with(self.root, BATCH, tasks=None, limit=None, execute=False, confirm="")
        self.assertEqual((code, json.loads(out), err), (0, planned, ""))
        with mock.patch("fbr_submit.run_batch", return_value={"execute": True}) as submit:
            code, out, err = self.run_cli("upload", "--batch", BATCH, "--tasks", task_dir.name,
                                        "--limit", "1", "--execute", "--confirm", "SUBMIT")
        submit.assert_called_once_with(self.root, BATCH, tasks=task_dir.name, limit=1, execute=True, confirm="SUBMIT")
        self.assertEqual((code, json.loads(out), err), (0, {"execute": True}, ""))
        code, out, err = self.run_cli("spotcheck", "--batch", BATCH, "--task", task_dir.name, "--state", "通过")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("错误：抽检是人看过之后的结论，要用 --by 写上是谁看的", err)
        code, out, _ = self.run_cli("spotcheck", "--batch", BATCH, "--task", task_dir.name, "--state", "通过",
                                    "--by", "测试员")
        self.assertEqual((code, out.strip()), (0, "s11-000001  抽检通过  测试员"))

    def test_upload_reports_missing_files(self):
        self.gated()
        with mock.patch("fbr_submit.run_batch", side_effect=lib.FbrError("缺运行录屏")):
            code, out, err = self.run_cli("upload", "--batch", BATCH)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("错误：缺运行录屏", err)
        code, out, err = self.run_cli("preview", "--batch", "b1111-09")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("错误：批次不存在：", err)


if __name__ == "__main__":
    unittest.main()
