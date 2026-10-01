"""Behavioral fixtures for the changed documentation/artifact guard."""

from __future__ import annotations

import subprocess
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from check_documentation_artifacts import changed_files, changed_living_markdown, check_paths, main


class DocumentationArtifactTests(unittest.TestCase):
    def test_rejects_incidental_paths_even_under_evidence(self):
        self.assertTrue(check_paths(Path("."), [("A", "evidence/cache/result.json")]))
        self.assertTrue(check_paths(Path("."), [("A", "evidence/run/result.tmp")]))

    def test_allows_historical_and_generated_products(self):
        self.assertEqual([], check_paths(Path("."), [("A", "evidence/run/result.json")]))
        self.assertEqual([], check_paths(Path("."), [("A", "results/run/receipt.txt")]))

    def test_allows_single_word_descriptive_name_but_rejects_issue_only(self):
        self.assertEqual([], check_paths(Path("."), [("A", "docs/architecture.md")]))
        self.assertEqual([], check_paths(Path("."), [("A", "docs/014-descriptive-topic.md")]))
        self.assertTrue(check_paths(Path("."), [("A", "docs/issue-123.md")]))
        self.assertTrue(check_paths(Path("."), [("A", "docs/123.md")]))

    def test_cli_prints_no_bytes_for_code_only_changes_but_guard_still_runs(self):
        with patch("check_documentation_artifacts.changed_files", return_value=[("M", "src/module.py")]):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), patch.object(
                sys, "argv", ["check_documentation_artifacts.py", "--base", "base", "--print-living-markdown"]
            ):
                self.assertEqual(0, main())
            self.assertEqual("", output.getvalue())

            output = io.StringIO()
            with contextlib.redirect_stdout(output), patch.object(
                sys, "argv", ["check_documentation_artifacts.py", "--base", "base"]
            ):
                self.assertEqual(0, main())
            self.assertIn("guard passed for 1 changed path(s)", output.getvalue())

    def test_changed_files_uses_real_git_rename_and_copy_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args: str) -> str:
                return subprocess.run(
                    ["git", *args], cwd=root, check=True, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                ).stdout.strip()

            git("init", "--quiet")
            git("config", "user.email", "docs@example.invalid")
            git("config", "user.name", "Docs Test")
            (root / "docs").mkdir()
            (root / "old.md").write_text("[target](target.md)\n", encoding="utf-8")
            (root / "target.md").write_text("# target\n", encoding="utf-8")
            git("add", "old.md", "target.md")
            git("commit", "--quiet", "-m", "initial")
            base = git("rev-parse", "HEAD")
            git("mv", "old.md", "docs/issue-123.md")
            (root / "docs" / "copied.md").write_text((root / "docs" / "issue-123.md").read_text(), encoding="utf-8")
            git("add", "docs/copied.md")
            git("commit", "--quiet", "-m", "rename and copy")

            records = changed_files(root, base)
            self.assertIn(("R", "docs/issue-123.md"), records)
            self.assertIn(("C", "docs/copied.md"), records)
            self.assertTrue(check_paths(root, records))

    def test_historical_markdown_is_not_sent_to_living_tooling(self):
        with patch(
            "check_documentation_artifacts.changed_files",
            return_value=[("A", "docs/living-map.md"), ("A", "evidence/issue-123/RESULT.md")],
        ):
            self.assertEqual(["docs/living-map.md"], changed_living_markdown(Path("."), "base"))


if __name__ == "__main__":
    unittest.main()
