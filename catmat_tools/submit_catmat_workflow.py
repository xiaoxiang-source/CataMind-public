#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""提交 CatMat 链式工作流（供 `verdi run` 调用）。

参照 run_adsorption.py（WorkflowFactory + submit(builder) 稳定提交）与
submit_cp2k_verdi.py（JSON 配置驱动）的模式：

  load_profile()
  -> 读取 params.json
  -> 将结构入库为 StructureData（或引用已有 aiida_pk）
  -> WorkflowFactory(entry_point) + submit(builder)
  -> 打印 SUBMITTED_PK=<pk>

由 server.py 经子进程 `verdi run catmat.py submit-catmat-workflow <config.json>` 调用，
保证「稳定 AiiDA 提交」且终端/服务关闭不影响 daemon 执行。

config.json 字段：
  code_label         AiiDA code 标签（默认 cp2k@local）
  workchain         entry point：'catmat.workflow'（方案A/默认）或 'catmat.batch'（方案B）
  periodic          周期边界（默认 XY）
  scf_method        diag / ot（默认 diag）
  sp_requests       ["pdos","bader","d_band"]（默认全开）
  options           {"resources": {"num_machines":1,"num_mpiprocs_per_machine":144}}
  geo_wallclock_seconds    GEO_OPT 单步 Wall-clock（秒，默认 10800）
  sp_wallclock_seconds     单点能单步 Wall-clock（秒，默认 21600）
  structures         [
                        {"label": "...", "aiida_pk": 123}                     # 已入库
                        {"label": "...", "symbols":[..], "cell":[[..]],       # 需入库
                         "scaled_positions":[[..]], "pbc":[true,true,true]}
                     ]
输出：成功打印 SUBMITTED_PK=<pk>
"""
import sys
import json

from aiida import load_profile

load_profile()

from aiida.engine import submit  # noqa: E402
from aiida.orm import Dict, Int, List, Str, StructureData, load_code  # noqa: E402
from aiida.plugins import WorkflowFactory  # noqa: E402
from ase import Atoms  # noqa: E402


def _store_structure(s):
    """从原始结构字典构造并入库 StructureData，返回 PK。"""
    atoms = Atoms(symbols=s["symbols"],
                  scaled_positions=s["scaled_positions"],
                  cell=s["cell"],
                  pbc=s.get("pbc", [True, True, True]))
    structure = StructureData(ase=atoms)
    if s.get("label"):
        structure.label = s["label"]
    structure.store()
    return structure.pk


def main():
    if len(sys.argv) < 2:
        print("用法: verdi run catmat.py submit-catmat-workflow <config.json>")
        sys.exit(1)
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        cfg = json.load(f)

    workchain = cfg.get("workchain", "catmat.workflow")
    code_label = cfg.get("code_label", "cp2k@local")

    raw_structs = cfg.get("structures") or []
    if not raw_structs:
        print("错误：structures 为空")
        sys.exit(1)

    pks, labels = [], []
    for i, s in enumerate(raw_structs):
        label = s.get("label") or ("item%d" % (i + 1))
        if s.get("aiida_pk"):
            pks.append(int(s["aiida_pk"]))
        else:
            pks.append(_store_structure(s))
        labels.append(label)
        print("  结构 %s -> PK=%d" % (label, pks[-1]))

    code = load_code(code_label)
    wf_class = WorkflowFactory(workchain)
    builder = wf_class.get_builder()

    builder.cp2k_code = code
    builder.structure_pks = List(list=pks)
    builder.labels = List(list=labels)
    builder.periodic = Str(str(cfg.get("periodic", "XY")))
    builder.scf_method = Str(str(cfg.get("scf_method", "diag")))
    builder.sp_requests = List(list=cfg.get("sp_requests", ["pdos", "bader", "d_band"]))
    if cfg.get("options"):
        builder.options = Dict(cfg["options"])
    if cfg.get("geo_wallclock_seconds"):
        builder.geo_wallclock = Int(int(cfg["geo_wallclock_seconds"]))
    if cfg.get("sp_wallclock_seconds"):
        builder.sp_wallclock = Int(int(cfg["sp_wallclock_seconds"]))

    print("提交 %d 个结构的 %s 工作链给 daemon..." % (len(pks), workchain))
    node = submit(builder)

    print("SUBMITTED_PK=%d" % node.pk)
    print("关闭终端不影响运行（由 daemon 执行）")
    print("监控: verdi process list | 日志: verdi process report %d" % node.pk)


if __name__ == "__main__":
    main()
