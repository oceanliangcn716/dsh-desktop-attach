import importlib.util
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('release_manager',ROOT/'tools/manage.py')
manager=importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)

class InstallationTests(unittest.TestCase):
    def test_prepare_preserves_existing_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'config.example.json').write_text(json.dumps({'bind_host':'auto','port':3081}))
            with patch.object(manager,'ROOT',root),patch.object(manager.subprocess,'run'):
                manager.prepare_files()
                self.assertEqual(json.loads((root/'config.json').read_text())['bind_host'],'auto')
                (root/'config.json').write_text(json.dumps({'port':4567,'custom':'preserve'}))
                manager.prepare_files()
                self.assertEqual(json.loads((root/'config.json').read_text()),{'port':4567,'custom':'preserve'})

    def test_plist_escapes_paths_and_identifies_own_installation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'example & folder'
            root.mkdir()
            file=Path(tmp)/'service.plist'
            with patch.object(manager,'ROOT',root):
                data=manager.service_definition(['python3',str(root/'entry.py')])
                file.write_bytes(plistlib.dumps(data))
                self.assertTrue(manager.owned_plist(file))
                self.assertEqual(plistlib.loads(file.read_bytes())['WorkingDirectory'],str(root))
                data['WorkingDirectory']=str(root/'another')
                file.write_bytes(plistlib.dumps(data))
                self.assertFalse(manager.owned_plist(file))

if __name__=='__main__': unittest.main()
