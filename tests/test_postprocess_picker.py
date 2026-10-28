import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import catmat_postprocessing as pp

class PickerTests(unittest.TestCase):
    def test_browse_filters_and_boundaries(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'allowed';root.mkdir()
            (root/'sub').mkdir();(root/'ok.cube').write_text('cube')
            (root/'secret.db').write_text('secret');(root/'.hidden.cube').write_text('hidden')
            outside=Path(td)/'outside';outside.mkdir();(outside/'bad.cube').write_text('bad')
            (root/'escape').symlink_to(outside,target_is_directory=True)
            (root/'bad.cube').symlink_to(outside/'bad.cube')
            entries=pp.browse_inputs('density_cube',str(root),[root])
            self.assertEqual([e['name'] for e in entries['entries']],['sub','ok.cube'])
            self.assertEqual(entries['parent'],'')
            self.assertEqual(len(pp.browse_inputs('directory','',[root])['entries']),1)
            for path in [outside,root/'escape',root/'../outside']:
                with self.assertRaises(ValueError):pp.browse_inputs('directory',str(path),[root])

    def test_upload_batch_then_select_and_validate(self):
        with tempfile.TemporaryDirectory() as td:
            files=[dict(name=n,content_b64=base64.b64encode(b'pdos').decode()) for n in ['a.pdos','b.pdos']]
            result=pp.upload_inputs('directory',files,td)
            self.assertEqual(result['count'],2)
            self.assertEqual(len(pp.browse_inputs('directory',result['path'],[td])['entries']),2)
            cfg=pp.validate(dict(tool='pdos',inputs=dict(directory=result['path'])),[td])
            self.assertEqual(cfg['inputs']['directory'],result['path'])
            self.assertNotEqual(result['path'],pp.upload_inputs('directory',files,td)['path'])

    def test_upload_rejects_invalid_without_partial_files(self):
        with tempfile.TemporaryDirectory() as td:
            for name,content in [('../a.cube','YQ=='),('a\\b.cube','YQ=='),('.a.cube','YQ=='),('a.py','YQ=='),('a.cube','???'),('a.cube','')]:
                with self.assertRaises(ValueError):pp.upload_inputs('density_cube',[dict(name=name,content_b64=content)],td)
            files=[dict(name='a.pdos',content_b64='YQ==')]*2
            with self.assertRaises(ValueError):pp.upload_inputs('directory',files,td)
            with patch.object(pp,'UPLOAD_LIMIT',1):
                with self.assertRaises(ValueError):pp.upload_inputs('density_cube',[dict(name='a.cube',content_b64='YWE=')],td)
            self.assertEqual(list(Path(td).iterdir()),[])

if __name__=='__main__':unittest.main()
