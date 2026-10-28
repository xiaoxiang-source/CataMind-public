import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import server

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_builder', ROOT/'scripts/prepare_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

class ReleaseTests(unittest.TestCase):
    def test_server_execution_is_owner_only_for_all_methods(self):
        for role in ['guest', 'user', 'steward']:
            for method in ['GET', 'POST']:
                for endpoint in ['chat', 'upload', 'files', 'download', 'status']:
                    self.assertFalse(server._business_allowed(role,method,['codex',endpoint],server.ALL_CAPABILITIES)[0])
        self.assertTrue(server._business_allowed('owner','POST',['codex','chat'])[0])

    def test_public_bind_rejected_before_database_initialization(self):
        with mock.patch.object(server, 'AUTH_ENABLED', False), mock.patch.object(server.sys,'argv',['server.py','--host','0.0.0.0']), mock.patch.object(server,'init_db') as init:
            with self.assertRaises(SystemExit) as exc:
                server.main()
            self.assertEqual(exc.exception.code,2)
            init.assert_not_called()

    def test_release_excludes_live_data_and_has_all_root_modules(self):
        files={p.relative_to(ROOT).as_posix() for p in release.source_files(ROOT)}
        for name in ['catmat_postprocessing.py','catmat_process_monitor.py','catmat_resource_governor.py','static/postprocess-tools.js']:
            self.assertIn(name,files)
        for name in files:
            self.assertNotIn('__pycache__',name)
            self.assertFalse(name.startswith(('backups/','codex_uploads/','data/datasets/','.git/','CataMind-')))
            self.assertNotEqual(name,'.env')
        self.assertIn('.env.example',files)

    def test_export_refuses_overwrite_and_changed_source(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'input.py';source.write_text('print(1)')
            import hashlib
            report={'files':[dict(path='input.py',bytes=8,sha256=hashlib.sha256(source.read_bytes()).hexdigest())], 'errors':[]}
            target=root/'out';release.export(root,target,report)
            self.assertEqual((target/'input.py').read_text(),'print(1)')
            with self.assertRaises(ValueError):release.export(root,target,report)
            source.write_text('print(2)')
            with self.assertRaises(ValueError):release.export(root,root/'changed',report)

    def test_audit_rejects_escape_without_reading_target(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);link=root/'source.py';link.symlink_to('/etc/passwd')
            with mock.patch.object(release,'source_files',return_value=[link]):
                report=release.audit(root)
            self.assertEqual(report['files'],[])
            self.assertEqual(len(report['errors']),1)

if __name__ == '__main__':unittest.main()

class PrivacyTests(unittest.TestCase):
    def test_export_redacts_identifiers_and_footer(self):
        filing='京ICP'+'备123456789号-1'
        home='/home/'+'alice'
        domain='catamind'+'.online'
        address='10.'+'1.2.3'
        text='<footer class="site-footer">'+filing+'</footer> '+home+'/work https://'+domain+' '+address
        result=release.public_bytes('index.html',text.encode()).decode()
        for identifier in [filing,home,domain,address]:
            self.assertNotIn(identifier,result)
        self.assertIn('catamind.example.com',result)

    def test_privacy_transform_preserves_binary_assets(self):
        self.assertEqual(release.public_bytes('logo.png',b'\x89PNG\xff'),b'\x89PNG\xff')
