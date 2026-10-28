#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通用 CP2K 提交脚本（供 `verdi run` 调用），提交真实 AiiDA Cp2kCalculation。

用法：verdi run catmat.py submit-cp2k-verdi <params.json>

params.json 字段：
  code_label          AiiDA code 标签（默认 cp2k@local）
  label               计算标签
  symbols             原子符号列表
  cell                3x3 晶胞矩阵（笛卡尔，Å）
  scaled_positions    分数坐标列表
  pbc                 周期边界（默认 [True,True,True]）
  parameters          CP2K 输入参数（Dict 字典形式）
  resources           {"num_machines": N, "num_mpiprocs_per_machine": M}
  max_wallclock_seconds
输出：成功打印 SUBMITTED_PK=<pk>
"""
import sys
import json

from aiida import load_profile

load_profile()

from aiida.orm import StructureData, Dict, load_code  # noqa: E402
from aiida.engine import submit  # noqa: E402
from aiida.plugins import CalculationFactory  # noqa: E402

Cp2kCalculation = CalculationFactory("cp2k")


def main():
    if len(sys.argv) < 2:
        print("用法: verdi run catmat.py submit-cp2k-verdi <params.json>")
        sys.exit(1)
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        cfg = json.load(f)

    code = load_code(cfg.get("code_label", "cp2k@local"))
    # 结构来源：优先 AiiDA 库中的 StructureData（structure_pk，批量工作流引用，避免重复建库）
    if cfg.get("structure_pk"):
        from aiida.orm import load_node
        structure = load_node(int(cfg["structure_pk"]))
        if "structure.StructureData" not in structure.node_type:
            print("STRUCT_PK=%s 非 StructureData（%s）" % (cfg["structure_pk"], structure.node_type))
            sys.exit(1)
    else:
        from ase import Atoms
        atoms = Atoms(symbols=cfg["symbols"], scaled_positions=cfg["scaled_positions"],
                      cell=cfg["cell"], pbc=cfg.get("pbc", [True, True, True]))
        structure = StructureData(ase=atoms)
    params = Dict(cfg["parameters"])

    builder = Cp2kCalculation.get_builder()
    builder.code = code
    builder.structure = structure
    builder.parameters = params
    builder.metadata.label = cfg.get("label", "cp2k_calc")
    builder.metadata.description = cfg.get("label", "cp2k_calc")
    builder.metadata.options = {
        "resources": cfg.get("resources", {"num_machines": 1, "num_mpiprocs_per_machine": 144}),
        "max_wallclock_seconds": cfg.get("max_wallclock_seconds", 12 * 3600),
        "withmpi": True,
    }

    node = submit(builder)
    print("SUBMITTED_PK=%d" % node.pk)


if __name__ == "__main__":
    main()
