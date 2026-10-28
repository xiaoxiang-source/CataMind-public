#!/usr/bin/env python3
"""Offline regressions safe to run without a production DB or external compute stack."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TESTS = [
    'test_release_packaging', 'test_postprocess_picker', 'test_postprocess_toolkit',
    'test_scientific_integrity', 'test_process_scheduling', 'test_governor_wiring',
    'test_governor_batch_wiring', 'test_governor_queue_order', 'test_resource_governor',
    'test_resource_defaults', 'test_dpgen_adapter', 'test_cli', 'test_desktop_release',
    'test_global_lock_scope.TestSubprocessCoverage',
    'test_global_lock_scope.TestBlockingUnlockedSemantics',
]

def main():
    env = dict(os.environ, OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1')
    subprocess.run([sys.executable, '-m', 'unittest', *['tests.'+x for x in TESTS], '-q'],
                   cwd=ROOT, env=env, check=True)
    for name in ['release', 'scientific', 'process_monitor', 'postprocess']:
        subprocess.run(['node', 'tests/test_'+name+'_frontend.js'], cwd=ROOT, env=env, check=True)
    subprocess.run([sys.executable, 'scripts/prepare_release.py'], cwd=ROOT, check=True)
    return 0

if __name__ == '__main__':
    sys.exit(main())
