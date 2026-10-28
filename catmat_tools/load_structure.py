#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从 AiiDA 库读取 StructureData 并输出 JSON（供 `verdi run` 调用）。

用法：verdi run catmat.py load-structure <pk>
输出：JSON 单行 {label, symbols, cell, scaled_positions, pbc}
用途：高通量工作流「通过 PK 汇总优化后的结构 → 重新导入 ASE 做位点分析」。
"""
import sys
import json

from aiida import load_profile

load_profile()

from aiida.orm import load_node  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print("用法: verdi run catmat.py load-structure <pk>")
        sys.exit(1)
    pk = int(sys.argv[1])
    node = load_node(pk)
    atoms = node.get_ase()
    print(json.dumps({
        "pk": pk,
        "label": node.label or str(pk),
        "symbols": atoms.get_chemical_symbols(),
        "cell": atoms.get_cell().tolist(),
        "scaled_positions": atoms.get_scaled_positions().tolist(),
        "pbc": [bool(x) for x in atoms.get_pbc()],
    }))


if __name__ == "__main__":
    main()
