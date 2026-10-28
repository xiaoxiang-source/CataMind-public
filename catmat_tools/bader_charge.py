#!/usr/bin/env python3
"""Legacy CLI for density DIFFERENCE, not atomic Bader partitioning.

Positive voxel counts / Bohr coordinates / scalar electron density in e/Bohr^3
only. Reject unsupported cube variants rather than guessing units.
"""
import argparse
from array import array
import json
import math
import os
from pathlib import Path

MAX_POINTS = 20_000_000


def number(value):
    result = float(str(value).replace('D', 'E').replace('d', 'e'))
    if not math.isfinite(result):
        raise ValueError('非有限数值')
    return result


def determinant(a, b, c):
    return a[0]*(b[1]*c[2]-b[2]*c[1])-a[1]*(b[0]*c[2]-b[2]*c[0])+a[2]*(b[0]*c[1]-b[1]*c[0])


def read_cube(path):
    with open(path, encoding='utf-8') as f:
        com1, com2 = f.readline().rstrip(), f.readline().rstrip()
        head = f.readline().split()
        if len(head) not in (4, 5) or (len(head) == 5 and int(head[4]) != 1):
            raise ValueError('仅支持单标量 cube')
        natoms = int(head[0])
        if natoms < 0 or natoms > 100000:
            raise ValueError('不支持轨道/多数据集 cube 或非法原子数')
        result = dict(com1=com1, com2=com2, natoms=natoms, origin=list(map(number, head[1:4])))
        for n, v in [('nx','vx'),('ny','vy'),('nz','vz')]:
            parts = f.readline().split()
            if len(parts) != 4 or int(parts[0]) <= 0:
                raise ValueError('需正网格数（Bohr）；不支持负网格数的单位约定')
            result[n], result[v] = int(parts[0]), list(map(number, parts[1:]))
        count = result['nx']*result['ny']*result['nz']
        if count > MAX_POINTS:
            raise ValueError('网格超过 %d 点上限，请使用离线大文件分析' % MAX_POINTS)
        if abs(determinant(result['vx'], result['vy'], result['vz'])) < 1e-15:
            raise ValueError('网格体积为零')
        atoms = []
        for _ in range(natoms):
            parts = f.readline().split()
            if len(parts) != 5:
                raise ValueError('原子记录不完整')
            atoms.append([int(parts[0])] + list(map(number, parts[1:])))
        result['atoms'] = atoms
        values = array('d')
        for line in f:
            for token in line.split():
                if len(values) >= count:
                    raise ValueError('cube 数据点超出声明网格')
                values.append(number(token))
        if len(values) != count:
            raise ValueError('cube 数据截断：声明 %d，实际 %d' % (count, len(values)))
        result['data'] = values
        return result


def same_grid(a, b):
    if any(a[k] != b[k] for k in ('nx','ny','nz')):
        raise ValueError('网格尺寸不一致')
    for k in ('origin','vx','vy','vz'):
        if any(not math.isclose(x, y, rel_tol=1e-8, abs_tol=1e-7) for x,y in zip(a[k],b[k])):
            raise ValueError('网格原点或步进向量不一致: '+k)


def write_cube(cube, data, path):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(cube['com1']+'\n'+cube['com2']+'\n')
        f.write('%5d % .10e % .10e % .10e\n' % (cube['natoms'], *cube['origin']))
        for n,v in [('nx','vx'),('ny','vy'),('nz','vz')]:
            f.write('%5d % .10e % .10e % .10e\n' % (cube[n], *cube[v]))
        for a in cube['atoms']:
            f.write('%5d % .10e % .10e % .10e % .10e\n' % tuple(map(number,a)))
        for i,v in enumerate(data):
            f.write(' % .10e%s' % (v, '\n' if (i+1)%6 == 0 else ''))
        f.write('\n')


def density_difference(total_path, slab_path, ads_path, output):
    if Path(output).resolve() in {Path(p).resolve() for p in (total_path,slab_path,ads_path)}:
        raise ValueError('输出不能覆盖输入')
    total, slab, ads = (read_cube(p) for p in (total_path,slab_path,ads_path))
    same_grid(total, slab); same_grid(total, ads)
    delta = array('d', (t-s-a for t,s,a in zip(total['data'],slab['data'],ads['data'])))
    dv = abs(determinant(total['vx'],total['vy'],total['vz']))
    integrated = math.fsum(delta)*dv
    positive = math.fsum(x for x in delta if x > 0)*dv
    negative = math.fsum(x for x in delta if x < 0)*dv
    total['com2'] = 'Delta electron density: total - slab - ads; e/Bohr^3'
    write_cube(total, delta, output)
    return dict(delta_cube=str(output), density_integral_e=integrated,
                accumulation_e=positive, depletion_e=negative, voxel_volume_bohr3=dv,
                grid=[total[k] for k in ('nx','ny','nz')], charge_transfer=None,
                warnings=['全空间差分积分不等于吸附物转移电荷；需要原子盆或明确空间分区。',
                          '需人工确认三份密度使用相同冻结几何、电子密度定义及计算设置。'])


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    for key in ('total','slab','ads','output'): ap.add_argument(key)
    ap.add_argument('--multiwfn', help='仅验证文件可读，不执行 Bader 分区')
    args=ap.parse_args()
    try:
        result=density_difference(args.total,args.slab,args.ads,args.output)
        if args.multiwfn:
            from catmat_tools.external_runner import run_checked
            out,err=run_checked([args.multiwfn,str(Path(args.output).resolve())],input_text='q\n',timeout=90,
                                cwd=str(Path(args.output).resolve().parent),
                                env=dict(os.environ,OMP_NUM_THREADS='1'))
            result['warnings'].append('Multiwfn 进程正常退出；未据此宣称完成 Bader 分析。')
        print(json.dumps(dict(ok=True,result=result),ensure_ascii=False,allow_nan=False))
    except Exception as exc:
        print(json.dumps(dict(ok=False,error=str(exc)),ensure_ascii=False))
        return 1
    return 0

if __name__ == '__main__':
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    sys.exit(main())
