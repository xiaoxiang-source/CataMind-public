#!/usr/bin/env python3
"""Versioned postprocessing workers. All results are previews, never auto-admitted."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from catmat_tools import bader_charge as cube_module
from catmat_tools.bader_charge import read_cube, same_grid, density_difference, determinant, number
from catmat_tools.external_runner import run_checked

VERSION='1.0.0'
TOOLS = {
 'density_difference': dict(label='差分电荷密度', inputs=['total_cube','slab_cube','ads_cube'],
    params=['density_unit'], description='同网格逐点相减，输出 Δρ cube、电子数积分及积累/耗尽量；不等同 Bader 电荷。'),
 'bader_import': dict(label='Bader 原子盆结果导入', inputs=['density_cube','acf_file'],
    params=['density_unit','valence_electrons','expected_electrons','electron_tolerance','atom_order_confirmed'],
    description='读取真实 ACF.dat；核对原子序号与总电子数，按显式参考电子数计算净电荷。'),
 'bader_run': dict(label='Bader 原子盆计算', inputs=['density_cube'], optional_inputs=['reference_cube'],
    params=['density_unit','valence_electrons','expected_electrons','electron_tolerance'],
    description='调用配置的 Henkelman Bader 程序；可指定重构密度定义分区，需核查赝势适用性。'),
 'pdos': dict(label='PDOS / 能带中心', inputs=['directory'], params=['sigma','de','erange'],
    description='保留 KIND、元素、自旋与源文件；相对费米能，输出 DOS/能带中心预览。'),
 'output_audit': dict(label='计算输出审查', inputs=['output_file'], params=[],
    description='提取最后一次尝试的能量和结束/收敛标记，保留未知状态，不替代科学验收。'),
}
PLANNED=[dict(id='work_function',label='平面平均势 / 功函数',requirement='势的单位与零点、真空平台窗口、费米能及 slab 偶极设置'),
 dict(id='vibration_shermo',label='振动 / Shermo 热力学校正',requirement='活动原子与模式数、虚频处理、温度/压力、Shermo 结果与方法版本'),
 dict(id='free_energy',label='吸附能 / CHE 自由能',requirement='匹配参考态、能量方法一致、ZPE/热力学校正及显式反应计量'),
 dict(id='local_reactivity',label='局域反应性 / Fukui / 软度',requirement='KIND 映射、物理通道、展宽窗口与全局软度定义的独立验证')]


def bader_binary():
    candidate=os.environ.get('CATMAT_BADER_BIN') or shutil.which('bader')
    return str(Path(candidate).resolve()) if candidate and os.path.isfile(candidate) and os.access(candidate,os.X_OK) else None


def catalog():
    rows=[]
    for key,spec in TOOLS.items():
        enabled=key != 'bader_run' or bool(bader_binary())
        rows.append(dict(spec,id=key,available=enabled,status='ready' if enabled else 'dependency_missing',
                         reason='' if enabled else '未配置可执行的 CATMAT_BADER_BIN；可先导入 ACF.dat'))
    return dict(version=VERSION,tools=rows,planned=PLANNED,
                result_policy='所有结果仅为预览；验收与入库分离，不自动写入训练集。')


def fingerprint(path):
    p=Path(path).resolve()
    h=hashlib.sha256()
    with p.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return dict(path=str(p),size_bytes=p.stat().st_size,sha256=h.hexdigest())


def source_paths(tool,inputs):
    if tool=='pdos':
        return sorted(Path(inputs['directory']).glob('*.pdos'))
    return [Path(p) for p in inputs.values() if p]


def parse_acf(path,natoms):
    atoms=[]; vacuum=None; reported=None
    for line in Path(path).read_text().splitlines():
        cols=line.split()
        if cols and cols[0].isdigit():
            if len(cols)<7:raise ValueError('ACF 原子记录不完整')
            values=list(map(number,cols[1:7]))
            if values[3]<0 or values[5]<0:raise ValueError('ACF 电子数或体积为负')
            atoms.append(dict(index=int(cols[0]),position=values[:3],basin_electrons=values[3],
                              min_distance=values[4],basin_volume=values[5]))
        elif 'VACUUM CHARGE:' in line:vacuum=number(line.split(':')[-1].strip())
        elif 'NUMBER OF ELECTRONS:' in line:reported=number(line.split(':')[-1].strip())
    if [a['index'] for a in atoms] != list(range(1,natoms+1)):
        raise ValueError('ACF 原子数量/序号与密度 cube 不匹配')
    if not atoms:raise ValueError('ACF 无原子记录')
    return atoms,vacuum,reported


def bader_result(cube,acf,params):
    atoms,vacuum,reported=parse_acf(acf,cube['natoms'])
    zvals=params.get('valence_electrons')
    if zvals is not None:
        if not isinstance(zvals,list) or len(zvals)!=len(atoms):raise ValueError('参考电子数必须按原子序逐项提供')
        zvals=[number(v) for v in zvals]
        if any(v<0 for v in zvals):raise ValueError('参考电子数必须非负')
        for row,z in zip(atoms,zvals):row['net_charge_e']=z-row['basin_electrons']
    basin=math.fsum(a['basin_electrons'] for a in atoms)
    dv=abs(determinant(cube['vx'],cube['vy'],cube['vz']))
    density_integral=math.fsum(cube['data'])*dv
    total=basin+vacuum if vacuum is not None else None
    tol=number(params.get('electron_tolerance',0.01))
    if tol<=0:raise ValueError('电子数容差必须为正')
    expected=params.get('expected_electrons')
    expected=number(expected) if expected is not None else None
    if expected is not None and expected<0:raise ValueError('预期电子数不能为负')
    checks=[dict(name='density_vs_partition',ok=None if total is None else abs(total-density_integral)<=tol,
                 difference_e=None if total is None else total-density_integral),
            dict(name='expected_electrons',ok=None if expected is None or total is None else abs(total-expected)<=tol),
            dict(name='acf_reported_total',ok=None if reported is None or total is None else abs(total-reported)<=tol)]
    return dict(atoms=atoms,basin_electrons=basin,vacuum_electrons=vacuum,reported_electrons=reported,
                density_integral_e=density_integral,checks=checks,
                warnings=['电子数守恒不代表原子盆已收敛；需检查网格、赝势/重构参考密度、原子映射。',
                          '净电荷 = 显式参考电子数 − 原子盆电子数；参考值需匹配所用电子密度。'] +
                         ([] if zvals is not None else ['未提供参考电子数，仅返回原子盆电子数，不猜测净电荷。']))


def audit_output(path):
    ended=False; aborted=False; scf_failed=False; energy=None; run_type=None; geometry=False; cell=False
    attempts=0
    with open(path,errors='replace') as f:
        for line in f:
            if 'PROGRAM STARTED AT' in line:
                attempts+=1; ended=aborted=scf_failed=geometry=cell=False; energy=None; run_type=None
            if 'PROGRAM ENDED AT' in line:ended=True
            if 'ABORT' in line:aborted=True
            if 'SCF run NOT converged' in line:scf_failed=True
            if 'GEOMETRY OPTIMIZATION COMPLETED' in line:geometry=True
            if 'CELL OPTIMIZATION COMPLETED' in line:cell=True
            m=re.search(r'GLOBAL\|\s+Run type\s+(\S+)',line)
            if m:run_type=m[1]
            m=re.search(r'ENERGY\|.*?energy.*?:\s*([-+\d.EeDd]+)',line,re.I)
            if m:energy=number(m[1])
    return dict(attempts=attempts,run_type=run_type,normal_end=ended,abort=aborted,scf_failure=scf_failed,
                geometry_completed=geometry,cell_completed=cell,last_energy_hartree=energy,
                execution_status='failed' if aborted or scf_failed else ('ended' if ended else 'incomplete_or_running'),
                warnings=['仅解析最后一次输出尝试；未检查宿主 PID 或退出码，也未完成振动模式/力阈值等科学验收。'])


def run_tool(config,workdir):
    tool=config.get('tool'); inputs=config.get('inputs') or {}; params=config.get('params') or {}
    if tool not in TOOLS:raise ValueError('未知或未实现工具')
    spec=TOOLS[tool]
    if not isinstance(inputs,dict) or not isinstance(params,dict):raise ValueError('inputs/params 必须为对象')
    if set(inputs)-set(spec['inputs']+spec.get('optional_inputs',[])):raise ValueError('未知输入字段')
    if set(params)-set(spec['params']):raise ValueError('未知参数')
    for key in spec['inputs']:
        if not inputs.get(key):raise ValueError('缺少输入: '+key)
    if tool.startswith('bader') or tool=='density_difference':
        if params.get('density_unit')!='e/bohr^3':raise ValueError('需显式确认 density_unit=e/bohr^3')
    workdir=Path(workdir).resolve();workdir.mkdir(parents=True,exist_ok=True)
    sources=source_paths(tool,inputs)
    if not sources:raise ValueError('未找到输入文件')
    before=[fingerprint(p) for p in sources]
    # Outputs are immutable per run; never overwrite input or previous evidence.
    for name in ('delta.cube','density.cube','reference.cube','ACF.dat','bader.log','pdos.csv','result.json'):
        if (workdir/name).exists():raise ValueError('工作目录已有产物，请使用新的目录: '+name)
    artifacts=[]
    if tool=='density_difference':
        out=workdir/'delta.cube'
        result=density_difference(inputs['total_cube'],inputs['slab_cube'],inputs['ads_cube'],out)
        artifacts=['delta.cube']
    elif tool=='pdos':
        from catmat_tools.postprocess_pdos import process_dir
        sigma=number(params.get('sigma',.05));de=number(params.get('de',.01))
        erange=params.get('erange',[-15.,5.])
        if not isinstance(erange,list) or len(erange)!=2:raise ValueError('erange 需两个能量边界')
        erange=tuple(map(number,erange))
        if sigma<=0 or de<=0 or erange[1]<=erange[0] or (erange[1]-erange[0])/de>50000:
            raise ValueError('展宽/能量网格非法或超过 50000 点')
        result=process_dir(inputs['directory'],sigma,de,erange,spectrum_path=workdir/'pdos.csv')
        artifacts=['pdos.csv']
    elif tool in ('bader_import','bader_run'):
        cube=read_cube(inputs['density_cube'])
        if any(v < -1e-10 for v in cube['data']):raise ValueError('Bader 输入需非负电子密度，不能传差分密度')
        if tool=='bader_import':
            if params.get('atom_order_confirmed') is not True:raise ValueError('需确认 ACF 与 cube 的原子顺序一致')
            acf=inputs['acf_file']
        else:
            binary=bader_binary()
            if not binary:raise ValueError('缺少 CATMAT_BADER_BIN 可执行程序')
            density=workdir/'density.cube';shutil.copyfile(inputs['density_cube'],density)
            argv=[binary,str(density)]
            if inputs.get('reference_cube'):
                ref=read_cube(inputs['reference_cube']);same_grid(cube,ref)
                if ref['atoms']!=cube['atoms']:raise ValueError('参考密度原子记录不一致')
                reference=workdir/'reference.cube';shutil.copyfile(inputs['reference_cube'],reference)
                argv += ['-ref',str(reference)]
            out,err=run_checked(argv,cwd=str(workdir),timeout=240,own_group=False,env=dict(os.environ,OMP_NUM_THREADS='1'))
            (workdir/'bader.log').write_text(out+'\n'+err)
            acf=workdir/'ACF.dat'
            if not acf.is_file():raise ValueError('Bader 未生成 ACF.dat')
            artifacts=['ACF.dat','bader.log']
        result=bader_result(cube,acf,params)
        if tool=='bader_run':result['engine']=fingerprint(binary)
    else:result=audit_output(inputs['output_file'])
    after=[fingerprint(p) for p in source_paths(tool,inputs)]
    if before!=after:raise ValueError('分析期间输入变化，结果作废，请等待文件稳定后重试')
    for name in artifacts:
        if (workdir/name).resolve() in {Path(x['path']) for x in before}:raise ValueError('产物覆盖输入')
    result.update(tool=tool,toolkit_version=VERSION,code_sha256=fingerprint(__file__)['sha256'],review_status='preview',parameters=params,
                  sources=before,artifacts=[dict(name=name,**fingerprint(workdir/name)) for name in artifacts],
                  created_at=time.strftime('%Y-%m-%d %H:%M:%S'),
                  implementation_sources=[fingerprint(__file__),fingerprint(cube_module.__file__)])
    (workdir/'result.json').write_text(json.dumps(result,ensure_ascii=False,allow_nan=False,indent=2))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['catalog','run'])
    parser.add_argument('config',nargs='?');parser.add_argument('--workdir')
    args=parser.parse_args()
    try:
        if args.action=='catalog':result=catalog()
        else:
            if not args.config or not args.workdir:raise ValueError('run 需要 config 和 --workdir')
            result=run_tool(json.loads(Path(args.config).read_text()),args.workdir)
        print(json.dumps(dict(ok=True,result=result),ensure_ascii=False,allow_nan=False))
    except Exception as exc:
        print(json.dumps(dict(ok=False,error=str(exc)),ensure_ascii=False));return 1
    return 0

if __name__=='__main__':sys.exit(main())
