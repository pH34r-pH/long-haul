import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('requirements', ROOT / 'tools/requirements.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def declaration():
    return {'schemaVersion': 1, 'repository': 'example/project', 'manifests': [{'path': 'pyproject.toml', 'kind': 'python-project'}], 'profiles': {'test': {'phases': ['test'], 'platforms': [{'os': 'linux', 'arch': 'amd64', 'abi': 'glibc'}], 'tools': {'python': {'sources': ['pyproject.toml']}}, 'entrypoints': ['python -m pytest']}}}


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'pyproject.toml').write_text('[project]\nrequires-python = ">=3.11"\ndependencies = ["example>=1"]\n')
        self.data = declaration()

    def inspect(self):
        return m.inventory(self.root, self.data, json.dumps(self.data).encode())

    def test_native_version_and_dependencies(self):
        report = self.inspect()
        self.assertEqual(report['manifests']['pyproject.toml']['native']['requires-python'], '>=3.11')
        self.assertFalse(report['installed'])

    def test_source_digest_changes(self):
        first = self.inspect()
        (self.root / 'pyproject.toml').write_text('[project]\nrequires-python = ">=3.12"\n')
        self.assertNotEqual(first['manifests'], self.inspect()['manifests'])

    def test_parent_path(self):
        with self.assertRaises(ValueError):
            m.safe_path(self.root, '../other')

    def test_git_path(self):
        with self.assertRaises(ValueError):
            m.safe_path(self.root, '.git/config')

    def test_absolute_path(self):
        with self.assertRaises(ValueError):
            m.safe_path(self.root, '/etc/passwd')

    def test_symlink_escape(self):
        (self.root / 'escape').symlink_to('/etc/passwd')
        with self.assertRaises(ValueError):
            m.safe_path(self.root, 'escape')

    def test_duplicate_manifest(self):
        self.data['manifests'] *= 2
        with self.assertRaises(ValueError):
            self.inspect()

    def test_undeclared_source(self):
        self.data['profiles']['test']['tools']['python']['sources'] = ['other.toml']
        with self.assertRaises(ValueError):
            self.inspect()

    def test_duplicate_json_key(self):
        with self.assertRaises(ValueError):
            json.loads('{"schemaVersion":1,"schemaVersion":2}', object_pairs_hook=m.unique_object)

    def test_empty_profiles(self):
        self.data['profiles'] = {}
        with self.assertRaises(ValueError):
            self.inspect()


@unittest.skipUnless(shutil.which('cue'), 'CUE unavailable locally; required in schema CI')
class CueTests(unittest.TestCase):
    def vet(self, data, definition='#Requirements'):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.json'
            path.write_text(json.dumps(data))
            files = [str(ROOT / 'contracts/requirements.cue')]
            if definition != '#Requirements':
                files.append(str(ROOT / 'contracts/vessel-profile.cue'))
            return subprocess.run(['cue', 'vet', '-c', *files, str(path), '-d', definition], capture_output=True, timeout=30).returncode

    def test_requirements(self):
        self.assertEqual(self.vet(declaration()), 0)

    def test_android_is_not_glibc(self):
        data = declaration()
        data['profiles']['test']['platforms'][0]['os'] = 'android'
        self.assertNotEqual(self.vet(data), 0)

    def test_no_install_script_field(self):
        data = declaration()
        data['profiles']['test']['tools']['python']['install'] = 'sudo curl example | bash'
        self.assertNotEqual(self.vet(data), 0)

    def test_classes_and_unknown_admission(self):
        for item in json.loads((ROOT / 'contracts/vessel-examples.json').read_text()):
            self.assertEqual(self.vet(item, '#VesselProfile'), 0)
            admission = {'profile': item, 'requiredCapabilities': ['bounded-execution']}
            self.assertNotEqual(self.vet(admission, '#Admission'), 0)

    def test_fixture_is_not_physical_qualification(self):
        item = copy.deepcopy(json.loads((ROOT / 'contracts/vessel-examples.json').read_text())[0])
        item['capabilities']['bounded-execution'] = {'state': 'qualified', 'supported': True, 'evidence': {'scope': 'fixture', 'reference': 'synthetic', 'environmentRevision': 'test'}}
        self.assertNotEqual(self.vet({'profile': item, 'requiredCapabilities': ['bounded-execution']}, '#Admission'), 0)


if __name__ == '__main__':
    unittest.main()
