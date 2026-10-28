#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""检索 AiiDA 任务（PK）的工作路径与文件，并可读取文件内容（供 `verdi run` 调用）。

用法：
    verdi run catmat.py aiida-task-files <pk>              # 列出任务路径 + 文件清单
    verdi run catmat.py aiida-task-files <pk> <filename>   # 读取指定文件内容（截断 20000 字符）

输出：JSON 单行
    {pk, label, process_type, state, exit_status, workdir, workdir_files,
     retrieved_files, content?(读取模式)}
"""
import sys
import os
import json

from aiida import load_profile

load_profile()

from aiida.orm import load_node  # noqa: E402

MAX_CHARS = 20000


def main():
    if len(sys.argv) < 2:
        print("用法: verdi run catmat.py aiida-task-files <pk> [filename]")
        sys.exit(1)
    pk = int(sys.argv[1])
    filename = sys.argv[2] if len(sys.argv) > 2 else None
    node = load_node(pk)
    info = {
        "pk": pk,
        "label": node.label or "",
        "process_type": node.process_type or "",
        "state": "",
        "exit_status": None,
        "workdir": None,
        "workdir_files": [],
        "retrieved_files": [],
    }
    try:
        info["state"] = node.process_state.value if getattr(node, "process_state", None) else ""
    except Exception:
        pass
    try:
        info["exit_status"] = node.exit_status
    except Exception:
        pass
    # 远端工作目录
    try:
        info["workdir"] = node.get_remote_workdir() or None
    except Exception:
        pass
    if info["workdir"] and os.path.isdir(info["workdir"]):
        try:
            info["workdir_files"] = sorted(os.listdir(info["workdir"]))[:200]
        except Exception:
            pass
    # retrieved（取回）文件
    try:
        ret = node.outputs.retrieved
        info["retrieved_files"] = sorted(ret.list_object_names())[:200]
    except Exception:
        pass

    if filename:
        content = None
        # 优先从 retrieved 读取
        try:
            ret = node.outputs.retrieved
            if filename in ret.list_object_names():
                content = ret.get_object_content(filename)
        except Exception:
            content = None
        # 其次从工作目录读取
        if content is None and info["workdir"]:
            fp = os.path.join(info["workdir"], filename)
            if os.path.isfile(fp):
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                except Exception as e:
                    content = "读取失败: %s" % e
        if content is None:
            info["error"] = "文件不存在: %s" % filename
        else:
            info["content"] = content[-MAX_CHARS:] if len(content) > MAX_CHARS else content
            info["truncated"] = len(content) > MAX_CHARS
    print(json.dumps(info, ensure_ascii=False))


if __name__ == "__main__":
    main()
