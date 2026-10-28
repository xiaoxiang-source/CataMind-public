#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AiiDA 进程查询探针 —— 供 CatMat 平台后端 subprocess 调用。

必须用 AiiDA 环境的 Python 运行：
    /home/user/anaconda3/envs/aiida/bin/python catmat.py aiida-probe

输出 JSON：
    {
      "active": [ {pk, label, process_type, state, status, ctime, mtime, parent, children}, ... ],
      "recent": [ {pk, label, process_type, state, status, ctime, mtime}, ... ]
    }
"""
import json
import sys

from aiida import load_profile
load_profile()  # 加载默认 profile

from aiida.orm import QueryBuilder, ProcessNode, load_node

ACTIVE_EXCLUDE = ["finished", "excepted", "killed"]


def short_label(t):
    if not t:
        return ""
    return t.rsplit(":", 1)[-1].split(".")[-1]


def fetch_rows(active_only, limit):
    qb = QueryBuilder()
    proj = ["id", "uuid", "label", "process_type", "attributes.process_state",
            "attributes.process_status", "ctime", "mtime"]
    if active_only:
        qb.append(ProcessNode, project=proj,
                  filters={"attributes.process_state": {"!in": ACTIVE_EXCLUDE}})
    else:
        qb.append(ProcessNode, project=proj)
    qb.order_by({ProcessNode: {"ctime": "desc"}})
    qb.limit(limit)
    return qb.all()


def fetch_states(pks):
    """批量查询指定 PK 的 process_state / process_status（一次调用取回，避免逐个起进程）。"""
    wanted = [int(p) for p in pks if str(p).strip().isdigit()]
    if not wanted:
        return {}
    qb = QueryBuilder()
    qb.append(ProcessNode, project=["id", "attributes.process_state",
                                    "attributes.process_status", "attributes.exit_status"],
              filters={"id": {"in": wanted}})
    out = {}
    for pk, state, status, exit_code in qb.all():
        out[str(pk)] = {"state": state, "status": status, "exit_status": exit_code}
    return out


def main():
    # 模式：--states pk1,pk2,... → 只批量返回状态（供任务同步用，轻量快速）
    if len(sys.argv) >= 3 and sys.argv[1] == "--states":
        print(json.dumps(fetch_states(sys.argv[2].split(",")),
                         ensure_ascii=False, default=str))
        return

    active_rows = fetch_rows(True, 200)
    recent_rows = fetch_rows(False, 30)
    active_pks = {r[0] for r in active_rows}

    active = []
    for pk, uuid, label, ptype, state, status, ctime, mtime in active_rows:
        parent = None
        children = []
        try:
            node = load_node(pk)
            if node.caller is not None:
                parent = node.caller.pk
            if hasattr(node, "called"):
                # 仅保留仍然活跃的子进程，避免返回大量已完成子进程
                children = [c.pk for c in node.called if c.pk in active_pks]
        except Exception:
            pass
        active.append({
            "pk": pk,
            "uuid": uuid,
            "label": label or short_label(ptype),
            "process_type": ptype,
            "state": state,
            "status": status,
            "ctime": str(ctime),
            "mtime": str(mtime),
            "parent": parent,
            "children": children,
        })

    recent = []
    for pk, uuid, label, ptype, state, status, ctime, mtime in recent_rows:
        recent.append({
            "pk": pk,
            "uuid": uuid,
            "label": label or short_label(ptype),
            "process_type": ptype,
            "state": state,
            "status": status,
            "ctime": str(ctime),
            "mtime": str(mtime),
        })

    print(json.dumps({"active": active, "recent": recent},
                     ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
