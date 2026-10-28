#!/usr/bin/env python3
"""从 CP2K `cp2k_input.xml` 解析全量输入规范，写入 SQLite。

用法:
    python3 catmat.py build-param-db [cp2k_input.xml 路径] [db 路径]

cp2k_input.xml 生成方式（CP2K 2026.2）:
    source /home/user/bin/cp2k-2026.2/install/cp2k_env
    cp2k.psmp --xml      # 在当前目录生成 cp2k_input.xml
"""
import os
import sys
import json
import time
import sqlite3
import xml.etree.ElementTree as ET

XML_PATH = sys.argv[1] if len(sys.argv) > 1 else "/tmp/cp2k_input.xml"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "catmat.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cp2k_sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    parent TEXT,
    full_path TEXT NOT NULL,
    level INTEGER DEFAULT 0,
    description TEXT,
    aliases TEXT DEFAULT '[]',
    is_repeatable INTEGER DEFAULT 0,
    sort_order INTEGER DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_section_path ON cp2k_sections(full_path);
CREATE TABLE IF NOT EXISTS cp2k_keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    section_path TEXT NOT NULL,
    name TEXT NOT NULL,
    datatype TEXT,
    default_value TEXT,
    description TEXT,
    usage TEXT,
    aliases TEXT DEFAULT '[]',
    enum_values TEXT DEFAULT '[]',
    is_lone INTEGER DEFAULT 0,
    sort_order INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kw_section ON cp2k_keywords(section_path);
CREATE TABLE IF NOT EXISTS cp2k_param_refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    section_path TEXT,
    keyword TEXT,
    repo_url TEXT NOT NULL,
    file_path TEXT,
    ref_type TEXT DEFAULT 'example',
    note TEXT,
    verified INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_refs_kw ON cp2k_param_refs(section_path, keyword);
"""


def extract_keyword(elem):
    """提取一个 KEYWORD 元素的所有字段。"""
    default_name = ""
    aliases = []
    for n in elem.findall("NAME"):
        txt = (n.text or "").strip()
        if not txt:
            continue
        if n.get("type") == "alias":
            if txt not in aliases:
                aliases.append(txt)
        elif not default_name:
            default_name = txt
    dt = elem.find("DATA_TYPE")
    datatype = dt.get("kind") if dt is not None else ""
    enum_values = []
    if dt is not None:
        enum = dt.find("ENUMERATION")
        if enum is not None:
            for item in enum.findall("ITEM"):
                iname = (item.findtext("NAME") or "").strip()
                if iname:
                    enum_values.append(iname)
    dv = elem.findtext("DEFAULT_VALUE")
    return {
        "name": default_name,
        "datatype": datatype,
        "default_value": dv.strip() if dv else None,
        "description": (elem.findtext("DESCRIPTION") or "").strip(),
        "usage": (elem.findtext("USAGE") or "").strip(),
        "aliases": aliases,
        "enum_values": enum_values,
        "is_lone": 1 if elem.find("LONE_KEYWORD_VALUE") is not None else 0,
    }


def parse_tree():
    """递归解析，返回 (sections, keywords)。父子关系天然正确。"""
    tree = ET.parse(XML_PATH)
    root = tree.getroot()
    sections, keywords = [], []
    counter = [0]

    def handle_keyword(elem, section_path):
        if elem.get("removed") == "yes":
            return
        kw = extract_keyword(elem)
        if not kw["name"]:
            return
        counter[0] += 1
        kw.update({
            "section_path": section_path,
            "aliases": json.dumps(kw["aliases"], ensure_ascii=False),
            "enum_values": json.dumps(kw["enum_values"], ensure_ascii=False),
            "sort_order": counter[0],
        })
        keywords.append(kw)

    def walk(elem, parent_path, level):
        name = (elem.findtext("NAME") or "").strip()
        full_path = (parent_path + "/" + name) if parent_path else name
        counter[0] += 1
        sections.append({
            "name": name,
            "parent": parent_path or None,
            "full_path": full_path,
            "level": level,
            "description": (elem.findtext("DESCRIPTION") or "").strip(),
            "aliases": "[]",
            "is_repeatable": 1 if elem.get("repeats") == "yes" else 0,
            "sort_order": counter[0],
        })
        for child in elem:
            if child.tag == "SECTION":
                walk(child, full_path, level + 1)
            elif child.tag == "KEYWORD":
                handle_keyword(child, full_path)
            elif child.tag == "SECTION_PARAMETERS":
                for kw in child.findall("KEYWORD"):
                    handle_keyword(kw, full_path)

    for child in root:
        if child.tag == "SECTION":
            walk(child, None, 1)
        elif child.tag in ("KEYWORD", "SECTION_PARAMETERS"):
            if child.tag == "KEYWORD":
                handle_keyword(child, "")
            else:
                for kw in child.findall("KEYWORD"):
                    handle_keyword(kw, "")
    return sections, keywords


def main():
    if not os.path.isfile(XML_PATH):
        print("未找到 XML: %s" % XML_PATH)
        sys.exit(1)
    t0 = time.time()
    print("解析 %s ..." % XML_PATH)
    sections, keywords = parse_tree()
    print("SECTION %d 个，KEYWORD %d 个，%.1fs" % (len(sections), len(keywords), time.time() - t0))

    top = [s["name"] for s in sections if s["parent"] is None]
    print("顶层 SECTION（%d）：%s" % (len(top), ", ".join(top)))

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.execute("DELETE FROM cp2k_sections")
    conn.execute("DELETE FROM cp2k_keywords")
    conn.executemany(
        "INSERT INTO cp2k_sections(name,parent,full_path,level,description,aliases,is_repeatable,sort_order) "
        "VALUES(:name,:parent,:full_path,:level,:description,:aliases,:is_repeatable,:sort_order)", sections)
    conn.executemany(
        "INSERT INTO cp2k_keywords(section_path,name,datatype,default_value,description,usage,aliases,enum_values,is_lone,sort_order) "
        "VALUES(:section_path,:name,:datatype,:default_value,:description,:usage,:aliases,:enum_values,:is_lone,:sort_order)", keywords)
    conn.commit()
    n_s = conn.execute("SELECT COUNT(*) FROM cp2k_sections").fetchone()[0]
    n_k = conn.execute("SELECT COUNT(*) FROM cp2k_keywords").fetchone()[0]
    conn.close()
    print("写入完成：sections=%d, keywords=%d，总耗时 %.1fs" % (n_s, n_k, time.time() - t0))


if __name__ == "__main__":
    main()
