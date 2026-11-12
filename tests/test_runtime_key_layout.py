"""独立复核回归：收集必须能用起跑 key 扫描；宿主 git 拒绝 commondir 布局。"""

from __future__ import annotations

import hashlib
import shutil

from fixtures import KEY, write
from test_run import DockerCase, SID

import fbrlib as lib
import fbr_run


class KeyAndGitLayout(DockerCase):
    def collected_candidate(self):
        task = self.make_task()
        lib.set_stage(task, "run", "ok", attempts=1, session_id=SID)
        lib.save_json(task / "run/key-identity.json", {"sha256": hashlib.sha256(KEY.encode()).hexdigest()})
        source = self.projects(answer="debug " + KEY)
        self.docker.containers.add(task.name)
        self.docker.projects[task.name] = source
        target = task / "trace/projects"
        shutil.copytree(source, target)
        return task, next(target.rglob("*.jsonl"))

    def test_collect_without_key_keeps_original_and_container(self):
        task, raw = self.collected_candidate()
        original = raw.read_bytes()
        self.key_file.unlink()
        row = fbr_run.collect_one(task, self.cfg)
        self.assertEqual(row["state"], "blocked")
        self.assertEqual(raw.read_bytes(), original)
        self.assertEqual(self.docker.docker_calls("rm"), [])
        self.assertEqual(self.docker.docker_calls("cp"), [])
        self.assertIn(task.name, self.docker.containers)
        self.assertFalse((task / "trace/ids.json").exists())
        self.assertNotIn(KEY, str(row))

    def test_collect_with_replaced_key_is_blocked(self):
        task, raw = self.collected_candidate()
        original = raw.read_bytes()
        write(self.key_file, "apikey=replacement-key-for-safety-test\n")
        row = fbr_run.collect_one(task, self.cfg)
        self.assertEqual(row["state"], "blocked")
        self.assertEqual(raw.read_bytes(), original)
        self.assertEqual(self.docker.docker_calls("rm"), [])
        self.assertIn(task.name, self.docker.containers)
        self.assertNotIn(KEY, str(row))

    def test_collect_without_start_fingerprint_is_blocked(self):
        task, raw = self.collected_candidate()
        (task / "run/key-identity.json").unlink()
        row = fbr_run.collect_one(task, self.cfg)
        self.assertEqual(row["state"], "blocked")
        self.assertEqual(self.docker.docker_calls("cp"), [])
        self.assertEqual(self.docker.docker_calls("rm"), [])

    def test_run_records_nonsecret_key_fingerprint(self):
        task = self.make_task()
        self.docker.outcomes = [self.good()]
        self.assertEqual(fbr_run.run_one(task, self.cfg)["state"], "ok")
        identity = lib.load_json(task / "run/key-identity.json")
        self.assertEqual(identity["sha256"], hashlib.sha256(KEY.encode()).hexdigest())
        self.assertNotIn(KEY, (task / "run/key-identity.json").read_text())

    def test_commondir_cannot_redirect_config_to_host_filter(self):
        task = self.make_task()
        ws = task / "workspace"
        lib.save_git_config(task)
        common = ws / ".common-git"
        shutil.copytree(ws / ".git", common)
        marker = self.tmp / "HOST_FILTER_RAN"
        with (common / "config").open("a") as stream:
            stream.write('\n[filter "proof"]\n\tclean = touch "' + str(marker) + '"; cat\n\trequired = true\n')
        write(ws / ".git/commondir", "../.common-git\n")
        write(ws / ".gitattributes", "*.txt filter=proof\n")
        write(ws / "proof.txt", "proof\n")
        with self.assertRaisesRegex(lib.FbrError, "commondir|独立"):
            lib.restore_git_config(task)
            lib.git(ws, "add", "--", "proof.txt")
        self.assertFalse(marker.exists())

    def test_dangling_commondir_symlink_is_rejected(self):
        task = self.make_task()
        lib.save_git_config(task)
        (task / "workspace/.git/commondir").symlink_to(self.tmp / "missing")
        with self.assertRaisesRegex(lib.FbrError, "commondir|独立"):
            lib.restore_git_config(task)

