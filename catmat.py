#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CataMind 统一命令行入口。

用法：
    python3 catmat.py serve [server.py 原参数]
    python3 catmat.py <子命令> [原脚本参数]

除 serve 外，子命令均将参数原样转发给 catmat_tools/ 下对应脚本，
因此旧脚本的参数顺序和选项保持不变。AiiDA 子命令仍应通过
`verdi run catmat.py <子命令> ...` 执行。
"""
import os
import runpy
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.join(ROOT, "catmat_tools")

COMMANDS = {
    "postprocess-tools": "postprocess_toolkit.py",
    "aiida-node": "aiida_node.py",
    "aiida-probe": "aiida_probe.py",
    "aiida-task-files": "aiida_task_files.py",
    "al-train": "al_train.py",
    "bader-charge": "bader_charge.py",
    "build-param-db": "build_param_db.py",
    "build-param-refs": "build_param_refs.py",
    "chart-gen": "chart_gen.py",
    "load-structure": "load_structure.py",
    "load-workflow-results": "load_workflow_results.py",
    "ml-train": "ml_train.py",
    "postprocess-pdos": "postprocess_pdos.py",
    "pdos-site-softness": "pdos_site_softness.py",
    "store-structures": "store_structures.py",
    "submit-catmat-workflow": "submit_catmat_workflow.py",
    "submit-cp2k-verdi": "submit_cp2k_verdi.py",
}


def _usage():
    print("用法: python3 catmat.py <子命令> [参数]")
    print("\n服务:")
    print("  serve                    启动 CataMind HTTP 服务（转发 server.py 参数）")
    print("\n功能子命令:")
    for name in sorted(COMMANDS):
        print("  %-24s -> catmat_tools/%s" % (name, COMMANDS[name]))
    print("\nAiiDA 工作流模块由 catmat_tools/setup.py 注册，不作为直接命令执行：")
    print("  安装: pip install -e ./catmat_tools --no-deps")
    print("  模块: catmat_tools.catmat_workflow_chain / catmat_tools.cp2k_workchain_input")


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        _usage()
        return 0
    command = sys.argv[1]
    forwarded = sys.argv[2:]
    if command == "serve":
        import server
        sys.argv = [os.path.join(ROOT, "server.py")] + forwarded
        server.main()
        return 0
    script = COMMANDS.get(command)
    if not script:
        print("未知子命令: %s\n" % command, file=sys.stderr)
        _usage()
        return 2
    path = os.path.join(TOOLS_DIR, script)
    if not os.path.isfile(path):
        print("功能脚本不存在: %s" % path, file=sys.stderr)
        return 2
    sys.argv = [path] + forwarded
    runpy.run_path(path, run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
