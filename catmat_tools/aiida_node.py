#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查询单个 AiiDA 节点详情（按 PK），输出 JSON。用 envs/aiida/bin/python 运行。

用法: python3 catmat.py aiida-node <pk>
"""
import sys
import json

from aiida import load_profile
load_profile()

from aiida.orm import load_node


def get_attr(node, key, default=None):
    """兼容 AiiDA 2.x 新旧属性 API。"""
    try:
        return node.base.attributes.get(key, default)
    except Exception:
        try:
            return node.attributes.get(key, default)
        except Exception:
            return default


def main():
    pk = int(sys.argv[1])
    node = load_node(pk)
    state = get_attr(node, "process_state", "unknown")
    status = get_attr(node, "process_status", "")
    exit_status = get_attr(node, "exit_status")
    parent = node.caller.pk if node.caller is not None else None
    children = [c.pk for c in node.called] if hasattr(node, "called") else []
    info = {
        "pk": pk,
        "uuid": str(node.uuid),
        "label": node.label or "",
        "process_type": node.process_type,
        "state": state,
        "status": status,
        "exit_status": exit_status,
        "ctime": str(node.ctime),
        "mtime": str(node.mtime),
        "parent": parent,
        "children": children,
    }
    print(json.dumps(info, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
