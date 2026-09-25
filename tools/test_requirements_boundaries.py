import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('requirements_boundaries', ROOT / 'tools/requirements.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'lean-toolchain').write_text('leanprover/lean4:v4.34.0-rc2\n')
        self.data = {'repository': 'example/project', 'manifests': [{'path': 'lean-toolchain', 'kind': 'toolchain'}], 'profiles': {'formal': {'tools': {'lean': {'sources': ['lean-toolchain']}}}}}

    def inspect(self):
        return m.inventory(self.root, self.data, json.dumps(self.data).encode())

    def test_git_metadata_symlink(self):
        (self.root / '.git').mkdir()
        (self.root / '.git/config').write_text('private metadata')
        (self.root / 'indirect').symlink_to('.git/config')
        with self.assertRaises(ValueError):
            m.safe_path(self.root, 'indirect')

    def test_bounded_read(self):
        with self.assertRaises(ValueError):
            m.read_bounded(self.root / 'lean-toolchain', 2)

    def test_total_limit(self):
        with patch.object(m, 'MAX_TOTAL_BYTES', 2), self.assertRaises(ValueError):
            self.inspect()

    def test_manifest_count(self):
        with patch.object(m, 'MAX_MANIFESTS', 0), self.assertRaises(ValueError):
            self.inspect()

    def test_native_selection(self):
        report = self.inspect()
        self.assertEqual(report['manifests']['lean-toolchain']['native']['selection'], 'leanprover/lean4:v4.34.0-rc2')

    def test_ambiguous_selection(self):
        (self.root / 'lean-toolchain').write_text('first\nsecond\n')
        with self.assertRaises(ValueError):
            self.inspect()


if __name__ == '__main__':
    unittest.main()
