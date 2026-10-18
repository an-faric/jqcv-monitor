"""Integración con repositorios Git temporales; nunca accede a GitHub ni envía correo."""

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import state_store


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.remote = root / "remote.git"
        self.repo = root / "checkout"
        self.repo.mkdir()
        self.run_git("init", "--bare", str(self.remote), cwd=root)
        self.run_git("init", "-b", "main", cwd=self.repo)
        self.run_git("config", "user.name", "Synthetic test", cwd=self.repo)
        self.run_git("config", "user.email", "test@example.invalid", cwd=self.repo)
        (self.repo / "code.txt").write_text("source stays intact", encoding="utf-8")
        self.run_git("add", "code.txt", cwd=self.repo)
        self.run_git("commit", "-m", "Synthetic code", cwd=self.repo)
        self.run_git("remote", "add", "origin", str(self.remote), cwd=self.repo)
        self.run_git("push", "origin", "main", cwd=self.repo)
        self.head = self.run_git("rev-parse", "HEAD", cwd=self.repo)
        previous = Path.cwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, previous)

    def run_git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def quietly(self, operation):
        with contextlib.redirect_stdout(io.StringIO()):
            operation("jqcv-monitor-state", "main")

    def test_save_load_and_second_identical_run_do_not_change_code_or_commit_again(self):
        self.quietly(state_store.load)
        Path("last_content.txt").write_text('{"synthetic": true}\n', encoding="utf-8")
        self.quietly(state_store.save)
        ref = "refs/heads/jqcv-monitor-state"
        first = self.run_git("--git-dir", str(self.remote), "rev-parse", ref)
        self.assertEqual(self.run_git("rev-parse", "HEAD"), self.head)
        self.assertEqual(self.run_git("--git-dir", str(self.remote), "ls-tree", "--name-only", ref),
                         "last_content.txt")
        Path("last_content.txt").unlink()
        self.quietly(state_store.load)
        self.assertIn("synthetic", Path("last_content.txt").read_text())
        self.quietly(state_store.save)
        second = self.run_git("--git-dir", str(self.remote), "rev-parse", ref)
        self.assertEqual(first, second)
        self.assertEqual(self.run_git("rev-parse", "HEAD"), self.head)

    def test_legacy_reference_in_code_branch_is_preserved_on_first_load(self):
        Path("last_content.txt").write_text("TEXTO\nLegacy\n\nENLACES\n", encoding="utf-8")
        self.run_git("add", "last_content.txt")
        self.run_git("commit", "-m", "Synthetic legacy reference")
        self.quietly(state_store.load)
        self.assertIn("Legacy", Path("last_content.txt").read_text())

    def test_source_branch_cannot_be_used_as_state_branch(self):
        with self.assertRaises(state_store.StateStoreError):
            state_store.load("main", "main")

    def test_load_does_not_overwrite_local_user_state(self):
        Path("last_content.txt").write_text("local reference", encoding="utf-8")
        with self.assertRaisesRegex(state_store.StateStoreError, "cambios locales"):
            self.quietly(state_store.load)
        self.assertEqual(Path("last_content.txt").read_text(), "local reference")

    def test_empty_state_cannot_be_published(self):
        Path("last_content.txt").write_text("", encoding="utf-8")
        with self.assertRaises(state_store.StateStoreError):
            self.quietly(state_store.save)

    def test_concurrent_remote_update_is_rejected_without_forcing_history(self):
        self.quietly(state_store.load)
        Path("last_content.txt").write_text('{"synthetic": 1}\n', encoding="utf-8")
        self.quietly(state_store.save)
        Path("last_content.txt").unlink()
        self.quietly(state_store.load)
        parent = self.run_git("rev-parse", state_store.TRACKING_REF)
        tree = self.run_git("rev-parse", f"{parent}^{{tree}}")
        concurrent = self.run_git("commit-tree", tree, "-p", parent, "-m", "Concurrent update")
        self.run_git("push", "origin", f"{concurrent}:refs/heads/jqcv-monitor-state")
        Path("last_content.txt").write_text('{"synthetic": 2}\n', encoding="utf-8")
        with self.assertRaises(state_store.StateStoreError):
            self.quietly(state_store.save)
        remote_head = self.run_git("--git-dir", str(self.remote), "rev-parse",
                                   "refs/heads/jqcv-monitor-state")
        self.assertEqual(remote_head, concurrent)


if __name__ == "__main__":
    unittest.main()
