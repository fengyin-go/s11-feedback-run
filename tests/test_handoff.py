"""出题输入变化时阻止使用旧验收状态，旧目录继续兼容。"""
import copy
from unittest.mock import patch

from fixtures import Sandbox, BATCH
import fbrlib as lib
import fbr_run


class Handoff(Sandbox):
    def new_task(self):
        task_dir = self.make_task()
        record = lib.load_task(task_dir)
        record["image_id"] = self.cfg["image_id"]
        baseline = {k: record.get(k) for k in ("repo_id", "domain", "subdirection", "language", "frameworks",
                    "repro_level", "image", "image_id", "harness", "harness_version", "os")}
        baseline.update(commit=record["initial_commit"], tree=lib.git_out(task_dir / "workspace", "rev-parse", "HEAD^{tree}"))
        record.update(handoff_version=2, baseline=baseline, baseline_id=lib.content_hash(baseline))
        recording = lib.load_json(task_dir / "recording-plan.json")
        record["handoff_hash"] = lib.content_hash(lib.handoff_payload(record, recording))
        lib.save_json(task_dir / "task.json", record)
        (task_dir / "prompt.txt").write_text(record["prompt"])
        row = {k: record[k] for k in ("task_id", "repo_id", "initial_commit", "task_type", "difficulty",
                                   "baseline_id", "handoff_hash", "task_repo")}
        row.update(task_file=f"{record['task_id']}/task.json", workspace=f"{record['task_id']}/workspace")
        self.manifest = {"schema_version": 2, "batch_id": BATCH, "count": 1, "tasks": [row],
                         "baselines": {record["baseline_id"]: baseline}}
        lib.save_json(task_dir.parent / "batch-manifest.json", self.manifest)
        return task_dir, record

    def test_new_contract_and_legacy_both_read(self):
        self.assertEqual(lib.load_task(self.make_task(number=2))["task_id"], "s11-000002")
        task, record = self.new_task()
        self.assertEqual(lib.load_task(task), record)

    def test_prompt_requirements_or_recording_change_block(self):
        task, record = self.new_task()
        for field, value in (("prompt", record["prompt"] + "再加一项"), ("requirements", [{"id": "new"}]),
                             ("frameworks", "different"), ("initial_commit", "f" * 40)):
            changed = dict(record, **{field: value})
            lib.save_json(task / "task.json", changed)
            with self.subTest(field=field), self.assertRaises(lib.FbrError):
                lib.load_task(task)
        lib.save_json(task / "task.json", record)
        plan = lib.load_json(task / "recording-plan.json")
        plan["setup_cmd"] = "something else"
        lib.save_json(task / "recording-plan.json", plan)
        with self.assertRaisesRegex(lib.FbrError, "重新交接"):
            lib.load_task(task)

    def test_manifest_tampering_and_plain_prompt_mismatch_block(self):
        task, record = self.new_task()
        changed = copy.deepcopy(self.manifest)
        changed["tasks"][0]["baseline_id"] = "another"
        lib.save_json(task.parent / "batch-manifest.json", changed)
        with self.assertRaisesRegex(lib.FbrError, "baseline_id"):
            lib.load_task(task)
        lib.save_json(task.parent / "batch-manifest.json", self.manifest)
        (task / "prompt.txt").write_text("other")
        with self.assertRaisesRegex(lib.FbrError, "prompt.txt"):
            lib.load_task(task)

    def test_publication_refresh_does_not_change_handoff(self):
        task, record = self.new_task()
        record["publish"]["published_at"] = "later"
        lib.save_json(task / "task.json", record)
        self.assertEqual(lib.load_task(task)["publish"]["published_at"], "later")

    def test_image_digest_drift_blocks_before_run(self):
        task, record = self.new_task()
        cfg = dict(self.cfg, image_id="sha256:other")
        with patch.object(fbr_run, "container_exists", return_value=False):
            problems = fbr_run.ready_problems(task, record, cfg)
        self.assertTrue(any("image_id" in item for item in problems))

    def test_new_contract_cannot_downgrade_or_remove_version(self):
        task, record = self.new_task()
        for remove in (False, True):
            changed = dict(record, handoff_version=1)
            if remove:
                changed.pop("handoff_version")
            lib.save_json(task / "task.json", changed)
            with self.subTest(remove=remove), self.assertRaisesRegex(lib.FbrError, "不能降级或删除交接版本"):
                lib.load_task(task)


class SingleProducer(Sandbox):
    """New producer freezes one origin, one workspace and one main branch."""

    new_task = Handoff.new_task

    def single_task(self):
        from fixtures import sh
        task, record = self.new_task()
        sh(task, "clone", "--no-hardlinks", str(task / "workspace"), str(task / "origin"))
        record.update(producer="s11-gsb-prompt-create", execution_mode="single", branch="main",
                      origin=f"{task.name}/origin", workspace=f"{task.name}/workspace")
        record["handoff_hash"] = lib.content_hash(lib.handoff_payload(record, lib.load_json(task / "recording-plan.json")))
        row = self.manifest["tasks"][0]
        for key in ("producer", "execution_mode", "branch", "origin", "workspace", "handoff_hash"):
            row[key] = record[key]
        for key in ("producer", "execution_mode", "branch"):
            self.manifest[key] = record[key]
        lib.save_json(task / "task.json", record)
        lib.save_json(task.parent / "batch-manifest.json", self.manifest)
        return task, record

    def test_accept_single_contract_and_reject_ab_residue(self):
        task, record = self.single_task()
        self.assertEqual(lib.load_task(task), record)
        (task / "workspaceA").mkdir()
        with self.assertRaisesRegex(lib.FbrError, "A/B"):
            lib.load_task(task)

    def test_pre_domain_contract_handoff_hash_remains_compatible(self):
        task, record = self.single_task()
        legacy = dict(record, domain_contract=None, domain_gate_version=None)
        legacy["handoff_hash"] = lib.content_hash(lib.handoff_payload(
            legacy,
            lib.load_json(task / "recording-plan.json"),
            include_domain_contract=False,
        ))
        lib.save_json(task / "task.json", legacy)
        row = self.manifest["tasks"][0]
        row["handoff_hash"] = legacy["handoff_hash"]
        lib.save_json(task.parent / "batch-manifest.json", self.manifest)
        self.assertEqual(lib.load_task(task), legacy)

    def test_single_mode_and_path_drift_block(self):
        task, record = self.single_task()
        for field, value in (("execution_mode", "pairwise"), ("branch", "A"), ("workspace", "other/workspace"),
                             ("origin", "other/origin"), ("task_type", "功能迭代")):
            changed = dict(record, **{field: value})
            lib.save_json(task / "task.json", changed)
            with self.subTest(field=field), self.assertRaises(lib.FbrError):
                lib.load_task(task)
        lib.save_json(task / "task.json", record)

    def test_wrong_workspace_branch_blocks_start(self):
        from fixtures import sh
        task, record = self.single_task()
        sh(task / "workspace", "checkout", "-b", "A")
        with patch.object(fbr_run, "container_exists", return_value=False):
            problems = fbr_run.ready_problems(task, record, self.cfg)
        self.assertTrue(any("main 分支" in item for item in problems))

    def test_origin_remains_frozen_and_main_required(self):
        from fixtures import sh
        task, record = self.single_task()
        sh(task / "origin", "checkout", "-b", "A")
        with self.assertRaisesRegex(lib.FbrError, "origin 必须在 main"):
            lib.load_task(task)
        sh(task / "origin", "checkout", "main")
        (task / "origin" / "new.txt").write_text("new")
        sh(task / "origin", "config", "user.name", "Selftest")
        sh(task / "origin", "config", "user.email", "selftest@example.com")
        sh(task / "origin", "add", "new.txt")
        sh(task / "origin", "commit", "-qm", "drift")
        with self.assertRaisesRegex(lib.FbrError, "origin 不在初始提交"):
            lib.load_task(task)
