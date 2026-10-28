#!/usr/bin/env python3
"""Versioned CP2K PDOS preview. No database writes; DOS units are states/eV.

KIND projections are retained and summed by element. Spin channels are summed.
Ambiguous repeated snapshots or k-point files are rejected, never averaged.
"""
import argparse
import hashlib
import io
import json
import os
import re
import sys
from pathlib import Path

import numpy as np

AU_EV = 27.211386245988
PARSER_VERSION = '2.1.0'
NUMBER = r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[EeDd][-+]?\d+)?'


def _read_pdos(path):
    raw = Path(path).read_bytes()
    text = raw.decode('utf-8')
    lines = text.splitlines()
    kind = ef = labels = None
    step = None
    for line in lines:
        if not line.strip().startswith('#'):
            continue
        m = re.search(r'Projected DOS for atomic kind (\w+)', line)
        if m:
            kind = m[1]
        m = re.search(r'E\(Fermi\)\s*=\s*(' + NUMBER + r')\s+a\.u\.', line)
        if m:
            ef = float(m[1].replace('D', 'E').replace('d', 'e')) * AU_EV
        m = re.search(r'iteration step i\s*=\s*(\d+)', line)
        if m:
            step = int(m[1])
        if re.search(r'\bMO\b', line) and 'Occupation' in line:
            labels = line.split('Occupation', 1)[1].split()
    if kind is None or ef is None or not labels:
        raise ValueError('缺少 KIND、E(Fermi) 或轨道列头: ' + str(path))
    data = np.loadtxt(io.StringIO(text.replace('D+', 'E+').replace('D-', 'E-')), ndmin=2)
    if not np.isfinite(data).all() or not np.isfinite(ef) or data.shape[1] != 3 + len(labels):
        raise ValueError('PDOS 列数与表头不一致或存在非有限值: ' + str(path))
    indices = [i for i, name in enumerate(labels) if name.lower() != 'total']
    orbital_labels = [labels[i].lower() for i in indices]
    if not orbital_labels or any(not re.match(r'^[spdfgh]', x) for x in orbital_labels):
        raise ValueError('需要明确的 s/p/d/... 轨道分量，不能从 Total 推断投影: ' + str(path))
    density = data[:, [3+i for i in indices]]
    if np.any(density < -1e-8):
        raise ValueError('投影含负权重，需单独科学核查: ' + str(path))
    spin = re.search(r'(?:^|[-_])(ALPHA|BETA)(?:[-_.]|$)', Path(path).name, re.I)
    return {'kind': kind, 'efermi': ef, 'energies': data[:, 1]*AU_EV-ef,
            'weights': density, 'labels': orbital_labels, 'spin': spin[1].upper() if spin else 'unresolved',
            'source': {'file': str(Path(path).resolve()), 'sha256': hashlib.sha256(raw).hexdigest(),
                       'size_bytes': len(raw), 'kind': kind, 'iteration_step': step,
                       'efermi_eV': ef, 'orbital_columns': orbital_labels}}


def trapz(y, x):
    return np.trapezoid(y, x) if hasattr(np, 'trapezoid') else np.trapz(y, x)


def element_symbol(kind):
    m = re.match(r'([A-Z][a-z]?)', kind)
    return m[1] if m else kind


def _spectrum(record, grid, sigma):
    out = np.zeros((len(grid), record['weights'].shape[1]))
    for i, energy in enumerate(grid):
        # Normalized Gaussian density (states/eV); integration supplies dE.
        w = np.exp(-.5*((energy-record['energies'])/sigma)**2)/(sigma*np.sqrt(2*np.pi))
        out[i] = w @ record['weights']
    return out


def process_dir(directory, sigma=0.05, de=0.01, erange=(-15.0, 5.0), spectrum_path=None):
    if not all(np.isfinite(x) for x in (sigma, de, *erange)) or sigma <= 0 or de <= 0 or erange[0] >= erange[1]:
        raise ValueError('sigma/de 必须为正，积分范围必须递增且有限')
    if (erange[1]-erange[0])/de > 100000:
        raise ValueError('能量网格过大')
    files = sorted(Path(directory).glob('*.pdos'))
    if not files:
        raise FileNotFoundError('目录中无 .pdos 文件: ' + str(directory))
    records = [_read_pdos(p) for p in files]
    if np.ptp([r['efermi'] for r in records]) > 1e-4:
        raise ValueError('各投影的 Fermi 能级不一致，不能自动合并不同计算/帧')
    steps = {r['source']['iteration_step'] for r in records}
    if len(steps) != 1:
        raise ValueError('目录包含不同 iteration step，须先选择同一帧')
    groups = {}
    for r in records:
        groups.setdefault(r['kind'], []).append(r)
    grid = np.linspace(erange[0], erange[1], int(round((erange[1]-erange[0])/de))+1)
    kind_spectra = {}; element_spectra = {}; warnings = []
    for kind, recs in groups.items():
        if len(recs) > 1 and (len(recs) != 2 or {r['spin'] for r in recs} != {'ALPHA','BETA'}):
            raise ValueError('KIND %s 有多份投影：无法区分重复帧/k 点权重，禁止自动平均' % kind)
        if len(recs) == 1 and recs[0]['spin'] != 'unresolved':
            raise ValueError('KIND %s 缺少配对自旋通道' % kind)
        parts = {orb: np.zeros(len(grid)) for orb in ('p','d','total')}
        for r in recs:
            spectrum = _spectrum(r, grid, sigma)
            parts['total'] += spectrum.sum(axis=1)
            for orb in ('p','d'):
                idx = [i for i,x in enumerate(r['labels']) if x.startswith(orb)]
                if idx:
                    parts[orb] += spectrum[:, idx].sum(axis=1)
        kind_spectra[kind] = parts
        sym = element_symbol(kind)
        dest = element_spectra.setdefault(sym, {orb: np.zeros(len(grid)) for orb in parts})
        for orb in parts:
            dest[orb] += parts[orb]

    def metrics(parts):
        def center(dens):
            integral = float(trapz(dens, grid))
            return float(trapz(grid*dens, grid)/integral) if integral > 1e-12 else None
        def at_zero(dens):
            return float(np.interp(0, grid, dens)) if grid[0] <= 0 <= grid[-1] else None
        return {'d_band_center': center(parts['d']), 'p_band_center': center(parts['p']),
                'd_ldos_fermi': at_zero(parts['d']), 'p_ldos_fermi': at_zero(parts['p']),
                'total_dos_fermi': at_zero(parts['total'])}
    kinds = {k: dict(metrics(v), element=element_symbol(k), files=[r['source']['file'] for r in groups[k]]) for k,v in kind_spectra.items()}
    elements = {k: dict(metrics(v), kinds=[kind for kind in kinds if element_symbol(kind)==k]) for k,v in element_spectra.items()}
    total = {orb: sum(v[orb] for v in kind_spectra.values()) for orb in ('p','d','total')}
    summary = metrics(total)
    summary['n_elements'] = len(elements)
    summary['n_kinds'] = len(kinds)
    warnings += ['投影为基组相关的 PDOS；LDOS(E_F) 不等同于严格的局部软度或凝聚 Fukui。',
                 '积分范围为所选能窗内全部态（未按占据数加权）；带中心以 E_F=0 为参考。',
                 '须人工核查计算收敛、投影完整性、同批基组/自旋/k 点及能窗一致性。']
    if any(v['d_band_center'] is None for v in elements.values()):
        warnings.append('部分元素在所选范围无 d 投影，d 带中心记为 null，不能作为零值特征。')
    if spectrum_path is not None:
        import csv
        # Wide KIND-resolved table avoids returning large spectra in HTTP JSON.
        with open(spectrum_path, 'x', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            keys = [(kind, orb) for kind in sorted(kind_spectra) for orb in ('p', 'd', 'total')]
            writer.writerow(['energy_minus_fermi_eV'] + [kind + ':' + orb + '_states_per_eV' for kind,orb in keys])
            for i, energy in enumerate(grid):
                writer.writerow([float(energy)] + [float(kind_spectra[kind][orb][i]) for kind,orb in keys])
    return {'elements': elements, 'kinds': kinds, 'summary': summary,
            'provenance': {'parser': 'catmat_tools.postprocess_pdos', 'parser_version': PARSER_VERSION,
                           'parser_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                           'sources': [r['source'] for r in records], 'energy_reference': 'E - E_F',
                           'units': {'energy': 'eV', 'band_center': 'eV', 'dos': 'states/eV'},
                           'projection_scope': {'kinds': sorted(kinds), 'aggregation': 'sum KIND projections and paired spin channels; no k-point averaging'},
                           'parameters': {'sigma_eV': sigma, 'de_eV': float(grid[1]-grid[0]), 'energy_window_eV': list(erange), 'occupation_weighted': False}},
            'warnings': warnings}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('directory')
    ap.add_argument('--sigma', type=float, default=0.05)
    ap.add_argument('--de', type=float, default=0.01)
    ap.add_argument('--erange', nargs=2, type=float, default=[-15.0, 5.0])
    args = ap.parse_args()
    try:
        result = process_dir(args.directory, args.sigma, args.de, tuple(args.erange))
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False, allow_nan=False))
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == '__main__':
    main()
