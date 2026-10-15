from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent.util import ROOT


class CheckCommandTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        example = ROOT / "(範例)D-Plan_TEAM_042_2026-10-27.json"
        self.document = self.directory / "plan.json"
        self.document.write_bytes(example.read_bytes())

    def check(self, *args):
        return subprocess.run([sys.executable, "-m", "agent", "check", str(self.document), *map(str, args)],
                              cwd=ROOT, text=True, capture_output=True, timeout=30, check=False)

    def test_explicit_missing_context_is_an_error(self):
        result = self.check("--context", self.directory / "missing.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("context 不存在或不是檔案", result.stderr)
        self.assertNotIn('"schema_and_references": "passed"', result.stdout)

    def test_explicit_context_directory_is_an_error(self):
        result = self.check("--context", self.directory)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("context 不存在或不是檔案", result.stderr)

    def test_omitted_context_keeps_structural_only_check_explicit(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        outcome = json.loads(result.stdout)
        self.assertEqual(outcome["schema_and_references"], "passed")
        self.assertFalse(outcome["full_semantics"])
        self.assertTrue(outcome["warnings"])
