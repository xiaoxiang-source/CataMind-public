#!/usr/bin/env python3
"""扫描 CP2K tests/**/*.inp，建立「关键词 → 官方测试示例」反向索引，写入 cp2k_param_refs。

每个 (section_path, keyword) 最多保留 MAX_REFS 个示例文件；文件对应 GitHub 仓库
github.com/cp2k/cp2k 的 tests/ 目录，作为该参数的 GitHub 参考实现。

用法:
    python3 catmat.py build-param-refs [tests 目录] [db 路径]
"""
import os
import sys
import glob
import time
import sqlite3

TESTS_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.environ.get("CATMAT_CP2K_ROOT") or os.path.expanduser("~/bin/cp2k-2026.2"), "tests")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "catmat.db")
REPO_URL = "https://github.com/cp2k/cp2k"
MAX_REFS = 3  # 每个关键词最多保留的示例文件数


def parse_inp(text):
    """重建 .inp 的 section 嵌套，返回 {(section_path, KEYWORD)} 集合。"""
    stack = []
    out = set()
    for raw in text.splitlines():
        line = raw.split("#")[0].strip()
        if not line:
            continue
        if line.startswith("@"):
            continue  # 跳过 @SET/@IF/@INCLUDE 等预处理指令
        if line.startswith("&END"):
            if stack:
                stack.pop()
        elif line.startswith("&"):
            nm = line[1:].strip().split()
            if nm:
                stack.append(nm[0].upper())
        else:
            parts = line.split()
            if parts and parts[0][0].isalpha():
                out.add(("/".join(stack), parts[0].upper()))
    return out


def main():
    if not os.path.isdir(TESTS_DIR):
        print("未找到 tests 目录: %s" % TESTS_DIR)
        sys.exit(1)
    t0 = time.time()
    files = glob.glob(os.path.join(TESTS_DIR, "**", "*.inp"), recursive=True)
    print("扫描 %d 个 .inp ..." % len(files))

    # 聚合：key=(section_path, name) -> [file_path...]
    refs = {}
    total_kw = 0
    for f in files:
        rel = os.path.relpath(f, TESTS_DIR)
        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                kws = parse_inp(fh.read())
        except Exception:
            continue
        total_kw += len(kws)
        for path, name in kws:
            key = (path, name)
            lst = refs.setdefault(key, [])
            if len(lst) < MAX_REFS:
                lst.append("tests/" + rel)

    print("解析出 %d 个 (section_path, keyword) 组合" % len(refs))

    conn = sqlite3.connect(DB_PATH)
    # 载入 cp2k_keywords 的 (section_path, name) 集合用于精确匹配
    valid = set()
    for r in conn.execute("SELECT section_path, name FROM cp2k_keywords"):
        valid.add((r[0], r[1].upper()))

    matched = 0
    rows = []
    for (path, name), fl in refs.items():
        if (path, name) in valid:
            matched += 1
            for fp in fl:
                rows.append((path, name, REPO_URL, fp, "example", "CP2K 官方回归测试示例", 0))
    print("精确匹配 cp2k_keywords：%d / %d (%.1f%%)" % (matched, len(refs), 100.0 * matched / max(1, len(refs))))

    conn.execute("DELETE FROM cp2k_param_refs")
    conn.executemany(
        "INSERT INTO cp2k_param_refs(section_path,keyword,repo_url,file_path,ref_type,note,verified) "
        "VALUES(?,?,?,?,?,?,?)", rows)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM cp2k_param_refs").fetchone()[0]
    conn.close()
    print("写入 cp2k_param_refs：%d 条（覆盖 %d 个关键词），耗时 %.1fs" % (n, matched, time.time() - t0))


if __name__ == "__main__":
    main()
