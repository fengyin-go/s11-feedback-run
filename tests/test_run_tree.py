"""Git-free run tree creation and host-side integration."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from fixtures import Sandbox, sh, write

import fbr_run_tree
import fbr_snapshot as snap
import fbrlib as lib


class RunTree(Sandbox):
    def prepared(self):
        task = self.make_task()
        record = lib.load_task(task)
        lib.save_git_config(task)
        meta = fbr_run_tree.prepare(task, record, self.cfg)
        return task, record, meta

    def test_prepare_is_git_free_and_workspace_is_untouched(self):
        task, record, meta = self.prepared()
        tree = task / "run-tree"
        self.assertTrue(meta["no_git"])
        self.assertTrue(tree.is_dir())
        self.assertFalse(any(path.name == ".git" for path in tree.rglob(".git")))
        self.assertEqual((tree / "src/clipcut/packer.py").read_text(),
                         "def pack(path, segment):\n    return [path]\n")
        self.assertEqual(sh(task / "workspace", "status", "--porcelain", "--ignored"), "")
        self.assertEqual(sh(task / "workspace", "rev-parse", "HEAD"), record["initial_commit"])
        fbr_run_tree.validate(task, record)

    def test_summary_and_integration_cover_add_modify_delete_and_ignored_files(self):
        task, record, _ = self.prepared()
        tree = task / "run-tree"
        write(tree / "src/clipcut/packer.py", "def pack(path, segment):\n    return []\n")
        write(tree / "src/clipcut/segment.py", "VALUE = 1\n")
        (tree / "README.md").unlink()
        write(tree / ".env", "LOCAL_ONLY=1\n")

        found = fbr_run_tree.summary(task, record)
        self.assertEqual({item["path"]: item["status"] for item in found["files"]},
                         {"README.md": "D", "src/clipcut/packer.py": "M",
                          "src/clipcut/segment.py": "A"})
        self.assertIn(".env", found["excluded"])

        integration = fbr_run_tree.integrate(task, record, self.cfg)
        workspace = task / "workspace"
        self.assertEqual(integration["state"], "integrated")
        self.assertFalse((workspace / "README.md").exists())
        self.assertIn("return []", (workspace / "src/clipcut/packer.py").read_text())
        self.assertTrue((workspace / "src/clipcut/segment.py").is_file())
        self.assertFalse((workspace / ".env").exists())
        self.assertTrue((workspace / ".git").is_dir())
        self.assertEqual(
            set(sh(workspace, "diff", "--cached", "--name-status").splitlines()),
            {"D\tREADME.md", "M\tsrc/clipcut/packer.py", "A\tsrc/clipcut/segment.py"},
        )

    def test_symlink_and_executable_bit_are_preserved(self):
        task, record, _ = self.prepared()
        tree = task / "run-tree"
        script = write(tree / "bin/run.sh", "#!/bin/sh\nexit 0\n")
        os.chmod(script, 0o755)
        os.symlink("../src/clipcut/packer.py", tree / "bin/packer-link")

        fbr_run_tree.integrate(task, record, self.cfg)
        workspace = task / "workspace"
        self.assertTrue((workspace / "bin/run.sh").stat().st_mode & 0o111)
        self.assertEqual(os.readlink(workspace / "bin/packer-link"), "../src/clipcut/packer.py")

    def test_dependency_cache_is_available_to_model_but_not_integrated(self):
        task = self.make_task()
        workspace = task / "workspace"
        write(workspace / "package.json", '{"name":"selftest"}\n')
        write(workspace / ".gitignore", ".env\nnode_modules/\n")
        sh(workspace, "add", "-A")
        sh(workspace, "commit", "-qm", "dependency baseline")
        record = lib.load_task(task)
        record["initial_commit"] = sh(workspace, "rev-parse", "HEAD")
        lib.save_json(task / "task.json", record)
        lib.save_git_config(task)

        def seed(repo, cfg, *, task=None, setup_cmd=None):
            write(repo / "node_modules/pkg/index.js", "cached dependency\n")
            lib.save_json(task / "run/dependencies.json", {
                "state": "copied", "initial_commit": record["initial_commit"],
                "cache": str(self.tmp / "cache"), "identity": "fixture",
                "paths": ["node_modules"],
            })
            return True

        with mock.patch("fbr_dependencies.seed", side_effect=seed):
            meta = fbr_run_tree.prepare(task, record, self.cfg)
        self.assertEqual(meta["dependency_paths"], ["node_modules"])
        self.assertTrue((task / "run-tree/node_modules/pkg/index.js").is_file())
        found = fbr_run_tree.summary(task, record)
        self.assertEqual(found["files"], [])
        self.assertIn("node_modules", found["excluded"])

        fbr_run_tree.integrate(task, record, self.cfg)
        self.assertFalse((workspace / "node_modules").exists())
        self.assertEqual(sh(workspace, "diff", "--cached", "--name-only"), "")

    def test_model_created_git_metadata_is_never_integrated(self):
        task, record, _ = self.prepared()
        original_config = (task / "workspace/.git/config").read_bytes()
        write(task / "run-tree/.git/config",
              "[remote \"origin\"]\n\turl = https://github.com/sibling/repo\n")
        write(task / "run-tree/src/clipcut/packer.py",
              "def pack(path, segment):\n    return ['safe']\n")

        found = fbr_run_tree.summary(task, record)
        self.assertTrue(found["model_git_created"])
        self.assertNotIn(".git/config", [item["path"] for item in found["files"]])
        integration = fbr_run_tree.integrate(task, record, self.cfg)
        self.assertTrue(integration["model_git_created"])
        self.assertEqual((task / "workspace/.git/config").read_bytes(), original_config)

    def test_docs_only_change_is_not_code(self):
        task, record, _ = self.prepared()
        write(task / "run-tree/README.md", "# changed docs\n")
        self.assertEqual(fbr_run_tree.code_changes(task, record), [])
        self.assertEqual(fbr_run_tree.summary(task, record)["files"],
                         [{"path": "README.md", "status": "M"}])

    def test_snapshot_integrates_run_tree_before_host_commit(self):
        task, record, _ = self.prepared()
        sid = "dddddddd-0000-4000-8000-000000000001"
        lib.save_json(task / "trace/ids.json", {"session_id": sid, "prompt_id": "prm-0001"})
        self.mark(task, "collect", collect={"session_id": sid, "prompt_id": "prm-0001"})
        write(task / "run-tree/src/clipcut/packer.py", "def pack(path, segment):\n    return [1]\n")

        row = snap.commit_one(task, self.cfg)
        self.assertEqual(row["state"], "ok", row)
        info = lib.load_json(task / "review/snapshot.json")
        self.assertEqual(info["workspace_source"], "git-free-run-tree")
        self.assertEqual(info["model_commits"], 0)
        self.assertFalse(info["model_git_created"])
        self.assertTrue(info["host_commit"])
        self.assertEqual(info["run_tree_files"],
                         [{"path": "src/clipcut/packer.py", "status": "M"}])
        self.assertEqual(sh(task / "workspace", "log", "-1", "--format=%B", info["commit"]), sid)
        self.assertEqual(sh(task / "workspace", "rev-parse", f"{info['commit']}^"),
                         record["initial_commit"])


if __name__ == "__main__":
    unittest.main()
