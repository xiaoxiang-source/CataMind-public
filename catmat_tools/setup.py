#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CatMat 平台 AiiDA 工作流安装配置。

参照 irru_adsorption_workchain.py 的 setup.py：
  - 将工作流模块注册为 aiida.workflows entry point，使 daemon 可通过
    WorkflowFactory('catmat.workflow' / 'catmat.batch') 加载；
  - 安装配置与功能脚本统一放在 catmat_tools 包；
  - 通过 package_dir 指向项目根目录，继续安装根目录的 catmat.py、server.py。

安装（在 aiida 环境中执行一次）：
    conda activate aiida
    cd <catmat-platform>
    pip install -e ./catmat_tools --no-deps
    verdi plugin list aiida.workflows  # 验证 entry point 已被发现
    verdi daemon restart  # 使 daemon 加载新插件（仅在维护窗口执行）
"""
import os

from setuptools import setup, find_packages

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

setup(
    name="catmat-workflow",
    version="0.1.0",
    description="CatMat HTS AiiDA workflow chains (per-structure chained & wavefront)",
    package_dir={"": ".."},
    packages=find_packages(where="..", include=["catmat_tools", "catmat_tools.*"],
                           exclude=["catmat_tools.*.egg-info"]),
    entry_points={
        "aiida.workflows": [
            "catmat.workflow = catmat_tools.catmat_workflow_chain:CatMatWorkflowChain",
            "catmat.batch = catmat_tools.catmat_workflow_chain:CatMatBatchWorkflowChain",
        ],
        "console_scripts": [
            "catmat = catmat:main",
        ],
    },
    py_modules=["catmat", "server", "catmat_postprocessing", "catmat_process_monitor", "catmat_resource_governor"],
)
