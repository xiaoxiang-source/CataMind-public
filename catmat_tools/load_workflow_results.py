#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""读取已完成 CatMat 工作链的 results 输出（供 `verdi run` 调用）。

用法: verdi run catmat.py load-workflow-results <WORKCHAIN_PK>

从工作链的 `results` 输出（List of {label, energy, optimization_pk,
single_point_pk, output_structure_pk}）读出并以 JSON 数组打印一行，供
server.py 的 load_workflow_results() 解析。工作链仍运行 / 尚无 results 时
打印空数组 []（调用方忽略即可）。
"""
import sys
import json

from aiida import load_profile

load_profile()

from aiida.orm import load_node  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print("用法: verdi run catmat.py load-workflow-results <WORKCHAIN_PK>")
        sys.exit(1)
    pk = int(sys.argv[1])
    try:
        node = load_node(pk)
    except Exception as e:
        print("无法加载节点 %s: %s" % (pk, e))
        print("[]")
        sys.exit(0)

    results = None
    try:
        if "results" in node.outputs:
            results = node.outputs.results.get_list()
    except Exception:
        results = None

    if results is None:
        # 兜底：尝试从 attributes 取（不同 aiida 版本差异）
        try:
            results = node.get_attribute("results", None)
        except Exception:
            results = None

    if results is None:
        print("[]")
        sys.exit(0)
    if not isinstance(results, list):
        try:
            results = list(results)
        except Exception:
            results = [results]
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
