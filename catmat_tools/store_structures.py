#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""批量把 ASE 生成的结构存入 AiiDA 数据库（供 `verdi run` 调用）。

用法：verdi run catmat.py store-structures <structures.json>

structures.json：JSON 数组，每项：
  {"label": "Ir111_a3.84", "symbols": [...], "cell": 3x3, "scaled_positions": [...],
   "pbc": [true,true,true], "description": "..."}

输出：每个结构打印 STRUCT_PK=<pk> <label>，最后打印 DONE <count>。
"""
import sys
import json

from aiida import load_profile

load_profile()

from aiida.orm import StructureData  # noqa: E402
from ase import Atoms  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print("用法: verdi run catmat.py store-structures <structures.json>")
        sys.exit(1)
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        data = json.load(f)
    pks = []
    for item in data:
        try:
            atoms = Atoms(symbols=item["symbols"],
                          scaled_positions=item["scaled_positions"],
                          cell=item["cell"],
                          pbc=item.get("pbc", [True, True, True]))
            sd = StructureData(ase=atoms)
            sd.label = item.get("label", "hts_structure")[:250]
            sd.description = item.get("description", "")[:250]
            sd.store()
            pks.append(sd.pk)
            print("STRUCT_PK=%d %s" % (sd.pk, item.get("label", "")))
        except Exception as e:
            print("STRUCT_ERR %s: %s" % (item.get("label", "?"), e))
    print("DONE %d" % len(pks))


if __name__ == "__main__":
    main()
