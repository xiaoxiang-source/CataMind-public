"""Release fixes: no production DB writes, computations or network requests."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import server


class ReleaseSafetyTests(unittest.TestCase):
    def handler(self):
        h = object.__new__(server.Handler)
        h._err = mock.Mock()
        h._serve_file_cached = mock.Mock()
        return h

    def test_static_blocks_parent_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'static').mkdir()
            (root / '.env').write_text('PRIVATE_FIXTURE')
            (root / 'static' / 'leak').symlink_to(root / '.env')
            (root / 'static' / 'safe.js').write_text('1')
            with mock.patch.object(server, 'BASE_DIR', td):
                for url in ['/static/../.env', '/static/leak', '/static/', '/static/../../outside']:
                    h = self.handler(); h._serve_static(url)
                    h._err.assert_called_once(); h._serve_file_cached.assert_not_called()
                h = self.handler(); h._serve_static('/static/safe.js')
                h._serve_file_cached.assert_called_once(); h._err.assert_not_called()

    def test_cp2k_view_blocks_sibling_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'view'; root.mkdir()
            sibling = Path(td) / 'view-private'; sibling.mkdir()
            (sibling / 'secret.txt').write_text('PRIVATE_FIXTURE')
            with mock.patch.object(server, 'CP2KVIEW_DIR', str(root)):
                h = self.handler(); h._serve_cp2kview('/cp2kview/../view-private/secret.txt')
                h._err.assert_called_once(); h._serve_file_cached.assert_not_called()

    def test_dpgen_production_rejected_before_database_or_thread(self):
        with mock.patch.dict(os.environ, {'CATMAT_DPGEN_MOCK': '0'}), \
                mock.patch.object(server.sqlite3, 'connect') as conn, \
                mock.patch.object(server.threading, 'Thread') as thread:
            run_id, error = server._start_dpgen_run('fixture', {'run_name': 'fixture', 'seed': 'fixture'})
            self.assertIsNone(run_id); self.assertIn('真实轨迹帧', error)
            conn.assert_not_called(); thread.assert_not_called()

    def test_csv_explicit_mapping_uses_target_not_descriptor(self):
        result = server.normalize_dataset_csv(
            'name,site_fukui_asym_Ir1_minus_Ru1,target\na,0.3,1.6\nb,0.2,1.2\n',
            mapping={'name_col': 'name', 'target_col': 'target', 'feature_cols': ['site_fukui_asym_Ir1_minus_Ru1']})
        self.assertEqual(result['rows'][0]['target'], 1.6)
        self.assertEqual(result['rows'][0]['features'], {'site_fukui_asym_Ir1_minus_Ru1': 0.3})


if __name__ == '__main__':
    unittest.main()
