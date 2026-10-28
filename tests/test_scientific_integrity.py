"""Scientific regression tests; all writes use disposable fixtures/SQLite."""
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
from catmat_tools.structure_transfer import parse_xyz, structure_xyz, preserve_cp2k_geometry, normalize_pbc, validate_cell
from catmat_tools import pdos_review
from catmat_tools.postprocess_pdos import process_dir

class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.cell = [[8.1459,0,0],[4.07295,8.1459*math.sqrt(3)/2,0],[0,0,44.6011]]
        self.structure = dict(cell=self.cell, symbols=['O','Ir'], scaled_positions=[[.2,.3,.4],[.7,.8,.9]],pbc=[True,True,False])

    def test_roundtrip_nonorthogonal(self):
        g = parse_xyz(structure_xyz(self.structure))
        self.assertEqual(g['pbc'], 'xy')
        for a,b in zip(g['cell'],self.cell):
            for x,y in zip(a,b): self.assertAlmostEqual(x,y,places=12)
        for (_,actual),p in zip(g['coords'],self.structure['scaled_positions']):
            for i,x in enumerate(actual): self.assertAlmostEqual(x,sum(p[j]*self.cell[j][i] for j in range(3)),places=12)

    def test_conflicting_cell_rejected(self):
        with self.assertRaises(ValueError): parse_xyz(structure_xyz(self.structure),[[1,0,0],[0,1,0],[0,0,1]])

    def test_malformed_geometry_rejected(self):
        for c in ([1,2,3], [[0,0,0]]*3, [[math.nan,0,0],[0,1,0],[0,0,1]]):
            with self.assertRaises(ValueError): validate_cell(c)
        with self.assertRaises(ValueError): normalize_pbc(['F','F','F'])
        self.structure['scaled_positions'][0]=[0,0]
        with self.assertRaises(ValueError): structure_xyz(self.structure)

    def test_replace_generator_geometry_and_reject_bad_sections(self):
        inp='&FORCE_EVAL\n &SUBSYS\n &CELL\n ABC 20 16 19\n &END CELL\n &COORD\n O 0 0 0\n &END COORD\n &KIND O\n ELEMENT O\n &END KIND\n &END SUBSYS\n&END FORCE_EVAL\n'
        g=parse_xyz(structure_xyz(self.structure))
        out=preserve_cp2k_geometry(inp,g,'xy')
        self.assertIn('B [angstrom] 4.07295',out)
        self.assertIn('PERIODIC XY',out)
        self.assertIn('&KIND O\n ELEMENT O',out)
        self.assertNotIn('ABC 20',out)
        for bad in (inp.replace('&END CELL','&END COORD'), inp.replace('&END FORCE_EVAL','')):
            with self.assertRaises(ValueError):preserve_cp2k_geometry(bad,g,'xy')

class CodexLockTests(unittest.TestCase):
    def test_codex_wait_releases_request_lock(self):
        import server
        import subprocess
        def run(cmd, **kwargs):
            self.assertFalse(server._lock.locked())
            Path(cmd[cmd.index('-o')+1]).write_text('test reply')
            return subprocess.CompletedProcess(cmd,0,stdout='',stderr='')
        server._lock_acquire()
        try:
            with mock.patch.object(server.subprocess,'run',side_effect=run):
                reply, sid, error=server.codex_chat('fixture','fixture-session')
            self.assertTrue(server._lock.locked())
            self.assertEqual(reply,'test reply')
            self.assertIsNone(error)
        finally:
            server._lock_release()

class PdosTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.file=self.root/'project-k1-1.pdos'
        self.file.write_text('# Projected DOS for atomic kind Ir1 at iteration step i = 0, E(Fermi) = 0.0 a.u.\n# MO Eigenvalue [a.u.] Occupation s py pz px d-2 d-1 d0 d+1 d+2\n1 0.0 2.0 0 0 0 0 0 0 1 0 0\n')
        self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row
        self.c.executescript(pdos_review.SCHEMA+'CREATE TABLE ml_samples(id INTEGER PRIMARY KEY,name TEXT,source TEXT,features TEXT,target REAL);')
    def tearDown(self):self.c.close();self.tmp.cleanup()
    def preview(self):
        before=pdos_review.snapshot(self.root)
        result=process_dir(self.root)
        return pdos_review.create_preview(self.c,'test-uuid',result,before,pdos_review.snapshot(self.root))
    def test_units_and_provenance(self):
        result=process_dir(self.root)
        self.assertAlmostEqual(result['elements']['Ir']['d_band_center'],0,places=10)
        self.assertAlmostEqual(result['elements']['Ir']['d_ldos_fermi'],1/(.05*math.sqrt(2*math.pi)),places=9)
        self.assertEqual(result['provenance']['units']['dos'],'states/eV')
        self.assertEqual(len(result['provenance']['sources'][0]['sha256']),64)
    def test_preview_does_not_train_and_admission_requires_acceptance(self):
        p=self.preview();rid=p['review_id']
        self.assertEqual(self.c.execute('select count(*) from ml_samples').fetchone()[0],0)
        with self.assertRaises(ValueError):pdos_review.transition(self.c,rid,'admit',{},'tester',self.root)
        with self.assertRaises(ValueError):pdos_review.transition(self.c,rid,'accept',{},'tester',self.root)
        pdos_review.transition(self.c,rid,'accept',{'checks':dict.fromkeys(pdos_review.CHECKS,True),'note':'Synthetic fixture verification'},'tester',self.root)
        for _ in range(2):pdos_review.transition(self.c,rid,'admit',{},'tester',self.root)
        self.assertEqual(self.c.execute('select count(*) from ml_samples').fetchone()[0],1)
        self.assertEqual(self.preview()['status'],'admitted')
    def test_changed_sources_rejected(self):
        p=self.preview();self.file.write_text(self.file.read_text()+'\n')
        with self.assertRaises(ValueError):pdos_review.transition(self.c,p['review_id'],'accept',{},'tester',self.root)
    def test_duplicate_projection_rejected(self):
        (self.root/'duplicate.pdos').write_bytes(self.file.read_bytes())
        with self.assertRaises(ValueError):process_dir(self.root)

if __name__=='__main__':unittest.main()
