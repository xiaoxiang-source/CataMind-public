"""Scientific fixtures and isolated execution; no production data or real computation."""
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from catmat_tools import bader_charge as cube
from catmat_tools import postprocess_toolkit as toolkit
from catmat_tools.external_runner import run_checked,run_json
import catmat_postprocessing as jobs


class ToolkitTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.base=dict(com1='fixture',com2='unit grid',natoms=1,origin=[0,0,0],nx=2,ny=2,nz=2,
                       vx=[1,0,0],vy=[0,1,0],vz=[0,0,1],atoms=[[1,1,0,0,0]])
        for name,val in [('total',3),('slab',1),('ads',1)]:cube.write_cube(self.base,[val]*8,self.root/(name+'.cube'))
    def tearDown(self):self.tmp.cleanup()
    def inputs(self):return dict(zip(['total_cube','slab_cube','ads_cube'],[str(self.root/(x+'.cube')) for x in ['total','slab','ads']]))
    def config(self):return dict(tool='density_difference',inputs=self.inputs(),params={'density_unit':'e/bohr^3'})
    def test_integral_and_output_units(self):
        r=toolkit.run_tool(self.config(),self.root/'run')
        self.assertEqual(r['density_integral_e'],8)
        self.assertEqual(r['accumulation_e'],8)
        self.assertEqual(r['depletion_e'],0)
        self.assertIsNone(r['charge_transfer'])
        self.assertEqual(r['review_status'],'preview')
        self.assertEqual(len(r['sources']),3)
        self.assertEqual(list(cube.read_cube(self.root/'run/delta.cube')['data']),[1]*8)
    def test_skew_voxel_determinant(self):
        self.base.update(vx=[2,0,0],vy=[1,3,0],vz=[0,0,4])
        for name,val in [('total',3),('slab',1),('ads',1)]:cube.write_cube(self.base,[val]*8,self.root/(name+'.cube'))
        self.assertEqual(toolkit.run_tool(self.config(),self.root/'run')['density_integral_e'],192)
    def test_mismatched_origin_and_vectors_rejected(self):
        for key in ['origin','vx']:
            with self.subTest(key=key):
                bad=dict(self.base);bad[key]=[100,0,0]
                cube.write_cube(bad,[1]*8,self.root/'ads.cube')
                with self.assertRaisesRegex(ValueError,'原点|向量'):toolkit.run_tool(self.config(),self.root/key)
    def test_truncated_nonfinite_and_negative_grid(self):
        path=self.root/'total.cube';original=path.read_text()
        for bad in [original.rsplit('3.0000000000e+00',1)[0],original.replace('3.0000000000e+00','NaN',1),original.replace('    2 ', '   -2 ',1)]:
            path.write_text(bad)
            with self.assertRaises(ValueError):cube.read_cube(path)
    def test_units_and_unknown_parameters_rejected(self):
        cfg=self.config();cfg['params']={}
        with self.assertRaises(ValueError):toolkit.run_tool(cfg,self.root/'run')
        cfg['params']={'shell':'x'}
        with self.assertRaises(ValueError):toolkit.run_tool(cfg,self.root/'run')
    def test_outputs_are_not_overwritten(self):
        toolkit.run_tool(self.config(),self.root/'run')
        with self.assertRaisesRegex(ValueError,'已有产物'):toolkit.run_tool(self.config(),self.root/'run')
        with self.assertRaisesRegex(ValueError,'覆盖输入'):cube.density_difference(*self.inputs().values(),self.root/'total.cube')
    def test_acf_charge_and_conservation(self):
        acf=self.root/'ACF.dat';acf.write_text('1 0 0 0 8 0.5 8\nVACUUM CHARGE: 0\nNUMBER OF ELECTRONS: 8\n')
        cfg=dict(tool='bader_import',inputs=dict(density_cube=str(self.root/'ads.cube'),acf_file=str(acf)),
                 params=dict(density_unit='e/bohr^3',atom_order_confirmed=True,valence_electrons=[9],expected_electrons=8))
        r=toolkit.run_tool(cfg,self.root/'run')
        self.assertEqual(r['atoms'][0]['net_charge_e'],1)
        self.assertTrue(all(c['ok'] for c in r['checks']))
        cfg['params']['expected_electrons']=7
        r=toolkit.run_tool(cfg,self.root/'bad')
        self.assertFalse(r['checks'][1]['ok'])
    def test_bader_adapter_requires_real_output_contract(self):
        binary=self.root/'fake-bader'
        binary.write_text('#!'+sys.executable+'\nfrom pathlib import Path\nPath("ACF.dat").write_text("1 0 0 0 8 0.5 8\\nVACUUM CHARGE: 0\\nNUMBER OF ELECTRONS: 8\\n")\n')
        binary.chmod(0o700)
        cfg=dict(tool='bader_run',inputs={'density_cube':str(self.root/'ads.cube')},params={'density_unit':'e/bohr^3'})
        with mock.patch.dict(os.environ,{'CATMAT_BADER_BIN':str(binary)}):
            result=toolkit.run_tool(cfg,self.root/'bader')
        self.assertEqual(result['basin_electrons'],8)
        self.assertIsNone(result['checks'][1]['ok'])
        self.assertNotIn('net_charge_e',result['atoms'][0])
        self.assertEqual(result['engine']['path'],str(binary))

    def test_acf_mismatch_rejected(self):
        acf=self.root/'ACF.dat';acf.write_text('2 0 0 0 8 0.5 8\n')
        with self.assertRaises(ValueError):toolkit.parse_acf(acf,1)
    def test_only_last_attempt_is_audited(self):
        path=self.root/'cp2k.out';path.write_text('PROGRAM STARTED AT old\nABORT\nPROGRAM STARTED AT new\nENERGY| Total FORCE_EVAL ( QS ) energy [a.u.]: -5.0\nPROGRAM ENDED AT now\n')
        r=toolkit.audit_output(path)
        self.assertEqual(r['execution_status'],'ended');self.assertEqual(r['last_energy_hartree'],-5)
        with path.open('a') as f:f.write('PROGRAM STARTED AT pending\n')
        self.assertEqual(toolkit.audit_output(path)['execution_status'],'incomplete_or_running')
    def test_input_changed_rejects_result(self):
        original=toolkit.density_difference
        def change(*args):
            result=original(*args)
            with Path(args[0]).open('a') as f:f.write('\n')
            return result
        with mock.patch.object(toolkit,'density_difference',side_effect=change):
            with self.assertRaisesRegex(ValueError,'输入变化'):toolkit.run_tool(self.config(),self.root/'run')
    def test_paths_and_symlinks_are_confined(self):
        self.assertEqual(jobs.validate(self.config(),[self.root])['tool'],'density_difference')
        cfg=self.config();cfg['inputs']['total_cube']='/etc/passwd'
        with self.assertRaises(ValueError):jobs.validate(cfg,[self.root])
        link=self.root/'escape.cube';link.symlink_to('/etc/passwd');cfg['inputs']['total_cube']=str(link)
        with self.assertRaises(ValueError):jobs.validate(cfg,[self.root])
    def test_pdos_cli_real_interpreter(self):
        python = os.environ.get('CATMAT_TEST_SCIENCE_PY', sys.executable)
        if subprocess.run([python, '-c', 'import numpy'], capture_output=True).returncode:
            self.skipTest('NumPy unavailable; set CATMAT_TEST_SCIENCE_PY for scientific tests')
        folder=self.root/'pdos';folder.mkdir()
        (folder/'fixture-k1-1.pdos').write_text('# Projected DOS for atomic kind Ir1 at iteration step i = 0, E(Fermi) = 0.0 a.u.\n# MO Eigenvalue [a.u.] Occupation s py pz px d-2 d-1 d0 d+1 d+2\n1 0.0 2.0 0 0 0 0 0 0 1 0 0\n')
        cfg=dict(tool='pdos',inputs={'directory':str(folder)},params={})
        file=self.root/'config.json';file.write_text(json.dumps(cfg))
        obj=run_json([os.environ.get('CATMAT_TEST_SCIENCE_PY', sys.executable),toolkit.__file__,'run',str(file),'--workdir',str(self.root/'pdos-run')],
                     timeout=30,env=dict(os.environ,OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1'))
        self.assertIn('provenance',obj['result']);self.assertEqual(obj['result']['review_status'],'preview')
        self.assertTrue((self.root/'pdos-run/pdos.csv').is_file())
    def test_background_job_persistence(self):
        db=self.root/'test.db';conn=sqlite3.connect(db);conn.row_factory=sqlite3.Row;conn.executescript(jobs.SCHEMA)
        r=jobs.start(conn,str(db),self.root/'runs',sys.executable,self.config(),1)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            row=conn.execute('SELECT * FROM postprocess_jobs WHERE id=?',(r['id'],)).fetchone()
            if row['status'] not in ('queued','running'):break
            time.sleep(.05)
        self.assertEqual(row['status'],'done',row['error'])
        self.assertEqual(jobs.view(row,True)['result']['density_integral_e'],8)
        conn.close()
    def test_recovery_does_not_claim_success(self):
        conn=sqlite3.connect(':memory:');conn.executescript(jobs.SCHEMA)
        conn.execute("INSERT INTO postprocess_jobs(id,tool,status,config,created_at) VALUES('a','pdos','running','{}','now')")
        jobs.recover(conn)
        self.assertEqual(conn.execute('SELECT status FROM postprocess_jobs').fetchone()[0],'interrupted')
        conn.close()

class WiringTests(unittest.TestCase):
    def test_legacy_training_calls_registered_async_path(self):
        import server
        with mock.patch.object(server,'_start_ml_task',return_value=(42,None)) as start:
            result,err=server._run_ml_train({'model':'ridge'})
        self.assertEqual(result,{'task_id':42,'status':'running','async':True})
        self.assertIsNone(err);start.assert_called_once()

    def test_untrusted_code_does_not_execute(self):
        import server
        with mock.patch.object(server.subprocess,'run') as run:
            result,err=server.run_ase_user_code('arbitrary user program')
        self.assertIsNone(result);self.assertIn('隔离',err);run.assert_not_called()

    def test_lammps_models_passed_as_variable(self):
        from catmat_tools import dpgen_adapter as dp
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'input.lammps').write_text('pair_style deepmd ${models}\n')
            (root/'one.pb').write_text('fixture');(root/'two.pb').write_text('fixture')
            with mock.patch.object(dp,'_run',return_value=(True,'','')) as run:
                dp.run_lammps_model_devi(td,'input.lammps',['one.pb','two.pb'])
            cmd=run.call_args.args[0]
            self.assertEqual(cmd[1:3],['-var','models'])
            self.assertIn(str(root/'one.pb'),cmd[3]);self.assertEqual(cmd[4],'-in')

    def test_result_access_requires_owner_or_platform_owner(self):
        import server
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row;conn.executescript(jobs.SCHEMA)
        conn.execute("INSERT INTO postprocess_jobs(id,owner_id,tool,status,config,created_at) VALUES('a',1,'pdos','done','{}','now')")
        handler=server.Handler.__new__(server.Handler)
        handler.auth_user={'id':2};handler._ok=lambda data:('ok',data);handler._err=lambda status,msg:('error',status)
        with mock.patch.object(server,'_effective_role',return_value=server.ROLE_GUEST):
            result=handler._postprocess_tools('GET',['postprocess','tools','jobs','a'],conn,{})
            self.assertEqual(result,('error',404))
            result=handler._postprocess_tools('GET',['postprocess','tools','jobs'],conn,{})
            self.assertEqual(result,('ok',[]))
        conn.close()


class RunnerTests(unittest.TestCase):
    def test_nonzero_success_json_rejected(self):
        with self.assertRaises(RuntimeError):run_json([sys.executable,'-c','print(\'{"ok":true}\');raise SystemExit(2)'])
    def test_invalid_json_and_false_success(self):
        for output in ['not json','{"ok":false,"error":"bad"}']:
            with self.assertRaises(ValueError):run_json([sys.executable,'-c','print('+repr(output)+')'])
    def test_timeout_and_output_limit(self):
        with self.assertRaises(TimeoutError):run_checked([sys.executable,'-c','import time;time.sleep(10)'],timeout=.05)
        with self.assertRaises(RuntimeError):run_checked([sys.executable,'-c','print("x"*100)'],max_output=10)

if __name__=='__main__':unittest.main()
