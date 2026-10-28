#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
催化智构 CataMind · 催化材料智能计算平台 —— 后端服务（零第三方依赖）

- 存储：SQLite（catmat.db）
- 协议：标准库 http.server（ThreadingHTTPServer）
- 静态：同源托管 index.html
- 能力：体系 / 文献库 / CP2K 知识库 / 任务 / ML 数据集 的完整读写与持久化、
        文件导入、AiiDA 任务 runner 抽象、异常处理、CORS。

运行：
    python3 catmat.py serve [--port 8000] [--host 127.0.0.1]
"""
import os
import sys
import json
import sqlite3
import threading
import time
import csv
import io
import random
import argparse
import re
import shutil
import subprocess
import contextlib
import functools
from catmat_process_monitor import ProcessSampler
import hashlib
import hmac
import base64
import secrets
from catmat_tools import pdos_review
import catmat_postprocessing as postprocessing
from catmat_tools.external_runner import run_checked, run_json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import logging
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 高通量筛选 AiiDA 工作流「输入文件构建」唯一基线（以 irru_adsorption_workchain.py 为准）
# 功能脚本统一收口至 catmat_tools 包，由根入口 catmat.py 以子命令分发。
from catmat_tools import cp2k_workchain_input as _wc_input
import catmat_resource_governor as _resource_governor

DATA_DIR = os.path.abspath(os.path.expanduser(os.environ.get("CATMAT_DATA_DIR") or os.path.join(BASE_DIR, "data")))
# 体系结构文件白名单：研究体系模块「结构文件」栏的上传/预览类型
# （实际解析由 parse_structure_text 走 ASE，失败时自动尝试多种格式）
STRUCT_FILE_EXTS = {"cif", "xyz", "gjf", "gaussian", "com", "g16", "pdb", "mol", "mol2",
                    "vasp", "poscar", "contcar", "gen", "in", "sdf", "sd", "json"}
_DEFAULT_DB_PATH = os.path.join(BASE_DIR, "catmat.db")
DB_PATH = os.path.abspath(os.path.expanduser(
    (os.environ.get("CATMAT_DB_PATH") or _DEFAULT_DB_PATH).strip()))
BACKUP_DIR = os.path.abspath(os.path.expanduser(
    (os.environ.get("CATMAT_BACKUP_DIR") or os.path.join(BASE_DIR, "backups")).strip()))
DOCS_DIR = os.path.join(BASE_DIR, "docs")

# 账号角色镜像：SQLite 仍是事务权威源；两个 JSON 文件按角色拆分，并在读取时与数据库核验。
# 测试若覆盖 DB_PATH，镜像跟随测试数据库放置，避免污染生产账号文件。
def _account_data_dir():
    override = (os.environ.get("CATMAT_ACCOUNT_DATA_DIR") or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if os.path.abspath(DB_PATH) == os.path.abspath(_DEFAULT_DB_PATH):
        return os.path.join(DATA_DIR, "accounts")
    return os.path.abspath(DB_PATH) + ".accounts"


def _account_role_path(role):
    if role not in ("admin", "user"):
        raise ValueError("未知账号角色: %s" % role)
    return os.path.join(_account_data_dir(), "admins.json" if role == "admin" else "users.json")

# 内置手册白名单（界面「平台设置」可查看与下载）；仅允许映射表内的文件，防路径穿越
MANUAL_DOCS = {
    "user-manual": "user-manual.md",            # 用户手册（面向业务用户）
    "admin-manual": "admin-manual.md",          # 管理员手册（面向运维/开发）
    "auth-design": "auth-security-design.md",   # 登录安全校验体系设计（任务一）
    "cloud-deploy": "tencent-cloud-deploy.md",  # 腾讯云部署实施方案（任务二）
    "remote-access": "公网中转访问方案_20260904.md",  # 腾讯云公网中转（本地不出网）方案
}


def _env_int(key, default):
    """读取整数型环境变量，容忍行内注释与空白。

    systemd 的 EnvironmentFile 只把「行首」的 # 当注释，行内的 # 会原样进入变量值，
    于是 `KEY=300  # 挑战有效期` 会让 int() 抛 ValueError 并在启动阶段直接崩掉。
    这里统一剥掉行内注释、去空白，解析失败时回退默认值而不是让整个服务起不来。
    """
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        return default
    # 只切第一个 # 或 ; 之前的内容，且要求其前面有空白（避免误伤合法值）
    for marker in ("#", ";"):
        pos = raw.find(marker)
        if pos > 0 and raw[pos - 1] in " \t":
            raw = raw[:pos]
            break
    raw = raw.strip().strip('"').strip("'")
    try:
        return int(raw)
    except (TypeError, ValueError):
        print(f"[WARN] 环境变量 {key} 不是合法整数（原始值={os.environ.get(key)!r}），回退默认值 {default}",
              file=sys.stderr)
        return default


# 平台鉴权总开关。本地账号密码为主通道；微信确认器代码兼容保留、默认关闭。
AUTH_ENABLED = os.environ.get("CATMAT_AUTH_ENABLED") == "1"
LOCAL_AUTH_ENABLED = os.environ.get("CATMAT_LOCAL_AUTH_ENABLED", "1") == "1"
WECHAT_CHALLENGE_ENABLED = os.environ.get("CATMAT_WECHAT_CHALLENGE_ENABLED") == "1"
WECHAT_TEST_MODE = os.environ.get("CATMAT_WECHAT_TEST_MODE") == "1"
WECHAT_APP_ID = (os.environ.get("CATMAT_WECHAT_APP_ID") or "").strip()
WECHAT_APP_SECRET = (os.environ.get("CATMAT_WECHAT_APP_SECRET") or "").strip()
JWT_SECRET = (os.environ.get("CATMAT_JWT_SECRET") or "").strip()
ACCESS_TOKEN_TTL = _env_int("CATMAT_ACCESS_TOKEN_TTL", 1800)
REFRESH_TOKEN_TTL = _env_int("CATMAT_REFRESH_TOKEN_TTL", 604800)
WECHAT_ADMIN_OPENIDS = {
    value.strip() for value in (os.environ.get("CATMAT_WECHAT_ADMIN_OPENIDS") or "").split(",")
    if value.strip()
}
# 网页端「管理员令牌」通道（网页科研访问用）：CATMAT_ADMIN_TOKEN 为空则该通道不可用。
# 校验通过后在 auth_users 建立/复用保留账号 __admin_token__（role=admin），
# 与微信会话共用 auth_sessions / 门禁 / refresh / logout 机制。
ADMIN_TOKEN = os.environ.get("CATMAT_ADMIN_TOKEN") or ""
ADMIN_HOLDER_OPENID = "__admin_token__"

# ---- 微信确认器（RFC 8628 风格的一次性登录挑战）----
# 浏览器发起挑战 → 微信小程序仅做批准/拒绝 → 浏览器凭 challenge_secret 兑换业务会话。
# 小程序签发的令牌 scope=approve，只能调用 /api/auth/approvals/*，不能访问任何业务 API。
CHALLENGE_TTL = _env_int("CATMAT_LOGIN_CHALLENGE_TTL", 300)      # 挑战有效期（秒）
CHALLENGE_POLL_INTERVAL = _env_int("CATMAT_LOGIN_POLL_INTERVAL", 3)  # 建议轮询间隔（秒）
CHALLENGE_USER_CODE_LEN = _env_int("CATMAT_LOGIN_CODE_LEN", 6)   # 人工输入短码长度
# 易读字符集：剔除 0/O/1/I/L 等易混字符，降低人工抄写错误率
CHALLENGE_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
# 同一来源 IP 每分钟最多创建的挑战数（防短码空间探测与滥用）
CHALLENGE_CREATE_LIMIT = _env_int("CATMAT_LOGIN_CREATE_LIMIT", 10)
# 同一审批者每分钟最多查询短码次数（防短码爆破）
APPROVAL_LOOKUP_LIMIT = _env_int("CATMAT_LOGIN_LOOKUP_LIMIT", 20)

# 令牌权限范围：platform=平台业务；approve=仅登录确认
SCOPE_PLATFORM = "platform"
SCOPE_APPROVE = "approve"
LOCAL_LOGIN_LIMIT = _env_int("CATMAT_LOCAL_LOGIN_LIMIT", 10)

# ==================== 三角色授权体系（东翁 · 掌书 · 清客）====================
# 命名规则与释义见 docs/role-naming-rules.md。
# 对外雅称：owner→东翁，steward→掌书，guest→清客；对内 role 值保持小写英文。
# 兼容旧值：'admin' 视同东翁（owner），'user' 视同清客（guest）。
ROLE_OWNER = "owner"
ROLE_STEWARD = "steward"
ROLE_GUEST = "guest"
ROLE_LEGACY = {"admin": ROLE_OWNER, "user": ROLE_GUEST}
ROLE_DISPLAY = {ROLE_OWNER: "东翁", ROLE_STEWARD: "掌书", ROLE_GUEST: "清客",
                "admin": "东翁", "user": "清客"}


def _norm_role(role):
    """旧值归一化：admin→owner、user→guest；未知值一律按清客处理（安全默认）。"""
    if role in (ROLE_OWNER, ROLE_STEWARD, ROLE_GUEST):
        return role
    return ROLE_LEGACY.get(role, ROLE_GUEST)


def _role_label(role):
    return ROLE_DISPLAY.get(role) or ROLE_DISPLAY.get(_norm_role(role), "清客")


def _is_owner_role(role):
    return _norm_role(role) == ROLE_OWNER


# ---- 掌书能力包（东翁逐项授予；系统配置/册封褫夺/传印为东翁专属，见文档 §5）----
CAP_MAINTAIN_DATA = "maintain_data"     # 新增/维护平台数据（体系/知识库/文献）
CAP_DELETE_DATA = "delete_data"         # 删除平台数据与任务
CAP_MANAGE_ACCOUNTS = "manage_accounts" # 管理账号（不含东翁）
CAP_RUN_TASKS = "run_tasks"             # 提交/运行计算任务与工作流
CAP_CONTROL_TASKS = "control_tasks"     # 暂停/终止/收集任务
CAP_RUN_PIPELINES = "run_pipelines"     # ML/AL/HTS/后处理等批量流水线
CAP_GLOBAL_SYNC = "global_sync"         # 全局导入与知识同步
CAP_VIEW_AUDIT = "view_audit"           # 查看审计记录
ALL_CAPABILITIES = (CAP_MAINTAIN_DATA, CAP_DELETE_DATA, CAP_MANAGE_ACCOUNTS,
                    CAP_RUN_TASKS, CAP_CONTROL_TASKS, CAP_RUN_PIPELINES,
                    CAP_GLOBAL_SYNC, CAP_VIEW_AUDIT)
CAP_LABELS = {
    CAP_MAINTAIN_DATA: "数据维护（增改体系/知识库/文献）",
    CAP_DELETE_DATA: "删除数据与任务",
    CAP_MANAGE_ACCOUNTS: "账号管理（不含东翁）",
    CAP_RUN_TASKS: "提交与运行计算任务/工作流",
    CAP_CONTROL_TASKS: "暂停/终止/收集任务",
    CAP_RUN_PIPELINES: "批量流水线（ML/AL/HTS/后处理）",
    CAP_GLOBAL_SYNC: "全局导入与知识同步",
    CAP_VIEW_AUDIT: "查看审计记录",
}

# 清客（及未获授权的掌书）也可执行的“全员写白名单”：
#   文献库扩充（DOI 导入/核验/联网检索）、解读归档（归档自动带用户名标签）；
#   提交平台内置 CM- 队列任务（含独立 CP2K 任务，见队列模块）。
def _guest_write_allowed(method, seg):
    if method != "POST":
        return False
    root = seg[0] if seg else ""
    if root == "literature" and len(seg) >= 2 and seg[1] in ("doi", "verify", "web-search"):
        return True
    if root == "literature" and len(seg) == 3 and seg[2] == "interpret":
        return True
    if root in ("queue", "cp2k-submit"):
        return True
    if root == "llm" and len(seg) >= 2 and seg[1] == "chat":
        return True   # 智能助手问答（不写库）
    if root == "workflow" and len(seg) >= 2 and seg[1] == "preview":
        return True   # 输入文件预览（只读生成）
    return False


def _steward_cap_allowed(capabilities, method, seg):
    """掌书：写操作按能力包判定。GET/浏览在 _business_allowed 已放行。"""
    caps = set(capabilities or ())
    route = "/".join(seg)
    root = seg[0] if seg else ""
    if method == "DELETE":
        if root in ("systems", "literature", "knowledge", "tasks", "queue",
                    "ml", "al", "hts", "workflow"):
            return "delete_data" in caps, "删除数据需东翁授予「删除数据」能力"
        return False, "该删除操作未获东翁授权"
    if root in ("ml", "al", "hts", "postprocess"):
        return "run_pipelines" in caps, "批量流水线需东翁授予「批量流水线」能力"
    if root in ("sync", "import"):
        return "global_sync" in caps, "全局导入与同步需东翁授予「全局导入」能力"
    if root == "auth" and (route == "users" or route.startswith("users/")):
        return "manage_accounts" in caps, "账号管理需东翁授予「账号管理」能力"
    if root == "workflow":
        return "run_tasks" in caps, "提交工作流需东翁授予「任务运行」能力"
    if root == "agent" and len(seg) >= 2 and seg[1] == "ml":
        return (("run_tasks" in caps) or ("run_pipelines" in caps)), \
               "休go 训练顾问/结果解读需东翁授予「任务运行」或「批量流水线」能力"
    if root == "cp2k-submit":
        return True, None  # 独立 CP2K 提交已并入 CM- 队列，资源可控
    if root in ("tasks", "queue"):
        return (("run_tasks" in caps) or ("control_tasks" in caps)
                or ("delete_data" in caps)), "任务操作需东翁授予「任务运行/终止」能力"
    if root in ("systems", "literature", "knowledge"):
        return "maintain_data" in caps, "数据维护需东翁授予「数据维护」能力"
    if (method, route) == ("POST", "llm/config") or (method, route) == ("POST", "agent/memory/sync"):
        return False, "系统配置与全局记忆同步仅东翁可执行"
    return False, "该写操作未获东翁授权"


def _business_allowed(role, method, seg, capabilities=None):
    """集中授权矩阵。返回 (允许, 原因)。

    role ∈ owner|steward|guest（兼容旧值 admin|user）。
    capabilities：掌书的能力包（JSON 数组）。
    清客：可浏览（GET）；可执行全员写白名单（文献扩充/解读归档/提交 CM- 队列任务）；
          其余写操作一律拒绝——清客只能删改/下载自己队列任务产生的文件，该归属
          校验在各 handler（tasks/queue）内按 owner_id 落实。
    """
    norm = _norm_role(role)
    if norm == ROLE_OWNER:
        return True, None
    if seg and seg[0] == "codex":
        return False, "服务器执行助手与共享文件仅东翁可访问"
    if method == "GET":
        # 浏览放行；需要“仅本人可见”的列表（tasks/queue）在 handler 内按 owner_id 过滤。
        return True, None
    if _guest_write_allowed(method, seg):
        return True, None
    if norm == ROLE_GUEST:
        root = seg[0] if seg else ""
        # 清客可以提交/删改/撤销/下载自己提交的任务（归属校验在各 handler 内按 owner_id 落实）
        if root == "tasks" and (method in ("DELETE", "PATCH") or method == "POST"):
            return True, None
        if root == "queue" and (method == "DELETE" or
                                (method == "POST" and len(seg) >= 2)):
            return True, None
        return False, "清客只可浏览，不能改动平台数据；如需提交、删除或维护，请联系东翁"
    return _steward_cap_allowed(capabilities, method, seg)

# ==================== 服务端日志（整改 [高2] P0-2：事故可复盘）====================
# 落盘 logs/server.log，单文件 5 MB × 保留 3 份；同时保留一份 stderr 兜底输出。
LOG_DIR = os.path.abspath(os.path.expanduser(
    (os.environ.get("CATMAT_LOG_DIR") or os.path.join(BASE_DIR, "logs")).strip()))
LOG_FILE = os.path.join(LOG_DIR, "server.log")

LOG = logging.getLogger("catmat")
LOG.setLevel(logging.INFO)
LOG.propagate = False
if not LOG.handlers:
    _sh = logging.StreamHandler(sys.stderr)
    _sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                        "%Y-%m-%d %H:%M:%S"))
    LOG.addHandler(_sh)

_LOG_FILE_READY = False


def setup_logging(level=logging.INFO):
    """把日志同时写入 logs/server.log（RotatingFileHandler）。可重复调用，幂等。"""
    global _LOG_FILE_READY
    if _LOG_FILE_READY:
        return True
    try:
        os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)
        try:
            os.chmod(LOG_DIR, 0o700)
        except OSError:
            pass
        fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                          "%Y-%m-%d %H:%M:%S"))
        LOG.addHandler(fh)
        _harden_private_file(LOG_FILE)
        LOG.setLevel(level)
        _LOG_FILE_READY = True
        LOG.info("=== 服务启动，日志落盘就绪: %s ===", LOG_FILE)
        return True
    except Exception:
        LOG.exception("日志落盘初始化失败，仅 stderr 输出")
        return False


def log_exc(context, exc=None):
    """统一记录异常：traceback + 上下文（请求方法/路径或阶段名）+ 时间戳。
    必须在 except 块内调用（依赖 sys.exc_info() 取 traceback）。"""
    try:
        if exc is None:
            LOG.exception("[EXC] %s", context)
        else:
            LOG.exception("[EXC] %s -> %s: %s", context, type(exc).__name__, exc)
    except Exception:
        pass  # 绝不让日志本身再抛异常影响业务
INDEX_PATH = os.path.join(BASE_DIR, "index.html")

VERSION = "2.4.1"
_lock = threading.Lock()
_tls_lock = threading.local()


def _lock_acquire():
    _lock.acquire()
    _tls_lock.held = True


def _lock_release():
    _tls_lock.held = False
    _lock.release()


@contextlib.contextmanager
def _blocking_unlocked():
    """执行阻塞式子进程期间临时释放全局锁。

    仅在本线程确实持有锁时释放；退出时无论正常返回还是抛异常都重新获取。
    未持锁（包括已由外层上下文临时释放）时为空操作。
    """
    held = bool(getattr(_tls_lock, "held", False))
    if not held:
        yield
        return
    _lock_release()
    try:
        yield
    finally:
        _lock_acquire()
_AUDIT_WRITE_FAILS = 0
_AUDIT_WRITE_FAILS_LOCK = threading.Lock()

# --------------------------------------------------------------------------
# 数据库
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS systems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT,
    name TEXT NOT NULL,
    formula TEXT,
    composition TEXT,
    space_group TEXT,
    lattice TEXT,
    layers INTEGER,
    vacuum REAL,
    elements TEXT,
    source TEXT DEFAULT '平台',
    descriptors TEXT DEFAULT '[]',
    structure TEXT DEFAULT '[]',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS literature (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    authors TEXT,
    journal TEXT,
    year INTEGER,
    doi TEXT,
    material_system TEXT,
    electrolyzer TEXT,
    overpotential_10 REAL,
    tafel_slope REAL,
    stability_hours REAL,
    mass_activity REAL,
    dft_code TEXT,
    uses_cp2k INTEGER DEFAULT 0,
    cp2k_settings TEXT,
    abstract TEXT,
    conclusion TEXT,
    tags TEXT DEFAULT '[]',
    verified INTEGER DEFAULT 0,
    interpreted_by TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT,
    topic TEXT NOT NULL,
    keyword TEXT,
    recommended TEXT,
    rationale TEXT,
    source TEXT,
    origin TEXT,
    archived_by TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    system_id INTEGER,
    calc_type TEXT DEFAULT 'GEO_OPT',
    input_file TEXT,
    resources TEXT DEFAULT '{}',
    status TEXT DEFAULT 'queued',
    progress REAL DEFAULT 0,
    log TEXT DEFAULT '[]',
    aiiada_pk TEXT,
    result TEXT,
    error TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    finished_at TEXT,
    owner_id INTEGER,
    cm_code TEXT
);
-- 平台内置 FIFO 计算队列（无外部调度器时的任务排队）：CM- 编号任务。
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cm_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    owner_id INTEGER,
    kind TEXT DEFAULT 'cp2k',
    nproc INTEGER DEFAULT 1,
    payload TEXT DEFAULT '{}',
    status TEXT DEFAULT 'queued',      -- queued|running|done|failed|canceled
    pid INTEGER,
    out_dir TEXT,
    out_log TEXT,
    error TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner_id);
CREATE TABLE IF NOT EXISTS ml_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    source TEXT,
    features TEXT DEFAULT '{}',
    target REAL,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    system_id INTEGER,
    calc_type TEXT DEFAULT 'GEO_OPT',
    params TEXT DEFAULT '{}',
    start_node TEXT DEFAULT 'structure',
    status TEXT DEFAULT 'pending',
    current_node TEXT,
    log TEXT DEFAULT '[]',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    finished_at TEXT,
    owner_id INTEGER
);
CREATE TABLE IF NOT EXISTS hts_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    status TEXT DEFAULT 'running',
    steps TEXT DEFAULT '[]',
    output_dir TEXT,
    params TEXT DEFAULT '{}',
    log TEXT DEFAULT '[]',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    finished_at TEXT,
    owner_id INTEGER
);
CREATE TABLE IF NOT EXISTS hts_batch_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    seq INTEGER,
    label TEXT,
    system_id INTEGER,
    aiida_struct_pk INTEGER,
    aiida_pk INTEGER,
    calc_type TEXT,
    status TEXT DEFAULT 'pending',
    energy REAL,
    error TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS workflow_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL,
    seq INTEGER,
    node_key TEXT,
    node_label TEXT,
    status TEXT DEFAULT 'pending',
    input_artifacts TEXT DEFAULT '{}',
    output TEXT,
    log TEXT DEFAULT '[]',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    finished_at TEXT
);
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
-- 机器学习 / 主动学习任务（异步后台：进度 / 日志 / 终止 / 删除）
CREATE TABLE IF NOT EXISTS ml_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT DEFAULT 'ml',
    name TEXT,
    model TEXT,
    device TEXT,
    status TEXT DEFAULT 'running',
    pid INTEGER,
    progress INTEGER DEFAULT 0,
    log TEXT DEFAULT '[]',
    params TEXT DEFAULT '{}',
    result TEXT,
    error TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    finished_at TEXT,
    owner_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_mlt_status ON ml_tasks(status);

-- 势函数延拓（DPGEN 主动学习闭环）主表：记录一次完整的迭代延拓任务
CREATE TABLE IF NOT EXISTS dpgen_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    status TEXT DEFAULT 'running',        -- running|done|failed|cancelled|paused
    current_iter INTEGER DEFAULT 0,
    max_iter INTEGER DEFAULT 10,
    trust_lo REAL DEFAULT 0.05,
    trust_hi REAL DEFAULT 0.15,
    stride INTEGER DEFAULT 1,
    params TEXT DEFAULT '{}',             -- 训练/探索/标注/收敛等全部配置
    seed_info TEXT DEFAULT '{}',          -- 初始数据集/结构集信息
    owner_id INTEGER,
    progress INTEGER DEFAULT 0,
    log TEXT DEFAULT '[]',
    result TEXT,
    error TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_dpgen_status ON dpgen_runs(status);

-- 势函数延拓逐轮迭代记录：训练/探索/筛选/标注/评估各子阶段与指标
CREATE TABLE IF NOT EXISTS dpgen_iterations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    iter_no INTEGER NOT NULL,
    phase TEXT,                          -- train|explore|label|done
    status TEXT DEFAULT 'running',        -- running|done|failed
    progress INTEGER DEFAULT 0,
    metrics TEXT DEFAULT '{}',           -- RMSE_E/F、偏差分布等
    n_candidates INTEGER DEFAULT 0,
    cp2k_queue_ids TEXT DEFAULT '[]',    -- 本轮提交的 CP2K 标注任务 id 列表
    log TEXT DEFAULT '[]',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_dpgen_iter_run ON dpgen_iterations(run_id);

-- 审计日志（整改 [高5] P1-6）：关键动作全链路留痕，支撑"数据可重复性 / 完全溯源"
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT (datetime('now','localtime')),
    actor TEXT DEFAULT 'local',
    action TEXT,
    target TEXT,
    params TEXT,
    result TEXT,
    ok INTEGER
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit(action);

-- AI 助手「休go」的持久化记忆：与平台知识库双向同步
-- ref_id 指向 knowledge.id（同步来源）；source=chat 的条目为对话沉淀，可回写知识库
CREATE TABLE IF NOT EXISTS agent_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT,
    topic TEXT,
    keyword TEXT,
    content TEXT,
    source TEXT DEFAULT 'knowledge_sync',
    ref_id INTEGER,
    origin TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT,
    synced_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_am_cat ON agent_memory(category);
CREATE INDEX IF NOT EXISTS idx_am_origin ON agent_memory(origin);
CREATE UNIQUE INDEX IF NOT EXISTS idx_am_ref ON agent_memory(ref_id) WHERE ref_id IS NOT NULL;

-- 微信小程序用户：只保存微信稳定标识与平台身份，不采集头像、昵称、手机号等资料
CREATE TABLE IF NOT EXISTS auth_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    openid TEXT NOT NULL UNIQUE,
    unionid TEXT,
    username TEXT,
    password_hash TEXT,
    password_updated_at TEXT,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    last_login_at TEXT,
    capabilities TEXT DEFAULT '[]'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_users_openid ON auth_users(openid);
CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_users_username ON auth_users(username) WHERE username IS NOT NULL;
-- username 唯一索引在 init_db 迁移补列后创建，兼容旧数据库。

-- refresh token 仅存 SHA-256 哈希；每次刷新轮换并吊销旧令牌，防止重放
CREATE TABLE IF NOT EXISTS auth_sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    refresh_token_hash TEXT NOT NULL UNIQUE,
    expires_at INTEGER NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    last_used_at TEXT,
    FOREIGN KEY(user_id) REFERENCES auth_users(id)
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_exp ON auth_sessions(expires_at);

-- 微信确认器：一次性登录挑战（浏览器发起 → 小程序批准 → 浏览器兑换业务会话）。
-- 双码设计：user_code 是给人工看/输入的易读短码；challenge_secret 是浏览器独占的高熵秘密，
-- 只以 SHA-256 落库，用于轮询与兑换，保证「谁发起谁兑换」。
CREATE TABLE IF NOT EXISTS auth_login_challenges (
    id TEXT PRIMARY KEY,
    user_code TEXT NOT NULL UNIQUE,
    secret_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',      -- pending|approved|denied|consumed|expired
    scope TEXT NOT NULL DEFAULT 'platform',      -- 兑换后会话的权限范围
    client_hint TEXT,                            -- 浏览器自述（UA/浏览器+系统摘要），供审批人比对
    client_ip TEXT,                              -- 来源 IP（优先取 X-Forwarded-For 首跳）
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    approved_at INTEGER,
    consumed_at INTEGER,
    approver_user_id INTEGER,                    -- 批准该挑战的微信账号 id
    exchange_session_id TEXT,                    -- 兑换出的会话 id（审计追溯）
    FOREIGN KEY(approver_user_id) REFERENCES auth_users(id)
);
CREATE INDEX IF NOT EXISTS idx_challenges_status ON auth_login_challenges(status);
CREATE INDEX IF NOT EXISTS idx_challenges_exp ON auth_login_challenges(expires_at);
""" + pdos_review.SCHEMA + postprocessing.SCHEMA


def _harden_private_file(path):
    """将包含账号、密钥或业务数据的本地文件限制为仅服务账号可读写。"""
    try:
        if os.path.exists(path):
            os.chmod(path, 0o600)
    except OSError:
        LOG.warning("无法收紧敏感文件权限: %s", path)


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    for path in (DB_PATH, DB_PATH + "-wal", DB_PATH + "-shm"):
        _harden_private_file(path)
    return conn


def _b64url_encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text):
    return base64.urlsafe_b64decode((text + "=" * (-len(text) % 4)).encode("ascii"))


def _auth_secret():
    """返回签名密钥。鉴权启用时不允许缺省；测试模式使用隔离的固定测试密钥。"""
    if JWT_SECRET:
        return JWT_SECRET.encode("utf-8")
    if WECHAT_TEST_MODE:
        return b"catmat-wechat-local-test-secret-not-for-production"
    return None


def auth_config_error():
    """全局配置就绪检查。本地账号由 SQLite 持久化，不依赖外部身份服务。"""
    if not AUTH_ENABLED:
        return None
    if not _auth_secret():
        return "缺少 CATMAT_JWT_SECRET"
    if not LOCAL_AUTH_ENABLED and not ADMIN_TOKEN and not WECHAT_CHALLENGE_ENABLED:
        return "没有启用任何登录通道"
    if WECHAT_CHALLENGE_ENABLED and not (WECHAT_TEST_MODE or (WECHAT_APP_ID and WECHAT_APP_SECRET)):
        return "已启用微信确认器，但缺少 CATMAT_WECHAT_APP_ID/APP_SECRET"
    return None


def jwt_sign(payload):
    secret = _auth_secret()
    if not secret:
        raise ValueError("JWT signing secret is not configured")
    header = {"alg": "HS256", "typ": "JWT"}
    head = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    body = _b64url_encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signing_input = (head + "." + body).encode("ascii")
    signature = _b64url_encode(hmac.new(secret, signing_input, hashlib.sha256).digest())
    return head + "." + body + "." + signature


def jwt_verify(token):
    """校验 HS256 access token，返回 (claims, error_code)。"""
    try:
        head, body, signature = token.split(".")
        expected = _b64url_encode(hmac.new(_auth_secret(), (head + "." + body).encode("ascii"),
                                            hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None, "UNAUTHORIZED"
        header = json.loads(_b64url_decode(head).decode("utf-8"))
        claims = json.loads(_b64url_decode(body).decode("utf-8"))
        if header.get("alg") != "HS256" or claims.get("typ") != "access":
            return None, "UNAUTHORIZED"
        if int(claims.get("exp") or 0) <= int(time.time()):
            return None, "TOKEN_EXPIRED"
        return claims, None
    except Exception:
        return None, "UNAUTHORIZED"


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


LOCAL_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
PASSWORD_MIN_LEN = 10
PASSWORD_MAX_LEN = 256
PASSWORD_SCRYPT_N = 2 ** 14
PASSWORD_SCRYPT_R = 8
PASSWORD_SCRYPT_P = 1


def _normalize_username(username):
    """本地用户名不区分大小写，仅允许便于人工管理的 ASCII 安全字符。"""
    return str(username or "").strip().lower()


def _validate_local_credentials(username, password):
    username = _normalize_username(username)
    if not LOCAL_USERNAME_RE.fullmatch(username):
        return None, "用户名须为 3-32 位字母、数字、点、下划线或连字符"
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_LEN:
        return None, "密码至少 %d 位" % PASSWORD_MIN_LEN
    if len(password) > PASSWORD_MAX_LEN:
        return None, "密码不能超过 %d 位" % PASSWORD_MAX_LEN
    return username, None


def _hash_password(password):
    """使用标准库 scrypt + 独立随机盐保存密码，绝不保存明文或普通 SHA-256。"""
    if not isinstance(password, str) or not (PASSWORD_MIN_LEN <= len(password) <= PASSWORD_MAX_LEN):
        raise ValueError("密码长度不符合要求")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                             n=PASSWORD_SCRYPT_N, r=PASSWORD_SCRYPT_R,
                             p=PASSWORD_SCRYPT_P, dklen=32)
    return "scrypt$%d$%d$%d$%s$%s" % (
        PASSWORD_SCRYPT_N, PASSWORD_SCRYPT_R, PASSWORD_SCRYPT_P,
        _b64url_encode(salt), _b64url_encode(derived))


def _verify_password(password, encoded):
    try:
        alg, n, r, p, salt, expected = str(encoded or "").split("$", 5)
        if alg != "scrypt":
            return False
        derived = hashlib.scrypt(str(password or "").encode("utf-8"),
                                 salt=_b64url_decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(_b64url_encode(derived), expected)
    except (TypeError, ValueError):
        return False


def _public_user(row, mode="local"):
    return {
        "uid": row["id"],
        "mode": mode,
        "username": row["username"] if "username" in row.keys() else None,
        "display_name": row["display_name"],
        "roles": [row["role"]],
        "disabled": bool(row["disabled"]),
    }


def _public_account(row):
    """账号管理响应白名单：不泄露 password_hash、openid、unionid。"""
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": _norm_role(row["role"]),
        "role_label": _role_label(row["role"]),
        "capabilities": _capabilities_of(row),
        "disabled": bool(row["disabled"]),
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
        "password_updated_at": row["password_updated_at"],
    }


_ACCOUNT_FIELDS = (
    "id", "openid", "unionid", "username", "password_hash", "password_updated_at",
    "display_name", "role", "disabled", "created_at", "last_login_at", "capabilities",
)


def _account_record(row):
    """保留 auth_users 原字段和 SQLite 值类型，用于受保护的角色镜像文件。"""
    return {key: row[key] for key in _ACCOUNT_FIELDS}


def _atomic_json_write(path, value):
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    tmp = path + ".tmp-" + secrets.token_hex(6)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _sync_account_role_files(conn):
    """从 SQLite 同一快照原子生成东翁/掌书与清客两个文件，不打印敏感字段。

    文件内角色统一为规范值 owner/steward/guest（admin/user 历史值在此归一）。
    """
    rows = conn.execute("SELECT * FROM auth_users ORDER BY id").fetchall()
    grouped = {"admin": [], "user": []}
    for row in rows:
        norm = _norm_role(row["role"])
        record = _account_record(row)
        record["role"] = norm
        if norm == ROLE_OWNER or norm == ROLE_STEWARD:
            grouped["admin"].append(record)
        elif norm == ROLE_GUEST:
            grouped["user"].append(record)
        else:
            raise RuntimeError("账号 %s 使用非法角色 %s" % (row["id"], row["role"]))
    _atomic_json_write(_account_role_path("admin"), grouped["admin"])
    _atomic_json_write(_account_role_path("user"), grouped["user"])
    return {"admin": len(grouped["admin"]), "user": len(grouped["user"])}


def _load_account_role_files(conn, repair=True):
    """加载并核验角色文件；重复、遗漏、字段或角色异常时按数据库原子重建。"""
    ADMIN_FILE_ROLES = (ROLE_OWNER, ROLE_STEWARD, "admin")
    USER_FILE_ROLES = (ROLE_GUEST, "user")

    def read_pair():
        out = {}
        for role, allowed in (("admin", ADMIN_FILE_ROLES), ("user", USER_FILE_ROLES)):
            path = _account_role_path(role)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError("%s 必须是 JSON 数组" % path)
            for item in data:
                if not isinstance(item, dict) or tuple(item.keys()) != _ACCOUNT_FIELDS:
                    raise ValueError("%s 的账号字段或顺序与 auth_users 不一致" % path)
                if item.get("role") not in allowed:
                    raise ValueError("%s 含角色错位账号" % path)
                if item.get("password_hash") and not str(item["password_hash"]).startswith("scrypt$"):
                    raise ValueError("%s 含非 scrypt 密码摘要" % path)
            out[role] = data
        return out

    expected = {}
    for row in conn.execute("SELECT * FROM auth_users ORDER BY id").fetchall():
        record = _account_record(row)
        record["role"] = _norm_role(row["role"])  # admin/user 历史值归一后与文件对照
        expected[row["id"]] = record
    try:
        grouped = read_pair()
        records = grouped["admin"] + grouped["user"]
        ids = [item.get("id") for item in records]
        if len(ids) != len(set(ids)):
            raise ValueError("管理员与普通用户文件存在重复账号")
        actual = {item["id"]: item for item in records}
        if actual != expected:
            raise ValueError("角色文件与 SQLite 账号数据不一致")
        return grouped
    except (OSError, ValueError, json.JSONDecodeError):
        if not repair:
            raise
        _sync_account_role_files(conn)
        return read_pair()


def _local_account_by_username(conn, username):
    """从拆分文件按角色定位账号，并以 SQLite 行作为最终会话与授权对象。"""
    grouped = _load_account_role_files(conn)
    matches = [item for role in ("admin", "user") for item in grouped[role]
               if item.get("username") == username]
    if len(matches) != 1:
        return None
    return conn.execute("SELECT * FROM auth_users WHERE id=? AND username=?",
                        (matches[0]["id"], username)).fetchone()


def _local_accounts_by_role(conn):
    """按管理员、普通用户顺序返回 SQLite 行，读取入口统一经过拆分文件核验。"""
    grouped = _load_account_role_files(conn)
    ids = [item["id"] for role in ("admin", "user") for item in grouped[role]
           if item.get("username") is not None]
    if not ids:
        return []
    rows = {row["id"]: row for row in conn.execute(
        "SELECT * FROM auth_users WHERE username IS NOT NULL").fetchall()}
    return [rows[i] for i in ids]


def _create_local_user(conn, username, password, display_name, role=ROLE_GUEST,
                       capabilities=None):
    username, error = _validate_local_credentials(username, password)
    if error:
        return None, error, "INPUT_ERROR"
    role = _norm_role(role)  # admin→owner、user→guest 归一
    if role == ROLE_OWNER:
        # 东翁唯一：创建前检查是否已存在本地东翁（username IS NOT NULL）
        exist_owner = conn.execute(
            "SELECT COUNT(*) c FROM auth_users WHERE role='owner' AND username IS NOT NULL"
        ).fetchone()["c"]
        if exist_owner:
            return None, "东翁仅此一位，不可再设", "OWNER_EXISTS"
    if role == ROLE_STEWARD:
        capabilities = _validate_capabilities(capabilities)
    else:
        capabilities = []
    display_name = str(display_name or username).strip()[:64]
    if not display_name:
        return None, "显示名称不能为空", "INPUT_ERROR"
    try:
        cur = conn.execute(
            "INSERT INTO auth_users(openid,unionid,username,password_hash,password_updated_at,"
            "display_name,role,capabilities) VALUES(?,?,?,?,?,?,?,?)",
            ("local:" + username, None, username, _hash_password(password), now_str(),
             display_name, role, json.dumps(capabilities, ensure_ascii=False)))
    except sqlite3.IntegrityError:
        return None, "用户名已存在", "USERNAME_EXISTS"
    return conn.execute("SELECT * FROM auth_users WHERE id=?", (cur.lastrowid,)).fetchone(), None, None


def _validate_capabilities(capabilities):
    """校验掌书能力包：仅接受 ALL_CAPABILITIES 中的项，去重保序。"""
    if capabilities is None:
        return []
    if isinstance(capabilities, str):
        try:
            capabilities = json.loads(capabilities or "[]")
        except Exception:
            return []
    if not isinstance(capabilities, (list, tuple)):
        return []
    valid = {CAP_MAINTAIN_DATA, CAP_DELETE_DATA, CAP_MANAGE_ACCOUNTS,
             CAP_RUN_TASKS, CAP_CONTROL_TASKS, CAP_RUN_PIPELINES,
             CAP_GLOBAL_SYNC, CAP_VIEW_AUDIT}
    out = []
    for cap in capabilities:
        cap = str(cap or "").strip()
        if cap in valid and cap not in out:
            out.append(cap)
    return out


def _capabilities_of(row):
    """读取账号的能力包（JSON 字符串 → 列表）。"""
    raw = row["capabilities"] if "capabilities" in row.keys() else None
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except Exception:
        return []


def _effective_role(row):
    """账号在授权层的“等效角色”。

    东翁（owner）唯一对应本地账号（username 非空）；系统令牌通道
    （openid=__admin_token__，即“平台管理员”身份）对标掌书（steward）记录，
    但作为东翁的“系统钥匙”在授权层始终全权放行（等效 owner）。
    """
    openid = row["openid"] if "openid" in row.keys() else None
    if openid == ADMIN_HOLDER_OPENID:
        return ROLE_OWNER
    return _norm_role(row["role"])


def _system_owner_id(conn):
    """任务归属兜底：无显式发起人（AI/系统通道/后台调度）时归东翁。

    优先系统令牌通道账号；未创建则取本地东翁（唯一 owner）。
    """
    r = conn.execute("SELECT id FROM auth_users WHERE openid=?",
                     (ADMIN_HOLDER_OPENID,)).fetchone()
    if r:
        return r["id"]
    r = conn.execute("SELECT id FROM auth_users WHERE role='owner' "
                     "AND username IS NOT NULL ORDER BY id LIMIT 1").fetchone()
    return r["id"] if r else None


def _upsert_admin_holder(conn):
    """取/建「管理员令牌」保留账号（auth_users 中 openid=__admin_token__，role=steward）。

    该账号即历史上的“平台管理员”，对标掌书记录（不占用“东翁唯一”名额，
    不显示为东翁）；授权层由 _effective_role 判为全权（东翁的系统钥匙）。
    """
    row = conn.execute("SELECT * FROM auth_users WHERE openid=?", (ADMIN_HOLDER_OPENID,)).fetchone()
    now = now_str()
    if row is None:
        cur = conn.execute(
            "INSERT INTO auth_users(openid,unionid,display_name,role,last_login_at) "
            "VALUES(?,?,?,?,?)",
            (ADMIN_HOLDER_OPENID, None, "系统通道", ROLE_STEWARD, now))
        row = conn.execute("SELECT * FROM auth_users WHERE id=?", (cur.lastrowid,)).fetchone()
    elif row["disabled"]:
        return None, "系统通道账号已停用", "ACCOUNT_DISABLED"
    else:
        conn.execute("UPDATE auth_users SET role=?, last_login_at=? WHERE id=?",
                     (ROLE_STEWARD, now, row["id"]))
        row = conn.execute("SELECT * FROM auth_users WHERE id=?", (row["id"],)).fetchone()
    return row, None, None


def _verify_admin_token(token):
    """恒定时间比较管理员令牌（防时序侧信道）。返回 (ok, errcode)。"""
    import hmac as _hmac
    token = (token or "").strip()
    if not ADMIN_TOKEN:
        return False, "NOT_READY"
    if not token:
        return False, "INPUT_ERROR"
    if not _hmac.compare_digest(ADMIN_TOKEN, token):
        return False, "INVALID_ADMIN_TOKEN"
    return True, None


def _issue_session_ex(conn, user_row, mode="wechat", scope=SCOPE_PLATFORM):
    """签发会话，返回 (对外载荷, session_id)。session_id 仅服务端内部使用（审计/追溯）。

    scope 决定该令牌能访问什么：
    - platform：平台业务会话（网页端登录后使用）
    - approve ：仅登录确认（微信小程序确认器专用，不能访问任何业务 API）
    """
    now = int(time.time())
    session_id = secrets.token_hex(16)
    claims = {
        "typ": "access", "uid": user_row["id"], "sid": session_id, "mode": mode,
        "display_name": user_row["display_name"], "roles": [user_row["role"]],
        "scope": scope,
        "iat": now, "exp": now + ACCESS_TOKEN_TTL,
    }
    access_token = jwt_sign(claims)
    refresh_token = secrets.token_urlsafe(48)
    try:
        conn.execute(
            "INSERT INTO auth_sessions(id,user_id,refresh_token_hash,expires_at,revoked,scope)"
            " VALUES(?,?,?,?,0,?)",
            (session_id, user_row["id"], _token_hash(refresh_token),
             now + REFRESH_TOKEN_TTL, scope))
    except sqlite3.OperationalError:
        # 极旧库尚未完成 scope 列迁移时的兼容路径（init_db 正常会补列，此分支仅兜底）
        conn.execute(
            "INSERT INTO auth_sessions(id,user_id,refresh_token_hash,expires_at,revoked) VALUES(?,?,?,?,0)",
            (session_id, user_row["id"], _token_hash(refresh_token), now + REFRESH_TOKEN_TTL))
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": ACCESS_TOKEN_TTL,
        "token_type": "Bearer",
        "mode": mode,
        "scope": scope,
        "uid": user_row["id"],
        "display_name": user_row["display_name"],
        "roles": [user_row["role"]],
    }, session_id


def _issue_session(conn, user_row, mode="wechat", scope=SCOPE_PLATFORM):
    return _issue_session_ex(conn, user_row, mode=mode, scope=scope)[0]


def _claims_scope(claims):
    """取令牌权限范围集合。老令牌（无 scope 字段）按 platform 处理，保证向后兼容。"""
    raw = (claims or {}).get("scope")
    if not raw:
        return {SCOPE_PLATFORM}
    return set(str(raw).split())


def _scope_allows(claims, needed):
    return needed in _claims_scope(claims)


def _wechat_code_to_identity(code):
    """用 jscode2session 将临时 code 换 openid；测试模式仅接受 test: 前缀。"""
    code = (code or "").strip()
    if not code:
        return None, "缺少微信登录 code", "INPUT_ERROR"
    if WECHAT_TEST_MODE:
        if not code.startswith("test:"):
            return None, "测试模式仅接受 test: 前缀的登录 code", "INVALID_WECHAT_CODE"
        suffix = re.sub(r"[^A-Za-z0-9_-]", "", code[5:])[:48] or "member"
        return {"openid": "test_openid_" + suffix, "unionid": None}, None, None
    import urllib.request
    import urllib.parse
    query = urllib.parse.urlencode({
        "appid": WECHAT_APP_ID, "secret": WECHAT_APP_SECRET,
        "js_code": code, "grant_type": "authorization_code",
    })
    try:
        with urllib.request.urlopen("https://api.weixin.qq.com/sns/jscode2session?" + query,
                                    timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        log_exc("微信 jscode2session 请求失败")
        return None, "微信登录服务暂不可用", "WECHAT_UPSTREAM_ERROR"
    if not data.get("openid"):
        LOG.warning("微信 code 校验失败 errcode=%s errmsg=%s", data.get("errcode"), data.get("errmsg"))
        return None, "微信登录凭证无效或已过期", "INVALID_WECHAT_CODE"
    return {"openid": data["openid"], "unionid": data.get("unionid")}, None, None


def _upsert_wechat_user(conn, identity):
    openid = identity["openid"]
    row = conn.execute("SELECT * FROM auth_users WHERE openid=?", (openid,)).fetchone()
    role = ROLE_OWNER if openid in WECHAT_ADMIN_OPENIDS else ROLE_GUEST
    now = now_str()
    if row is None:
        cursor = conn.execute(
            "INSERT INTO auth_users(openid,unionid,display_name,role,last_login_at) VALUES(?,?,?,?,?)",
            (openid, identity.get("unionid"), "科研用户-%s" % openid[-6:], role, now))
        row = conn.execute("SELECT * FROM auth_users WHERE id=?", (cursor.lastrowid,)).fetchone()
    else:
        conn.execute("UPDATE auth_users SET unionid=COALESCE(?,unionid), role=?, last_login_at=? WHERE id=?",
                     (identity.get("unionid"), role, now, row["id"]))
        row = conn.execute("SELECT * FROM auth_users WHERE id=?", (row["id"],)).fetchone()
    return row


# ==================== 登录挑战（微信确认器 · RFC 8628 风格） ====================
# 浏览器发起一次性挑战 → 微信小程序仅做批准/拒绝 → 浏览器凭 challenge_secret 兑换业务会话。
# 小程序拿到的令牌 scope=approve，除 /api/auth/approvals/* 外不能访问任何平台接口。

_RATE_BUCKETS = {}


def _rate_allow(key, limit, window=60):
    """进程内滑动窗口限流。仅用于防滥用，重启即清零，不持久化。"""
    if limit <= 0:
        return True
    now = time.time()
    bucket = _RATE_BUCKETS.setdefault(key, [])
    bucket[:] = [t for t in bucket if now - t < window]
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    if len(_RATE_BUCKETS) > 4096:  # 防止字典无限增长
        for k in [k for k, v in _RATE_BUCKETS.items() if not v]:
            _RATE_BUCKETS.pop(k, None)
    return True


def _client_ip(headers, peer_ip=None):
    """来源 IP；仅当直连方为本机反代时信任 X-Forwarded-For/X-Real-IP。"""
    peer = (peer_ip or "").strip()
    if peer in ("127.0.0.1", "::1"):
        xff = (headers.get("X-Forwarded-For") or "").strip()
        if xff:
            return xff.split(",")[0].strip()[:64]
        xri = (headers.get("X-Real-IP") or "").strip()
        if xri:
            return xri[:64]
    return peer[:64] if peer else None


def _gen_user_code(conn):
    """生成全局唯一的人工输入短码（易读字符集，剔除 0/O/1/I/L）。"""
    for _ in range(50):
        code = "".join(secrets.choice(CHALLENGE_CODE_ALPHABET)
                       for _ in range(CHALLENGE_USER_CODE_LEN))
        if not conn.execute("SELECT 1 FROM auth_login_challenges WHERE user_code=?",
                            (code,)).fetchone():
            return code
    return None


def _expire_stale_challenges(conn):
    """惰性过期：把已超时但仍 pending/approved 的挑战置为 expired。"""
    conn.execute(
        "UPDATE auth_login_challenges SET status='expired' "
        "WHERE status IN ('pending','approved') AND expires_at<=?", (int(time.time()),))


def _create_login_challenge(conn, client_hint=None, client_ip=None):
    """创建一次登录挑战。challenge_secret 明文只在此返回一次，服务端只存 SHA-256。"""
    _expire_stale_challenges(conn)
    user_code = _gen_user_code(conn)
    if not user_code:
        return None, "短码空间暂时紧张，请稍后重试", "CHALLENGE_BUSY"
    now = int(time.time())
    challenge_id = secrets.token_hex(16)
    secret = secrets.token_urlsafe(48)  # >= 256 bit 熵，仅浏览器持有
    conn.execute(
        "INSERT INTO auth_login_challenges"
        "(id,user_code,secret_hash,status,scope,client_hint,client_ip,created_at,expires_at)"
        " VALUES(?,?,?,'pending',?,?,?,?,?)",
        (challenge_id, user_code, _token_hash(secret), SCOPE_PLATFORM,
         (str(client_hint or "").strip()[:200] or None), client_ip, now, now + CHALLENGE_TTL))
    return {
        "challenge_id": challenge_id,
        "user_code": user_code,
        "challenge_secret": secret,
        "status": "pending",
        "expires_in": CHALLENGE_TTL,
        "interval": CHALLENGE_POLL_INTERVAL,
    }, None, None


def _find_challenge(conn, challenge_id, secret):
    """按 id + secret 定位挑战。secret 不匹配一律按「不存在」处理，避免可探测的差异化响应。"""
    if not challenge_id or not secret:
        return None
    row = conn.execute("SELECT * FROM auth_login_challenges WHERE id=?",
                       (str(challenge_id).strip(),)).fetchone()
    if not row:
        return None
    if not hmac.compare_digest(row["secret_hash"], _token_hash(secret)):
        return None
    return row


def _consume_challenge(conn, row, session_id):
    """原子兑换：仅 status='approved' 且未过期者可兑换，且全局只能成功一次。"""
    now = int(time.time())
    cur = conn.execute(
        "UPDATE auth_login_challenges SET status='consumed', consumed_at=?, exchange_session_id=? "
        "WHERE id=? AND status='approved' AND expires_at>?",
        (now, session_id, row["id"], now))
    return cur.rowcount == 1


def _decide_challenge(conn, row, approver_id, action):
    """批准/拒绝：仅 pending 状态可被决策，且只能决策一次。"""
    now = int(time.time())
    if action == "approve":
        cur = conn.execute(
            "UPDATE auth_login_challenges SET status='approved', approved_at=?, approver_user_id=? "
            "WHERE id=? AND status='pending' AND expires_at>?",
            (now, approver_id, row["id"], now))
    else:
        cur = conn.execute(
            "UPDATE auth_login_challenges SET status='denied', approved_at=?, approver_user_id=? "
            "WHERE id=? AND status='pending'",
            (now, approver_id, row["id"]))
    return cur.rowcount == 1


def _challenge_view(row, detail=False):
    """挑战的对外视图。detail=False 时只暴露确认人做判断所需的最小信息。"""
    view = {
        "challenge_id": row["id"],
        "user_code": row["user_code"],
        "status": row["status"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
    }
    if detail:
        view.update({
            "client_hint": row["client_hint"],
            "client_ip": row["client_ip"],
            "scope": row["scope"],
            "approved_at": row["approved_at"],
        })
    return view


def _audit_fail_count():
    with _AUDIT_WRITE_FAILS_LOCK:
        return _AUDIT_WRITE_FAILS


def audit(conn, action, target=None, params=None, result=None, ok=True, actor="local",
          commit=True):
    """[高5] 审计写入：时间 / 动作 / 对象 / 参数摘要 / 结果 / 调用方。

    设计原则：**审计永不阻断业务**——任何异常只落日志、绝不向上抛。
    """
    try:
        conn.execute(
            "INSERT INTO audit (actor, action, target, params, result, ok) "
            "VALUES (?,?,?,?,?,?)",
            (str(actor)[:64], str(action)[:64],
             (str(target)[:200] if target is not None else None),
             (json.dumps(params, ensure_ascii=False)[:2000] if params is not None else None),
             (str(result)[:2000] if result is not None else None),
             1 if ok else 0))
        if commit:
            conn.commit()
        return True
    except Exception:
        if commit:
            try:
                conn.rollback()
            except Exception:
                pass
        global _AUDIT_WRITE_FAILS
        with _AUDIT_WRITE_FAILS_LOCK:
            _AUDIT_WRITE_FAILS += 1
            first_failure = _AUDIT_WRITE_FAILS == 1
        if first_failure:
            LOG.warning("[AUDIT WRITE FAILURE] 审计写入首次失败，后续失败将累计到 /api/metrics")
        log_exc("审计写入失败 action=%s target=%s" % (action, target))
        return False


# 需要审计的写操作域：覆盖数据增删改与计算/训练/工作流提交
AUDIT_ROOTS = {
    "systems", "literature", "kb", "tasks", "ml", "al", "hts", "workflow", "dpgen",
    "cp2k-submit", "postprocess", "build", "import", "params", "chart", "agent",
}


# ==================== 休go记忆 ↔ 平台知识库 双向同步 ====================
def sync_knowledge_to_agent_memory(conn):
    """把平台知识库（knowledge 表）增量同步进休go的记忆（agent_memory）。

    增量判定：以 ref_id（= knowledge.id）为唯一键，内容变化才更新，无变化跳过。
    返回 (新增, 更新, 跳过, 知识库总数)。任何异常只落日志，不阻断启动。
    """
    try:
        rows = conn.execute(
            "SELECT id, category, topic, keyword, recommended, rationale, source, origin "
            "FROM knowledge ORDER BY id").fetchall()
        added = updated = skipped = 0
        now = now_str()
        for r in rows:
            content = "\n".join(x for x in (
                ("推荐：" + r["recommended"]) if r["recommended"] else "",
                ("理由：" + r["rationale"]) if r["rationale"] else "",
                ("出处：" + r["source"]) if r["source"] else "",
            ) if x).strip()
            cur = conn.execute(
                "SELECT id, content FROM agent_memory WHERE ref_id=?", (r["id"],)).fetchone()
            if cur is None:
                conn.execute(
                    "INSERT INTO agent_memory (category, topic, keyword, content, source, "
                    "ref_id, origin, created_at, synced_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (r["category"], r["topic"], r["keyword"], content, "knowledge_sync",
                     r["id"], r["origin"], now, now))
                added += 1
            elif (cur["content"] or "") != content:
                conn.execute(
                    "UPDATE agent_memory SET category=?, topic=?, keyword=?, content=?, "
                    "origin=?, updated_at=?, synced_at=? WHERE id=?",
                    (r["category"], r["topic"], r["keyword"], content, r["origin"],
                     now, now, cur["id"]))
                updated += 1
            else:
                skipped += 1
        conn.commit()
        LOG.info("知识库 → 休go记忆 同步：新增 %d / 更新 %d / 无变化 %d（知识库 %d 条）",
                 added, updated, skipped, len(rows))
        return added, updated, skipped, len(rows)
    except Exception:
        log_exc("知识库同步到休go记忆失败")
        try:
            conn.rollback()
        except Exception:
            pass
        return 0, 0, 0, 0


def agent_memory_index(conn, max_cats=20, kw_per_cat=4, kw_len=22, line_len=110):
    """生成休go记忆的**紧凑索引**注入系统提示（分类 + 条数 + 代表关键词）。

    刻意只放索引且严格限长：某些分类（如平台技能SKILL）的 keyword 存的是整段文档描述，
    若不加截断会把系统提示撑爆。细节通过 search_agent_memory / search_knowledge 按需检索。
    """
    try:
        total = conn.execute("SELECT count(*) c FROM agent_memory").fetchone()["c"]
        if not total:
            return ""
        cats = conn.execute(
            "SELECT category, count(*) c FROM agent_memory "
            "GROUP BY category ORDER BY c DESC LIMIT ?", (max_cats,)).fetchall()
        lines = []
        for r in cats:
            cat = (r["category"] or "未分类").strip()
            kws = conn.execute(
                "SELECT keyword FROM agent_memory WHERE category IS ? "
                "AND keyword IS NOT NULL AND keyword != '' LIMIT ?",
                (r["category"], kw_per_cat)).fetchall()
            kw_list = []
            for k in kws:
                kw = " ".join((k["keyword"] or "").split())   # 压平换行与多余空白
                if kw:
                    kw_list.append(kw[:kw_len])
            line = "· %s(%d)%s" % (cat, r["c"],
                                   ("：" + "、".join(kw_list)) if kw_list else "")
            lines.append(line[:line_len])
        return ("【记忆索引】你已记住 %d 条平台知识，覆盖 %d 个分类：\n%s\n"
                "需要具体内容时用 search_agent_memory 检索；CP2K 参数类问题也可用 search_knowledge。"
                % (total, len(cats), "\n".join(lines)))
    except Exception:
        log_exc("生成休go记忆索引失败")
        return ""


def _summarize_body(body, max_keys=12, max_len=200):
    """审计用：把请求体压成简要摘要，避免批量导入把审计表撑爆。"""
    try:
        if not isinstance(body, dict):
            return {"_type": type(body).__name__}
        out = {}
        for k, v in list(body.items())[:max_keys]:
            s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            out[str(k)] = s[:max_len]
        if len(body) > max_keys:
            out["_more_keys"] = len(body) - max_keys
        return out
    except Exception:
        return {"_unserializable": True}


def init_db():
    _lock_acquire()
    try:
        conn = get_db()
        conn.executescript(SCHEMA)
        postprocessing.recover(conn)
        # 休go 编排层：任务流与步骤表（独立 schema，便于旧库平滑升级）
        try:
            conn.executescript(AGENT_ORCH_SCHEMA)
        except Exception:
            log_exc("创建 agent_runs/agent_run_steps 失败")
        # 兼容旧库：为 systems 表补充 structure 列（若不存在）
        try:
            conn.execute("ALTER TABLE systems ADD COLUMN structure TEXT DEFAULT '[]'")
        except Exception:
            pass
        # 兼容旧库：为 systems 表补充 uuid 列（若不存在）
        try:
            conn.execute("ALTER TABLE systems ADD COLUMN uuid TEXT")
        except Exception:
            pass
        # 兼容旧库：为 systems 表补充 folder 列（二级目录，若不存在）
        try:
            conn.execute("ALTER TABLE systems ADD COLUMN folder TEXT")
        except Exception:
            pass
        # 兼容旧库：为 literature 表补充 abstract 列（若不存在）
        try:
            conn.execute("ALTER TABLE literature ADD COLUMN abstract TEXT")
        except Exception:
            pass
        # 兼容旧库：为 literature 表补充 highlights / interpret_full / verified_at / stability_condition 列
        for col, ddl in (("highlights", "TEXT"), ("interpret_full", "TEXT"), ("verified_at", "TEXT"),
                         ("stability_condition", "TEXT")):
            try:
                conn.execute("ALTER TABLE literature ADD COLUMN %s %s" % (col, ddl))
            except Exception:
                pass
        # 兼容旧库：为 knowledge 表补充 origin 列（来源区分：CP2K官方 / 思想家公社）
        try:
            conn.execute("ALTER TABLE knowledge ADD COLUMN origin TEXT")
        except Exception:
            pass
        # 兼容旧库：为 ml_tasks 表补充 task_type 列（区分 ml / al 任务）
        try:
            conn.execute("ALTER TABLE ml_tasks ADD COLUMN task_type TEXT DEFAULT 'ml'")
        except Exception:
            pass
        # 本地账号密码鉴权：兼容旧库，为 auth_users 补充本地凭据字段。
        for col, ddl in (("username", "TEXT"), ("password_hash", "TEXT"),
                         ("password_updated_at", "TEXT")):
            try:
                conn.execute("ALTER TABLE auth_users ADD COLUMN %s %s" % (col, ddl))
            except Exception:
                pass
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_users_username "
                         "ON auth_users(username) WHERE username IS NOT NULL")
        except Exception:
            pass
        # 微信确认器兼容字段：保留旧挑战会话的 scope，默认不开放确认器入口。
        try:
            conn.execute("ALTER TABLE auth_sessions ADD COLUMN scope TEXT DEFAULT 'platform'")
        except Exception:
            pass
        try:
            conn.execute("UPDATE auth_sessions SET scope='platform' WHERE scope IS NULL OR scope=''")
        except Exception:
            pass
        # ---- 三角色体系（东翁/掌书/清客）兼容迁移 ----
        try:
            conn.execute("ALTER TABLE auth_users ADD COLUMN capabilities TEXT DEFAULT '[]'")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN owner_id INTEGER")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN cm_code TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE knowledge ADD COLUMN archived_by TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE literature ADD COLUMN interpreted_by TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE literature ADD COLUMN interpret_full TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE literature ADD COLUMN highlights TEXT DEFAULT '[]'")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE literature ADD COLUMN stability_condition TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE literature ADD COLUMN verified_at TEXT")
        except Exception:
            pass
        # 任务归属：工作流 / HTS 批次 / ML·AL 异步任务记录发起账号（东翁/掌书/清客/AI 通道）
        for tbl, col in (("workflows", "owner_id"), ("hts_batches", "owner_id"),
                         ("ml_tasks", "owner_id")):
            try:
                conn.execute("ALTER TABLE %s ADD COLUMN %s INTEGER" % (tbl, col))
            except Exception:
                pass
        # 角色值迁移：本地管理员 admin→owner（多个本地 admin 时仅保留 id 最小者为东翁，
        # 其余降为掌书）；本地 user→guest。
        # 系统令牌账号（openid=__admin_token__）即历史上的“平台管理员”，对标掌书
        # （steward）记录，不占用“东翁唯一”名额、不显示为东翁；授权层经
        # _effective_role 判为东翁的系统钥匙（全权）。
        try:
            rows = conn.execute(
                "SELECT id FROM auth_users WHERE role='admin' AND username IS NOT NULL "
                "ORDER BY id").fetchall()
            for i, r in enumerate(rows):
                conn.execute("UPDATE auth_users SET role=? WHERE id=?",
                             (ROLE_OWNER if i == 0 else ROLE_STEWARD, r["id"]))
            conn.execute("UPDATE auth_users SET role=? WHERE role='user' AND username IS NOT NULL",
                         (ROLE_GUEST,))
            conn.execute("UPDATE auth_users SET display_name='系统通道' WHERE openid=?",
                         (ADMIN_HOLDER_OPENID,))
            conn.execute("UPDATE auth_users SET role=? WHERE role='admin' "
                         "AND openid=?", (ROLE_STEWARD, ADMIN_HOLDER_OPENID))
        except Exception:
            pass
        seed_env_codes(conn)
        conn.commit()
        # 迁移完成后同步本地账号角色文件；SQLite 仍是唯一事务权威源。
        _sync_account_role_files(conn)
        conn.close()
    finally:
        _lock_release()


def seed_if_empty():
    """首次运行：从 data/*.json 导入种子数据。"""
    _lock_acquire()
    try:
        conn = get_db()
        n = conn.execute("SELECT COUNT(*) c FROM literature").fetchone()["c"]
        if n == 0:
            for fn in ("seed_systems.json", "seed_literature.json",
                       "seed_knowledge.json", "seed_knowledge_sobko.json"):
                path = os.path.join(DATA_DIR, fn)
                if not os.path.exists(path):
                    continue
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if fn.startswith("seed_systems"):
                    for s in data:
                        _insert_system(conn, s)
                elif fn.startswith("seed_literature"):
                    for it in data:
                        _insert_literature(conn, it)
                elif fn.startswith("seed_knowledge"):
                    for it in data:
                        _insert_knowledge(conn, it)
        conn.commit()
        conn.close()
    finally:
        _lock_release()


def _insert_system(conn, s):
    import uuid as _uuid
    conn.execute(
        "INSERT INTO systems(uuid,name,formula,composition,space_group,lattice,layers,vacuum,elements,"
        "source,descriptors,structure,folder) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (s.get("uuid") or str(_uuid.uuid4()), s.get("name"), s.get("formula"), s.get("composition"),
         s.get("space_group"), s.get("lattice"), s.get("layers"), s.get("vacuum"),
         json.dumps(s.get("elements", []), ensure_ascii=False),
         s.get("source", "平台"), json.dumps(s.get("descriptors", []), ensure_ascii=False),
         s.get("structure") if isinstance(s.get("structure"), str)
         else json.dumps(s.get("structure") or [], ensure_ascii=False, sort_keys=True),
         s.get("folder") or None))


def _insert_literature(conn, it):
    conn.execute(
        "INSERT INTO literature(title,authors,journal,year,doi,material_system,electrolyzer,"
        "overpotential_10,tafel_slope,stability_hours,mass_activity,dft_code,uses_cp2k,cp2k_settings,"
        "abstract,conclusion,tags,verified) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (it.get("title"), it.get("authors"), it.get("journal"), it.get("year"), it.get("doi"),
         it.get("material_system"), it.get("electrolyzer"), it.get("overpotential_10"),
         it.get("tafel_slope"), it.get("stability_hours"), it.get("mass_activity"),
         it.get("dft_code"), 1 if it.get("uses_cp2k") else 0, it.get("cp2k_settings"),
         it.get("abstract"), it.get("conclusion"), json.dumps(it.get("tags", []), ensure_ascii=False),
         1 if it.get("verified") else 0))


def _insert_knowledge(conn, it):
    conn.execute(
        "INSERT INTO knowledge(category,topic,keyword,recommended,rationale,source,origin) "
        "VALUES(?,?,?,?,?,?,?)",
        (it.get("category"), it.get("topic"), it.get("keyword"), it.get("recommended"),
         it.get("rationale"), it.get("source"), it.get("origin")))


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def rows_to_list(rows):
    out = []
    for r in rows:
        d = dict(r)
        for k in ("descriptors", "elements", "tags", "log", "resources", "result", "features", "structure"):
            if k in d and isinstance(d[k], str):
                try:
                    d[k] = json.loads(d[k])
                except Exception:
                    pass
        out.append(d)
    return out


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _owner_view(conn, rows):
    """给任务/工作流/批次/训练任务行附加发起人展示字段（名称 + 身份）。

    owner_id 为空 → 无发起记录；指向系统令牌账号时名称即“系统通道”。
    """
    try:
        users = {r["id"]: r for r in conn.execute(
            "SELECT id,display_name,role FROM auth_users")}
    except Exception:
        users = {}
    out = []
    for d in rows:
        d = dict(d)
        o = d.get("owner_id")
        u = users.get(o) if o is not None else None
        if u is not None:
            d["owner_name"] = u["display_name"] or "系统通道"
            d["owner_role"] = _role_label(u["role"])
        else:
            d["owner_name"] = None
            d["owner_role"] = None
        out.append(d)
    return out


# ==================== 数据集 CSV 智能导入（表头自动识别与特征/标签映射） ====================
# 需求：机器学习 / 主动学习导入 CSV 时，应自动检索表头并识别各列角色——
# 样本名列（name）、目标/标签列（target）、特征列（features），避免依赖固定列名。
DATASET_NAME_HINTS = ("name", "sample", "samples", "id", "material", "system",
                      "structure", "体系", "样本", "材料", "结构", "名称")
DATASET_TARGET_HINTS = ("target", "label", "y", "eta", "eta_oer", "eta_10", "eta_0",
                        "eta_oer_10", "overpotential", "η", "η_oer", "过电位", "目标",
                        "标签", "活性", "性能", "response")
AL_ED_HINTS = ("ed", "d_band_center", "dband", "d-band", "d_band", "dbandcenter",
               "εd", "ε_d", "eps_d", "epsilon_d", "d带中心", "d带", "带中心", "d 带中心")
DATASET_IGNORE_HINTS = ("source", "note", "comment", "remark", "origin", "来源",
                        "备注", "说明", "注释")


def _csv_col_stats(head, sample_rows):
    """统计一列的类型信息：是否全数值、非空数量、首个样例值。"""
    nonempty = 0
    numeric = 0
    first = ""
    for r in sample_rows:
        v = r.get(head)
        if v is None or str(v).strip() == "":
            continue
        nonempty += 1
        if first == "":
            first = str(v).strip()
        try:
            float(str(v).replace(",", "").strip())
            numeric += 1
        except ValueError:
            pass
    return {"name": head, "numeric": nonempty > 0 and numeric == nonempty,
            "nonempty": nonempty, "total": len(sample_rows), "sample": first}


def _csv_hint_hit(head, hints):
    hh = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]", "", str(head or "").lower())
    return any(k.replace("_", "").replace("-", "") in hh for k in hints)


# ---------------------------------------------------------------------------
# 数据集按模块独立落盘（ML-XXX.csv / AL-XXX.csv）
# ---------------------------------------------------------------------------
_MODULE_PREFIX = {"ml": "ML", "al": "AL", "dpgen": "DPGEN"}


def _norm_features(feats):
    """features 可能是 dict，也可能是 JSON 字符串（导入路径两种都会出现）。"""
    if isinstance(feats, dict):
        return feats
    if isinstance(feats, str):
        try:
            v = json.loads(feats)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    return {}


def dataset_csv_text(rows):
    """把 [{name, features, target}] 渲染成 CSV 文本（name,<特征列...>,target）。"""
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        return ""
    feat_keys = []
    norm = []
    for r in rows:
        feats = _norm_features(r.get("features"))
        for k in feats:
            if k not in feat_keys:
                feat_keys.append(k)
        norm.append((r.get("name") or "", feats, r.get("target")))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name"] + feat_keys + ["target"])
    for name, feats, tgt in norm:
        w.writerow([name] + [feats.get(k, "") for k in feat_keys]
                   + ["" if tgt is None else tgt])
    return buf.getvalue()


def save_dataset_file(module, base_name, rows):
    """按模块前缀落盘数据集为独立 CSV 文件。

    module: "ml" | "al"（写入文件名前缀 ML- / AL-）
    同名已存在时自动追加 -2/-3 序号，避免覆盖历史数据集。
    返回 (文件路径, 文件名)；无数据返回 (None, None)。
    """
    text = dataset_csv_text(rows)
    if not text:
        return None, None
    prefix = _MODULE_PREFIX.get(str(module or "").lower(), str(module or "").upper() or "DATA")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(base_name or "dataset")).strip("_") or "dataset"
    os.makedirs(DATASETS_DIR, exist_ok=True)
    fname = "%s-%s.csv" % (prefix, safe)
    path = os.path.join(DATASETS_DIR, fname)
    n = 1
    while os.path.exists(path):
        n += 1
        fname = "%s-%s-%d.csv" % (prefix, safe, n)
        path = os.path.join(DATASETS_DIR, fname)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return path, fname


def list_dataset_files(module=None):
    """列出数据集目录下的 CSV 文件；module 非空时只返回该模块前缀的文件。"""
    if not os.path.isdir(DATASETS_DIR):
        return []
    prefix = _MODULE_PREFIX.get(str(module or "").lower()) if module else None
    out = []
    for fn in sorted(os.listdir(DATASETS_DIR)):
        if not fn.lower().endswith(".csv"):
            continue
        if prefix and not fn.upper().startswith(prefix + "-"):
            continue
        full = os.path.join(DATASETS_DIR, fn)
        if not os.path.isfile(full):
            continue
        try:
            stt = os.stat(full)
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                row_count = max(0, sum(1 for _ in f) - 1)
        except OSError:
            continue
        out.append({"file": fn, "path": full, "size": stt.st_size,
                    "rows": row_count, "mtime": stt.st_mtime})
    return out


def load_dataset_file(path, module=None):
    """读取数据集文件为 [{name, features, target}]；带模块前缀校验，防止跨模块串数据。"""
    full = os.path.abspath(path)
    if os.path.dirname(full) != os.path.abspath(DATASETS_DIR):
        return None, "数据集文件不在受管目录内"
    if not os.path.isfile(full):
        return None, "数据集文件不存在"
    if module:
        prefix = _MODULE_PREFIX.get(str(module).lower())
        if prefix and not os.path.basename(full).upper().startswith(prefix + "-"):
            return None, "数据集不属于该模块（%s）" % module
    rows = []
    with open(full, "r", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            name = (r.get("name") or "").strip()
            if not name:
                continue
            feats = {k: v for k, v in r.items() if k not in ("name", "target")}
            tgt = r.get("target")
            try:
                tgt = float(tgt) if tgt not in (None, "") else None
            except (TypeError, ValueError):
                tgt = None
            rows.append({"name": name, "features": feats, "target": tgt})
    return rows, None


def sniff_dataset_csv(text):
    """读取 CSV（兼容 UTF-8 BOM），返回列统计与抽样行，用于前端列映射预览。"""
    buf = io.StringIO(str(text or "").lstrip("\ufeff"))
    rdr = csv.DictReader(buf)
    heads = [h for h in (rdr.fieldnames or []) if h is not None]
    sample_rows = []
    for i, r in enumerate(rdr):
        if i >= 50:
            break
        sample_rows.append(r)
    cols = [_csv_col_stats(h, sample_rows) for h in heads]
    return {"columns": cols, "sample_rows": sample_rows, "head": heads}


def classify_dataset_columns(cols, mode="ml"):
    """根据列统计自动判定角色：name / target / feature / ignored。

    mode='al' 时额外识别 ε_d 特征列（主动学习语义为 ε_d → η_OER）。
    返回 {name_col, target_col, feature_cols, ed_col, ignored_cols, columns_with_kind}
    """
    out = []
    name_col = None
    target_col = None
    ed_col = None
    # 1) 样本名列：命中的非数值列优先；数值列命中（如数字 id）亦可
    for c in cols:
        if not c["numeric"] and _csv_hint_hit(c["name"], DATASET_NAME_HINTS):
            name_col = c["name"]
            break
    # 2) 目标/标签列：数值列中命中 target 语义（AL 只认 eta/η 系）
    for c in cols:
        h = c["name"]
        if h == name_col or not c["numeric"]:
            continue
        if mode == "al":
            if not _csv_hint_hit(h, ("eta", "η", "过电位", "target", "目标", "活性")):
                continue
        if not _csv_hint_hit(h, DATASET_TARGET_HINTS):
            continue
        target_col = h
        break
    # 3) AL：ε_d 特征列
    if mode == "al":
        for c in cols:
            h = c["name"]
            if h in (name_col, target_col) or not c["numeric"]:
                continue
            if _csv_hint_hit(h, AL_ED_HINTS):
                ed_col = h
                break
    feature_cols = []
    ignored_cols = []
    for c in cols:
        h = c["name"]
        kind = "feature"
        if h == name_col:
            kind = "name"
        elif h == target_col:
            kind = "target"
        elif mode == "al" and h == ed_col:
            kind = "feature"
        elif not c["numeric"]:
            kind = "ignored"
        elif _csv_hint_hit(h, DATASET_IGNORE_HINTS):
            kind = "ignored"
        elif mode == "al":
            # AL 单特征（ε_d）之外的多余数值列视为可附加特征，保留
            kind = "feature"
        if kind == "feature":
            feature_cols.append(h)
        if kind == "ignored":
            ignored_cols.append(h)
        entry = dict(c)
        entry["kind"] = kind
        out.append(entry)
    return {"name_col": name_col, "target_col": target_col,
            "ed_col": ed_col if mode == "al" else None,
            "feature_cols": feature_cols, "ignored_cols": ignored_cols,
            "columns": out}


def normalize_dataset_csv(text, mapping=None, mode="ml"):
    """把任意表头 CSV 按 mapping（或自动识别）解析为统一行结构。

    返回 {columns(detected), rows:[{name, features, target}], skipped, warnings}
    mapping: {name_col, target_col, feature_cols:[...]|None, ed_col, exclude_cols:[...]}
    """
    sniff = sniff_dataset_csv(text)
    heads = sniff["head"]
    if not heads:
        raise ValueError("CSV 无有效表头")
    cols = sniff["columns"]
    auto = classify_dataset_columns(cols, mode=mode)
    m = mapping or {}
    exclude = set(m.get("exclude_cols") or [])
    name_col = (str(m.get("name_col") or "").strip() or auto["name_col"] or "")
    target_col = (str(m.get("target_col") or "").strip() or auto["target_col"] or "")
    ed_col = (str(m.get("ed_col") or "").strip() or auto["ed_col"] or "")
    fe_cols = m.get("feature_cols")
    if not isinstance(fe_cols, list) or not fe_cols:
        fe_cols = [h for h in auto["feature_cols"] if h not in exclude]
    fe_cols = [str(h) for h in fe_cols if h and h != name_col and h != target_col]
    if mode == "al" and ed_col and ed_col not in fe_cols:
        fe_cols.insert(0, ed_col)
    warnings = []
    if not target_col and auto["target_col"]:
        target_col = auto["target_col"]
    if not name_col:
        warnings.append("未识别到样本名列，将自动生成 sample_0001… 序号作为名称")
    # 重新读取全量数据
    buf = io.StringIO(str(text or "").lstrip("\ufeff"))
    rdr = csv.DictReader(buf)
    rows = []
    skipped = 0
    idx = 0
    for r in rdr:
        idx += 1
        nm = (str(r.get(name_col) or "").strip()) if name_col else ""
        if not nm:
            nm = "sample_%04d" % idx
        feats = {}
        bad = False
        for h in fe_cols:
            v = r.get(h)
            if v is None or str(v).strip() == "":
                continue
            try:
                feats[str(h)] = float(str(v).replace(",", "").strip())
            except ValueError:
                warnings.append("第 %d 行特征列「%s」非数值，已忽略该值" % (idx, h))
                bad = True
        if bad:
            pass
        tgt = None
        if target_col:
            tv = r.get(target_col)
            if tv not in (None, ""):
                try:
                    tgt = float(str(tv).replace(",", "").strip())
                except ValueError:
                    warnings.append("第 %d 行目标列「%s」非数值，按缺失处理" % (idx, target_col))
        rows.append({"name": nm, "features": feats, "target": tgt})
    if not rows:
        raise ValueError("CSV 无有效数据行")
    return {"rows": rows, "skipped": skipped, "warnings": warnings[:20],
            "detected": auto, "feature_cols": fe_cols,
            "name_col": name_col, "target_col": target_col,
            "ed_col": ed_col if mode == "al" else None}





# ---- 资源指标缓存 ----
# 前端每 2.5s 轮询 /api/metrics，而服务端请求走全局 _lock 串行；若每次都执行
# nvidia-smi（子进程，最坏 3s），会阻塞其他全部 API 请求 → 平台整体卡顿。
# 因此：主机指标缓存 2s（后台线程预刷新，请求零等待），GPU 单独缓存 8s（nvidia-smi 开销大）。
_METRICS_CACHE = {"ts": 0.0, "data": None}
_METRICS_TTL = 1.5
_CPU_PREV = {"ts": 0.0, "idle": 0.0, "total": 0.0, "pct": 0.0}  # /proc/stat 上一次采样，用于 CPU busy% 差值
_GPU_CACHE = {"ts": 0.0, "data": []}
_GPU_TTL = 8.0


def host_metrics_gpu():
    """GPU 指标（nvidia-smi，8s 缓存，避免频繁子进程调用阻塞请求）。"""
    now = time.time()
    if _GPU_CACHE["data"] is not None and (now - _GPU_CACHE["ts"]) < _GPU_TTL:
        return _GPU_CACHE["data"]
    gpus = []
    try:
        with _blocking_unlocked():
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,name",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=1.5).stdout.strip()
        for line in out.splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) < 4:
                continue
            u, mu, mt, name = parts[0], parts[1], parts[2], parts[3]
            gpus.append({"util": int(u), "mem_used": round(float(mu) / 1024, 1),
                         "mem_total": round(float(mt) / 1024, 1), "name": name})
    except Exception:
        pass
    if gpus:
        _GPU_CACHE["ts"] = now
        _GPU_CACHE["data"] = gpus
    return gpus


def host_metrics(force=False):
    """读取本机真实资源指标（/proc + 磁盘 + GPU），带 2s 缓存（force 强制刷新）。"""
    now = time.time()
    if (not force and _METRICS_CACHE["data"] is not None
            and (now - _METRICS_CACHE["ts"]) < _METRICS_TTL):
        return _METRICS_CACHE["data"]
    m = {"cpu_percent": 0.0, "cpu_cores": os.cpu_count() or 1,
         "load1": 0.0, "load5": 0.0, "load15": 0.0,
         "mem_used": 0, "mem_total": 1, "disk_used": 0, "disk_total": 1,
         "gpu": [], "hostname": os.uname().nodename if hasattr(os, "uname") else "unknown",
         "ts": now, "cached": False}
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            m["load1"], m["load5"], m["load15"] = (float(parts[0]), float(parts[1]), float(parts[2]))
    except Exception:
        pass
    # CPU 占用率：基于 /proc/stat 两次采样差值算真实 busy%（与 top/htop 的 %Cpu 一致），
    # 不再用 loadavg（loadavg 含 I/O 等待、且不反映多核实际占用，与真实 CPU 出入大）。
    # 采样窗口 = 后台 _metrics_updater 的刷新间隔（1.5s）。
    try:
        with open("/proc/stat") as f:
            _parts = f.readline().split()
        _vals = [float(x) for x in _parts[1:]]
        _idle = _vals[3] + _vals[4]          # idle + iowait
        _total = sum(_vals)
        _prev = _CPU_PREV
        _dt = now - _prev["ts"]
        if _prev["ts"] > 0 and _dt > 0.05 and _total > _prev["total"]:
            _dtotal = _total - _prev["total"]
            _didle = _idle - _prev["idle"]
            _busy = (_dtotal - _didle) / _dtotal * 100.0
            m["cpu_percent"] = max(0.0, min(100.0, _busy))
        else:
            m["cpu_percent"] = _prev["pct"]
        _CPU_PREV["ts"] = now
        _CPU_PREV["idle"] = _idle
        _CPU_PREV["total"] = _total
        _CPU_PREV["pct"] = m["cpu_percent"]
    except Exception:
        pass
    try:
        with open("/proc/meminfo") as f:
            d = {}
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    d[k.strip()] = int(v.strip().split()[0]) * 1024
            m["mem_total"] = d.get("MemTotal", 1)
            m["mem_used"] = m["mem_total"] - d.get("MemAvailable", d.get("MemFree", 0))
            m["mem_percent"] = round(m["mem_used"] / max(m["mem_total"], 1) * 100.0, 1)
    except Exception:
        pass
    try:
        st = os.statvfs(BASE_DIR)
        m["disk_total"] = st.f_blocks * st.f_frsize
        m["disk_used"] = (st.f_blocks - st.f_bfree) * st.f_frsize
    except Exception:
        pass
    m["gpu"] = host_metrics_gpu()
    _METRICS_CACHE["ts"] = now
    _METRICS_CACHE["data"] = m
    return m


def _metrics_updater():
    """后台守护线程：每 1.5s 预刷新指标缓存，使 /api/metrics 请求几乎零等待。"""
    while True:
        try:
            host_metrics(force=True)
        except Exception:
            pass
        time.sleep(1.5)


_PROCESS_SAMPLER = ProcessSampler()


def process_snapshot():
    """所有模块共享短 TTL 快照；等待采样不持有 HTTP 全局锁。"""
    with _blocking_unlocked():
        return _PROCESS_SAMPLER.snapshot()


# --------------------------------------------------------------------------
# AiiDA 探针 + ASE 结构生成（真实计算能力）
# 外部程序/环境路径统一支持环境变量覆盖（CATMAT_*），便于迁移到其他服务器：
#   只需 export CATMAT_AII_PY=... CATMAT_CP2K_DATA_DIR=... 即可零改动迁移
# --------------------------------------------------------------------------
AII_PY = os.environ.get("CATMAT_AII_PY") or os.path.expanduser("~/anaconda3/envs/aiida/bin/python")
# 机器学习 / 主动学习训练环境（dpmd 环境含 torch 2.8+cu129，CUDA 功能齐全，2×RTX 4090 可用）
ML_PY = os.environ.get("CATMAT_ML_PY") or os.path.expanduser("~/anaconda3/envs/dpmd/bin/python")
TOOLS_DIR = os.path.join(BASE_DIR, "catmat_tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)
# 休go · 机器学习训练顾问/结果解读师（独立模块，与 ML/AL 解耦、可复用）
try:
    import ml_agent  # noqa: E402
except Exception as _e:  # 模块缺失不应阻断服务启动
    ml_agent = None
    LOG.warning("ml_agent 模块未加载：%s", _e)
# 国内主流大模型（OpenAI 兼容端点）自适应适配层
try:
    import llm_vendors  # noqa: E402
except Exception as _e:
    llm_vendors = None
    LOG.warning("llm_vendors 模块未加载：%s", _e)
CLI_PATH = os.path.join(BASE_DIR, "catmat.py")
PROBE_PATH = os.path.join(TOOLS_DIR, "aiida_probe.py")
AIIDA_FILES = os.environ.get("CATMAT_AIIDA_FILES") or os.path.expanduser("~/aiida_files")
POSTPROCESS = os.path.join(TOOLS_DIR, "postprocess_pdos.py")
# Additional scientific directories are configurable; empty disables extra roots.
POSTPROCESS_EXTRA_ROOTS = [os.path.abspath(os.path.expanduser(p)) for p in
    os.environ.get("CATMAT_POSTPROCESS_ROOTS", "~/phd" + os.pathsep + "~/workflow").split(os.pathsep) if p.strip()]
MULTIWFN_BIN = os.environ.get("CATMAT_MULTIWFN_BIN") or os.path.expanduser("~/bin/Multiwfn/Multiwfn")
CP2KVIEW_DIR = os.environ.get("CATMAT_CP2KVIEW_DIR") or (
    os.path.join(BASE_DIR, "cp2kview") if os.path.isdir(os.path.join(BASE_DIR, "cp2kview"))
    else os.path.expanduser("~/bin/cp2kview"))
# 数据集按模块独立落盘：ML-XXX.csv / AL-XXX.csv。
# 此前 ML 与 AL 的导入都写进同一张 ml_samples 表，训练时全表取数导致两类数据混用；
# 落盘为带模块前缀的独立文件后，训练任务可显式选择本模块的数据集。
DATASETS_DIR = os.path.abspath(os.path.expanduser(
    (os.environ.get("CATMAT_DATASETS_DIR") or os.path.join(DATA_DIR, "datasets")).strip()))
os.makedirs(DATASETS_DIR, exist_ok=True)
LLM_CONFIG_PATH = os.path.join(BASE_DIR, "llm_config.json")
TMP_DIR = os.path.abspath(os.path.expanduser(
    (os.environ.get("CATMAT_TMP_DIR") or os.path.join(BASE_DIR, ".tmp")).strip()))
os.makedirs(TMP_DIR, exist_ok=True)

# --------------------------------------------------------------------------
# CP2K 真实任务提交（参照 /home/user/sh/run.sh 的提交脚本逻辑）
#   for inf in *.inp; do nohup mpirun -np 32 -x OMP_NUM_THREADS=1 cp2k.psmp -i ${inf} -o ${inf//inp/out} >/dev/null &
# --------------------------------------------------------------------------
_CP2K_ROOT = os.environ.get("CATMAT_CP2K_ROOT") or os.path.expanduser("~/bin/cp2k-2026.2")
CP2K_PSMP = os.environ.get("CATMAT_CP2K_PSMP") or os.path.join(_CP2K_ROOT, "install", "bin", "cp2k.psmp")
CP2K_LIB = os.environ.get("CATMAT_CP2K_LIB") or os.path.join(_CP2K_ROOT, "install", "lib")
CP2K_ENV = os.environ.get("CATMAT_CP2K_ENV") or os.path.join(_CP2K_ROOT, "install", "cp2k_env")
CP2K_DATA_DIR = os.environ.get("CATMAT_CP2K_DATA_DIR") or os.path.join(_CP2K_ROOT, "data")
# MPI 随 CP2K 编译时一并装好（toolchain 的 OpenMPI），不单独指定路径：
# 只需 source cp2k_env，mpirun / cp2k.psmp 与依赖库即全部就位。
# 这样 mpirun 是 OpenMPI（支持 -x OMP_NUM_THREADS=...）；
# 此前硬编码 dpmd 环境的 MPICH，因不支持 -x 导致提交直接失败。
CP2K_RUN_DIR = os.path.abspath(os.path.expanduser(os.environ.get("CATMAT_CP2K_RUN_DIR") or os.path.join(BASE_DIR, "cp2k_runs")))   # 独立 CP2K 任务工作目录
os.makedirs(CP2K_RUN_DIR, exist_ok=True)
VERDI = os.environ.get("CATMAT_VERDI") or os.path.expanduser("~/anaconda3/envs/aiida/bin/verdi")
SUBMIT_VERDI_SCRIPT = os.path.join(TOOLS_DIR, "submit_cp2k_verdi.py")
# 高通量批量链式工作流提交脚本（经 verdi run 提交 CatMat 工作链 catmat.workflow / catmat.batch）
SUBMIT_CATMAT_SCRIPT = os.path.join(TOOLS_DIR, "submit_catmat_workflow.py")
# 读取已完成工作链 results 输出（逐结构能量等）脚本（verdi run 调用）
LOAD_WORKFLOW_RESULTS_SCRIPT = os.path.join(TOOLS_DIR, "load_workflow_results.py")
# 高通量批量结构 → AiiDA StructureData 库存储脚本（verdi python 调用）
STORE_STRUCTURES_SCRIPT = os.path.join(TOOLS_DIR, "store_structures.py")
# 差分电荷密度后处理脚本（delta = total - slab - ads，逐格点相减）
BADER_SCRIPT = os.path.join(TOOLS_DIR, "bader_charge.py")
# 从 AiiDA 库读取 StructureData → JSON（PK 汇总 → 重新导入 ASE 做位点分析）
LOAD_STRUCTURE_SCRIPT = os.path.join(TOOLS_DIR, "load_structure.py")
# AiiDA 任务（PK）路径检索与文件读取（供 AI 助手使用）
AIIDA_TASK_FILES_SCRIPT = os.path.join(TOOLS_DIR, "aiida_task_files.py")

_PHYSICAL_CORE_COUNT = None
_PHYSICAL_CORE_COUNT_RELIABLE = False


def _physical_core_count():
    """返回物理核数（非逻辑核；SMT 下 os.cpu_count() 通常是它的两倍）。

    优先按 /proc/cpuinfo 的 (physical id, core id) 唯一组合计数。
    字段缺失或解析失败时回退到 os.cpu_count()，再失败则回退到 1。
    结果保存在模块级变量中，每个进程只探测一次。
    """
    global _PHYSICAL_CORE_COUNT, _PHYSICAL_CORE_COUNT_RELIABLE
    if _PHYSICAL_CORE_COUNT is not None:
        return _PHYSICAL_CORE_COUNT

    try:
        pairs = set()
        processor_records = 0
        identified_records = 0
        fields = {}

        def finish_record():
            nonlocal processor_records, identified_records
            if "processor" not in fields:
                return
            processor_records += 1
            physical_id = fields.get("physical id")
            core_id = fields.get("core id")
            if physical_id not in (None, "") and core_id not in (None, ""):
                pairs.add((physical_id, core_id))
                identified_records += 1

        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    finish_record()
                    fields = {}
                    continue
                key, sep, value = line.partition(":")
                if sep:
                    fields[key.strip()] = value.strip()
        finish_record()

        # 所有逻辑处理器记录都必须有拓扑字段，否则该结果不可靠。
        if pairs and processor_records > 0 and identified_records == processor_records:
            _PHYSICAL_CORE_COUNT = max(1, len(pairs))
            _PHYSICAL_CORE_COUNT_RELIABLE = True
            return _PHYSICAL_CORE_COUNT
    except Exception:
        pass

    try:
        _PHYSICAL_CORE_COUNT = max(1, int(os.cpu_count() or 1))
    except Exception:
        _PHYSICAL_CORE_COUNT = 1
    _PHYSICAL_CORE_COUNT_RELIABLE = False
    return _PHYSICAL_CORE_COUNT


def _derive_resource_defaults(physical_cores, reliable):
    """由物理核数与探测可靠性推导资源默认值。

    返回 ``(default_nproc, queue_max_total_nproc)``。不是正整数的
    ``physical_cores`` 按 1 处理；``reliable`` 按真值判断。
    纯函数：不读环境变量、不读文件、不读全局状态。
    """
    if (not isinstance(physical_cores, int)
            or isinstance(physical_cores, bool)
            or physical_cores <= 0):
        physical_cores = 1
    if reliable:
        return (min(32, max(1, physical_cores // 6)),
                physical_cores // 2)
    return 8, 16


_RESOURCE_PHYSICAL_CORES = _physical_core_count()
# 独立 CP2K 任务默认 MPI 进程数：可靠时取 clamp(P // 6, 1, 32)；
# 本机 P=192 时为 32，与已验证安全的单点能基线一致。探测不可靠时保守取 8。
_DEFAULT_NPROC, _DEFAULT_QUEUE_MAX_TOTAL_NPROC = _derive_resource_defaults(
    _RESOURCE_PHYSICAL_CORES, _PHYSICAL_CORE_COUNT_RELIABLE)
DEFAULT_NPROC = _env_int("CATMAT_DEFAULT_NPROC", _DEFAULT_NPROC)
DEFAULT_OMP_THREADS = 1
# 已提交的进程对象注册表（pid → Popen），用于停止任务时回收，避免僵尸堆积
SUBMITTED_PROCS = {}



def aiida_dir_for_uuid(uuid):
    """由 AiiDA 节点 uuid 定位输出目录：aiida_files/{uuid[:2]}/{uuid[2:4]}/{uuid[4:]}"""
    u = uuid.lower()
    return os.path.join(AIIDA_FILES, u[:2], u[2:4], u[4:])


# --------------------------------------------------------------------------
# 版本探测（缓存 60s）+ 资料库同步 + DOI + ML 训练 支持
# --------------------------------------------------------------------------
WORKSPACE = os.path.dirname(BASE_DIR)          # /home/user/bin/Agent
ML_TRAIN = os.path.join(TOOLS_DIR, "ml_train.py")
ML_COMPARE = os.path.join(TOOLS_DIR, "ml_compare.py")
AL_TRAIN = os.path.join(TOOLS_DIR, "al_train.py")
AL_MULTIDIM = os.path.join(TOOLS_DIR, "al_multidim.py")
CHART_GEN = os.path.join(TOOLS_DIR, "chart_gen.py")
AIIDA_NODE = os.path.join(TOOLS_DIR, "aiida_node.py")


def _run_ml_train(params):
    """Legacy callers now receive a tracked task, not an ungoverned sync process."""
    cfg = dict(params or {})
    tid, err = _start_ml_task(cfg.get("name"), cfg.get("model"), cfg.get("device"), cfg)
    return ({"task_id": tid, "status": "running", "async": True}, None) if not err else (None, err)


def _append_ml_task_log(tid, progress, msg):
    """训练任务：更新进度并追加日志（线程内独立连接）。"""
    return _append_task_log("ml_tasks", tid, progress, msg)


def _append_task_log(table, tid, progress, msg):
    """通用：向训练/主动学习任务追加日志并更新进度（线程内独立连接）。

    进度单调不减：模型内部迭代的中间输出（PROGRESS=0）不会把已推进的进度拉回。"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT log, progress FROM %s WHERE id=?" % table, (tid,)).fetchone()
        if not row:
            conn.close()
            return
        log = json.loads(row["log"] or "[]") if row else []
        if msg:
            log.append({"ts": now_str(), "msg": msg})
        prog = max(int(progress or 0), int(row["progress"] or 0))
        conn.execute("UPDATE %s SET progress=?, log=? WHERE id=?" % table,
                     (prog, json.dumps(log[-300:], ensure_ascii=False), tid))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _finish_ml_task(tid, rc, result_line):
    """训练任务收尾：解析结果并落库（带重试，避免与主请求线程锁竞争导致丢失结果）。"""
    return _finish_task("ml_tasks", tid, rc, result_line)


def _finish_task(table, tid, rc, result_line):
    """通用：任务收尾（带重试，避免与主请求线程锁竞争导致丢失结果）。"""
    for attempt in range(4):
        if _finish_task_once(table, tid, rc, result_line):
            return
        time.sleep(0.4 * (attempt + 1))
    try:
        conn = sqlite3.connect(DB_PATH, timeout=15)
        conn.execute("UPDATE %s SET progress=100, finished_at=? WHERE id=? AND finished_at IS NULL" % table,
                     (now_str(), tid))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _finish_task_once(table, tid, rc, result_line):
    """单次收尾写入，成功返回 True。"""
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=15)
        conn.row_factory = sqlite3.Row
        status, result, error, prog = "failed", None, None, None
        if result_line:
            try:
                j = json.loads(result_line)
                if j.get("ok") and rc == 0:
                    status = "done"
                    prog = 100
                    result = json.dumps(j.get("result"), ensure_ascii=False)
                else:
                    status = "failed"
                    error = j.get("error") or "进程异常退出（退出码 %s）" % rc
            except Exception:
                status, error = "failed", "结果解析失败"
        else:
            status = "cancelled" if (rc is not None and rc < 0) else "failed"
            error = ("训练进程被终止（退出码 %s）" % rc) if (rc is not None and rc < 0) \
                else ("训练进程退出码 %s，未输出结果" % rc)
        row = conn.execute("SELECT status FROM ml_tasks WHERE id=?", (tid,)).fetchone()
        if not row:
            return True
        if row["status"] != "cancelled":  # 已被用户终止则保留 cancelled
            conn.execute("UPDATE ml_tasks SET status=?, result=?, error=?, progress=?, finished_at=? WHERE id=?",
                         (status, result, error, prog, now_str(), tid))
        else:
            conn.execute("UPDATE ml_tasks SET finished_at=? WHERE id=?", (now_str(), tid))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


_ML_START_LOCK = threading.Lock()
_ML_READERS = set()


def _limited_training_start(func):
    """Serialize admission + PID persistence across all async training modules."""
    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        # Always release HTTP lock before waiting for admission lock.
        with _blocking_unlocked():
            with _ML_START_LOCK:
                with contextlib.closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
                    rows = conn.execute("SELECT pid FROM ml_tasks WHERE status='running'").fetchall()
                active = sum(1 for (pid,) in rows if pid and proc_alive(pid))
                limit = max(1, _env_int("CATMAT_ML_MAX_RUNNING", 2))
                if active >= limit:
                    return None, "后台训练并发已满（%d/%d），请等待运行任务完成后重试" % (active, limit)
                return func(*args, **kwargs)
    return wrapped


def _training_env(cfg):
    """Bound estimator and BLAS parallelism before creating any worker."""
    limit = max(1, min(16, _env_int("CATMAT_ML_THREADS", 4)))
    def normalize(value):
        if isinstance(value, dict):
            for key in list(value):
                if key in ("n_jobs", "nthread", "num_threads"):
                    try:
                        n = int(value[key])
                    except (TypeError, ValueError):
                        n = limit
                    value[key] = min(n, limit) if n > 0 else limit
                else:
                    normalize(value[key])
        elif isinstance(value, list):
            for item in value:
                normalize(item)
    normalize(cfg)
    cfg.setdefault("n_jobs", limit)
    env = os.environ.copy()
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS", "BLIS_NUM_THREADS", "LOKY_MAX_CPU_COUNT"):
        env[key] = str(limit)
    return env


@_limited_training_start
def _start_task(task_type, name, model, device, params, owner_id=None):
    """异步启动后台任务（ML 训练 / 主动学习），后台线程读进度。返回 (task_id, err)。

    owner_id：发起账号（东翁/掌书/清客）；None（AI/系统触发）归东翁系统通道。

    params.dataset_file：显式选择的数据集文件（仅文件名，按模块独立保存的
    ML-XXX.csv / AL-XXX.csv）。此处解析为受管目录下的绝对路径并校验模块前缀，
    既避免路径穿越，也防止 ML 训练误用 AL 数据集（反之亦然）。
    """
    script = AL_TRAIN if task_type == "al" else ML_TRAIN
    cfg = dict(params or {})
    # 数据集文件：文件名 → 受管目录绝对路径 + 模块前缀校验
    _dsf = cfg.get("dataset_file")
    if _dsf:
        _mod = cfg.get("dataset_module") or task_type
        _full = os.path.join(DATASETS_DIR, os.path.basename(str(_dsf)))
        _rows, _derr = load_dataset_file(_full, module=_mod)
        if _derr:
            return None, "数据集不可用：%s" % _derr
        if not _rows:
            return None, "数据集文件为空或无有效样本：%s" % os.path.basename(str(_dsf))
        cfg["dataset_file"] = _full
    if task_type == "ml":
        cfg["model"] = model or cfg.get("model") or "rf"
    cfg["device"] = device or "auto"
    token = time.strftime("%Y%m%d%H%M%S") + "_" + str(os.getpid()) + "_" + str(random.randint(1000, 9999))
    cfg_path = os.path.join(TMP_DIR, ("alrun_" if task_type == "al" else "mltrain_") + token + ".json")
    if task_type == "ml":
        if cfg.get("params") is None:
            cfg["params"] = {}
        if not isinstance(cfg["params"], dict):
            return None, "模型 params 必须为对象"
        cfg["params"].setdefault("n_jobs", max(1, min(16, _env_int("CATMAT_ML_THREADS", 4))))
    task_env = _training_env(cfg)
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        with _blocking_unlocked():
            proc = subprocess.Popen([ML_PY, script, DB_PATH, cfg_path],
                                    env=task_env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1)
    except Exception as e:
        return None, "启动任务失败: %s" % e

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    owner_id = owner_id or _system_owner_id(conn)
    cur = conn.execute(
        "INSERT INTO ml_tasks(task_type,name,model,device,status,pid,progress,log,params,owner_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (task_type, name or (("al_" + str(cfg.get("method", "al"))) if task_type == "al"
                             else "train_" + str(cfg["model"])),
         str(cfg.get("model", cfg.get("method", task_type))), cfg["device"], "running",
         proc.pid, 0,
         json.dumps([{"ts": now_str(), "msg": "任务已启动（PID %s，设备 %s）"
                      % (proc.pid, cfg["device"])}], ensure_ascii=False),
         json.dumps(cfg, ensure_ascii=False), owner_id))
    tid = cur.lastrowid
    conn.commit()
    conn.close()

    _ML_READERS.add(tid)

    def _reader():
        last_json = None
        try:
            for line in proc.stdout:
                line = (line or "").rstrip()
                if not line:
                    continue
                if line.startswith("PROGRESS="):
                    rest = line[len("PROGRESS="):]
                    parts = rest.split(" ", 1)
                    try:
                        prog = int(parts[0])
                    except ValueError:
                        prog = 0
                    _append_task_log("ml_tasks", tid, prog, parts[1] if len(parts) > 1 else "")
                elif line.lstrip().startswith("{"):
                    last_json = line.strip()
        except Exception:
            pass
        try:
            rc = proc.wait(timeout=30)
        except Exception:
            rc = None
        _finish_task("ml_tasks", tid, rc, last_json)
        _ML_READERS.discard(tid)
        try:
            os.remove(cfg_path)
        except Exception:
            pass

    threading.Thread(target=_reader, daemon=True).start()
    return tid, None


def _start_ml_task(name, model, device, params, owner_id=None):
    """兼容包装：机器学习训练任务。"""
    return _start_task("ml", name, model, device, params, owner_id=owner_id)


# ---------------------------------------------------------------------------
# 多模型集成对比训练（ml_compare.py）
#
# 动机：单模型接口的三个指标在本类小样本数据上口径都有问题 ——
#   · r2/mae 含训练点（乐观偏置可达 6×）
#   · cv_r2_mean 用**不打乱** KFold，按分组有序的数据上会退化成"整段外推"
#   · CPU 与 GPU 并行路径的 KFold 参数不一致（同一模型 CV 可差 5 倍）
# 对比任务一次性对多个模型跑同一套严谨协议（打乱 RepeatedKFold 主指标 +
# 可选分组 OOF + 预注册切分 + 朴素基线），结果直接可排序。
# ---------------------------------------------------------------------------
COMPARE_DEFAULT_MODELS = [
    "linear", "ridge", "lasso", "elasticnet", "bayesian_ridge", "huber",
    "svr", "kernel_ridge", "gpr", "knn", "pls",
    "decision_tree", "rf", "extra_trees", "gradient_boosting",
    "hist_gradient_boosting", "adaboost", "xgb", "lgbm",
    "mlp", "mlp_torch", "gpr_torch",
]


def _cmp_brief(row, with_result=False):
    """把 mlcmp 任务行压成简短摘要（列表用）；with_result=True 时附排名与 SHAP 前 15。"""
    keys = row.keys()
    d = {"id": row["id"], "name": row["name"], "status": row["status"],
         "progress": row["progress"], "created_at": row["created_at"],
         "finished_at": row["finished_at"] if "finished_at" in keys else None,
         "error": row["error"] if "error" in keys else None}
    res = row["result"] if "result" in keys else None
    if isinstance(res, str):
        try:
            res = json.loads(res)
        except Exception:
            res = None
    if isinstance(res, dict):
        d.update({
            "dataset": res.get("dataset"), "n_samples": res.get("n_samples"),
            "n_features": res.get("n_features"), "n_models": res.get("n_models"),
            "best_model": res.get("best_model"), "best_cv_r2": res.get("best_cv_r2"),
            "best_cv_mae": res.get("best_cv_mae"), "baseline": res.get("baseline"),
        })
        if with_result:
            d["protocol"] = res.get("protocol")
            d["ranking"] = res.get("ranking")
            d["shap_top15"] = (res.get("shap_aggregate") or [])[:15]
            d["full_result_path"] = res.get("full_result_path")
            d["note"] = res.get("note")
    return d


@_limited_training_start
def _start_compare_task(name, params, owner_id=None):
    """启动多模型对比训练（后台任务，task_type='mlcmp'）。返回 (task_id, err)。

    params 关键字段：
      dataset_file / dataset_module  显式数据集（建议带 parent_id、fixed_split 列）
      models                         None/"all" = 全部默认池；或模型名数组
      cv_folds(5) / cv_repeats(10)   打乱 RepeatedKFold 主指标
      group_col                     如 "parent_id" → 追加 LeaveOneGroupOut OOF
      fixed_split_col               如 "fixed_split" → 追加预注册切分评估
      preprocess(true) / test_size(0.2) / n_jobs(4) / shap(true)
      params                        每模型超参覆盖 {model: {...}}
    """
    cfg = dict(params or {})
    models = cfg.get("models")
    if isinstance(models, str):
        models = None if models.lower() in ("all", "") else \
            [m.strip() for m in models.split(",") if m.strip()]
    if not models:
        models = list(COMPARE_DEFAULT_MODELS)
    unknown = [m for m in models if m not in COMPARE_DEFAULT_MODELS]
    if unknown:
        return None, "未知模型：%s（可选：%s）" % (", ".join(unknown[:5]),
                                                 ", ".join(COMPARE_DEFAULT_MODELS))
    cfg["models"] = models

    # 数据集：文件名 → 受管目录绝对路径 + 模块前缀校验（与单模型训练同规则）
    _dsf = cfg.get("dataset_file")
    if _dsf:
        _mod = cfg.get("dataset_module") or "ml"
        _full = os.path.join(DATASETS_DIR, os.path.basename(str(_dsf)))
        _rows, _derr = load_dataset_file(_full, module=_mod)
        if _derr:
            return None, "数据集不可用：%s" % _derr
        if not _rows:
            return None, "数据集文件为空或无有效样本：%s" % os.path.basename(str(_dsf))
        if len(_rows) < 10:
            return None, "样本不足（%d），多模型对比至少需要 10 条" % len(_rows)
        cfg["dataset_file"] = _full
        with open(_full, encoding="utf-8-sig", newline="") as fh:
            headers = next(csv.reader(fh), [])
        for key in ("group_col", "fixed_split_col"):
            if cfg.get(key) and cfg[key] not in headers:
                return None, "数据集缺少指定列 %s=%s，不能静默省略该验证协议" % (key, cfg[key])
    else:
        conn0 = sqlite3.connect(DB_PATH, timeout=10)
        n0 = conn0.execute("SELECT COUNT(*) FROM ml_samples WHERE target IS NOT NULL").fetchone()[0]
        conn0.close()
        if n0 < 10:
            return None, "ml_samples 中可用样本不足（%d）" % n0

    cfg.setdefault("preprocess", True)
    cfg.setdefault("test_size", 0.2)
    cfg.setdefault("cv_folds", 5)
    cfg.setdefault("cv_repeats", 10)
    cfg.setdefault("n_jobs", 4)          # 384 核机器上 n_jobs=-1 会过度订阅
    cfg.setdefault("device", "cpu")      # 统一 CPU：各模型数值路径可比
    cfg["dataset_module"] = cfg.get("dataset_module") or "ml"

    stamp = time.strftime("%Y%m%d%H%M%S") + "_" + str(random.randint(1000, 9999))
    cfg_path = os.path.join(TMP_DIR, "mlcmp_" + stamp + ".json")
    res_dir = os.path.join(TMP_DIR, "mlcmp_results")
    os.makedirs(res_dir, exist_ok=True)
    result_path = os.path.join(res_dir, "compare_%s.json" % stamp)
    cfg["output"] = result_path
    task_env = _training_env(cfg)
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        with _blocking_unlocked():
            proc = subprocess.Popen([ML_PY, ML_COMPARE, DB_PATH, cfg_path],
                                    env=task_env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1)
    except Exception as e:
        return None, "启动对比训练失败: %s" % e

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    owner_id = owner_id or _system_owner_id(conn)
    label = name or ("compare_%dmodels" % len(models))
    cur = conn.execute(
        "INSERT INTO ml_tasks(task_type,name,model,device,status,pid,progress,log,params,owner_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("mlcmp", label, "compare", cfg["device"], "running", proc.pid, 0,
         json.dumps([{"ts": now_str(), "msg": "多模型对比已启动（PID %s，%d 个模型）"
                      % (proc.pid, len(models))}], ensure_ascii=False),
         json.dumps(cfg, ensure_ascii=False), owner_id))
    tid = cur.lastrowid
    conn.commit()
    conn.close()

    _ML_READERS.add(tid)

    def _reader():
        last_json = None
        try:
            for line in proc.stdout:
                line = (line or "").rstrip()
                if not line:
                    continue
                if line.startswith("PROGRESS="):
                    rest = line[len("PROGRESS="):]
                    parts = rest.split(" ", 1)
                    try:
                        prog = int(parts[0])
                    except ValueError:
                        prog = 0
                    _append_task_log("ml_tasks", tid, prog, parts[1] if len(parts) > 1 else "")
                elif line.lstrip().startswith("{"):
                    last_json = line.strip()
        except Exception:
            pass
        try:
            rc = proc.wait(timeout=60)
        except Exception:
            rc = None
        _finish_task("ml_tasks", tid, rc, last_json)
        _ML_READERS.discard(tid)
        for p in (cfg_path,):
            try:
                os.remove(p)
            except Exception:
                pass

    threading.Thread(target=_reader, daemon=True).start()
    return tid, None


@_limited_training_start
def _start_al_multidim(name, params, owner_id=None):
    """启动**多描述符**主动学习（后台任务，task_type='almd'）。返回 (task_id, err)。

    与内置 1 维 AL（`al_train.py`：ε_d → η_OER，取 systems 表）的区别：
    本任务从**数据集文件**读任意多列特征 + 任意目标，用 GPR 代理 + 分组标定噪声 +
    UCB/EI/PI 采集；无候选文件时做"外推母体"AL 模拟（AL vs 同预算随机）。

    params 关键字段：
      dataset_file(s) / dataset_module  训练数据集（建议带分组列）
      group_col          分组列（如 parent_id）→ 分组 OOF 与 AL 模拟用它
      exclude_cols       排除出特征矩阵的元数据列（**seed/composition 一定要排**，
                         否则数值型批次变量会被当特征，随机 CV 虚高）
      target(缺省 target)  method(gpr_ucb/gpr_ei/gpr_pi)  kappa  xi  minimize
      batch_size  noise_floor  simulate  sim_rounds  sim_random_repeats
      candidate_file     无标签候选集（给了就直接出推荐列表）
    """
    cfg = dict(params or {})
    _dsf = cfg.get("dataset_file")
    if not _dsf:
        return None, "缺少 dataset_file（多描述符主动学习需要显式数据集）"
    _mod = cfg.get("dataset_module") or "ml"
    _full = os.path.join(DATASETS_DIR, os.path.basename(str(_dsf)))
    if not os.path.isfile(_full):
        return None, "数据集文件不存在：%s" % os.path.basename(str(_dsf))
    with open(_full, encoding="utf-8-sig", newline="") as fh:
        headers = next(csv.reader(fh), [])
    for key, column in (("target", cfg.get("target") or "target"), ("group_col", cfg.get("group_col"))):
        if column and column not in headers:
            return None, "数据集缺少指定列 %s=%s" % (key, column)
    cfg["dataset_file"] = _full
    cfg["dataset_module"] = _mod
    method = str(cfg.get("method") or "gpr_ucb")
    if method not in ("gpr_ucb", "gpr_ei", "gpr_pi"):
        return None, "不支持的采集方法：%s（可选 gpr_ucb / gpr_ei / gpr_pi）" % method
    cfg.setdefault("kappa", 2.0)
    cfg.setdefault("xi", 0.01)
    cfg.setdefault("minimize", True)
    cfg.setdefault("batch_size", 10)
    cfg.setdefault("simulate", True)
    cfg.setdefault("n_jobs", 4)
    if cfg.get("candidate_file"):
        cfg["candidate_file"] = os.path.join(DATASETS_DIR,
                                             os.path.basename(str(cfg["candidate_file"])))

    stamp = time.strftime("%Y%m%d%H%M%S") + "_" + str(random.randint(1000, 9999))
    cfg_path = os.path.join(TMP_DIR, "almd_" + stamp + ".json")
    res_dir = os.path.join(TMP_DIR, "almd_results")
    os.makedirs(res_dir, exist_ok=True)
    cfg["output"] = os.path.join(res_dir, "al_%s.json" % stamp)
    task_env = _training_env(cfg)
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        with _blocking_unlocked():
            proc = subprocess.Popen([ML_PY, AL_MULTIDIM, DB_PATH, cfg_path],
                                    env=task_env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1)
    except Exception as e:
        return None, "启动多描述符主动学习失败: %s" % e

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    owner_id = owner_id or _system_owner_id(conn)
    label = name or ("agent_AL多维_%s" % method)
    cur = conn.execute(
        "INSERT INTO ml_tasks(task_type,name,model,device,status,pid,progress,log,params,owner_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("almd", label, method, "cpu", "running", proc.pid, 0,
         json.dumps([{"ts": now_str(), "msg": "多描述符主动学习已启动（PID %s，%s）"
                      % (proc.pid, method)}], ensure_ascii=False),
         json.dumps(cfg, ensure_ascii=False), owner_id))
    tid = cur.lastrowid
    conn.commit()
    conn.close()

    _ML_READERS.add(tid)

    def _reader():
        last_json = None
        try:
            for line in proc.stdout:
                line = (line or "").rstrip()
                if not line:
                    continue
                if line.startswith("PROGRESS="):
                    rest = line[len("PROGRESS="):]
                    parts = rest.split(" ", 1)
                    try:
                        prog = int(parts[0])
                    except ValueError:
                        prog = 0
                    _append_task_log("ml_tasks", tid, prog, parts[1] if len(parts) > 1 else "")
                elif line.lstrip().startswith("{"):
                    last_json = line.strip()
        except Exception:
            pass
        try:
            rc = proc.wait(timeout=60)
        except Exception:
            rc = None
        _finish_task("ml_tasks", tid, rc, last_json)
        _ML_READERS.discard(tid)
        try:
            os.remove(cfg_path)
        except Exception:
            pass

    threading.Thread(target=_reader, daemon=True).start()
    return tid, None


def _al_brief(row, with_result=False):
    """把 almd 任务行压成简短摘要。"""
    keys = row.keys()
    d = {"id": row["id"], "name": row["name"], "status": row["status"],
         "progress": row["progress"], "created_at": row["created_at"],
         "finished_at": row["finished_at"] if "finished_at" in keys else None,
         "error": row["error"] if "error" in keys else None}
    res = row["result"] if "result" in keys else None
    if isinstance(res, str):
        try:
            res = json.loads(res)
        except Exception:
            res = None
    if isinstance(res, dict):
        diag = res.get("diagnostics") or {}
        sim = res.get("al_simulation") or {}
        d.update({
            "dataset": res.get("dataset"), "n_samples": res.get("n_samples"),
            "n_features": res.get("n_features"), "n_groups": res.get("n_groups"),
            "method": res.get("method"), "minimize": res.get("minimize"),
            "grouped_oof": diag.get("grouped_oof"),
            "baseline": diag.get("baseline_predict_train_mean"),
            "noise_floor": diag.get("noise_floor"),
            "al_gain_vs_random_final": sim.get("al_gain_vs_random_final"),
        })
        if with_result:
            d["diagnostics"] = diag
            d["permutation_importance"] = (res.get("permutation_importance") or [])[:20]
            d["al_simulation"] = sim
            d["recommendations"] = res.get("recommendations")
            d["n_candidates"] = res.get("n_candidates")
    return d


# ---------------------------------------------------------------------------
# 势函数延拓（DPGEN 主动学习闭环）编排
# ---------------------------------------------------------------------------
def _start_dpgen_run(name, cfg, owner_id=None):
    """启动势函数延拓（DPGEN）后台编排线程，返回 (run_id, err)。

    配置先经 dpgen_adapter.validate_dpgen_config 校验，避免无效任务进入队列。
    owner_id：发起账号（东翁/掌书/清客）；None（AI/系统触发）归东翁系统通道。
    """
    from catmat_tools import dpgen_adapter as dpgen_ad
    ok, errs = dpgen_ad.validate_dpgen_config(cfg or {})
    if not ok:
        return None, "配置校验失败：" + "；".join(errs)
    # The current adapter has no trajectory -> real frame -> completed labels
    # -> training data handshake. Never submit placeholder atoms as real work.
    if os.environ.get("CATMAT_DPGEN_MOCK") != "1":
        return None, "DPGEN 生产闭环尚未接通真实轨迹帧、标注验收与数据回流；当前仅支持配置和历史结果查看，禁止占位结构提交。"
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    owner_id = owner_id or _system_owner_id(conn)
    cur = conn.execute(
        "INSERT INTO dpgen_runs(name,status,current_iter,max_iter,trust_lo,trust_hi,stride,"
        "params,seed_info,owner_id,progress,log) VALUES(?,?,0,?,?,?,?,?,?,?,0,?)",
        (name or ("dpgen_" + str(cfg.get("max_iter"))), "running",
         int(cfg.get("max_iter", 10)), float(cfg.get("trust_lo", dpgen_ad.DEFAULT_TRUST_LO)),
         float(cfg.get("trust_hi", dpgen_ad.DEFAULT_TRUST_HI)), int(cfg.get("stride", 1)),
         json.dumps(cfg, ensure_ascii=False),
         json.dumps(cfg.get("seed_info") or cfg.get("seed") or {}, ensure_ascii=False),
         owner_id,
         json.dumps([{"ts": now_str(), "msg": "势函数延拓任务已创建，启动闭环编排线程"}], ensure_ascii=False)))
    run_id = cur.lastrowid
    conn.commit(); conn.close()
    threading.Thread(target=_dpgen_orchestrate, args=(run_id, cfg, owner_id), daemon=True).start()
    return run_id, None


def _prepare_dpgen_workspace(run_id, cfg):
    """为真实执行准备 workdir 与最小 DP-GEN param.json / machine.json（Local 执行器）。"""
    workdir = os.path.join(TMP_DIR, "dpgen_%d" % run_id)
    os.makedirs(workdir, exist_ok=True)
    from catmat_tools import dpgen_adapter as dpgen_ad
    tp = dict(dpgen_ad.DEFAULT_TRAINING_PARAMS)
    tp.update(cfg.get("training_params") or {})
    param = {
        "type_map": cfg.get("type_map") or [],
        "mass_map": cfg.get("mass_map") or [],
        "init_data_sys": cfg.get("seed") or [],
        "sys_configs": cfg.get("sys_configs") or [],
        "numb_models": 4,
        "train_param": {
            "model": {"descriptor": tp["descriptor"],
                      "fitting_net": {"neuron": tp["descriptor"]["neuron"], "resnet_dt": True}},
            "learning_rate": {"type": "exp", "start_lr": 1e-3, "decay_steps": 2000},
            "loss": {"type": "ener", "start_pref_e": 0.02, "start_pref_f": 1000, "start_pref_v": 0},
            "training": tp["training"],
        },
        "model_devi": cfg.get("model_devi") or {},
        "fp": cfg.get("fp") or {"type": "cp2k"},
    }
    with open(os.path.join(workdir, "param.json"), "w", encoding="utf-8") as f:
        json.dump(param, f, ensure_ascii=False, indent=2)
    machine = {
        "train": [{"machine": {"batch_type": "Shell", "local_root": workdir, "clean_at_exit": False},
                   "resources": {"task_num": 1, "numb_node": 1, "numb_gpu": 1, "gpu_usage": False}}],
        "model_devi": [{"machine": {"batch_type": "Shell", "local_root": workdir, "clean_at_exit": False},
                        "resources": {"task_num": 1, "numb_node": 1, "numb_gpu": 1, "gpu_usage": True}}],
        "fp": [{"machine": {"batch_type": "Shell", "local_root": workdir, "clean_at_exit": False},
                "resources": {"task_num": 1, "numb_node": 1, "numb_gpu": 0, "gpu_usage": False}}],
    }
    with open(os.path.join(workdir, "machine.json"), "w", encoding="utf-8") as f:
        json.dump(machine, f, ensure_ascii=False, indent=2)
    return workdir, param, machine


def _dpgen_orchestrate(run_id, cfg, owner_id):
    """势函数延拓闭环编排线程：训练→探索→筛选→CP2K标注→扩充→评估，复用既有 CP2K 队列与进度协议。

    真实执行（CATMAT_DPGEN_MOCK≠1）调用 dpmd 环境的 dpgen/dp/lmp；
    测试/MOCK 模式（CATMAT_DPGEN_MOCK=1）返回内嵌样本数据，仅真实验证编排与 CP2K 通道调用。
    """
    from catmat_tools import dpgen_adapter as dpgen_ad
    use_mock = os.environ.get("CATMAT_DPGEN_MOCK") == "1"
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute("SELECT * FROM dpgen_runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            return
        run_state = {"iter_no": 0, "max_iter": run["max_iter"], "trust_lo": run["trust_lo"],
                    "trust_hi": run["trust_hi"], "stride": run["stride"], "lcurve_history": []}

        def _log(msg):
            _append_task_log("dpgen_runs", run_id, 0, msg)

        def ex_train(state):
            if use_mock:
                # 模拟 DeePMD/DP-GEN lcurve.out：表头 + 7 列，列序与
                # dpgen_adapter._LCURVE_KEYS 一致，确保 parse_lcurve 能正确解析出
                # rmse_e_train / rmse_f_train 等指标（与真实训练产物一致）。
                return ("# step rmse_val_e rmse_val_f rmse_test_e rmse_test_f "
                        "rmse_e_train rmse_f_train\n"
                        "1000 0.085 0.0042 0.085 0.0042 0.080 0.0040\n"
                        "2000 0.048 0.0016 0.048 0.0016 0.045 0.0015\n"
                        "4000 0.045 0.0015 0.045 0.0015 0.042 0.0014")
            workdir, param, machine = _prepare_dpgen_workspace(run_id, cfg)
            ok, out, err = dpgen_ad.run_dp_train(workdir,
                                                os.path.join(workdir, "param.json"),
                                                os.path.join(workdir, "machine.json"))
            if not ok:
                raise RuntimeError("DP 训练失败：" + (err or "")[:800])
            lc = os.path.join(workdir, "lcurve.out")
            return open(lc, encoding="utf-8").read() if os.path.exists(lc) else out

        def ex_explore(graphs, iter_no):
            if use_mock:
                return ("1 0.01 0.02 0.03\n2 0.10 0.12 0.11\n3 0.08 0.07 0.06\n"
                        "4 0.13 0.11 0.12\n5 0.04 0.03 0.05")
            workdir, _, _ = _prepare_dpgen_workspace(run_id, cfg)
            ok, out, err = dpgen_ad.run_lammps_model_devi(
                workdir, cfg.get("lammps_input", "input.lammps"), graphs)
            if not ok:
                raise RuntimeError("LAMMPS model_devi 失败：" + (err or "")[:800])
            md = os.path.join(workdir, "model_devi.out")
            return open(md, encoding="utf-8").read() if os.path.exists(md) else out

        def ex_label(candidates, iter_no):
            if use_mock:
                _log("模拟标注 %d 帧；不创建真实计算队列" % len(candidates))
                return {"queue_ids": [], "simulation": True}
            raise RuntimeError("缺少真实轨迹帧与标注回流，已阻止提交")

        while True:
            # 每轮边界检查：用户已终止/取消则优雅退出
            cur = conn.execute("SELECT status FROM dpgen_runs WHERE id=?", (run_id,)).fetchone()
            if not cur or cur["status"] != "running":
                _log("收到终止/取消信号，编排线程退出")
                break
            iter_no = run_state["iter_no"] + 1
            icur = conn.execute(
                "INSERT INTO dpgen_iterations(run_id,iter_no,phase,status,progress,log) "
                "VALUES(?,?,?,?,?,?)",
                (run_id, iter_no, "running", "running", 0,
                 json.dumps([{"ts": now_str(), "msg": "迭代 #%d 启动" % iter_no}], ensure_ascii=False)))
            iid = icur.lastrowid; conn.commit()
            new_state, rec = dpgen_ad.drive_iteration(
                run_state,
                {"train": ex_train, "explore": ex_explore, "label": ex_label},
                criteria=cfg.get("converge"), log_fn=_log)
            conn.execute(
                "UPDATE dpgen_iterations SET phase=?,status=?,progress=?,metrics=?,"
                "n_candidates=?,cp2k_queue_ids=?,finished_at=? WHERE id=?",
                (rec.get("phase"), "done", 100,
                 json.dumps(rec.get("metrics") or {}, ensure_ascii=False),
                 rec.get("n_candidates", 0),
                 json.dumps(rec.get("queue_ids", []), ensure_ascii=False),
                 now_str(), iid))
            conn.execute(
                "UPDATE dpgen_runs SET current_iter=?,progress=? WHERE id=?",
                (new_state["iter_no"],
                 min(99, int(100 * new_state["iter_no"] / max(1, new_state["max_iter"]))),
                 run_id))
            conn.commit()
            if new_state.get("converged") or new_state["iter_no"] >= new_state["max_iter"]:
                _finish_dpgen_run(run_id, new_state.get("converged", False), new_state)
                break
    except Exception as e:
        import traceback as _tb
        err = "势函数延拓编排异常：%s\n%s" % (e, (_tb.format_exc() or "")[:2000])
        try:
            conn.execute("UPDATE dpgen_runs SET status='failed',error=?,finished_at=? WHERE id=?",
                         (err, now_str(), run_id))
            conn.commit()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _finish_dpgen_run(run_id, converged, new_state=None):
    """势函数延拓收尾：标记完成并写入结果摘要（含末轮指标与迭代轮次）。"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=15)
        conn.row_factory = sqlite3.Row
        history = conn.execute(
            "SELECT metrics FROM dpgen_iterations WHERE run_id=? AND metrics<>'{}' "
            "ORDER BY iter_no", (run_id,)).fetchall()
        last = None
        for h in history:
            try:
                m = json.loads(h["metrics"] or "{}")
                if m:
                    last = m
            except Exception:
                pass
        result = {"converged": converged,
                  "simulation": os.environ.get("CATMAT_DPGEN_MOCK") == "1",
                  "iters": new_state.get("iter_no") if new_state else None,
                  "last_metrics": last}
        conn.execute(
            "UPDATE dpgen_runs SET status='done',progress=100,result=?,finished_at=? "
            "WHERE id=? AND finished_at IS NULL",
            (json.dumps(result, ensure_ascii=False), now_str(), run_id))
        conn.commit(); conn.close()
    except Exception:
        pass


def _dpgen_iter_to_json(it):
    """将 dpgen_iterations 行（TEXT 列以 JSON 字符串存储）转为 API 友好的字典，
    解析 metrics / cp2k_queue_ids / log 三个 JSON 字段，保证与
    GET /api/dpgen/{id}/iterations 返回结构一致。"""
    d = dict(it)
    for k in ("metrics", "cp2k_queue_ids", "log"):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                pass
    return d


def _sync_ml_tasks(conn):
    """同步训练任务状态：进程已退出且超过宽限期仍未收尾的，标记为失败。

    注意：训练脚本输出最后一行 JSON 后，reader 线程需要一点时间落库；
    若此时进程刚退出就把状态判为 failed，会覆盖掉真实结果。故设 20s 宽限期。
    """
    try:
        import datetime as _dt
        rows = conn.execute("SELECT id,pid,log FROM ml_tasks WHERE status='running'").fetchall()
        now_ts = time.time()
        for r in rows:
            # A live reader owns finalization, even after its child has exited.
            if r["id"] in _ML_READERS:
                continue
            alive = bool(r["pid"]) and proc_alive(int(r["pid"]))
            if alive:
                continue
            # 进程已退出：看最后一条日志的时间，20s 内等待 reader 收尾
            in_grace = False
            try:
                lg = json.loads(r["log"] or "[]")
                last_ts = lg[-1].get("ts") if lg else None
                if last_ts:
                    t0 = _dt.datetime.strptime(str(last_ts)[:19], "%Y-%m-%d %H:%M:%S").timestamp()
                    in_grace = (now_ts - t0) < 20
            except Exception:
                in_grace = False
            if in_grace:
                continue
            conn.execute(
                "UPDATE ml_tasks SET status='failed', error='训练进程已退出（未捕获结果）', "
                "finished_at=? WHERE id=?", (now_str(), r["id"]))
        conn.commit()
    except Exception:
        pass


RESOURCE_FILES = [
    ("灵感_平台设计关键词.md", os.path.join(WORKSPACE, "灵感_平台设计关键词.md")),
    ("SaaS_说明_研究体系.md", os.path.join(WORKSPACE, "SaaS_说明_研究体系.md")),
]

_VER_CACHE = {"t": 0.0, "data": None}
# 版本探测缓存时长（秒）。[中2] 由 60 提到 300：探测需起多个子进程，刷新成本远高于价值。
VER_TTL = 300


def _py_version(mod):
    try:
        with _blocking_unlocked():
            out = subprocess.run([sys.executable, "-c", "import %s;print(%s.__version__)" % (mod, mod)],
                                 capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return None
        return out.stdout.strip() or None
    except Exception:
        return None


def _aiida_py_version(mod, py=None):
    """查询指定解释器里某模块的版本（默认 AiiDA 环境；ML 相关用 dpmd 环境）。"""
    try:
        with _blocking_unlocked():
            out = subprocess.run([py or AII_PY, "-c", "import %s;print(%s.__version__)" % (mod, mod)],
                                 capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return None
        return out.stdout.strip().splitlines()[-1] if out.stdout.strip() else None
    except Exception:
        return None


def _ml_gpu_info():
    """查询 ML 环境（dpmd）的 GPU 可用性：torch + CUDA 设备列表。"""
    try:
        code = ("import json,torch;"
                "print(json.dumps({'torch':torch.__version__,'cuda':torch.cuda.is_available(),"
                "'count':torch.cuda.device_count(),"
                "'devices':[torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())]}))")
        with _blocking_unlocked():
            out = subprocess.run([ML_PY, "-c", code], capture_output=True, text=True, timeout=90)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else None
    except Exception:
        return None


def _probe_cp2k():
    # 固定返回「实际用于计算的 CP2K 根目录名」（默认 cp2k-2026.2，可由 CATMAT_CP2K_ROOT 覆盖），
    # 不再按 /home/user/bin/cp2k-* 就近 glob 最新目录——否则 codex 的构建产物目录
    # （如 cp2k-<git-hash>、cp2k-lowdin-kp-install）会被字符串倒序误判为「当前版本」，
    # 造成网页端软件版本号随构建目录实时漂移。显示版本与计算版本保持一致即可。
    root = (_CP2K_ROOT or os.path.expanduser("~/bin/cp2k-2026.2")).rstrip("/")
    return os.path.basename(root)


def _probe_multiwfn():
    try:
        with _blocking_unlocked():
            out = subprocess.run([MULTIWFN_BIN], input="q\n",
                                 capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return None
        m = re.search(r"Version\s+([0-9.]+)", out.stdout or "")
        return "Multiwfn " + m.group(1) if m else None
    except Exception:
        return None


def _safe_call(fn):
    try:
        return fn()
    except Exception:
        return None


# ==================== 结构化错误码（整改 [低2] P2-3）====================
# 错误响应在保留 error 文本的同时附带稳定的 code 字段，便于按类型处置
# （"用户改改就好" vs "需要报修"）。code 为**新增字段**，不影响既有前端。
ERR_INPUT = "INPUT_ERROR"            # 参数/输入错误（用户可自行修正）
ERR_NOT_FOUND = "NOT_FOUND"          # 资源不存在
ERR_METHOD = "METHOD_NOT_ALLOWED"    # HTTP 方法不支持
ERR_CONFLICT = "CONFLICT"            # 状态冲突（如任务已终态）
ERR_DEPENDENCY = "DEPENDENCY"        # 外部依赖缺失/不可用（CP2K / AiiDA / Multiwfn）
ERR_TIMEOUT = "TIMEOUT"              # 执行超时
ERR_UPSTREAM = "UPSTREAM"            # 上游服务错误（大模型等）
ERR_INTERNAL = "INTERNAL"            # 内部异常（需查日志）

# HTTP 状态 → 默认错误码（_err 未显式指定时使用）
HTTP_DEFAULT_CODE = {
    400: ERR_INPUT, 404: ERR_NOT_FOUND, 405: ERR_METHOD, 409: ERR_CONFLICT,
    500: ERR_INTERNAL, 502: ERR_UPSTREAM, 504: ERR_TIMEOUT,
}


def get_versions(force=False):
    """各软件版本探测。

    [中2] 此前 14 个探测项**串行**起子进程，首调实测约 17 秒（`_ml_gpu_info` 单次上限
    90s、`_probe_multiwfn` 上限 30s）。现改为线程池并行 + 缓存 TTL 300s + 启动预热，
    首屏不再被拖慢。force=True 用于预热或强制刷新。
    """
    now = time.time()
    if not force and _VER_CACHE["data"] is not None and now - _VER_CACHE["t"] < VER_TTL:
        return _VER_CACHE["data"]

    # ML / 主动学习相关库版本取自 dpmd 环境（训练实际使用的环境）
    probes = {
        "cp2k": _probe_cp2k,
        "multiwfn": _probe_multiwfn,
        "aiida": lambda: _aiida_py_version("aiida"),
        "ase": lambda: _py_version("ase"),
        "numpy": lambda: _py_version("numpy"),
        "scipy": lambda: _py_version("scipy"),
        "sklearn": lambda: _aiida_py_version("sklearn", ML_PY),
        "shap": lambda: _aiida_py_version("shap", ML_PY),
        "xgboost": lambda: _aiida_py_version("xgboost", ML_PY),
        "lightgbm": lambda: _aiida_py_version("lightgbm", ML_PY),
        "torch": lambda: _aiida_py_version("torch", ML_PY),
        "botorch": lambda: _aiida_py_version("botorch", ML_PY),
        "gpytorch": lambda: _aiida_py_version("gpytorch", ML_PY),
        "ml_gpu": _ml_gpu_info,
    }
    v = {"python": sys.version.split()[0]}
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(probes))) as ex:
            futs = {ex.submit(fn): k for k, fn in probes.items()}
            for f in futs:
                v[futs[f]] = _safe_call(f.result)
    except Exception:
        log_exc("版本探测并行执行失败，降级为串行")
        for k, fn in probes.items():
            v[k] = _safe_call(fn)
    v["llm_model"] = (load_llm_config() or {}).get("model")
    v["reasoning_effort"] = (load_llm_config() or {}).get("reasoning_effort")
    _VER_CACHE["data"] = v
    _VER_CACHE["t"] = now
    return v


def warm_versions():
    """启动时后台预热版本缓存（[中2]），避免首个用户请求承担 10+ 秒探测。"""
    try:
        t0 = time.time()
        get_versions(force=True)
        LOG.info("版本探测预热完成，耗时 %.1fs（缓存 %ds）", time.time() - t0, VER_TTL)
    except Exception:
        log_exc("版本探测预热失败")


def query_aiida_node(pk):
    """按 PK 查单个节点详情，返回 (data, err)。"""
    try:
        with _blocking_unlocked():
            out = subprocess.run([AII_PY, AIIDA_NODE, str(pk)],
                                 capture_output=True, text=True, timeout=90)
        if out.returncode != 0:
            return None, (out.stderr or "查询失败").strip()[:300]
        return json.loads(out.stdout), None
    except Exception as e:
        return None, "AiiDA 查询异常: %s" % e


def fetch_doi(doi):
    """调用 CrossRef API 获取文献元数据，返回 (data, err)。"""
    import urllib.request
    import urllib.parse
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi.strip())
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CatMatLab/2.5 (mailto:research@localhost)"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            msg = json.loads(resp.read().decode("utf-8"))["message"]
        authors = msg.get("author", []) or []
        author_str = "; ".join(
            "%s %s" % (a.get("family", ""), a.get("given", "")) for a in authors[:6])
        if len(authors) > 6:
            author_str += " et al."
        return {
            "doi": msg.get("DOI", doi),
            "title": (msg.get("title") or [""])[0],
            "authors": author_str,
            "journal": (msg.get("container-title") or [""])[0] or None,
            "year": (msg.get("issued", {}).get("date-parts") or [[None]])[0][0],
            "publisher": msg.get("publisher"),
            "abstract": (msg.get("abstract") or "").strip() or None,
        }, None
    except Exception as e:
        return None, "DOI 元数据获取失败: %s" % e


def fetch_abstract(doi):
    """从 OpenAlex / Semantic Scholar 拉取摘要，返回 (abstract, source, err)。"""
    import urllib.request
    import urllib.parse
    d = urllib.parse.quote(doi.strip())
    # OpenAlex
    try:
        url = "https://api.openalex.org/works/https://doi.org/" + d
        req = urllib.request.Request(url, headers={"User-Agent": "CatMatLab/2.5 (mailto:research@localhost)"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            w = json.loads(resp.read().decode("utf-8"))
        ab = None
        inv = w.get("abstract_inverted_index")
        if inv:
            pos = {}
            for word, idxs in inv.items():
                for i in idxs:
                    pos[i] = word
            ab = " ".join(pos[i] for i in sorted(pos))
        if ab:
            return ab, "OpenAlex", None
    except Exception:
        pass
    # Semantic Scholar
    try:
        url = "https://api.semanticscholar.org/graph/v1/paper/DOI:" + d + "?fields=abstract"
        req = urllib.request.Request(url, headers={"User-Agent": "CatMatLab/2.5"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            p = json.loads(resp.read().decode("utf-8"))
        if p.get("abstract"):
            return p["abstract"], "Semantic Scholar", None
    except Exception:
        pass
    return None, None, "未获取到摘要（该文献可能无公开摘要）"


def web_search(query, limit=5):
    """学术联网检索：OpenAlex + CrossRef，返回 (results, err)。"""
    import urllib.request
    import urllib.parse
    results = []
    # OpenAlex
    try:
        url = "https://api.openalex.org/works?search=" + urllib.parse.quote(query) + "&per-page=" + str(limit)
        req = urllib.request.Request(url, headers={"User-Agent": "CatMatLab/2.5 (mailto:research@localhost)"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        for w in (d.get("results") or [])[:limit]:
            title = (w.get("title") or "")
            results.append({
                "title": title,
                "doi": w.get("doi"),
                "year": (w.get("publication_year")),
                "venue": ((w.get("primary_location") or {}).get("source") or {}).get("display_name"),
                "source": "OpenAlex",
            })
    except Exception:
        pass
    # CrossRef 补充
    if len(results) < limit:
        try:
            url = "https://api.crossref.org/works?query=" + urllib.parse.quote(query) + "&rows=" + str(limit - len(results))
            req = urllib.request.Request(url, headers={"User-Agent": "CatMatLab/2.5 (mailto:research@localhost)"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            for it in (d.get("message", {}).get("items") or []):
                title = (it.get("title") or [""])[0]
                if not title or any(r["title"] == title for r in results):
                    continue
                results.append({
                    "title": title,
                    "doi": it.get("DOI"),
                    "year": (it.get("issued", {}).get("date-parts") or [[None]])[0][0],
                    "venue": (it.get("container-title") or [""])[0],
                    "source": "CrossRef",
                })
        except Exception:
            pass
    if not results:
        return None, "联网检索失败（OpenAlex/CrossRef 均不可达）"
    return results, None


# [中6] AiiDA 进程快照缓存：2 秒内的并发/多客户端请求共用一次探测结果，
# 避免"每个请求起一个 aiida_probe 子进程"导致的子进程线性膨胀。
_PROC_CACHE = {"t": 0.0, "data": None, "err": None, "lock": threading.Lock()}
PROC_TTL = 2.0


def query_aiida_processes(force=False):
    """调用 aiida_probe.py 获取真实 AiiDA 进程状态，返回 (data, err)。

    force=True 跳过缓存强制探测（用于用户手动点刷新需要最新状态时）。
    """
    now = time.time()
    if not force and _PROC_CACHE["t"] and now - _PROC_CACHE["t"] < PROC_TTL:
        return _PROC_CACHE["data"], _PROC_CACHE["err"]
    with _PROC_CACHE["lock"]:
        # 双重检查：等待锁期间可能已有其他线程完成刷新
        now = time.time()
        if not force and _PROC_CACHE["t"] and now - _PROC_CACHE["t"] < PROC_TTL:
            return _PROC_CACHE["data"], _PROC_CACHE["err"]
        try:
            with _blocking_unlocked():
                out = subprocess.run([AII_PY, PROBE_PATH], capture_output=True,
                                     text=True, timeout=90)
            if out.returncode != 0:
                data, err = None, (out.stderr or "aiida_probe 执行失败").strip()[:300]
            else:
                data, err = json.loads(out.stdout), None
        except FileNotFoundError:
            data, err = None, "未找到 AiiDA Python 环境: " + AII_PY
        except Exception as e:
            data, err = None, "AiiDA 查询异常: %s" % e
        _PROC_CACHE["data"], _PROC_CACHE["err"], _PROC_CACHE["t"] = data, err, time.time()
        return data, err


def build_adsorbate_molecule(name):
    """返回吸附分子的 ASE Atoms（锚定原子在 index 0，其余原子朝 +z 远离表面）。

    单原子（O/H/C/N/S/P 等）返回 None（交由 add_adsorbate 直接放置）。
    """
    if name in ("O", "H", "C", "N", "S", "P", "F", "Cl", "Br", "I"):
        return None
    from ase import Atoms
    from ase.build import molecule
    import math
    if name == "OOH":
        # O-O-H：O-O 键 1.45 Å，O-H 键 0.97 Å，O-O-H 角 ~100°；第一个 O 锚定
        ang = math.radians(100)
        hx = 0.97 * math.sin(ang)
        hz = 1.45 + 0.97 * math.cos(ang)
        return Atoms("OOH", positions=[[0.0, 0.0, 0.0], [0.0, 0.0, 1.45], [hx, 0.0, hz]])
    try:
        mol = molecule(name)
        # 确保锚定原子（index 0，通常为 O/N/C）在原点
        mol.positions -= mol.positions[0]
        # 关键修复：ASE 的 molecule() 把 OH/H2O/O2/CO/NH3 等的非锚定原子放在负 z
        # （指向 slab 表面方向），会导致 H 被夹在锚定原子与表面之间。若其余原子
        # 的 z 均值为负，则沿 z 轴翻转整个分子，使非锚定原子朝向 +z（远离表面）。
        import numpy as _np
        rest_z = _np.asarray(mol.positions[1:, 2])
        if len(rest_z) and float(rest_z.mean()) < 0.0:
            mol.positions[:, 2] *= -1.0
        return mol
    except Exception:
        return None


def central_site(slab, site):
    """计算位于晶胞 xy 中心的高对称吸附位点笛卡尔坐标 (x, y)。

    site: ontop（最接近中心的顶层原子）/ bridge（中心近邻对中点）/
          fcc / hcp（中心三原子三角形 hollow；hcp 为下方有第二层原子的三角形）。
    全部位点取在晶胞中心区域，保证吸附物不被周期性 wrap 边缘切断。
    """
    import numpy as np
    cell = np.array(slab.get_cell())
    center = (0.5 * (cell[0] + cell[1]))[:2]
    zs = slab.positions[:, 2]
    top = slab.positions[zs > zs.max() - 0.4]
    top_xy = top[:, :2]
    n = len(top_xy)
    if n == 0:
        return (float(center[0]), float(center[1]))
    if site == "ontop":
        i = int(np.argmin(np.linalg.norm(top_xy - center, axis=1)))
        return (float(top_xy[i][0]), float(top_xy[i][1]))
    # 顶层原子两两距离与最近邻
    D = np.linalg.norm(top_xy[:, None, :] - top_xy[None, :, :], axis=-1)
    np.fill_diagonal(D, np.inf)
    nn = float(D.min())
    # 第二层原子（用于区分 fcc / hcp hollow）
    sec_mask = (zs <= zs.max() - 0.4) & (zs > zs.max() - 2.0)
    sec_xy = slab.positions[sec_mask][:, :2]
    _has_sec = bool(len(sec_xy))

    def _below(cen):
        """质心正下方（<0.55×最近邻）是否有第二层原子 → hcp hollow。"""
        if not _has_sec:
            return False
        return bool((np.linalg.norm(sec_xy - cen, axis=1) < nn * 0.55).any())

    want_below = (site == "hcp")
    if site == "bridge":
        best, best_d = None, 1e9
        for i in range(n):
            for j in range(i + 1, n):
                if abs(D[i, j] - nn) > 0.3:
                    continue
                mid = (top_xy[i] + top_xy[j]) / 2.0
                d = float(np.linalg.norm(mid - center))
                if d < best_d:
                    best_d, best = d, (float(mid[0]), float(mid[1]))
        return best if best is not None else (float(center[0]), float(center[1]))
    # fcc / hcp / 其他 hollow：三原子近邻三角形质心
    best, best_d = None, 1e9
    for i in range(n):
        for j in range(i + 1, n):
            if abs(D[i, j] - nn) > 0.3:
                continue
            for k in range(j + 1, n):
                if abs(D[i, k] - nn) > 0.3 or abs(D[j, k] - nn) > 0.3:
                    continue
                cen = (top_xy[i] + top_xy[j] + top_xy[k]) / 3.0
                if _below(cen) != want_below:
                    continue
                d = float(np.linalg.norm(cen - center))
                if d < best_d:
                    best_d, best = d, (float(cen[0]), float(cen[1]))
    if best is not None:
        return best
    # 回退：不区分 fcc/hcp，取任意中心三角形
    best, best_d = None, 1e9
    for i in range(n):
        for j in range(i + 1, n):
            if abs(D[i, j] - nn) > 0.3:
                continue
            for k in range(j + 1, n):
                if abs(D[i, k] - nn) > 0.3 or abs(D[j, k] - nn) > 0.3:
                    continue
                cen = (top_xy[i] + top_xy[j] + top_xy[k]) / 3.0
                d = float(np.linalg.norm(cen - center))
                if d < best_d:
                    best_d, best = d, (float(cen[0]), float(cen[1]))
    return best if best is not None else (float(center[0]), float(center[1]))


def place_adsorbate(slab, adsorbate, height, site, position=None):
    """把吸附物放到表面位点：单原子用 add_adsorbate，多原子分子手动放置。

    site 为位点名称（ontop/bridge/fcc/hcp），经 central_site 统一换算为
    晶胞中心的高对称位点坐标；position 为自定义笛卡尔坐标 [x, y]（优先级更高，
    供后续自定义吸附位点）。放置后立即 wrap 并验证分子完整性（键长不被
    周期性边界断裂）。
    """
    from ase import Atom
    from ase.build import add_adsorbate
    mol = build_adsorbate_molecule(adsorbate)
    anchor_symbol = mol.get_chemical_symbols()[0] if mol is not None else adsorbate

    def _verify_molecule(start, mol):
        """wrap 后验证分子几何完整性（未被周期性边界断裂）。"""
        import numpy as np
        placed = slab.positions[start:]
        for i in range(1, len(placed)):
            d = float(np.linalg.norm(placed[i] - placed[0]))
            d0 = float(np.linalg.norm(mol.positions[i] - mol.positions[0]))
            if abs(d - d0) > 0.1:
                raise ApiError(400,
                               "吸附物 %s 放置失败：原子 %d(%s) 与锚定原子的距离在周期性 wrap 后"
                               "变为 %.3f Å（应为 %.3f Å）——吸附位点疑似在晶胞边缘导致分子被"
                               "周期边界切断。请使用默认中心位点，或提供位于表面中心附近的"
                               "自定义坐标 ads_position" %
                               (adsorbate, i, mol.get_chemical_symbols()[i], d, d0))

    # 位点坐标：自定义 position 优先；命名位点经 central_site 换算为中心高对称位点
    if position is not None and len(position) >= 3:
        start = len(slab)
        if mol is None:
            slab.append(Atom(adsorbate, position=[float(x) for x in position]))
        else:
            rel = mol.positions - mol.positions[0]
            for i in range(len(mol)):
                slab.append(Atom(mol.get_chemical_symbols()[i],
                                 position=[float(position[j]) + rel[i][j] for j in range(3)]))
        slab.wrap()
        if mol is not None:
            _verify_molecule(start, mol)
        return
    if position is not None:
        pos2d = (float(position[0]), float(position[1]))
    elif isinstance(site, str) and site.lower() == "center":
        # 表面中心高对称位点：取晶胞 xy 中心附近的 fcc hollow（稳定高对称位）
        pos2d = central_site(slab, "fcc")
    elif isinstance(site, str) and site.lower() in ("ontop", "bridge", "fcc", "hcp"):
        pos2d = central_site(slab, site.lower())
    else:
        pos2d = None

    # 用显式坐标放置锚定原子（单原子即完成）
    tmp = slab.copy()
    if pos2d is not None:
        add_adsorbate(tmp, anchor_symbol, height=height, position=pos2d)
    else:
        add_adsorbate(tmp, anchor_symbol, height=height, position=site)
    anchor_pos = tmp.positions[-1].copy()
    if mol is None:
        # 单原子：把锚定原子加入 slab
        slab.append(Atom(adsorbate, position=anchor_pos))
        return
    # 多原子分子：锚定原子在 anchor_pos，其余按分子几何（朝 +z 远离表面）
    rel = mol.positions - mol.positions[0]
    start = len(slab)
    for i in range(len(mol)):
        slab.append(Atom(mol.get_chemical_symbols()[i],
                         position=anchor_pos + rel[i]))
    slab.wrap()
    _verify_molecule(start, mol)


# ─────────────────────────────────────────────────────────────
# 配位环境识别与编码（概念化：不绑定具体元素，识别位点邻居拓扑）
# 规则源自 8-18 数据集 build_adsorption.py：
#   env_key(n_neigh, bits)：6 邻居 → D6 正则环；其他 → N{邻居数}:{排序bits}
#   0/1 = 邻居元素相对基准元素（出现最多的元素=0，其余=1）的二分类
# 周期边界：mic_xy 面内最小镜像，自动校准晶胞边界两个位点的数据
# ─────────────────────────────────────────────────────────────
def normalize_slab_vacuum(atoms, z_bottom_max=10.0, vacuum_top_min=25.0):
    """统一平移 + 真空层归一化（所有 ASE 生成结构的必经处理步骤）。

    ASE 的 fcc111/hcp0001 等 slab builder 默认上下真空层厚度一致，本函数统一调整为：
    - 结构下表面（最低原子 z）最高不超过 z_bottom_max（默认 10 Å）；
    - 上表面真空层（c − 最高原子 z）最低不低于 vacuum_top_min（默认 25 Å）；
    - 若原晶胞 Z 轴尺寸不足以同时满足两条，先扩展 Z 轴，再整体向下平移。

    仅做整体平移（不改原子间距），必要时扩展 c。返回 atoms（原地修改）。
    """
    import numpy as np
    pos = np.asarray(atoms.get_positions(), dtype=float)
    zmin = float(pos[:, 2].min())
    zmax = float(pos[:, 2].max())
    thick = zmax - zmin
    cell = atoms.get_cell()
    c = float(cell[2][2])
    need = thick + float(z_bottom_max) + float(vacuum_top_min)
    if c < need - 1e-6:
        cell[2][2] = need
        atoms.set_cell(cell, scale_atoms=False)
        c = need
    atoms.translate([0.0, 0.0, float(z_bottom_max) - zmin])
    atoms.wrap()
    return atoms


def aiida_task_files(pk, filename=None):
    """检索 AiiDA 任务（PK）的工作路径与文件清单；传 filename 时读取文件内容。

    返回 (info, err)。info 含 workdir / workdir_files / retrieved_files / content。
    """
    if not os.path.isfile(AIIDA_TASK_FILES_SCRIPT):
        return None, "未找到脚本: " + AIIDA_TASK_FILES_SCRIPT
    env = dict(os.environ)
    env.setdefault("AIIDA_PATH", os.path.expanduser("~/.aiida"))
    args = [VERDI, "run", AIIDA_TASK_FILES_SCRIPT, str(int(pk))]
    if filename:
        args.append(str(filename))
    try:
        with _blocking_unlocked():
            proc = subprocess.run(args, capture_output=True, text=True, timeout=180, env=env)
    except subprocess.TimeoutExpired:
        return None, "检索超时（>180s）"
    except Exception as e:
        return None, "检索失败: %s" % e
    if proc.returncode != 0:
        return None, "外部程序失败: " + (proc.stderr or proc.stdout or "")[-500:]
    for ln in (proc.stdout or "").splitlines():
        ln = ln.strip()
        if ln.startswith("{") and ln.endswith("}"):
            try:
                return json.loads(ln), None
            except Exception:
                continue
    return None, "AiiDA 任务检索失败（PK=%s）: %s" % (pk, ((proc.stderr or proc.stdout) or "")[-300:])


def run_ase_user_code(code, count=3):
    """Legacy entry disabled until a real execution isolation backend is configured."""
    return None, "任意 Python 结构生成入口暂不可用：字符串过滤不构成安全隔离。请使用结构生成表单或导入 ASE 生成的结构文件。"


def mic_xy_vec(vec, cell):
    """面内最小镜像（仅 xy 周期）——晶胞边界位点的 PBC 校准。"""
    import numpy as np
    cell = np.asarray(cell, dtype=float)
    frac = np.asarray(vec, dtype=float) @ np.linalg.inv(cell)
    frac[:2] -= np.round(frac[:2])
    return frac @ cell


def canonical_ring(bits):
    """六元环 0/1 序列在 D6 群（旋转+镜像）下的正则形式（最小字符串）。"""
    forms = []
    for seq in (bits, bits[::-1]):
        for k in range(len(bits)):
            forms.append("".join(str(b) for b in seq[k:] + seq[:k]))
    return min(forms) if forms else ""


def env_key(n_neigh, bits):
    """配位环境唯一编码：6 邻居 → D6 正则环；其他 → N{邻居}:{排序bits}。"""
    if n_neigh == 6:
        return "N6:" + canonical_ring(bits)
    return "N%d:%s" % (n_neigh, "".join(map(str, sorted(bits))))


def ase_analyze_sites(atoms, a=None):
    """表面吸附位点识别 + 配位环境编码 + PBC 校准。

    atoms: ASE Atoms（slab，pbc 开启）。
    返回 (sites, pairs)：
      sites: [{idx, elem, pos(xyz), z, cn, n_neigh, bits, env, neigh}]
      pairs: 相邻表面位点对（用于桥式/双位点吸附）
    """
    from collections import Counter
    import numpy as np
    cell = np.asarray(atoms.get_cell(), dtype=float)
    pos = np.asarray(atoms.get_positions(), dtype=float)
    syms = atoms.get_chemical_symbols()
    nn = (float(a) / np.sqrt(2.0)) if a else (float(cell[0, 0]) / np.sqrt(2.0))
    r_cut = nn * 1.15      # 配位数截断
    ring_cut = nn * 1.25   # 环邻居截断
    pair_cut = nn * 1.20   # 相邻位点对截断
    n = len(atoms)
    dmat = np.full((n, n), np.inf)
    vecs = {}
    for i in range(n):
        for j in range(i + 1, n):
            v = mic_xy_vec(pos[j] - pos[i], cell)
            d = np.linalg.norm(v)
            dmat[i, j] = dmat[j, i] = d
            vecs[(i, j)] = v
            vecs[(j, i)] = -v
    CN = (dmat < r_cut).sum(axis=1)
    zmax = pos[:, 2].max()
    surf = [i for i in range(n) if CN[i] <= 10 and pos[i, 2] > zmax - 4.0]
    dom = Counter(syms).most_common(1)[0][0] if syms else "A"
    sites = []
    for i in surf:
        js = [j for j in range(n)
              if j != i and dmat[i, j] < ring_cut and vecs[(i, j)][2] > -0.5]
        if len(js) >= 3:
            rel = np.array([vecs[(i, j)] for j in js])
            ang = np.arctan2(rel[:, 1], rel[:, 0])
            js = [js[k] for k in np.argsort(ang)]
        bits = [1 if syms[j] != dom else 0 for j in js]
        sites.append({
            "idx": int(i), "elem": syms[i],
            "pos": [round(float(x), 4) for x in pos[i]],
            "z": round(float(pos[i, 2]), 4),
            "cn": int(CN[i]), "n_neigh": len(js), "bits": bits,
            "neigh": list(js), "env": env_key(len(js), bits),
        })
    pairs = []
    si = [s["idx"] for s in sites]
    for i in range(len(si)):
        for j in range(i + 1, len(si)):
            if dmat[si[i], si[j]] < pair_cut:
                pairs.append([si[i], si[j]])
    return sites, pairs


# 配位环境编码知识库种子（源自 8-18 数据集 structure_matrix.xlsx：13 预设 + 28 补充）
# 概念化编码：不绑定具体元素，仅识别位点邻居拓扑（N{邻居数}:{D6正则环或排序bits}）
ASE_ENV_CODES = [
    ("〇", "M=(Ir=0,Ru=1)", [0, 0, 0, 0, 0, 0]), ("Ⅰ", "", [0, 0, 0, 0, 0, 1]),
    ("Ⅱ-Ⅰ", "", [0, 0, 0, 0, 1, 1]), ("Ⅱ-Ⅱ", "", [0, 0, 0, 1, 0, 1]),
    ("Ⅱ-Ⅲ", "", [0, 0, 1, 0, 0, 1]), ("Ⅲ-Ⅰ", "", [0, 0, 0, 1, 1, 1]),
    ("Ⅲ-Ⅱ", "", [0, 0, 1, 0, 1, 1]), ("Ⅲ-Ⅲ", "", [0, 1, 0, 1, 0, 1]),
    ("Ⅳ-Ⅰ", "", [1, 1, 1, 1, 0, 0]), ("Ⅳ-Ⅱ", "", [1, 1, 1, 0, 1, 0]),
    ("Ⅳ-Ⅲ", "", [1, 1, 0, 1, 1, 0]), ("Ⅴ", "", [1, 1, 1, 1, 1, 0]),
    ("Ⅵ", "", [1, 1, 1, 1, 1, 1]),
    ("N5-Ⅰ", "N=5 ring=00111", [0, 0, 1, 1, 1]), ("N7-Ⅰ", "N=7 ring=0001111", [0, 0, 0, 1, 1, 1]),
    ("N5-Ⅱ", "N=5 ring=00011", [0, 0, 0, 1, 1]), ("N4-Ⅰ", "N=4 ring=0000", [0, 0, 0, 0]),
    ("N4-Ⅱ", "N=4 ring=0001", [0, 0, 0, 1]), ("N1-Ⅰ", "N=1 ring=0", [0]),
    ("N5-Ⅲ", "N=5 ring=00001", [0, 0, 0, 0, 1]), ("N3-Ⅰ", "N=3 ring=001", [0, 0, 1]),
    ("N2-Ⅰ", "N=2 ring=00", [0, 0]), ("N3-Ⅱ", "N=3 ring=000", [0, 0, 0]),
    ("N7-Ⅱ", "N=7 ring=0000111", [0, 0, 0, 0, 1, 1]), ("N4-Ⅲ", "N=4 ring=0111", [0, 1, 1, 1]),
    ("N0-Ⅰ", "N=0 ring=", []), ("N5-Ⅳ", "N=5 ring=01111", [0, 1, 1, 1, 1]),
    ("N3-Ⅲ", "N=3 ring=011", [0, 1, 1]), ("N7-Ⅲ", "N=7 ring=0000001", [0, 0, 0, 0, 0, 0]),
    ("N8-Ⅰ", "N=8 ring=00001111", [0, 0, 0, 0, 1, 1]), ("N7-Ⅳ", "N=7 ring=0000011", [0, 0, 0, 0, 0, 1]),
    ("N4-Ⅳ", "N=4 ring=0011", [0, 0, 1, 1]), ("N2-Ⅱ", "N=2 ring=11", [1, 1]),
    ("N8-Ⅱ", "N=8 ring=00000001", [0, 0, 0, 0, 0, 0]), ("N5-Ⅴ", "N=5 ring=00000", [0, 0, 0, 0, 0]),
    ("N2-Ⅲ", "N=2 ring=01", [0, 1]), ("N3-Ⅳ", "N=3 ring=111", [1, 1, 1]),
    ("N1-Ⅱ", "N=1 ring=1", [1]), ("N8-Ⅲ", "N=8 ring=00011111", [0, 0, 0, 1, 1, 1]),
    ("N8-Ⅳ", "N=8 ring=00000111", [0, 0, 0, 0, 0, 1]), ("N8-Ⅴ", "N=8 ring=00000000", [0, 0, 0, 0, 0, 0]),
]


def seed_env_codes(conn):
    """把配位环境编码写入知识库（幂等：topic 已存在则跳过）。

    随结构变化持续更新：analyze-sites / ads-submit 发现新 env 编码时调用
    upsert_env_code(conn, env) 追加，无需人工维护。
    """
    for code, note, bits in ASE_ENV_CODES:
        n = len(bits)
        env = ("N6:" + canonical_ring(bits)) if n == 6 else ("N%d:%s" % (n, "".join(map(str, sorted(bits)))))
        exists = conn.execute(
            "SELECT COUNT(*) c FROM knowledge WHERE category='ASE配位环境编码' AND topic=?",
            (code,)).fetchone()["c"]
        if not exists:
            conn.execute(
                "INSERT INTO knowledge(category,topic,keyword,recommended,rationale,source,origin) VALUES(?,?,?,?,?,?,?)",
                ("ASE配位环境编码", code, "配位环境 " + env,
                 "编码 %s（邻居数 %d）bit=%s %s" % (env, n, bits, note),
                 "概念化配位环境识别与编码（不绑定具体元素）：0/1 = 邻居相对基准元素的二分类；"
                 "6 邻居取 D6 群正则环；位点分析自动做 PBC 面内最小镜像校准。",
                 "structure_matrix.xlsx", "ASE配位环境"))


def upsert_env_code(conn, env):
    """把新发现的配位环境编码写入知识库（不存在才插入，随结构变化持续更新）。"""
    try:
        conn.execute(
            "INSERT INTO knowledge(category,topic,keyword,recommended,rationale,source,origin) "
            "SELECT 'ASE配位环境编码',?,?,?,?,?,? WHERE NOT EXISTS "
            "(SELECT 1 FROM knowledge WHERE category='ASE配位环境编码' AND topic=?)",
            (env, "配位环境 " + env, "新发现配位环境编码 " + env,
             "由 ASE 位点分析自动识别的新配位环境（概念化编码，不绑定具体元素）。",
             "结构变化自动更新", "ASE位点分析", env))
    except Exception:
        pass


def import_skills_to_kb(conn, skills_dir=None):
    """把本地 SKILL（~/.workbuddy/skills/）导入知识库，供 AI 助手检索学习使用。

    幂等：topic（技能名）已存在则跳过；更新 SKILL 后重新调用即可刷新（先删后插）。
    返回新增/刷新条数。
    """
    skills_dir = skills_dir or os.path.expanduser("~/.workbuddy/skills")
    if not os.path.isdir(skills_dir):
        return 0
    n = 0
    for entry in sorted(os.listdir(skills_dir)):
        d = os.path.join(skills_dir, entry)
        f = os.path.join(d, "SKILL.md")
        if os.path.isdir(d) and os.path.isfile(f):
            pass
        elif os.path.isfile(os.path.join(skills_dir, entry)) and entry.endswith(".md"):
            f = os.path.join(skills_dir, entry)
        else:
            continue
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                txt = fh.read()
        except Exception:
            continue
        name = entry
        m = re.search(r"^name:\s*(.+)$", txt, re.M)
        if m:
            name = m.group(1).strip()
        desc = ""
        m = re.search(r"^description:\s*(.+)$", txt, re.M)
        if m:
            desc = m.group(1).strip()[:300]
        # 去掉 frontmatter，只保留正文
        body = txt
        if txt.startswith("---"):
            parts = txt.split("---", 2)
            if len(parts) >= 3:
                body = parts[2]
        conn.execute("DELETE FROM knowledge WHERE category='平台技能SKILL' AND topic=?", (name,))
        conn.execute(
            "INSERT INTO knowledge(category,topic,keyword,recommended,rationale,source,origin) VALUES(?,?,?,?,?,?,?)",
            ("平台技能SKILL", name, desc or entry,
             "本地 SKILL 技能文档（供 AI 助手学习使用）：" + (desc or entry),
             body[:6000], f.replace(os.path.expanduser("~"), "~"), "SKILL"))
        n += 1
    return n


def load_aiida_structure_json(pk):
    """通过 verdi run 从 AiiDA 库读取 StructureData → dict（重新导入 ASE 用）。"""
    if not os.path.isfile(LOAD_STRUCTURE_SCRIPT):
        return None, "未找到读取脚本: " + LOAD_STRUCTURE_SCRIPT
    env = dict(os.environ)
    env.setdefault("AIIDA_PATH", os.path.expanduser("~/.aiida"))
    try:
        with _blocking_unlocked():
            proc = subprocess.run([VERDI, "run", LOAD_STRUCTURE_SCRIPT, str(pk)],
                                  capture_output=True, text=True, timeout=120, env=env)
    except Exception as e:
        return None, "读取 AiiDA 结构失败: %s" % e
    for ln in (proc.stdout or "").splitlines():
        ln = ln.strip()
        if ln.startswith("{") and ln.endswith("}"):
            try:
                return json.loads(ln), None
            except Exception:
                continue
    return None, "AiiDA 结构读取失败（PK=%s）: %s" % (pk, ((proc.stderr or proc.stdout) or "")[-300:])


def ase_atoms_from_dict(d):
    """把 load_aiida_structure_json 的 dict 还原为 ASE Atoms（笛卡尔坐标）。"""
    from ase import Atoms
    import numpy as np
    cell = np.array(d["cell"], dtype=float)
    sp = np.array(d["scaled_positions"], dtype=float)
    cart = sp.dot(cell)
    return Atoms(symbols=d["symbols"], positions=cart, cell=cell,
                 pbc=d.get("pbc", [True, True, True]))


# ==================== 结构生成参数白名单（整改 [中1]：杜绝静默回退）====================
# 背景：此前传入无法识别的参数（如把 element 误写成 elements）会被**无声忽略**，
# 结构生成回退到默认元素 Ir，用户拿到的是元素完全错误的 INP 且全程无任何警告
# —— 属于"算错而不自知"。现在改为显式 400 报错并列出合法参数。
ASE_PARAM_KEYS = {
    "a", "ads_height", "ads_position", "ads_site", "adsorbate", "alloy", "alloy_frac",
    "c", "dop_site", "dopant", "element", "face", "layers", "m", "mix_elements",
    "mix_ratio", "mix_seed", "n", "normalize_vacuum", "orthogonal", "vacuum",
    "vacuum_top_min", "z_bottom_max",
}
# 不参与 ASE 构建、但前端表单/内部调用会携带的元信息字段
ASE_META_KEYS = {"name", "label", "comment", "save", "folder", "kind", "hkl", "symbols", "site"}
# /api/multiwfn/inp 自身的接口参数
MWF_INP_KEYS = {"calc_type", "dispersion", "smearing", "pbc", "scf_method", "extra", "structure", "cell"}

ASE_ENUMS = {
    "face": ("fcc111", "fcc100", "fcc110", "hcp0001"),
    "ads_site": ("ontop", "bridge", "fcc", "hcp", "center"),
}


# 标准元素符号表（避免为校验引入 ase 导入开销——ase 是延迟导入的重模块）
_ELEMENTS = set("""H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn
Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu
Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm
Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og""".split())


def validate_ase_params(p):
    """校验结构生成参数取值（非法取值直接报错）。返回 (ok, errmsg)。

    覆盖两类：枚举字段（face / ads_site）与元素符号（element / alloy / dopant）。
    元素符号若不校验，非法值会一路穿透到 ASE 抛出 KeyError，用户只看到
    「结构生成失败: 'Xx'」这种无法定位的报错。
    """
    if not isinstance(p, dict):
        return False, "参数格式错误：应为 JSON 对象"
    for k, allowed in ASE_ENUMS.items():
        v = p.get(k)
        if v not in (None, "", []) and str(v) not in allowed:
            return False, "不支持的 %s: %s（可选 %s）" % (k, v, " / ".join(allowed))
    for k in ("element", "alloy", "dopant"):
        v = p.get(k)
        if v not in (None, "") and str(v) not in _ELEMENTS:
            return False, "不支持的 %s 元素符号：%s（应为标准元素符号，如 Pt / Ir / Ru）" % (k, v)
    return True, None


def check_unknown_ase_params(p, allowed_extra=()):
    """检查是否存在无法识别的参数（会被静默忽略）。返回 (ok, errmsg)。"""
    if not isinstance(p, dict):
        return False, "参数格式错误：应为 JSON 对象"
    allowed = ASE_PARAM_KEYS | ASE_META_KEYS | set(allowed_extra or ())
    unknown = [k for k in p.keys() if k not in allowed]
    if unknown:
        return False, ("存在无法识别的参数：%s。这些参数会被静默忽略并回退为默认元素，"
                       "可能生成错误结构。合法参数：%s"
                       % ("、".join(sorted(unknown)), "、".join(sorted(allowed))))
    return True, None


def build_structure_ase(p):
    """用 ASE 真实生成结构；参数全部落地到生成流程。"""
    import ase  # noqa: F401（延迟导入，避免拖慢启动）
    import numpy as np
    from ase.build import fcc111, fcc100, fcc110, hcp0001, add_adsorbate

    face = p.get("face", "fcc111")
    a = float(p.get("a", 3.84) or 3.84)
    c = float(p.get("c", 0) or 0)
    layers = int(p.get("layers", 4) or 4)
    vacuum = float(p.get("vacuum", 15.0) if p.get("vacuum") not in (None, "") else 15.0)
    n = int(p.get("n", 3) or 3)
    m = int(p.get("m", 3) or 3)
    element = p.get("element", "Ir")
    alloy = p.get("alloy") or None
    alloy_frac = float(p.get("alloy_frac", 0) or 0)
    dopant = p.get("dopant") or None
    dop_site = int(p.get("dop_site", 0) or 0)
    adsorbate = p.get("adsorbate") or None
    ads_site = p.get("ads_site", "ontop")
    ads_height = float(p.get("ads_height", 1.5) if p.get("ads_height") not in (None, "") else 1.5)
    orthogonal = bool(p.get("orthogonal"))

    builders = {"fcc111": fcc111, "fcc100": fcc100, "fcc110": fcc110, "hcp0001": hcp0001}
    bfn = builders.get(face)
    if bfn is None:
        raise ApiError(400, "不支持的晶面: " + str(face))

    kw = dict(symbol=element, size=(n, m, layers), a=a, vacuum=vacuum)
    if face == "hcp0001":
        kw["c"] = c if c else a * 1.63
    if face == "fcc111":
        kw["orthogonal"] = orthogonal
    try:
        slab = bfn(**kw)
    except Exception as e:
        raise ApiError(400, "结构生成失败: %s" % e)

    # ── 两种元素随机混杂（mix_elements=[A,B]，比例 mix_ratio 可调，种子 mix_seed 可复现）──
    mix_els = p.get("mix_elements") or None
    if isinstance(mix_els, str):
        mix_els = [x.strip() for x in str(mix_els).replace("，", ",").split(",") if x.strip()]
    mix_ratio = float(p.get("mix_ratio") if p.get("mix_ratio") not in (None, "") else 0.5)
    mix_seed = p.get("mix_seed")
    if mix_els and len(mix_els) >= 2:
        rng = random.Random(int(mix_seed) if str(mix_seed).strip() not in ("", "None") else 42)
        ea, eb = str(mix_els[0]).strip(), str(mix_els[1]).strip()
        for at in slab:
            at.symbol = eb if rng.random() < mix_ratio else ea
    # 合金：按比例替换为第二元素（固定随机种子，可复现）
    elif alloy and alloy_frac > 0:
        rng = random.Random(42)
        idxs = list(range(len(slab)))
        rng.shuffle(idxs)
        for i in idxs[:int(round(len(slab) * alloy_frac))]:
            slab[i].symbol = alloy
    # 掺杂：替换指定位点原子
    if dopant and dop_site >= 1:
        di = dop_site - 1
        if di < len(slab):
            slab[di].symbol = dopant
    # 吸附物种 + 位点
    if adsorbate:
        # 关键修复 1：fcc111 等 builder 的 index 0 是底层原子，而 ASE add_adsorbate 的
        # 命名位点（ontop/bridge 等）参考固定索引原子，总是落在晶胞角落。
        # 先将顶层原子排到 index 0，再由 place_adsorbate 经 central_site 把命名位点
        # 统一换算为晶胞 xy 中心的高对称位点（禁止边缘放置）。
        order = np.argsort(-slab.positions[:, 2])
        slab = slab[order]
        # 关键修复 2：OH/OOH/H2O 等多原子分子无法用 add_adsorbate（仅支持单原子），
        # 改用 place_adsorbate 手动放置分子几何，放置后验证键长不被 wrap 断裂。
        # 关键修复 3：支持自定义吸附位点 ads_position（笛卡尔 [x,y] 或 [x,y,z]，
        # 优先级高于命名位点）。
        ads_position = p.get("ads_position") or None
        if isinstance(ads_position, (list, tuple)) and len(ads_position) >= 2:
            try:
                ads_position = [float(x) for x in ads_position]
            except (TypeError, ValueError):
                raise ApiError(400, "ads_position 需为数值坐标 [x, y] 或 [x, y, z]")
        else:
            ads_position = None
        try:
            place_adsorbate(slab, adsorbate, height=ads_height, site=ads_site,
                            position=ads_position)
        except ApiError:
            raise
        except Exception as e:
            raise ApiError(400, "吸附设置失败: %s" % e)
    slab.wrap()
    # 统一平移处理（所有 ASE 生成结构必经）：下表面 ≤ z_bottom_max，上真空 ≥ vacuum_top_min
    if p.get("normalize_vacuum", True):
        normalize_slab_vacuum(slab,
                              z_bottom_max=float(p.get("z_bottom_max") or 10.0),
                              vacuum_top_min=float(p.get("vacuum_top_min") or 25.0))

    result = {
        "formula": slab.get_chemical_formula(),
        "natoms": len(slab),
        "symbols": slab.get_chemical_symbols(),
        "cell": [[float(round(x, 6)) for x in v] for v in slab.get_cell()],
        "scaled_positions": [[float(round(x, 6)) for x in sp] for sp in slab.get_scaled_positions()],
        "pbc": [bool(x) for x in slab.get_pbc()],
        "cellpar": [float(round(x, 4)) for x in slab.get_cell_lengths_and_angles()],
        "info": {"face": face, "a": a, "layers": layers, "vacuum": vacuum, "supercell": [n, m]},
    }
    # 吸附物组成信息（供前端确认 O/H 等原子均存在）
    if adsorbate:
        result["adsorbate"] = adsorbate
        mol = build_adsorbate_molecule(adsorbate)
        result["adsorbate_atoms"] = (mol.get_chemical_symbols() if mol is not None
                                     else [adsorbate])
    return result


def proc_alive(pid):
    """进程是否仍在运行。**僵尸（Z）视为已结束**——这是关键的坑：

    被 kill 的进程会成为僵尸等待父进程 reap，此时 `os.kill(pid, 0)` 仍然成功，
    若只用它判断存活，会得到「进程未退出」的假阴性结论。
    """
    try:
        with open("/proc/%d/stat" % pid, "r") as f:
            data = f.read()
        # 格式：pid (comm) state ... —— comm 可能含空格/括号，故从最后一个 ')' 后取
        state = data.rsplit(")", 1)[1].split()[0]
        return state not in ("Z", "X", "x")
    except FileNotFoundError:
        return False
    except Exception:
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def reap_pid(pid):
    """尝试回收子进程，避免僵尸堆积（非本进程的子进程会抛错，忽略即可）。"""
    try:
        p = SUBMITTED_PROCS.pop(int(pid), None)
        if p is not None:
            p.poll()
            return
    except Exception:
        pass
    try:
        os.waitpid(int(pid), os.WNOHANG)
    except (ChildProcessError, OSError, ValueError):
        pass


def terminate_process_tree(pid, sig=15, timeout=8):
    """终止进程树（mpirun/prterun 父进程 + 全部 cp2k.psmp 子进程）。

    submit_cp2k_direct 用 start_new_session=True 启动，故 pid 即会话首进程，
    其进程组 ID == pid；用 killpg 可一次性终止整个 MPI 进程树。
    返回 (killed, detail)。
    """
    import signal
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False, "非法 PID: %r" % pid
    if not proc_alive(pid):
        reap_pid(pid)
        return False, "进程 %d 不存在（可能已结束）" % pid
    pgid = None
    try:
        pgid = os.getpgid(pid)
    except Exception:
        pgid = pid
    # 先温柔终止（SIGTERM），给 MPI 与 CP2K 收尾机会；未生效再升级 SIGKILL
    for s in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, s)
        except ProcessLookupError:
            reap_pid(pid)
            return True, "进程组 %d 已终止" % pgid
        except PermissionError:
            try:
                os.kill(pid, s)
            except Exception:
                pass
        name = "SIGTERM" if s == signal.SIGTERM else "SIGKILL"
        deadline = time.time() + (timeout if s == signal.SIGTERM else 3)
        while time.time() < deadline:
            if not proc_alive(pid):
                reap_pid(pid)
                return True, "已发送 %s，进程组 %d 已终止" % (name, pgid)
            time.sleep(0.3)
        if s == signal.SIGTERM:
            continue   # TERM 未生效 → 升级为 KILL
    reap_pid(pid)
    if proc_alive(pid):
        return False, "已发送 SIGKILL，但进程 %d 仍未退出（可能处于不可中断状态）" % pid
    return True, "已强制终止进程组 %d" % pgid


def kill_aiida_process(pk):
    """通过 verdi 终止 AiiDA 进程。返回 (ok, detail)。"""
    if not pk:
        return False, "无 AiiDA PK"
    if not os.path.isfile(VERDI):
        return False, "未找到 verdi: " + VERDI
    try:
        with _blocking_unlocked():
            out = subprocess.run([VERDI, "process", "kill", "-F", "--timeout", "20", str(pk)],
                                 capture_output=True, text=True, timeout=40)
    except subprocess.TimeoutExpired:
        return False, "verdi process kill 超时（进程可能仍在收尾）"
    except Exception as e:
        return False, "verdi kill 执行失败: %s" % e
    txt = (out.stdout or "") + (out.stderr or "")
    if out.returncode == 0:
        return True, "AiiDA 进程 %s 已终止" % pk
    return False, "verdi kill 返回 %d：%s" % (out.returncode, txt.strip()[-300:])


def query_aiida_states(pks):
    """批量查询 AiiDA 进程状态（一次调用，避免逐个起进程）。返回 {pk: {state, status, exit_status}}。"""
    wanted = [str(p) for p in (pks or []) if str(p).strip().isdigit()]
    if not wanted:
        return {}
    try:
        with _blocking_unlocked():
            out = subprocess.run(
                [AII_PY, PROBE_PATH, "--states", ",".join(wanted)],
                capture_output=True, text=True, timeout=90)
        if out.returncode != 0:
            return {}
        return json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        return {}


def map_aiida_state(info):
    """把 AiiDA process_state/exit_status 映射为平台任务状态。"""
    if not info:
        return "running"
    state = (info.get("state") or "").lower()
    code = info.get("exit_status")
    if state == "finished":
        return "done" if code in (0, None, "0") else "failed"
    if state in ("killed", "cancelled"):
        return "cancelled"
    if state in ("excepted", "exceptioned"):
        return "failed"
    return "running"


def cp2k_out_status(out_path):
    """从 CP2K 输出文件判断任务是否已完成/失败；无法判断返回 None。"""
    if not out_path or not os.path.isfile(out_path):
        return None
    ended, failed = False, False
    try:
        # Stream without retaining large logs; reset at each appended attempt.
        with open(out_path, "r", encoding="utf-8", errors="ignore") as stream:
            for line in stream:
                if "PROGRAM STARTED AT" in line:
                    ended, failed = False, False
                if "ABORT" in line or "SCF run NOT converged" in line:
                    failed = True
                if "PROGRAM ENDED AT" in line:
                    ended = True
    except OSError:
        return None
    if failed:
        return "failed"
    return "done" if ended else None


def cp2k_out_path_for(resources, name):
    """从 resources（含 workdir）+ 任务名推导 .out 路径。"""
    wd = resources.get("workdir")
    if not wd:
        return None
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name or "cp2k_task"))[:80] or "cp2k_task"
    return os.path.join(wd, safe + ".out")


def sync_task_statuses(conn):
    """刷新任务真实状态：非终态任务按 PID / AiiDA PK 检测是否已终止并更新。

    返回 {"updated": N, "removed": N, "detail": [...]}。
    """
    rows = conn.execute("SELECT * FROM tasks").fetchall()
    aiida_pks = [r["aiiada_pk"] for r in rows if r["aiiada_pk"]]
    states = query_aiida_states(aiida_pks) if aiida_pks else {}

    updated, removed, detail = 0, 0, []
    for r in rows:
        tid, status, log = r["id"], r["status"], json.loads(r["log"] or "[]")
        res = {}
        try:
            res = json.loads(r["resources"] or "{}")
        except Exception:
            res = {}
        pid, pk = res.get("pid"), r["aiiada_pk"]

        # ③ 既无 PID 也无 PK：幽灵/占位任务（无法追踪，予以清除）——无论当前状态
        if not pid and not pk:
            removed += 1
            detail.append("已清除无效任务 #%d「%s」（无 PID 且无 AiiDA PK，无法追踪）" % (tid, r["name"]))
            conn.execute("DELETE FROM tasks WHERE id=?", (tid,))
            continue

        # 已处终态的任务不再检测
        if status in ("done", "failed", "cancelled"):
            continue

        new_status = None
        reason = ""
        # ① 独立 CP2K 任务：按 PID 存活 + 输出文件判定
        if pid:
            if not proc_alive(pid):
                out = res.get("out") or cp2k_out_path_for(res, r["name"])
                st = cp2k_out_status(out)
                new_status = st or "failed"   # 进程已结束但无法判定 → 按失败
                reason = "PID %s 已结束（输出文件判定：%s）" % (pid, new_status)
        # ② AiiDA 任务：按 PK 状态
        elif pk:
            info = states.get(str(pk))
            mapped = map_aiida_state(info)
            if info and mapped in ("done", "failed", "cancelled"):
                new_status = mapped
                reason = "AiiDA PK %s → %s (exit_status=%s)" % (
                    pk, info.get("state"), info.get("exit_status"))
            # 仍活跃则不改

        if new_status and new_status != status:
            if reason:
                log.append({"ts": now_str(), "level": "accent", "msg": "状态同步 → " + reason})
            conn.execute(
                "UPDATE tasks SET status=?, log=?, finished_at=? WHERE id=?",
                (new_status, json.dumps(log, ensure_ascii=False), now_str(), tid))
            updated += 1
            detail.append("任务 #%d「%s」: %s → %s" % (tid, r["name"], status, new_status))
    return {"updated": updated, "removed": removed, "detail": detail}


def parse_structure_text(content, filename="structure.xyz"):
    """用 ASE 解析结构文件（.cif/.xyz/.gjf/.gaussian/.pdb/.mol/.mol2/.vasp 等）。

    返回标准化结构 dict {symbols, cell, scaled_positions, pbc, positions}，或 (None, err)。
    """
    try:
        from ase.io import read as ase_read
        from io import StringIO
        import numpy as np
    except Exception as e:
        return None, "ASE 不可用: %s" % e

    ext = os.path.splitext(filename)[1].lower().lstrip(".") if filename else ""
    fmt_map = {"cif": "cif", "xyz": "xyz", "gjf": "gaussian-in", "gaussian": "gaussian-in",
               "com": "gaussian-in", "g16": "gaussian-in", "pdb": "pdb", "mol": "mol",
               "mol2": "mol2", "vasp": "vasp", "poscar": "vasp", "contcar": "vasp",
               "in": "gen", "gen": "gen", "sdf": "sdf", "sd": "sdf", "json": "json"}
    try:
        atoms = ase_read(StringIO(content), format=fmt_map.get(ext, "xyz"))
    except Exception:
        # 扩展名不识别或内容不匹配 → 依次尝试常见格式
        atoms = None
        for fmt in ("cif", "xyz", "gaussian-in", "vasp", "gen", "pdb"):
            try:
                atoms = ase_read(StringIO(content), format=fmt)
                break
            except Exception:
                continue
        if atoms is None:
            return None, "无法识别该结构文件格式（支持 .cif/.xyz/.gjf/.pdb/.mol/.vasp 等）"

    if atoms is None or len(atoms) == 0:
        return None, "结构文件为空或未解析出原子"

    cell = np.array(atoms.get_cell())
    positions = np.array(atoms.get_positions())
    pbc = [bool(x) for x in atoms.get_pbc()] if hasattr(atoms, "get_pbc") else [False, False, False]

    # 无晶胞（分子，如 .xyz/.gjf 常见）→ 用 20 Å 立方盒包裹，保证分数坐标可逆
    if np.abs(np.linalg.det(cell)) < 1e-8:
        L = 20.0
        cell = np.eye(3) * L
        pbc = [False, False, False]
    scaled = positions @ np.linalg.inv(cell)

    return {
        "symbols": atoms.get_chemical_symbols(),
        "cell": [[float(round(x, 6)) for x in v] for v in cell],
        "scaled_positions": [[float(round(x, 6)) for x in sp] for sp in scaled],
        "pbc": pbc,
        "positions": [[float(round(x, 6)) for x in p] for p in positions],
        "formula": atoms.get_chemical_formula(),
        "natoms": len(atoms),
    }, None


from catmat_tools.structure_transfer import (structure_xyz, parse_xyz, normalize_pbc,
                                             preserve_cp2k_geometry)


def ase_result_to_xyz(result, comment="CatMat"):
    """Extended XYZ: retain the complete cell, PBC and Cartesian coordinates."""
    try:
        return structure_xyz(result, comment)
    except (KeyError, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Multiwfn CP2K 输入文件生成（真实调用）
# --------------------------------------------------------------------------
# 计算类型 → (Multiwfn 任务编号, CP2K RUN_TYPE, 说明)
# 编号与 RUN_TYPE 均于 2026-09-09 在本机 Multiwfn（cp2k 模块选项 -1）逐项实测确认。
# 注意：Multiwfn 的 11(BSSE) 与 15(XAS) 进入后还需交互式指定片段/原子，无法非交互生成，
#       故不纳入自动映射（如需请在生成后手工补 &BSSE / &XAS_TDP 段落）。
CALC_TYPES = {
    "energy": (1, "ENERGY", "单点能"),
    "energy_force": (2, "ENERGY_FORCE", "能量 + 力"),
    "geo_opt": (3, "GEO_OPT", "结构优化（晶胞固定）"),
    "cell_opt": (4, "CELL_OPT", "结构 + 晶胞优化"),
    "vib": (5, "VIBRATIONAL_ANALYSIS", "振动分析（自动置 PRINT_LEVEL MEDIUM，输出 IR/拉曼所需信息）"),
    "md": (6, "MD", "分子动力学"),
    "ts_dimer": (7, "GEO_OPT", "过渡态搜索（dimer 算法：RUN_TYPE GEO_OPT + &DIMER）"),
    "band": (8, "BAND", "BAND（CI-NEB 等）"),
    "nmr": (9, "LR", "NMR（自动切全电子 6-31G* + GAPW，RUN_TYPE LR）"),
    "polar": (10, "LR", "极化率（RUN_TYPE LR + &LINRES）"),
    "rt_prop": (13, "RT_PROPAGATION", "实时电子动力学传播（RT-TDDFT）"),
    "pimd": (14, "PINT", "路径积分分子动力学（PIMD，RUN_TYPE PINT）"),
}
# 色散 → (Multiwfn 编号, CP2K 关键词)
DISPERSIONS = {
    "none": (0, "无"),
    "d3": (1, "DFT-D3"),
    "d3bj": (2, "DFT-D3(BJ)"),
    "d4": (3, "DFT-D4"),
    "rvv10": (5, "rVV10"),
}
# SCF 求解方法 → 说明（Multiwfn 配置菜单选项 4 切换 Diagonalization / OT）
SCF_METHODS = {
    "diag": ("Diagonalization（对角化）", "金属/带隙体系均适用，需 &MIXING 密度混合"),
    "ot": ("OT（Orbital Transformation）", "非金属、带隙体系推荐；无需对角化，收敛快；不支持 smearing、金属体系收敛差"),
}
# 「add row」可选参数 → 映射到 Multiwfn CP2K 配置菜单的选项号与取值。
#   本表于 2026-09-09 在本机 Multiwfn 3.8(dev) 的 cp2k 模块逐项实测校准：
#   选项号、可选值、默认值、以及由此产生的 CP2K 关键词均与 Multiwfn 实际行为一致。
#
# 字段说明：
#   opt    : Multiwfn 菜单选项号（字符串）
#   parent : 若该参数位于「-9 Other settings」子菜单，则为 "-9"（进入后需发 "0" 返回主菜单）
#   type   : choice=选项号 + 选项值；toggle=仅发选项号；text=选项号 + 用户输入
#   order  : 生成输入序列时的处理顺序（保证如 tddft(15) 先于 tddft_nstates(16)）
#   group  : 前端分组（basic=主菜单常用；output=输出控制；other=其他设置）
#   cp2k   : 该选项在生成的 CP2K 输入文件中对应的关键词（便于用户核对）
#   default: Multiwfn 的默认值
MWF_EXTRA_PARAMS = {
    # ---------------- 主菜单：常用设置 ----------------
    "method": {"label": "理论方法", "opt": "1", "type": "choice", "group": "basic", "order": 10,
               "choices": {"Pade (LDA)": "1", "PBE": "2", "revPBE": "-2", "PBEsol": "-3",
                           "TPSS (LibXC)": "3", "BP86": "4", "BLYP": "5",
                           "PBE0": "6", "PBE0-ADMM": "-6",
                           "B3LYP": "7", "B3LYP-ADMM": "-7",
                           "HSE06": "8", "HSE06-ADMM": "-8",
                           "BHandHLYP": "9", "BHandHLYP-ADMM": "-9",
                           "M06-2X": "10", "M06-2X-ADMM": "-10",
                           "B97M-rV (LibXC)": "11", "MN15L (LibXC)": "12",
                           "SCAN (LibXC)": "13", "r2SCAN (LibXC)": "14",
                           "RPBE (LibXC)": "15", "revTPSS (LibXC)": "16",
                           "BEEF-vdW": "17", "HLE17 (LibXC)": "18",
                           "RI-MP2": "20", "RI-SCS-MP2": "21", "RI-(EXX+RPA)@PBE": "22",
                           "RI-B2PLYP": "25", "RI-B2GP-PLYP": "26", "RI-DSD-BLYP": "27",
                           "RI-revDSD-PBEP86-ADMM": "28",
                           "GFN1-xTB": "30", "GFN0-xTB": "31", "GFN2-xTB (tblite)": "32",
                           "PM6": "40", "SCC-DFTB+disp": "50",
                           "GW@BHandHLYP-ADMM": "60", "GW@MN15L": "61",
                           "PBEh": "80", "PBEh-ADMM": "-80",
                           "FIST (分子力学)": "100"},
               "default": "PBE", "cp2k": "&XC / &XC_FUNCTIONAL / &HF、&KIND POTENTIAL",
               "hint": "杂化泛函（PBE0/B3LYP/HSE06 等）会自动加 &HF 与 &SCREENING；"
                       "带 -ADMM 的编号使用 ADMM 近似加速杂化计算；"
                       "选 0 可切换 RI-HFX（杂化泛函用 RI 加速）"},
    "basis": {"label": "基组 + 赝势", "opt": "2", "type": "choice", "group": "basic", "order": 20,
              "choices": {"QZV3P-GTH": "-6", "QZV2P-GTH": "-5", "TZV2P-GTH": "-4",
                          "TZVP-GTH": "-3", "DZVP-GTH": "-2", "SZV-GTH": "-1",
                          "SZV-MOLOPT-SR-GTH": "1", "DZVP-MOLOPT-SR-GTH": "2",
                          "TZVP-MOLOPT-GTH": "3", "TZV2P-MOLOPT-GTH": "4",
                          "TZV2PX-MOLOPT-GTH": "5",
                          "ccGRB-D": "7", "ccGRB-T": "8", "ccGRB-Q": "9",
                          "cc-DZ (RI_DZ)": "20", "cc-TZ (RI_TZ)": "21", "cc-QZ (RI_QZ)": "22",
                          "6-31G*": "10", "6-311G**": "11",
                          "Ahlrichs-def2-TZVP": "12", "pob-TZVP": "13",
                          "pob-DZVP-rev2": "14", "pob-TZVP-rev2": "15",
                          "Ahlrichs-def2-QZVP": "16",
                          "def2-QZVP (RI_5Z)": "19"},
              "default": "DZVP-MOLOPT-SR-GTH",
              "cp2k": "&KIND BASIS_SET / POTENTIAL、BASIS_SET_FILE_NAME",
              "hint": "赝势基组配 GTH-PBE 类赝势；选全电子基组会自动置 POTENTIAL ALL、"
                      "BASIS_SET_FILE_NAME EMSL_BASIS_SETS 并改用 METHOD GAPW"},
    "mixing": {"label": "密度混合方式", "opt": "5", "type": "choice", "group": "basic", "order": 30,
               "choices": {"Direct(DIIS)": "1", "Broyden": "2", "Pulay": "3"},
               "default": "Broyden", "cp2k": "&SCF &MIXING METHOD",
               "hint": "仅对角化（Diagonalization）求解时有 &MIXING；切到 OT 后无 &MIXING"},
    "kpoints": {"label": "k 点网格", "opt": "8", "type": "text", "group": "basic", "order": 40,
                "hint": "Monkhorst-Pack 三方向，如 4,4,1（默认 GAMMA only，空表示不设置）",
                "default": "", "cp2k": "&DFT &KPOINTS SCHEME MONKHORST-PACK nx ny nz"},
    "solvation": {"label": "SCCS 隐式溶剂", "opt": "7", "type": "toggle", "group": "basic",
                  "order": 50, "default": "No", "cp2k": "&DFT &SCCS（ALPHA/BETA/GAMMA 等）",
                  "hint": "自洽连续介质溶剂化模型，默认关闭"},
    "smearing_toggle": {"label": "Smearing（费米-狄拉克展宽）", "opt": "6", "type": "toggle",
                        "group": "basic", "order": 60, "default": "No",
                        "cp2k": "&SCF &SMEAR METHOD FERMI_DIRAC, ELECTRONIC_TEMPERATURE 300 K + ADDED_MOS 30",
                        "hint": "金属/窄带隙体系收敛用；OT 求解器不支持（与顶层 smearing 参数等效，二者选一）"},
    "tddft": {"label": "TDDFT 激发态计算", "opt": "15", "type": "toggle", "group": "basic",
              "order": 70, "default": "No", "cp2k": "&PROPERTIES &TDDFPT（Tamm-Dancoff）",
              "hint": "开启后可用下方「激发态数目」「三重态」两项细化；默认 3 个单重态"},
    "tddft_nstates": {"label": "TDDFT 激发态数目", "opt": "16", "type": "text", "group": "basic",
                      "order": 71, "default": "3", "cp2k": "&TDDFPT NSTATES",
                      "hint": "需先开启 TDDFT（选项 15）；默认 3"},
    "tddft_triplet": {"label": "TDDFT 计算三重态", "opt": "17", "type": "toggle", "group": "basic",
                      "order": 72, "default": "No", "cp2k": "&TDDFPT RKS_TRIPLETS T",
                      "hint": "需先开启 TDDFT；在 Singlet / Triplet 间切换"},
    "tddft_stda": {"label": "TDDFT 用 sTDA 近似", "opt": "18", "type": "toggle", "group": "basic",
                   "order": 73, "default": "No", "cp2k": "&TDDFPT（sTDA 核，大幅降低大体系成本）",
                   "hint": "需先开启 TDDFT；sTDA 近似适合大体系快速估算激发能"},
    "tddft_soc": {"label": "TDDFT 考虑自旋轨道耦合", "opt": "21", "type": "toggle", "group": "basic",
                  "order": 74, "default": "No", "cp2k": "&TDDFPT + SOC 相关设置",
                  "hint": "需先开启 TDDFT；含重元素体系考虑 SOC"},
    # ---------------- 主菜单：输出控制 ----------------
    "molden": {"label": "导出 .molden 波函数文件", "opt": "-2", "type": "toggle", "group": "output",
               "order": 80, "default": "No", "cp2k": "&DFT &PRINT &MO_MOLDEN (NDIGITS 9, WRITE_PSEUDO T)",
               "hint": "强烈建议开启：生成的 .molden 可直接作为 Multiwfn 输入做波函数分析"},
    "cube": {"label": "导出 cube 文件（实空间函数）", "opt": "-3", "type": "choice", "group": "output",
             "order": 85,
             "choices": {"不导出": "0", "仅 HOMO/LUMO（并打印能级与能隙）": "-1",
                         "电子密度（含自旋密度）": "1", "ELF 电子定域化函数": "2",
                         "交换相关势": "3", "Hartree 势（静电势负值）": "4",
                         "电场各分量": "5", "分子轨道": "6",
                         "电子密度 + Hartree 势": "7"},
             "default": "不导出", "cp2k": "&DFT &PRINT &E_DENSITY_CUBE / &ELF_CUBE / &MO_CUBES / &V_HARTREE_CUBE 等",
             "hint": "选「分子轨道」时请在下方填写 NHOMO,NLUMO（如 1,1）"},
    "cube_mo": {"label": "cube 分子轨道范围", "opt": "", "type": "text", "group": "output",
                "order": 86, "default": "", "cp2k": "&MO_CUBES NHOMO / NLUMO",
                "hint": "格式 NHOMO,NLUMO（如 1,1）；仅在上面选择「分子轨道」时生效"},
    "atomic_charge": {"label": "输出原子电荷", "opt": "-4", "type": "choice", "group": "output",
                      "order": 90,
                      "choices": {"不计算": "0", "Mulliken": "1", "Löwdin": "2", "Hirshfeld": "3",
                                  "Hirshfeld-I": "4", "Voronoi": "5", "RESP": "6", "REPEAT": "7"},
                      "default": "不计算", "cp2k": "&DFT &PRINT &MULLIKEN / &HIRSHFELD / &RESP 等",
                      "hint": "周期性体系推荐 Hirshfeld；注：CP2K 的 Hirshfeld-I 实现并不严格"},
    "pbc": {"label": "周期性方向", "opt": "-7", "type": "text", "group": "output", "order": 95,
            "default": "XYZ", "cp2k": "&CELL A/B/C、&POISSON PERIODIC",
            "hint": "xyz / xy / x / yz ...；缺省即 XYZ 三维周期（孤立分子可置 NONE）"},
    # ---------------- 「-9 Other settings」子菜单 ----------------
    "net_charge": {"label": "净电荷", "opt": "1", "parent": "-9", "type": "text", "group": "other",
                   "order": 100, "default": "0", "cp2k": "&DFT CHARGE",
                   "hint": "整数，如 -1 / 0 / 2"},
    "multiplicity": {"label": "自旋多重度", "opt": "2", "parent": "-9", "type": "text",
                     "group": "other", "order": 105, "default": "1",
                     "cp2k": "&DFT MULTIPLICITY（>1 时自动置 UKS）",
                     "hint": "1=单重态，2=二重态，3=三重态…"},
    "cell_repeat": {"label": "晶胞重复倍数", "opt": "3", "parent": "-9", "type": "text",
                    "group": "other", "order": 110, "default": "1 1 1",
                    "cp2k": "&TOPOLOGY MULTIPLE_UNIT_CELL nx ny nz",
                    "hint": "三方向重复数，用逗号或空格分隔，如 2,2,1（建超胞）"},
    "finer_xc_grid": {"label": "更精细的 XC 积分网格", "opt": "4", "parent": "-9", "type": "toggle",
                      "group": "other", "order": 115, "default": "No",
                      "cp2k": "&MGRID &XC_GRID USE_FINER_GRID T",
                      "hint": "提高交换相关项积分精度，代价更慢"},
    "cutoff": {"label": "平面波截断能", "opt": "5", "parent": "-9", "type": "text", "group": "other",
               "order": 120, "default": "350,50", "cp2k": "&MGRID CUTOFF / REL_CUTOFF (Ry)",
               "hint": "格式 CUTOFF,REL_CUTOFF，如 400,60；默认 350,50"},
    "moments": {"label": "输出电/磁偶极矩", "opt": "6", "parent": "-9", "type": "toggle",
                "group": "other", "order": 125, "default": "No",
                "cp2k": "&DFT &PRINT &MOMENTS（PERIODIC/MAGNETIC/REFERENCE/MAX_MOMENT）",
                "hint": "周期性体系偶极建议用 Berry phase（Multiwfn 在非周期时置 PERIODIC F）"},
    "dft_plus_u": {"label": "DFT+U", "opt": "8", "parent": "-9", "type": "toggle", "group": "other",
                   "order": 130, "default": "No",
                   "cp2k": "PLUS_U_METHOD MULLIKEN + &KIND 内注释形式的 &DFT_PLUS_U 模板",
                   "hint": "开启后 Multiwfn 只写注释模板，需手工在 &KIND 中填 L 与 U_MINUS_J"},
    "print_level": {"label": "输出信息级别", "opt": "10", "parent": "-9", "type": "choice",
                    "group": "other", "order": 135,
                    "choices": {"Low": "1", "Medium": "2", "High": "3"},
                    "default": "Low", "cp2k": "&GLOBAL PRINT_LEVEL",
                    "hint": "Medium 会打印更多信息（如轨道占据）；振动分析任务 Multiwfn 会自动设为 Medium"},
    "efield": {"label": "外电场", "opt": "11", "parent": "-9", "type": "text", "group": "other",
               "order": 140, "default": "",
               "cp2k": "&DFT &EFIELD INTENSITY / POLARISATION",
               "hint": "格式 x,y,z（a.u.），如 0,0,0.01；Multiwfn 会换算为强度 + 单位方向"},
    "added_mos": {"label": "额外求解虚轨道数", "opt": "12", "parent": "-9", "type": "text",
                  "group": "other", "order": 145, "default": "0", "cp2k": "&SCF ADDED_MOS",
                  "hint": "空轨道数；开 smearing 时 Multiwfn 自动置 30"},
    "center_coords": {"label": "坐标居中于盒子", "opt": "13", "parent": "-9", "type": "toggle",
                      "group": "other", "order": 150, "default": "No",
                      "cp2k": "&TOPOLOGY &CENTER_COORDINATES", "hint": "把体系移到晶胞中心"},
    "print_mo": {"label": "SCF 后打印轨道能级与占据", "opt": "14", "parent": "-9", "type": "toggle",
                 "group": "other", "order": 155, "default": "No",
                 "cp2k": "&DFT &PRINT &MO ENERGIES T / OCCUPATION_NUMBERS T",
                 "hint": "做能带/DOS 分析或检查占据时有用"},
    "overlap_csr": {"label": "输出重叠矩阵 .csr", "opt": "15", "parent": "-9", "type": "toggle",
                    "group": "other", "order": 160, "default": "No",
                    "cp2k": "&DFT &PRINT &S_CSR_WRITE", "hint": "供外部程序（如 Multiwfn/COHP）读取"},
    "ks_matrix_csr": {"label": "输出 Kohn-Sham 矩阵 .csr", "opt": "16", "parent": "-9",
                      "type": "toggle", "group": "other", "order": 165, "default": "No",
                      "cp2k": "&DFT &PRINT &K_CSR_WRITE（推测，请以实际生成结果核对）",
                      "hint": "与重叠矩阵配套输出"},
    "epr": {"label": "计算 EPR 超精细耦合张量", "opt": "18", "parent": "-9", "type": "toggle",
            "group": "other", "order": 170, "default": "No",
            "cp2k": "&DFT &PRINT &HYPERFINE_COUPLING_TENSOR", "hint": "需全电子基组（GAPW）"},
    "lrigpw": {"label": "LRIGPW 加速（替代 GPW）", "opt": "20", "parent": "-9", "type": "toggle",
               "group": "other", "order": 175, "default": "No",
               "cp2k": "&QS METHOD LRIGPW + BASIS_SET LRI_AUX + &LRIGPW",
               "hint": "大体系加速；精度略降"},
    "ls_scf": {"label": "线性标度 SCF（LS_SCF）", "opt": "21", "parent": "-9", "type": "toggle",
               "group": "other", "order": 180, "default": "No",
               "cp2k": "&DFT LS_SCF + &LS_SCF（替换常规 &SCF）",
               "hint": "超大体系用；会移除 &SCF / &DIAGONALIZATION / &MIXING"},
}
# 前端分组展示顺序与标题
MWF_PARAM_GROUPS = [
    {"key": "basic", "label": "常用设置", "desc": "方法 / 基组 / 混合 / k 点 / 溶剂化 / 展宽 / TDDFT"},
    {"key": "output", "label": "输出控制", "desc": "molden 波函数、cube 文件、原子电荷、周期性方向"},
    {"key": "other", "label": "其他设置（-9）", "desc": "电荷 / 自旋 / 截断能 / DFT+U / 电场 / 打印级别等"},
]


def _mwf_choice_value(spec, val):
    """把用户传入的 choice 值解析为 Multiwfn 菜单编号字符串。

    容错顺序：精确匹配 → 去括号后缀（"TPSS (LibXC)"→"TPSS"、"6-31G*（全电子）"→"6-31G*"）
    → 忽略大小写 → 允许直接传编号（如 "2"、"-3"）。无法解析返回 None（由调用方报 400）。
    """
    choices = spec.get("choices") or {}
    v = str(val).strip()
    if v in choices:
        return choices[v]

    def _strip(x):
        return re.sub(r"[（(].*?[)）]", "", str(x)).strip()

    for k in choices:
        if _strip(k) == _strip(v):
            return choices[k]
    low = v.lower()
    for k in choices:
        if k.lower() == low or _strip(k).lower() == low:
            return choices[k]
    if re.fullmatch(r"-?\d+", v) and v in set(choices.values()):
        return v
    return None


def generate_multiwfn_inp(structure_text, calc_type="energy", dispersion="none",
                          smearing=False, pbc=None, scf_method="diag", extra=None, cell=None):
    """用 Multiwfn 非交互式生成 CP2K 输入文件。structure_text 为 xyz 内容。

    extra：可选参数字典 {参数key: 值}，映射到 MWF_EXTRA_PARAMS 的 Multiwfn 菜单选项。
    返回 (inp_content, err)。
    """
    if not os.path.isfile(MULTIWFN_BIN):
        return None, "未找到 Multiwfn: " + MULTIWFN_BIN
    if not structure_text or not structure_text.strip():
        return None, "结构为空，请先提供 xyz 结构"

    task = CALC_TYPES.get(calc_type)
    if task is None:
        return None, "不支持的计算类型: " + str(calc_type)
    disp = DISPERSIONS.get(dispersion)
    if disp is None:
        return None, "不支持的色散校正: " + str(dispersion)

    try:
        geometry = parse_xyz(structure_text, cell)
        pbc = normalize_pbc((extra or {}).get("pbc") or pbc or geometry["pbc"] or "xyz")
        if pbc != "none" and geometry["cell"] is None:
            return None, "周期结构缺少晶胞：请传入含 Lattice 的 XYZ 或 cell 3×3 矩阵（Å），禁止按原子包围盒猜测晶胞"
    except (TypeError, ValueError) as exc:
        return None, str(exc)

    # 临时工作目录
    token = time.strftime("%Y%m%d%H%M%S") + "_" + str(os.getpid()) + "_" + str(random.randint(1000, 9999))
    wd = os.path.join(TMP_DIR, token)
    os.makedirs(wd, exist_ok=True)
    xyz_path = os.path.join(wd, "structure.xyz")
    inp_path = os.path.join(wd, "structure.inp")
    try:
        with open(xyz_path, "w", encoding="utf-8") as f:
            f.write(structure_text.strip() + "\n")
    except Exception as e:
        return None, "写临时结构失败: %s" % e

    # 构造 Multiwfn 输入序列（cp2k → 输出路径 → 配置 → 0 生成 → -10 返回 → q 退出）
    seq = ["cp2k", inp_path]
    seq += ["-1", str(task[0])]                       # 任务类型
    seq += ["3", str(disp[0])]                        # 色散校正
    if scf_method == "ot":
        seq += ["4"]                                   # 选项 4：切换为 OT（Orbital Transformation）
    if smearing and scf_method != "ot":
        seq += ["6"]                                   # 开启展宽（FERMI_DIRAC）——OT 不支持 smearing

    # 「add row」可选参数：按 MWF_EXTRA_PARAMS 映射插入对应菜单选项 + 取值。
    # 处理顺序：按 spec["order"] 升序（保证 tddft(15) → nstates(16) → triplet(17) 这类依赖顺序）；
    # 带 parent（如 "-9"）的参数需先进入子菜单、操作后再发 "0" 返回主菜单。
    items = [(k, v) for k, v in (extra or {}).items() if MWF_EXTRA_PARAMS.get(k)]
    items.sort(key=lambda kv: MWF_EXTRA_PARAMS[kv[0]].get("order", 999))
    # 实测交互语义（2026-09-09）：
    #  · 选 cube=6（分子轨道）后，Multiwfn 立即要求在同一行输入 NHOMO,NLUMO；
    #  · 开启 TDDFT（选项 15）后，Multiwfn 立即要求输入激发态数目（默认 3），
    #    而不是通过选项 16——因此 15 之后必须紧跟一个数字，否则后续指令会被误读。
    _ex = extra or {}
    cube_mo_val = str(_ex.get("cube_mo", "")).strip() or None
    tddft_n = str(_ex.get("tddft_nstates", "")).strip() or None
    tddft_on = any(str(v).lower() in ("1", "true", "on", "yes") for v in [_ex.get("tddft")])
    for key, val in items:
        spec = MWF_EXTRA_PARAMS[key]
        if val is None or str(val).strip() == "":
            continue
        opt = spec["opt"]
        parent = spec.get("parent")
        # cube 分子轨道范围单独延后处理（需紧跟在 cube=6 之后输入）
        if key == "cube_mo":
            cube_mo_val = str(val).strip()
            continue
        if key == "pbc":
            # 与顶层 pbc 参数二选一：此处优先（顶层仅在未指定时生效，见下方）
            if str(val).strip().lower() != "xyz":
                seq += ["-7", str(val).strip().lower()]
            continue
        body = []
        if key == "tddft":
            # 实测：开启 TDDFT（选项 15）后 Multiwfn 会立即读取一个数字，
            # 必须紧跟激发态数目（默认 3），否则后续的 0/-10 会被误读为输入而无法生成文件。
            if str(val).lower() in ("1", "true", "on", "yes"):
                body = [opt, tddft_n or "3"]
        elif key == "tddft_nstates":
            # 数目已随 tddft 一并下发；且生成后会再按文本精确校准（见函数末尾）
            continue
        elif spec["type"] == "toggle":
            if str(val).lower() in ("1", "true", "on", "yes"):
                body = [opt]
        elif spec["type"] == "choice":
            v = _mwf_choice_value(spec, val)
            if v is None:
                return None, "参数「%s」取值无效：%s（可选：%s）" % (
                    spec["label"], val, "、".join(spec["choices"].keys()))
            body = [opt, v]
            if key == "cube" and str(v) == "6" and cube_mo_val:
                # Multiwfn 分两行读取 NHOMO 与 NLUMO，故按逗号/空格拆成两项
                body += [x for x in re.split(r"[,\s]+", cube_mo_val.strip()) if x != ""]
        else:  # text
            body = [opt, str(val).strip()]
        if not body:
            continue
        if parent:
            seq += [parent] + body + ["0"]             # 进入子菜单 → 设置 → 返回
        else:
            seq += body
    # 顶层 pbc（兼容旧调用）：仅当 extra 未指定 pbc 时生效
    if "pbc" not in (extra or {}) and pbc and pbc.lower() != "xyz":
        seq += ["-7", pbc.lower()]
    seq += ["0", "-10", "q"]
    stdin_data = "\n".join(seq) + "\n"

    env = dict(os.environ)
    env["OMP_STACKSIZE"] = "256M"
    env["OMP_NUM_THREADS"] = "1"
    env.setdefault("MULTIWFNPATH", os.path.dirname(MULTIWFN_BIN))
    try:
        with _blocking_unlocked():
            mw_out, mw_err = run_checked([MULTIWFN_BIN, xyz_path], input_text=stdin_data,
                                         timeout=120, cwd=wd, env=env)
    except subprocess.TimeoutExpired:
        return None, "Multiwfn 生成超时（>120s）"
    except Exception as e:
        return None, "Multiwfn 调用失败: %s" % e

    if not os.path.isfile(inp_path):
        # 尝试从 stdout 定位是否报错
        tail = (mw_out or mw_err or "")[-300:]
        return None, "未生成输入文件" + (("：" + tail.strip()[-200:]) if tail.strip() else "")

    with open(inp_path, "r", encoding="utf-8", errors="ignore") as f:
        inp = f.read()
    # TDDFT 激发态数：Multiwfn 选项 16 在管道模式下不生效（实测始终为默认 3），
    # 因此在此按文本精确改写，确保生成结果与用户设定一致（可追溯、可核对）。
    if tddft_n and "&TDDFPT" in inp:
        inp = re.sub(r"(NSTATES\s+)(\d+)", lambda m: m.group(1) + tddft_n, inp, count=1)
    # 周期体系后处理：Multiwfn 默认只写 NDIGITS/WRITE_PSEUDO，不带晶胞信息；
    # 而手册 2.9.2 要求周期性体系在 &MO_MOLDEN 中加 WRITE_CELL T（CP2K >= 2026.2），
    # 否则导出的 .molden 缺少 [Cell] 字段，Multiwfn 无法按周期波函数做分析。
    eff_pbc = str((extra or {}).get("pbc") or pbc or "xyz").strip().lower()
    if "&MO_MOLDEN" in inp and eff_pbc not in ("none", "n", "0", "off"):
        inp = re.sub(
            r"(\n([ \t]*)&MO_MOLDEN\b[^\n]*)",
            lambda m: m.group(1) + "\n" + m.group(2) +
                      "  WRITE_CELL T #（平台补充，CP2K>=2026.2）把晶胞写入 .molden，供 Multiwfn 周期波函数分析",
            inp, count=1)
    try:
        inp = preserve_cp2k_geometry(inp, geometry, pbc)
    except ValueError as exc:
        return None, "生成几何一致性检查失败: " + str(exc)
    return inp, None


# --------------------------------------------------------------------------
# CP2K 基组/赝势数据文件检索（data/ 目录，供输入推荐下拉选择）
# --------------------------------------------------------------------------
_ELEMENT_RE = re.compile(r"^[A-Z][a-z]?$")

_DATA_FILES_CACHE = {"mtime": None, "data": None}


def _cp2k_data_file_names():
    """扫描 CP2K data 目录，分类返回基组文件与赝势文件清单。按目录 mtime 缓存。"""
    try:
        mt = os.path.getmtime(CP2K_DATA_DIR)
    except Exception:
        mt = None
    if _DATA_FILES_CACHE["data"] is not None and _DATA_FILES_CACHE["mtime"] == mt:
        return _DATA_FILES_CACHE["data"]

    basis, potential = [], []
    if os.path.isdir(CP2K_DATA_DIR):
        for fn in sorted(os.listdir(CP2K_DATA_DIR)):
            # 跳过带版本后缀的符号链接目标（如 BASIS_MOLOPT_UZH_2026.2），
            # 保留干净的符号链接名（BASIS_MOLOPT_UZH / POTENTIAL_UZH）。
            if fn.endswith("_2026.1") or fn.endswith("_2026.2"):
                continue
            if os.path.isdir(os.path.join(CP2K_DATA_DIR, fn)):
                continue
            if fn.startswith("BASIS") or fn in ("GTH_BASIS_SETS", "ALL_BASIS_SETS",
                                                "EMSL_BASIS_SETS", "NUCLEAR_BASIS_SETS", "HFX_BASIS"):
                basis.append(fn)
            elif "POTENTIAL" in fn.upper() or fn.endswith("_POTENTIALS"):
                potential.append(fn)
    result = {"basis_files": basis, "potential_files": potential}
    _DATA_FILES_CACHE["mtime"] = mt
    _DATA_FILES_CACHE["data"] = result
    return result


def _parse_basis_file(name):
    """解析基组文件 → {元素: [基组名]}。条目格式：<元素> <基组名> [别名…]。"""
    mapping = {}
    path = os.path.join(CP2K_DATA_DIR, name)
    if not os.path.isfile(path):
        return mapping
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2 and _ELEMENT_RE.match(parts[0]) and parts[1][:1].isupper():
                mapping.setdefault(parts[0], [])
                if parts[1] not in mapping[parts[0]]:
                    mapping[parts[0]].append(parts[1])
    return mapping


def _parse_potential_file(name):
    """解析赝势文件 → {元素: [赝势名]}。条目格式：<元素> <赝势名> [别名…]。"""
    mapping = {}
    path = os.path.join(CP2K_DATA_DIR, name)
    if not os.path.isfile(path):
        return mapping
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2 and _ELEMENT_RE.match(parts[0]) and parts[1][:1].isupper():
                mapping.setdefault(parts[0], [])
                if parts[1] not in mapping[parts[0]]:
                    mapping[parts[0]].append(parts[1])
    return mapping


def _pick_basis(names):
    """从某元素可用基组中挑选推荐默认：优先 DZVP（排除 SCAN/PBE0/HYB/MGGA 专用），其次 SZV/SVP。"""
    if not names:
        return None
    dz = [n for n in names if "DZVP" in n]
    if dz:
        plain = [n for n in dz if not any(k in n for k in ("SCAN", "PBE0", "HYB", "MGGA", "HSE", "B3LYP"))]
        return (plain[0] if plain else dz[0])
    sz = [n for n in names if "SZV" in n or "SVP" in n]
    if sz:
        return sz[0]
    return names[0]


def _pick_potential(names, basis_name=None):
    """挑选推荐赝势：优先 GTH-PBE-qN（PBE GGA 默认），并尽量与基组价电子数（-qNN）匹配。"""
    if not names:
        return None
    # 从基组名提取价电子数（如 DZVP-MOLOPT-PBE-GTH-q17 → q17）
    q = None
    if basis_name:
        m = re.search(r"-q(\d+)\b", basis_name)
        if m:
            q = m.group(1)
    # 与价电子匹配的 PBE 赝势最优先
    pbe = [n for n in names if "PBE" in n and not any(k in n for k in ("PBE0", "SCAN", "BLYP", "BP", "OLYP", "HCTH", "HF", "NLCC"))]
    if q:
        matched = [n for n in pbe if re.search(r"-q%s\b" % q, n)]
        if matched:
            return matched[0]
    if pbe:
        return pbe[0]
    return names[0]


def resolve_cp2k_kinds(basis_file, potential_file, elements):
    """根据所选基组/赝势文件名 + 元素列表，解析出每个元素推荐的基组名与赝势名。"""
    bmap = _parse_basis_file(basis_file)
    pmap = _parse_potential_file(potential_file)
    out = {}
    for el in (elements or []):
        el = el.strip()
        if not el or not _ELEMENT_RE.match(el):
            continue
        bnames = bmap.get(el, [])
        pnames = pmap.get(el, [])
        basis = _pick_basis(bnames)
        out[el] = {
            "basis": basis,
            "potential": _pick_potential(pnames, basis),
            "basis_options": bnames,
            "potential_options": pnames,
        }
    return out


def _all_kinds_catalog(elements):
    """跨所有基组/赝势文件，构建逐元素可用的基组/赝势选项（含来源文件名）+ 推荐默认。

    供前端"按元素逐个设置"模式使用：每个元素可独立选择不同文件、不同计算级别的基组/赝势。
    返回 {元素: {basis, basis_file, potential, potential_file,
                basis_options: [{name, file}], potential_options: [{name, file}]}}
    """
    files = _cp2k_data_file_names()
    basis_by_el, pot_by_el = {}, {}

    def _add(dst, el, name, fn):
        lst = dst.setdefault(el, [])
        if not any(x[0] == name and x[1] == fn for x in lst):
            lst.append((name, fn))

    # 主流文件排前，辅助/特殊文件（ADMM/RI/TB/EMSL 等）排后，保证下拉默认先看到常用库
    def _rank(fn, kind):
        pref = (["BASIS_MOLOPT", "BASIS_MOLOPT_UZH", "BASIS_MOLOPT_UCL", "BASIS_MOLOPT_OPT"]
                if kind == "basis"
                else ["POTENTIAL", "GTH_POTENTIALS", "POTENTIAL_UZH", "ECP_POTENTIALS"])
        try:
            return pref.index(fn)
        except ValueError:
            mainstream = fn.startswith("BASIS_MOLOPT") or fn.startswith("POTENTIAL") or fn.startswith("GTH")
            return len(pref) + (10 if mainstream else 20)

    basis_files = sorted(files.get("basis_files", []), key=lambda f: _rank(f, "basis"))
    pot_files = sorted(files.get("potential_files", []), key=lambda f: _rank(f, "potential"))
    for fn in basis_files:
        for el, names in _parse_basis_file(fn).items():
            for n in names:
                _add(basis_by_el, el, n, fn)
    for fn in pot_files:
        for el, names in _parse_potential_file(fn).items():
            for n in names:
                _add(pot_by_el, el, n, fn)

    # 以统一模式（BASIS_MOLOPT + POTENTIAL）的推荐作为逐元素默认值
    defaults = resolve_cp2k_kinds("BASIS_MOLOPT", "POTENTIAL", elements or [])

    out = {}
    for el in (elements or []):
        el = str(el).strip()
        if not el or not _ELEMENT_RE.match(el):
            continue
        bopts = basis_by_el.get(el, [])
        popts = pot_by_el.get(el, [])
        d = defaults.get(el, {})
        def_b = d.get("basis")
        # 默认文件优先对齐统一模式（BASIS_MOLOPT / POTENTIAL），找不到再回退到首个可用项
        def_b_file = next((f for n, f in bopts if n == def_b and f == "BASIS_MOLOPT"),
                          next((f for n, f in bopts if n == def_b),
                               (bopts[0][1] if bopts else None)))
        def_p = d.get("potential")
        def_p_file = next((f for n, f in popts if n == def_p and f == "POTENTIAL"),
                          next((f for n, f in popts if n == def_p),
                               (popts[0][1] if popts else None)))
        out[el] = {
            "basis": def_b,
            "basis_file": def_b_file,
            "potential": def_p,
            "potential_file": def_p_file,
            "basis_options": [{"name": n, "file": f} for n, f in bopts],
            "potential_options": [{"name": n, "file": f} for n, f in popts],
        }
    return out



def resolve_run_dir(workdir):
    """解析任务工作目录：允许相对路径（基于 CP2K_RUN_DIR）或 CP2K_RUN_DIR 下的绝对路径。

    为防止任意路径写入，绝对路径必须落在 CP2K_RUN_DIR 内。
    返回 (abs_path, err)。
    """
    base = os.path.normpath(CP2K_RUN_DIR)
    if not workdir:
        return base, None
    wd = str(workdir).strip()
    if not os.path.isabs(wd):
        wd = os.path.join(base, wd)
    wd = os.path.normpath(wd)
    if not (wd == base or wd.startswith(base + os.sep)):
        return None, "工作目录必须位于 %s 内（当前：%s）" % (base, wd)
    return wd, None


_CP2K_ENV_CACHE = {"mtime": None, "env": None}


def cp2k_runtime_env():
    """构造 CP2K 运行环境：source cp2k_env 后导出完整环境（PATH / LD_LIBRARY_PATH …）。

    CP2K 的 MPI 是编译时随 toolchain 一并装好的（OpenMPI），因此**不单独指定 mpirun
    路径**，而是与 ~/.bashrc 一致地 `source cp2k_env`，让 mpirun / cp2k.psmp 与全部
    依赖库从 PATH 解析。这样 mpirun 为 OpenMPI（支持 `-x OMP_NUM_THREADS=...`）。
    结果按 cp2k_env 的 mtime 缓存，避免每次提交都重启一次 bash。
    """
    try:
        mt = os.path.getmtime(CP2K_ENV) if os.path.isfile(CP2K_ENV) else None
    except Exception:
        mt = None
    if _CP2K_ENV_CACHE["env"] is not None and _CP2K_ENV_CACHE["mtime"] == mt:
        return dict(_CP2K_ENV_CACHE["env"])

    env = dict(os.environ)
    sourced = False
    if os.path.isfile(CP2K_ENV):
        try:
            with _blocking_unlocked():
                dump = subprocess.run(
                    ["bash", "-c", "source %s >/dev/null 2>&1; env -0" % CP2K_ENV],
                    capture_output=True, timeout=30)
            if dump.returncode == 0 and dump.stdout:
                new_env = {}
                for item in dump.stdout.decode("utf-8", "replace").split("\0"):
                    if not item:
                        continue
                    k, _, v = item.partition("=")
                    if k:
                        new_env[k] = v
                if new_env.get("PATH"):
                    env = new_env
                    sourced = True
        except Exception:
            pass
    if not sourced:
        # 兜底：手工补齐 PATH / LD_LIBRARY_PATH
        env["PATH"] = os.path.dirname(CP2K_PSMP) + os.pathsep + env.get("PATH", "")
        env["LD_LIBRARY_PATH"] = CP2K_LIB + ":" + env.get("LD_LIBRARY_PATH", "")

    _CP2K_ENV_CACHE["mtime"] = mt
    _CP2K_ENV_CACHE["env"] = dict(env)
    return dict(env)


def resolve_mpirun(env):
    """从环境 PATH 解析 mpirun，并识别 MPI 实现。

    返回 (mpirun_path, flavor, err)；flavor ∈ {'openmpi', 'mpich', 'unknown'}。
    OpenMPI 用 `-x K=V` 传递环境变量，MPICH / Intel MPI 用 `-env K V`。
    """
    path = shutil.which("mpirun", path=env.get("PATH") or os.environ.get("PATH", ""))
    if not path:
        return None, None, ("未在 PATH 中找到 mpirun。请确认 %s 存在且 source 后可提供 OpenMPI"
                            "（当前 PATH 前 200 字符：%s）" % (CP2K_ENV, (env.get("PATH") or "")[:200]))
    flavor = "unknown"
    try:
        with _blocking_unlocked():
            v = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15)
        out = (v.stdout or "") + (v.stderr or "")
        low = out.lower()
        if "open mpi" in low or "openrte" in low or "prte" in low:
            flavor = "openmpi"
        elif "mpich" in low or "intel(r) mpi" in low:
            flavor = "mpich"
    except Exception:
        pass
    return path, flavor, None


def build_mpi_env_args(flavor, key, value):
    """按 MPI 实现生成传递环境变量的参数（OpenMPI: `-x K=V`；MPICH/Intel: `-env K V`）。"""
    if flavor == "mpich":
        return ["-env", key, value]
    return ["-x", "%s=%s" % (key, value)]


def submit_cp2k_direct(inp_text, name, nproc=DEFAULT_NPROC, omp_threads=DEFAULT_OMP_THREADS,
                       workdir=None, conn=None):
    """独立 CP2K 任务：参照 run.sh 用 mpirun 直接并行提交。

    命令等价于：
        nohup mpirun -np N -x OMP_NUM_THREADS=1 cp2k.psmp -i x.inp -o x.out >/dev/null &
    workdir：输入输出文件存放目录（相对路径基于 cp2k_runs/，或该目录下的绝对路径）。
    返回 (data, err)。
    """
    if not inp_text or not inp_text.strip():
        return None, "输入文件内容为空"
    try:
        nproc = max(1, int(nproc))
        omp_threads = max(1, int(omp_threads))
    except (TypeError, ValueError):
        return None, "nproc / omp_threads 需为整数"

    own_conn = None
    try:
        if conn is None:
            own_conn = get_db()
            conn = own_conn
        decision = _governor_check(conn, nproc, omp_threads)
        blocked = _governor_should_block(
            conn, decision, nproc, omp_threads, "submit_cp2k_direct")
    finally:
        if own_conn is not None:
            own_conn.close()
    if blocked:
        if decision["action"] == "QUEUE":
            return None, "GOVERNOR_QUEUE: %s" % decision["message"]
        return None, "%s: %s" % (decision["code"], decision["message"])

    # 运行环境：source cp2k_env（MPI 与 CP2K 均为编译时一并装好，不单独指定路径）
    env = cp2k_runtime_env()
    mpirun, mpi_flavor, merr = resolve_mpirun(env)
    if merr:
        return None, merr
    # cp2k.psmp 优先从 cp2k_env 提供的 PATH 解析，回退到硬编码路径
    cp2k_bin = shutil.which("cp2k.psmp", path=env.get("PATH", ""))
    if not cp2k_bin:
        if os.path.isfile(CP2K_PSMP):
            cp2k_bin = CP2K_PSMP
        else:
            return None, "未找到 cp2k.psmp（已 source %s，PATH 中仍无）" % CP2K_ENV
    # 安全化任务名（避免路径注入）
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name or "cp2k_task"))[:80] or "cp2k_task"
    # 工作目录：指定则用之（输入输出文件直接放该目录），否则默认 cp2k_runs/<任务名>
    if workdir:
        wd, werr = resolve_run_dir(workdir)
        if werr:
            return None, werr
    else:
        wd = os.path.join(CP2K_RUN_DIR, safe)
    os.makedirs(wd, exist_ok=True)
    inp_path = os.path.join(wd, safe + ".inp")
    out_path = os.path.join(wd, safe + ".out")
    log_path = os.path.join(wd, "submit.log")
    try:
        with open(inp_path, "w", encoding="utf-8") as f:
            f.write(inp_text.rstrip("\n") + "\n")
    except Exception as e:
        return None, "写输入文件失败: %s" % e

    env["OMP_NUM_THREADS"] = str(omp_threads)
    # mpirun 参数：OpenMPI 用 -x K=V；MPICH/Intel 用 -env K V（自动按实现适配）
    cmd = ([mpirun, "-np", str(nproc)]
           + build_mpi_env_args(mpi_flavor, "OMP_NUM_THREADS", str(omp_threads))
           + [cp2k_bin, "-i", inp_path, "-o", out_path])
    try:
        with open(log_path, "w") as logf:
            proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                    cwd=wd, env=env, start_new_session=True)
            # 登记以便停止任务时回收（避免僵尸进程堆积）
            SUBMITTED_PROCS[proc.pid] = proc
    except Exception as e:
        return None, "mpirun 启动失败: %s" % e

    return {
        "pid": proc.pid,
        "dir": wd,
        "inp": inp_path,
        "out": out_path,
        "command": " ".join(cmd),
        "nproc": nproc,
        "omp_threads": omp_threads,
        "mpirun": mpirun,
        "mpi_flavor": mpi_flavor,
        "cp2k_bin": cp2k_bin,
    }, None


# ==================== 平台内置 CM- 队列（无外部调度器时的 FIFO 排队） ====================
# 服务器没有 slurm/队列系统时，任务按提交时间排队，资源允许时由后台线程按序转
# 本地命令行真实提交。排队期间可撤销（移出队列）。所有任务给出 CM- 开头的平台编号。
# 并发默认限 1；总 MPI 进程数默认限 P // 2，为 AiiDA 与他人任务预留一半物理核。
# 物理核探测不可靠时保守限 16；环境变量显式设为 0 仍表示不限。
QUEUE_MAX_RUNNING = max(1, _env_int("CATMAT_QUEUE_MAX_RUNNING", 1))
QUEUE_MAX_TOTAL_NPROC = max(
    0, _env_int("CATMAT_QUEUE_MAX_TOTAL_NPROC", _DEFAULT_QUEUE_MAX_TOTAL_NPROC))
QUEUE_POLL_SECONDS = max(1, _env_int("CATMAT_QUEUE_POLL_SECONDS", 5))    # 调度心跳（秒）


class _GovernorAdmissionError(str):
    """保留既有字符串错误契约，同时让 HTTP 层能带回结构化治理预算。"""

    def __new__(cls, decision):
        text = "%s: %s" % (decision["code"], decision["message"])
        obj = str.__new__(cls, text)
        obj.decision = decision
        return obj


def _governor_policy():
    """从环境变量读取资源治理阈值，非法值保守回退到源码默认值。"""
    defaults = {
        "min_avail_gb": 130,
        "mem_per_rank_gb": 1.2,
        "core_reserve": 16,
        "mem_reserve_ratio": 1.15,
    }
    env_keys = {
        "min_avail_gb": "CATMAT_MIN_AVAIL_GB",
        "mem_per_rank_gb": "CATMAT_MEM_PER_RANK_GB",
        "core_reserve": "CATMAT_CORE_RESERVE",
        "mem_reserve_ratio": "CATMAT_MEM_RESERVE_RATIO",
    }
    policy = {}
    for key, default in defaults.items():
        raw = (os.environ.get(env_keys[key]) or "").strip()
        if not raw:
            policy[key] = default
            continue
        try:
            value = float(raw)
            if value < 0 or value != value or value in (float("inf"), float("-inf")):
                raise ValueError
            policy[key] = value
        except (TypeError, ValueError):
            LOG.warning("环境变量 %s=%r 非法，资源治理回退默认值 %s",
                        env_keys[key], raw, default)
            policy[key] = default
    return policy


def _platform_running_stats(conn):
    """返回平台自身运行中的作业数与 MPI rank 数（queue + tasks）。"""
    queue_rows = conn.execute(
        "SELECT nproc FROM queue WHERE status='running'").fetchall()
    task_rows = conn.execute(
        "SELECT resources FROM tasks WHERE status='running'").fetchall()
    nproc = sum(max(1, int(row["nproc"] or 1)) for row in queue_rows)
    for row in task_rows:
        try:
            resources = json.loads(row["resources"] or "{}")
            task_nproc = int(resources.get("num_mpiprocs_per_machine") or 1)
            nproc += max(1, task_nproc)
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            nproc += 1
    return len(queue_rows) + len(task_rows), nproc


def _governor_snapshot(conn):
    """组装含宿主探测结果与平台运行统计的 governor 快照。"""
    physical_cores = _physical_core_count()
    running_jobs, running_nproc = _platform_running_stats(conn)
    policy = _governor_policy()
    return _resource_governor.build_snapshot(
        physical_cores, _PHYSICAL_CORE_COUNT_RELIABLE,
        running_jobs, running_nproc,
        QUEUE_MAX_RUNNING, QUEUE_MAX_TOTAL_NPROC, policy=policy)


def _governor_check(conn, nproc, omp_threads=1):
    """按当前资源快照评估一次请求，返回 governor Decision 字典。"""
    policy = _governor_policy()
    snapshot = _governor_snapshot(conn)
    return _resource_governor.evaluate(
        {"nproc": nproc, "omp_threads": omp_threads}, snapshot, policy=policy)


def _governor_should_block(conn, decision, nproc, omp_threads, target):
    """执行强制/影子模式公共处置；返回当前决策是否应阻断。"""
    enforce = _env_int("CATMAT_GOVERNOR_ENFORCE", 1) != 0
    if not enforce:
        LOG.warning("资源治理影子模式 target=%s code=%s action=%s reason=%s",
                    target, decision["code"], decision["action"], decision["message"])
        audit(conn, "governor_shadow", target=target,
              params={"code": decision["code"], "nproc": nproc,
                      "omp_threads": omp_threads},
              result=decision["message"], ok=True)
        return False
    return decision["action"] != "ADMIT"


def _next_cm_code(conn, stamp=None):
    """生成 CM-YYYYMMDD-NNNN 编号（当日自增序号）。"""
    day = stamp or time.strftime("%Y%m%d")
    row = conn.execute(
        "SELECT cm_code FROM queue WHERE cm_code LIKE ? ORDER BY cm_code DESC LIMIT 1",
        ("CM-" + day + "-%",)).fetchone()
    seq = 1
    if row:
        try:
            seq = int(str(row["cm_code"]).rsplit("-", 1)[-1]) + 1
        except ValueError:
            seq = 1
    return "CM-%s-%04d" % (day, seq)


def _queue_running_stats(conn):
    """统计运行中队列任务的核数占用（供调度资源判断）。"""
    rows = conn.execute(
        "SELECT nproc FROM queue WHERE status='running'").fetchall()
    return len(rows), sum(int(r["nproc"] or 0) for r in rows)


def _queue_entry_view(row):
    """队列条目对外视图：隐藏输入文件明文，仅保留元信息供列表/状态展示。"""
    d = dict(row)
    try:
        payload = json.loads(row["payload"] or "{}")
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        d["payload"] = {
            "name": payload.get("name"),
            "system_id": payload.get("system_id"),
            "omp_threads": payload.get("omp_threads", 1),
            "has_inp": bool(payload.get("inp")),
        }
    else:
        d["payload"] = {}
    d["status_label"] = {"queued": "排队中", "running": "运行中", "done": "已完成",
                         "failed": "失败", "canceled": "已撤销"}.get(d["status"], d["status"])
    return d


def queue_enqueue(conn, owner_id, name, inp_text, nproc=1, omp_threads=1,
                  system_id=None, kind="cp2k"):
    """把本地命令行任务放入 CM- 队列。返回 (row, err)。"""
    if not inp_text or not str(inp_text).strip():
        return None, "缺少输入文件内容"
    try:
        nproc = max(1, int(nproc))
        omp_threads = max(1, int(omp_threads))
    except (TypeError, ValueError):
        return None, "nproc / omp_threads 需为整数"
    decision = _governor_check(conn, nproc, omp_threads)
    blocked = _governor_should_block(
        conn, decision, nproc, omp_threads, "queue_enqueue")
    if blocked and decision["action"] == "REJECT":
        return None, _GovernorAdmissionError(decision)
    cm_code = _next_cm_code(conn)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name or "task"))[:60] or "task"
    cur = conn.execute(
        "INSERT INTO queue(cm_code,name,owner_id,kind,nproc,payload,status) "
        "VALUES(?,?,?,?,?,?,?)",
        (cm_code, safe, owner_id, kind, nproc,
         json.dumps({"name": str(name or safe), "inp": str(inp_text),
                     "omp_threads": omp_threads, "system_id": system_id},
                    ensure_ascii=False), "queued"))
    qid = cur.lastrowid
    audit(conn, "queue_enqueue", target="queue/%s" % qid,
          params={"cm_code": cm_code, "nproc": nproc}, result="queued",
          actor="uid:%s" % owner_id if owner_id else "system")
    return conn.execute("SELECT * FROM queue WHERE id=?", (qid,)).fetchone(), None


def _queue_try_start_one(conn):
    """资源允许时取最早排队项出队，转真实本地命令行提交。返回启动了的 (qid, err)。"""
    running_cnt, running_nproc = _queue_running_stats(conn)
    row = conn.execute(
        "SELECT * FROM queue WHERE status='queued' ORDER BY created_at ASC, id ASC LIMIT 1"
    ).fetchone()
    if not row:
        return None, None
    want = int(row["nproc"] or 1)
    payload = {}
    try:
        payload = json.loads(row["payload"] or "{}")
    except Exception:
        payload = {}
    cm_code = row["cm_code"]
    omp_threads = payload.get("omp_threads") or 1
    decision = _governor_check(conn, want, omp_threads)
    if _governor_should_block(
            conn, decision, want, omp_threads, "queue_start/%s" % row["id"]):
        err = "%s: %s" % (decision["code"], decision["message"])
        if decision["action"] == "QUEUE" or decision["code"] == "BUDGET_UNKNOWN":
            # Probe failure is not evidence that a queued calculation is invalid.
            conn.execute("UPDATE queue SET error=? WHERE id=?", (err, row["id"]))
            return None, err
        conn.execute("UPDATE queue SET status='failed', error=?, finished_at=? WHERE id=?",
                     (err, now_str(), row["id"]))
        audit(conn, "governor_reject", target="queue/%s" % row["id"],
              params={"code": decision["code"], "nproc": want,
                      "omp_threads": omp_threads},
              result=decision["message"], ok=False)
        return row["id"], err
    if running_cnt >= QUEUE_MAX_RUNNING:
        conn.execute("UPDATE queue SET error=? WHERE id=?", ("等待平台并发名额", row["id"]))
        return None, "busy"
    if QUEUE_MAX_TOTAL_NPROC > 0 and running_nproc + want > QUEUE_MAX_TOTAL_NPROC:
        conn.execute("UPDATE queue SET error=? WHERE id=?", ("等待平台核数预算", row["id"]))
        return None, "nproc-busy"
    # 专属工作目录：cp2k_runs/<cm_code>/ —— 便于按归属浏览/下载/删除该任务文件
    wd = os.path.join(CP2K_RUN_DIR, cm_code)
    try:
        data, err = submit_cp2k_direct(
            payload.get("inp") or "", str(payload.get("name") or row["name"]),
            row["nproc"], omp_threads, workdir=cm_code, conn=conn)
    except Exception as e:
        data, err = None, "队列启动异常: %s" % e
    # 真正起飞点会再次采样资源；若两次采样之间资源转忙，仍须保持排队而非误标失败。
    if err and str(err).startswith("GOVERNOR_QUEUE:"):
        return None, err
    if err or not data:
        now = now_str()
        conn.execute("UPDATE queue SET status='failed', error=?, finished_at=? WHERE id=?",
                     (err or "启动失败", now, row["id"]))
        audit(conn, "queue_start_fail", target="queue/%s" % row["id"],
              params={"cm_code": cm_code}, result=err, ok=False)
        return row["id"], err
    out_path = data["out"]
    conn.execute(
        "UPDATE queue SET status='running', pid=?, out_dir=?, out_log=?, started_at=?, error=NULL "
        "WHERE id=?", (data["pid"], data["dir"], data["out"], now_str(), row["id"]))
    # 同步一条任务记录到 tasks 表，便于既有“任务”页与进度轮询统一展示
    resources = {"nproc": data["nproc"], "omp_threads": data["omp_threads"],
                 "command": data["command"], "workdir": data["dir"],
                 "pid": data["pid"], "mpirun": data["mpirun"],
                 "mpi_flavor": data["mpi_flavor"], "out": out_path,
                 "cm_code": cm_code, "queue_id": row["id"]}
    conn.execute(
        "INSERT INTO tasks(name,system_id,calc_type,input_file,resources,status,progress,log,"
        "owner_id,cm_code) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (str(payload.get("name") or row["name"]), payload.get("system_id"), "GEO_OPT",
         os.path.join(data["dir"], os.path.basename(data["inp"])),
         json.dumps(resources, ensure_ascii=False), "running", 0,
         json.dumps([{"ts": now_str(), "level": "ok",
                      "msg": "CM- 队列启动：%s，命令=%s" % (cm_code, data["command"])}],
                    ensure_ascii=False),
         row["owner_id"], cm_code))
    audit(conn, "queue_start", target="queue/%s" % row["id"],
          params={"cm_code": cm_code, "pid": data["pid"]}, result="running",
          actor="uid:%s" % row["owner_id"] if row["owner_id"] else "system")
    return row["id"], None


def queue_scheduler_once(conn):
    """单次调度：回收已完成项 + 按资源余量启动排队项。"""
    # 1) 回收运行中进程已结束的队列项
    for row in conn.execute("SELECT * FROM queue WHERE status='running'").fetchall():
        pid = row["pid"]
        proc = SUBMITTED_PROCS.get(pid)
        alive = True
        if proc is not None:
            try:
                alive = proc.poll() is None
            except Exception:
                alive = False
        elif pid:
            alive = proc_alive(pid)
        else:
            alive = False
        if not alive:
            out_path = row["out_log"]
            rc = proc.returncode if proc is not None else None
            output_status = cp2k_out_status(out_path)
            ok = output_status == "done" and (proc is None or rc == 0)
            status = "done" if ok else "failed"
            err = None if ok else "进程已退出但未正常结束（退出码 %s，输出判定 %s）；请检查日志" % (rc, output_status)
            conn.execute(
                "UPDATE queue SET status=?, finished_at=?, error=? WHERE id=?",
                (status, now_str(), err, row["id"]))
            # 同步 tasks 行
            t = conn.execute("SELECT id FROM tasks WHERE cm_code=? LIMIT 1",
                             (row["cm_code"],)).fetchone()
            if t:
                conn.execute("UPDATE tasks SET status=?, finished_at=?, error=? WHERE id=?",
                             (status, now_str(), err, t["id"]))
            if pid:
                SUBMITTED_PROCS.pop(pid, None)
            audit(conn, "queue_finish", target="queue/%s" % row["id"],
                  params={"cm_code": row["cm_code"], "status": status}, result=status)
    # 2) 按 FIFO 逐个尝试启动（每次心跳至多启动一批；循环内自然按时间序）
    for _ in range(QUEUE_MAX_RUNNING):
        qid, err = _queue_try_start_one(conn)
        if not qid:
            break


def queue_scheduler_loop():
    """后台调度线程主循环（服务启动后运行；异常不影响主服务）。"""
    LOG.info("CM- 队列调度器已启动：并发上限 %d，总核数上限 %s，心跳 %ds",
             QUEUE_MAX_RUNNING, QUEUE_MAX_TOTAL_NPROC or "不限", QUEUE_POLL_SECONDS)
    while True:
        try:
            _lock_acquire()
            try:
                conn = get_db()
                try:
                    queue_scheduler_once(conn)
                    conn.commit()
                finally:
                    conn.close()
            finally:
                _lock_release()
        except Exception:
            log_exc("CM- 队列调度循环异常")
        time.sleep(QUEUE_POLL_SECONDS)


def _structures_from_system(conn, system_id):
    """从已保存体系（systems.structure）构造 StructureData 所需字段（cell/positions/symbols/pbc）。"""
    if not system_id:
        return None
    row = conn.execute("SELECT structure FROM systems WHERE id=?", (system_id,)).fetchone()
    if not row or not row["structure"]:
        return None
    try:
        st = json.loads(row["structure"])
    except Exception:
        return None
    if not isinstance(st, dict) or not st.get("symbols") or not st.get("cell"):
        return None
    return st


def build_cp2k_parameters(calc_type, scf_method="diag", dispersion="d3bj", smearing=True,
                          elements=None, pdos=False, charge_density=False, periodic="XYZ",
                          cutoff=None, rel_cutoff=None, max_scf=None, eps_scf=None,
                          ngrids=None, vdw_parameter_file=None, requests=None, band_center=False):
    """构造 CP2K 输入 parameters（Dict，供 verdi 提交 / AiiDA 输入文件预览）。

    已重构为「唯一基线」模式：所有计算级别参数（基组 / 赝势 / CUTOFF / MGRID /
    QS / XC / VDW / POISSON / SCF 收敛盘）固化于 cp2k_workchain_input.TEMPLATE，
    以 irru_adsorption_workchain.py 为唯一基准，禁止在流程中被覆盖。

    两套相互独立参数（详见 cp2k_workchain_input 模块文档）：
      - GEO_OPT / CELL_OPT -> build_geo_opt_params（仅优化结构，严禁 PDOS/Bader/d 带）
      - 其余（ENERGY / SCF / PDOS / CHARGE / BADER / BAND...）-> build_single_point_params
        （在 &DFT &PRINT 追加 PDOS / E_DENSITY_CUBE；d 带中心依赖 PDOS 输出）
    关键参数（elements）缺失或元素不在固化 KIND_BASIS 中，将抛出明确异常。
    """
    return _wc_input.build_cp2k_parameters(
        calc_type, scf_method=scf_method, dispersion=dispersion, smearing=smearing,
        elements=elements, pdos=pdos, charge_density=charge_density, periodic=periodic,
        cutoff=cutoff, rel_cutoff=rel_cutoff, max_scf=max_scf, eps_scf=eps_scf,
        ngrids=ngrids, vdw_parameter_file=vdw_parameter_file, requests=requests,
        band_center=band_center)

def submit_verdi_calc(conn, name, system_id, calc_type, scf_method="diag",
                      dispersion="d3bj", smearing=True, resources=None, max_wallclock=None,
                      parameters=None, elements=None, pdos=False, structure_pk=None,
                      charge_density=False, band_center=False, periodic="XYZ", cutoff=None, rel_cutoff=None):
    """工作流/CP2K 任务：经 verdi run 提交真实 AiiDA Cp2kCalculation。

    resources: {"num_machines": N, "num_mpiprocs_per_machine": M}（从 AiiDA 输入文件要求读取）。
    parameters: 外部传入的 Aiida 输入 parameters（JSON 字典，已按 CP2K 手册层级校验）；
                为 None 时由 build_cp2k_parameters 自动构造。
    structure_pk: AiiDA 库中 StructureData 的 PK（高通量批量工作流引用，避免重复建库）。
    返回 (data, err)。data 含 pk / resources / aiida_input（提交用的 Aiida 输入文件 JSON）。
    """
    if not os.path.isfile(SUBMIT_VERDI_SCRIPT):
        return None, "未找到提交脚本: " + SUBMIT_VERDI_SCRIPT
    requested_nproc = DEFAULT_NPROC
    if resources is not None:
        try:
            requested_nproc = int(
                resources.get("num_mpiprocs_per_machine") or DEFAULT_NPROC)
        except (AttributeError, TypeError, ValueError):
            requested_nproc = DEFAULT_NPROC
    decision = _governor_check(conn, requested_nproc, 1)
    if _governor_should_block(
            conn, decision, requested_nproc, 1, "submit_verdi_calc"):
        err = "%s: %s" % (decision["code"], decision["message"])
        if decision["action"] == "QUEUE":
            err += "；请稍后重试或改用 CM- 队列"
        return None, err
    if structure_pk:
        st = {"symbols": [], "cell": [], "scaled_positions": [], "pbc": [True, True, True]}
    else:
        st = _structures_from_system(conn, system_id)
    if not st:
        return None, "体系 #%s 无完整结构（symbols/cell/scaled_positions），无法构造 StructureData" % system_id

    # 注：cutoff / rel_cutoff 已固化为模板常量（GEO_OPT=400/55, SP=350/50），
    # 不再透传给 builder（禁止在流程中覆盖）。
    params = parameters if parameters is not None else build_cp2k_parameters(
        calc_type, scf_method, dispersion, smearing,
        elements=(elements if elements is not None else (list(set(st["symbols"])) if st.get("symbols") else None)),
        pdos=pdos, charge_density=charge_density, periodic=periodic,
        band_center=band_center)
    res = resources or {"num_machines": 1, "num_mpiprocs_per_machine": DEFAULT_NPROC}
    try:
        res = {"num_machines": int(res.get("num_machines", 1)),
               "num_mpiprocs_per_machine": int(
                   res.get("num_mpiprocs_per_machine") or DEFAULT_NPROC)}
    except (TypeError, ValueError):
        res = {"num_machines": 1, "num_mpiprocs_per_machine": DEFAULT_NPROC}

    cfg = {
        "code_label": "cp2k@local",
        "label": (name or "cp2k_calc")[:100],
        "symbols": st.get("symbols") or [],
        "cell": st.get("cell") or [],
        "scaled_positions": st.get("scaled_positions") or [],
        "pbc": st.get("pbc", [True, True, True]),
        "parameters": params,
        "resources": res,
        "max_wallclock_seconds": int(max_wallclock or 12 * 3600),
    }
    if structure_pk:
        cfg["structure_pk"] = int(structure_pk)
    # 写参数 JSON 到临时目录
    token = time.strftime("%Y%m%d%H%M%S") + "_" + str(os.getpid()) + "_" + str(random.randint(1000, 9999))
    wd = os.path.join(TMP_DIR, "verdi_" + token)
    os.makedirs(wd, exist_ok=True)
    cfg_path = os.path.join(wd, "params.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)

    env = dict(os.environ)
    env.setdefault("AIIDA_PATH", os.path.expanduser("~/.aiida"))
    try:
        proc = subprocess.run([VERDI, "run", SUBMIT_VERDI_SCRIPT, cfg_path],
                              capture_output=True, text=True, timeout=300, env=env)
    except subprocess.TimeoutExpired:
        return None, "verdi run 提交超时（>300s）"
    except Exception as e:
        return None, "verdi run 调用失败: %s" % e

    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    m = re.search(r"SUBMITTED_PK=(\d+)", out)
    aiida_input = {"code_label": cfg["code_label"], "label": cfg["label"],
                   "parameters": params, "resources": res,
                   "max_wallclock_seconds": cfg["max_wallclock_seconds"]}
    if m:
        return {"pk": int(m.group(1)), "resources": res, "output": out[-200:],
                "aiida_input": aiida_input}, None
    return None, "verdi run 未返回 PK：%s" % out[-500:]


def submit_catmat_workchain(conn, name, structs, mode="A", scf_method="diag",
                            dispersion="d3bj", smearing=True, resources=None,
                            max_wallclock=None, periodic="XY", sp_requests=None,
                            do_pdos=False, do_bader=False, band_center=False, dry_run=False):
    """高通量批量：提交 CatMat 链式工作流（A 逐结构链式 / B 波前并行）。

    经 `verdi run catmat.py submit-catmat-workflow <config.json>` 提交真实 AiiDA 工作链
    （catmat.workflow / catmat.batch），由 daemon 执行；终端/服务关闭不影响运行。

    此即「稳定 AiiDA 提交」：load_profile() -> WorkflowFactory(entry_point) ->
    submit(builder)，与 irru_adsorption_workchain / run_adsorption 同源。

    mode:
      "A" -> catmat.workflow  （默认；逐结构 geo→energy→post，再下一结构）
      "B" -> catmat.batch     （波前并行：先全部 GEO_OPT，再全部单点能；支持几何预筛选）

    structs: [{"label", "system_id"?, "aiida_pk"?}]，与 /api/hts/run 同构。
      有 aiida_pk 直接引用已入库 StructureData；否则从 systems 表补结构。

    sp_requests 推导：由 do_pdos / charge_density / band_center 自动映射为
      ["pdos","bader","d_band"] 子集；外部显式传入则优先。

    返回 (data, err)。data 含 pk（工作链 PK）/ workchain / mode / num_structures / sp_requests。
    dry_run=True 时只生成 config.json 并返回其路径与内容，不调用 verdi（用于自测）。
    """
    requested_mode = "A" if mode is None else str(mode).upper()
    mpiprocs = int((resources or {}).get("num_mpiprocs_per_machine") or DEFAULT_NPROC)
    n_structs = max(1, len(structs))
    batch_mode = (os.environ.get("CATMAT_GOVERNOR_BATCH_MODE") or "strict").strip().lower()
    if batch_mode not in ("strict", "per_calc"):
        LOG.warning("CATMAT_GOVERNOR_BATCH_MODE=%r 非法，回退 strict", batch_mode)
        batch_mode = "strict"
    # per_calc 放弃批总量防护，仅限明确理解批内并发风险时使用。
    if batch_mode == "strict" and requested_mode != "A":
        planned_cores = mpiprocs * n_structs
    else:
        planned_cores = mpiprocs
    decision = _governor_check(conn, planned_cores, 1)
    should_block = _governor_should_block(
        conn, decision, planned_cores, 1, "catmat_workchain")
    if not dry_run and should_block:
        decision = dict(decision)
        decision["message"] = decision["message"] + (
            "（批计划：%d 结构 × %d 核 = %d 核）"
            % (n_structs, mpiprocs, planned_cores))
        if decision["action"] == "QUEUE":
            decision["message"] = decision["message"] + (
                "；请减小批大小、降低 num_mpiprocs_per_machine，"
                "或改用 mode A（逐结构链式）")
        return None, _GovernorAdmissionError(decision)

    if mode is None:
        mode = "A"
    mode = str(mode).upper()
    if mode == "B":
        workchain = "catmat.batch"
    else:
        mode = "A"
        workchain = "catmat.workflow"

    if not os.path.isfile(SUBMIT_CATMAT_SCRIPT):
        return None, "未找到工作流提交脚本: " + SUBMIT_CATMAT_SCRIPT

    res = resources or {"num_machines": 1, "num_mpiprocs_per_machine": DEFAULT_NPROC}
    try:
        res = {"num_machines": int(res.get("num_machines", 1)),
               "num_mpiprocs_per_machine": int(res.get("num_mpiprocs_per_machine", DEFAULT_NPROC))}
    except (TypeError, ValueError):
        res = {"num_machines": 1, "num_mpiprocs_per_machine": DEFAULT_NPROC}

    # sp_requests：单点能阶段的后续分析请求，由 steps 派生：
    #   pdos / band_center -> 需要 PDOS 输出；band_center -> 还需 d 带中心；
    #   bader -> 输出 E_DENSITY_CUBE。三者皆未选时 sp_requests 为空 -> 工作链只跑 ENERGY。
    if not sp_requests:
        sp_requests = []
        if band_center or do_pdos:
            sp_requests.append("pdos")
        if band_center:
            sp_requests.append("d_band")
        if do_bader:
            sp_requests.append("bader")
        # 去重并保序
        seen, uniq = set(), []
        for r in sp_requests:
            if r not in seen:
                seen.add(r); uniq.append(r)
        sp_requests = uniq

    wall = int(max_wallclock or 12 * 3600)
    options = {"resources": dict(res)}

    raw_structs = []
    for i, s in enumerate(structs):
        spk = s.get("aiida_pk")
        label = s.get("label") or ("item%d" % (i + 1))
        if spk:
            raw_structs.append({"label": label, "aiida_pk": int(spk)})
        else:
            sid = int(s.get("system_id") or 0)
            if sid and conn:
                try:
                    srow = conn.execute("SELECT structure FROM systems WHERE id=?", (sid,)).fetchone()
                    if srow and srow["structure"]:
                        stj = json.loads(srow["structure"])
                        raw_structs.append({
                            "label": label,
                            "symbols": stj.get("symbols") or [],
                            "cell": stj.get("cell") or [],
                            "scaled_positions": stj.get("scaled_positions") or [],
                            "pbc": stj.get("pbc", [True, True, True]),
                        })
                except Exception:
                    pass
    if not raw_structs:
        return None, "无可用结构（需 system_id 含结构或 aiida_pk）"

    cfg = {
        "code_label": "cp2k@local",
        "workchain": workchain,
        "periodic": periodic,
        "scf_method": scf_method,
        "sp_requests": sp_requests,
        "options": options,
        "geo_wallclock_seconds": wall,
        "sp_wallclock_seconds": wall,
        "structures": raw_structs,
    }
    token = time.strftime("%Y%m%d%H%M%S") + "_" + str(os.getpid()) + "_" + str(random.randint(1000, 9999))
    wd = os.path.join(TMP_DIR, "verdi_" + token)
    os.makedirs(wd, exist_ok=True)
    cfg_path = os.path.join(wd, "params.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)

    if dry_run:
        return {"dry_run": True, "cfg_path": cfg_path, "config": cfg,
                "workchain": workchain, "mode": mode,
                "num_structures": len(raw_structs), "sp_requests": sp_requests,
                "governor": decision}, None

    env = dict(os.environ)
    env.setdefault("AIIDA_PATH", os.path.expanduser("~/.aiida"))
    try:
        proc = subprocess.run([VERDI, "run", SUBMIT_CATMAT_SCRIPT, cfg_path],
                              capture_output=True, text=True, timeout=300, env=env)
    except subprocess.TimeoutExpired:
        return None, "verdi run（CatMat 工作流）提交超时（>300s）"
    except Exception as e:
        return None, "verdi run（CatMat 工作流）调用失败: %s" % e

    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    m = re.search(r"SUBMITTED_PK=(\d+)", out)
    if m:
        return {"pk": int(m.group(1)), "workchain": workchain, "mode": mode,
                "num_structures": len(raw_structs), "sp_requests": sp_requests,
                "output": out[-300:]}, None
    return None, "verdi run（CatMat 工作流）未返回 PK：%s" % out[-800:]


def load_workflow_results(pk):
    """读取已完成 CatMat 工作链的输出 results（逐结构 {label,energy,...}）。

    经 verdi run catmat.py load-workflow-results 调用；返回 (list, err)。工作链未完成
    或正在运行、尚无 results 输出时返回 ([], None)（调用方应忽略）。
    """
    if not os.path.isfile(LOAD_WORKFLOW_RESULTS_SCRIPT):
        return None, "未找到工作流结果读取脚本: " + LOAD_WORKFLOW_RESULTS_SCRIPT
    env = dict(os.environ)
    env.setdefault("AIIDA_PATH", os.path.expanduser("~/.aiida"))
    try:
        with _blocking_unlocked():
            proc = subprocess.run([VERDI, "run", LOAD_WORKFLOW_RESULTS_SCRIPT, str(pk)],
                                  capture_output=True, text=True, timeout=120, env=env)
    except Exception as e:
        return None, "读取工作流结果失败: %s" % e
    for ln in (proc.stdout or "").splitlines():
        ln = ln.strip()
        if ln.startswith("[") and ln.endswith("]"):
            try:
                return json.loads(ln), None
            except Exception:
                continue
    return [], None


def sanitize_dict(obj):
    """递归清理用户编辑的 Aiida 输入字典：删除 None 值、确保键为字符串。"""
    if isinstance(obj, dict):
        return {str(k): sanitize_dict(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [sanitize_dict(v) for v in obj if v is not None]
    if obj in (True, False) or isinstance(obj, (int, float, str)):
        return obj
    return str(obj)


def build_aiida_preview(conn, system_id, calc_type="GEO_OPT", scf_method="diag",
                        dispersion="d3bj", smearing=True, resources=None, max_wallclock=None,
                        label=None, charge_density=False, band_center=False, periodic="XYZ",
                        cutoff=None, rel_cutoff=None):
    """构建 Aiida 输入文件预览（CP2K 输入按官方手册以 JSON 层级表示）。

    自动按元素生成 &KIND、重金属切换 UZH 基组、PDOS 加 &DOS&PDOS、
    电荷密度加 &E_DENSITY_CUBE、周期边界 PERIODIC 可设；
    返回 aiida_input（前端展示/编辑/确认）与资源建议。
    """
    st = _structures_from_system(conn, system_id)
    if not st:
        return None, "体系 #%s 无完整结构（symbols/cell/scaled_positions）" % system_id
    elements = list(dict.fromkeys(st["symbols"]))
    ct = (calc_type or "GEO_OPT").upper()
    # cutoff / rel_cutoff 已固化为模板常量，不再透传（禁止在流程中覆盖）。
    params = build_cp2k_parameters(ct, scf_method, dispersion, smearing,
                                   elements=elements, pdos=(ct == "PDOS"),
                                   charge_density=charge_density, periodic=periodic,
                                   band_center=band_center)
    res = {"num_machines": 1, "num_mpiprocs_per_machine": DEFAULT_NPROC}
    try:
        res = {"num_machines": int((resources or {}).get("num_machines", 1)),
               "num_mpiprocs_per_machine": int((resources or {}).get("num_mpiprocs_per_machine", DEFAULT_NPROC))}
    except (TypeError, ValueError):
        res = {"num_machines": 1, "num_mpiprocs_per_machine": DEFAULT_NPROC}
    wall = int(max_wallclock or 12 * 3600)
    aiida_input = {
        "code_label": "cp2k@local",
        "label": (label or ("cp2k_" + ct.lower()))[:100],
        "parameters": params,
        "resources": res,
        "max_wallclock_seconds": wall,
    }
    notes = []
    heavy = any(e in ("Ir", "Ru", "Rh", "Pd", "Tc", "Au", "Pt", "Os", "Re", "W", "Ta", "Hf",
                      "Ag", "Cd", "In", "Sn", "Sb", "Te", "I") for e in elements)
    if heavy:
        notes.append("体系含重金属（%s），已切换 BASIS_MOLOPT_UZH 基组库（DZVP-MOLOPT-SR-GTH）"
                     % ", ".join(e for e in elements if e in (
                         "Ir", "Ru", "Rh", "Pd", "Tc", "Au", "Pt", "Os", "Re", "W", "Ta", "Hf",
                         "Ag", "Cd", "In", "Sn", "Sb", "Te", "I")))
    if ct == "PDOS":
        notes.append("PDOS 计算已在 &DFT &PRINT 下启用 &DOS（NLUMO=-1）+ &PDOS COMPONENTS=T")
    if scf_method == "ot":
        notes.append("SCF 采用 OT 求解（无 &MIXING/&SMEAR，适用于带隙体系）")
    else:
        notes.append("SCF 采用对角化 + BROYDEN 混合%s" % (" + FERMI_DIRAC 300K smearing" if smearing else ""))
    return {"aiida_input": aiida_input,
            "structure": {"symbols": st["symbols"], "natoms": len(st["symbols"]),
                          "cell": st["cell"], "elements": elements, "pbc": st.get("pbc", [True] * 3)},
            "notes": notes}, None


def build_workflow_preview(conn, system_id, scf_method="diag", dispersion="d3bj",
                           smearing=True, periodic="XY", sp_requests=("pdos", "bader", "d_band"),
                           resources=None, max_wallclock=None, label=None):
    """构建「两阶段」工作流的 Aiida 输入预览：结构优化(GEO_OPT) + 单点能(ENERGY)。

    两个参数集相互独立、均以 irru_adsorption_workchain.py 为唯一基准（经 cp2k_workchain_input
    固化模板）：GEO_OPT 仅优化结构；ENERGY 承接优化结构，并据 sp_requests 追加 PDOS /
    E_DENSITY_CUBE 等输出。返回 {aiida_input_geo, aiida_input_sp, notes}。
    """
    st = _structures_from_system(conn, system_id)
    if not st:
        return None, "体系 #%s 无完整结构（symbols/cell/scaled_positions）" % system_id
    elements = list(dict.fromkeys(st["symbols"]))
    geo = _wc_input.build_geo_opt_params(elements, periodic=periodic, scf_method=scf_method, label=label)
    sp = _wc_input.build_single_point_params(elements, periodic=periodic, scf_method=scf_method,
                                             requests=set(sp_requests), label=label)
    res = {"num_machines": 1, "num_mpiprocs_per_machine": DEFAULT_NPROC}
    try:
        res = {"num_machines": int((resources or {}).get("num_machines", 1)),
               "num_mpiprocs_per_machine": int((resources or {}).get("num_mpiprocs_per_machine", DEFAULT_NPROC))}
    except (TypeError, ValueError):
        res = {"num_machines": 1, "num_mpiprocs_per_machine": DEFAULT_NPROC}
    wall = int(max_wallclock or 12 * 3600)
    aiida_geo = {"code_label": "cp2k@local", "label": (label or "GEO_OPT").strip()[:100],
                 "parameters": geo, "resources": res, "max_wallclock_seconds": wall}
    aiida_sp = {"code_label": "cp2k@local", "label": (label or "SP_PDOS").strip()[:100],
                "parameters": sp, "resources": res, "max_wallclock_seconds": wall}
    notes = []
    if scf_method == "ot":
        notes.append("结构优化与单点能均采用 OT 求解（无 &MIXING/&SMEAR，适用于带隙体系）")
    else:
        notes.append("结构优化与单点能均采用对角化 + BROYDEN 混合 + FERMI_DIRAC 300K smearing")
    notes.append("基组/赝势/CUTOFF 已固化为模板常量（GEO_OPT: BASIS_MOLOPT / CUTOFF 400；"
                 "SP: BASIS_MOLOPT_UZH / CUTOFF 350），禁止在流程中覆盖")
    if "pdos" in sp_requests or "d_band" in sp_requests:
        notes.append("单点能已启用 PDOS（&DOS NLUMO=-1 + &PDOS COMPONENTS=T）；d 带中心将基于 PDOS 解析")
    if "bader" in sp_requests:
        notes.append("单点能已启用 &E_DENSITY_CUBE（Bader 电荷分析输入）")
    return {"aiida_input_geo": aiida_geo, "aiida_input_sp": aiida_sp, "notes": notes}, None


# --------------------------------------------------------------------------
# 工作流（统一启动 + 断点续跑）
# --------------------------------------------------------------------------
# 标准催化计算工作流节点序列
WORKFLOW_NODES = [
    {"key": "structure", "label": "结构生成", "desc": "ASE 生成表面平板 / 合金 / 掺杂 / 吸附结构"},
    {"key": "inp", "label": "INP 生成", "desc": "Multiwfn / 对话式 AI 生成 CP2K 输入文件"},
    {"key": "scf", "label": "SCF 单点能", "desc": "CP2K 单点能计算"},
    {"key": "geo_opt", "label": "几何优化", "desc": "CP2K 几何优化"},
    {"key": "pdos", "label": "PDOS 计算", "desc": "CP2K 态密度 / 投影态密度"},
    {"key": "postprocess", "label": "后处理", "desc": "解析 PDOS → 描述符 → ML 数据集"},
]
NODE_KEYS = [n["key"] for n in WORKFLOW_NODES]
# 可本地自动执行的节点（无需真实 CP2K 计算）
AUTO_NODES = ("structure", "inp")
# 计算节点（提交到 AiiDA / 任务队列）
CALC_NODES = ("scf", "geo_opt", "pdos")


def calc_node_for(calc_type):
    """根据计算类型映射到主计算节点。"""
    ct = (calc_type or "").lower()
    if ct in ("energy", "scf", "energy_force", "single"):
        return "scf"
    if ct in ("pdos", "dos", "band"):
        return "pdos"
    return "geo_opt"


def system_structure_xyz(conn, system_id):
    """从已保存体系（systems.structure）生成 xyz 文本；无结构返回 None。"""
    if not system_id:
        return None
    row = conn.execute("SELECT name,structure FROM systems WHERE id=?", (system_id,)).fetchone()
    if not row or not row["structure"]:
        return None
    try:
        st = json.loads(row["structure"])
    except Exception:
        return None
    if not isinstance(st, dict) or not st.get("symbols"):
        return None
    import numpy as np
    cell = np.array(st["cell"])
    sp = np.array(st["scaled_positions"])
    cart = sp.dot(cell)
    lines = [str(len(st["symbols"])), "system #%s %s" % (system_id, row["name"])]
    for sym, p in zip(st["symbols"], cart):
        lines.append("%s  %.6f  %.6f  %.6f" % (sym, p[0], p[1], p[2]))
    return "\n".join(lines) + "\n"


def create_workflow(conn, body, owner_id=None):
    """创建并启动工作流，返回 (data, err)。owner_id=发起账号；缺省归东翁（系统/AI 通道）。"""
    name = body.get("name") or "workflow"
    system_id = body.get("system_id")
    calc_type = body.get("calc_type", "GEO_OPT")
    params = body.get("params") or {}
    start_node = body.get("start_node", "structure")
    artifacts = body.get("artifacts") or {}   # 断点续跑：{node_key: 中间产物文本/JSON}

    if start_node not in NODE_KEYS:
        return None, "未知起始节点: " + start_node
    start_idx = NODE_KEYS.index(start_node)

    # 需求：启动工作流仅需选择已保存的结构与计算参数——system_id 指定的已保存
    # 结构作为 structure 节点输入（无需重新建模/上传）。
    if system_id and start_node == "structure":
        saved = system_structure_xyz(conn, system_id)
        if saved and not artifacts.get("structure"):
            artifacts["structure"] = saved
        params = dict(params)
        params["system_id"] = system_id

    conn.execute(
        "INSERT INTO workflows(name,system_id,calc_type,params,start_node,status,current_node,log,owner_id) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (name, system_id, calc_type, json.dumps(params, ensure_ascii=False), start_node,
         "running", start_node, json.dumps([{"ts": now_str(), "level": "info",
                                             "msg": "工作流启动，起始节点 %s" % start_node}], ensure_ascii=False),
         owner_id or _system_owner_id(conn)))
    wid = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]

    # 建节点序列：start_node 之前的标记 skipped（断点续跑），之后 pending
    for i, nd in enumerate(WORKFLOW_NODES):
        status = "skipped" if i < start_idx else "pending"
        art = artifacts.get(nd["key"])
        conn.execute(
            "INSERT INTO workflow_nodes(workflow_id,seq,node_key,node_label,status,input_artifacts) "
            "VALUES(?,?,?,?,?,?)",
            (wid, i, nd["key"], nd["label"], status,
             json.dumps(art if art is not None else {}, ensure_ascii=False)))

    # 从 start_node 起依次执行：结构/INP 本地自动执行；计算节点按 calc_type 入队；后处理等待
    calc_target = calc_node_for(calc_type)
    idx = start_idx
    while idx < len(WORKFLOW_NODES):
        key = WORKFLOW_NODES[idx]["key"]
        if key in AUTO_NODES:
            run_workflow_node(conn, wid, key, params)
        elif key in CALC_NODES:
            if key == calc_target:
                run_workflow_node(conn, wid, key, params)   # 入队到 AiiDA 队列
            else:
                conn.execute("UPDATE workflow_nodes SET status='skipped' WHERE workflow_id=? AND node_key=?",
                             (wid, key))
        elif key == "postprocess":
            run_workflow_node(conn, wid, key, params)       # 标记 pending，等待计算完成
        idx += 1

    nodes = conn.execute("SELECT seq,node_key,node_label,status,output FROM workflow_nodes "
                         "WHERE workflow_id=? ORDER BY seq", (wid,)).fetchall()
    node_list = []
    for nd in nodes:
        item = {"seq": nd["seq"], "key": nd["node_key"], "label": nd["node_label"], "status": nd["status"]}
        if nd["output"]:
            item["output"] = nd["output"][:200]
        node_list.append(item)
    return {"workflow_id": wid, "status": "running", "start_node": start_node, "nodes": node_list}, None


def run_workflow_node(conn, wid, node_key, params):
    """执行单个节点（本地可自动的 structure/inp；计算节点入队）。"""
    node = conn.execute("SELECT id,node_key,status FROM workflow_nodes WHERE workflow_id=? AND node_key=?",
                        (wid, node_key)).fetchone()
    if not node:
        return
    conn.execute("UPDATE workflows SET current_node=? WHERE id=?", (node_key, wid))
    conn.execute("UPDATE workflow_nodes SET status='running' WHERE id=?", (node["id"],))

    def _finish(status, output=None, msg=""):
        conn.execute("UPDATE workflow_nodes SET status=?, output=?, log=? WHERE id=?",
                     (status, output, json.dumps([{"ts": now_str(), "level": "info", "msg": msg}], ensure_ascii=False),
                      node["id"]))

    if node_key == "structure":
        # 若提供了结构中间产物则直接采用，否则用 ASE 生成
        art = json.loads(node and conn.execute(
            "SELECT input_artifacts FROM workflow_nodes WHERE id=?", (node["id"],)).fetchone()["input_artifacts"] or "{}")
        struct_text = art if isinstance(art, str) else (art or {}).get("structure")
        if struct_text:
            _finish("done", struct_text[:200], "采用提供的中间结构（断点续跑）")
        else:
            try:
                r = build_structure_ase(params)
                # 必须转笛卡尔坐标（scaled @ cell），否则原子间距会被压缩成荒谬值
                xyz = ase_result_to_xyz(r, "CatMat workflow")
                _finish("done", xyz[:200], "ASE 结构生成完成：%s · %d 原子" % (r["formula"], r["natoms"]))
            except ApiError as e:
                _finish("failed", None, str(e.msg))
    elif node_key == "inp":
        art = json.loads(conn.execute(
            "SELECT input_artifacts FROM workflow_nodes WHERE id=?", (node["id"],)).fetchone()["input_artifacts"] or "{}")
        inp_text = art if isinstance(art, str) else (art or {}).get("inp")
        if inp_text:
            _finish("done", inp_text[:200], "采用提供的 INP（断点续跑）")
        else:
            # 结构来源优先级：artifacts.structure（含已保存体系结构） > system_id > params ASE 生成
            struct_text = (art or {}).get("structure") if isinstance(art, dict) else None
            if struct_text is None:
                wf = conn.execute("SELECT system_id FROM workflows WHERE id=?", (wid,)).fetchone()
                struct_text = system_structure_xyz(conn, wf["system_id"] if wf else None)
            if struct_text is None:
                try:
                    r = build_structure_ase(params)
                    struct_text = ase_result_to_xyz(r, "CatMat workflow")
                except ApiError:
                    struct_text = None
            if struct_text:
                inp, err = generate_multiwfn_inp(struct_text, calc_type=params.get("calc_type", "geo_opt"),
                                                 dispersion=params.get("dispersion", "d3bj"),
                                                 smearing=params.get("smearing", True),
                                                 scf_method=params.get("scf_method", "diag"))
                if err:
                    _finish("failed", None, err)
                else:
                    # 输入文件存放路径：若指定 input_dir，把生成的 .inp 落盘便于查看/管理
                    wf_row = conn.execute("SELECT name FROM workflows WHERE id=?", (wid,)).fetchone()
                    wf_name = re.sub(r"[^A-Za-z0-9_.-]", "_", (wf_row["name"] or "workflow")[:60]) if wf_row else "workflow"
                    in_dir, derr = resolve_run_dir(params.get("input_dir"))
                    saved_note = ""
                    if params.get("input_dir") and not derr:
                        try:
                            os.makedirs(in_dir, exist_ok=True)
                            fp = os.path.join(in_dir, wf_name + ".inp")
                            with open(fp, "w", encoding="utf-8") as f:
                                f.write(inp)
                            saved_note = "，已保存到 " + fp
                        except Exception as e:
                            saved_note = "，保存失败: %s" % e
                    elif params.get("input_dir") and derr:
                        saved_note = "，路径无效: " + derr
                    _finish("done", inp[:200], "INP 生成完成（Multiwfn）" + saved_note)
            else:
                _finish("failed", None, "无可用结构生成 INP")
    elif node_key in CALC_NODES:
        # 计算节点：真实 verdi run 提交 Cp2kCalculation（资源从 AiiDA 输入文件要求读取）
        wf = conn.execute("SELECT name,system_id FROM workflows WHERE id=?", (wid,)).fetchone()
        resources = params.get("resources") or {}
        name = "%s_%s" % (wf["name"], node_key)
        # 人工确认后的 Aiida 输入覆盖（在「预览 Aiida 输入文件」中编辑确认）
        ov = params.get("aiida_override") or {}
        # 两阶段覆盖：{geo:{...}, sp:{...}}；兼容旧式单盒 {parameters,resources,...}
        if isinstance(ov, dict) and ("geo" in ov or "sp" in ov):
            box = ov.get("geo") if node_key == "geo_opt" else (ov.get("sp") or ov.get("geo"))
            if isinstance(box, dict):
                ov = box
        ov_params = ov.get("parameters") if isinstance(ov.get("parameters"), dict) else None
        if ov.get("resources"):
            resources = {**resources, **ov["resources"]}
        # 输入/输出文件存放路径（便于管理与查看）
        paths = {}
        if params.get("input_dir"): paths["input_dir"] = params["input_dir"]
        if params.get("output_dir"): paths["output_dir"] = params["output_dir"]
        data, err = submit_verdi_calc(
            conn, name, wf["system_id"], node_key.upper(),
            scf_method=params.get("scf_method", "diag"),
            dispersion=params.get("dispersion", "d3bj"),
            smearing=params.get("smearing", True),
            resources=resources,
            max_wallclock=ov.get("max_wallclock_seconds") or params.get("max_wallclock_seconds"),
            parameters=ov_params,
            pdos=(node_key.upper() == "PDOS"),
            charge_density=params.get("charge_density", False),
            periodic=params.get("periodic", "XYZ"),
            cutoff=params.get("cutoff"), rel_cutoff=params.get("rel_cutoff"),
        )
        if err:
            res_fail = dict(resources); res_fail.update(paths)
            conn.execute(
                "INSERT INTO tasks(name,system_id,calc_type,input_file,resources,status,progress,log,aiiada_pk,error) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (name, wf["system_id"], node_key.upper(), None,
                 json.dumps(res_fail, ensure_ascii=False), "failed", 0,
                 json.dumps([{"ts": now_str(), "level": "err", "msg": err}], ensure_ascii=False),
                 None, err))
            _finish("failed", None, err)
        else:
            res_ok = dict(data["resources"]); res_ok.update(paths)
            conn.execute(
                "INSERT INTO tasks(name,system_id,calc_type,input_file,resources,status,progress,log,aiiada_pk) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (name, wf["system_id"], node_key.upper(), None,
                 json.dumps(res_ok, ensure_ascii=False), "running", 0,
                 json.dumps([{"ts": now_str(), "level": "ok",
                              "msg": "已提交 AiiDA Cp2kCalculation，PK=%s，资源=%s" % (data["pk"], data["resources"])}],
                            ensure_ascii=False),
                 str(data["pk"])))
            _finish("running", "PK " + str(data["pk"]),
                    "已提交 AiiDA Cp2kCalculation（PK %s，资源 %s）" % (data["pk"], data["resources"]))
    elif node_key == "postprocess":
        _finish("pending", None, "等待计算完成后执行 PDOS 后处理")
    return


# --------------------------------------------------------------------------
# 大模型接入（OpenAI 兼容接口 + RAG + 联网检索）
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 平台设置 · 大模型统一配置源
#
# 唯一配置源：LLM_CONFIG_PATH（llm_config.json，chmod 0600、gitignored）。
# 全部字段（含 api_key）由「平台设置 → 大模型」集中读写；运行时代码每次调用
# 动态读取该文件。不再读取任何 LLM_* 环境变量，也不再回写 .env / 进程环境，
# 因此本地运行不依赖任何密钥类环境变量注入。
#
# 其余平台鉴权类凭据（CATMAT_JWT_SECRET / CATMAT_ADMIN_TOKEN / 微信 AppSecret）
# 属服务器“启动级”凭据（web 设置页就绪之前就必须存在、用于签发/校验自身令牌），
# 仍由 systemd EnvironmentFile（.env，0600）注入，应用代码从不将其用于对外
# API 调用，也不写入 llm_config.json。
# --------------------------------------------------------------------------
LLM_FILE_FIELDS = ("base_url", "model", "api_key", "temperature", "reasoning_effort",
                   "ai_name", "ai_nicknames", "vendor", "max_tokens", "timeout", "stream")


def _llm_cfg_defaults():
    """大模型配置默认值（vendor=auto|deepseek|qwen|glm|kimi|doubao|siliconflow|openai|generic）。"""
    return {"base_url": "", "model": "", "api_key": "", "temperature": 0.7,
            "reasoning_effort": None, "ai_name": "休go", "ai_nicknames": ["小休", "小go"],
            "vendor": "auto", "max_tokens": None, "timeout": None, "stream": False}


def _read_llm_file():
    """读取平台设置文件。文件缺失 / 损坏 → 空 dict，由默认值兜底。"""
    if not os.path.isfile(LLM_CONFIG_PATH):
        return {}
    try:
        with open(LLM_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_llm_file(cfg):
    """原子写回平台设置文件（含 api_key），权限收敛 0600。"""
    payload = {k: cfg.get(k) for k in LLM_FILE_FIELDS}
    fd = os.open(LLM_CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.chmod(LLM_CONFIG_PATH, 0o600)


def load_llm_config(public=False):
    """大模型运行配置：唯一来源为平台设置文件（llm_config.json）。"""
    cfg = _llm_cfg_defaults()
    fc = _read_llm_file()
    for k in LLM_FILE_FIELDS:
        if fc.get(k) not in (None, ""):
            cfg[k] = fc[k]
    # 类型归一
    for k in ("max_tokens", "timeout"):
        try:
            cfg[k] = int(cfg[k]) if cfg.get(k) not in (None, "") else None
        except (TypeError, ValueError):
            cfg[k] = None
    try:
        cfg["temperature"] = float(cfg["temperature"])
    except (TypeError, ValueError):
        cfg["temperature"] = 0.7
    cfg["stream"] = str(cfg.get("stream")).lower() in ("1", "true", "yes", "on")
    if not isinstance(cfg.get("ai_nicknames"), list):
        cfg["ai_nicknames"] = []
    cfg["has_api_key"] = bool(cfg.get("api_key"))
    # 对外不回传完整 api_key，仅回传掩码
    key = cfg.get("api_key") or ""
    cfg["api_key_masked"] = (key[:6] + "…" + key[-4:]) if len(key) > 12 else ("已设置" if key else "")
    # 厂商识别（按 base URL 自动识别或按显式 vendor），供前端与调用层使用
    if llm_vendors:
        try:
            cfg["vendor_info"] = llm_vendors.vendor_summary(cfg.get("base_url"), cfg.get("vendor"))
        except Exception:
            pass
    if public:
        cfg.pop("api_key", None)
    return cfg


def save_llm_config(body):
    """平台设置保存：body 中出现的非空字段覆盖；提供 api_key 即更新、clear_key=1
    即清除；随后整体原子写回平台设置文件（平台设置的唯一落盘位置）。"""
    cfg = _llm_cfg_defaults()
    cfg.update({k: v for k, v in _read_llm_file().items() if v not in (None, "")})
    for k in ("base_url", "model", "reasoning_effort", "vendor"):
        if body.get(k) not in (None, ""):
            cfg[k] = str(body[k]).strip()
    if body.get("temperature") not in (None, ""):
        try:
            cfg["temperature"] = float(body["temperature"])
        except (TypeError, ValueError):
            pass
    for k in ("max_tokens", "timeout"):
        if body.get(k) not in (None, ""):
            try:
                cfg[k] = int(body[k])
            except (TypeError, ValueError):
                pass
    if "stream" in body:
        cfg["stream"] = str(body.get("stream")).lower() in ("1", "true", "yes", "on")
    for k in ("ai_name", "ai_nicknames"):
        if body.get(k) not in (None, ""):
            cfg[k] = body[k]
    if body.get("clear_key"):
        cfg.pop("api_key", None)
    if body.get("api_key"):
        cfg["api_key"] = str(body["api_key"]).strip()
    _write_llm_file(cfg)
    return load_llm_config()


def _chat_completions(payload, cfg):
    """调用大模型 /chat/completions，返回 (原始响应 dict, err)。

    已接入国内主流大模型适配层（catmat_tools/llm_vendors.py）：
      · 按 base URL（或显式 vendor）识别厂商；
      · 自动补齐厂商必需路径后缀（/v1、/compatible-mode/v1、/api/paas/v4、/api/v3 …）；
      · temperature / max_tokens / 推理字段 / 流式输出按厂商能力裁剪；
      · 错误按状态码翻译为中文可读提示（含该厂商正确 URL 示例）。
    """
    import urllib.request
    import urllib.error
    import socket

    base_url = cfg.get("base_url") or ""
    key = cfg.get("api_key") or ""
    model = cfg.get("model") or ""
    if not base_url:
        return None, "未配置大模型：请在「平台设置 → 大模型」中填写 base_url、model 与 API Key（密钥存于平台设置，不依赖环境变量）"
    if not key:
        return None, "未配置 API Key：请在「平台设置 → 大模型」中保存 API Key（密钥存于平台设置，不依赖环境变量）"

    if llm_vendors:
        vkey, prof, _matched = llm_vendors.detect_vendor(base_url, cfg.get("vendor"))
        base = llm_vendors.normalize_base_url(base_url, prof)
        url = base + prof.get("path", "/chat/completions")
        p = llm_vendors.build_payload(prof, payload.get("messages") or [],
                                      model=model, temperature=payload.get("temperature"),
                                      cfg=cfg)
        timeout = int(cfg.get("timeout") or prof.get("timeout") or 120)
    else:  # 适配层缺失时的兼容兜底
        vkey, prof = "generic", {"label": "通用 OpenAI 兼容端点", "timeout": 120,
                                 "default_model": ""}
        base = base_url.rstrip("/")
        url = base + "/chat/completions"
        p = dict(payload)
        p["model"] = model
        p.setdefault("temperature", float(cfg.get("temperature", 0.7)))
        if cfg.get("reasoning_effort"):
            p["reasoning_effort"] = cfg["reasoning_effort"]
        timeout = int(cfg.get("timeout") or 120)
    if not p.get("model"):
        return None, "未配置模型名（model）：%s 建议填写 %s" % (
            prof.get("label", ""), prof.get("default_model") or "对应厂商的模型名")

    def _request(pp):
        req = urllib.request.Request(
            url, data=json.dumps(pp).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key,
                     "Accept": "text/event-stream" if pp.get("stream") else "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if not pp.get("stream"):
                return json.loads(resp.read().decode("utf-8"))
            # 流式（SSE）聚合：把增量 delta 拼成与非流式一致的结构
            chunks = []
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                for ch in obj.get("choices", []) or []:
                    delta = ch.get("delta") or {}
                    if delta.get("content"):
                        chunks.append(delta["content"])
            return {"choices": [{"message": {"content": "".join(chunks)}}], "streamed": True}
        return None

    def _err_msg(exc):
        try:
            body = exc.read().decode("utf-8")
            return json.loads(body).get("error", {}).get("message", "")
        except Exception:
            return ""

    try:
        return _request(p), None
    except urllib.error.HTTPError as e:
        msg = _err_msg(e)
        # 厂商不支持的附加参数（如 reasoning_effort）→ 去掉重试一次
        droppable = [k for k in ("reasoning_effort", "enable_thinking", "max_tokens")
                     if k in p]
        if e.code == 400 and droppable and any(
                k in msg.lower() for k in ("unknown parameter", "unexpected parameter",
                                           "not supported", "unsupported", "invalid")):
            for k in droppable:
                p.pop(k, None)
            try:
                return _request(p), None
            except urllib.error.HTTPError as e2:
                return None, (llm_vendors.error_hint(e2.code, _err_msg(e2), vkey, base_url)
                              if llm_vendors else "大模型接口错误 %s" % e2.code)
            except Exception as e2:
                return None, (llm_vendors.error_hint("network", str(e2), vkey, base_url)
                              if llm_vendors else "大模型调用失败: %s" % e2)
        return None, (llm_vendors.error_hint(e.code, msg, vkey, base_url)
                      if llm_vendors else "大模型接口错误 %s: %s" % (e.code, msg or e.reason))
    except urllib.error.URLError as e:
        return None, (llm_vendors.error_hint("network", getattr(e, "reason", str(e)), vkey, base_url)
                      if llm_vendors else "无法连接大模型服务: %s" % e)
    except socket.timeout:
        return None, (llm_vendors.error_hint("timeout", "", vkey, base_url)
                      if llm_vendors else "大模型请求超时")
    except json.JSONDecodeError:
        return None, (llm_vendors.error_hint("not_json", "", vkey, base_url)
                      if llm_vendors else "大模型返回不是 JSON：请检查 base URL")
    except Exception as e:
        return None, "大模型调用失败: %s" % e


def llm_call(messages, cfg, temperature=None):
    """调用大模型 /chat/completions（经厂商适配层），返回 (content, err)。"""
    payload = {"messages": messages}
    if temperature is not None:
        payload["temperature"] = temperature
    data, err = _chat_completions(payload, cfg)
    if err:
        return None, err
    try:
        content = data["choices"][0]["message"].get("content")
        if content in (None, ""):
            raise ValueError("empty content")
        return content, None
    except Exception:
        if llm_vendors:
            vkey = ((cfg or {}).get("vendor_info") or {}).get("vendor") or \
                   llm_vendors.detect_vendor((cfg or {}).get("base_url"),
                                             (cfg or {}).get("vendor"))[0]
            return None, llm_vendors.error_hint("no_content", "", vkey,
                                                (cfg or {}).get("base_url", ""))
        return None, "大模型返回格式异常"


# ---------------------------------------------------------------------------
# 平台 Agent：AI 助手自我认知 + 跨模块工具调用（Function Calling）
# ---------------------------------------------------------------------------
AGENT_SYSTEM_PROMPT = (
    "你是「{name}」（别称：{nick}），催化智构 CataMind 材料计算平台的智能体助手（Platform Agent）。"
    "你当前由大模型 gpt-5.6-sol 驱动（推理强度由用户配置）。\n"
    "你直接运行在平台内部，拥有调用平台各模块工具的真实权限，可以跨模块执行完整工作流：\n"
    "  · 研究体系：查询/检索体系库，查看描述符（d带中心 ε_d、η_OER、功函数等）\n"
    "  · 文献与知识库：检索文献库、CP2K 参数知识库\n"
    "  · 结构生成：用 ASE 生成晶体/表面/吸附结构\n"
    "  · CP2K 输入：按计算类型生成/推荐输入文件\n"
    "  · 机器学习：多模型集成对比训练（compare_ml_models / get_ml_comparison，**选模型优先用它**）、"
    "单模型训练（GPU 双卡并行：XGBoost / LightGBM / PyTorch MLP / GPyTorch GPR）、查看训练任务\n"
    "  · 主动学习：**多描述符主动学习（start_al 传 dataset_file → GPR + UCB/EI/PI + 分组 OOF + "
    "外推母体 AL 模拟，用 get_al_result 看结果，推荐优先用它）**；内置 1 维贝叶斯优化"
    "（BoTorch qEI/qUCB、skopt GP、Optuna TPE）、查看任务\n"
    "  · 计算任务：查看 CP2K 计算任务、AiiDA 进程、提交计算\n"
    "  · AiiDA 任务文件（按 PK）：检索任务工作路径与文件清单、读取输出/cube 等文件内容\n"
    "  · 结构处理：对已有体系做平移归一化（下表面 ≤10 Å、上真空 ≥25 Å，不足自动扩展晶胞 Z 轴）\n"
    "  · ASE 定制代码：运行自定义 ASE 代码批量生成结构（预览 2~3 个或正式批量入库）\n"
    "  · 吸附位点分析：按 AiiDA PK 取回优化后结构，识别表面位点并给出配位环境编码（含 PBC 校准）\n"
    "  · 差分电荷密度：对 total/slab/ads 三个 cube 逐格点相减，输出 delta.cube 与全空间密度积分（不等同转移电荷）\n"
    "  · 本地技能库：知识库中「平台技能SKILL」分类收录了本机的 SKILL 技能文档，可检索学习并用于指导操作\n"
    "  · 记忆：检索自己的持久化记忆（与平台知识库双向同步），并把对话中确认的知识沉淀写回平台知识库\n"
    "工作规则：\n"
    "1. 需要平台数据时，先调用工具获取真实数据，再基于工具结果回答；严禁编造数据。\n"
    "2. 一次可连续调用多个工具完成任务（如：先查体系 → 再启动训练 → 查看任务状态）。\n"
    "3. 有副作用的操作（启动训练/主动学习、提交计算）执行前，向用户简要说明将要执行的动作。\n"
    "4. 用中文回答，专业术语可保留英文。\n"
    "5. 涉及模型评估时：单模型训练返回的 r2/mae 是在**全部样本**上算的（含训练点，乐观偏高约 6 倍），"
    "cv_r2_mean 用的是**不打乱**的 KFold 且与 GPU 路径口径不一致，二者都不能用于跨模型排序；"
    "需要比较模型、判断泛化能力或回答某模型行不行时，改用 compare_ml_models（打乱 RepeatedKFold 主指标），"
    "并在数据带分组列（如 parent_id）时传 group_col 以获得分组 OOF —— 那才是换体系的真实泛化能力。"
    "报 MAE 时必须同时给出 baseline（预测训练集均值）作对照。\n"
    "6. 描述符口径：过渡金属看 d 带中心 ε_d；O/S/N 等**主族元素看 p 带中心 ε_p**"
    "（其 d 通道只是基组极化投影，占据区通常仅占几个百分点，不能当物理 d 带）。\n"
)

# ==================== LLM Agent 高权限工具管控（整改 [高4] P0-4）====================
# 这些工具具备"真实副作用"：提交真实计算 / 执行任意 ASE 代码并入库 / 改写 systems 表 /
# 触发吸附位点分析。为防止 prompt 注入被诱导批量执行，默认**不暴露给模型**，
# 必须由用户在前端显式开启（请求带 allow_high_priv=true）后才会出现在工具列表中；
# 执行前另有二次拦截（即使模型绕过工具列表直接点名调用也会被拒绝）。
HIGH_PRIV_TOOLS = {
    "submit_calc",                 # 提交真实 CP2K/AiiDA 计算，消耗算力
    "run_ase_custom_code",         # 执行任意 ASE 代码，可写库
    "normalize_structure",         # 直接改写 systems 表结构
    "analyze_adsorption_sites",    # 触发吸附位点分析（写库）
}

# 服务端硬开关：环境变量 CATMAT_AGENT_HIGH_PRIV=1 才允许前端开启（默认关闭）
AGENT_HIGH_PRIV_ENABLED = os.environ.get("CATMAT_AGENT_HIGH_PRIV") == "1"


def agent_tools_for(allow_high_priv=False):
    """按开关返回本次对话可用的工具列表（高权限工具默认不可见 → 模型无从调用）。"""
    allow = bool(allow_high_priv) and AGENT_HIGH_PRIV_ENABLED
    if allow:
        return AGENT_TOOLS
    return [t for t in AGENT_TOOLS
            if (t.get("function") or {}).get("name") not in HIGH_PRIV_TOOLS]


AGENT_TOOLS = [
    {"type": "function", "function": {
        "name": "get_platform_stats",
        "description": "获取平台统计信息：研究体系数 / 文献数 / CP2K 知识条目 / 计算任务数 / ML 样本数",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "list_systems",
        "description": "列出平台研究体系（可按名称/化学式关键词过滤），返回 id/名称/化学式/空间群/描述符摘要",
        "parameters": {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "名称或化学式关键词（可选）"},
            "limit": {"type": "integer", "description": "返回条数，默认 20"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_system",
        "description": "获取单个体系的完整信息：描述符（ε_d、η_OER、功函数等）、结构、来源",
        "parameters": {"type": "object", "properties": {
            "system_id": {"type": "integer", "description": "体系 ID"}}, "required": ["system_id"]}}},
    {"type": "function", "function": {
        "name": "search_literature",
        "description": "检索平台文献库（标题/材料体系/结论关键词）",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "检索关键词"},
            "limit": {"type": "integer"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "search_web_literature",
        "description": "联网检索学术文献（OpenAlex + CrossRef），按主题/关键词返回标题/期刊/年份/DOI",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "主题或关键词，如 酸性OER 铱基 催化剂"},
            "limit": {"type": "integer", "description": "返回条数，默认 8"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "import_literature_by_doi",
        "description": "按 DOI 导入文献到平台文献库（CrossRef 元数据 + 摘要，可选 AI 解读归档）",
        "parameters": {"type": "object", "properties": {
            "doi": {"type": "string", "description": "文献 DOI，如 10.1126/science.aaf5050"},
            "interpret": {"type": "boolean", "description": "是否用 AI 深度解读归档，默认 false"}},
            "required": ["doi"]}}},
    {"type": "function", "function": {
        "name": "search_knowledge",
        "description": "检索平台 CP2K 参数知识库（关键词/参数名）",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "检索关键词"},
            "limit": {"type": "integer"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "generate_structure",
        "description": "用 ASE 生成晶体/表面/吸附结构，并加入研究体系库",
        "parameters": {"type": "object", "properties": {
            "symbols": {"type": "string", "description": "元素符号，如 'Pt'（单元素；合金请用 alloy + alloy_frac）"},
            "kind": {"type": "string", "enum": ["slab", "adsorb"], "description": "slab 表面 / adsorb 表面吸附（结构生成仅支持表面模型，不支持 bulk 体相）"},
            "hkl": {"type": "string", "description": "表面指数，如 '111'（slab/adsorb 用）"},
            "layers": {"type": "integer", "description": "表面层数（slab/adsorb）"},
            "vacuum": {"type": "number", "description": "真空层厚度 Å（slab/adsorb）"},
            "adsorbate": {"type": "string", "enum": ["OH", "O", "H2O", "H"], "description": "吸附分子（adsorb）"},
            "site": {"type": "string", "enum": ["top", "bridge", "hollow"], "description": "吸附位点（adsorb）"},
            "name": {"type": "string", "description": "自定义名称"}},
            "required": ["symbols"]}}},
    {"type": "function", "function": {
        "name": "recommend_inp",
        "description": "生成/推荐 CP2K 输入文件（可结合体系结构，返回 .inp 内容）",
        "parameters": {"type": "object", "properties": {
            "calc_type": {"type": "string", "enum": ["energy", "geo_opt", "md", "ot", "pdos"]},
            "structure_text": {"type": "string", "description": "结构 XYZ 文本（**必填**）：可先用 generate_structure 生成，或从体系库取该体系的 xyz"},
            "dispersion": {"type": "string", "enum": ["d3bj", "none"]},
            "smearing": {"type": "boolean"}},
            "required": ["calc_type", "structure_text"]}}},
    {"type": "function", "function": {
        "name": "train_ml",
        "description": "启动机器学习模型训练（后台任务；GPU 双卡并行支持 xgb/mlp_torch/gpr_torch）",
        "parameters": {"type": "object", "properties": {
            "model": {"type": "string", "enum": ["rf", "xgb", "lgbm", "mlp_torch", "gpr_torch", "gpr", "svr", "knn", "linear"], "description": "模型类型"},
            "device": {"type": "string", "enum": ["auto", "cuda", "cpu"]},
            "params": {"type": "object", "description": "训练参数（n_estimators/n_jobs 等，可选）"}},
            "required": ["model"]}}},
    {"type": "function", "function": {
        "name": "list_ml_tasks",
        "description": "查看机器学习训练任务列表与状态（进行中/完成/失败/已终止）",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "compare_ml_models",
        "description": ("【多模型集成对比训练·推荐优先使用】一次对多个回归模型跑同一套严谨协议并直接给出可排序的排名。"
                        "平台单模型接口的 r2/mae 含训练点（乐观约 6×）、cv_r2_mean 用不打乱的 KFold（按分组有序的数据上会退化），"
                        "因此单模型指标不可用于选模型；本工具用打乱 RepeatedKFold 作主指标，并可附加分组 OOF 与预注册切分。"
                        "后台运行，用 get_ml_comparison 查结果。"),
        "parameters": {"type": "object", "properties": {
            "dataset_file": {"type": "string",
                             "description": "数据集文件名（ML-xxx.csv，受管目录 data/datasets 下），省略则用 ml_samples 全表"},
            "dataset_module": {"type": "string", "enum": ["ml"], "description": "固定 ml"},
            "models": {"type": "array", "items": {"type": "string"},
                       "description": ("要对比的模型名；省略 = 全部 22 个。可选：linear/ridge/lasso/elasticnet/"
                                       "bayesian_ridge/huber/svr/kernel_ridge/gpr/knn/pls/decision_tree/rf/"
                                       "extra_trees/gradient_boosting/hist_gradient_boosting/adaboost/xgb/lgbm/"
                                       "mlp/mlp_torch/gpr_torch")},
            "cv_folds": {"type": "integer", "description": "CV 折数，默认 5"},
            "cv_repeats": {"type": "integer", "description": "重复次数（打乱），默认 10；越大越稳"},
            "group_col": {"type": "string",
                          "description": "分组列名（如 parent_id）。给了就追加 LeaveOneGroupOut 分组 OOF —— 只有它才反映换母体/新体系的泛化能力"},
            "fixed_split_col": {"type": "string",
                                "description": "预注册切分列名（如 fixed_split，取值 train/validation/test）"},
            "preprocess": {"type": "boolean", "description": "是否 StandardScaler，默认 true"},
            "test_size": {"type": "number", "description": "随机留出比例，默认 0.2"},
            "shap": {"type": "boolean", "description": "是否对树模型算 SHAP，默认 true"},
            "params": {"type": "object", "description": "每模型超参覆盖，如 {\"extra_trees\": {\"n_estimators\": 500}}，可选"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "get_ml_comparison",
        "description": ("查看多模型对比训练任务：不传 id 返回最近若干条对比任务摘要；传 id 返回该任务的完整排名"
                        "（cv_r2/cv_mae/分组 OOF/预注册切分/留出）、协议设置、跨模型 SHAP 前 15 与朴素基线。"
                        "判断模型行不行必须把 MAE 与 baseline 对照看。"),
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer", "description": "对比任务 id；省略则列出最近的任务"},
            "limit": {"type": "integer", "description": "列表模式返回条数，默认 5"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "start_al",
        "description": ("启动主动学习。**给了 dataset_file 就走多描述符模式（推荐）**："
                        "从数据集文件读任意多列特征 + 任意目标，GPR 代理 + 分组标定噪声 + UCB/EI/PI 采集；"
                        "无 candidate_file 时做'外推母体'AL 模拟（AL vs 同预算随机），"
                        "用 get_al_result 看分组 OOF、置换重要度与 AL 收益。"
                        "不给 dataset_file 则走内置 1 维模式（ε_d → η_OER，取 systems 表）。"),
        "parameters": {"type": "object", "properties": {
            "method": {"type": "string", "enum": ["gpr_ucb", "gpr_ei", "gpr_pi", "botorch_qei", "botorch_qucb", "skopt_gp", "optuna_tpe"],
                       "description": "多描述符模式支持 gpr_ucb/gpr_ei/gpr_pi"},
            "dataset_file": {"type": "string",
                             "description": "数据集文件名（ML-xxx.csv，受管目录下）；给了即启用多描述符模式"},
            "group_col": {"type": "string",
                          "description": "分组列名（如 parent_id）——**强烈建议传**，分组 OOF 与 AL 模拟都依赖它"},
            "exclude_cols": {"type": "array", "items": {"type": "string"},
                             "description": "排除出特征的元数据列；**seed/composition 一定要排**（数值型批次变量会被当特征导致 CV 虚高）"},
            "target": {"type": "string", "description": "目标列名，默认 target"},
            "kappa": {"type": "number", "description": "UCB 探索系数，默认 2.0"},
            "minimize": {"type": "boolean", "description": "目标是否越小越好，默认 true"},
            "batch_size": {"type": "integer", "description": "每批推荐数，默认 10"},
            "candidate_file": {"type": "string", "description": "无标签候选集文件名（可选）"},
            "device": {"type": "string", "enum": ["auto", "cuda", "cpu"]}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "get_al_result",
        "description": ("查看多描述符主动学习结果：不传 id 列最近若干次；传 id 返回完整结果 —— "
                        "分组 OOF（R²/MAE + 逐组明细）、朴素基线、噪声下界、置换重要度前 20、"
                        "AL 模拟学习曲线（al vs random，al_gain_vs_random_final > 0 才说明 AL 优于随机）、"
                        "以及候选推荐列表。报 MAE 必须与基线对照。"),
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer", "description": "任务 id；省略则列出最近的任务"},
            "limit": {"type": "integer", "description": "列表模式返回条数，默认 5"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "list_al_tasks",
        "description": "查看主动学习任务列表与状态",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "list_calc_tasks",
        "description": "查看 CP2K 计算任务列表与状态（排队/运行/完成/失败）",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "返回条数，默认 15"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "list_aiida_processes",
        "description": "查看 AiiDA 计算进程快照（运行中的计算）",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "submit_calc",
        "description": "提交研究体系的计算任务（如 PDOS/能量计算，通过 verdi 提交）",
        "parameters": {"type": "object", "properties": {
            "system_id": {"type": "integer", "description": "体系 ID"},
            "calc_type": {"type": "string", "enum": ["energy", "geo_opt", "pdos", "md"]}},
            "required": ["system_id", "calc_type"]}}},
    {"type": "function", "function": {
        "name": "aiida_task_files",
        "description": "按 AiiDA 工作流 PK 检索任务的工作路径与文件清单；传 filename 时读取该文件内容（用于查看 CP2K 输出、cube、结构等）",
        "parameters": {"type": "object", "properties": {
            "pk": {"type": "integer", "description": "AiiDA 进程 PK（CalcJob 或 WorkChain）"},
            "filename": {"type": "string", "description": "可选：要读取的文件名（如 aiida.out / *.cube）"}},
            "required": ["pk"]}}},
    {"type": "function", "function": {
        "name": "normalize_structure",
        "description": "对已有体系做平移归一化：结构下表面最高不超过 10 Å，上表面真空层最低不低于 25 Å（不足则自动扩展晶胞 Z 轴）",
        "parameters": {"type": "object", "properties": {
            "system_id": {"type": "integer", "description": "体系 ID"},
            "z_bottom_max": {"type": "number", "description": "下表面最大高度，默认 10 Å"},
            "vacuum_top_min": {"type": "number", "description": "上表面真空层最小厚度，默认 25 Å"}},
            "required": ["system_id"]}}},
    {"type": "function", "function": {
        "name": "run_ase_custom_code",
        "description": "运行自定义 ASE 代码批量生成结构（代码需定义 build_structures(count=n) 返回 Atoms 列表）；preview=true 只生成 2~3 个预览不入库",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "description": "Python 代码（仅允许 ase/numpy/random），需定义 build_structures(count=n)"},
            "count": {"type": "integer", "description": "生成数量，默认 3"},
            "preview": {"type": "boolean", "description": "true=仅预览不入库，默认 true"}},
            "required": ["code"]}}},
    {"type": "function", "function": {
        "name": "analyze_adsorption_sites",
        "description": "从 AiiDA 库按 PK 取回（优化后的）结构，识别表面吸附位点并给出配位环境编码（含周期边界 PBC 校准）",
        "parameters": {"type": "object", "properties": {
            "pk_list": {"type": "array", "items": {"type": "integer"}, "description": "AiiDA 结构 PK 列表"},
            "a": {"type": "number", "description": "可选：最近邻基准晶格常数"}},
            "required": ["pk_list"]}}},
    {"type": "function", "function": {
        "name": "run_bader_diff",
        "description": "差分电荷密度：delta = total - slab - ads 三个 cube 逐格点相减，输出 delta.cube 与全空间密度积分（e），不执行原子盆划分，不返回电荷转移量",
        "parameters": {"type": "object", "properties": {
            "total_cube": {"type": "string", "description": "整体（slab+吸附）电荷密度 cube 路径"},
            "slab_cube": {"type": "string", "description": "slab 单独 cube 路径"},
            "ads_cube": {"type": "string", "description": "吸附分子单独 cube 路径"},
            "density_unit": {"type": "string", "enum": ["e/bohr^3"], "description": "必须先确认单位"},
            "output_dir": {"type": "string", "description": "输出目录（默认 bader_diff）"}},
            "required": ["total_cube", "slab_cube", "ads_cube", "density_unit"]}}},
    {"type": "function", "function": {
        "name": "search_agent_memory",
        "description": "检索休go的持久化记忆（与平台知识库同步的分类 / 关键词 / 推荐值 / 理由）；按关键词或分类查找，返回最相关条目",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "检索关键词（匹配 topic / keyword / content）"},
            "category": {"type": "string", "description": "限定分类（可选，如 SCF 收敛 / OT 方法 / 平台技能SKILL）"},
            "limit": {"type": "integer", "description": "返回条数，默认 8"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "add_agent_memory",
        "description": "把对话中确认的知识或结论写入休go记忆；sync_to_knowledge=true 时同步回写平台知识库（origin=休go沉淀），实现记忆 → 知识库的反向同步",
        "parameters": {"type": "object", "properties": {
            "category": {"type": "string", "description": "分类，如 SCF 收敛 / 几何优化 / 平台技能SKILL"},
            "topic": {"type": "string", "description": "主题标题"},
            "keyword": {"type": "string", "description": "关键词（便于检索）"},
            "content": {"type": "string", "description": "知识正文（推荐值 + 理由 + 出处）"},
            "sync_to_knowledge": {"type": "boolean", "description": "是否回写平台知识库，默认 true"}},
            "required": ["topic", "content"]}}},
    {"type": "function", "function": {
        "name": "sync_knowledge_memory",
        "description": "手动触发「平台知识库 → 休go记忆」的增量同步（仅新增/变更条目会写入）",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
]


def agent_execute_tool(conn, name, args):
    """执行平台 Agent 工具，返回 (ok, result_dict / error)。"""
    args = args or {}
    try:
        if name == "get_platform_stats":
            return True, {
                "systems": conn.execute("SELECT COUNT(*) c FROM systems").fetchone()["c"],
                "literature": conn.execute("SELECT COUNT(*) c FROM literature").fetchone()["c"],
                "knowledge": conn.execute("SELECT COUNT(*) c FROM knowledge").fetchone()["c"],
                "calc_tasks": conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"],
                "ml_samples": conn.execute("SELECT COUNT(*) c FROM ml_samples").fetchone()["c"],
            }
        if name == "list_systems":
            kw = str(args.get("keyword") or "").strip()
            limit = min(int(args.get("limit") or 20), 100)
            if kw:
                like = "%" + kw + "%"
                rows = conn.execute(
                    "SELECT id,name,formula,space_group,source,descriptors FROM systems "
                    "WHERE name LIKE ? OR formula LIKE ? ORDER BY id DESC LIMIT ?",
                    (like, like, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id,name,formula,space_group,source,descriptors FROM systems "
                    "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                ds = d.pop("descriptors", "[]")
                try:
                    dd = json.loads(ds or "[]")
                    d["descriptors"] = [{"name": x.get("name"), "value": x.get("value")} for x in dd]
                except Exception:
                    d["descriptors"] = []
                out.append(d)
            return True, {"count": len(out), "systems": out}
        if name == "get_system":
            sid = int(args.get("system_id") or 0)
            r = conn.execute("SELECT * FROM systems WHERE id=?", (sid,)).fetchone()
            if not r:
                return False, "体系不存在（id=%s）" % sid
            d = dict(r)
            try:
                d["descriptors"] = json.loads(d.get("descriptors") or "[]")
            except Exception:
                d["descriptors"] = []
            try:
                d["structure"] = json.loads(d.get("structure") or "[]")
            except Exception:
                d["structure"] = []
            return True, d
        if name == "search_literature":
            q = str(args.get("query") or "").strip()
            limit = min(int(args.get("limit") or 5), 20)
            like = "%" + q + "%"
            rows = conn.execute(
                "SELECT id,title,authors,journal,year,doi,material_system,conclusion FROM literature "
                "WHERE title LIKE ? OR material_system LIKE ? OR conclusion LIKE ? LIMIT ?",
                (like, like, like, limit)).fetchall()
            return True, {"count": len(rows), "literature": [dict(r) for r in rows]}
        if name == "search_web_literature":
            q = str(args.get("query") or "").strip()
            limit = min(int(args.get("limit") or 8), 20)
            results, err = web_search(q, limit)
            if err:
                return False, err
            return True, {"count": len(results), "query": q,
                          "results": [dict(r) for r in results]}
        if name == "import_literature_by_doi":
            doi = str(args.get("doi") or "").strip()
            if not doi:
                return False, "缺少 doi"
            meta, err = fetch_doi(doi)
            if err:
                return False, err
            abstract, abs_src, _ = fetch_abstract(doi)
            meta["abstract"] = abstract
            exist = conn.execute("SELECT id FROM literature WHERE doi=?", (meta["doi"],)).fetchone()
            if exist:
                return True, {"id": exist["id"], "duplicated": True, "title": meta["title"]}
            conn.execute(
                "INSERT INTO literature(title,authors,journal,year,doi,electrolyzer,dft_code,abstract,conclusion,tags,verified) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (meta["title"], meta["authors"], meta["journal"], meta["year"], meta["doi"],
                 None, None, abstract, "由 DOI 自动导入（待解读归档）", "[]", 0))
            lid = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]
            interp = None
            if args.get("interpret"):
                interp = interpret_literature_core(conn, lid, archived_by="AI 助手(休go)")
                if isinstance(interp, dict) and interp.get("error"):
                    interp = None
            return True, {"id": lid, "duplicated": False, "title": meta["title"],
                          "interpreted": bool(interp)}
        if name == "search_knowledge":
            q = str(args.get("query") or "").strip()
            limit = min(int(args.get("limit") or 5), 20)
            like = "%" + q + "%"
            # 「统一定义标准」条目优先展示（topic 前缀【统一定义标准】/ origin 含"定义标准"），
            # 保证权威口径不被低 rowid 的旧条目挤出 LIMIT；其余保持原顺序。
            rows = conn.execute(
                "SELECT id,category,topic,keyword,recommended,rationale FROM knowledge "
                "WHERE topic LIKE ? OR keyword LIKE ? OR recommended LIKE ? OR rationale LIKE ? "
                "ORDER BY (topic LIKE '【统一定义标准】%' OR origin LIKE '%定义标准%') DESC, id "
                "LIMIT ?",
                (like, like, like, like, limit)).fetchall()
            return True, {"count": len(rows), "knowledge": [dict(r) for r in rows]}

        # ---- 休go记忆 ↔ 平台知识库 双向同步 ----
        if name == "search_agent_memory":
            q = str(args.get("query") or "").strip()
            cat = str(args.get("category") or "").strip()
            limit = min(int(args.get("limit") or 8), 30)
            sql = ("SELECT id,category,topic,keyword,content,source,origin FROM agent_memory "
                   "WHERE 1=1")
            params = []
            if cat:
                sql += " AND category LIKE ?"
                params.append("%" + cat + "%")
            if q:
                sql += " AND (topic LIKE ? OR keyword LIKE ? OR content LIKE ?)"
                params += ["%" + q + "%"] * 3
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return True, {"count": len(rows), "memories": [dict(r) for r in rows]}

        if name == "add_agent_memory":
            topic = str(args.get("topic") or "").strip()
            content = str(args.get("content") or "").strip()
            if not topic or not content:
                return False, "缺少 topic 或 content"
            category = str(args.get("category") or "休go沉淀").strip() or "休go沉淀"
            keyword = str(args.get("keyword") or topic).strip()
            to_kb = bool(args.get("sync_to_knowledge", True))
            now = now_str()
            cur = conn.execute(
                "INSERT INTO agent_memory (category, topic, keyword, content, source, "
                "origin, created_at, synced_at) VALUES (?,?,?,?,?,?,?,?)",
                (category, topic, keyword, content, "chat", "休go沉淀", now, now))
            mem_id = cur.lastrowid
            kb_id = None
            if to_kb:
                # 反向同步：回写平台知识库，并把 knowledge.id 记回记忆条目形成双向关联
                k = conn.execute(
                    "INSERT INTO knowledge (category, topic, keyword, recommended, rationale, "
                    "source, origin, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (category, topic, keyword, content,
                     "由 AI 助手「休go」在对话中沉淀",   # rationale
                     "休go沉淀",                        # source
                     "休go沉淀",                        # origin
                     now))
                kb_id = k.lastrowid
                conn.execute("UPDATE agent_memory SET ref_id=?, synced_at=? WHERE id=?",
                             (kb_id, now, mem_id))
            conn.commit()
            LOG.info("休go写入记忆 #%s（回写知识库：%s）", mem_id, kb_id)
            return True, {"memory_id": mem_id, "knowledge_id": kb_id,
                          "synced_to_knowledge": bool(kb_id)}

        if name == "sync_knowledge_memory":
            added, updated, skipped, total = sync_knowledge_to_agent_memory(conn)
            return True, {"added": added, "updated": updated,
                          "skipped": skipped, "knowledge_total": total}

        if name == "generate_structure":
            res, err = _build_ase_wrap(args)
            if err:
                return False, err
            return True, res
        if name == "recommend_inp":
            calc_type = str(args.get("calc_type") or "geo_opt")
            structure_text = str(args.get("structure_text") or "").strip()
            dispersion = str(args.get("dispersion") or "d3bj")
            smearing = bool(args.get("smearing", True))
            if not structure_text:
                # 无结构时 Multiwfn 无法生成 INP（description 中若只给 calc_type 会到这里），
                # 明确告知下一步该调哪个工具，避免模型反复重试同一调用。
                return False, ("缺少结构（structure_text）。请先用 generate_structure 生成结构、"
                               "或从体系库读取某体系的 structure 转成 xyz 后再调用本工具。")
            inp, err = generate_multiwfn_inp(structure_text, calc_type, dispersion, smearing)
            if err:
                return False, err
            return True, {"calc_type": calc_type, "source": "multiwfn", "inp": inp}
        if name == "train_ml":
            model = str(args.get("model") or "rf")
            device = str(args.get("device") or "auto")
            params = dict(args.get("params") or {})
            params["model"] = model
            params["device"] = device
            # 编排层：显式数据集文件（ML-xxx.csv / AL-xxx.csv）透传给训练任务
            if args.get("dataset_file"):
                params["dataset_file"] = str(args["dataset_file"])
                params["dataset_module"] = str(args.get("dataset_module") or "ml")
            tid, err = _start_task("ml", "agent_训练_" + model, model, device, params)
            if err:
                return False, err
            return True, {"task_id": tid, "model": model, "device": device, "status": "running"}
        if name == "list_ml_tasks":
            _sync_ml_tasks(conn)
            rows = conn.execute(
                "SELECT id,name,model,device,status,progress,created_at,error FROM ml_tasks "
                "WHERE task_type='ml' ORDER BY id DESC LIMIT 10").fetchall()
            return True, {"count": len(rows), "tasks": [dict(r) for r in rows]}
        if name == "compare_ml_models":
            _me = getattr(self, "auth_user", None)
            tid, err = _start_compare_task(
                args.get("name") or "agent_多模型对比", args or {},
                owner_id=_me["id"] if _me is not None else None)
            if err:
                return False, err
            return True, {"task_id": tid, "status": "running",
                          "n_models": len(args.get("models") or COMPARE_DEFAULT_MODELS),
                          "hint": "用 get_ml_comparison(id=%d) 查看排名与指标" % tid}
        if name == "get_ml_comparison":
            _sync_ml_tasks(conn)
            cid = args.get("id")
            if cid:
                r = conn.execute("SELECT * FROM ml_tasks WHERE id=? AND task_type='mlcmp'",
                                 (int(cid),)).fetchone()
                if not r:
                    return False, "对比任务 %s 不存在" % cid
                return True, _cmp_brief(r, with_result=True)
            lim = max(1, min(20, int(args.get("limit") or 5)))
            rows = conn.execute(
                "SELECT id,name,status,progress,created_at,finished_at,error,result "
                "FROM ml_tasks WHERE task_type='mlcmp' ORDER BY id DESC LIMIT ?",
                (lim,)).fetchall()
            return True, {"count": len(rows), "comparisons": [_cmp_brief(r) for r in rows]}
        if name == "start_al":
            method = str(args.get("method") or "gpr_ucb")
            batch = max(1, int(args.get("batch_size") or 5))
            device = str(args.get("device") or "auto")
            _me = getattr(self, "auth_user", None)
            # 给了 dataset_file → 多描述符模式
            if args.get("dataset_file"):
                body = dict(args)
                body["method"] = method if method in ("gpr_ucb", "gpr_ei", "gpr_pi") else "gpr_ucb"
                body["batch_size"] = batch
                tid, err = _start_al_multidim("agent_AL多维_" + body["method"], body,
                                              owner_id=_me["id"] if _me is not None else None)
                if err:
                    return False, err
                return True, {"task_id": tid, "mode": "multidim", "method": body["method"],
                              "batch_size": batch, "status": "running",
                              "hint": "用 get_al_result(id=%d) 查看分组 OOF / AL 收益 / 推荐" % tid}
            body = {"method": method, "batch_size": batch, "device": device}
            tid, err = _start_task("al", "agent_AL_" + method, method, device, body)
            if err:
                return False, err
            return True, {"task_id": tid, "mode": "legacy_1d", "method": method,
                          "batch_size": batch, "status": "running"}
        if name == "get_al_result":
            _sync_ml_tasks(conn)
            rid = args.get("id")
            if rid:
                r = conn.execute("SELECT * FROM ml_tasks WHERE id=? AND task_type='almd'",
                                 (int(rid),)).fetchone()
                if not r:
                    return False, "多描述符主动学习任务 %s 不存在" % rid
                return True, _al_brief(r, with_result=True)
            lim = max(1, min(20, int(args.get("limit") or 5)))
            rows = conn.execute(
                "SELECT id,name,status,progress,created_at,finished_at,error,result "
                "FROM ml_tasks WHERE task_type='almd' ORDER BY id DESC LIMIT ?",
                (lim,)).fetchall()
            return True, {"count": len(rows), "runs": [_al_brief(r) for r in rows]}
        if name == "list_al_tasks":
            _sync_ml_tasks(conn)
            rows = conn.execute(
                "SELECT id,name,model,device,status,progress,created_at,error FROM ml_tasks "
                "WHERE task_type='al' ORDER BY id DESC LIMIT 10").fetchall()
            return True, {"count": len(rows), "tasks": [dict(r) for r in rows]}
        if name == "list_calc_tasks":
            limit = min(int(args.get("limit") or 15), 50)
            rows = conn.execute(
                "SELECT id,name,status,created_at,finished_at,error FROM tasks "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return True, {"count": len(rows), "tasks": [dict(r) for r in rows]}
        if name == "list_aiida_processes":
            data, err = process_snapshot()
            if err:
                return False, err
            return True, data
        if name == "submit_calc":
            sid = int(args.get("system_id") or 0)
            calc_type = str(args.get("calc_type") or "energy")
            row = conn.execute("SELECT id,name FROM systems WHERE id=?", (sid,)).fetchone()
            if not row:
                return False, "体系不存在（id=%s）" % sid
            data, err = submit_verdi_calc(conn, "AGENT_" + (row["name"] or str(sid)), sid, calc_type)
            if err:
                return False, err
            return True, {"system_id": sid, "name": row["name"], "calc_type": calc_type, **data}
        # ---- AI 助手：AiiDA 任务路径检索与文件处理 ----
        if name == "aiida_task_files":
            pk = int(args.get("pk") or 0)
            if not pk:
                return False, "缺少 pk 参数"
            fn = str(args.get("filename") or "").strip() or None
            info, err = aiida_task_files(pk, fn)
            if err:
                return False, err
            return True, info
        if name == "normalize_structure":
            sid = int(args.get("system_id") or 0)
            row = conn.execute("SELECT id,name,structure FROM systems WHERE id=?", (sid,)).fetchone()
            if not row:
                return False, "体系不存在（id=%s）" % sid
            try:
                st = json.loads(row["structure"] or "{}")
            except Exception:
                return False, "该体系无有效结构"
            if not st.get("symbols"):
                return False, "该体系缺少结构坐标"
            atoms = ase_atoms_from_dict({
                "symbols": st["symbols"], "cell": st["cell"],
                "scaled_positions": st["scaled_positions"],
                "pbc": st.get("pbc", [True, True, True])})
            import numpy as np
            z0 = float(np.asarray(atoms.get_positions())[:, 2].min())
            c0 = float(atoms.get_cell()[2][2])
            normalize_slab_vacuum(atoms, z_bottom_max=float(args.get("z_bottom_max") or 10.0),
                                  vacuum_top_min=float(args.get("vacuum_top_min") or 25.0))
            pos = np.asarray(atoms.get_positions())
            new_st = {"symbols": atoms.get_chemical_symbols(),
                      "cell": [[float(round(x, 6)) for x in v] for v in atoms.get_cell()],
                      "scaled_positions": [[float(round(x, 6)) for x in sp]
                                           for sp in atoms.get_scaled_positions()],
                      "pbc": [bool(x) for x in atoms.get_pbc()]}
            conn.execute("UPDATE systems SET structure=? WHERE id=?", (json.dumps(new_st), sid))
            return True, {"system_id": sid, "name": row["name"],
                          "before": {"zmin": round(z0, 3), "c": round(c0, 3)},
                          "after": {"zmin": round(float(pos[:, 2].min()), 3),
                                    "c": round(float(atoms.get_cell()[2][2]), 3),
                                    "vacuum_top": round(float(atoms.get_cell()[2][2]) - float(pos[:, 2].max()), 3)}}
        if name == "run_ase_custom_code":
            code = str(args.get("code") or "").strip()
            if not code:
                return False, "缺少 code 参数"
            n = max(1, min(int(args.get("count") or 3), 50))
            data, err = run_ase_user_code(code, n)
            if err:
                return False, err
            preview = bool(args.get("preview", True))
            if preview:
                return True, {"preview": True, "count": len(data),
                              "structures": [{k: s[k] for k in
                                              ("label", "formula", "natoms", "c", "zmin", "vacuum_top")}
                                             for s in data]}
            created = []
            for s in data:
                cur = conn.execute(
                    "INSERT INTO systems(name,formula,composition,space_group,lattice,elements,source,structure) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (s["label"], s["formula"], s["formula"], "ASE-AI",
                     "c=%.2f zmin=%.2f vac=%.2f" % (s["c"], s["zmin"], s["vacuum_top"]),
                     json.dumps(sorted(set(s["symbols"]))), "AI定制",
                     json.dumps({"symbols": s["symbols"], "cell": s["cell"],
                                 "scaled_positions": s["scaled_positions"],
                                 "pbc": s.get("pbc", [True, True, True])})))
                created.append({"system_id": cur.lastrowid, "label": s["label"],
                                "formula": s["formula"], "natoms": s["natoms"],
                                "zmin": s["zmin"], "vacuum_top": s["vacuum_top"]})
            return True, {"preview": False, "count": len(created), "created": created}
        if name == "analyze_adsorption_sites":
            pks = [int(p) for p in (args.get("pk_list") or []) if str(p).strip().lstrip("-").isdigit()]
            if not pks:
                return False, "缺少 pk_list"
            out = []
            for pk in pks[:20]:
                st, err = load_aiida_structure_json(pk)
                if err:
                    out.append({"pk": pk, "error": err})
                    continue
                atoms = ase_atoms_from_dict(st)
                sites, pairs = ase_analyze_sites(atoms, a=args.get("a"))
                for s in sites:
                    upsert_env_code(conn, s["env"])
                out.append({"pk": pk, "label": st.get("label"), "n_sites": len(sites),
                            "n_pairs": len(pairs),
                            "sites": sites[:12],
                            "env_summary": sorted({s["env"] for s in sites})[:12]})
            return True, {"count": len(out), "results": out}
        if name == "run_bader_diff":
            if args.get("density_unit") != "e/bohr^3":
                return False, "请显式确认 density_unit=e/bohr^3"
            tc = str(args.get("total_cube") or "").strip()
            sc = str(args.get("slab_cube") or "").strip()
            ac = str(args.get("ads_cube") or "").strip()
            if not (tc and sc and ac):
                return False, "需提供 total_cube / slab_cube / ads_cube 三个 cube 路径"
            for p in (tc, sc, ac):
                if not os.path.isfile(p):
                    return False, "cube 文件不存在: " + p
            out_dir = str(args.get("output_dir") or os.path.join(CP2K_RUN_DIR, "bader_diff"))
            try:
                out_dir, derr = resolve_run_dir(out_dir)
                if derr:
                    out_dir = os.path.join(CP2K_RUN_DIR, "bader_diff")
                os.makedirs(out_dir, exist_ok=True)
            except Exception:
                out_dir = os.path.join(CP2K_RUN_DIR, "bader_diff")
                os.makedirs(out_dir, exist_ok=True)
            token = secrets.token_hex(12)
            delta_path = os.path.join(out_dir, "delta_%s.cube" % token)
            try:
                with _blocking_unlocked():
                    obj = run_json([sys.executable, BADER_SCRIPT, tc, sc, ac, delta_path], timeout=300)
                return True, obj["result"]
            except Exception as exc:
                return False, "差分电荷处理失败: %s" % exc
        return False, "未知工具: %s" % name
    except Exception as e:
        return False, "工具 %s 执行失败: %s" % (name, e)


# Agent 工具入参（symbols/kind/hkl/site）→ 结构生成规范参数（element/face/ads_site）映射表
_AGENT_HKL_FACE = {"111": "fcc111", "100": "fcc100", "110": "fcc110", "0001": "hcp0001"}
_AGENT_SITE = {"top": "ontop", "ontop": "ontop", "bridge": "bridge",
               "hollow": "fcc", "fcc": "fcc", "hcp": "hcp", "center": "center"}


def _agent_ase_params(args):
    """把 Agent 工具入参翻译为 build_structure_ase 的规范参数。返回 (params, err)。

    工具面向模型暴露的是 symbols/kind/hkl/site 这套词汇，而结构生成内核用的是
    element/face/ads_site/...。两者不一致时**不能静默忽略**（会生成元素错误的结构），
    因此这里显式翻译并对不支持的取值直接报错。不规范参数一律拒绝，不做兜底猜测。
    """
    p = {}
    for k in ASE_PARAM_KEYS:
        if args.get(k) not in (None, ""):
            p[k] = args[k]
    if not p.get("element"):
        sym = str(args.get("symbols") or "").strip()
        if sym:
            els = re.findall(r"[A-Z][a-z]?", sym)
            uniq = sorted(set(els))
            if not els:
                return None, "无法解析 symbols：%s（示例：'Pt'）" % sym
            if len(uniq) > 1:
                return None, ("symbols 含多种元素（%s）。结构生成不按化学式直接建合金，"
                              "请改用 element + alloy + alloy_frac，例如 "
                              "element='Pt'、alloy='Fe'、alloy_frac=0.25" % "/".join(uniq))
            p["element"] = els[0]
    if not p.get("face"):
        kind = str(args.get("kind") or "").strip().lower()
        if kind == "bulk":
            return None, ("结构生成仅支持表面模型（fcc111 / fcc100 / fcc110 / hcp0001），"
                          "不支持 bulk 体相；请用 kind='slab' 并给出 hkl，或直接给 face 参数")
        hkl = str(args.get("hkl") or "").strip().strip("()")
        if hkl:
            face = _AGENT_HKL_FACE.get(hkl)
            if not face:
                return None, "不支持的 hkl：%s（可选 111 / 100 / 110 / 0001）" % hkl
            p["face"] = face
    if args.get("site") and not p.get("ads_site"):
        site = str(args["site"]).strip().lower()
        if site not in _AGENT_SITE:
            return None, "不支持的 site：%s（可选 top / bridge / hollow）" % args["site"]
        p["ads_site"] = _AGENT_SITE[site]
    if not p.get("element"):
        return None, "缺少 symbols 参数（元素符号，如 'Pt'）"
    if str(args.get("kind") or "").strip().lower() == "adsorb" and not p.get("adsorbate"):
        return None, "kind='adsorb' 需同时给出 adsorbate（OH / O / H2O / H）"
    return p, None


def _build_ase_wrap(args):
    """结构生成（Agent 用）：调 ASE 生成并入库，返回摘要信息。

    契约提醒：build_structure_ase **成功返回单个 dict，入参非法时抛 ApiError**，
    没有 (result, err) 二元组形态。此处曾误写为 `st, err = build_structure_ase(p)`，
    对 dict 解包会抛 "too many values to unpack" 并被兜底 except 吞掉，
    导致该 Agent 工具恒返回「结构生成失败」，且参数名（symbols/kind/hkl/site）也与内核
    契约不符。现改为先经 _agent_ase_params 翻译，再按 ApiError 契约取错误信息。
    """
    p, err = _agent_ase_params(args)
    if err:
        return None, err
    ok, verr = validate_ase_params(p)
    if not ok:
        return None, verr
    conn = None
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        try:
            st = build_structure_ase(p)
        except ApiError as e:
            return None, str(e.msg)
        name = str(args.get("name") or "").strip() or \
            (st.get("formula", "structure") + " " + str(p.get("face", "fcc111")))
        _insert_system(conn, {
            "name": name, "formula": st.get("formula", p.get("element")),
            "composition": st.get("formula", p.get("element")),
            "space_group": p.get("face", "fcc111"), "lattice": "ASE",
            "layers": p.get("layers"), "vacuum": p.get("vacuum"),
            "elements": sorted(set(st.get("symbols", []))),
            "source": "Agent 生成", "folder": "Agent生成",
            "structure": json.dumps({k: st.get(k) for k in
                                     ("symbols", "cell", "scaled_positions", "pbc")},
                                    ensure_ascii=False),
        })
        sid = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]
        conn.commit()
        xyz = ase_result_to_xyz(st)
        return {"system_id": sid, "name": name, "formula": st.get("formula"),
                "natoms": st.get("natoms"), "face": p.get("face", "fcc111"),
                "xyz": xyz[:2000]}, None
    except Exception as e:
        log_exc("agent generate_structure failed")
        return None, "结构生成失败: %s" % e
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

# ---------------------------------------------------------------------------
# Codex 对话通道（本机 Codex CLI，danger-full-access 档）
# ---------------------------------------------------------------------------
# 设计：把平台的 AI 助手对话接到本机运行的 Codex。Codex 在服务器上拥有完全权限，
# 能执行命令、读写文件、运行 Python、提交 CP2K 计算、修复平台 bug，并能查询平台
# 知识库（catmat.db 的 knowledge / agent_memory 表）作为经验依据。
# 后端通过 `codex exec`（第一轮，带系统提示）+ `codex exec resume <sid>`（续接）驱动，
# session_id 由前端透传维护，实现多轮对话。
CODEX_BIN = os.path.expanduser("~/.local/bin/codex")
CODEX_WORKDIR = BASE_DIR
CODEX_TIMEOUT = int(os.environ.get("CATMAT_CODEX_TIMEOUT", "600"))  # 秒
CODEX_UPLOAD_DIR = os.path.join(CODEX_WORKDIR, "codex_uploads")
CODEX_OUTPUT_DIR = os.path.join(CODEX_WORKDIR, "analysis_outputs")


def _codex_download_path(requested):
    """Resolve a Codex file inside the two public file areas only."""
    requested = (requested or "").strip()
    if not requested or requested.startswith("/") or "\\" in requested:
        return None
    parts = requested.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    if parts[0] == "analysis_outputs":
        root, relative = CODEX_OUTPUT_DIR, parts[1:]
    elif parts[0] == "codex_uploads":
        root, relative = CODEX_UPLOAD_DIR, parts[1:]
    elif len(parts) == 1:  # compatibility with existing upload links
        root, relative = CODEX_UPLOAD_DIR, parts
    else:
        return None
    if not relative:
        return None
    full = os.path.realpath(os.path.join(root, *relative))
    if os.path.commonpath((os.path.realpath(root), full)) != os.path.realpath(root):
        return None
    return full

CODEX_SYSTEM_PROMPT = (
    "你是催化智构 CataMind 材料计算平台的智能助手，直接运行在本机 Codex 上，"
    "拥有 danger-full-access 权限：可以在服务器上执行命令、读写文件、运行 Python、"
    "提交 CP2K 计算、修复平台 bug。\n"
    f"工作目录：{BASE_DIR}（平台根目录）。\n"
    "平台知识库：SQLite 数据库 catmat.db 的 knowledge 表与 agent_memory 表，"
    "可用 python3 + sqlite3 查询（字段含 topic/keywords/content/category 等）。"
    "回答涉及平台数据/参数/经验的问题前，先查知识库取真实依据，严禁编造。\n"
    "平台的功能与经验说明见 docs/ 目录、README.md、ARCHITECTURE.md。\n"
    "用户上传的文件在 codex_uploads/ 目录，可直接读取分析。\n"
    "工作规则：\n"
    "1. 需要真实数据/文件时先读取再回答，严禁编造数据；报告数字带口径（样本数/协议/是否实测）。\n"
    "2. 一次可连续执行多个动作完成任务（如先查库 → 再跑脚本 → 再修 bug）。\n"
    "3. 有副作用的操作（提交计算、改代码、删除文件、kill 进程）执行前，向用户简要说明将做什么及影响。\n"
    "4. 用中文回答，专业术语保留英文。\n"
    "5. 保持谨慎：不要执行不可逆的破坏性操作（如 rm 重要数据、kill 平台/数据库进程）；"
    "遇到平台 bug 可先诊断再修复，修改前保留原文件备份。"
)


def _codex_extract_sid(stdout):
    m = re.search(r"session id:\s*([0-9a-fA-F-]{20,})", stdout or "")
    return m.group(1) if m else None


_codex_iface_cache = None
_codex_iface_mtime = 0


def _codex_interface_section():
    """拼一段「平台接口与调用权限」注入 Codex 首轮提示词（来源 catmat.db）。
    带按文件 mtime 的缓存；DB 不可用时降级为空串，绝不影响 codex_chat 主流程。"""
    global _codex_iface_cache, _codex_iface_mtime
    db = os.path.join(CODEX_WORKDIR, "catmat.db")
    try:
        mtime = os.path.getmtime(db)
    except OSError:
        mtime = 0
    if _codex_iface_cache is not None and mtime == _codex_iface_mtime:
        return _codex_iface_cache
    block = [
        "\n\n## 平台接口与调用权限（自动注入 · 来源 catmat.db origin='catmat_interface_catalog'）",
        "调用本平台 REST 接口前，按以下分级自我约束；明细可在 catmat.db 的 knowledge / agent_memory 表检索。",
        "分级：AUTO=只读/查询/预览，可直接调用；CONFIRM=写库/写文件/提交训练，调用前应在微信通道发 confirm 请求（human-in-loop）；ESCALATE=销毁性/提交真实作业/启动高危 exec/改授权，必须显式人工授权（发 permission 类 channel-request，经微信由博士批准），禁止先斩后奏。",
    ]
    try:
        conn = sqlite3.connect(db, timeout=10)
        rows = conn.execute(
            "SELECT k.topic, a.content FROM knowledge k "
            "LEFT JOIN agent_memory a ON a.topic=k.topic AND a.origin=k.origin "
            "WHERE k.origin='catmat_interface_catalog' ORDER BY k.topic"
        ).fetchall()
        conn.close()
        for topic, content in rows:
            tier = ""
            if content:
                try:
                    tier = (json.loads(content).get("permission") or {}).get("tier", "")
                except Exception:
                    pass
            block.append("- %s  [%s]" % (topic, tier))
    except Exception as e:
        block.append("（接口目录加载失败：%s）" % e)
    block.append("示例 ESCALATE 接口：/api/codex（spawn danger-full-access exec）、/api/aiida/kill、/api/cp2k-submit、/api/workflow、/api/hts、/api/al、/api/dpgen、/api/agent runs start(high_priv)、/api/auth、/api/import。")
    out = "\n".join(block)
    _codex_iface_cache, _codex_iface_mtime = out, mtime
    return out


def codex_chat(message, session_id):
    """调用本机 Codex CLI 对话，返回 (reply, session_id, err)。

    第一轮（无 session_id）：codex exec -s danger-full-access -C workdir，带系统提示；
    续接（有 session_id）：codex exec resume <sid> -c sandbox_mode="danger-full-access"
    （resume 不接受 -C/-s，用 -c 覆盖 sandbox_mode；工作目录取运行 cwd）。
    """
    import tempfile
    fd, out_file = tempfile.mkstemp(suffix=".txt", prefix="catmat_codex_")
    os.close(fd)
    try:
        if session_id:
            cmd = [CODEX_BIN, "exec", "resume", session_id,
                   "-c", 'sandbox_mode="danger-full-access"',
                   "--skip-git-repo-check", "-o", out_file, message]
        else:
            prompt = CODEX_SYSTEM_PROMPT + _codex_interface_section() + "\n\n---\n\n用户：" + message
            cmd = [CODEX_BIN, "exec", "-s", "danger-full-access",
                   "--skip-git-repo-check", "-C", CODEX_WORKDIR,
                   "--color", "never", "-o", out_file, prompt]
        with _blocking_unlocked():
            r = subprocess.run(cmd, cwd=CODEX_WORKDIR, capture_output=True,
                               text=True, timeout=CODEX_TIMEOUT)
        if r.returncode != 0:
            return None, session_id, "Codex 调用失败（exit %s）：%s" % (
                r.returncode, (r.stderr or r.stdout)[-600:])
        reply = ""
        if os.path.exists(out_file):
            with open(out_file, encoding="utf-8", errors="replace") as f:
                reply = f.read().strip()
        if not session_id:
            session_id = _codex_extract_sid((r.stdout or "") + "\n" + (r.stderr or "")) or session_id
        return reply, session_id, None
    except subprocess.TimeoutExpired:
        return None, session_id, ("Codex 调用超时（>%d 秒）——它可能正在执行较长任务（如提交计算）。"
                                  "请稍后追问进度，或让它分小步执行。" % CODEX_TIMEOUT)
    except Exception as e:
        return None, session_id, "Codex 调用异常: %s" % e
    finally:
        try:
            os.unlink(out_file)
        except Exception:
            pass


def agent_llm_chat(messages, cfg, conn, max_rounds=6, allow_high_priv=False):
    """平台 Agent 对话：自我认知 system + 工具调用循环，返回 (content, steps, err)。

    allow_high_priv=False（默认）：高权限工具既不进入工具列表，执行前还有二次拦截，
    即便模型被 prompt 注入诱导点名调用也会被拒绝（[高4] 防注入静默执行）。
    """
    steps = []
    name = cfg.get("ai_name") or "休go"
    nicks = cfg.get("ai_nicknames") or ["小休", "小go"]
    nick = "、".join(nicks[:3]) if isinstance(nicks, list) else str(nicks or "小休、小go")
    sys_prompt = AGENT_SYSTEM_PROMPT.format(name=name, nick=nick)
    # 注入休go持久化记忆的**紧凑索引**（细节由 search_agent_memory 按需检索，避免撑爆上下文）
    try:
        _idx = agent_memory_index(conn)
        if _idx:
            sys_prompt += "\n\n" + _idx
    except Exception:
        log_exc("注入休go记忆索引失败")
    full = [{"role": "system", "content": sys_prompt}] + list(messages)
    # [高4] 未显式开启时，高权限工具对模型不可见（不可见即不可调用）
    tools_payload = agent_tools_for(allow_high_priv)
    for _ in range(max_rounds):
        data, err = _chat_completions({"messages": full, "tools": tools_payload}, cfg)
        if err:
            return None, steps, err
        try:
            msg = data["choices"][0]["message"]
        except Exception:
            return None, steps, "大模型返回格式异常"
        if not msg.get("tool_calls"):
            return msg.get("content") or "", steps, None
        # 执行工具调用
        full.append(msg)
        for tc in msg["tool_calls"]:
            fname = (tc.get("function") or {}).get("name") or "?"
            try:
                fargs = json.loads((tc.get("function") or {}).get("arguments") or "{}")
            except Exception:
                fargs = {}
            # [高4] 执行前二次拦截：即使模型绕过工具列表点名调用高权限工具也拒绝
            if fname in HIGH_PRIV_TOOLS and not (allow_high_priv and AGENT_HIGH_PRIV_ENABLED):
                ok, res = False, ("高权限工具「%s」未启用：请在 AI 助手面板勾选「允许高权限工具」后重试；"
                                  "如未显示该选项，说明服务端未开启 CATMAT_AGENT_HIGH_PRIV。" % fname)
                LOG.warning("Agent 高权限工具被拦截（未授权）: %s args=%s", fname, str(fargs)[:300])
            else:
                ok, res = agent_execute_tool(conn, fname, fargs)
                if fname in HIGH_PRIV_TOOLS:
                    LOG.warning("Agent 高权限工具已执行: %s ok=%s args=%s",
                                fname, ok, str(fargs)[:300])
            # [高5] 审计：每一次 Agent 工具调用都留痕（含被拦截的高权限调用）
            audit(conn, "agent_tool", target=fname, params=fargs,
                  result=(res if ok else str(res)), ok=ok, actor="llm_agent")
            steps.append({"tool": fname, "args": fargs, "ok": ok,
                          "result": res if ok else str(res), "ts": now_str()})
            full.append({"role": "tool", "tool_call_id": tc.get("id") or "",
                         "content": json.dumps({"ok": ok, "result": res}, ensure_ascii=False)[:6000]})
    return "已达到最大工具调用轮次（%d），请精简指令或分步执行。" % max_rounds, steps, None


# ==================== 休go · 自然语言任务编排层（Orchestrator） ====================
# 目标：用户一句自然语言 → 意图解析 → 生成「由平台模块组成的可执行步骤序列」→
#       人工确认/改参 → 后台执行 → 全程状态监控与进度反馈。
# 设计原则：
#   1) 复用既有工具层 agent_execute_tool()，不重复实现业务动作；
#   2) 计划显式化（存库），任何有副作用的步骤执行前必须经用户确认；
#   3) 参数推荐值可追溯（来源标注 knowledge/dataset/default），用户可改；
#   4) 每步状态、耗时、结果摘要落库，前端可轮询，异常可定位到具体步骤。
AGENT_ORCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT, instruction TEXT, intent TEXT, template TEXT,
    status TEXT DEFAULT 'draft',
    plan_json TEXT, summary_json TEXT,
    progress REAL DEFAULT 0,
    error TEXT, actor TEXT,
    created_at TEXT, started_at TEXT, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS agent_run_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL, seq INTEGER NOT NULL,
    key TEXT NOT NULL, tool TEXT NOT NULL, label TEXT NOT NULL,
    params_json TEXT, schema_json TEXT, depends_on TEXT,
    enabled INTEGER DEFAULT 1, requires_confirm INTEGER DEFAULT 1,
    internal INTEGER DEFAULT 0, high_priv INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending', result_json TEXT, error TEXT,
    started_at TEXT, finished_at TEXT, duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_agent_steps_run ON agent_run_steps(run_id, seq);
"""

# 流水线模板库：每条模板描述"一个典型任务流程"由哪些平台模块步骤组成。
#   tool 以 __ 开头者为编排层内部步骤（不对应大模型工具，由执行引擎直接实现）。
#   schema 用于前端渲染参数编辑控件并做取值校验。
PIPELINE_TEMPLATES = {
    "ml_pipeline": {
        "title": "机器学习训练 → 结果解读 → 分析报告",
        "keywords": ["机器学习", "训练", "模型", "预测", "回归", "分类", "ml",
                     "xgboost", "xgb", "lightgbm", "神经网络", "gpr"],
        "steps": [
            {"key": "dataset", "tool": "list_ml_tasks", "label": "检查数据集与既有训练任务",
             "params": {}, "schema": {}, "note": "确认数据规模与是否有在跑任务，避免重复提交"},
            {"key": "train", "tool": "train_ml", "label": "启动机器学习模型训练",
             "params": {"dataset_file": "", "model": "xgb", "device": "auto",
                       "params": {"n_estimators": 400}},
             "schema": {
                 "dataset_file": {"type": "dataset", "label": "数据集文件（留空则用默认 ml_samples）",
                                  "default": "", "module": "ml",
                                  "hint": "形如 ML-xxx.csv；可在确认界面从已有数据集中选择"},
                 "model": {"type": "choice", "label": "模型", "default": "xgb",
                           "choices": ["rf", "xgb", "lgbm", "mlp_torch", "gpr_torch", "gpr",
                                       "svr", "knn", "linear"]},
                 "device": {"type": "choice", "label": "计算设备", "default": "auto",
                            "choices": ["auto", "cuda", "cpu"]},
                 "params": {"type": "object", "label": "训练参数(JSON)", "default": {"n_estimators": 400}}},
             "note": "小样本(<100)建议 gpr_torch / gpr；中大样本用 xgb / lgbm"},
            {"key": "monitor", "tool": "list_ml_tasks", "label": "监控训练任务状态",
             "params": {}, "schema": {}, "note": "轮询直到 done/failed"},
            {"key": "interpret", "tool": "__interpret", "label": "结果解读与后处理分析",
             "params": {}, "schema": {}, "internal": True,
             "note": "基于训练结果 + 数据集统计给出可读解读"},
            {"key": "report", "tool": "__report", "label": "生成分析报告",
             "params": {"format": "markdown"},
             "schema": {"format": {"type": "choice", "label": "报告格式", "default": "markdown",
                                   "choices": ["markdown", "html"]}},
             "internal": True, "note": "汇总指令、各步结果生成报告文件"},
        ],
    },
    "al_pipeline": {
        "title": "主动学习推荐 → 候选结构 → 解读",
        "keywords": ["主动学习", "贝叶斯", "优化", "推荐", "候选", "active learning", "al",
                     "botorch", "optuna", "skopt"],
        "steps": [
            {"key": "al_start", "tool": "start_al", "label": "启动主动学习推荐任务",
             "params": {"method": "botorch_qei", "batch_size": 5, "device": "auto"},
             "schema": {
                 "method": {"type": "choice", "label": "推荐方法", "default": "botorch_qei",
                            "choices": ["gpr_ucb", "gpr_ei", "gpr_pi", "botorch_qei",
                                        "botorch_qucb", "skopt_gp", "optuna_tpe"]},
                 "batch_size": {"type": "int", "label": "每批推荐数", "default": 5, "min": 1, "max": 50},
                 "device": {"type": "choice", "label": "计算设备", "default": "auto",
                            "choices": ["auto", "cuda", "cpu"]}}},
            {"key": "al_monitor", "tool": "list_al_tasks", "label": "监控主动学习任务", "params": {}},
            {"key": "interpret", "tool": "__interpret", "label": "候选结果解读",
             "params": {}, "internal": True},
            {"key": "report", "tool": "__report", "label": "生成分析报告",
             "params": {"format": "markdown"}, "internal": True},
        ],
    },
    "structure_calc_pipeline": {
        "title": "结构建模 → CP2K 输入 → 提交计算",
        "keywords": ["建模", "结构", "表面", "吸附", "生成结构", "cp2k", "计算", "优化", "单点",
                     "slab", "adsorb", "提交"],
        "steps": [
            {"key": "build", "tool": "generate_structure", "label": "生成结构并入库",
             "params": {"symbols": "Pt", "kind": "slab", "hkl": "111", "layers": 4, "vacuum": 15},
             "schema": {
                 "symbols": {"type": "text", "label": "化学式", "default": "Pt"},
                 "kind": {"type": "choice", "label": "类型", "default": "slab",
                          "choices": ["slab", "adsorb"]},
                 "hkl": {"type": "text", "label": "表面指数", "default": "111"},
                 "layers": {"type": "int", "label": "层数", "default": 4, "min": 1, "max": 20},
                 "vacuum": {"type": "number", "label": "真空层 Å", "default": 15, "min": 5, "max": 40},
                 "adsorbate": {"type": "choice", "label": "吸附分子", "default": "OH",
                               "choices": ["OH", "O", "H2O", "H"]},
                 "site": {"type": "choice", "label": "吸附位点", "default": "top",
                          "choices": ["top", "bridge", "hollow"]}}},
            {"key": "inp", "tool": "recommend_inp", "label": "生成 CP2K 输入文件",
             "params": {"calc_type": "geo_opt", "dispersion": "d3bj", "smearing": True},
             "schema": {
                 "calc_type": {"type": "choice", "label": "计算类型", "default": "geo_opt",
                               "choices": ["energy", "geo_opt", "md", "ot", "pdos"]},
                 "dispersion": {"type": "choice", "label": "色散校正", "default": "d3bj",
                                "choices": ["d3bj", "none"]},
                 "smearing": {"type": "bool", "label": "Smearing", "default": True}}},
            {"key": "submit", "tool": "submit_calc", "label": "提交计算（高权限，消耗算力）",
             "params": {}, "schema": {}, "high_priv": True,
             "note": "需前端勾选「允许高权限工具」且服务端开启 CATMAT_AGENT_HIGH_PRIV"},
            {"key": "monitor", "tool": "list_calc_tasks", "label": "监控计算任务", "params": {}},
        ],
    },
    "bader_pipeline": {
        "title": "差分电荷密度分析（非 Bader 分区）",
        "keywords": ["差分电荷", "差分密度", "差分", "cube"],
        "steps": [
            {"key": "bader", "tool": "run_bader_diff", "label": "计算差分电荷密度",
             "params": {}, "schema": {}},
            {"key": "report", "tool": "__report", "label": "生成分析报告",
             "params": {"format": "markdown"}, "internal": True},
        ],
    },
}


def _agent_pick_template(instruction):
    """按关键词命中流水线模板（规则层解析，LLM 不可用时同样可用）。"""
    text = (instruction or "").lower()
    best, best_score = None, 0
    for key, tpl in PIPELINE_TEMPLATES.items():
        score = sum(1 for kw in tpl["keywords"] if kw.lower() in text)
        if score > best_score:
            best, best_score = key, score
    return best, best_score


def _agent_dataset_hint(instruction):
    """从指令中提取"数据集"线索（如"用 ML-xxx 数据集"），供参数映射与展示。"""
    m = re.search(r"(ML|AL)[-_]?([A-Za-z0-9_\-.]+)", instruction or "")
    if m:
        return (m.group(1) + "-" + m.group(2)).strip()
    m = re.search(r"([^\s，,。]+?)\s*数据集", instruction or "")
    return m.group(1).strip() if m else ""


def agent_build_plan(instruction):
    """把自然语言指令编排为显式步骤计划（草稿）。

    返回 plan = {template, title, intent, dataset, steps:[{key,tool,label,params,schema,
    depends_on,enabled,internal,high_priv,note}]}
    """
    tpl_key, score = _agent_pick_template(instruction)
    if not tpl_key:
        return None, "未能识别任务意图：请说明要做什么（如「用 xx 数据集训练模型并出报告」、" \
                     "「生成 Pt(111) 表面并优化」、「做主动学习推荐」、「差分电荷密度」）"
    tpl = PIPELINE_TEMPLATES[tpl_key]
    dataset = _agent_dataset_hint(instruction)
    steps = []
    tpl_steps = tpl["steps"]
    for i, s in enumerate(tpl_steps):
        steps.append({
            "key": s["key"], "tool": s["tool"], "label": s["label"],
            "params": json.loads(json.dumps(s.get("params") or {})),
            "schema": s.get("schema") or {},
            "depends_on": [tpl_steps[i - 1]["key"]] if i else [],
            "enabled": True, "internal": bool(s.get("internal")),
            "high_priv": bool(s.get("high_priv")), "note": s.get("note") or "",
        })
    # 数据集线索 → 注入训练步骤参数（供用户在确认界面核对与修改）
    if dataset and tpl_key == "ml_pipeline":
        for s in steps:
            if s["key"] == "train":
                s["params"]["dataset_file"] = dataset
                s["params"]["dataset_module"] = "ml"
    plan = {"template": tpl_key, "title": tpl["title"], "intent": tpl_key,
            "dataset": dataset, "match_score": score, "steps": steps}
    return plan, None


def _agent_step_summary(tool, result):
    """把工具返回压成一句可读摘要（避免前端展示超长 JSON）。"""
    try:
        if isinstance(result, dict):
            for k in ("message", "summary", "note", "status", "result"):
                v = result.get(k)
                if isinstance(v, str) and 0 < len(v) <= 300:
                    return v
            if "tasks" in result and isinstance(result["tasks"], list):
                return "共 %d 个任务" % len(result["tasks"])
            if "systems" in result and isinstance(result["systems"], list):
                return "共 %d 个体系" % len(result["systems"])
            if "id" in result:
                return "已创建/定位对象 id=%s" % result["id"]
            return json.dumps(result, ensure_ascii=False)[:300]
        return str(result)[:300]
    except Exception:
        return str(result)[:200]


def _agent_samples_rows(conn, limit=300):
    """取 ml_samples 样本行（与 Handler._ml_samples_rows 同格式，供模块级复用）。"""
    rows = []
    for r in conn.execute(
            "SELECT name,features,target FROM ml_samples ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit or 300), 2000)),)).fetchall():
        try:
            feats = json.loads(r["features"] or "{}")
        except Exception:
            feats = {}
        rows.append({"name": r["name"], "features": feats, "target": r["target"]})
    return rows


def _agent_run_internal(tool, params, ctx):
    """执行编排层内部步骤（__ 开头）：结果解读 / 生成报告。"""
    if tool == "__interpret":
        if ml_agent is None:
            return False, "结果解读模块不可用"
        conn = ctx["conn"]
        # 等待本流程刚启动的训练任务收敛（最多 ~90s，避免"训练未完成 → 解读无对象"）
        deadline = time.time() + 90
        row = None
        while time.time() < deadline:
            row = conn.execute(
                "SELECT id,result,task_type,status FROM ml_tasks WHERE status IN ('done','finished')"
                " ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                break
            running = conn.execute(
                "SELECT COUNT(*) c FROM ml_tasks WHERE task_type='ml' AND status IN "
                "('running','queued','pending')").fetchone()["c"]
            if not running:
                break
            time.sleep(3)
        if not row:
            # 区分"还在跑"与"已失败"，避免用户只看到一句含糊的失败
            bad = conn.execute(
                "SELECT id,status,error FROM ml_tasks WHERE task_type='ml' AND status IN "
                "('failed','error','cancelled') ORDER BY id DESC LIMIT 1").fetchone()
            if bad:
                return False, "最新的训练任务 #%s 状态为 %s：%s（请先在机器学习模块排查，再单独执行解读）" % (
                    bad["id"], bad["status"], (bad["error"] or "无详细信息")[:120])
            return False, "暂无已完成的训练任务可解读（可待训练完成后单独执行解读步骤）"
        try:
            result = json.loads(row["result"] or "{}") or {}
        except Exception:
            result = {}
        result.setdefault("task_id", row["id"])
        result.setdefault("task_type", row["task_type"])
        summary = ml_agent.summarize_dataset(
            _agent_samples_rows(conn, 300), target_key="target")
        out = ml_agent.interpret_result(result, summary, ctx={"task": "ml"})
        return True, {"interpretation": out, "task_id": row["id"]}
    if tool == "__report":
        lines = ["# 休go 任务执行报告", "",
                 "- 指令：%s" % (ctx.get("instruction") or ""),
                 "- 流程：%s" % (ctx.get("title") or ""),
                 "- 生成时间：%s" % now_str(), ""]
        for st in ctx.get("done_steps") or []:
            lines.append("## 步骤：%s（%s）" % (st["label"], st["tool"]))
            lines.append("- 状态：%s" % st.get("status"))
            if st.get("summary"):
                lines.append("- 结果摘要：%s" % st["summary"])
            if st.get("error"):
                lines.append("- 错误：%s" % st["error"])
            lines.append("")
        outdir = os.path.join(DATA_DIR, "reports")
        try:
            os.makedirs(outdir, exist_ok=True)
            path = os.path.join(outdir, "agent_report_%s.md" % ctx["run_id"])
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            return True, {"report_path": path, "chars": sum(len(x) for x in lines)}
        except Exception as e:
            return False, "写报告失败: %s" % e
    return False, "未知的内部步骤: %s" % tool


def _agent_execute_run(run_id, actor, allow_high_priv):
    """后台线程：按序执行已确认的步骤，逐步落库状态/耗时/摘要。

    注意：这里**不使用** _lock（全局请求锁），否则长任务会阻塞整个 HTTP 服务；
    get_db() 每次返回独立连接，后台线程写入安全。
    """
    conn = None
    try:
        conn = get_db()
        run = conn.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            return
        steps = conn.execute(
            "SELECT * FROM agent_run_steps WHERE run_id=? AND enabled=1 ORDER BY seq",
            (run_id,)).fetchall()
        conn.execute("UPDATE agent_runs SET status='running', started_at=?, error=NULL WHERE id=?",
                     (now_str(), run_id))
        conn.commit()
        total = len(steps) or 1
        done_steps = []
        aborted = False
        for idx, st in enumerate(steps):
            if aborted:
                conn.execute("UPDATE agent_run_steps SET status='skipped' WHERE id=?", (st["id"],))
                conn.commit()
                continue
            t0 = time.time()
            conn.execute("UPDATE agent_run_steps SET status='running', started_at=? WHERE id=?",
                         (now_str(), st["id"]))
            conn.commit()
            params = json.loads(st["params_json"] or "{}")
            tool = st["tool"]
            ok, res = False, ""
            try:
                if st["internal"]:
                    ok, res = _agent_run_internal(
                        tool, params, {"conn": conn, "run_id": run_id,
                                       "instruction": run["instruction"],
                                       "title": run["title"], "done_steps": done_steps})
                else:
                    if st["high_priv"] and not (allow_high_priv and AGENT_HIGH_PRIV_ENABLED):
                        ok, res = False, ("高权限步骤「%s」未授权：请在对话面板勾选「允许高权限工具」"
                                          "后再执行（服务端需开启 CATMAT_AGENT_HIGH_PRIV）" % st["label"])
                    else:
                        ok, res = agent_execute_tool(conn, tool, params)
            except Exception as e:
                ok, res = False, "步骤执行异常: %s" % e
                log_exc("agent run step failed")
            dur = int((time.time() - t0) * 1000)
            summary = _agent_step_summary(tool, res) if ok else str(res)[:300]
            conn.execute(
                "UPDATE agent_run_steps SET status=?, result_json=?, error=?, finished_at=?,"
                " duration_ms=? WHERE id=?",
                ("success" if ok else "failed",
                 json.dumps(res, ensure_ascii=False)[:8000] if ok else None,
                 None if ok else str(res)[:2000], now_str(), dur, st["id"]))
            conn.execute("UPDATE agent_runs SET progress=? WHERE id=?",
                         (round((idx + 1) * 100.0 / total, 1), run_id))
            audit(conn, "agent_run_step", target=tool,
                  params={"run_id": run_id, "step": st["key"], "params": params},
                  result=summary, ok=ok, actor=actor or "agent_run")
            conn.commit()
            done_steps.append({"label": st["label"], "tool": tool,
                               "status": "success" if ok else "failed",
                               "summary": summary, "error": None if ok else str(res)[:300]})
            # 关键步骤失败即中止后续，避免带病继续（可观测步骤失败不阻断）
            if not ok and st["tool"] in ("train_ml", "start_al", "submit_calc",
                                         "generate_structure", "run_bader_diff"):
                aborted = True
        failed = conn.execute(
            "SELECT COUNT(*) c FROM agent_run_steps WHERE run_id=? AND status='failed'",
            (run_id,)).fetchone()["c"]
        conn.execute(
            "UPDATE agent_runs SET status=?, finished_at=?, progress=?, summary_json=? WHERE id=?",
            ("failed" if (failed and aborted) else ("partial" if failed else "succeeded"),
             now_str(), 100.0, json.dumps(done_steps, ensure_ascii=False), run_id))
        conn.commit()
    except Exception as e:
        log_exc("agent run worker failed")
        try:
            if conn is not None:
                conn.execute("UPDATE agent_runs SET status='failed', error=?, finished_at=? WHERE id=?",
                             (str(e)[:500], now_str(), run_id))
                conn.commit()
        except Exception:
            pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def extract_inp(text):
    """从大模型输出中提取 INP 内容（去掉可能的 markdown 代码块包裹）。"""
    m = re.search(r"```(?:inp|cp2k|text|ini)?\s*\n?(.*?)```", text, re.S)
    if m:
        return m.group(1).strip()
    return (text or "").strip()


def rag_search(query, conn, top=8):
    """检索本地知识库 + 文献库 + 体系，返回结构化上下文。"""
    like = "%" + query + "%"
    out = {"knowledge": [], "literature": [], "systems": []}
    try:
        rows = conn.execute(
            "SELECT category, topic, keyword, recommended, rationale FROM knowledge "
            "WHERE topic LIKE ? OR keyword LIKE ? OR rationale LIKE ? OR recommended LIKE ? "
            "ORDER BY (topic LIKE '【统一定义标准】%' OR origin LIKE '%定义标准%') DESC, id "
            "LIMIT ?",
            (like, like, like, like, top)).fetchall()
        out["knowledge"] = [dict(r) for r in rows]
    except Exception:
        pass
    try:
        rows = conn.execute(
            "SELECT title, authors, journal, year, doi, material_system, conclusion FROM literature "
            "WHERE title LIKE ? OR conclusion LIKE ? OR material_system LIKE ? LIMIT ?",
            (like, like, like, top)).fetchall()
        out["literature"] = [dict(r) for r in rows]
    except Exception:
        pass
    try:
        rows = conn.execute(
            "SELECT name, formula, space_group, descriptors FROM systems "
            "WHERE name LIKE ? OR formula LIKE ? LIMIT ?", (like, like, top)).fetchall()
        out["systems"] = [dict(r) for r in rows]
    except Exception:
        pass
    return out


def build_rag_context(rag):
    """把 RAG 检索结果拼成给 LLM 的上下文文本。"""
    parts = []
    if rag.get("knowledge"):
        parts.append("【本地 CP2K 知识库】")
        for k in rag["knowledge"][:6]:
            parts.append("- [%s] %s：推荐 %s；%s" % (
                k.get("category") or "", k.get("topic") or "",
                k.get("recommended") or "—", (k.get("rationale") or "")[:200]))
    if rag.get("literature"):
        parts.append("【本地文献库】")
        for l in rag["literature"][:5]:
            parts.append("- %s（%s %s）：%s" % (
                l.get("title") or "", l.get("journal") or "", l.get("year") or "",
                (l.get("conclusion") or "")[:180]))
    if rag.get("systems"):
        parts.append("【本地研究体系】")
        for s in rag["systems"][:5]:
            parts.append("- %s（%s）：%s" % (
                s.get("name") or "", s.get("formula") or "", (s.get("descriptors") or "")[:160]))
    return "\n".join(parts) if parts else ""


# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "CatMatLab/" + VERSION
    # 公网/弱网加固：http.server 默认无超时，一个「只建连、不发数据」的客户端即可永久占用
    # 一个工作线程（无任何上限），累积后表现为后端无响应/离线。此处限制单连接空闲等待。
    timeout = 120

    def _cors(self):
        """平台采用同源访问；不向任意第三方站点开放跨域业务 API。"""
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,PATCH,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send(self, code, obj=None, raw=None, ctype="application/json", headers=None):
        self._resp_status = code
        self._resp_code = (obj.get("code") if isinstance(obj, dict) and obj.get("code")
                           else None)
        if isinstance(obj, dict) and "ok" in obj:
            self._resp_ok = obj["ok"]
        if isinstance(obj, dict) and obj.get("error") is not None:
            self._resp_error = obj["error"]
        self.send_response(code)
        self._cors()
        if raw is not None:
            self.send_header("Content-Type", ctype)
            body = raw
        else:
            self.send_header("Content-Type", "application/json; charset=utf-8")
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg, errcode=None):
        """统一错误响应。

        [低2] 除 error 文本外附带结构化 code 字段（如 INPUT_ERROR / DEPENDENCY /
        UPSTREAM）。未显式指定时按 HTTP 状态取默认码，因此既有调用点无需改动。
        """
        self._send(code, {"ok": False, "error": msg,
                          "code": errcode or HTTP_DEFAULT_CODE.get(code, ERR_INTERNAL)})

    def _governor_err(self, err):
        """把入队治理拒绝映射为统一 409 与结构化预算。"""
        decision = getattr(err, "decision", None)
        if not decision:
            return self._err(500, str(err))
        return self._send(409, {
            "ok": False,
            "error": str(err),
            "code": decision["code"],
            "budget": decision.get("budget") or {},
        })

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if urlparse(self.path).path == "/api/postprocess/tools/upload" and (n < 0 or n > 45 * 1024 * 1024):
            self.close_connection = True
            raise ApiError(413, "每批上传总量不得超过 32 MiB；大文件请从服务器选择")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _ok(self, data=None, **kw):
        payload = {"ok": True}
        if data is not None:
            payload["data"] = data
        payload.update(kw)
        self._send(200, payload)

    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    # ---------------- 路由 ----------------
    def do_OPTIONS(self):
        self._send(204, raw=b"")

    def do_GET(self):
        try:
            self._route("GET")
        except Exception as e:
            log_exc("GET %s" % self.path, e)
            self._err(500, "服务器内部错误: %s" % e)

    def do_POST(self):
        try:
            self._route("POST")
        except Exception as e:
            log_exc("POST %s" % self.path, e)
            self._err(500, "服务器内部错误: %s" % e)

    def do_PUT(self):
        try:
            self._route("PUT")
        except Exception as e:
            log_exc("PUT %s" % self.path, e)
            self._err(500, "服务器内部错误: %s" % e)

    def do_PATCH(self):
        try:
            self._route("PATCH")
        except Exception as e:
            log_exc("PATCH %s" % self.path, e)
            self._err(500, "服务器内部错误: %s" % e)

    def do_DELETE(self):
        try:
            self._route("DELETE")
        except Exception as e:
            log_exc("DELETE %s" % self.path, e)
            self._err(500, "服务器内部错误: %s" % e)

    def _route(self, method):
        self._resp_status = None
        self._resp_code = None
        self._resp_ok = None
        self._resp_error = None
        self._pending_audit = None
        self.auth_claims = None
        self.auth_user = None
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # 静态文件
        if method == "GET" and (path == "/" or path == "/index.html"):
            return self._serve_index()
        if method == "GET" and path.startswith("/static/"):
            return self._serve_static(path)
        # 图标别名：浏览器除读取 <link rel="icon"> 外，还会默认探测根路径 /favicon.ico
        # （书签栏、历史记录、部分旧缓存页面）。前端实际资源在 /static/ 下，
        # 这里做一层转发，避免这些探测持续产生 404 噪音。
        if method == "GET" and path in ("/favicon.ico", "/favicon.png", "/apple-touch-icon.png"):
            return self._serve_static("/static" + path)
        # CP2K View 静态托管
        if method == "GET" and (path == "/cp2kview" or path.startswith("/cp2kview/")):
            return self._serve_cp2kview(path)

        if not path.startswith("/api/"):
            return self._err(404, "未找到资源")

        route = path[len("/api/"):].strip("/")
        seg = route.split("/")

        _lock_acquire()
        try:
            conn = get_db()
            try:
                # 鉴权启用时，除健康检查与 auth 公共端点外，所有业务 API 均需有效 access token。
                root = seg[0] if seg else ""
                public_api = root == "auth" or (root == "health" and method == "GET")
                if AUTH_ENABLED and not public_api:
                    claims, auth_error = self._authenticate_access_token()
                    if auth_error:
                        return self._err(401, "登录状态无效，请重新登录" if auth_error == "UNAUTHORIZED"
                                         else "登录已过期，请刷新会话", auth_error)
                    # 权限范围门禁：确认器令牌（scope=approve）不能访问任何业务 API。
                    if not _scope_allows(claims, SCOPE_PLATFORM):
                        return self._err(403, "当前凭证仅用于登录确认，不能访问平台业务",
                                         "SCOPE_DENIED")
                    # 每次业务请求读取数据库中的实时账号状态，停用/角色调整立即生效，
                    # 不等待旧 access token 到期。
                    current_user = conn.execute("SELECT * FROM auth_users WHERE id=?",
                                                (claims.get("uid"),)).fetchone()
                    if not current_user or current_user["disabled"]:
                        return self._err(403, "该平台账号已停用或不存在", "ACCOUNT_DISABLED")
                    sid = claims.get("sid")
                    if not sid:
                        return self._err(401, "旧版登录会话已失效，请使用账号密码重新登录", "SESSION_REVOKED")
                    active_session = conn.execute(
                        "SELECT 1 FROM auth_sessions WHERE id=? AND user_id=? AND revoked=0 AND expires_at>?",
                        (sid, current_user["id"], int(time.time()))).fetchone()
                    if not active_session:
                        return self._err(401, "登录会话已被撤销，请重新登录", "SESSION_REVOKED")
                    eff_role = _effective_role(current_user)
                    allowed, reason = _business_allowed(
                        eff_role, method, seg,
                        capabilities=_capabilities_of(current_user))
                    if not allowed:
                        audit(conn, "permission_denied", target="/api/" + "/".join(seg),
                              params={"method": method, "role": current_user["role"]},
                              result=reason, ok=False, actor="uid:%s" % current_user["id"])
                        return self._err(403, reason, "PERMISSION_DENIED")
                    claims["roles"] = [eff_role]
                    claims["role_label"] = _role_label(eff_role)
                    claims["display_name"] = current_user["display_name"]
                    self.auth_claims = claims
                    self.auth_user = current_user
                else:
                    self.auth_claims = None
                self._dispatch(method, seg, qs, conn, self._body())
                # 通用审计与业务写入在同一事务提交；但 _dispatch() 已内联发出响应，
                # 因而客户端立即回读可能短暂看不到该行（不会丢）。严格的提交后响应
                # 顺序需要留待后续架构改造。
                self._emit_pending_audit(conn)
                conn.commit()
            except ApiError as e:
                conn.rollback()
                self._emit_pending_audit(
                    conn, status=e.code,
                    code=HTTP_DEFAULT_CODE.get(e.code, ERR_INTERNAL), msg=e.msg)
                conn.commit()
                self._err(e.code, e.msg)
            except BaseException:
                conn.rollback()
                self._emit_pending_audit(
                    conn, status=500, code="INTERNAL_ERROR")
                conn.commit()
                raise
            finally:
                conn.close()
        finally:
            _lock_release()

    def _emit_pending_audit(self, conn, status=None, code=None, msg=None):
        """写入本次请求的通用审计；审计故障绝不改变业务控制流。"""
        try:
            pending = self._pending_audit
            if not pending:
                return False
            self._pending_audit = None
            status = status if status is not None else self._resp_status
            if status is None:
                status = 500
                code = code or "INTERNAL_ERROR"
            ok = status < 400
            elapsed = max(0.0, time.monotonic() - pending["started"])
            if ok:
                result = "http=%s elapsed=%.3f" % (status, elapsed)
            else:
                response_code = code or self._resp_code or "-"
                result = "http=%s code=%s elapsed=%.3f" % (
                    status, response_code, elapsed)
                error_text = msg if msg is not None else self._resp_error
                if error_text is not None:
                    error_text = " ".join(str(error_text).split())[:120]
                    if error_text:
                        result += " msg=" + error_text
            user = self.auth_user
            actor = "uid:%s" % user["id"] if user is not None else "system"
            return audit(
                conn, pending["action"], target=pending["target"],
                params=pending["params"], result=result, ok=ok,
                actor=actor, commit=False)
        except BaseException:
            log_exc("写入请求通用审计失败")
            return False

    def _authenticate_access_token(self):
        header = (self.headers.get("Authorization") or "").strip()
        if not header.startswith("Bearer "):
            return None, "UNAUTHORIZED"
        return jwt_verify(header[7:].strip())

    # ---------------- 静态 ----------------
    def _serve_file_cached(self, full, ctype):
        """带 ETag / Cache-Control 的静态文件下发；命中协商缓存返回 304，避免每次刷新重复下载。"""
        try:
            st = os.stat(full)
        except OSError:
            return self._err(404, "文件不存在")
        etag = '"%s"' % hashlib.md5(("%s|%d|%d" % (full, int(st.st_mtime), st.st_size)).encode("utf-8")).hexdigest()
        inm = self.headers.get("If-None-Match", "")
        if inm and inm.strip('"') == etag.strip('"'):
            return self._send(304, raw=b"", ctype=ctype,
                              headers={"ETag": etag, "Cache-Control": "public, max-age=300"})
        with open(full, "rb") as f:
            data = f.read()
        return self._send(200, raw=data, ctype=ctype,
                          headers={"ETag": etag, "Cache-Control": "public, max-age=300"})

    def _serve_index(self):
        if not os.path.exists(INDEX_PATH):
            return self._err(404, "index.html 不存在")
        return self._serve_file_cached(INDEX_PATH, "text/html; charset=utf-8")

    def _serve_static(self, path):
        static_root = os.path.realpath(os.path.join(BASE_DIR, "static"))
        full = os.path.realpath(os.path.join(BASE_DIR, path.lstrip("/")))
        if os.path.commonpath([static_root, full]) != static_root or not os.path.isfile(full):
            return self._err(404, "静态资源不存在")
        ext = os.path.splitext(full)[1].lower()
        mime = {".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml",
                ".json": "application/json", ".png": "image/png",
                ".ico": "image/x-icon"}.get(ext, "application/octet-stream")
        return self._serve_file_cached(full, mime)

    def _serve_cp2kview(self, path):
        """托管 CP2K View 静态站点（index.html + src/ + styles.css + samples/）。"""
        if not os.path.isdir(CP2KVIEW_DIR):
            return self._err(404, "CP2K View 目录不存在: " + CP2KVIEW_DIR)
        rel = path[len("/cp2kview"):].lstrip("/")
        if not rel or rel == "index.html":
            rel = "index.html"
        full = os.path.normpath(os.path.join(CP2KVIEW_DIR, rel))
        # 防目录穿越
        if os.path.commonpath([os.path.realpath(CP2KVIEW_DIR), os.path.realpath(full)]) != os.path.realpath(CP2KVIEW_DIR) or not os.path.isfile(full):
            return self._err(404, "CP2K View 资源不存在: " + rel)
        ext = os.path.splitext(full)[1].lower()
        mime = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
                ".css": "text/css", ".svg": "image/svg+xml", ".json": "application/json",
                ".png": "image/png", ".xyz": "chemical/x-xyz", ".inp": "text/plain",
                ".out": "text/plain", ".log": "text/plain", ".cif": "text/plain",
                ".pdb": "text/plain", ".zip": "application/zip"}.get(ext, "application/octet-stream")
        return self._serve_file_cached(full, mime)

    # ---------------- 分发 ----------------
    def _dispatch(self, method, seg, qs, conn, body):
        root = seg[0] if seg else ""

        # [高5] 审计：统一登记所有写操作，待业务结束后按真实响应结果写入。
        if method in ("POST", "PUT", "PATCH", "DELETE") and root in AUDIT_ROOTS:
            self._pending_audit = {
                "action": "%s_%s" % (method.lower(), root),
                "target": "/" + "/".join(str(s) for s in seg),
                "params": _summarize_body(body),
                "started": time.monotonic(),
            }

        # 健康 / 统计
        if root == "health":
            return self._ok({"status": "up", "version": VERSION, "time": now_str(),
                             "db": os.path.basename(DB_PATH),
                             "capabilities": {"pdos_review": 1, "preserve_periodic_geometry": 1,
                                              "desktop_client": "1.0.1", "dpgen_production": False,
                                              "static_path_guard": 1, "postprocess_toolkit": 1, "postprocess_file_picker": 1}})
        if root == "metrics":
            m = host_metrics()
            # 任务队列统计
            def _cnt(st):
                return conn.execute("SELECT COUNT(*) c FROM tasks WHERE status=?", (st,)).fetchone()["c"]
            m["queue"] = {"running": _cnt("running"), "queued": _cnt("queued"),
                          "done": _cnt("done"), "failed": _cnt("failed"),
                          "paused": _cnt("paused"), "cancelled": _cnt("cancelled")}
            m["audit"] = {"fail_count": _audit_fail_count()}
            return self._ok(m)
        if root == "stats":
            return self._ok({
                "systems": conn.execute("SELECT COUNT(*) c FROM systems").fetchone()["c"],
                "literature": conn.execute("SELECT COUNT(*) c FROM literature").fetchone()["c"],
                "knowledge": conn.execute("SELECT COUNT(*) c FROM knowledge").fetchone()["c"],
                "tasks": conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"],
                "ml_samples": conn.execute("SELECT COUNT(*) c FROM ml_samples").fetchone()["c"],
            })
        if root == "processes":
            data, err = process_snapshot()
            if err:
                return self._err(500, err)
            return self._ok(data)

        # ---------------- 数据集文件（按模块独立保存） ----------------
        if root == "datasets":
            return self._datasets(method, seg, qs, conn, body)

        # ---------------- 工作流 ----------------
        if root == "workflow":
            return self._workflow(method, seg, qs, conn, body)

        # ---------------- 高通量筛选（批量结构生成 / 批量工作流） ----------------
        if root == "hts":
            return self._hts(method, seg, qs, conn, body)

        # ---------------- 势函数延拓（DPGEN 主动学习闭环） ----------------
        if root == "dpgen":
            return self._dpgen(method, seg, qs, conn, body)

        # ---------------- 版本号 ----------------
        if root == "versions":
            return self._ok(get_versions())

        # ---------------- 资料库同步 ----------------
        if root == "sync":
            return self._sync(method, seg, qs, conn, body)

        # ---------------- 体系 ----------------
        if root == "systems":
            return self._systems(method, seg, qs, conn, body)

        # ---------------- 文献 ----------------
        if root == "literature":
            return self._literature(method, seg, qs, conn, body)

        # ---------------- 知识库 ----------------
        if root == "kb":
            return self._kb(method, seg, qs, conn, body)

        # ---------------- 任务 ----------------
        if root == "tasks":
            return self._tasks(method, seg, qs, conn, body)

        # ---------------- AiiDA 真实进程 ----------------
        if root == "aiida":
            return self._aiida(method, seg, qs, conn, body)

        # ---------------- ASE 结构生成 ----------------
        if root == "build":
            return self._build(method, seg, qs, conn, body)

        # ---------------- PDOS 后处理 ----------------
        if root == "postprocess":
            return self._postprocess(method, seg, qs, conn, body)

        # ---------------- ML ----------------
        if root == "ml":
            return self._ml(method, seg, qs, conn, body)

        # ---------------- 主动学习 ----------------
        if root == "al":
            return self._al(method, seg, qs, conn, body)

        # ---------------- 图表下载 ----------------
        if root == "chart":
            return self._chart(method, seg, qs, conn, body)

        # ---------------- Multiwfn CP2K 输入生成 ----------------
        if root == "multiwfn":
            return self._multiwfn(method, seg, qs, conn, body)

        # ---------------- CP2K vim 工具 ----------------
        if root == "cp2k":
            return self._cp2k(method, seg, qs, conn, body)

        # ---------------- CP2K 视图（指定路径/PK 打开） ----------------
        if root == "cp2kview":
            return self._cp2kview(method, seg, qs, conn, body)

        # ---------------- 全参数设置 ----------------
        if root == "params":
            return self._params(method, seg, qs, conn, body)

        # ---------------- 独立 CP2K 任务提交（mpirun 直连，走 CM- 队列） ----------------
        if root == "cp2k-submit":
            return self._cp2k_submit(method, seg, qs, conn, body)

        # ---------------- 平台内置 CM- 队列 ----------------
        if root == "queue":
            return self._queue(method, seg, qs, conn, body)

        # ---------------- 大模型接入 ----------------
        if root == "llm":
            return self._llm(method, seg, qs, conn, body)
        if root == "agent":
            return self._agent(method, seg, qs, conn, body)
        if root == "codex":
            return self._codex(method, seg, qs, conn, body)
        if root == "manual":
            return self._manual(method, seg, qs, conn, body)
        if root == "auth":
            return self._auth(method, seg, qs, conn, body)

        # ---------------- 导入 ----------------
        if root == "import":
            return self._import(method, seg, qs, conn, body)

        return self._err(404, "未知 API: /api/" + root)

    # ---------------- 体系 ----------------
    def _systems(self, method, seg, qs, conn, body):
        # 结构解析预览：POST /api/systems/parse {content, filename}
        # 只解析不入库，供「结构文件导入」弹窗即时 3D 预览与组成校验（支持 cif/xyz/gjf/pdb/mol/vasp…）
        if method == "POST" and len(seg) == 2 and seg[1] == "parse":
            content = body.get("content") or ""
            filename = body.get("filename") or "structure.xyz"
            if not content or not content.strip():
                return self._err(400, "缺少文件内容（content）")
            st, err = parse_structure_text(content, filename)
            if err:
                return self._err(400, err, "PARSE_ERROR")
            return self._ok({"structure": st, "filename": filename,
                             "formula": st.get("formula"), "natoms": st.get("natoms")})
        # ---------- 体系结构文件管理（研究体系模块「结构文件」栏） ----------
        #   GET    /api/systems/{id}/files            → 文件清单
        #   GET    /api/systems/{id}/files?file=xx    → 文件内容（供前端解析预览）
        #   POST   /api/systems/{id}/files {name, content} → 上传保存（扩展名白名单）
        #   DELETE /api/systems/{id}/files?file=xx    → 删除
        _fm = re.fullmatch(r"(\d+)/files", "/".join(seg[1:]))
        if _fm:
            sid = int(_fm.group(1))
            row = conn.execute("SELECT id,name FROM systems WHERE id=?", (sid,)).fetchone()
            if not row:
                return self._err(404, "体系不存在")
            sdir = os.path.join(DATA_DIR, "systems", str(sid))
            fname = (qs.get("file", [""])[0] or "").strip()
            if fname:
                # 防目录穿越：仅取文件名，且必须位于该体系目录内
                safe = os.path.basename(fname)
                full = os.path.normpath(os.path.join(sdir, safe))
                if not full.startswith(os.path.normpath(sdir)) or not os.path.isfile(full):
                    return self._err(404, "文件不存在")
            if method == "GET":
                if not os.path.isdir(sdir):
                    return self._ok({"files": [], "dir": sdir})
                if fname:
                    try:
                        with open(full, "r", encoding="utf-8", errors="replace") as f:
                            content = f.read()
                    except Exception as e:
                        return self._err(500, "读取失败: %s" % e)
                    return self._ok({"name": safe, "content": content, "size": len(content)})
                files = []
                for fn in sorted(os.listdir(sdir)):
                    p = os.path.join(sdir, fn)
                    if not os.path.isfile(p):
                        continue
                    stt = os.stat(p)
                    files.append({"name": fn, "size": stt.st_size,
                                  "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(stt.st_mtime)),
                                  "ext": os.path.splitext(fn)[1].lower().lstrip(".")})
                return self._ok({"files": files, "dir": sdir})
            if method == "POST":
                name = (body.get("name") or "").strip()
                content = body.get("content") or ""
                if not name or not content:
                    return self._err(400, "缺少文件名或内容")
                ext = os.path.splitext(name)[1].lower().lstrip(".")
                if ext not in STRUCT_FILE_EXTS:
                    return self._err(400, "不支持的结构文件类型：.%s（支持 %s）"
                                     % (ext, "、".join(sorted(STRUCT_FILE_EXTS))))
                safe = os.path.basename(name)
                try:
                    os.makedirs(sdir, exist_ok=True)
                    full = os.path.normpath(os.path.join(sdir, safe))
                    if not full.startswith(os.path.normpath(sdir)):
                        return self._err(400, "文件名非法")
                    # 先用 ASE 校验可解析，避免把坏文件写进库
                    st, err = parse_structure_text(content, safe)
                    if err:
                        return self._err(400, "结构解析失败，未保存：%s" % err, "PARSE_ERROR")
                    with open(full, "w", encoding="utf-8") as f:
                        f.write(content)
                except Exception as e:
                    return self._err(500, "保存失败: %s" % e)
                audit(conn, "system_file_upload", target="systems/%s" % sid,
                      params={"file": safe, "natoms": st.get("natoms")}, result="saved",
                      actor=("uid:%s" % self.auth_user["id"]) if getattr(self, "auth_user", None) else "system")
                conn.commit()
                return self._ok({"name": safe, "natoms": st.get("natoms"), "formula": st.get("formula")})
            if method == "DELETE":
                if not fname:
                    return self._err(400, "缺少文件名（file）")
                try:
                    os.remove(full)
                except Exception as e:
                    return self._err(500, "删除失败: %s" % e)
                audit(conn, "system_file_delete", target="systems/%s" % sid,
                      params={"file": os.path.basename(full)}, result="deleted",
                      actor=("uid:%s" % self.auth_user["id"]) if getattr(self, "auth_user", None) else "system")
                conn.commit()
                return self._ok({"deleted": os.path.basename(full)})
            return self._err(405, "不支持的方法")
        # 结构文件导入：POST /api/systems/import-file  {name, filename, content}
        if method == "POST" and len(seg) == 2 and seg[1] == "import-file":
            content = body.get("content") or ""
            filename = body.get("filename") or "structure.xyz"
            if not content or not content.strip():
                return self._err(400, "缺少文件内容（content）")
            st, err = parse_structure_text(content, filename)
            if err:
                return self._err(400, err)
            name = (body.get("name") or "").strip() or os.path.splitext(filename)[0] or st["formula"]
            # 去重：按结构内容指纹
            struct_json = json.dumps({
                "symbols": st["symbols"], "cell": st["cell"],
                "scaled_positions": st["scaled_positions"], "pbc": st["pbc"],
            }, ensure_ascii=False, sort_keys=True)
            hit = conn.execute("SELECT id FROM systems WHERE structure=? LIMIT 1", (struct_json,)).fetchone()
            if hit:
                return self._ok({"id": hit["id"], "name": name, "duplicated": True,
                                 "formula": st["formula"], "natoms": st["natoms"]})
            _insert_system(conn, {
                "name": name, "formula": st["formula"], "composition": st["formula"],
                "space_group": "导入结构", "lattice": "ASE 解析",
                "elements": sorted(set(st["symbols"])), "source": "文件导入",
                "folder": body.get("folder") or "初始结构",
                "structure": struct_json,
            })
            sid = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]
            return self._ok({"id": sid, "name": name, "duplicated": False,
                             "formula": st["formula"], "natoms": st["natoms"],
                             "symbols": st["symbols"]}, created_id=sid)
        if method == "GET" and len(seg) == 1:
            # 需求：体系列表按生成时间倒序，最新生成的置顶
            rows = conn.execute(
                "SELECT * FROM systems ORDER BY datetime(created_at) DESC, id DESC").fetchall()
            return self._ok(rows_to_list(rows))
        # 返回完整结构（供生成 INP / 启动工作流直接调用，避免重复建模）
        if method == "GET" and len(seg) == 3 and seg[2] == "structure":
            r = conn.execute("SELECT * FROM systems WHERE id=?", (seg[1],)).fetchone()
            if not r:
                return self._err(404, "体系不存在")
            try:
                struct = json.loads(r["structure"] or "[]")
            except Exception:
                struct = []
            return self._ok({"id": r["id"], "uuid": r["uuid"], "name": r["name"],
                             "formula": r["formula"], "structure": struct})
        # 已有二级目录清单（供保存时选择/归属）
        if method == "GET" and len(seg) == 2 and seg[1] == "folders":
            rows = conn.execute("SELECT DISTINCT folder FROM systems "
                                "WHERE folder IS NOT NULL AND folder != '' ORDER BY folder").fetchall()
            return self._ok([r["folder"] for r in rows])
        if method == "GET" and len(seg) == 2:
            r = conn.execute("SELECT * FROM systems WHERE id=?", (seg[1],)).fetchone()
            return self._ok(rows_to_list([r])[0]) if r else self._err(404, "体系不存在")
        if method == "POST":
            if not body.get("name"):
                return self._err(400, "缺少必填字段 name")
            _insert_system(conn, body)
            sid = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]
            return self._ok({"id": sid}, created_id=sid)
        if method == "PUT" and len(seg) == 2:
            sets, vals = [], []
            for k in ("name", "formula", "composition", "space_group", "lattice", "layers", "vacuum", "folder"):
                if k in body:
                    sets.append(k + "=?"); vals.append(body[k])
            if "elements" in body:
                sets.append("elements=?"); vals.append(json.dumps(body["elements"], ensure_ascii=False))
            if "descriptors" in body:
                sets.append("descriptors=?"); vals.append(json.dumps(body["descriptors"], ensure_ascii=False))
            if not sets:
                return self._err(400, "无更新字段")
            vals.append(seg[1])
            conn.execute("UPDATE systems SET " + ",".join(sets) + " WHERE id=?", vals)
            return self._ok({"updated": True})
        if method == "DELETE" and len(seg) == 2:
            conn.execute("DELETE FROM systems WHERE id=?", (seg[1],))
            return self._ok({"deleted": True})
        # 目录管理：PATCH /api/systems/move
        #   {ids:[1,2], folder:"目标"}            → 移动指定结构
        #   {from_folder:"源", to_folder:"目标"}  → 移动整个目录（含子目录）
        if method == "PATCH" and len(seg) == 2 and seg[1] == "move":
            b = body or {}
            target = (b.get("folder") or b.get("to_folder") or "")
            target = str(target).strip().strip("/") or None
            ids = b.get("ids")
            if ids:
                if not isinstance(ids, list):
                    return self._err(400, "ids 需为数组")
                n = 0
                for sid in ids:
                    cur = conn.execute("UPDATE systems SET folder=? WHERE id=?", (target, sid))
                    n += cur.rowcount
                return self._ok({"moved": n, "folder": target})
            src = (b.get("from_folder") or "").strip().strip("/")
            if not src:
                return self._err(400, "缺少 ids 或 from_folder")
            if target and (target == src or target.startswith(src + "/")):
                return self._err(400, "不能把目录移动到自身或其子目录下")
            # 移动该目录及其所有子目录（路径前缀替换）
            rows = conn.execute(
                "SELECT id,folder FROM systems WHERE folder=? OR folder LIKE ?",
                (src, src + "/%")).fetchall()
            n = 0
            for r in rows:
                old = r["folder"] or ""
                rest = old[len(src):].lstrip("/")
                new = (target + "/" + rest) if (target and rest) else (target or rest or None)
                conn.execute("UPDATE systems SET folder=? WHERE id=?", (new, r["id"]))
                n += 1
            return self._ok({"moved": n, "from": src, "to": target})
        return self._err(405, "方法不支持")

    # ---------------- 文献 ----------------
    def _literature(self, method, seg, qs, conn, body):
        # DOI 导入：POST /api/literature/doi  {doi, interpret?}
        if method == "POST" and len(seg) == 2 and seg[1] == "doi":
            doi = (body or {}).get("doi")
            if not doi:
                return self._err(400, "缺少 doi")
            meta, err = fetch_doi(doi)
            if err:
                return self._err(502, err)
            # 拉取摘要（OpenAlex / Semantic Scholar）
            abstract, abs_src, _ = fetch_abstract(doi)
            meta["abstract"] = abstract
            meta["abstract_source"] = abs_src
            # 去重：DOI 已存在则返回已存在
            exist = conn.execute("SELECT id FROM literature WHERE doi=?", (meta["doi"],)).fetchone()
            if exist:
                return self._ok({"id": exist["id"], "duplicated": True, "meta": meta}, existed=True)
            conn.execute(
                "INSERT INTO literature(title,authors,journal,year,doi,electrolyzer,dft_code,abstract,conclusion,tags,verified) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (meta["title"], meta["authors"], meta["journal"], meta["year"], meta["doi"],
                 None, None, abstract, "由 DOI 自动导入（待解读归档）", "[]", 0))
            lid = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]

            # 可选：用大模型解读摘要，提取材料体系 / 性能 / 结论并归档
            interpretation = None
            if body.get("interpret"):
                interpretation = self._interpret_literature(lid, conn)
            return self._ok({"id": lid, "meta": meta, "duplicated": False,
                             "interpretation": interpretation}, created_id=lid)
        # 文献 DOI 核验与自动订正：POST /api/literature/verify {id} 或 {doi}
        # 用 CrossRef 官方元数据逐项核验 title/authors/journal/year，有误自动订正
        if method == "POST" and len(seg) == 2 and seg[1] == "verify":
            lid = body.get("id")
            doi = (str(body.get("doi") or "")).strip()
            if lid is None and not doi:
                return self._err(400, "缺少 id 或 doi")
            if lid is not None:
                r = conn.execute("SELECT * FROM literature WHERE id=?", (lid,)).fetchone()
                if not r:
                    return self._err(404, "文献不存在")
                doi = (r["doi"] or "").strip()
                if not doi:
                    return self._err(400, "该文献无 DOI，无法核验")
            else:
                r = conn.execute("SELECT * FROM literature WHERE doi=?", (doi,)).fetchone()
                if not r:
                    return self._err(404, "库中无该 DOI 的文献，请先通过 DOI 导入")
            meta, err = fetch_doi(doi)
            if err:
                return self._err(502, err)

            def _norm(s):
                return re.sub(r"\s+", "", str(s or "")).lower()

            changed = {}
            fmap = {"title": "标题", "authors": "作者", "journal": "期刊", "year": "年份"}
            for key in ("title", "authors", "journal", "year"):
                new = meta.get(key)
                cur = r[key]
                if new is None:
                    continue
                if key == "year":
                    try:
                        if int(cur or 0) != int(new):
                            changed[fmap[key]] = [cur, int(new)]
                    except (TypeError, ValueError):
                        pass
                else:
                    if _norm(cur) != _norm(new):
                        changed[fmap[key]] = [cur, new]
            if changed:
                sets, vals = [], []
                rev = {v: k for k, v in fmap.items()}
                for label, pair in changed.items():
                    sets.append(rev[label] + "=?"); vals.append(pair[1])
                sets.append("verified=?"); vals.append(1)
                sets.append("verified_at=?"); vals.append(now_str())
                vals.append(r["id"])
                conn.execute("UPDATE literature SET " + ",".join(sets) + " WHERE id=?", vals)
            else:
                conn.execute("UPDATE literature SET verified=1, verified_at=? WHERE id=?",
                             (now_str(), r["id"]))
            return self._ok({"id": r["id"], "doi": doi, "verified": True,
                             "changed": changed,
                             "official": {k: meta.get(k) for k in ("title", "authors", "journal", "year")}})
        # 大模型联网检索文献：POST /api/literature/web-search {query, limit, interpret}
        # 按主题/关键词联网检索学术文献（OpenAlex + CrossRef），可选大模型输出综述与精读推荐
        if method == "POST" and len(seg) == 2 and seg[1] == "web-search":
            q = (body or {}).get("query")
            if not q or not str(q).strip():
                return self._err(400, "缺少 query（主题 / 关键词）")
            q = str(q).strip()
            limit = min(int((body or {}).get("limit") or 8), 20)
            results, err = web_search(q, limit)
            if err:
                return self._err(502, err)
            summary = None
            if (body or {}).get("interpret") and results:
                cfg = load_llm_config()
                titles = "\n".join(
                    "- %s（%s，%s，DOI: %s）" % (r.get("title", ""), r.get("venue", ""),
                                                r.get("year", ""), r.get("doi", "—"))
                    for r in results)
                content, lerr = llm_call(
                    [{"role": "user", "content":
                      "以下是按检索词「%s」从学术数据库找到的文献列表。请用中文给出主题综述（3-5 句：该方向的研究脉络、代表性工作），"
                      "并指出最值得精读的 2-3 篇及理由。只能基于下列文献，不要编造列表外的文献：\n%s" % (q, titles)}],
                    cfg, temperature=0.3)
                if not lerr:
                    summary = content
            return self._ok({"query": q, "count": len(results), "results": results,
                             "summary": summary})
        # 文献解读：POST /api/literature/{id}/interpret  （用大模型解读并归档）
        if method == "POST" and len(seg) == 3 and seg[2] == "interpret":
            lid = seg[1]
            r = conn.execute("SELECT * FROM literature WHERE id=?", (lid,)).fetchone()
            if not r:
                return self._err(404, "文献不存在")
            res = self._interpret_literature(lid, conn)
            if res.get("error"):
                return self._err(502, res["error"])
            return self._ok(res)
        if method == "GET" and len(seg) == 2 and seg[1] == "facets":
            rows = conn.execute("SELECT DISTINCT journal FROM literature ORDER BY journal").fetchall()
            years = conn.execute("SELECT MIN(year) a, MAX(year) b FROM literature").fetchone()
            return self._ok({"journals": [r["journal"] for r in rows],
                             "years": [years["a"], years["b"]]})
        if method == "GET" and len(seg) == 2 and seg[1].isdigit():
            r = conn.execute("SELECT * FROM literature WHERE id=?", (seg[1],)).fetchone()
            return self._ok(rows_to_list([r])[0]) if r else self._err(404, "文献不存在")
        if method == "GET":
            where, params = [], []
            if qs.get("q"):
                like = "%" + qs["q"][0] + "%"
                where.append("(title LIKE ? OR material_system LIKE ? OR authors LIKE ? OR conclusion LIKE ?)")
                params += [like, like, like, like]
            if qs.get("journal"):
                where.append("journal=?"); params.append(qs["journal"][0])
            if qs.get("material"):
                where.append("material_system LIKE ?"); params.append("%" + qs["material"][0] + "%")
            if (qs.get("uses_cp2k") or [""])[0] == "1":
                where.append("uses_cp2k=1")
            if qs.get("electrolyzer"):
                where.append("electrolyzer=?"); params.append(qs["electrolyzer"][0])
            sql = "SELECT * FROM literature"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY year DESC, journal"
            rows = conn.execute(sql, params).fetchall()
            return self._ok(rows_to_list(rows), total=len(rows))
        if method == "POST":
            if not body.get("title"):
                return self._err(400, "缺少必填字段 title")
            _insert_literature(conn, body)
            lid = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]
            return self._ok({"id": lid}, created_id=lid)
        if method == "PUT" and len(seg) == 2:
            cols = ["title", "authors", "journal", "year", "doi", "material_system", "electrolyzer",
                    "overpotential_10", "tafel_slope", "stability_hours", "mass_activity",
                    "dft_code", "cp2k_settings", "conclusion"]
            sets, vals = [], []
            for k in cols:
                if k in body:
                    sets.append(k + "=?"); vals.append(body[k])
            if "uses_cp2k" in body:
                sets.append("uses_cp2k=?"); vals.append(1 if body["uses_cp2k"] else 0)
            if "tags" in body:
                sets.append("tags=?"); vals.append(json.dumps(body["tags"], ensure_ascii=False))
            if not sets:
                return self._err(400, "无更新字段")
            vals.append(seg[1])
            conn.execute("UPDATE literature SET " + ",".join(sets) + " WHERE id=?", vals)
            return self._ok({"updated": True})
        if method == "DELETE" and len(seg) == 2:
            conn.execute("DELETE FROM literature WHERE id=?", (seg[1],))
            return self._ok({"deleted": True})
        return self._err(405, "方法不支持")
    def _interpret_literature(self, lid, conn):
        me = getattr(self, "auth_user", None)
        label = None
        if me is not None:
            label = (me["username"] or "").strip() or (me["display_name"] or "").strip()
        if not label:
            label = "东翁系统通道"
        return interpret_literature_core(conn, lid, archived_by=label)
    # ---------------- 知识库 ----------------
    def _kb(self, method, seg, qs, conn, body):
        # 导入本地 SKILL 到知识库（供 AI 助手检索学习）：POST /api/kb/import-skills
        if method == "POST" and len(seg) == 2 and seg[1] == "import-skills":
            n = import_skills_to_kb(conn, body.get("skills_dir"))
            conn.commit()
            return self._ok({"imported": n, "total": conn.execute(
                "SELECT COUNT(*) c FROM knowledge WHERE category='平台技能SKILL'").fetchone()["c"]})
        if method == "GET" and len(seg) == 2 and seg[1] == "categories":
            rows = conn.execute("SELECT DISTINCT category FROM knowledge ORDER BY category").fetchall()
            return self._ok([r["category"] for r in rows])
        if method == "GET" and len(seg) == 2 and seg[1] == "origins":
            rows = conn.execute(
                "SELECT DISTINCT origin FROM knowledge WHERE origin IS NOT NULL AND origin != '' "
                "ORDER BY origin").fetchall()
            return self._ok([r["origin"] for r in rows])
        if method == "GET":
            where, params = [], []
            if qs.get("q"):
                like = "%" + qs["q"][0] + "%"
                where.append("(topic LIKE ? OR keyword LIKE ? OR rationale LIKE ?)")
                params += [like, like, like]
            if qs.get("category"):
                where.append("category=?"); params.append(qs["category"][0])
            if qs.get("origin"):
                where.append("origin=?"); params.append(qs["origin"][0])
            sql = "SELECT * FROM knowledge"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY id"
            rows = conn.execute(sql, params).fetchall()
            return self._ok(rows_to_list(rows), total=len(rows))
        if method == "POST":
            if not body.get("topic"):
                return self._err(400, "缺少必填字段 topic")
            _insert_knowledge(conn, body)
            kid = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]
            return self._ok({"id": kid}, created_id=kid)
        if method == "PUT" and len(seg) == 2:
            cols = ["category", "topic", "keyword", "recommended", "rationale", "source", "origin"]
            sets, vals = [], []
            for k in cols:
                if k in body:
                    sets.append(k + "=?"); vals.append(body[k])
            if not sets:
                return self._err(400, "无更新字段")
            vals.append(seg[1])
            conn.execute("UPDATE knowledge SET " + ",".join(sets) + " WHERE id=?", vals)
            return self._ok({"updated": True})
        if method == "DELETE" and len(seg) == 2:
            conn.execute("DELETE FROM knowledge WHERE id=?", (seg[1],))
            return self._ok({"deleted": True})
        return self._err(405, "方法不支持")

    # ---------------- 任务 ----------------
    def _task_visible_rows(self, conn, status=None):
        """任务可见范围：东翁/掌书可见全部；清客仅可见自己提交（owner_id=本人）。

        返回 (sql_where, params)。
        """
        me = getattr(self, "auth_user", None)
        if me is not None and _norm_role(me["role"]) == ROLE_GUEST:
            return "owner_id=?", (me["id"],)
        return "", ()

    def _task_change_allowed(self, conn, row):
        """清客只能删改/下载自己提交任务产生的文件；东翁/掌书按能力已在授权层放行。"""
        me = getattr(self, "auth_user", None)
        if me is None:
            return True  # 无鉴权（AUTH_ENABLED=0）场景，保持全量
        if _norm_role(me["role"]) != ROLE_GUEST:
            return True
        owner_id = row["owner_id"] if "owner_id" in row.keys() else None
        return (owner_id is not None and owner_id == me["id"])

    def _remove_cm_task_dir(self, row):
        """删除专属任务目录 cp2k_runs/<cm_code>/（仅当目录名恰为该任务的 CM- 编号）。"""
        try:
            resources = json.loads(row["resources"] or "{}")
        except Exception:
            resources = {}
        cm_code = resources.get("cm_code") or row["cm_code"]
        wd = resources.get("workdir")
        if not cm_code or not wd:
            return None
        base = os.path.normpath(CP2K_RUN_DIR)
        wd = os.path.normpath(str(wd))
        target = os.path.normpath(os.path.join(base, str(cm_code)))
        if wd != target:
            return None  # 非专属目录（如 hts_collect 共享目录）不做物理删除
        if not (wd == base or wd.startswith(base + os.sep)):
            return None
        try:
            import shutil as _sh
            _sh.rmtree(wd, ignore_errors=True)
            return "已删除任务目录 %s" % wd
        except Exception:
            return None

    def _tasks(self, method, seg, qs, conn, body):
        # 显式同步：POST /api/tasks/sync（或 GET ?sync=1）
        if method == "POST" and len(seg) == 2 and seg[1] == "sync":
            return self._ok(sync_task_statuses(conn))
        if method == "GET" and len(seg) == 1:
            # 刷新列表前自动检测已终止任务并更新状态（轻量：仅处理非终态任务）
            synced = None
            if (qs.get("sync") or ["1"])[0] != "0":
                synced = sync_task_statuses(conn)
            where, params = [], []
            extra, extra_params = self._task_visible_rows(conn)
            if extra:
                where.append(extra); params += list(extra_params)
            if qs.get("status"):
                where.append("status=?"); params.append(qs["status"][0])
            sql = "SELECT * FROM tasks"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY id DESC LIMIT 200"
            rows = conn.execute(sql, params).fetchall()
            return self._ok(_owner_view(conn, rows_to_list(rows)), total=len(rows), synced=synced)
        if method == "GET" and len(seg) == 2:
            r = conn.execute("SELECT * FROM tasks WHERE id=?", (seg[1],)).fetchone()
            if not r:
                return self._err(404, "任务不存在")
            if not self._task_change_allowed(conn, r):
                return self._err(403, "清客只能浏览自己提交的任务", "OWNERSHIP_DENIED")
            return self._ok(_owner_view(conn, rows_to_list([r]))[0])
        if method == "POST" and len(seg) == 1:
            if not body.get("name"):
                return self._err(400, "缺少必填字段 name")
            me = getattr(self, "auth_user", None)
            owner_id = me["id"] if me is not None else None
            conn.execute(
                "INSERT INTO tasks(name,system_id,calc_type,input_file,resources,status,progress,log,owner_id) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (body["name"], body.get("system_id"), body.get("calc_type", "GEO_OPT"),
                 body.get("input_file"), json.dumps(body.get("resources", {}), ensure_ascii=False),
                 body.get("status", "queued"), 0, json.dumps([], ensure_ascii=False), owner_id))
            tid = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]
            return self._ok({"id": tid}, created_id=tid)
        if method == "PATCH" and len(seg) == 2:
            r = conn.execute("SELECT * FROM tasks WHERE id=?", (seg[1],)).fetchone()
            if not r:
                return self._err(404, "任务不存在")
            if not self._task_change_allowed(conn, r):
                return self._err(403, "清客只能修改自己提交的任务", "OWNERSHIP_DENIED")
            sets, vals = [], []
            for k in ("status", "name", "calc_type"):
                if k in body:
                    sets.append(k + "=?"); vals.append(body[k])
            if "progress" in body:
                sets.append("progress=?"); vals.append(body["progress"])
            if not sets:
                return self._err(400, "无更新字段")
            vals.append(seg[1])
            conn.execute("UPDATE tasks SET " + ",".join(sets) + " WHERE id=?", vals)
            return self._ok({"updated": True})
        if method == "DELETE" and len(seg) == 2:
            r = conn.execute("SELECT * FROM tasks WHERE id=?", (seg[1],)).fetchone()
            if not r:
                return self._err(404, "任务不存在")
            if not self._task_change_allowed(conn, r):
                return self._err(403, "清客只能删除自己提交的任务", "OWNERSHIP_DENIED")
            removed = self._remove_cm_task_dir(r)
            conn.execute("DELETE FROM tasks WHERE id=?", (seg[1],))
            return self._ok({"deleted": True, "removed_dir": removed})
        # 动作路由：/api/tasks/{id}/run|pause|cancel|log
        if method == "POST" and len(seg) == 3:
            tid = seg[1]; action = seg[2]
            r = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if not r:
                return self._err(404, "任务不存在")
            if not self._task_change_allowed(conn, r):
                return self._err(403, "清客只能操作自己提交的任务", "OWNERSHIP_DENIED")
            log = json.loads(r["log"] or "[]")
            if action == "run":
                if r["status"] == "done":
                    return self._err(409, "任务已完成")
                log.append({"ts": now_str(), "level": "accent", "msg": "提交至 AiiDA 工作流（runner 接管）"})
                conn.execute("UPDATE tasks SET status='running', log=? WHERE id=?",
                             (json.dumps(log, ensure_ascii=False), tid))
                return self._ok({"status": "running"})
            if action == "pause":
                if r["status"] == "running":
                    log.append({"ts": now_str(), "level": "warn", "msg": "任务已暂停"})
                    conn.execute("UPDATE tasks SET status='paused', log=? WHERE id=?",
                                 (json.dumps(log, ensure_ascii=False), tid))
                return self._ok({"status": "paused"})
            if action == "cancel":
                log.append({"ts": now_str(), "level": "err", "msg": "任务已取消"})
                conn.execute("UPDATE tasks SET status='cancelled', log=? WHERE id=?",
                             (json.dumps(log, ensure_ascii=False), tid))
                return self._ok({"status": "cancelled"})
            if action == "kill":
                return self._kill_task(conn, r, log)
            if action == "log":
                return self._ok(log)
        return self._err(405, "方法不支持")

    def _kill_task(self, conn, row, log):
        """停止任务：独立 CP2K 任务杀进程树；AiiDA 任务调 verdi process kill。

        POST /api/tasks/{id}/kill
        """
        tid = row["id"]
        if row["status"] in ("done", "failed", "cancelled"):
            return self._err(409, "任务已处于终态（%s），无需停止" % row["status"])

        actions, killed = [], False
        resources = {}
        try:
            resources = json.loads(row["resources"] or "{}")
        except Exception:
            resources = {}

        # ① 独立 CP2K 任务（mpirun 直连，记录有 pid）
        pid = resources.get("pid")
        if pid:
            ok, detail = terminate_process_tree(pid)
            actions.append("进程树(PID %s)：%s" % (pid, detail))
            killed = killed or ok

        # ② AiiDA 工作流任务（记录有 PK）
        pk = row["aiiada_pk"]
        if pk:
            ok, detail = kill_aiida_process(pk)
            actions.append("AiiDA(PK %s)：%s" % (pk, detail))
            killed = killed or ok

        if not actions:
            log.append({"ts": now_str(), "level": "warn",
                        "msg": "该任务无 PID / AiiDA PK 记录，仅标记为已取消"})
            conn.execute("UPDATE tasks SET status='cancelled', log=? WHERE id=?",
                         (json.dumps(log, ensure_ascii=False), tid))
            return self._ok({"status": "cancelled", "killed": False,
                             "actions": ["无进程记录，仅标记取消"]})

        level = "ok" if killed else "warn"
        for a in actions:
            log.append({"ts": now_str(), "level": level, "msg": "停止任务 → " + a})
        log.append({"ts": now_str(), "level": "err", "msg": "任务已被用户手动停止"})
        conn.execute(
            "UPDATE tasks SET status='cancelled', log=?, finished_at=? WHERE id=?",
            (json.dumps(log, ensure_ascii=False), now_str(), tid))
        return self._ok({"status": "cancelled", "killed": killed, "actions": actions})

    # ---------------- AiiDA 真实进程 ----------------
    def _aiida(self, method, seg, qs, conn, body):
        # 按 PK 停止进程：POST /api/aiida/kill/{pk}
        if method == "POST" and len(seg) >= 3 and seg[1] == "kill":
            pk = seg[2]
            if not str(pk).isdigit():
                return self._err(400, "非法 PK: %s" % pk)
            ok, detail = kill_aiida_process(pk)
            # 同步把本平台同名 PK 的任务标记为取消
            try:
                row = conn.execute("SELECT id,log FROM tasks WHERE aiiada_pk=?", (pk,)).fetchone()
                if row:
                    log = json.loads(row["log"] or "[]")
                    log.append({"ts": now_str(), "level": "err",
                                "msg": "停止 AiiDA 进程 PK %s → %s" % (pk, detail)})
                    conn.execute(
                        "UPDATE tasks SET status='cancelled', log=?, finished_at=? WHERE id=?",
                        (json.dumps(log, ensure_ascii=False), now_str(), row["id"]))
            except Exception:
                pass
            return self._ok({"pk": pk, "killed": ok, "detail": detail}) if ok \
                else self._err(500, detail)
        if method != "GET":
            return self._err(405, "仅支持 GET/POST(kill)")
        # 按 PK 查询单个任务详情 + 输入文件
        if len(seg) >= 2 and seg[1] == "task" and len(seg) >= 3:
            pk = seg[2]
            info, err = query_aiida_node(pk)
            if err:
                return self._err(500, err)
            inp_content = None
            inp_path = None
            u = info.get("uuid")
            if u:
                d = aiida_dir_for_uuid(u)
                p = os.path.join(d, "aiida.inp")
                if os.path.isfile(p):
                    try:
                        with open(p, "r", encoding="utf-8", errors="ignore") as f:
                            inp_content = f.read()
                        inp_path = p
                    except Exception:
                        pass
            info["input_file"] = inp_content
            info["input_file_path"] = inp_path
            return self._ok(info)
        # 列出某计算的 CP2K 可查看文件：GET /api/aiida/files?uuid=<uuid>
        if len(seg) >= 2 and seg[1] == "files":
            return self._aiida_files(qs)
        # 取某计算的文件内容：GET /api/aiida/file?uuid=<uuid>&name=<filename>
        if len(seg) >= 2 and seg[1] == "file":
            return self._aiida_file(qs)
        data, err = query_aiida_processes()
        if err:
            # AiiDA 进程查询失败：多为环境/daemon 依赖问题，附带结构化错误码
            return self._send(200, {"ok": True, "data": {"active": [], "recent": []},
                                    "error": err, "code": ERR_DEPENDENCY})
        return self._ok(data)

    def _aiida_files(self, qs):
        import urllib.parse as _up
        u = (qs.get("uuid") or [""])[0]
        if not u:
            return self._err(400, "缺少 uuid")
        if not re.match(r"^[0-9a-f]{8}-[0-9a-f-]{19,35}$", u, re.I):
            return self._err(400, "非法 uuid")
        d = aiida_dir_for_uuid(u)
        if not os.path.isdir(d):
            return self._err(404, "未找到该计算输出目录: " + d)
        viewable = (".inp", ".out", ".xyz", ".pdos", ".log", ".restart", ".cif", ".pdb", ".txt")
        files = []
        try:
            for fn in sorted(os.listdir(d)):
                full = os.path.join(d, fn)
                if not os.path.isfile(full):
                    continue
                if fn.startswith("_scheduler-") or fn.startswith("."):
                    continue  # 过滤调度器日志与隐藏文件
                ext = os.path.splitext(fn)[1].lower()
                if ext not in viewable:
                    continue
                files.append({
                    "name": fn,
                    "size": os.path.getsize(full),
                    "url": "/api/aiida/file?uuid=" + u + "&name=" + _up.quote(fn),
                })
        except Exception as e:
            return self._err(500, "列目录失败: %s" % e)
        return self._ok({"uuid": u, "dir": d, "files": files})

    def _aiida_file(self, qs):
        u = (qs.get("uuid") or [""])[0]
        name = (qs.get("name") or [""])[0]
        if not u or not name:
            return self._err(400, "缺少 uuid 或 name")
        if not re.match(r"^[0-9a-f]{8}-[0-9a-f-]{19,35}$", u, re.I):
            return self._err(400, "非法 uuid")
        # 防路径穿越：name 不允许含路径分隔符
        if "/" in name or "\\" in name or name in (".", "..") or name.startswith("."):
            return self._err(400, "非法文件名")
        d = aiida_dir_for_uuid(u)
        full = os.path.join(d, name)
        if not os.path.isfile(full):
            return self._err(404, "文件不存在: " + name)
        ext = os.path.splitext(full)[1].lower()
        mime = {".xyz": "chemical/x-xyz", ".json": "application/json"}.get(ext, "text/plain; charset=utf-8")
        with open(full, "rb") as f:
            return self._send(200, raw=f.read(), ctype=mime)

    # ---------------- 数据集文件（按模块独立保存） ----------------
    def _datasets(self, method, seg, qs, conn, body):
        """数据集文件管理：按模块独立保存，供训练任务显式选择。

        GET /api/datasets?module=ml|al
            → 列出该模块前缀的数据集文件（ML-XXX.csv / AL-XXX.csv）
        说明：ML 与 AL 的导入此前都写入同一张 ml_samples 表，训练时全表取数导致
        两类数据混用。落盘为带模块前缀的独立文件后，训练任务只能选择本模块的数据集。
        """
        # 删除数据集文件：DELETE /api/datasets?module=ml|al&file=XXX
        # 仅删除按模块落盘的独立文件，不影响 ml_samples / systems 中的样本。
        if method == "DELETE":
            module = (qs.get("module") or [""])[0].strip().lower() or None
            if module and module not in _MODULE_PREFIX:
                return self._err(400, "未知模块: %s（可选 ml / al）" % module)
            fname = (qs.get("file") or [""])[0].strip()
            if not fname:
                return self._err(400, "缺少 file")
            full = os.path.join(DATASETS_DIR, os.path.basename(fname))
            if os.path.dirname(os.path.abspath(full)) != os.path.abspath(DATASETS_DIR):
                return self._err(400, "数据集文件不在受管目录内")
            if module:
                prefix = _MODULE_PREFIX.get(module)
                if prefix and not os.path.basename(full).upper().startswith(prefix + "-"):
                    return self._err(400, "数据集不属于该模块（%s），拒绝删除" % module)
            if not os.path.isfile(full):
                return self._err(404, "数据集文件不存在")
            try:
                os.remove(full)
            except OSError as e:
                return self._err(500, "删除失败: %s" % e)
            audit(conn, "delete_dataset", target=os.path.basename(full),
                  params={"module": module}, ok=True, actor="api")
            return self._ok({"deleted": os.path.basename(full)})

        if method != "GET":
            return self._err(405, "仅支持 GET / DELETE")
        module = (qs.get("module") or [""])[0].strip().lower() or None
        if module and module not in _MODULE_PREFIX:
            return self._err(400, "未知模块: %s（可选 ml / al）" % module)

        # 读取某个数据集文件的内容（供「查看数据集」按独立文件查看，而非整张混合表）
        if len(seg) >= 2 and seg[1] == "rows":
            fname = (qs.get("file") or [""])[0].strip()
            if not fname:
                return self._err(400, "缺少 file")
            full = os.path.join(DATASETS_DIR, os.path.basename(fname))
            rows, err = load_dataset_file(full, module=module)
            if err:
                return self._err(400, err)
            cols = []
            for r in rows:
                for k in (r.get("features") or {}):
                    if k not in cols:
                        cols.append(k)
            return self._ok({"file": os.path.basename(full), "module": module,
                             "columns": cols, "count": len(rows),
                             "rows": rows[:500], "truncated": len(rows) > 500})

        try:
            files = list_dataset_files(module)
        except OSError as e:
            return self._err(500, "读取数据集目录失败: %s" % e)
        return self._ok({"dir": DATASETS_DIR, "module": module, "count": len(files),
                         "files": files})

    # ---------------- CP2K 视图（指定路径/PK 打开） ----------------
    def _cp2kview(self, method, seg, qs, conn, body):
        """CP2K 视图：按输出文件路径或 AiiDA PK 打开对应文件目录，查看与下载。

        GET /api/cp2kview/open?pk=<pk>  或  ?path=<文件/目录路径>
        GET /api/cp2kview/file?path=<完整文件路径>   （下载/查看）
        """
        import urllib.parse as _up
        allowed = [os.path.realpath(AIIDA_FILES), os.path.realpath(CP2K_RUN_DIR),
                   os.path.realpath(CP2K_DATA_DIR)]

        def _safe_resolve(p):
            rp = os.path.realpath(p)
            for base in allowed:
                if rp == base or rp.startswith(base + os.sep):
                    return rp
            return None

        viewable = (".inp", ".out", ".xyz", ".pdos", ".log", ".restart", ".cif", ".pdb", ".wfn", ".cube")

        def _list_entries(d, urlfn):
            if os.path.isfile(d):
                return {"dirs": [], "files": [{"name": os.path.basename(d), "size": os.path.getsize(d), "url": urlfn(d)}]}
            if not os.path.isdir(d):
                return None
            dirs, files = [], []
            for fn in sorted(os.listdir(d)):
                full = os.path.join(d, fn)
                if fn.startswith("_scheduler-") or fn.startswith("."):
                    continue
                if os.path.isdir(full):
                    dirs.append({"name": fn, "path": full})
                elif os.path.isfile(full):
                    ext = os.path.splitext(fn)[1].lower()
                    if ext in viewable:
                        files.append({"name": fn, "size": os.path.getsize(full), "url": urlfn(full)})
            return {"dirs": dirs, "files": files}

        if method == "GET" and len(seg) >= 2 and seg[1] == "open":
            pk = (qs.get("pk") or [""])[0].strip()
            path = (qs.get("path") or [""])[0].strip()
            kind = "path"
            if pk:
                info, err = query_aiida_node(pk)
                if err:
                    return self._err(500, err)
                u = info.get("uuid")
                if not u:
                    return self._err(404, "未找到 PK %s 对应节点的 uuid" % pk)
                d = aiida_dir_for_uuid(u)
                kind = "aiida"
            else:
                d = _safe_resolve(path) if path else os.path.realpath(CP2K_RUN_DIR)
            if not d or not os.path.isdir(d):
                return self._err(404, "目录不存在或不在允许目录内: %s" % (d or path))
            entries = _list_entries(d, lambda full: "/api/cp2kview/file?path=" + _up.quote(full) + "&name=" + _up.quote(os.path.basename(full)))
            if entries is None:
                return self._err(404, "路径不可读")
            root = os.path.realpath(CP2K_RUN_DIR)
            parent = os.path.dirname(d) if os.path.realpath(d) != root else None
            return self._ok({"kind": kind, "pk": pk or None, "dir": d, "parent": parent,
                             "dirs": entries["dirs"], "files": entries["files"]})

        if method == "GET" and len(seg) >= 2 and seg[1] == "file":
            path = (qs.get("path") or [""])[0].strip()
            d = _safe_resolve(path)
            if not d or not os.path.isfile(d):
                return self._err(404, "文件不存在或不在允许目录内")
            try:
                with open(d, "rb") as f:
                    content = f.read()
            except Exception as e:
                return self._err(500, "读取失败: %s" % e)
            ext = os.path.splitext(d)[1].lower()
            mime = {".xyz": "chemical/x-xyz", ".json": "application/json",
                    ".inp": "text/plain; charset=utf-8", ".out": "text/plain; charset=utf-8",
                    ".log": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8",
                    ".cif": "text/plain; charset=utf-8", ".pdb": "text/plain; charset=utf-8",
                    ".pdos": "text/plain; charset=utf-8", ".restart": "text/plain; charset=utf-8"}.get(ext, "application/octet-stream")
            fname = os.path.basename(d)
            disp = 'inline; filename="%s"' % fname
            return self._send(200, raw=content, ctype=mime, headers={"Content-Disposition": disp})
        return self._err(405, "方法不支持")

    # ---------------- ASE 结构生成 ----------------
    def _build(self, method, seg, qs, conn, body):
        if method != "POST":
            return self._err(405, "仅支持 POST")
        p = body or {}
        # [中1] 校验参数取值（非法取值直接报错，避免静默回退）
        ok, verr = validate_ase_params(p)
        if not ok:
            return self._err(400, verr)
        result = build_structure_ase(p)
        # 需求：ASE 生成的结构仅在用户明确确认保存时才入库。
        # 此处只返回结构数据与「建议名称」，不做任何数据库写入；
        # 保存由前端「保存到体系列表」显式调用 /api/systems 完成。
        result["suggest_name"] = (result["formula"] + " " + result["info"]["face"]
                                 + (" @" + result["adsorbate"] if result.get("adsorbate") else ""))
        result["saved"] = "unsaved"
        return self._ok(result)

    # ---------------- PDOS 后处理 ----------------
    def _postprocess(self, method, seg, qs, conn, body):
        if len(seg) >= 2 and seg[1] == "tools":
            return self._postprocess_tools(method, seg, conn, body)
        action = seg[1] if len(seg) > 1 and seg[1] in ("preview", "accept", "admit") else "preview"
        if method == "GET" and len(seg) == 3 and seg[1] == "review":
            row = conn.execute("SELECT * FROM pdos_reviews WHERE id=?", (seg[2],)).fetchone()
            return self._ok(pdos_review.response(row)) if row else self._err(404, "预览记录不存在")
        if method != "POST":
            return self._err(405, "仅支持 POST，或 GET /postprocess/review/{id}")
        if action in ("accept", "admit"):
            rid = body.get("review_id")
            row = conn.execute("SELECT * FROM pdos_reviews WHERE id=?", (rid,)).fetchone()
            if not row:
                return self._err(404, "预览记录不存在")
            me = getattr(self, "auth_user", None)
            reviewer = "uid:%s" % me["id"] if me is not None else "local"
            try:
                data = pdos_review.transition(conn, rid, action, body, reviewer, aiida_dir_for_uuid(row["uuid"]))
            except ValueError as exc:
                return self._err(409, str(exc))
            return self._ok(data)
        uuid = body.get("uuid") or (seg[1] if len(seg) > 1 and seg[1] != "preview" else None)
        if not isinstance(uuid, str) or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", uuid, re.I):
            return self._err(400, "缺少或非法 uuid")
        d = aiida_dir_for_uuid(uuid)
        if not os.path.isdir(d):
            return self._err(404, "未找到该计算的输出目录: %s" % d)
        try:
            before = pdos_review.snapshot(d)
            env = dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
            with _blocking_unlocked():
                out = subprocess.run([AII_PY, POSTPROCESS, d], capture_output=True, text=True,
                                     timeout=180, env=env)
            if out.returncode != 0:
                return self._err(422, "后处理失败: " + (out.stdout or out.stderr or "")[:500])
            res = json.loads(out.stdout)
            if not res.get("ok"):
                return self._err(422, res.get("error", "后处理失败"))
            data = pdos_review.create_preview(conn, uuid, res["result"], before, pdos_review.snapshot(d))
        except ValueError as exc:
            return self._err(409, str(exc))
        except Exception as exc:
            return self._err(500, "后处理执行失败: %s" % exc)
        return self._ok(data)

    def _postprocess_tools(self, method, seg, conn, body):
        me = getattr(self, "auth_user", None)
        uid = me["id"] if me is not None else None
        owner = me is None or _effective_role(me) == ROLE_OWNER
        root = os.path.join(DATA_DIR, "postprocess_runs")
        if method == "GET" and len(seg) == 2:
            return self._ok(postprocessing.toolkit.catalog())
        if method == "POST" and seg[2:] in (["browse"], ["upload"]):
            upload_root = os.path.join(DATA_DIR, "postprocess_inputs", str(uid if uid is not None else "local"))
            try:
                if not isinstance(body, dict):
                    raise ValueError("请求必须为对象")
                if seg[2] == "browse":
                    result = postprocessing.browse_inputs(body.get("field"), body.get("path"),
                        [AIIDA_FILES, CP2K_RUN_DIR, *POSTPROCESS_EXTRA_ROOTS, upload_root])
                else:
                    result = postprocessing.upload_inputs(body.get("field"), body.get("files"), upload_root)
            except (ValueError, OSError) as exc:
                return self._err(400, str(exc))
            return self._ok(result)
        if method == "POST" and seg[2:] == ["run"]:
            try:
                cfg = postprocessing.validate(body, [AIIDA_FILES, CP2K_RUN_DIR, DATA_DIR, TMP_DIR,
                                                       *POSTPROCESS_EXTRA_ROOTS])
                result = postprocessing.start(conn, DB_PATH, root, AII_PY, cfg, uid)
            except ValueError as exc:
                return self._err(400, str(exc))
            except RuntimeError as exc:
                return self._err(409, str(exc))
            audit(conn, "postprocess_start", target=result["id"], params={"tool":cfg["tool"]}, result="queued")
            return self._ok(result)
        if method == "GET" and seg[2:] == ["jobs"]:
            rows = conn.execute("SELECT * FROM postprocess_jobs " + ("" if owner else "WHERE owner_id=? ") +
                                "ORDER BY created_at DESC,id DESC LIMIT 50", () if owner else (uid,)).fetchall()
            return self._ok([postprocessing.view(r) for r in rows])
        if method == "GET" and len(seg) in (4, 5) and seg[2] == "jobs":
            row = conn.execute("SELECT * FROM postprocess_jobs WHERE id=?", (seg[3],)).fetchone()
            if row is None or (not owner and row["owner_id"] != uid):
                return self._err(404, "后处理任务不存在")
            if len(seg) == 4:
                return self._ok(postprocessing.view(row, True))
            result = json.loads(row["result"]) if row["result"] else {}
            names = {a["name"] for a in result.get("artifacts", [])} | ({"result.json"} if result else set())
            name = seg[4]
            if name not in names or os.path.basename(name) != name:
                return self._err(404, "产物不存在")
            path = os.path.join(root, row["id"], name)
            if not os.path.isfile(path): return self._err(404, "产物文件已缺失")
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(os.path.getsize(path)))
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
            self.end_headers()
            with _blocking_unlocked():
                with open(path, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, 1024 * 1024)
            return None
        return self._err(405, "方法不支持")

    # ---------------- ML ----------------
    def _ml(self, method, seg, qs, conn, body):
        if method == "GET" and len(seg) == 1:
            samples = conn.execute("SELECT * FROM ml_samples ORDER BY id").fetchall()
            # 特征并集
            keys = set()
            for s in samples:
                for k in json.loads(s["features"] or "{}"):
                    keys.add(k)
            provenance = pdos_review.provenance_by_source(conn)
            sample_data = [dict(s, scientific_review=provenance.get(s["source"])) for s in samples]
            return self._ok({"samples": sample_data, "features": sorted(keys),
                             "total": len(samples)})
        if method == "GET" and len(seg) == 2 and seg[1] == "export":
            fmt = (qs.get("format") or ["csv"])[0].lower()
            return self._export_dataset(conn, fmt)
        if method == "POST" and len(seg) == 2 and seg[1] == "train":
            # 用 AiiDA 环境的 scikit-learn 训练回归模型（支持模型选择/超参/预处理/真实 SHAP）
            me = getattr(self, "auth_user", None)
            tid, err = _start_ml_task(body.get("name"), body.get("model"), body.get("device"), body,
                                      owner_id=me["id"] if me is not None else None)
            data = {"task_id": tid, "status": "running", "async": True} if not err else None
            if err:
                return self._send(200, {"ok": False, "error": err})
            return self._ok(data)
        # 异步训练（后台任务，可监控进度/终止）：POST /api/ml/train-async
        if method == "POST" and seg and seg[0] == "ml" and len(seg) == 2 and seg[1] == "train-async":
            name = body.get("name")
            model = body.get("model") or (body.get("params") or {}).get("model") or "rf"
            device = body.get("device") or "auto"
            me = getattr(self, "auth_user", None)
            tid, err = _start_ml_task(name, model, device, body,
                                      owner_id=me["id"] if me is not None else None)
            if err:
                # 数据集选错属于调用方参数问题，返回 400 而非 500
                return self._err(400 if "数据集" in err else 500, err)
            return self._ok({"task_id": tid, "status": "running"})
        # 多模型集成对比训练（后台任务）：POST /api/ml/compare-async
        if method == "POST" and len(seg) == 2 and seg[1] == "compare-async":
            me = getattr(self, "auth_user", None)
            tid, err = _start_compare_task(body.get("name"), body or {},
                                           owner_id=me["id"] if me is not None else None)
            if err:
                return self._err(400 if any(k in err for k in ("数据集", "模型", "样本不足"))
                                 else 500, err)
            return self._ok({"task_id": tid, "status": "running"})
        # 对比任务列表：GET /api/ml/comparisons
        if method == "GET" and len(seg) == 2 and seg[1] == "comparisons":
            _sync_ml_tasks(conn)
            rows = conn.execute(
                "SELECT id,name,status,progress,created_at,finished_at,error,result "
                "FROM ml_tasks WHERE task_type='mlcmp' ORDER BY id DESC LIMIT 50").fetchall()
            return self._ok({"comparisons": [_cmp_brief(r) for r in rows]})
        # 对比任务详情：GET /api/ml/compare/{id}
        if method == "GET" and len(seg) == 3 and seg[1] == "compare" and seg[2].isdigit():
            r = conn.execute("SELECT * FROM ml_tasks WHERE id=? AND task_type='mlcmp'",
                             (seg[2],)).fetchone()
            if not r:
                return self._err(404, "对比任务不存在")
            return self._ok(_cmp_brief(r, with_result=True))
        # 训练任务列表：GET /api/ml/tasks
        if method == "GET" and seg and seg[0] == "ml" and len(seg) == 2 and seg[1] == "tasks":
            _sync_ml_tasks(conn)
            rows = conn.execute(
                "SELECT * FROM ml_tasks WHERE task_type='ml' ORDER BY id DESC LIMIT 100").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                for k in ("log", "params"):
                    if isinstance(d.get(k), str):
                        try:
                            d[k] = json.loads(d[k])
                        except Exception:
                            pass
                out.append(d)
            return self._ok(_owner_view(conn, out), total=len(out))
        # 训练任务详情：GET /api/ml/tasks/{id}
        if method == "GET" and seg and seg[0] == "ml" and len(seg) == 3 and seg[1] == "tasks" and seg[2].isdigit():
            r = conn.execute("SELECT * FROM ml_tasks WHERE id=? AND task_type='ml'",
                             (seg[2],)).fetchone()
            if not r:
                return self._err(404, "训练任务不存在")
            d = dict(r)
            for k in ("log", "params", "result"):
                if isinstance(d.get(k), str):
                    try:
                        d[k] = json.loads(d[k])
                    except Exception:
                        pass
            return self._ok(_owner_view(conn, [d])[0])
        # 终止训练任务：POST /api/ml/tasks/{id}/kill
        if method == "POST" and seg and seg[0] == "ml" and len(seg) == 4 and seg[1] == "tasks" and seg[3] == "kill":
            r = conn.execute("SELECT * FROM ml_tasks WHERE id=? AND task_type='ml'",
                             (seg[2],)).fetchone()
            if not r:
                return self._err(404, "训练任务不存在")
            if r["status"] != "running":
                return self._err(400, "任务已结束（状态：%s）" % r["status"])
            killed, detail = False, ""
            try:
                os.kill(int(r["pid"]), 15)  # SIGTERM
                killed, detail = True, "已发送 SIGTERM 到 PID %s" % r["pid"]
            except Exception as e:
                detail = "终止失败: %s" % e
            log = json.loads(r["log"] or "[]")
            log.append({"ts": now_str(), "msg": detail})
            conn.execute("UPDATE ml_tasks SET status='cancelled', log=?, progress=0, "
                         "error=?, finished_at=? WHERE id=?",
                         (json.dumps(log, ensure_ascii=False), detail, now_str(), r["id"]))
            return self._ok({"killed": killed, "detail": detail, "id": int(seg[2])})
        # 删除训练任务：DELETE /api/ml/tasks/{id}（仅已结束的任务）
        if method == "DELETE" and seg and seg[0] == "ml" and len(seg) == 3 and seg[1] == "tasks" and seg[2].isdigit():
            r = conn.execute("SELECT status FROM ml_tasks WHERE id=? AND task_type='ml'",
                             (seg[2],)).fetchone()
            if not r:
                return self._err(404, "训练任务不存在")
            if r["status"] == "running":
                return self._err(400, "任务仍在运行中，请先终止")
            conn.execute("DELETE FROM ml_tasks WHERE id=?", (seg[2],))
            return self._ok({"deleted": True, "id": int(seg[2])})

        # 主动导入数据集（需求：上传/粘贴数据集写入 ml_samples，可随后触发训练）
        # CSV 自动识别表头：样本名列 / 目标(标签)列 / 特征列均可任意命名；
        # 传 dry_run=true 只解析并返回列角色（供前端预览映射）；传 mapping 可显式指定：
        #   {name_col, target_col, feature_cols:[..], exclude_cols:[..]}
        if method == "POST" and len(seg) == 2 and seg[1] == "import":
            rows = body.get("rows") or []
            fmt = body.get("format") or "json"
            dry_run = bool(body.get("dry_run"))
            detected = None
            warnings = []
            if not rows and body.get("csv"):
                try:
                    norm = normalize_dataset_csv(body["csv"], mapping=body.get("mapping"),
                                                 mode="ml")
                    detected = norm["detected"]
                    warnings = norm["warnings"]
                    if dry_run:
                        return self._ok({"dry_run": True, "detected": detected,
                                         "columns": detected["columns"],
                                         "name_col": norm["name_col"],
                                         "target_col": norm["target_col"],
                                         "feature_cols": norm["feature_cols"],
                                         "row_count": len(norm["rows"]),
                                         "sample_rows": norm["rows"][:5],
                                         "warnings": warnings})
                    rows = norm["rows"]
                except Exception as e:
                    return self._err(400, "CSV 解析失败: %s" % e)
            if not isinstance(rows, list) or not rows:
                return self._err(400, "缺少 rows（JSON 数组）或 csv 文本")
            ok_n, skipped = 0, 0
            for r in rows:
                if not isinstance(r, dict) or not r.get("name"):
                    skipped += 1
                    continue
                feats = r.get("features") or {}
                if isinstance(feats, str):
                    try:
                        feats = json.loads(feats)
                    except Exception:
                        feats = {}
                tgt = r.get("target")
                if isinstance(tgt, str):
                    try:
                        tgt = float(tgt)
                    except ValueError:
                        tgt = None
                conn.execute("INSERT INTO ml_samples(name,source,features,target) VALUES(?,?,?,?)",
                             (str(r["name"]), r.get("source") or ("import_" + fmt),
                              json.dumps(feats, ensure_ascii=False), tgt))
                ok_n += 1
            # 可选：导入后立即训练
            trained = None
            # Training must use the newly saved dataset, after transaction commit.
            resp = {"imported": ok_n, "skipped": skipped, "trained": trained}
            if detected is not None:
                resp["detected"] = detected
                resp["warnings"] = warnings
            # 按模块落盘为独立数据集文件 ML-XXX.csv（与 AL 数据集物理隔离，便于训练时显式选择）
            if ok_n:
                try:
                    _p, _fn = save_dataset_file(
                        "ml", body.get("dataset_name") or ("import_" + fmt), rows)
                    if _fn:
                        resp["dataset_file"] = _fn
                except OSError as _e:
                    LOG.warning("ML 数据集落盘失败: %s", _e)
            if body.get("train") and ok_n >= 5:
                if not resp.get("dataset_file"):
                    resp["training_error"] = "数据集未保存，未启动训练"
                else:
                    conn.commit()
                    cfg = dict(body.get("train_params") or {})
                    cfg.update(dataset_file=resp["dataset_file"], dataset_module="ml")
                    me = getattr(self, "auth_user", None)
                    tid, err = _start_ml_task(cfg.get("name"), cfg.get("model"), cfg.get("device"), cfg,
                                              owner_id=me["id"] if me is not None else None)
                    resp["trained"] = {"task_id": tid, "status": "running", "async": True} if not err else None
                    if err: resp["training_error"] = err
            return self._ok(resp)
        if method == "POST" and len(seg) == 1:
            # 手动写入样本
            if not body.get("name"):
                return self._err(400, "缺少 name")
            conn.execute("INSERT INTO ml_samples(name,source,features,target) VALUES(?,?,?,?)",
                         (body["name"], body.get("source", "manual"),
                          json.dumps(body.get("features", {}), ensure_ascii=False), body.get("target")))
            return self._ok({"created": True})
        # 更新样本：PUT /api/ml/{id} {name?, features?, target?, source?}
        if method == "PUT" and len(seg) == 2 and seg[1].isdigit():
            sid = int(seg[1])
            row = conn.execute("SELECT * FROM ml_samples WHERE id=?", (sid,)).fetchone()
            if not row:
                return self._err(404, "样本不存在")
            name = body.get("name", row["name"])
            source = body.get("source", row["source"])
            if "features" in body:
                feats = body.get("features") or {}
                if isinstance(feats, str):
                    try:
                        feats = json.loads(feats)
                    except Exception:
                        feats = {}
                features = json.dumps(feats, ensure_ascii=False)
            else:
                features = row["features"]
            target = body["target"] if "target" in body else row["target"]
            conn.execute("UPDATE ml_samples SET name=?, source=?, features=?, target=? WHERE id=?",
                         (name, source, features, target, sid))
            return self._ok({"updated": True, "id": sid})
        # 删除样本：DELETE /api/ml/{id}
        if method == "DELETE" and len(seg) == 2 and seg[1].isdigit():
            sid = int(seg[1])
            cur = conn.execute("DELETE FROM ml_samples WHERE id=?", (sid,))
            if cur.rowcount == 0:
                return self._err(404, "样本不存在")
            return self._ok({"deleted": True, "id": sid})
        return self._err(405, "方法不支持")

    def _export_dataset(self, conn, fmt):
        samples = conn.execute("SELECT * FROM ml_samples ORDER BY id").fetchall()
        # 合并体系描述符 + 任务结果 作为完整样本（若 ml_samples 为空，则从 systems 描述符派生）
        if not samples:
            sys_rows = conn.execute("SELECT * FROM systems").fetchall()
            for s in sys_rows:
                descs = json.loads(s["descriptors"] or "[]")
                feats = {d["name"]: d["value"] for d in descs}
                target = feats.pop("eta_OER", None)
                feats.pop("d_band_center", None) if False else None
                samples.append({"name": s["name"], "source": "system",
                                "features": json.dumps(feats, ensure_ascii=False), "target": target})
        keys = set()
        for s in samples:
            for k in json.loads(s["features"] or "{}"):
                keys.add(k)
        keys = sorted(keys)
        provenance = pdos_review.provenance_by_source(conn)
        if fmt == "json":
            data = [{"name": s["name"], "source": s["source"],
                     "features": json.loads(s["features"] or "{}"), "target": s["target"], "scientific_review": provenance.get(s["source"])} for s in samples]
            return self._ok({"features": keys, "rows": data, "total": len(samples)})
        # CSV
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["name", "source", "target", "scientific_review_json"] + keys)
        for s in samples:
            feats = json.loads(s["features"] or "{}")
            w.writerow([s["name"], s["source"], s["target"], json.dumps(provenance.get(s["source"]), ensure_ascii=False)] + [feats.get(k, "") for k in keys])
        return self._send(200, raw=buf.getvalue().encode("utf-8-sig"),
                          ctype="text/csv; charset=utf-8")

    # ---------------- 主动学习 ----------------
    def _al(self, method, seg, qs, conn, body):
        # 提交推荐体系计算：POST /api/al/submit {system_ids:[...], calc_type:"PDOS"}
        if method == "POST" and len(seg) == 2 and seg[1] == "submit":
            ids = body.get("system_ids") or []
            if not isinstance(ids, list) or not ids:
                return self._err(400, "缺少 system_ids 列表")
            calc_type = body.get("calc_type") or "PDOS"
            results = []
            for sid in ids:
                try:
                    sid = int(sid)
                except (TypeError, ValueError):
                    results.append({"system_id": sid, "ok": False, "error": "非法体系 id"})
                    continue
                row = conn.execute("SELECT id,name FROM systems WHERE id=?", (sid,)).fetchone()
                if not row:
                    results.append({"system_id": sid, "ok": False, "error": "体系不存在"})
                    continue
                if not _structures_from_system(conn, sid):
                    results.append({"system_id": sid, "name": row["name"], "ok": False,
                                    "error": "体系无结构（请先在「结构生成」构建）"})
                    continue
                data, err = submit_verdi_calc(conn, "AL_" + (row["name"] or str(sid)), sid, calc_type)
                if err:
                    results.append({"system_id": sid, "name": row["name"], "ok": False, "error": err})
                else:
                    results.append({"system_id": sid, "name": row["name"], "ok": True, "pk": data.get("pk")})
            return self._ok({"results": results,
                             "submitted": sum(1 for r in results if r.get("ok")),
                             "failed": sum(1 for r in results if not r.get("ok"))})

        # 标注回流：POST /api/al/annotate {system_id, eta_oer}
        if method == "POST" and len(seg) == 2 and seg[1] == "annotate":
            sid = body.get("system_id")
            eta = body.get("eta_oer")
            if sid is None or eta is None:
                return self._err(400, "缺少 system_id / eta_oer")
            try:
                eta = float(eta)
                sid = int(sid)
            except (TypeError, ValueError):
                return self._err(400, "eta_oer 需为数值、system_id 需为整数")
            row = conn.execute("SELECT * FROM systems WHERE id=?", (sid,)).fetchone()
            if not row:
                return self._err(404, "体系不存在")
            # 1) 更新 systems.descriptors：加/更新 eta_OER
            try:
                descs = json.loads(row["descriptors"] or "[]")
            except Exception:
                descs = []
            descs = [d for d in descs if d.get("name") != "eta_OER"]
            descs.append({"name": "eta_OER", "value": eta, "unit": "V"})
            conn.execute("UPDATE systems SET descriptors=? WHERE id=?",
                         (json.dumps(descs, ensure_ascii=False), sid))
            # 2) 更新 ml_samples：按 name 匹配样本更新 target，无则新建
            name = row["name"]
            found = conn.execute("SELECT id FROM ml_samples WHERE name=?", (name,)).fetchone()
            if found:
                conn.execute("UPDATE ml_samples SET target=? WHERE id=?", (eta, found["id"]))
            else:
                feats = {d["name"]: d["value"] for d in descs
                         if d.get("name") != "eta_OER" and isinstance(d.get("value"), (int, float))}
                conn.execute("INSERT INTO ml_samples(name,source,features,target) VALUES(?,?,?,?)",
                             (name, "al_annotate", json.dumps(feats, ensure_ascii=False), eta))
            return self._ok({"system_id": sid, "name": name, "eta_oer": eta, "annotated": True})
        # ================= 主动学习任务（异步后台，可监控/终止/删除） =================
        # 启动主动学习任务：POST /api/al/train-async
        if method == "POST" and len(seg) == 2 and seg[1] == "train-async":
            name = body.get("name")
            method_name = body.get("method") or (body.get("params") or {}).get("method") or "gpr_ucb"
            device = body.get("device") or "auto"
            me = getattr(self, "auth_user", None)
            tid, err = _start_task("al", name, method_name, device, body,
                                   owner_id=me["id"] if me is not None else None)
            if err:
                # 数据集选错属于调用方参数问题，返回 400 而非 500
                return self._err(400 if "数据集" in err else 500, err)
            return self._ok({"task_id": tid, "status": "running", "method": method_name})
        # 多描述符主动学习：POST /api/al/multidim-async
        if method == "POST" and len(seg) == 2 and seg[1] == "multidim-async":
            me = getattr(self, "auth_user", None)
            tid, err = _start_al_multidim(body.get("name"), body or {},
                                          owner_id=me["id"] if me is not None else None)
            if err:
                return self._err(400 if any(k in err for k in ("数据集", "方法", "候选"))
                                 else 500, err)
            return self._ok({"task_id": tid, "status": "running",
                             "method": (body or {}).get("method") or "gpr_ucb"})
        # 多描述符主动学习列表：GET /api/al/multidim
        if method == "GET" and len(seg) == 2 and seg[1] == "multidim":
            _sync_ml_tasks(conn)
            rows = conn.execute(
                "SELECT id,name,status,progress,created_at,finished_at,error,result "
                "FROM ml_tasks WHERE task_type='almd' ORDER BY id DESC LIMIT 50").fetchall()
            return self._ok({"runs": [_al_brief(r) for r in rows]})
        # 多描述符主动学习详情：GET /api/al/multidim/{id}
        if method == "GET" and len(seg) == 3 and seg[1] == "multidim" and seg[2].isdigit():
            r = conn.execute("SELECT * FROM ml_tasks WHERE id=? AND task_type='almd'",
                             (seg[2],)).fetchone()
            if not r:
                return self._err(404, "主动学习任务不存在")
            return self._ok(_al_brief(r, with_result=True))
        # 主动学习任务列表：GET /api/al/tasks
        if method == "GET" and len(seg) == 2 and seg[1] == "tasks":
            _sync_ml_tasks(conn)
            rows = conn.execute(
                "SELECT * FROM ml_tasks WHERE task_type='al' ORDER BY id DESC LIMIT 100").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                for k in ("log", "params"):
                    if isinstance(d.get(k), str):
                        try:
                            d[k] = json.loads(d[k])
                        except Exception:
                            pass
                out.append(d)
            return self._ok(out, total=len(out))
        # 主动学习任务详情：GET /api/al/tasks/{id}
        if method == "GET" and len(seg) == 3 and seg[1] == "tasks" and seg[2].isdigit():
            r = conn.execute("SELECT * FROM ml_tasks WHERE id=? AND task_type='al'",
                             (seg[2],)).fetchone()
            if not r:
                return self._err(404, "主动学习任务不存在")
            d = dict(r)
            for k in ("log", "params", "result"):
                if isinstance(d.get(k), str):
                    try:
                        d[k] = json.loads(d[k])
                    except Exception:
                        pass
            return self._ok(d)
        # 终止主动学习任务：POST /api/al/tasks/{id}/kill
        if method == "POST" and len(seg) == 4 and seg[1] == "tasks" and seg[3] == "kill":
            r = conn.execute("SELECT * FROM ml_tasks WHERE id=? AND task_type='al'",
                             (seg[2],)).fetchone()
            if not r:
                return self._err(404, "主动学习任务不存在")
            if r["status"] != "running":
                return self._err(400, "任务已结束（状态：%s）" % r["status"])
            killed, detail = False, ""
            try:
                os.kill(int(r["pid"]), 15)  # SIGTERM
                killed, detail = True, "已发送 SIGTERM 到 PID %s" % r["pid"]
            except Exception as e:
                detail = "终止失败: %s" % e
            log = json.loads(r["log"] or "[]")
            log.append({"ts": now_str(), "msg": detail})
            conn.execute("UPDATE ml_tasks SET status='cancelled', log=?, progress=0, "
                         "error=?, finished_at=? WHERE id=?",
                         (json.dumps(log, ensure_ascii=False), detail, now_str(), r["id"]))
            return self._ok({"killed": killed, "detail": detail, "id": int(seg[2])})
        # 删除主动学习任务：DELETE /api/al/tasks/{id}（仅已结束的任务）
        if method == "DELETE" and len(seg) == 3 and seg[1] == "tasks" and seg[2].isdigit():
            r = conn.execute("SELECT status FROM ml_tasks WHERE id=? AND task_type='al'",
                             (seg[2],)).fetchone()
            if not r:
                return self._err(404, "主动学习任务不存在")
            if r["status"] == "running":
                return self._err(400, "任务仍在运行中，请先终止")
            conn.execute("DELETE FROM ml_tasks WHERE id=?", (seg[2],))
            return self._ok({"deleted": True, "id": int(seg[2])})
        # 导入数据集：POST /api/al/import {rows:[{name,ed,eta?}]} 或 {csv}
        # 有 eta 的行进入训练集；无 eta 的行作为候选集；同步写入 ml_samples 与 systems
        # CSV 自动识别表头角色：样本名列 / ε_d 特征列 / η_OER(eta) 目标列均可任意命名；
        # dry_run=true 仅解析返回列映射供前端预览；mapping={name_col, ed_col, target_col} 显式指定。
        if method == "POST" and len(seg) == 2 and seg[1] == "import":
            rows = body.get("rows") or []
            dry_run = bool(body.get("dry_run"))
            detected = None
            warnings = []
            if not rows and body.get("csv"):
                try:
                    norm = normalize_dataset_csv(body["csv"], mapping=body.get("mapping"),
                                                 mode="al")
                    detected = norm["detected"]
                    warnings = norm["warnings"]
                    if dry_run:
                        return self._ok({"dry_run": True, "detected": detected,
                                         "columns": detected["columns"],
                                         "name_col": norm["name_col"],
                                         "target_col": norm["target_col"],
                                         "ed_col": norm["ed_col"],
                                         "feature_cols": norm["feature_cols"],
                                         "row_count": len(norm["rows"]),
                                         "sample_rows": norm["rows"][:5],
                                         "warnings": warnings})
                    ed_col = norm["ed_col"] or (norm["feature_cols"][0] if norm["feature_cols"] else None)
                    rows = []
                    for it in norm["rows"]:
                        ed = None
                        if ed_col:
                            try:
                                ed = float(it["features"].get(ed_col))
                            except (TypeError, ValueError):
                                ed = None
                        if ed is None:
                            continue
                        item = {"name": it["name"], "ed": ed, "extra_features": it["features"]}
                        if it["target"] is not None:
                            item["eta"] = it["target"]
                        rows.append(item)
                except Exception as e:
                    return self._err(400, "CSV 解析失败: %s" % e)
            if not isinstance(rows, list) or not rows:
                return self._err(400, "缺少 rows（[{name,ed,eta?}]）或 csv 文本")
            n_train = n_cand = n_new = 0
            ds_rows = []          # 用于按模块落盘为独立数据集文件 AL-XXX.csv
            for r in rows:
                if not isinstance(r, dict) or not r.get("name"):
                    continue
                name = str(r["name"]).strip()
                try:
                    ed = float(r.get("ed"))
                except (TypeError, ValueError):
                    continue
                eta = r.get("eta")
                try:
                    eta = float(eta) if eta not in (None, "") else None
                except (TypeError, ValueError):
                    eta = None
                # 1) ml_samples：写入训练/候选样本（ε_d 固定键 + 其余表头特征保留）
                feats = dict(r.get("extra_features") or {})
                feats["d_band_center"] = ed
                hit = conn.execute("SELECT id FROM ml_samples WHERE name=?", (name,)).fetchone()
                if hit:
                    conn.execute("UPDATE ml_samples SET features=?, target=? WHERE id=?",
                                 (json.dumps(feats, ensure_ascii=False), eta, hit["id"]))
                else:
                    conn.execute("INSERT INTO ml_samples(name,source,features,target) VALUES(?,?,?,?)",
                                 (name, "al_import", json.dumps(feats, ensure_ascii=False), eta))
                ds_rows.append({"name": name, "features": feats, "target": eta})
                # 2) systems：更新或新建体系（含 ε_d 描述符，eta 可选）
                srow = conn.execute("SELECT id,descriptors FROM systems WHERE name=?", (name,)).fetchone()
                if srow:
                    try:
                        descs = json.loads(srow["descriptors"] or "[]")
                    except Exception:
                        descs = []
                    descs = [d for d in descs if d.get("name") not in ("d_band_center", "eta_OER")]
                    descs.append({"name": "d_band_center", "value": ed, "unit": "eV"})
                    if eta is not None:
                        descs.append({"name": "eta_OER", "value": eta, "unit": "V"})
                    conn.execute("UPDATE systems SET descriptors=? WHERE id=?",
                                 (json.dumps(descs, ensure_ascii=False), srow["id"]))
                else:
                    descs = [{"name": "d_band_center", "value": ed, "unit": "eV"}]
                    if eta is not None:
                        descs.append({"name": "eta_OER", "value": eta, "unit": "V"})
                    conn.execute("INSERT INTO systems(name,formula,source,descriptors) VALUES(?,?,?,?)",
                                 (name, name.split("_")[0], "AL 导入", json.dumps(descs, ensure_ascii=False)))
                    n_new += 1
                if eta is not None:
                    n_train += 1
                else:
                    n_cand += 1
            resp = {"imported": len(rows), "train": n_train, "candidates": n_cand,
                    "new_systems": n_new,
                    "note": "含 η_OER 的行进入训练集；仅含 ε_d 的行作为候选集"}
            if detected is not None:
                resp["detected"] = detected
                resp["warnings"] = warnings
            # 按模块落盘为独立数据集文件 AL-XXX.csv（与 ML 数据集物理隔离，便于训练时显式选择）
            if ds_rows:
                try:
                    _p, _fn = save_dataset_file(
                        "al", body.get("dataset_name") or "al_import", ds_rows)
                    if _fn:
                        resp["dataset_file"] = _fn
                except OSError as _e:
                    LOG.warning("AL 数据集落盘失败: %s", _e)
            return self._ok(resp)
        return self._err(405, "方法不支持")

    # ---------------- 图表下载 ----------------
    def _chart(self, method, seg, qs, conn, body):
        """POST /api/chart/download —— 用 matplotlib 生成图表（PNG/SVG），供下载/人工改图。"""
        if method == "POST" and len(seg) >= 2 and seg[1] == "download":
            chart_type = body.get("chart_type", "volcano")
            fmt = (body.get("format") or "png").lower()
            if fmt not in ("png", "svg", "pdf"):
                return self._err(400, "format 需为 png/svg/pdf")
            if chart_type not in ("volcano", "importance", "scatter", "gpr"):
                return self._err(400, "未知图表类型: %s" % chart_type)
            token = time.strftime("%Y%m%d%H%M%S") + "_" + str(os.getpid()) + "_" + str(random.randint(1000, 9999))
            out_path = os.path.join(TMP_DIR, "chart_" + token + "." + fmt)
            cfg = {
                "chart_type": chart_type,
                "title": body.get("title", ""),
                "xlabel": body.get("xlabel", ""),
                "ylabel": body.get("ylabel", ""),
                "x_key": body.get("x_key"),
                "y_key": body.get("y_key"),
                "figsize": body.get("figsize") or [8, 5],
                "dpi": int(body.get("dpi") or 150),
                "format": fmt,
                "out_path": out_path,
                "length_scale": body.get("length_scale"),
                "noise": body.get("noise"),
            }
            cfg_path = out_path + ".json"
            try:
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump({k: v for k, v in cfg.items() if v is not None}, f, ensure_ascii=False)
                with _blocking_unlocked():
                    out = subprocess.run([AII_PY, CHART_GEN, DB_PATH, cfg_path],
                                         capture_output=True, text=True, timeout=120)
                if out.returncode != 0:
                    raise RuntimeError((out.stderr or out.stdout or "外部程序失败")[-1000:])
                res = json.loads(out.stdout.strip().splitlines()[-1] if out.stdout.strip() else "{}")
            except Exception as e:
                return self._err(500, "图表生成失败: %s" % e)
            finally:
                try:
                    os.remove(cfg_path)
                except Exception:
                    pass
            if not res.get("ok"):
                return self._err(400, res.get("error", "图表生成失败"))
            if not os.path.isfile(out_path):
                return self._err(500, "图表未生成")
            with open(out_path, "rb") as f:
                content = f.read()
            mime = {"png": "image/png", "svg": "image/svg+xml", "pdf": "application/pdf"}.get(fmt, "image/png")
            fname = ("chart_%s.%s" % (chart_type, fmt))
            return self._send(200, raw=content, ctype=mime,
                              headers={"Content-Disposition": 'attachment; filename="%s"' % fname})
        return self._err(405, "方法不支持")

    # ---------------- 工作流 ----------------
    def _workflow(self, method, seg, qs, conn, body):
        # 节点定义
        if method == "GET" and len(seg) == 2 and seg[1] == "nodes":
            return self._ok({"nodes": WORKFLOW_NODES})
        # Aiida 输入文件预览（展示生成的 CP2K JSON 输入 + 计算资源建议）：POST /api/workflow/preview
        if method == "POST" and len(seg) == 2 and seg[1] == "preview":
            sid = body.get("system_id")
            if not sid:
                return self._err(400, "缺少 system_id")
            try:
                sid = int(sid)
            except (TypeError, ValueError):
                return self._err(400, "system_id 需为整数")
            data, err = build_workflow_preview(
                conn, sid,
                scf_method=body.get("scf_method", "diag"),
                dispersion=body.get("dispersion", "d3bj"),
                smearing=bool(body.get("smearing", True)),
                periodic=str(body.get("periodic") or "XY"),
                sp_requests=body.get("sp_requests") or [],
                resources=body.get("resources"),
                max_wallclock=body.get("max_wallclock_seconds"),
                label=body.get("label"))
            if err:
                return self._err(400, err)
            return self._ok(data)
        # 人工确认后的 Aiida 提交：POST /api/workflow/submit
        # {system_id, name, calc_type, parameters(编辑后), resources, max_wallclock_seconds}
        if method == "POST" and len(seg) == 2 and seg[1] == "submit":
            sid = body.get("system_id")
            if not sid:
                return self._err(400, "缺少 system_id")
            try:
                sid = int(sid)
            except (TypeError, ValueError):
                return self._err(400, "system_id 需为整数")
            params_raw = body.get("parameters")
            if isinstance(params_raw, str):
                try:
                    params_raw = json.loads(params_raw)
                except Exception:
                    return self._err(400, "parameters JSON 解析失败")
            if not isinstance(params_raw, dict) or not params_raw:
                return self._err(400, "parameters 需为非空 JSON 字典（Aiida CP2K 输入文件）")
            params = sanitize_dict(params_raw)
            data, err = submit_verdi_calc(
                conn, body.get("name") or ("cp2k_" + str(body.get("calc_type", "calc")).lower()),
                sid, body.get("calc_type", "GEO_OPT"),
                resources=body.get("resources"), max_wallclock=body.get("max_wallclock_seconds"),
                parameters=params)
            if err:
                return self._err(502, err)
            return self._ok(data)
        # 启动工作流（统一入口，支持 start_node 断点续跑）
        if method == "POST" and len(seg) == 2 and seg[1] == "start":
            me = getattr(self, "auth_user", None)
            data, err = create_workflow(conn, body,
                                        owner_id=me["id"] if me is not None else None)
            if err:
                return self._err(400, err)
            return self._ok(data, workflow_id=data["workflow_id"])
        # 列表
        if method == "GET" and len(seg) == 1:
            rows = conn.execute("SELECT * FROM workflows ORDER BY id DESC LIMIT 100").fetchall()
            out = []
            for w in rows:
                d = dict(w)
                for k in ("params", "log"):
                    if isinstance(d.get(k), str):
                        try:
                            d[k] = json.loads(d[k])
                        except Exception:
                            pass
                out.append(d)
            return self._ok(_owner_view(conn, out), total=len(out))
        # 详情
        if method == "GET" and len(seg) == 2 and seg[1].isdigit():
            w = conn.execute("SELECT * FROM workflows WHERE id=?", (seg[1],)).fetchone()
            if not w:
                return self._err(404, "工作流不存在")
            d = dict(w)
            for k in ("params", "log"):
                if isinstance(d.get(k), str):
                    try:
                        d[k] = json.loads(d[k])
                    except Exception:
                        pass
            nodes = conn.execute("SELECT * FROM workflow_nodes WHERE workflow_id=? ORDER BY seq", (seg[1],)).fetchall()
            nl = []
            for nd in nodes:
                x = dict(nd)
                for k in ("input_artifacts", "log"):
                    if isinstance(x.get(k), str):
                        try:
                            x[k] = json.loads(x[k])
                        except Exception:
                            pass
                nl.append(x)
            d["nodes"] = nl
            # 关联计算任务（工作流名 + 节点后缀精确匹配，供「停止/监控」使用）
            wname = d.get("name") or "workflow"
            tnames = tuple("%s_%s" % (wname, k) for k in NODE_KEYS)
            ph = ",".join("?" * len(tnames))
            tasks = conn.execute(
                "SELECT id,name,calc_type,status,progress,aiiada_pk,error,log,created_at FROM tasks "
                "WHERE name IN (%s) ORDER BY id DESC LIMIT 20" % ph, tnames).fetchall()
            d["tasks"] = [dict(t) for t in tasks]
            return self._ok(d)
        # 删除工作流（级联删除节点）：DELETE /api/workflow/{id}
        if method == "DELETE" and len(seg) == 2 and seg[1].isdigit():
            wid = seg[1]
            cur = conn.execute("DELETE FROM workflows WHERE id=?", (wid,))
            if cur.rowcount == 0:
                return self._err(404, "工作流不存在")
            conn.execute("DELETE FROM workflow_nodes WHERE workflow_id=?", (wid,))
            return self._ok({"deleted": True, "id": wid, "nodes_cleared": True})
        return self._err(405, "方法不支持")

    # ---------------- 高通量筛选（批量结构生成 → AiiDA 库 → 批量工作流） ----------------
    def _hts(self, method, seg, qs, conn, body):
        # ① 批量生成结构：ASE 批量 → systems 库 + AiiDA StructureData 库
        #    POST /api/hts/gen-structures
        #    body: {elements:[...], a_min,a_max,a_step(或 a_list), face, layers, vacuum, n, m,
        #           adsorbates:[...], alloy, alloy_frac, include_ads:bool}
        if method == "POST" and len(seg) == 2 and seg[1] == "gen-structures":
            elems = [str(e).strip() for e in (body.get("elements") or []) if str(e).strip()]
            if not elems:
                elems = [str(body.get("element") or "Ir")]
            # 两种元素随机混杂（条件1/2）：mix_elements=[A,B]、比例 mix_ratio 可调、
            # 随机种子 mix_seed 可调、mix_seeds 多种子并行批量
            mix_els = [str(x).strip() for x in (body.get("mix_elements") or []) if str(x).strip()]
            if isinstance(body.get("mix_elements"), str):
                mix_els = [x.strip() for x in str(body["mix_elements"]).replace("，", ",").split(",") if x.strip()]
            mix_ratio = float(body.get("mix_ratio") if body.get("mix_ratio") not in (None, "") else 0.5)
            mix_seeds = body.get("mix_seeds") or []
            if isinstance(mix_seeds, str):
                mix_seeds = [x.strip() for x in str(mix_seeds).replace("，", ",").split(",") if x.strip()]
            mix_seeds = [int(s) for s in mix_seeds if str(s).strip().lstrip("-").isdigit()] or [42]
            a_list = body.get("a_list") or []
            if a_list:
                a_list = [float(x) for x in a_list if str(x).strip()]
            else:
                a_min = float(body.get("a_min") or 3.8)
                a_max = float(body.get("a_max") or a_min)
                a_step = float(body.get("a_step") or 0.05)
                a_list = [a_min]
                while a_list[-1] + a_step <= a_max + 1e-9:
                    a_list.append(round(a_list[-1] + a_step, 6))
            ads = [str(a).strip() for a in (body.get("adsorbates") or []) if str(a).strip()]
            face = body.get("face", "fcc111")
            layers = int(body.get("layers") or 4)
            vacuum = float(body.get("vacuum") if body.get("vacuum") not in (None, "") else 15.0)
            n = int(body.get("n") or 3)
            m = int(body.get("m") or 3)
            alloy = body.get("alloy") or None
            alloy_frac = float(body.get("alloy_frac") or 0)
            prefix = (body.get("prefix") or "HTS").strip()
            total_combos = len(elems) * len(a_list) * len(mix_seeds) * max(1, len(ads))
            if total_combos > 200:
                return self._err(400, "组合数过多（≤200）：%d×%d×%d×%d" % (
                    len(elems), len(a_list), len(mix_seeds), max(1, len(ads))))
            structs = []
            seq = 0
            for el in elems:
                for a in a_list:
                    for sd in mix_seeds:
                        base = {"element": el, "a": a, "face": face, "layers": layers,
                                "vacuum": vacuum, "n": n, "m": m,
                                "alloy": alloy, "alloy_frac": alloy_frac}
                        if mix_els:
                            base["mix_elements"] = [el, mix_els[0]]
                            base["mix_ratio"] = mix_ratio
                            base["mix_seed"] = sd
                        mix_tag = ("_mix%s_%.2f_s%d" % (mix_els[0], mix_ratio, sd)) if mix_els else ""
                        if ads:
                            for ad in ads:
                                seq += 1
                                p = dict(base, adsorbate=ad)
                                structs.append((seq, p, "%s_%s%s_a%.3f%s_%s" % (prefix, el, ("_"+alloy if alloy else ""), a, mix_tag, ad)))
                        else:
                            seq += 1
                            structs.append((seq, base, "%s_%s%s_a%.3f%s" % (prefix, el, ("_"+alloy if alloy else ""), a, mix_tag)))
            created = []
            store_items = []   # 写入 AiiDA 的结构（JSON 数组）
            for seq, p, label in structs:
                try:
                    r = build_structure_ase(p)
                except ApiError as e:
                    store_items.append({"err": label, "msg": str(e.msg)})
                    continue
                # 写 systems 库（同一生成结构多次运行会重复建体系；用 name 前缀区分批次）
                cur = conn.execute(
                    "INSERT INTO systems(name,formula,composition,space_group,lattice,elements,source,structure) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (label, r["formula"], r["formula"], face,
                     "a=%.3f vacuum=%.1f" % (float(p["a"]), p["vacuum"]),
                     json.dumps(sorted(set(r["symbols"]))), "HTS批量", json.dumps(r)))
                sid = cur.lastrowid
                store_items.append({
                    "label": label, "symbols": r["symbols"], "cell": r["cell"],
                    "scaled_positions": r["scaled_positions"],
                    "pbc": r.get("pbc", [True, True, True]), "description": "HTS %s" % label,
                })
                created.append({"system_id": sid, "label": label, "formula": r["formula"],
                                "natoms": r["natoms"], "element": el, "a": float(p["a"])})
            # 批量存入 AiiDA StructureData 库（便于后续逐个引用）
            aiida_map = {}
            if created and os.path.isfile(STORE_STRUCTURES_SCRIPT):
                jp = os.path.join(TMP_DIR, "hts_structs_%s.json" % now_str().replace(" ", "_").replace(":", ""))
                with open(jp, "w", encoding="utf-8") as f:
                    json.dump(store_items, f, ensure_ascii=False)
                env = dict(os.environ)
                env.setdefault("AIIDA_PATH", os.path.expanduser("~/.aiida"))
                try:
                    with _blocking_unlocked():
                        proc = subprocess.run([VERDI, "run", STORE_STRUCTURES_SCRIPT, jp],
                                              capture_output=True, text=True, timeout=600, env=env)
                    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
                    pks = {}
                    for ln in (proc.stdout or "").splitlines():
                        mm = re.match(r"STRUCT_PK=(\d+)\s+(.+)", ln)
                        if mm:
                            pks[mm.group(2)] = int(mm.group(1))
                    for c in created:
                        c["aiida_pk"] = pks.get(c["label"])
                    if not pks:
                        c = created[0]
                        c["aiida_pk"] = None
                        c["store_note"] = "AiiDA 存储失败: " + out[-200:]
                except Exception as e:
                    for c in created:
                        c["aiida_pk"] = None
                    created[0]["store_note"] = "AiiDA 存储调用失败: %s" % e
            else:
                for c in created:
                    c["aiida_pk"] = None
            conn.commit()
            return self._ok({"count": len(created), "structures": created})

        # ② 批量提交工作流：对每个结构按 steps 依次提交 AiiDA Cp2kCalculation
        #    POST /api/hts/run
        #    body: {name, structures:[{system_id, aiida_pk}], steps:["geo_opt","energy","pdos"],
        #           calc_type, scf_method, dispersion, smearing, resources, max_wallclock_seconds, output_dir}
        if method == "POST" and len(seg) == 2 and seg[1] == "run":
            name = (body.get("name") or "hts_batch").strip()[:80]
            structs = body.get("structures") or []
            if not structs:
                return self._err(400, "缺少 structures（请先生成或选择结构）")
            steps = [s.lower() for s in (body.get("steps") or ["geo_opt"])
                     if s.lower() in ("geo_opt", "energy", "pdos", "bader", "band_center")]
            if not steps:
                return self._err(400, "steps 需包含 geo_opt/energy/pdos/bader/band_center 之一")
            # 计算步骤（用于确定主计算类型）；分析步骤作为后处理标记
            calc_steps = [s for s in steps if s in ("geo_opt", "energy", "pdos")] or ["geo_opt"]
            do_charge = ("bader" in steps)          # Bader 电荷：需输出 E_DENSITY_CUBE
            do_pdos = ("band_center" in steps) or ("pdos" in steps)   # d 带中心需 PDOS
            resources = body.get("resources") or {}
            try:
                resources = {"num_machines": int(resources.get("num_machines", 1)),
                             "num_mpiprocs_per_machine": int(resources.get("num_mpiprocs_per_machine", DEFAULT_NPROC))}
            except (TypeError, ValueError):
                resources = {"num_machines": 1, "num_mpiprocs_per_machine": DEFAULT_NPROC}
            output_dir = str(body.get("output_dir") or "").strip()
            params_json = {
                "steps": steps, "scf_method": body.get("scf_method", "diag"),
                "dispersion": body.get("dispersion", "d3bj"),
                "smearing": bool(body.get("smearing", True)),
                "charge_density": bool(body.get("charge_density", False)),
                "periodic": str(body.get("periodic") or "XYZ"),
                "analyze": {"bader": do_charge, "band_center": ("band_center" in steps)},
                "cutoff": body.get("cutoff"), "rel_cutoff": body.get("rel_cutoff"),
                "resources": resources, "output_dir": output_dir,
                "max_wallclock_seconds": int(body.get("max_wallclock_seconds") or 12 * 3600),
            }
            me = getattr(self, "auth_user", None)
            owner_id = me["id"] if me is not None else None
            cur = conn.execute(
                "INSERT INTO hts_batches(name,status,steps,output_dir,params,log,owner_id) "
                "VALUES(?,?,?,?,?,?,?)",
                (name, "running", json.dumps(steps, ensure_ascii=False), output_dir,
                 json.dumps(params_json, ensure_ascii=False),
                 json.dumps([{"ts": now_str(), "level": "info", "msg": "批量工作流启动：%d 个结构 × %s" % (len(structs), steps)}], ensure_ascii=False),
                 owner_id or _system_owner_id(conn)))
            bid = cur.lastrowid
            # ---- 链式工作流模式（A 逐结构 / B 波前）----
            # 默认 A（逐结构 geo→energy→post 链式，wall-clock 最短、故障隔离好）。
            # mode="legacy" 保留旧的单阶段逐结构提交（仅提交第一计算步骤）。
            mode = (body.get("mode") or "A").upper()
            if mode in ("A", "B"):
                wf, werr = submit_catmat_workchain(
                    conn, name, structs, mode=mode,
                    scf_method=params_json["scf_method"], dispersion=params_json["dispersion"],
                    smearing=params_json["smearing"], resources=resources,
                    max_wallclock=params_json["max_wallclock_seconds"],
                    periodic=params_json["periodic"], do_pdos=do_pdos, do_bader=do_charge,
                    band_center=("band_center" in steps))
                if werr:
                    conn.execute("UPDATE hts_batches SET status='failed', log=? WHERE id=?",
                                 (json.dumps([{"ts": now_str(), "level": "err", "msg": werr}], ensure_ascii=False), bid))
                    conn.commit()
                    if getattr(werr, "decision", None):
                        return self._governor_err(werr)
                    return self._err(500, "工作流提交失败: " + werr)
                wpk = wf["pk"]
                conn.execute(
                    "INSERT INTO tasks(name,system_id,calc_type,input_file,resources,status,progress,log,aiiada_pk) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    ("%s_[%s]" % (name, mode)[:100], None, "WORKFLOW_%s" % mode, None,
                     json.dumps({"num_machines": resources["num_machines"],
                                 "num_mpiprocs_per_machine": resources["num_mpiprocs_per_machine"],
                                 "output_dir": output_dir}, ensure_ascii=False),
                     "running", 0,
                     json.dumps([{"ts": now_str(), "level": "ok",
                                  "msg": "已提交 CatMat 工作链 %s（PK %s，%d 个结构，步骤 %s）" % (wf["workchain"], wpk, wf["num_structures"], steps)}], ensure_ascii=False),
                     str(wpk)))
                items_out = []
                for i, s in enumerate(structs):
                    label = s.get("label") or ("item%d" % (i + 1))
                    conn.execute(
                        "INSERT INTO hts_batch_items(batch_id,seq,label,system_id,aiida_struct_pk,calc_type,status,aiida_pk) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (bid, i + 1, label, int(s.get("system_id") or 0) or None,
                         int(s.get("aiida_pk")) if s.get("aiida_pk") else None,
                         "WORKFLOW", "running", wpk))
                    items_out.append({"label": label, "workchain_pk": wpk, "mode": mode})
                conn.commit()
                return self._ok({"batch_id": bid, "name": name, "mode": mode,
                                 "workchain": wf["workchain"], "workchain_pk": wpk,
                                 "submitted": len(items_out), "items": items_out,
                                 "note": "A=逐结构链式 geo→energy→post（推荐）；B=波前并行（先全 GEO_OPT 再全 SP，支持几何预筛选）；legacy=旧单阶段"},
                                batch_id=bid)
            # ---- 旧逻辑：legacy 模式逐结构提交第一计算步骤 ----
            ok_items, fail_items = [], []
            for i, s in enumerate(structs):
                sid = int(s.get("system_id") or 0)
                spk = s.get("aiida_pk")
                label = s.get("label") or ("item%d" % (i + 1))
                it = conn.execute(
                    "INSERT INTO hts_batch_items(batch_id,seq,label,system_id,aiida_struct_pk,calc_type,status) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (bid, i + 1, label, sid if sid else None, int(spk) if spk else None,
                     ",".join(s.upper() for s in steps), "pending"))
                item_id = it.lastrowid
                # 提交主计算（第一计算步骤）；bader/band_center 作为输出与后处理标记
                main_ct = calc_steps[0].upper()
                # structure_pk 模式需从 systems 库补元素（生成 &KIND 段用）
                elms = None
                if sid:
                    try:
                        srow = conn.execute("SELECT structure FROM systems WHERE id=?", (sid,)).fetchone()
                        if srow and srow["structure"]:
                            stj = json.loads(srow["structure"])
                            if stj.get("symbols"):
                                elms = list(set(stj["symbols"]))
                    except Exception:
                        pass
                data, err = submit_verdi_calc(
                    conn, "%s_%s" % (name, label)[:100], sid if sid else None, main_ct,
                    scf_method=params_json["scf_method"], dispersion=params_json["dispersion"],
                    smearing=params_json["smearing"], resources=resources,
                    max_wallclock=params_json["max_wallclock_seconds"],
                    parameters=None, elements=elms, structure_pk=spk,
                    pdos=do_pdos,
                    charge_density=(do_charge or params_json["charge_density"]),
                    band_center=("band_center" in steps), periodic=params_json["periodic"],
                    cutoff=params_json.get("cutoff"), rel_cutoff=params_json.get("rel_cutoff"))
                if err:
                    conn.execute("UPDATE hts_batch_items SET status='failed',error=? WHERE id=?", (err[:300], item_id))
                    fail_items.append({"label": label, "error": err})
                else:
                    conn.execute(
                        "UPDATE hts_batch_items SET aiida_pk=?,status='running' WHERE id=?",
                        (data["pk"], item_id))
                    conn.execute(
                        "INSERT INTO tasks(name,system_id,calc_type,input_file,resources,status,progress,log,aiiada_pk) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        ("%s_%s" % (name, label)[:100], sid if sid else None, main_ct, None,
                         json.dumps({"num_machines": resources["num_machines"],
                                     "num_mpiprocs_per_machine": resources["num_mpiprocs_per_machine"],
                                     "output_dir": output_dir}, ensure_ascii=False),
                         "running", 0,
                         json.dumps([{"ts": now_str(), "level": "ok",
                                      "msg": "HTS 批量提交 AiiDA PK=%s（步骤 %s）" % (data["pk"], steps)}], ensure_ascii=False),
                         str(data["pk"])))
                    ok_items.append({"label": label, "aiida_pk": data["pk"]})
            conn.commit()
            return self._ok({"batch_id": bid, "name": name, "submitted": len(ok_items),
                             "failed": len(fail_items), "items": ok_items, "failures": fail_items},
                            batch_id=bid)

        # ③ 批次列表：GET /api/hts/batches
        if method == "GET" and len(seg) == 2 and seg[1] == "batches":
            rows = conn.execute("SELECT * FROM hts_batches ORDER BY id DESC LIMIT 50").fetchall()
            out = []
            for b in rows:
                d = dict(b)
                for k in ("steps", "params", "log"):
                    if isinstance(d.get(k), str):
                        try:
                            d[k] = json.loads(d[k])
                        except Exception:
                            pass
                d["items"] = conn.execute("SELECT COUNT(*) n FROM hts_batch_items WHERE batch_id=?", (b["id"],)).fetchone()["n"]
                d["running_items"] = conn.execute("SELECT COUNT(*) n FROM hts_batch_items WHERE batch_id=? AND status IN ('running','pending')", (b["id"],)).fetchone()["n"]
                d["failed_items"] = conn.execute("SELECT COUNT(*) n FROM hts_batch_items WHERE batch_id=? AND status='failed'", (b["id"],)).fetchone()["n"]
                out.append(d)
            return self._ok(_owner_view(conn, out))

        # ④ 批次详情：GET /api/hts/batch/{id}（含每项实时状态，从 tasks 表同步）
        if method == "GET" and len(seg) == 3 and seg[1] == "batch" and seg[2].isdigit():
            b = conn.execute("SELECT * FROM hts_batches WHERE id=?", (seg[2],)).fetchone()
            if not b:
                return self._err(404, "批次不存在")
            d = dict(b)
            for k in ("steps", "params", "log"):
                if isinstance(d.get(k), str):
                    try:
                        d[k] = json.loads(d[k])
                    except Exception:
                        pass
            items = conn.execute("SELECT * FROM hts_batch_items WHERE batch_id=? ORDER BY seq", (seg[2],)).fetchall()
            il = []
            for it in items:
                x = dict(it)
                # 从 tasks 表同步实时状态（AiiDA PK 对应任务）
                if x.get("aiida_pk"):
                    t = conn.execute("SELECT status,progress,error FROM tasks WHERE aiiada_pk=?", (str(x["aiida_pk"]),)).fetchone()
                    if t:
                        x["status"] = t["status"]
                        if t["error"]:
                            x["error"] = t["error"]
                # WORKFLOW 项：直接查工作链真实状态（更准，含 done/failed）
                if (x.get("calc_type") or "").startswith("WORKFLOW") and x.get("aiida_pk"):
                    st = query_aiida_states([x["aiida_pk"]])
                    info = st.get(str(x["aiida_pk"])) or st.get(x["aiida_pk"])
                    if info:
                        x["status"] = map_aiida_state(info)
                        x["workchain_state"] = info.get("state")
                        x["exit_status"] = info.get("exit_status")
                il.append(x)
            d["items"] = il
            return self._ok(d)

        # ⑤ 终止批次：POST /api/hts/batch/{id}/stop（verdi process kill 全部运行中项）
        if method == "POST" and len(seg) == 4 and seg[1] == "batch" and seg[2].isdigit() and seg[3] == "stop":
            b = conn.execute("SELECT name FROM hts_batches WHERE id=?", (seg[2],)).fetchone()
            if not b:
                return self._err(404, "批次不存在")
            items = conn.execute("SELECT id,aiida_pk FROM hts_batch_items WHERE batch_id=? AND aiida_pk IS NOT NULL", (seg[2],)).fetchall()
            killed = 0
            for it in items:
                ok, _ = kill_aiida_process(it["aiida_pk"])
                if ok:
                    killed += 1
                conn.execute("UPDATE hts_batch_items SET status='stopped' WHERE id=?", (it["id"],))
            conn.execute("UPDATE hts_batches SET status='stopped',finished_at=datetime('now','localtime') WHERE id=?", (seg[2],))
            conn.commit()
            return self._ok({"killed": killed, "total": len(items)})

        # ⑥ 删除批次（仅允许 stopped/failed/完成）：DELETE /api/hts/batch/{id}
        if method == "DELETE" and len(seg) == 3 and seg[1] == "batch" and seg[2].isdigit():
            b = conn.execute("SELECT status FROM hts_batches WHERE id=?", (seg[2],)).fetchone()
            if not b:
                return self._err(404, "批次不存在")
            if b["status"] in ("running", "pending"):
                return self._err(400, "批次仍在运行，请先终止（POST /api/hts/batch/%s/stop）" % seg[2])
            conn.execute("DELETE FROM hts_batch_items WHERE batch_id=?", (seg[2],))
            conn.execute("DELETE FROM hts_batches WHERE id=?", (seg[2],))
            return self._ok({"deleted": True, "id": int(seg[2])})

        # ⑦ 收集结果 → 数据集文件（保存到提交时指定 output_dir）
        #    POST /api/hts/batch/{id}/collect
        if method == "POST" and len(seg) == 4 and seg[1] == "batch" and seg[2].isdigit() and seg[3] == "collect":
            b = conn.execute("SELECT name,output_dir,steps FROM hts_batches WHERE id=?", (seg[2],)).fetchone()
            if not b:
                return self._err(404, "批次不存在")
            items = conn.execute("SELECT * FROM hts_batch_items WHERE batch_id=? ORDER BY seq", (seg[2],)).fetchall()
            # 预取 WORKFLOW 工作链的 results（按工作链 PK 聚合，避免重复 verdi 调用）
            wf_results_by_pk, wf_pks = {}, []
            for it in items:
                if (it["calc_type"] or "").startswith("WORKFLOW") and it["aiida_pk"] and it["aiida_pk"] not in wf_pks:
                    wf_pks.append(it["aiida_pk"])
            for wpk in wf_pks:
                try:
                    res, rerr = load_workflow_results(wpk)
                    if res:
                        wf_results_by_pk[wpk] = {r.get("label"): r for r in res if isinstance(r, dict)}
                except Exception:
                    pass
            rows = []
            for it in items:
                label = it["label"]
                st = ""
                if it["aiida_pk"]:
                    t = conn.execute("SELECT status FROM tasks WHERE aiiada_pk=?", (str(it["aiida_pk"]),)).fetchone()
                    st = t["status"] if t else "?"
                energy = it["energy"]
                if (it["calc_type"] or "").startswith("WORKFLOW") and it["aiida_pk"] in wf_results_by_pk:
                    r = wf_results_by_pk[it["aiida_pk"]].get(label)
                    if isinstance(r, dict) and r.get("energy") is not None and energy is None:
                        energy = r.get("energy")
                        conn.execute("UPDATE hts_batch_items SET energy=? WHERE id=?", (energy, it["id"]))
                rows.append({"name": label, "calc_type": it["calc_type"], "aiida_pk": it["aiida_pk"],
                             "status": it["status"] or st, "energy": energy, "error": it["error"]})
            if wf_pks:
                conn.commit()
            # 写入数据集 CSV（统一保存到任务指定路径）
            out_dir = b["output_dir"] or os.path.join(CP2K_RUN_DIR, "hts_collect")
            try:
                resolved, derr = resolve_run_dir(out_dir)
                if derr:
                    resolved = os.path.join(CP2K_RUN_DIR, "hts_collect")
                os.makedirs(resolved, exist_ok=True)
            except Exception:
                resolved = os.path.join(CP2K_RUN_DIR, "hts_collect")
                os.makedirs(resolved, exist_ok=True)
            csv_path = os.path.join(resolved, "%s_dataset.csv" % re.sub(r"[^A-Za-z0-9_.-]", "_", b["name"] or "hts"))
            with open(csv_path, "w", encoding="utf-8-sig") as f:
                f.write("name,calc_type,aiida_pk,status,energy,error\n")
                for r in rows:
                    f.write("%s,%s,%s,%s,%s,%s\n" % (
                        r["name"], r["calc_type"], r["aiida_pk"], r["status"],
                        r["energy"] if r["energy"] is not None else "", (r["error"] or "").replace(",", ";")))
            return self._ok({"csv": csv_path, "rows": len(rows), "items": rows})

        # ⑧ 配位环境位点分析（条件3/4/5）：PK 汇总 → 重新导入 ASE → 位点识别 + 编码 + PBC 校准
        #    POST /api/hts/analyze-sites
        #    body: {structure_pk 或 pk_list:[...], a?(最近邻基准晶格常数)}
        if method == "POST" and len(seg) == 2 and seg[1] == "analyze-sites":
            pks = body.get("pk_list") or []
            if body.get("structure_pk"):
                pks = [body["structure_pk"]]
            pks = [int(p) for p in pks if str(p).strip().lstrip("-").isdigit()]
            if not pks:
                return self._err(400, "需提供 structure_pk 或 pk_list（AiiDA 库中优化后的结构 PK）")
            if len(pks) > 60:
                return self._err(400, "单次分析结构数 ≤60（当前 %d）" % len(pks))
            results = []
            for pk in pks:
                st, err = load_aiida_structure_json(pk)
                if err:
                    results.append({"pk": pk, "error": err})
                    continue
                atoms = ase_atoms_from_dict(st)
                try:
                    sites, pairs = ase_analyze_sites(atoms, a=body.get("a"))
                except Exception as e:
                    results.append({"pk": pk, "label": st.get("label"), "error": "位点分析失败: %s" % e})
                    continue
                # 新配位环境编码自动写入知识库（随结构变化持续更新）
                for s in sites:
                    upsert_env_code(conn, s["env"])
                results.append({
                    "pk": pk, "label": st.get("label"), "natoms": len(st["symbols"]),
                    "n_sites": len(sites), "n_pairs": len(pairs),
                    "sites": sites, "pairs": pairs,
                })
            return self._ok({"count": len(results), "results": results})

        # ⑨ 按位点放置吸附并入库/提交（条件3/4/5 落库执行）
        #    POST /api/hts/ads-submit
        #    body: {structure_pk, site_idx, adsorbate, height?, calc_type?, charge_density?,
        #           periodic?, resources?, output_dir?, submit:bool(默认true)}
        if method == "POST" and len(seg) == 2 and seg[1] == "ads-submit":
            pk = body.get("structure_pk")
            try:
                pk = int(pk)
            except (TypeError, ValueError):
                return self._err(400, "structure_pk 需为 AiiDA 库结构 PK")
            site_idx = int(body.get("site_idx") if str(body.get("site_idx") or "").strip().lstrip("-").isdigit() else -1)
            adsorbate = str(body.get("adsorbate") or "O").strip()
            height = float(body.get("height") if body.get("height") not in (None, "") else 1.5)
            st, err = load_aiida_structure_json(pk)
            if err:
                return self._err(502, err)
            atoms = ase_atoms_from_dict(st)
            try:
                sites, pairs = ase_analyze_sites(atoms, a=body.get("a"))
            except Exception as e:
                return self._err(500, "位点分析失败: %s" % e)
            if not sites:
                return self._err(400, "该结构未识别到表面吸附位点")
            if site_idx < 0 or site_idx >= len(sites):
                return self._err(400, "site_idx 超出范围（0~%d）" % (len(sites) - 1))
            target = sites[site_idx]
            try:
                place_adsorbate(atoms, adsorbate, height=height, site="ontop",
                                position=[target["pos"][0], target["pos"][1]])
            except Exception as e:
                return self._err(400, "吸附放置失败: %s" % e)
            atoms.wrap()
            label = "%s_%s_s%d_%s" % (st.get("label") or ("pk%d" % pk), target["env"].replace(":", "_"), site_idx, adsorbate)
            # 写 systems 库
            sd = {
                "symbols": atoms.get_chemical_symbols(),
                "cell": [[float(round(x, 6)) for x in v] for v in atoms.get_cell()],
                "scaled_positions": [[float(round(x, 6)) for x in sp] for sp in atoms.get_scaled_positions()],
                "pbc": [bool(x) for x in atoms.get_pbc()],
            }
            cur = conn.execute(
                "INSERT INTO systems(name,formula,composition,space_group,lattice,elements,source,structure) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (label, atoms.get_chemical_formula(), atoms.get_chemical_formula(),
                 st.get("label") or "", "site=%s env=%s" % (site_idx, target["env"]),
                 json.dumps(sorted(set(atoms.get_chemical_symbols()))), "HTS吸附", json.dumps(sd)))
            sid = cur.lastrowid
            ret = {"system_id": sid, "label": label, "site": target, "adsorbate": adsorbate,
                   "pbc_calibrated": True}
            # 可选提交 CP2K 计算（电荷密度/单点能等）
            if body.get("submit", True):
                resources = body.get("resources") or {}
                try:
                    resources = {"num_machines": int(resources.get("num_machines", 1)),
                                 "num_mpiprocs_per_machine": int(resources.get("num_mpiprocs_per_machine", DEFAULT_NPROC))}
                except (TypeError, ValueError):
                    resources = {"num_machines": 1, "num_mpiprocs_per_machine": DEFAULT_NPROC}
                ct = str(body.get("calc_type") or "ENERGY").upper()
                data, derr = submit_verdi_calc(
                    conn, label[:100], sid, ct,
                    scf_method=body.get("scf_method", "diag"),
                    dispersion=body.get("dispersion", "d3bj"),
                    smearing=bool(body.get("smearing", True)),
                    resources=resources,
                    max_wallclock=body.get("max_wallclock_seconds"),
                    charge_density=bool(body.get("charge_density", False)),
                    periodic=str(body.get("periodic") or "XYZ"),
                    cutoff=body.get("cutoff"), rel_cutoff=body.get("rel_cutoff"))
                if derr:
                    ret["submit_error"] = derr
                else:
                    ret["aiida_pk"] = data["pk"]
                    conn.execute(
                        "INSERT INTO tasks(name,system_id,calc_type,input_file,resources,status,progress,log,aiiada_pk) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (label[:100], sid, ct, None,
                         json.dumps(resources, ensure_ascii=False), "running", 0,
                         json.dumps([{"ts": now_str(), "level": "ok",
                                      "msg": "吸附位点计算已提交 AiiDA PK=%s（位点 %d %s）" % (data["pk"], site_idx, target["env"])}],
                                    ensure_ascii=False), str(data["pk"])))
            conn.commit()
            return self._ok(ret, created_id=sid)

        # ⑪ AI 辅助定制生成：由需求生成 ASE 批量生成代码
        #    POST /api/hts/ai-gen-code {requirement, ...}
        if method == "POST" and len(seg) == 2 and seg[1] == "ai-gen-code":
            req = str(body.get("requirement") or "").strip()
            if not req:
                return self._err(400, "请输入结构生成需求描述")
            cfg = load_llm_config()
            if not (cfg.get("base_url") and cfg.get("model") and cfg.get("api_key")):
                return self._err(400, "未配置大模型 API，无法生成代码（请在「平台设置 → 大模型」中配置）")
            prompt = (
                "你是材料结构生成专家（ASE 库）。用户要用 ASE 批量生成催化表面/吸附结构。\n"
                "请**只输出 Python 代码**（不要解释文字、不要 markdown 代码块标记），严格满足：\n"
                "1. 定义函数 `build_structures(count=3)`，返回 ASE Atoms 列表（长度 = count）；\n"
                "2. 只允许使用：ase（fcc111/fcc100/fcc110/hcp0001/add_adsorbate/bulk/molecule/surface 等）、numpy、random；\n"
                "   禁止 os/subprocess/socket/网络/文件读写/eval/exec；\n"
                "3. 每个 Atoms 必须设置 cell 与 pbc=[True,True,True]，z 方向留真空层；\n"
                "4. 结构多样性：按 count 变化成分比例、晶格常数、超胞尺寸、吸附位点或随机种子；\n"
                "5. 结构标签放入 `atoms.info['label']`（便于识别）。\n\n"
                "参考示例（可按需改写）：\n"
                "```python\n"
                "import random\n"
                "from ase.build import fcc111, add_adsorbate\n\n"
                "def build_structures(count=3):\n"
                "    out = []\n"
                "    for i in range(count):\n"
                "        slab = fcc111('Ir', size=(3, 3, 4), a=3.88, vacuum=15.0)\n"
                "        rng = random.Random(42 + i)\n"
                "        for at in slab:\n"
                "            if rng.random() < 0.3 * (i + 1) / count:\n"
                "                at.symbol = 'Ru'\n"
                "        add_adsorbate(slab, 'O', height=1.8, position='ontop')\n"
                "        slab.info['label'] = 'IrRu_a3.88_O_s%d' % i\n"
                "        out.append(slab)\n"
                "    return out\n"
                "```\n\n"
                "用户需求：%s" % req[:2000]
            )
            content, err = llm_call([{"role": "user", "content": prompt}], cfg, temperature=0.3)
            if err:
                return self._err(502, err)
            code = (content or "").strip()
            # 去掉 markdown 代码块围栏
            m = re.search(r"```(?:python)?\s*([\s\S]*?)```", code)
            if m:
                code = m.group(1).strip()
            if "def build_structures" not in code:
                return self._err(500, "AI 生成的代码缺少 build_structures 函数，请调整需求后重试", raw=code[:800])
            return self._ok({"code": code})

        # ⑫ 运行 AI 生成代码 → 预览结构（2~3 个，供用户判断后再批量执行）
        #    POST /api/hts/ai-run-preview {code, preview_count}
        if method == "POST" and len(seg) == 2 and seg[1] == "ai-run-preview":
            code = str(body.get("code") or "").strip()
            if not code:
                return self._err(400, "缺少代码")
            if "def build_structures" not in code:
                return self._err(400, "代码需定义 build_structures(count=n) 函数")
            n = int(body.get("preview_count") or 3)
            n = max(1, min(3, n))
            data, err = run_ase_user_code(code, n)
            if err:
                return self._err(500, err)
            return self._ok({"count": len(data), "preview": True, "structures": data})

        # ⑬ AI 代码确认后正式批量执行（生成全部结构 → systems 库 + AiiDA 库）
        #    POST /api/hts/ai-run-batch {code, count, prefix}
        if method == "POST" and len(seg) == 2 and seg[1] == "ai-run-batch":
            code = str(body.get("code") or "").strip()
            if not code:
                return self._err(400, "缺少代码")
            if "def build_structures" not in code:
                return self._err(400, "代码需定义 build_structures(count=n) 函数")
            n = int(body.get("count") or 8)
            n = max(1, min(200, n))
            data, err = run_ase_user_code(code, n)
            if err:
                return self._err(500, err)
            prefix = (body.get("prefix") or "AI").strip()
            created = []
            store_items = []
            for i, s in enumerate(data):
                label = "%s_%s" % (prefix, (s.get("label") or ("s%d" % (i + 1))))
                cur = conn.execute(
                    "INSERT INTO systems(name,formula,composition,space_group,lattice,elements,source,structure) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (label, s["formula"], s["formula"], "ASE-AI",
                     "c=%.2f 下表面%.2f 上真空%.2f" % (s["c"], s["zmin"], s["vacuum_top"]),
                     json.dumps(sorted(set(s["symbols"]))), "AI定制",
                     json.dumps({"symbols": s["symbols"], "cell": s["cell"],
                                 "scaled_positions": s["scaled_positions"],
                                 "pbc": s.get("pbc", [True, True, True])})))
                sid = cur.lastrowid
                store_items.append({"label": label, "symbols": s["symbols"], "cell": s["cell"],
                                    "scaled_positions": s["scaled_positions"],
                                    "pbc": s.get("pbc", [True, True, True]),
                                    "description": "AI定制 " + label})
                created.append({"system_id": sid, "label": label, "formula": s["formula"],
                                "natoms": s["natoms"], "c": s["c"], "zmin": s["zmin"],
                                "vacuum_top": s["vacuum_top"]})
            # 批量存入 AiiDA StructureData 库
            if store_items and os.path.isfile(STORE_STRUCTURES_SCRIPT):
                jp = os.path.join(TMP_DIR, "ai_structs_%s.json" % now_str().replace(" ", "_").replace(":", ""))
                with open(jp, "w", encoding="utf-8") as f:
                    json.dump(store_items, f, ensure_ascii=False)
                env = dict(os.environ)
                env.setdefault("AIIDA_PATH", os.path.expanduser("~/.aiida"))
                try:
                    with _blocking_unlocked():
                        proc = subprocess.run([VERDI, "run", STORE_STRUCTURES_SCRIPT, jp],
                                              capture_output=True, text=True, timeout=600, env=env)
                    pks = {}
                    for ln in (proc.stdout or "").splitlines():
                        mm = re.match(r"STRUCT_PK=(\d+)\s+(.*)", ln)
                        if mm:
                            pks[mm.group(2)] = int(mm.group(1))
                    for c in created:
                        c["aiida_pk"] = pks.get(c["label"])
                except Exception as e:
                    for c in created:
                        c["aiida_pk"] = None
            conn.commit()
            return self._ok({"count": len(created), "structures": created})

        # ⑩ 差分电荷密度后处理：delta = total - slab - ads
        #    POST /api/hts/bader-diff        #    body: {total_cube, slab_cube, ads_cube, output_dir?, use_multiwfn?}
        #    三个 cube 来自同一晶胞+同一网格的 CP2K &E_DENSITY_CUBE 输出。
        if method == "POST" and len(seg) == 2 and seg[1] == "bader-diff":
            if body.get("density_unit") != "e/bohr^3":
                return self._err(400, "请显式确认 density_unit=e/bohr^3")
            tc = str(body.get("total_cube") or "").strip()
            sc = str(body.get("slab_cube") or "").strip()
            ac = str(body.get("ads_cube") or "").strip()
            if not (tc and sc and ac):
                return self._err(400, "需提供 total_cube / slab_cube / ads_cube 三个 .cube 文件路径")
            for p in (tc, sc, ac):
                if not os.path.isfile(p):
                    return self._err(400, "cube 文件不存在: " + p)
            out_dir = str(body.get("output_dir") or "").strip()
            if out_dir:
                try:
                    out_dir, derr = resolve_run_dir(out_dir)
                    if derr:
                        out_dir = os.path.join(CP2K_RUN_DIR, "bader_diff")
                except Exception:
                    out_dir = os.path.join(CP2K_RUN_DIR, "bader_diff")
            else:
                out_dir = os.path.join(CP2K_RUN_DIR, "bader_diff")
            try:
                os.makedirs(out_dir, exist_ok=True)
            except Exception:
                out_dir = os.path.join(CP2K_RUN_DIR, "bader_diff")
                os.makedirs(out_dir, exist_ok=True)
            token = secrets.token_hex(12)
            delta_path = os.path.join(out_dir, "delta_%s.cube" % token)
            cmd = [sys.executable, BADER_SCRIPT, tc, sc, ac, delta_path]
            if body.get("use_multiwfn") and os.path.isfile(MULTIWFN_BIN):
                cmd += ["--multiwfn", MULTIWFN_BIN]
            try:
                with _blocking_unlocked():
                    obj = run_json(cmd, timeout=300)
                return self._ok(obj["result"])
            except Exception as exc:
                return self._err(422, "差分电荷处理失败: %s" % exc)
        return self._err(405, "方法不支持")


    # ---------------- Multiwfn CP2K 输入生成 ----------------
    def _multiwfn(self, method, seg, qs, conn, body):
        if method == "GET" and len(seg) == 2 and seg[1] == "presets":
            # 返回各计算类型 + 色散 + 展宽 + SCF 方法 + 「add row」可选参数的预设说明
            glabel = {g["key"]: g["label"] for g in MWF_PARAM_GROUPS}
            return self._ok({
                "calc_types": [{"key": k, "num": v[0], "run_type": v[1], "label": v[2]}
                               for k, v in CALC_TYPES.items()],
                "dispersions": [{"key": k, "num": v[0], "label": v[1]} for k, v in DISPERSIONS.items()],
                "scf_methods": [{"key": k, "label": v[0], "desc": v[1]} for k, v in SCF_METHODS.items()],
                "groups": MWF_PARAM_GROUPS,
                "extra_params": [{"key": k, "label": v["label"], "type": v["type"],
                                  "choices": list(v["choices"].keys()) if v["type"] == "choice" else [],
                                  "default": v.get("default", ""), "hint": v.get("hint", ""),
                                  "group": v.get("group", "basic"),
                                  "group_label": glabel.get(v.get("group", "basic"), ""),
                                  "cp2k": v.get("cp2k", ""), "order": v.get("order", 999)}
                                 for k, v in sorted(MWF_EXTRA_PARAMS.items(),
                                                    key=lambda kv: kv[1].get("order", 999))],
                "notes": [
                    "所有参数项（选项号 / 可选值 / 默认值 / 对应 CP2K 关键词）均按本机 Multiwfn 实际菜单实测校准。",
                    "Multiwfn 任务 11(BSSE) 与 15(XAS) 需交互式指定片段或原子，平台不做自动映射，需生成后手工补充。",
                    "做波函数后处理（轨道成分 / 电子密度拓扑 / 静电势 / 布居分析等）时请开启「导出 .molden 波函数文件」，"
                    "生成的 .molden 可直接作为 Multiwfn 输入。",
                ],
            })
        if method != "POST":
            return self._err(405, "仅支持 POST")

        calc = body.get("calc_type", "energy")
        disp = body.get("dispersion", "none")
        smearing = bool(body.get("smearing"))
        pbc = body.get("pbc")
        scf_method = body.get("scf_method", "diag")
        extra = body.get("extra") or {}
        if scf_method not in SCF_METHODS:
            return self._err(400, "不支持的 SCF 方法: %s（可选 diag/ot）" % scf_method)
        # extra 参数白名单 + 取值校验：拼错的键或值一律 400，避免"静默生成默认输入文件"
        unknown = [k for k in (extra or {}) if k not in MWF_EXTRA_PARAMS]
        if unknown:
            return self._err(400, "未知的 Multiwfn 参数：%s（可用：%s）"
                             % ("、".join(unknown), "、".join(sorted(MWF_EXTRA_PARAMS))))
        for k, v in (extra or {}).items():
            spec = MWF_EXTRA_PARAMS[k]
            if spec["type"] == "choice" and str(v).strip() and _mwf_choice_value(spec, v) is None:
                return self._err(400, "参数「%s」取值无效：%s（可选：%s）"
                                 % (spec["label"], v, "、".join(spec["choices"].keys())))
        # [中1] 参数白名单：未知字段直接 400，避免"传错元素静默生成默认结构"
        ok, verr = check_unknown_ase_params(body, MWF_INP_KEYS)
        if not ok:
            return self._err(400, verr)
        ok, verr = validate_ase_params(body)
        if not ok:
            return self._err(400, verr)

        # 结构来源：优先直接结构文本，其次从体系库 / AiiDA 坐标获取
        struct = body.get("structure")
        if not struct:
            # 从 ASE 生成（用现有 build_structure_ase）
            try:
                r = build_structure_ase(body)
                struct = ase_result_to_xyz(r, "CatMat generated")
            except Exception as e:
                return self._err(400, "缺少结构（structure）或 ASE 生成失败: %s" % e)
        if not struct:
            return self._err(400, "缺少结构（structure）且 ASE 生成未返回结果")

        try:
            geometry = parse_xyz(struct, body.get("cell"))
            eff_pbc = normalize_pbc(extra.get("pbc") or pbc or geometry["pbc"] or "xyz")
            if eff_pbc != "none" and geometry["cell"] is None:
                raise ValueError("周期结构缺少晶胞：请使用含 Lattice 的 XYZ 或提供 cell 矩阵（Å）")
        except (TypeError, ValueError) as exc:
            return self._err(400, str(exc))
        inp, err = generate_multiwfn_inp(struct, calc, disp, smearing, pbc, scf_method, extra, body.get("cell"))
        if err:
            # Multiwfn 属外部程序，调用失败归为依赖问题（便于前端区分"需报修"）
            return self._err(500, err, ERR_DEPENDENCY)
        return self._ok({"inp": inp, "calc_type": calc, "scf_method": scf_method,
                         "run_type": CALC_TYPES.get(calc, (0, "", ""))[1]})

    # ---------------- CP2K vim 工具 ----------------
    def _params(self, method, seg, qs, conn, body):
        """全参数设置端点：CP2K 输入规范树 / SECTION / KEYWORD / 搜索。"""
        def _kw(row):
            d = dict(row)
            for k in ("aliases", "enum_values"):
                try:
                    d[k] = json.loads(d.get(k) or "[]")
                except Exception:
                    d[k] = []
            return d

        op = seg[1] if len(seg) >= 2 else ""
        if method != "GET":
            return self._err(405, "仅支持 GET")

        # 树（懒加载）：GET /api/params/tree?parent=<full_path>
        if op == "tree":
            parent = (qs.get("parent") or [""])[0]
            if parent:
                sub = conn.execute(
                    "SELECT name,full_path,level,is_repeatable FROM cp2k_sections "
                    "WHERE parent=? ORDER BY sort_order", (parent,)).fetchall()
                kws = conn.execute(
                    "SELECT name,datatype,default_value,is_lone,aliases,enum_values "
                    "FROM cp2k_keywords WHERE section_path=? ORDER BY sort_order", (parent,)).fetchall()
                sections = [dict(r) for r in sub]
            else:
                sub = conn.execute(
                    "SELECT name,full_path,level,is_repeatable FROM cp2k_sections "
                    "WHERE parent IS NULL ORDER BY sort_order").fetchall()
                sections = [dict(r) for r in sub]
                kws = []
            return self._ok({"parent": parent or None,
                             "sections": sections,
                             "keywords": [_kw(r) for r in kws]})

        # section 元信息：GET /api/params/section?path=<full_path>
        if op == "section":
            path = (qs.get("path") or [""])[0]
            if not path:
                return self._err(400, "缺少 path")
            row = conn.execute("SELECT * FROM cp2k_sections WHERE full_path=?", (path,)).fetchone()
            if not row:
                return self._err(404, "未找到 section: " + path)
            n_sub = conn.execute("SELECT COUNT(*) c FROM cp2k_sections WHERE parent=?", (path,)).fetchone()["c"]
            n_kw = conn.execute("SELECT COUNT(*) c FROM cp2k_keywords WHERE section_path=?", (path,)).fetchone()["c"]
            d = dict(row)
            d["n_subsections"] = n_sub
            d["n_keywords"] = n_kw
            return self._ok(d)

        # 单关键词：GET /api/params/keyword?path=<full_path>&name=<KEYWORD>
        if op == "keyword":
            path = (qs.get("path") or [""])[0]
            name = (qs.get("name") or [""])[0]
            if not path or not name:
                return self._err(400, "缺少 path/name")
            row = conn.execute("SELECT * FROM cp2k_keywords WHERE section_path=? AND name=?",
                               (path, name)).fetchone()
            if not row:
                return self._err(404, "未找到关键词: " + name)
            return self._ok(_kw(row))

        # 搜索：GET /api/params/search?q=<keyword>
        if op == "search":
            q = (qs.get("q") or [""])[0].strip()
            if not q:
                return self._ok({"query": q, "results": []})
            like = "%" + q + "%"
            qu = q.upper()
            # 专用功能分支（XAS / 含能校正 / 多体格林函数等）排后，通用计算路径优先，
            # 避免搜索 PDOS 等关键词时 XAS/PRINT/PDOS 等专用 section 挤占 FORCE_EVAL/DFT/PRINT/DOS/PDOS
            SPEC = ("XAS", "SMEAGOL", "KG_METHOD", "ENERGY_CORRECTION", "ACTIVE_SPACE",
                    "TDDFPT", "LS_SCF", "NEGF", "PW_DFT", "QMMM", "EIP", "NNP", "MIXED",
                    "FIST", "SIRIUS", "MULTIPLE_FORCE_EVALS", "BSE", "LOW_SPIN", "PROPERTIES",
                    "LOCALIZE", "MM", "SCCS", "UKS", "DFT_LOCALIZE", "GW", "RI_RPA", "MP2")
            spec_cond = " OR ".join(["section_path LIKE ?"] * len(SPEC))
            spec_args = ["%/"+s+"%" for s in SPEC]
            # 顶层 section 检索优先级：GLOBAL > FORCE_EVAL > MOTION > 其他
            sql = ("SELECT section_path,name,datatype,default_value,description,enum_values FROM cp2k_keywords "
                   "WHERE (name LIKE ? OR description LIKE ? OR aliases LIKE ?) ")
            args = [like, like, like]
            secs = [s.strip() for s in (qs.get("sections") or [""])[0].split(",") if s.strip()]
            if secs:
                sql += "AND (" + " OR ".join(
                    ["(section_path = ? OR section_path LIKE ? || '/%')"] * len(secs)) + ") "
                for s in secs:
                    args += [s, s + "/%"]
            sql += ("ORDER BY "
                    "CASE WHEN section_path LIKE '%%/' || ? THEN 0 ELSE 1 END, "     # ① section 名精确匹配（如搜 PDOS 直接命中 .../PDOS 层级）
                    "CASE WHEN name=? THEN 0 WHEN name LIKE ? THEN 1 ELSE 2 END, "   # ② 关键词名匹配优先
                    "CASE WHEN (%s) THEN 1 ELSE 0 END, " % spec_cond +                # ③ 通用路径优先
                    "CASE WHEN section_path='GLOBAL' OR section_path LIKE 'GLOBAL/%%' THEN 0 "
                    "     WHEN section_path='FORCE_EVAL' OR section_path LIKE 'FORCE_EVAL/%%' THEN 1 "
                    "     WHEN section_path='MOTION' OR section_path LIKE 'MOTION/%%' THEN 2 "
                    "     ELSE 3 END, "
                    "sort_order, id LIMIT 50")
            # ? 绑定顺序须与 SQL 文本一致：WHERE 3 个 → ① section 名匹配 → ② name=? / name LIKE ? → ③ spec 条件
            args += [qu, qu, qu + "%"] + spec_args
            rows = conn.execute(sql, args).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                try:
                    d["enum_values"] = json.loads(d.get("enum_values") or "[]")
                except Exception:
                    d["enum_values"] = []
                out.append(d)
            return self._ok({"query": q, "sections": secs, "results": out})

        # 校验词典（全量 SECTION/KEYWORD 名去重）：GET /api/params/dict
        if op == "dict":
            secs = [r["name"] for r in conn.execute(
                "SELECT DISTINCT name FROM cp2k_sections ORDER BY name")]
            kws = [r["name"] for r in conn.execute(
                "SELECT DISTINCT name FROM cp2k_keywords ORDER BY name")]
            return self._ok({"sections": secs, "keywords": kws})

        # 某 section 全部关键词的 GitHub 参考（批量）：GET /api/params/section-refs?path=<full_path>
        if op == "section-refs":
            path = (qs.get("path") or [""])[0]
            if not path:
                return self._err(400, "缺少 path")
            rows = conn.execute(
                "SELECT keyword, repo_url, file_path, ref_type, note, verified FROM cp2k_param_refs "
                "WHERE section_path=? ORDER BY keyword, id", (path,)).fetchall()
            out = {}
            for r in rows:
                out.setdefault(r["keyword"], []).append(dict(r))
            return self._ok(out)

        # 单关键词参考：GET /api/params/refs?path=<full_path>&keyword=<KEYWORD>
        if op == "refs":
            path = (qs.get("path") or [""])[0]
            keyword = (qs.get("keyword") or [""])[0]
            if not keyword:
                return self._err(400, "缺少 keyword")
            rows = conn.execute(
                "SELECT * FROM cp2k_param_refs WHERE keyword=? AND section_path=? ORDER BY id LIMIT 10",
                (keyword.upper(), path)).fetchall()
            if not rows:
                rows = conn.execute(
                    "SELECT * FROM cp2k_param_refs WHERE keyword=? ORDER BY id LIMIT 10",
                    (keyword.upper(),)).fetchall()
            return self._ok([dict(r) for r in rows])

        return self._err(404, "未知 params 端点: " + op)

    def _cp2k(self, method, seg, qs, conn, body):
        """CP2K 相关端点：vim 工具 + 基组/赝势数据文件检索。"""
        # 数据文件清单：GET /api/cp2k/data-files
        if method == "GET" and len(seg) == 2 and seg[1] == "data-files":
            return self._ok(_cp2k_data_file_names())
        # 解析元素基组/赝势：POST /api/cp2k/resolve-kinds {basis_file, potential_file, elements}
        if method == "POST" and len(seg) == 2 and seg[1] == "resolve-kinds":
            bf = body.get("basis_file") or "BASIS_MOLOPT"
            pf = body.get("potential_file") or "POTENTIAL"
            elements = body.get("elements") or []
            if not isinstance(elements, list):
                return self._err(400, "elements 需为元素符号列表")
            return self._ok(resolve_cp2k_kinds(bf, pf, elements))
        # 逐元素跨文件目录：POST /api/cp2k/kinds-catalog {elements}
        if method == "POST" and len(seg) == 2 and seg[1] == "kinds-catalog":
            elements = body.get("elements") or []
            if not isinstance(elements, list):
                return self._err(400, "elements 需为元素符号列表")
            return self._ok(_all_kinds_catalog(elements))
        return self._cp2k_vim(method, seg, qs, conn, body)

    def _cp2k_vim(self, method, seg, qs, conn, body):
        """CP2K 官方 Vim 语法高亮工具（https://www.cp2k.org/tools:vim）。

        GET /api/cp2k/vim          → 返回 cp2k.vim 语法文件内容 + 安装说明
        GET /api/cp2k/vim/download → 直接下载 cp2k.vim 文件（application/octet-stream）
        """
        vim_path = os.path.join(BASE_DIR, "data", "vim", "cp2k.vim")
        if not os.path.isfile(vim_path):
            return self._err(404, "cp2k.vim 语法文件缺失，请先下载官方文件")
        install_cmds = (
            "mkdir -p ~/.vim/syntax ~/.vim\n"
            "cp cp2k.vim ~/.vim/syntax/\n"
            "# 在 ~/.vim/filetype.vim 中添加：\n"
            "if exists(\"did_load_filetypes\")\n  finish\nendif\n"
            "augroup filetypedetect\n"
            "  au! BufNewFile,BufRead *.inp set filetype=cp2k\n"
            "  au! BufNewFile,BufRead *.restart set filetype=cp2k\n"
            "augroup END\n"
            "# 折叠：au FileType cp2k setlocal foldlevel=99 foldmethod=indent"
        )
        if method == "GET" and len(seg) >= 2 and seg[1] == "vim":
            # /api/cp2k/vim/download → 直接下载文件；/api/cp2k/vim → 元信息 + 安装说明
            if len(seg) >= 3 and seg[2] == "download":
                with open(vim_path, "rb") as f:
                    return self._send(200, raw=f.read(),
                                      ctype="application/octet-stream",
                                      headers={"Content-Disposition": "attachment; filename=cp2k.vim"})
            return self._ok({
                "url": "https://www.cp2k.org/tools:vim",
                "download_url": "https://manual.cp2k.org/trunk/cp2k.vim",
                "file_size": os.path.getsize(vim_path),
                "install_cmds": install_cmds,
            })
        return self._err(404, "未知 CP2K vim 端点")

    # ---------------- 独立 CP2K 任务提交（mpirun 直连） ----------------
    def _cp2k_submit(self, method, seg, qs, conn, body):
        """POST /api/cp2k-submit —— 独立提交单个 CP2K 计算任务（非工作流，走 CM- 队列）。

        本机无外部调度器（如 slurm），任务先进入平台内置 FIFO 队列，按提交时间排队；
        资源允许时由后台调度线程按序执行本地 mpirun 提交。返回 CM- 编号，排队中可撤销。

        body:
          name          任务名
          inp            输入文件内容（*.inp 文本）
          nproc         MPI 进程数（默认按物理核数推算，本机为 32）
          omp_threads   OMP_NUM_THREADS（默认 1）
          system_id     可选，关联已保存体系
        """
        if method != "POST":
            return self._err(405, "仅支持 POST")
        name = body.get("name") or "cp2k_task"
        inp = body.get("inp") or body.get("input_file")
        if not inp or not str(inp).strip():
            return self._err(400, "缺少输入文件内容（inp）")
        try:
            nproc = max(1, int(body.get("nproc", DEFAULT_NPROC)))
            omp_threads = max(1, int(body.get("omp_threads", DEFAULT_OMP_THREADS)))
        except (TypeError, ValueError):
            return self._err(400, "nproc / omp_threads 需为整数")
        me = getattr(self, "auth_user", None)
        owner_id = me["id"] if me is not None else None
        qrow, err = queue_enqueue(conn, owner_id, str(name), str(inp),
                                  nproc=nproc, omp_threads=omp_threads,
                                  system_id=body.get("system_id"))
        if err:
            return self._governor_err(err)
        # 兼容旧调用方：qid 与 cm_code 立即可用；任务在调度启动后出现在 /api/tasks
        qview = _queue_entry_view(qrow)
        qview["queue_id"] = qrow["id"]
        qview["enqueued"] = True
        qview["task_id"] = None
        qview["pid"] = None
        return self._ok(qview)

    def _queue(self, method, seg, qs, conn, body):
        """平台内置 CM- 队列：查询 / 提交 / 撤销 / 文件浏览下载。

        清客只能查看与操作自己提交的队列项；撤销排队项即移出队列（不执行）。
        """
        def _q_visible(extra="", params=None):
            me = getattr(self, "auth_user", None)
            params = list(params or [])
            if me is not None and _norm_role(me["role"]) == ROLE_GUEST:
                return ("owner_id=? " + extra).strip(), [me["id"]] + params
            return extra, params

        if method == "GET" and len(seg) == 1:
            where, params = _q_visible()
            if qs.get("status"):
                where = (where + " AND status=?").strip() if where else "status=?"
                params.append(qs["status"][0])
            sql = "SELECT * FROM queue"
            if where:
                sql += " WHERE " + where
            sql += " ORDER BY created_at ASC, id ASC LIMIT 500"
            rows = conn.execute(sql, params).fetchall()
            return self._ok([_queue_entry_view(r) for r in rows], total=len(rows))

        if method == "POST" and len(seg) == 1:
            # 提交本地命令行任务（kind=cp2k 为默认：mpirun 直连单点/优化计算）
            name = body.get("name") or "cm_task"
            inp = body.get("inp") or body.get("input_file")
            if not inp or not str(inp).strip():
                return self._err(400, "缺少输入文件内容（inp）")
            try:
                nproc = max(1, int(body.get("nproc", DEFAULT_NPROC)))
                omp_threads = max(1, int(body.get("omp_threads", DEFAULT_OMP_THREADS)))
            except (TypeError, ValueError):
                return self._err(400, "nproc / omp_threads 需为整数")
            me = getattr(self, "auth_user", None)
            owner_id = me["id"] if me is not None else None
            qrow, err = queue_enqueue(conn, owner_id, str(name), str(inp),
                                      nproc=nproc, omp_threads=omp_threads,
                                      system_id=body.get("system_id"),
                                      kind=str(body.get("kind") or "cp2k"))
            if err:
                return self._governor_err(err)
            audit(conn, "queue_submit", target="queue/%s" % qrow["id"],
                  params={"cm_code": qrow["cm_code"]}, result="queued",
                  actor="uid:%s" % owner_id if owner_id else "system")
            return self._ok(_queue_entry_view(qrow), created_id=qrow["id"])

        # /api/queue/{id}
        qmatch = re.fullmatch(r"(\d+)", seg[1]) if len(seg) >= 2 else None
        if qmatch and len(seg) == 2 and method == "GET":
            r = conn.execute("SELECT * FROM queue WHERE id=?", (seg[1],)).fetchone()
            if not r:
                return self._err(404, "队列项不存在")
            if not self._task_change_allowed(conn, r):
                return self._err(403, "清客只能查看自己提交的队列任务", "OWNERSHIP_DENIED")
            return self._ok(_queue_entry_view(r))

        if qmatch and method == "DELETE":
            # 删除（含撤销语义）：排队项移出队列；运行中先终止进程
            r = conn.execute("SELECT * FROM queue WHERE id=?", (seg[1],)).fetchone()
            if not r:
                return self._err(404, "队列项不存在")
            if not self._task_change_allowed(conn, r):
                return self._err(403, "清客只能删除自己提交的队列任务", "OWNERSHIP_DENIED")
            if r["status"] == "running" and r["pid"]:
                terminate_process_tree(r["pid"])
                SUBMITTED_PROCS.pop(r["pid"], None)
            if r["status"] in ("queued", "running"):
                conn.execute("UPDATE queue SET status='canceled', finished_at=? WHERE id=?",
                             (now_str(), r["id"]))
                t = conn.execute("SELECT id FROM tasks WHERE cm_code=? LIMIT 1",
                                 (r["cm_code"],)).fetchone()
                if t:
                    conn.execute("UPDATE tasks SET status='cancelled', finished_at=? WHERE id=?",
                                 (now_str(), t["id"]))
            else:
                conn.execute("DELETE FROM queue WHERE id=?", (r["id"],))
            audit(conn, "queue_cancel", target="queue/%s" % r["id"],
                  params={"cm_code": r["cm_code"]}, result="canceled")
            return self._ok({"deleted": True, "cm_code": r["cm_code"]})

        # /api/queue/{id}/cancel —— 撤销（排队期间移出队列）
        if qmatch and len(seg) == 3 and seg[2] == "cancel" and method == "POST":
            r = conn.execute("SELECT * FROM queue WHERE id=?", (seg[1],)).fetchone()
            if not r:
                return self._err(404, "队列项不存在")
            if not self._task_change_allowed(conn, r):
                return self._err(403, "清客只能撤销自己提交的队列任务", "OWNERSHIP_DENIED")
            if r["status"] in ("done", "failed", "canceled"):
                return self._err(409, "队列项已处于终态（%s）" % r["status"])
            if r["status"] == "running" and r["pid"]:
                terminate_process_tree(r["pid"])
                SUBMITTED_PROCS.pop(r["pid"], None)
            conn.execute("UPDATE queue SET status='canceled', finished_at=? WHERE id=?",
                         (now_str(), r["id"]))
            t = conn.execute("SELECT id FROM tasks WHERE cm_code=? LIMIT 1",
                             (r["cm_code"],)).fetchone()
            if t:
                conn.execute("UPDATE tasks SET status='cancelled', finished_at=? WHERE id=?",
                             (now_str(), t["id"]))
            audit(conn, "queue_cancel", target="queue/%s" % r["id"],
                  params={"cm_code": r["cm_code"]}, result="canceled")
            return self._ok({"cm_code": r["cm_code"], "status": "canceled"})

        # /api/queue/{id}/files + /api/queue/{id}/download?f=<相对路径> —— 归属内浏览/下载
        if qmatch and len(seg) == 3 and seg[2] == "files" and method == "GET":
            r = conn.execute("SELECT * FROM queue WHERE id=?", (seg[1],)).fetchone()
            if not r:
                return self._err(404, "队列项不存在")
            if not self._task_change_allowed(conn, r):
                return self._err(403, "清客只能浏览自己任务的文件", "OWNERSHIP_DENIED")
            base = os.path.normpath(r["out_dir"]) if r["out_dir"] else ""
            if not base:
                return self._err(404, "任务尚未启动，尚无文件")
            files = []
            for root, _dirs, names in os.walk(base):
                for n in sorted(names):
                    fp = os.path.join(root, n)
                    rel = os.path.relpath(fp, base)
                    files.append({"name": rel, "size": os.path.getsize(fp),
                                  "mtime": time.strftime(
                                      "%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(fp)))})
            return self._ok({"cm_code": r["cm_code"], "dir": base, "files": files})

        if qmatch and len(seg) == 3 and seg[2] == "download" and method == "GET":
            r = conn.execute("SELECT * FROM queue WHERE id=?", (seg[1],)).fetchone()
            if not r:
                return self._err(404, "队列项不存在")
            if not self._task_change_allowed(conn, r):
                return self._err(403, "清客只能下载自己任务的文件", "OWNERSHIP_DENIED")
            if not r["out_dir"]:
                return self._err(404, "任务尚未启动，尚无文件")
            base = os.path.normpath(r["out_dir"])
            fname = (qs.get("f") or qs.get("file") or [""])[0]
            if not fname or not str(fname).strip():
                return self._err(400, "缺少文件名（f）")
            full = os.path.normpath(os.path.join(base, str(fname)))
            if not (full == base or full.startswith(base + os.sep)) or not os.path.isfile(full):
                return self._err(404, "文件不存在")
            try:
                with open(full, "rb") as f:
                    content = f.read()
            except Exception as e:
                return self._err(500, "读取文件失败: %s" % e)
            from http.server import BaseHTTPRequestHandler
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition",
                             "attachment; filename=%s" % os.path.basename(full))
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return None
        return self._err(405, "方法不支持")


    def _llm(self, method, seg, qs, conn, body):
        # 支持的国内大模型厂商清单（供设置界面下拉与提示）
        if len(seg) >= 2 and seg[1] == "vendors" and method == "GET":
            if not llm_vendors:
                return self._err(500, "厂商适配模块不可用")
            current = load_llm_config(public=True)
            return self._ok({"vendors": llm_vendors.list_vendors(),
                             "current": current.get("vendor_info") or {}})
        # 配置读写
        if len(seg) >= 2 and seg[1] == "config":
            if method == "GET":
                return self._ok(load_llm_config(public=True))
            if method == "POST":
                return self._ok(save_llm_config(body or {}))
            return self._err(405, "方法不支持")
        # 对话
        if len(seg) >= 2 and seg[1] == "chat":
            if method != "POST":
                return self._err(405, "仅支持 POST")
            messages = body.get("messages") or []
            use_rag = bool(body.get("rag", True))
            use_web = bool(body.get("web", False))
            agent_mode = bool(body.get("agent", False))
            # 高权限工具必须同时满足：当前实时角色是管理员 + 前端显式勾选 + 服务端硬开关。
            is_admin = bool(getattr(self, "auth_user", None) and self.auth_user["role"] == "admin")
            allow_high_priv = bool(body.get("allow_high_priv", False)) and is_admin
            cfg = load_llm_config()
            if not messages:
                return self._err(400, "缺少 messages")

            # ---- 平台 Agent 模式：自我认知 + 跨模块工具调用 ----
            if agent_mode:
                content, steps, err = agent_llm_chat(
                    messages, cfg, conn, allow_high_priv=allow_high_priv)
                if err:
                    return self._err(502, err)
                return self._ok({"content": content, "agent": True, "agent_steps": steps,
                                 "model": cfg.get("model"),
                                 "high_priv_enabled": AGENT_HIGH_PRIV_ENABLED,
                                 "high_priv_used": allow_high_priv and AGENT_HIGH_PRIV_ENABLED})

            # ---- 普通对话模式：注入轻量自我认知 + RAG/联网 ----
            ai_name = cfg.get("ai_name") or "休go"
            self_aware = ("你是「%s」，催化智构 CataMind 材料计算平台的智能助手（别称：小休、小go），"
                          "当前由大模型 %s 驱动。你运行在平台内，可回答催化材料、CP2K 计算、"
                          "机器学习与主动学习相关问题；回答用户关于「你是谁」的问题时说明这一点。"
                          "请用中文回答，专业术语可用英文。") % (ai_name, cfg.get("model") or "gpt-5.6-sol")
            messages = [{"role": "system", "content": self_aware}] + list(messages)

            # RAG：检索本地知识库/文献/体系
            rag = None
            if use_rag:
                last_user = next((m.get("content", "") for m in reversed(messages)
                                  if m.get("role") == "user"), "")
                rag = rag_search(last_user, conn)
                ctx = build_rag_context(rag)
                if ctx:
                    messages = messages + [{"role": "system",
                                            "content": "以下是平台本地知识库/文献库/研究体系的检索结果，"
                                                       "回答时优先引用这些内部数据（可注明来源），再结合你的领域知识。\n\n" + ctx}]

            # 联网检索
            web_results = None
            if use_web:
                last_user = next((m.get("content", "") for m in reversed(messages)
                                  if m.get("role") == "user"), "")
                web_results, werr = web_search(last_user)
                if web_results and not werr:
                    web_ctx = "【联网学术检索结果】\n" + "\n".join(
                        "- %s（%s，%s，DOI: %s）" % (r.get("title", ""), r.get("venue", ""),
                                                    r.get("year", ""), r.get("doi", "—"))
                        for r in web_results)
                    messages = messages + [{"role": "user", "content": "（补充）以下是联网检索到的相关文献，请结合回答：\n" + web_ctx}]
                elif werr:
                    messages = messages + [{"role": "system", "content": "（联网检索不可用：" + werr + "）"}]

            content, err = llm_call(messages, cfg)
            if err:
                return self._err(502, err)
            return self._ok({"content": content, "rag_used": bool(rag and (rag["knowledge"] or rag["literature"] or rag["systems"])),
                             "web_used": bool(web_results), "model": cfg.get("model")})
        # 对话式 AI 生成 INP
        if len(seg) >= 2 and seg[1] == "gen_inp":
            if method != "POST":
                return self._err(405, "仅支持 POST")
            return self._llm_gen_inp(body)
        return self._err(404, "未知 LLM 端点")

    # ---------------- AI 助手「休go」的持久化记忆（与知识库双向同步）----------------
    def _ml_agent_llm(self, temperature=None):
        """把平台大模型封装为 ml_agent 可注入的调用函数；未配置时返回 None（自动规则降级）。"""
        cfg = load_llm_config()
        if not (cfg.get("base_url") and cfg.get("model") and cfg.get("api_key")):
            return None
        t = temperature if isinstance(temperature, (int, float)) else 0.2

        def _call(messages):
            return llm_call(messages, cfg, temperature=t)
        return _call

    def _ml_samples_rows(self, conn, limit=500):
        limit = max(1, min(int(limit or 500), 2000))
        rows = []
        for r in conn.execute(
                "SELECT name,features,target FROM ml_samples ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall():
            try:
                feats = json.loads(r["features"] or "{}")
            except Exception:
                feats = {}
            rows.append({"name": r["name"], "features": feats, "target": r["target"]})
        return rows

    def _dpgen(self, method, seg, qs, conn, body):
        """势函数延拓（DPGEN）模块统一路由：任务创建/列表/详情/迭代/终止/导出/评估/删除。"""
        me = getattr(self, "auth_user", None)

        # 列表：GET /api/dpgen
        if method == "GET" and len(seg) == 1:
            rows = conn.execute("SELECT * FROM dpgen_runs ORDER BY id DESC LIMIT 200").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                for k in ("log", "params", "seed_info", "result"):
                    if isinstance(d.get(k), str):
                        try:
                            d[k] = json.loads(d[k])
                        except Exception:
                            pass
                try:
                    d["iteration_count"] = conn.execute(
                        "SELECT COUNT(*) c FROM dpgen_iterations WHERE run_id=?", (r["id"],)).fetchone()["c"]
                except Exception:
                    d["iteration_count"] = 0
                out.append(d)
            return self._ok(_owner_view(conn, out), total=len(out))

        # 创建：POST /api/dpgen  {name, config}
        if method == "POST" and len(seg) == 1:
            cfg = body.get("config") or body.get("params") or {}
            name = body.get("name") or cfg.get("run_name") or "DPGEN延拓"
            cfg["run_name"] = name
            run_id, err = _start_dpgen_run(name, cfg, owner_id=me["id"] if me else None)
            if err:
                return self._err(400, err)
            return self._ok({"run_id": run_id, "status": "running"})

        # 详情：GET /api/dpgen/{id}
        if method == "GET" and len(seg) == 2 and seg[1].isdigit():
            r = conn.execute("SELECT * FROM dpgen_runs WHERE id=?", (seg[1],)).fetchone()
            if not r:
                return self._err(404, "势函数延拓任务不存在")
            d = dict(r)
            for k in ("log", "params", "seed_info", "result"):
                if isinstance(d.get(k), str):
                    try:
                        d[k] = json.loads(d[k])
                    except Exception:
                        pass
            d["iterations"] = [_dpgen_iter_to_json(it) for it in conn.execute(
                "SELECT * FROM dpgen_iterations WHERE run_id=? ORDER BY iter_no", (seg[1],)).fetchall()]
            return self._ok(_owner_view(conn, [d])[0])

        # 迭代列表：GET /api/dpgen/{id}/iterations
        if method == "GET" and len(seg) == 3 and seg[1].isdigit() and seg[2] == "iterations":
            rows = conn.execute(
                "SELECT * FROM dpgen_iterations WHERE run_id=? ORDER BY iter_no", (seg[1],)).fetchall()
            out = [_dpgen_iter_to_json(it) for it in rows]
            return self._ok(out)

        # 终止：POST /api/dpgen/{id}/kill
        if method == "POST" and len(seg) == 3 and seg[1].isdigit() and seg[2] == "kill":
            r = conn.execute("SELECT * FROM dpgen_runs WHERE id=?", (seg[1],)).fetchone()
            if not r:
                return self._err(404, "任务不存在")
            if r["status"] != "running":
                return self._err(400, "任务已结束（状态：%s）" % r["status"])
            conn.execute(
                "UPDATE dpgen_runs SET status='cancelled',error=?,finished_at=? WHERE id=?",
                ("用户终止", now_str(), seg[1]))
            return self._ok({"killed": True, "id": int(seg[1])})

        # 导出：POST /api/dpgen/{id}/export  {format}
        if method == "POST" and len(seg) == 3 and seg[1].isdigit() and seg[2] == "export":
            r = conn.execute("SELECT * FROM dpgen_runs WHERE id=?", (seg[1],)).fetchone()
            if not r:
                return self._err(404, "任务不存在")
            fmt = (body.get("format") or (qs.get("format") or ["json"])[0]).lower()
            d = dict(r)
            try:
                d["params"] = json.loads(d.get("params") or "{}")
            except Exception:
                pass
            d["iterations"] = [dict(it) for it in conn.execute(
                "SELECT * FROM dpgen_iterations WHERE run_id=?", (seg[1],)).fetchall()]
            if fmt == "csv":
                import csv as _csv, io as _io
                buf = _io.StringIO()
                w = _csv.writer(buf)
                w.writerow(["iter_no", "phase", "status", "n_candidates", "metrics"])
                for it in d["iterations"]:
                    w.writerow([it["iter_no"], it["phase"], it["status"],
                               it["n_candidates"], it["metrics"]])
                return self._send(200, raw=buf.getvalue().encode("utf-8"), ctype="text/csv",
                                 headers={"Content-Disposition":
                                          "attachment; filename=dpgen_%s.csv" % seg[1]})
            return self._ok(d)

        # 收敛评估：POST /api/dpgen/{id}/assess
        if method == "POST" and len(seg) == 3 and seg[1].isdigit() and seg[2] == "assess":
            from catmat_tools import dpgen_adapter as dpgen_ad
            rows = conn.execute(
                "SELECT metrics FROM dpgen_iterations WHERE run_id=? AND metrics<>'{}' "
                "ORDER BY iter_no", (seg[1],)).fetchall()
            history = []
            for h in rows:
                try:
                    m = json.loads(h["metrics"] or "{}")
                    if m:
                        history.append(m)
                except Exception:
                    pass
            return self._ok(dpgen_ad.assess_convergence(history, body.get("criteria")))

        # 删除：DELETE /api/dpgen/{id}
        if method == "DELETE" and len(seg) == 2 and seg[1].isdigit():
            r = conn.execute("SELECT status FROM dpgen_runs WHERE id=?", (seg[1],)).fetchone()
            if not r:
                return self._err(404, "任务不存在")
            if r["status"] == "running":
                return self._err(400, "任务仍在运行中，请先终止")
            conn.execute("DELETE FROM dpgen_iterations WHERE run_id=?", (seg[1],))
            conn.execute("DELETE FROM dpgen_runs WHERE id=?", (seg[1],))
            return self._ok({"deleted": True, "id": int(seg[1])})

        return self._err(404, "未知 DPGEN 接口: /api/" + "/".join(seg))

    def _codex(self, method, seg, qs, conn, body):
        """Codex 对话通道（本机 Codex CLI，danger-full-access 档）。

        POST /api/codex/chat      {message, session_id?} → 对话（多轮，session_id 续接）
        POST /api/codex/upload    {name, content | content_b64} → 上传文件到 codex_uploads/（供 Codex 读取）
        GET  /api/codex/download?file=xx     → 下载上传或生成的文件
        GET  /api/codex/files                → 列出上传与 analysis_outputs/ 文件
        GET  /api/codex/status               → Codex 可用性自检
        """
        if method == "POST" and len(seg) >= 2 and seg[1] == "chat":
            message = (body.get("message") or "").strip()
            if not message:
                return self._err(400, "缺少消息内容（message）")
            session_id = (body.get("session_id") or "").strip() or None
            reply, new_sid, err = codex_chat(message, session_id)
            if err:
                return self._err(500, err, "CODEX_ERROR")
            return self._ok({"reply": reply, "session_id": new_sid})
        # ---------- 上传文件（供 Codex 读取分析） ----------
        if method == "POST" and len(seg) >= 2 and seg[1] == "upload":
            name = (body.get("name") or "").strip()
            content = body.get("content")
            content_b64 = body.get("content_b64")
            if not name or (content is None and content_b64 is None):
                return self._err(400, "缺少文件名（name）或内容（content/content_b64）")
            safe = os.path.basename(name)
            os.makedirs(CODEX_UPLOAD_DIR, exist_ok=True)
            full = os.path.normpath(os.path.join(CODEX_UPLOAD_DIR, safe))
            if not full.startswith(os.path.normpath(CODEX_UPLOAD_DIR)):
                return self._err(400, "文件名非法")
            try:
                if content_b64 is not None:
                    import base64 as _b64
                    with open(full, "wb") as f:
                        f.write(_b64.b64decode(str(content_b64)))
                else:
                    with open(full, "w", encoding="utf-8") as f:
                        f.write(str(content))
            except Exception as e:
                return self._err(500, "写入失败: %s" % e)
            audit(conn, "codex_upload", target=safe, ok=True, actor=self.auth_user)
            return self._ok({"name": safe, "dir": CODEX_UPLOAD_DIR,
                             "hint": "文件已上传，可在对话里让 Codex 读取 codex_uploads/%s" % safe})
        # ---------- 下载上传文件或生成文件（限制在两个公开目录内） ----------
        if method == "GET" and len(seg) >= 2 and seg[1] == "download":
            requested = (qs.get("file", [""])[0] or "").strip()
            full = _codex_download_path(requested)
            if not full or not os.path.isfile(full):
                return self._err(404, "文件不存在")
            try:
                with open(full, "rb") as f:
                    data = f.read()
            except Exception as e:
                return self._err(500, "读取失败: %s" % e)
            safe = os.path.basename(full)
            return self._send(200, raw=data, ctype="application/octet-stream",
                              headers={"Content-Disposition": "attachment; filename=\"%s\"" % safe})
        # ---------- 列出上传文件与 Codex 生成文件 ----------
        if method == "GET" and len(seg) >= 2 and seg[1] == "files":
            files = []
            for label, root in (("codex_uploads", CODEX_UPLOAD_DIR),
                                ("analysis_outputs", CODEX_OUTPUT_DIR)):
                if not os.path.isdir(root):
                    continue
                for here, dirs, names in os.walk(root):
                    dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(here, d))]
                    for fn in sorted(names):
                        relative = os.path.relpath(os.path.join(here, fn), root).replace(os.sep, "/")
                        public_path = label + "/" + relative
                        p = _codex_download_path(public_path)
                        if not p or not os.path.isfile(p):
                            continue
                        st = os.stat(p)
                        files.append({"name": fn, "path": public_path, "size": st.st_size,
                                      "mtime": time.strftime("%Y-%m-%d %H:%M",
                                                             time.localtime(st.st_mtime))})
            files.sort(key=lambda f: f["path"])
            return self._ok({"files": files, "dir": CODEX_UPLOAD_DIR,
                             "output_dir": CODEX_OUTPUT_DIR})
        # ---------- Codex 可用性自检 ----------
        if method == "GET" and len(seg) >= 2 and seg[1] == "status":
            ok = os.path.isfile(CODEX_BIN) and os.access(CODEX_BIN, os.X_OK)
            return self._ok({"codex_bin": CODEX_BIN, "available": ok,
                             "workdir": CODEX_WORKDIR, "upload_dir": CODEX_UPLOAD_DIR})
        return self._err(404, "未知 codex 端点")

    def _agent(self, method, seg, qs, conn, body):
        # 路径：/api/agent/memory[/{id}|/sync]、/api/agent/ml/advise、/api/agent/ml/interpret
        sub = seg[1] if len(seg) >= 2 else ""
        act = seg[2] if len(seg) >= 3 else ""
        _me0 = getattr(self, "auth_user", None)
        _actor = ("uid:%s" % _me0["id"]) if _me0 is not None else "anonymous"

        # ---------- 休go · 自然语言任务编排（计划 / 确认 / 执行 / 监控） ----------
        # POST /api/agent/plan            {instruction}          → 解析意图并生成待确认计划
        # GET  /api/agent/runs            ?limit=                → 任务流列表
        # GET  /api/agent/runs/{id}                              → 计划 + 逐步状态进度
        # PATCH /api/agent/runs/{id}      {steps:[{key,enabled,params}]} → 确认前改参与增删
        # POST /api/agent/runs/{id}/start {allow_high_priv}      → 确认并执行（后台）
        # POST /api/agent/runs/{id}/cancel                       → 取消/中止
        if sub == "plan" and method == "POST":
            instruction = (body.get("instruction") or "").strip()
            if not instruction:
                return self._err(400, "请提供自然语言指令（instruction）")
            plan, err = agent_build_plan(instruction)
            if err:
                return self._err(400, err)
            cur = conn.execute(
                "INSERT INTO agent_runs(title,instruction,intent,template,status,plan_json,"
                "progress,actor,created_at) VALUES(?,?,?,?,'draft',?,0,?,?)",
                (plan["title"], instruction, plan["intent"], plan["template"],
                 json.dumps(plan, ensure_ascii=False), _actor, now_str()))
            rid = cur.lastrowid
            for i, s in enumerate(plan["steps"]):
                conn.execute(
                    "INSERT INTO agent_run_steps(run_id,seq,key,tool,label,params_json,schema_json,"
                    "depends_on,enabled,internal,high_priv,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending')",
                    (rid, i, s["key"], s["tool"], s["label"],
                     json.dumps(s["params"], ensure_ascii=False),
                     json.dumps(s.get("schema") or {}, ensure_ascii=False),
                     json.dumps(s.get("depends_on") or []), 1 if s.get("enabled", True) else 0,
                     1 if s.get("internal") else 0, 1 if s.get("high_priv") else 0))
            audit(conn, "agent_plan", target="agent_runs/%d" % rid,
                  params={"instruction": instruction[:300], "template": plan["template"]},
                  result="draft", actor=_actor)
            conn.commit()
            return self._ok({"run_id": rid, "plan": plan,
                             "hint": "请确认每一步的参数后调用 /api/agent/runs/%d/start 执行" % rid})

        if sub == "runs":
            # 列表
            if method == "GET" and not act:
                limit = max(1, min(int(qs.get("limit", ["20"])[0] or 20), 100))
                rows = conn.execute(
                    "SELECT id,title,instruction,intent,template,status,progress,error,"
                    "created_at,started_at,finished_at FROM agent_runs ORDER BY id DESC LIMIT ?",
                    (limit,)).fetchall()
                return self._ok([dict(r) for r in rows])
            try:
                rid = int(act)
            except Exception:
                return self._err(400, "任务流 id 无效")
            run = conn.execute("SELECT * FROM agent_runs WHERE id=?", (rid,)).fetchone()
            if not run:
                return self._err(404, "任务流不存在")
            verb = seg[3] if len(seg) >= 4 else ""
            # 详情（计划 + 逐步状态）
            if method == "GET" and not verb:
                steps = conn.execute(
                    "SELECT seq,key,tool,label,params_json,schema_json,depends_on,enabled,"
                    "internal,high_priv,status,result_json,error,duration_ms,started_at,finished_at "
                    "FROM agent_run_steps WHERE run_id=? ORDER BY seq", (rid,)).fetchall()
                out = []
                for s in steps:
                    d = dict(s)
                    d["params"] = json.loads(d.pop("params_json") or "{}")
                    d["schema"] = json.loads(d.pop("schema_json") or "{}")
                    d["depends_on"] = json.loads(d.pop("depends_on") or "[]")
                    try:
                        d["result"] = json.loads(d.pop("result_json") or "null")
                    except Exception:
                        d["result"] = d.pop("result_json")
                    out.append(d)
                return self._ok({"run": {k: run[k] for k in run.keys()}, "steps": out})
            # 编辑（仅 draft 状态可改）
            if method == "PATCH":
                if run["status"] != "draft":
                    return self._err(409, "该任务流已启动，不能修改（可取消后重新规划）")
                edits = body.get("steps") or []
                if not isinstance(edits, list):
                    return self._err(400, "steps 需为数组")
                for ed in edits:
                    key = ed.get("key")
                    if not key:
                        continue
                    row = conn.execute(
                        "SELECT id,schema_json,internal,tool FROM agent_run_steps WHERE run_id=? AND key=?",
                        (rid, key)).fetchone()
                    if not row:
                        return self._err(400, "未知步骤: %s" % key)
                    if "enabled" in ed:
                        conn.execute("UPDATE agent_run_steps SET enabled=? WHERE id=?",
                                     (1 if ed["enabled"] else 0, row["id"]))
                    if "params" in ed and isinstance(ed["params"], dict):
                        # 参数校验：按 schema 的 choice/int/number/bool 收敛取值
                        schema = json.loads(row["schema_json"] or "{}")
                        clean = {}
                        for k, v in ed["params"].items():
                            spec = schema.get(k)
                            if spec and spec.get("type") == "choice":
                                if v not in (spec.get("choices") or []):
                                    return self._err(400, "步骤 %s 的参数 %s 取值非法：%s" % (key, k, v))
                            if spec and spec.get("type") in ("int", "number") and v is not None:
                                try:
                                    v = int(v) if spec["type"] == "int" else float(v)
                                except Exception:
                                    return self._err(400, "步骤 %s 的参数 %s 需为数值" % (key, k))
                                if spec.get("min") is not None and v < spec["min"]:
                                    return self._err(400, "步骤 %s 的参数 %s 小于下限 %s" % (key, k, spec["min"]))
                                if spec.get("max") is not None and v > spec["max"]:
                                    return self._err(400, "步骤 %s 的参数 %s 超过上限 %s" % (key, k, spec["max"]))
                            if spec and spec.get("type") == "bool":
                                v = bool(v)
                            clean[k] = v
                        conn.execute("UPDATE agent_run_steps SET params_json=? WHERE id=?",
                                     (json.dumps(clean, ensure_ascii=False), row["id"]))
                audit(conn, "agent_plan_edit", target="agent_runs/%d" % rid,
                      params={"steps": [e.get("key") for e in edits]}, result="edited", actor=_actor)
                conn.commit()
                return self._ok({"run_id": rid, "edited": True})
            # 启动
            if method == "POST" and verb == "start":
                if run["status"] not in ("draft", "failed"):
                    return self._err(409, "该任务流当前状态为 %s，不能重复启动" % run["status"])
                allow_hp = bool(body.get("allow_high_priv"))
                hp_steps = conn.execute(
                    "SELECT COUNT(*) c FROM agent_run_steps WHERE run_id=? AND high_priv=1 AND enabled=1",
                    (rid,)).fetchone()["c"]
                if hp_steps and not (allow_hp and AGENT_HIGH_PRIV_ENABLED):
                    return self._err(403, "计划含高权限步骤，请勾选「允许高权限工具」后执行"
                                          "（服务端需开启 CATMAT_AGENT_HIGH_PRIV）", "HIGH_PRIV_REQUIRED")
                conn.execute(
                    "UPDATE agent_runs SET status='queued', progress=0, error=NULL, actor=? WHERE id=?",
                    (_actor, rid))
                conn.execute(
                    "UPDATE agent_run_steps SET status='pending', result_json=NULL, error=NULL"
                    " WHERE run_id=? AND enabled=1", (rid,))
                audit(conn, "agent_run_start", target="agent_runs/%d" % rid,
                      params={"allow_high_priv": allow_hp}, result="queued", actor=_actor)
                conn.commit()
                threading.Thread(target=_agent_execute_run, args=(rid, _actor, allow_hp),
                                 daemon=True).start()
                return self._ok({"run_id": rid, "status": "queued",
                                 "hint": "已开始执行，轮询 GET /api/agent/runs/%d 查看进度" % rid})
            # 取消
            if method == "POST" and verb == "cancel":
                if run["status"] in ("succeeded", "failed", "cancelled"):
                    return self._err(409, "任务流已结束，无需取消")
                conn.execute("UPDATE agent_runs SET status='cancelled', finished_at=? WHERE id=?",
                             (now_str(), rid))
                conn.execute("UPDATE agent_run_steps SET status='cancelled' "
                             "WHERE run_id=? AND status IN ('pending','running')", (rid,))
                audit(conn, "agent_run_cancel", target="agent_runs/%d" % rid,
                      result="cancelled", actor=_actor)
                conn.commit()
                return self._ok({"run_id": rid, "status": "cancelled"})
            return self._err(405, "不支持的操作")

        # ---------- 休go · 机器学习训练顾问（导入数据集 → 训练模型） ----------
        # POST /api/agent/ml/advise {rows?|csv?|mapping?, model?, device?, task?}
        if method == "POST" and sub == "ml" and act == "advise":
            if ml_agent is None:
                return self._err(500, "训练顾问模块不可用")
            rows = body.get("rows") or []
            if not rows and body.get("csv"):
                try:
                    norm = normalize_dataset_csv(body["csv"], mapping=body.get("mapping"),
                                                 mode="ml")
                    rows = norm["rows"]
                except Exception as e:
                    return self._err(400, "CSV 解析失败: %s" % e)
            if not rows:
                rows = self._ml_samples_rows(conn, body.get("limit") or 500)
            if not rows:
                return self._err(400, "暂无样本：请先导入数据集，或传入 rows / csv")
            summary = ml_agent.summarize_dataset(rows, target_key=body.get("target_key") or "target")
            ctx = {"task": body.get("task") or "ml", "model_hint": body.get("model"),
                   "device": body.get("device")}
            llm = self._ml_agent_llm(body.get("temperature"))
            advice = ml_agent.advise_dataset(summary, ctx=ctx, llm=llm)
            _me = getattr(self, "auth_user", None)
            audit(conn, "ml_agent_advise", target="ml_samples",
                  params={"n": summary["n_samples"], "source": advice.get("source")},
                  result="advised",
                  actor=("uid:%s" % _me["id"]) if _me is not None else "system")
            return self._ok({"advice": advice, "summary": summary,
                             "source": advice.get("source"), "llm_ready": bool(llm)})

        # ---------- 休go · 结果解读师（后处理分析：训练结果 / 主动学习推荐） ----------
        # POST /api/agent/ml/interpret {result?|task_id?|task_type?|candidates?|include_candidates?}
        if method == "POST" and sub == "ml" and act == "interpret":
            if ml_agent is None:
                return self._err(500, "结果解读模块不可用")
            result = body.get("result")
            task_row = None
            if not result:
                sql = "SELECT id,result,params,log,task_type FROM ml_tasks WHERE 1=1"
                params = []
                if body.get("task_id"):
                    sql += " AND id=?"
                    params.append(body.get("task_id"))
                else:
                    sql += " AND status IN ('done','finished')"
                if body.get("task_type"):
                    sql += " AND task_type=?"
                    params.append(body.get("task_type"))
                sql += " ORDER BY id DESC LIMIT 1"
                task_row = conn.execute(sql, params).fetchone()
                if not task_row:
                    return self._err(404, "未找到可解读的已完成任务")
                try:
                    result = json.loads(task_row["result"] or "{}") or {}
                except Exception:
                    result = {}
                result.setdefault("task_id", task_row["id"])
                result.setdefault("task_type", task_row["task_type"])
            summary = None
            if body.get("with_summary", True):
                summary = ml_agent.summarize_dataset(
                    self._ml_samples_rows(conn, body.get("limit") or 300))
            candidates = body.get("candidates")
            if not candidates and body.get("include_candidates"):
                candidates = (result or {}).get("candidates") or \
                             (result or {}).get("recommendations") or []
            ctx = {"task": body.get("task") or "ml", "candidates": candidates}
            llm = self._ml_agent_llm(body.get("temperature"))
            out = ml_agent.interpret_result(result, summary, ctx=ctx, llm=llm)
            audit(conn, "ml_agent_interpret", target="ml_tasks/%s" % (result.get("task_id") or "-"),
                  params={"source": out.get("source")}, result="interpreted")
            return self._ok({"interpretation": out, "result": result, "summary": summary,
                             "source": out.get("source"), "llm_ready": bool(llm)})

        # POST /api/agent/memory/sync —— 手动触发 知识库 → 记忆 增量同步
        if method == "POST" and sub == "memory" and act == "sync":
            added, updated, skipped, total = sync_knowledge_to_agent_memory(conn)
            return self._ok({"added": added, "updated": updated, "skipped": skipped,
                             "knowledge_total": total,
                             "memory_total": conn.execute(
                                 "SELECT count(*) c FROM agent_memory").fetchone()["c"]})

        # GET /api/agent/memory?q=&category=&limit= —— 查看/检索记忆（含索引）
        if method == "GET" and sub == "memory":
            q = (qs.get("q") or [""])[0]
            cat = (qs.get("category") or [""])[0]
            limit = min(int((qs.get("limit") or ["50"])[0]), 200)
            sql = ("SELECT id,category,topic,keyword,content,source,origin,synced_at "
                   "FROM agent_memory WHERE 1=1")
            params = []
            if cat:
                sql += " AND category LIKE ?"
                params.append("%" + cat + "%")
            if q:
                sql += " AND (topic LIKE ? OR keyword LIKE ? OR content LIKE ?)"
                params += ["%" + q + "%"] * 3
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return self._ok({
                "total": conn.execute("SELECT count(*) c FROM agent_memory").fetchone()["c"],
                "count": len(rows),
                "index": agent_memory_index(conn),
                "memories": [dict(r) for r in rows]})

        # DELETE /api/agent/memory/{id} —— 删除单条记忆（同步来源条目清掉 ref_id 关联）
        if method == "DELETE" and sub == "memory" and act.isdigit():
            mid = int(act)
            row = conn.execute("SELECT ref_id FROM agent_memory WHERE id=?", (mid,)).fetchone()
            if not row:
                return self._err(404, "记忆不存在：%s" % mid)
            conn.execute("DELETE FROM agent_memory WHERE id=?", (mid,))
            conn.commit()
            LOG.info("删除休go记忆 #%s（ref_id=%s，知识库原条目保留）", mid, row["ref_id"])
            return self._ok({"deleted": mid, "ref_id": row["ref_id"]})

        return self._err(404, "未知 agent 端点（可用：GET /api/agent/memory、"
                              "POST /api/agent/memory/sync、DELETE /api/agent/memory/{id}）")

    # ---------------- 内置手册（平台设置 → 帮助与文档）----------------
    def _manual(self, method, seg, qs, conn, body):
        """GET /api/manual?doc=user-manual[&download=1]

        doc 走白名单映射（MANUAL_DOCS），不接受任意文件名或路径，杜绝目录穿越。
        不带 download：返回 JSON（含 Markdown 原文、标题、版本、更新日期）；
        带 download=1：以附件形式返回 .md 文件。
        """
        if method != "GET":
            return self._err(405, "仅支持 GET")
        name = (qs.get("doc") or ["user-manual"])[0]
        fname = MANUAL_DOCS.get(name)
        if not fname:
            return self._err(404, "手册不存在：%s（可选：%s）"
                             % (name, "、".join(sorted(MANUAL_DOCS))))
        full = os.path.normpath(os.path.join(DOCS_DIR, fname))
        if not full.startswith(os.path.normpath(DOCS_DIR)) or not os.path.isfile(full):
            return self._err(404, "手册文件缺失：%s" % fname)
        try:
            with open(full, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception as e:
            log_exc("读取手册失败 %s" % fname, e)
            return self._err(500, "读取失败: %s" % e)

        # 从 Markdown 中解析标题与版本（手册头部信息表）
        title = fname
        m = re.search(r"^#\s+(.+)$", text, re.M)
        if m:
            title = m.group(1).strip()
        version = ""
        m = re.search(r"\*\*文档版本\*\*\s*\|\s*([^|\n]+)", text)
        if m:
            version = m.group(1).strip()
        updated = ""
        m = re.search(r"\*\*更新日期\*\*\s*\|\s*([^|\n]+)", text)
        if m:
            updated = m.group(1).strip()

        if (qs.get("download") or [""])[0].lower() in ("1", "true", "yes"):
            return self._send(200, raw=text.encode("utf-8"),
                              ctype="text/markdown; charset=utf-8",
                              headers={"Content-Disposition":
                                       'attachment; filename="%s"' % fname})
        return self._ok({"doc": name, "file": fname, "title": title,
                         "version": version, "updated": updated,
                         "size": len(text), "content": text})

    # ---------------- 平台本地账号、会话与兼容登录通道 ----------------
    def _require_auth_user(self, conn):
        """auth 公共路由内显式校验当前账号，返回实时数据库角色。"""
        claims, error = self._authenticate_access_token()
        if error:
            self._err(401, "登录已过期" if error == "TOKEN_EXPIRED" else "未登录", error)
            return None, None, error
        if not _scope_allows(claims, SCOPE_PLATFORM):
            self._err(403, "当前凭证不能访问平台账号功能", "SCOPE_DENIED")
            return None, None, "SCOPE_DENIED"
        user = conn.execute("SELECT * FROM auth_users WHERE id=?", (claims.get("uid"),)).fetchone()
        if not user or user["disabled"]:
            self._err(403, "该平台账号不可用", "ACCOUNT_DISABLED")
            return None, None, "ACCOUNT_DISABLED"
        sid = claims.get("sid")
        if not sid:
            self._err(401, "旧版登录会话已失效，请使用账号密码重新登录", "SESSION_REVOKED")
            return None, None, "SESSION_REVOKED"
        active = conn.execute(
            "SELECT 1 FROM auth_sessions WHERE id=? AND user_id=? AND revoked=0 AND expires_at>?",
            (sid, user["id"], int(time.time()))).fetchone()
        if not active:
            self._err(401, "登录会话已被撤销，请重新登录", "SESSION_REVOKED")
            return None, None, "SESSION_REVOKED"
        return claims, user, None

    def _require_admin(self, conn):
        claims, user, error = self._require_auth_user(conn)
        if error:
            return None, None, error
        eff = _effective_role(user)
        if eff == ROLE_OWNER:
            return claims, user, None
        if eff == ROLE_STEWARD and CAP_MANAGE_ACCOUNTS in _capabilities_of(user):
            return claims, user, None
        self._err(403, "仅东翁或获「账号管理」授权的掌书可管理平台账号", "PERMISSION_DENIED")
        return None, None, "PERMISSION_DENIED"

    def _require_approver(self, conn):
        """校验「登录确认」权限：有效 access token + scope 含 approve + 平台角色 admin。

        失败时已自行发送响应，调用方只需 `if err: return`。
        返回 (claims, user_row, None) 或 (None, None, 错误码)。
        """
        claims, error = self._authenticate_access_token()
        if error:
            self._err(401, "登录已过期" if error == "TOKEN_EXPIRED" else "未登录", error)
            return None, None, error
        if not (_scope_allows(claims, SCOPE_APPROVE) or _scope_allows(claims, SCOPE_PLATFORM)):
            self._err(403, "当前凭证无权进行登录确认", "SCOPE_DENIED")
            return None, None, "SCOPE_DENIED"
        user = conn.execute("SELECT * FROM auth_users WHERE id=?", (claims.get("uid"),)).fetchone()
        if not user or user["disabled"]:
            self._err(403, "该平台账号不可用", "ACCOUNT_DISABLED")
            return None, None, "ACCOUNT_DISABLED"
        sid = claims.get("sid")
        if not sid:
            self._err(401, "旧版登录会话已失效，请重新登录", "SESSION_REVOKED")
            return None, None, "SESSION_REVOKED"
        active = conn.execute(
            "SELECT 1 FROM auth_sessions WHERE id=? AND user_id=? AND revoked=0 AND expires_at>?",
            (sid, user["id"], int(time.time()))).fetchone()
        if not active:
            self._err(401, "登录会话已被撤销，请重新登录", "SESSION_REVOKED")
            return None, None, "SESSION_REVOKED"
        if _effective_role(user) != ROLE_OWNER:
            self._err(403, "仅东翁可确认登录请求", "NOT_APPROVER")
            return None, None, "NOT_APPROVER"
        return claims, user, None

    def _auth(self, method, seg, qs, conn, body):
        route = "/".join(seg[1:]) if len(seg) > 1 else ""

        if method == "GET" and route in ("", "status"):
            config_error = auth_config_error()
            admin_ready = bool(ADMIN_TOKEN)
            wechat_configured = bool(WECHAT_TEST_MODE or (WECHAT_APP_ID and WECHAT_APP_SECRET))
            local_count = conn.execute("SELECT COUNT(*) c FROM auth_users "
                                       "WHERE username IS NOT NULL AND disabled=0").fetchone()["c"]
            return self._ok({
                "enabled": AUTH_ENABLED,
                "mode": "local",
                "version": 4,
                "test_mode": WECHAT_TEST_MODE,
                "channels": {
                    "local": LOCAL_AUTH_ENABLED,
                    "admin": admin_ready,
                    "wechat": WECHAT_CHALLENGE_ENABLED and wechat_configured,
                    "challenge": WECHAT_CHALLENGE_ENABLED and wechat_configured,
                },
                "accounts": {"active_local": local_count, "bootstrap_required": local_count == 0},
                "challenge": {
                    "ttl": CHALLENGE_TTL,
                    "interval": CHALLENGE_POLL_INTERVAL,
                    "code_len": CHALLENGE_USER_CODE_LEN,
                },
                "ready": config_error is None,
                "note": config_error or ("本地账号密码通道已启用%s" %
                                         ("，尚未创建账号" if local_count == 0 else "")),
            })

        if method == "POST" and route == "local/login":
            if not AUTH_ENABLED or not LOCAL_AUTH_ENABLED:
                return self._err(403, "本地账号登录未启用", "AUTH_DISABLED")
            username = _normalize_username(body.get("username"))
            password = body.get("password")
            ip = _client_ip(self.headers, self.client_address[0]) or "unknown"
            if not _rate_allow("local_login:" + ip, LOCAL_LOGIN_LIMIT):
                return self._err(429, "登录尝试过于频繁，请稍后再试", "RATE_LIMITED")
            user = _local_account_by_username(conn, username)
            # 不区分“用户名不存在”和“密码错误”，避免账号枚举。
            if not user or not _verify_password(password, user["password_hash"]):
                audit(conn, "local_login_fail", target=username or "(empty)", result="invalid credentials",
                      ok=False, actor="ip:%s" % ip)
                return self._err(401, "用户名或密码不正确", "INVALID_CREDENTIALS")
            if user["disabled"]:
                return self._err(403, "该平台账号已停用", "ACCOUNT_DISABLED")
            conn.execute("UPDATE auth_users SET last_login_at=? WHERE id=?", (now_str(), user["id"]))
            user = conn.execute("SELECT * FROM auth_users WHERE id=?", (user["id"],)).fetchone()
            data = _issue_session(conn, user, mode="local")
            audit(conn, "local_login", target="auth_users/%s" % user["id"],
                  result="role=%s" % user["role"], actor="uid:%s" % user["id"])
            return self._ok(data)

        # 首个本地东翁的安全引导：仅当数据库中没有任何本地账号时，
        # 使用服务器预置的应急管理员令牌创建；创建后该端点永久关闭。
        if method == "POST" and route == "local/bootstrap":
            if not AUTH_ENABLED or not LOCAL_AUTH_ENABLED:
                return self._err(403, "本地账号登录未启用", "AUTH_DISABLED")
            count = conn.execute("SELECT COUNT(*) c FROM auth_users WHERE username IS NOT NULL").fetchone()["c"]
            if count:
                return self._err(409, "本地账号已初始化", "BOOTSTRAP_CLOSED")
            ok, code = _verify_admin_token(body.get("admin_token"))
            if not ok:
                return self._err(401, "初始化令牌无效", "INVALID_ADMIN_TOKEN" if code != "NOT_READY" else "AUTH_NOT_READY")
            user, error, errcode = _create_local_user(
                conn, body.get("username"), body.get("password"),
                body.get("display_name") or body.get("username") or "平台账户",
                role=ROLE_OWNER)
            if error:
                return self._err(409 if errcode == "USERNAME_EXISTS" else 400, error, errcode)
            audit(conn, "local_bootstrap", target="auth_users/%s" % user["id"],
                  result="first local owner", actor="bootstrap")
            _sync_account_role_files(conn)
            return self._ok(_public_account(user))

        if method == "POST" and route == "admin/login":
            """网页端管理员令牌登录：校验 CATMAT_ADMIN_TOKEN → 建/复用 __admin_token__ 账号 → 签发会话。"""
            if not AUTH_ENABLED:
                return self._err(403, "鉴权功能未启用", "AUTH_DISABLED")
            ip = _client_ip(self.headers, self.client_address[0]) or "unknown"
            if not _rate_allow("admin_login:" + ip, max(3, LOCAL_LOGIN_LIMIT)):
                return self._err(429, "管理员令牌尝试过于频繁，请稍后再试", "RATE_LIMITED")
            ok, code = _verify_admin_token(body.get("admin_token"))
            if code == "NOT_READY":
                return self._err(503, "鉴权配置不完整：未设置 CATMAT_ADMIN_TOKEN", "AUTH_NOT_READY")
            if code == "INPUT_ERROR":
                return self._err(400, "缺少 admin_token", "INPUT_ERROR")
            if not _auth_secret():
                return self._err(503, "鉴权配置不完整：缺少 CATMAT_JWT_SECRET", "AUTH_NOT_READY")
            if not ok:
                audit(conn, "admin_login_fail", target="auth_admin_token",
                      result="token mismatch", ok=False, actor="ip:%s" % ip)
                return self._err(401, "管理员令牌无效", "INVALID_ADMIN_TOKEN")
            user, uerr, ucode = _upsert_admin_holder(conn)
            if uerr:
                return self._err(403, uerr, ucode)
            data = _issue_session(conn, user, mode="admin")
            audit(conn, "admin_login", target="auth_users/%s" % user["id"],
                  result="role=%s" % user["role"], actor="admin")
            return self._ok(data)

        # 确认器会话：小程序用微信身份换取「仅登录确认」令牌（scope=approve）。
        # 该令牌只能调用 /api/auth/approvals/*，不能访问任何业务 API。
        if method == "POST" and route in ("wechat/login", "wechat/session"):
            if not AUTH_ENABLED or not WECHAT_CHALLENGE_ENABLED:
                return self._err(403, "微信确认器已关闭", "AUTH_DISABLED")
            config_error = auth_config_error()
            if config_error:
                return self._err(503, "鉴权配置不完整：" + config_error, "AUTH_NOT_READY")
            identity, error, code = _wechat_code_to_identity(body.get("code"))
            if error:
                return self._err(401 if code == "INVALID_WECHAT_CODE" else 502, error, code)
            user = _upsert_wechat_user(conn, identity)
            if user["disabled"]:
                return self._err(403, "该平台账号已停用", "ACCOUNT_DISABLED")
            data = _issue_session(conn, user, scope=SCOPE_APPROVE)
            audit(conn, "wechat_approver_session", target="auth_users/%s" % user["id"],
                  result="role=%s scope=approve" % user["role"], actor="wechat")
            return self._ok(data)

        # ---------- 登录挑战（浏览器侧：发起 / 轮询 / 兑换） ----------
        if method == "POST" and route == "challenges":
            """兼容保留的微信登录挑战；默认由 CATMAT_WECHAT_CHALLENGE_ENABLED=0 关闭。"""
            if not AUTH_ENABLED or not WECHAT_CHALLENGE_ENABLED:
                return self._err(403, "微信确认器已关闭", "AUTH_DISABLED")
            if not (WECHAT_TEST_MODE or (WECHAT_APP_ID and WECHAT_APP_SECRET)):
                return self._err(503, "未配置微信确认通道，无法发起登录挑战", "AUTH_NOT_READY")
            ip = _client_ip(self.headers, self.client_address[0]) or "unknown"
            if not _rate_allow("challenge_create:" + ip, CHALLENGE_CREATE_LIMIT):
                return self._err(429, "登录请求过于频繁，请稍后再试", "RATE_LIMITED")
            data, error, code = _create_login_challenge(
                conn, client_hint=body.get("client_hint"), client_ip=ip)
            if error:
                return self._err(503, error, code)
            audit(conn, "login_challenge_create", target=data["challenge_id"],
                  params={"ip": ip}, result="pending", actor="web")
            return self._ok(data)

        if method == "POST" and route == "challenges/poll":
            """浏览器轮询挑战状态：pending / approved / denied / expired / consumed。"""
            if not AUTH_ENABLED or not WECHAT_CHALLENGE_ENABLED:
                return self._err(403, "微信确认器已关闭", "AUTH_DISABLED")
            row = _find_challenge(conn, body.get("challenge_id"), body.get("challenge_secret"))
            if not row:
                return self._err(404, "登录请求不存在或已失效", "CHALLENGE_NOT_FOUND")
            now = int(time.time())
            status = row["status"]
            if status in ("pending", "approved") and row["expires_at"] <= now:
                status = "expired"
                conn.execute(
                    "UPDATE auth_login_challenges SET status='expired' "
                    "WHERE id=? AND status IN ('pending','approved')", (row["id"],))
            return self._ok({
                "status": status,
                "user_code": row["user_code"],
                "expires_in": max(0, row["expires_at"] - now),
                "interval": CHALLENGE_POLL_INTERVAL,
            })

        if method == "POST" and route == "challenges/exchange":
            """用 challenge_secret 兑换业务会话（兼容保留，通道关闭时不可用）。"""
            if not AUTH_ENABLED or not WECHAT_CHALLENGE_ENABLED:
                return self._err(403, "微信确认器已关闭", "AUTH_DISABLED")
            row = _find_challenge(conn, body.get("challenge_id"), body.get("challenge_secret"))
            if not row:
                return self._err(404, "登录请求不存在或已失效", "CHALLENGE_NOT_FOUND")
            if row["status"] == "consumed":
                return self._err(409, "该登录请求已被使用", "CHALLENGE_CONSUMED")
            if row["status"] == "denied":
                return self._err(403, "登录请求已被拒绝", "CHALLENGE_DENIED")
            if row["status"] == "expired" or row["expires_at"] <= int(time.time()):
                return self._err(410, "登录请求已超时，请重新发起", "CHALLENGE_EXPIRED")
            if row["status"] == "pending":
                # 尚未被批准：不报错，让前端按建议间隔继续轮询
                return self._ok({"status": "pending", "ready": False,
                                 "interval": CHALLENGE_POLL_INTERVAL})
            approver = conn.execute("SELECT * FROM auth_users WHERE id=?",
                                    (row["approver_user_id"],)).fetchone()
            if not approver or approver["disabled"]:
                return self._err(403, "审批账号不可用，请重新发起登录", "ACCOUNT_DISABLED")
            payload, session_id = _issue_session_ex(
                conn, approver, mode="wechat", scope=row["scope"] or SCOPE_PLATFORM)
            if not _consume_challenge(conn, row, session_id):
                conn.rollback()  # 并发下已被兑换：撤销刚签发的会话，不留下孤儿会话
                return self._err(409, "该登录请求已被使用", "CHALLENGE_CONSUMED")
            audit(conn, "login_challenge_exchange", target=row["id"],
                  params={"user_code": row["user_code"], "ip": row["client_ip"]},
                  result="uid=%s session=%s" % (approver["id"], session_id), actor="web")
            return self._ok(payload)

        # ---------- 审批端（微信小程序确认器：查码 / 批准 / 拒绝） ----------
        if method == "POST" and route == "approvals/lookup":
            """按人工短码查询待确认的登录请求（兼容保留，通道关闭时不可用）。"""
            if not AUTH_ENABLED or not WECHAT_CHALLENGE_ENABLED:
                return self._err(403, "微信确认器已关闭", "AUTH_DISABLED")
            _, approver, err = self._require_approver(conn)
            if err:
                return
            if not _rate_allow("approval_lookup:%s" % approver["id"], APPROVAL_LOOKUP_LIMIT):
                return self._err(429, "查询过于频繁，请稍后再试", "RATE_LIMITED")
            user_code = str(body.get("user_code") or "").strip().upper()
            if not user_code:
                return self._err(400, "请输入登录确认码", "INPUT_ERROR")
            _expire_stale_challenges(conn)
            row = conn.execute("SELECT * FROM auth_login_challenges WHERE user_code=?",
                               (user_code,)).fetchone()
            if not row or row["status"] in ("expired", "consumed"):
                audit(conn, "approval_lookup_miss", target=user_code, ok=False,
                      result="not found or closed", actor="approver:%s" % approver["id"])
                return self._err(404, "未找到待确认的登录请求，请核对确认码，或让网页端重新发起",
                                 "CHALLENGE_NOT_FOUND")
            return self._ok(_challenge_view(row, detail=True))

        if method == "POST" and route == "approvals/decide":
            """批准 / 拒绝一次登录请求（兼容保留，通道关闭时不可用）。"""
            if not AUTH_ENABLED or not WECHAT_CHALLENGE_ENABLED:
                return self._err(403, "微信确认器已关闭", "AUTH_DISABLED")
            _, approver, err = self._require_approver(conn)
            if err:
                return
            action = str(body.get("action") or "").strip().lower()
            if action not in ("approve", "deny"):
                return self._err(400, "action 必须为 approve 或 deny", "INPUT_ERROR")
            user_code = str(body.get("user_code") or "").strip().upper()
            challenge_id = str(body.get("challenge_id") or "").strip()
            _expire_stale_challenges(conn)
            row = None
            if challenge_id:
                row = conn.execute("SELECT * FROM auth_login_challenges WHERE id=?",
                                   (challenge_id,)).fetchone()
            elif user_code:
                row = conn.execute("SELECT * FROM auth_login_challenges WHERE user_code=?",
                                   (user_code,)).fetchone()
            if not row:
                return self._err(404, "未找到待确认的登录请求", "CHALLENGE_NOT_FOUND")
            if row["status"] != "pending":
                return self._err(409, "该登录请求已处理或已超时（%s）" % row["status"],
                                 "CHALLENGE_CLOSED")
            if not _decide_challenge(conn, row, approver["id"], action):
                return self._err(409, "该登录请求已处理，无法重复确认", "CHALLENGE_CLOSED")
            audit(conn, "login_challenge_%s" % ("approve" if action == "approve" else "deny"),
                  target=row["id"],
                  params={"user_code": row["user_code"], "ip": row["client_ip"],
                          "hint": row["client_hint"]},
                  result="by uid=%s" % approver["id"], actor="approver:%s" % approver["id"])
            return self._ok({"challenge_id": row["id"], "user_code": row["user_code"],
                             "status": "approved" if action == "approve" else "denied"})

        # ---------- 本地账号管理（东翁，或获「账号管理」授权的掌书） ----------
        if route == "users" and method == "GET":
            _, admin, err = self._require_admin(conn)
            if err:
                return
            rows = _local_accounts_by_role(conn)
            return self._ok([_public_account(row) for row in rows])

        if route == "users" and method == "POST":
            _, admin, err = self._require_admin(conn)
            if err:
                return
            user, error, errcode = _create_local_user(
                conn, body.get("username"), body.get("password"),
                body.get("display_name"), body.get("role") or ROLE_GUEST,
                capabilities=body.get("capabilities"))
            if error:
                return self._err(409 if errcode == "USERNAME_EXISTS" else 400, error, errcode)
            audit(conn, "account_create", target="auth_users/%s" % user["id"],
                  params={"username": user["username"], "role": user["role"]},
                  result="created", actor="uid:%s" % admin["id"])
            _sync_account_role_files(conn)
            return self._ok(_public_account(user))

        user_match = re.fullmatch(r"users/(\d+)", route)
        if user_match and method == "PATCH":
            _, admin, err = self._require_admin(conn)
            if err:
                return
            target_id = int(user_match.group(1))
            target = conn.execute("SELECT * FROM auth_users WHERE id=? AND username IS NOT NULL",
                                  (target_id,)).fetchone()
            if not target:
                return self._err(404, "本地账号不存在", "NOT_FOUND")
            admin_norm = _norm_role(admin["role"])
            target_norm = _norm_role(target["role"])
            role = str(body.get("role", target["role"])).strip().lower()
            role_norm = _norm_role(role)
            if role_norm not in (ROLE_OWNER, ROLE_STEWARD, ROLE_GUEST):
                return self._err(400, "角色只能为 owner/steward/guest", "INPUT_ERROR")
            # 东翁专属保护：
            #   1) 掌书不得对东翁做任何变更（角色/停用/改名）；
            #   2) 只有东翁可册封 owner；本地东翁恒为一人。
            if target_norm == ROLE_OWNER and admin_norm != ROLE_OWNER:
                return self._err(403, "东翁的身份只有东翁本人可变更", "OWNER_ONLY")
            if role_norm == ROLE_OWNER and admin_norm != ROLE_OWNER:
                return self._err(403, "册封东翁仅东翁本人可行", "OWNER_ONLY")
            owner_count = conn.execute(
                "SELECT COUNT(*) c FROM auth_users WHERE role='owner' AND username IS NOT NULL"
            ).fetchone()["c"]
            if role_norm == ROLE_OWNER and owner_count >= 1:
                is_owner_self_keep = (target_norm == ROLE_OWNER and target_id == admin["id"])
                if not is_owner_self_keep:
                    return self._err(409, "东翁仅此一位，不可再设；让位请走传印流程", "OWNER_UNIQUE")
            if "disabled" in body and not isinstance(body["disabled"], bool):
                return self._err(400, "disabled 必须为布尔值", "INPUT_ERROR")
            disabled_target = bool(body.get("disabled", bool(target["disabled"])))
            disabled = 1 if disabled_target else 0
            # 保护最后一个本地东翁不被停用/降级
            if target_norm == ROLE_OWNER and (role_norm != ROLE_OWNER or disabled_target):
                active_owners = conn.execute(
                    "SELECT COUNT(*) c FROM auth_users WHERE username IS NOT NULL "
                    "AND role='owner' AND disabled=0").fetchone()["c"]
                if active_owners <= 1:
                    return self._err(409, "不能停用或降级最后一个东翁", "LAST_OWNER")
            display_name = str(body.get("display_name", target["display_name"])).strip()[:64]
            if not display_name:
                return self._err(400, "显示名称不能为空", "INPUT_ERROR")
            # 能力包：仅掌书持有；设为清客/东翁时清空
            if role_norm == ROLE_STEWARD:
                capabilities = _validate_capabilities(body.get("capabilities"))
            else:
                capabilities = []
            conn.execute(
                "UPDATE auth_users SET display_name=?,role=?,disabled=?,capabilities=? WHERE id=?",
                (display_name, role_norm, disabled,
                 json.dumps(capabilities, ensure_ascii=False), target_id))
            if disabled or role_norm != target_norm:
                conn.execute("UPDATE auth_sessions SET revoked=1,last_used_at=? WHERE user_id=?",
                             (now_str(), target_id))
            updated = conn.execute("SELECT * FROM auth_users WHERE id=?", (target_id,)).fetchone()
            audit(conn, "account_update", target="auth_users/%s" % target_id,
                  params={"role": role_norm, "disabled": bool(disabled),
                          "capabilities": capabilities}, result="updated",
                  actor="uid:%s" % admin["id"])
            _sync_account_role_files(conn)
            return self._ok(_public_account(updated))

        password_match = re.fullmatch(r"users/(\d+)/password", route)
        if password_match and method == "POST":
            _, admin, err = self._require_admin(conn)
            if err:
                return
            target_id = int(password_match.group(1))
            target = conn.execute("SELECT * FROM auth_users WHERE id=? AND username IS NOT NULL",
                                  (target_id,)).fetchone()
            if not target:
                return self._err(404, "本地账号不存在", "NOT_FOUND")
            if _norm_role(target["role"]) == ROLE_OWNER and _norm_role(admin["role"]) != ROLE_OWNER:
                return self._err(403, "东翁的密码只有东翁本人可重置", "OWNER_ONLY")
            _, error = _validate_local_credentials(target["username"], body.get("password"))
            if error:
                return self._err(400, error, "INPUT_ERROR")
            conn.execute("UPDATE auth_users SET password_hash=?,password_updated_at=? WHERE id=?",
                         (_hash_password(body["password"]), now_str(), target_id))
            conn.execute("UPDATE auth_sessions SET revoked=1,last_used_at=? WHERE user_id=?",
                         (now_str(), target_id))
            audit(conn, "account_password_reset", target="auth_users/%s" % target_id,
                  result="sessions revoked", actor="uid:%s" % admin["id"])
            _sync_account_role_files(conn)
            return self._ok({"id": target_id, "username": target["username"], "sessions_revoked": True})

        if method == "POST" and route in ("local/refresh", "wechat/refresh", "admin/refresh"):
            if not AUTH_ENABLED:
                return self._err(403, "鉴权功能未启用", "AUTH_DISABLED")
            refresh_token = (body.get("refresh_token") or "").strip()
            if not refresh_token:
                return self._err(401, "刷新凭证无效", "INVALID_REFRESH_TOKEN")
            now = int(time.time())
            session = conn.execute(
                "SELECT s.*,u.openid,u.unionid,u.display_name,u.role,u.disabled "
                "FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id "
                "WHERE s.refresh_token_hash=?", (_token_hash(refresh_token),)).fetchone()
            if not session:
                return self._err(401, "刷新凭证无效", "INVALID_REFRESH_TOKEN")
            if session["revoked"] or session["expires_at"] <= now:
                return self._err(403, "刷新凭证已失效", "REFRESH_REVOKED")
            if session["disabled"]:
                return self._err(403, "该平台账号已停用", "ACCOUNT_DISABLED")
            if route == "admin/refresh" and session["openid"] != ADMIN_HOLDER_OPENID:
                return self._err(401, "刷新凭证无效", "INVALID_REFRESH_TOKEN")
            if route == "local/refresh" and not session["openid"].startswith("local:"):
                return self._err(401, "刷新凭证无效", "INVALID_REFRESH_TOKEN")
            if route == "wechat/refresh" and (session["openid"] == ADMIN_HOLDER_OPENID or
                                                session["openid"].startswith("local:")):
                return self._err(401, "刷新凭证无效", "INVALID_REFRESH_TOKEN")
            conn.execute("UPDATE auth_sessions SET revoked=1,last_used_at=? WHERE id=?",
                         (now_str(), session["id"]))
            user = dict(session)
            user["id"] = session["user_id"]
            mode = ("admin" if session["openid"] == ADMIN_HOLDER_OPENID else
                    "local" if session["openid"].startswith("local:") else "wechat")
            try:  # 刷新严格继承原 scope：确认器令牌不会因刷新而获得业务权限
                scope = session["scope"] or SCOPE_PLATFORM
            except (IndexError, KeyError):
                scope = SCOPE_PLATFORM
            return self._ok(_issue_session(conn, user, mode=mode, scope=scope))

        if method == "POST" and route == "logout":
            refresh_token = (body.get("refresh_token") or "").strip()
            if refresh_token:
                conn.execute("UPDATE auth_sessions SET revoked=1,last_used_at=? WHERE refresh_token_hash=?",
                             (now_str(), _token_hash(refresh_token)))
            return self._ok()

        if method == "GET" and route == "me":
            if not AUTH_ENABLED:
                return self._err(403, "鉴权功能未启用", "AUTH_DISABLED")
            # _route 对 auth 公共域不做统一门禁，因此 me 在端点内独立校验。
            claims, error = self._authenticate_access_token()
            if error:
                return self._err(401, "登录已过期" if error == "TOKEN_EXPIRED" else "未登录", error)
            user = conn.execute("SELECT * FROM auth_users WHERE id=?", (claims.get("uid"),)).fetchone()
            if not user or user["disabled"]:
                return self._err(403, "该平台账号不可用", "ACCOUNT_DISABLED")
            sid = claims.get("sid")
            if not sid:
                return self._err(401, "旧版登录会话已失效，请使用账号密码重新登录", "SESSION_REVOKED")
            active = conn.execute(
                "SELECT 1 FROM auth_sessions WHERE id=? AND user_id=? AND revoked=0 AND expires_at>?",
                (sid, user["id"], int(time.time()))).fetchone()
            if not active:
                return self._err(401, "登录会话已被撤销，请重新登录", "SESSION_REVOKED")
            data = _public_user(user, mode=claims.get("mode") or "local")
            data["exp"] = claims["exp"]
            data["scope"] = sorted(_claims_scope(claims))
            return self._ok(data)

        return self._err(404, "未知 auth 端点", "NOT_FOUND")

    def _llm_gen_inp(self, body):
        description = body.get("description") or ""
        calc_type = body.get("calc_type", "geo_opt")
        structure = body.get("structure") or None
        dispersion = body.get("dispersion", "d3bj")
        smearing = bool(body.get("smearing", True))

        # 1) 若有结构，先用 Multiwfn 生成基础模板作参考
        base_inp = None
        if structure:
            base_inp, _ = generate_multiwfn_inp(structure, calc_type, dispersion, smearing)

        # 2) 用大模型按自然语言描述定制
        cfg = load_llm_config()
        llm_ready = bool(cfg.get("base_url") and cfg.get("model") and cfg.get("api_key"))
        if not llm_ready:
            if base_inp:
                return self._ok({"inp": base_inp, "source": "multiwfn",
                                 "note": "未配置大模型，已返回 Multiwfn 默认模板"})
            return self._err(502, "未配置大模型且无结构可生成 INP，请先配置模型或提供结构")

        prompt = (
            "你是 CP2K 输入文件专家。请根据用户的自然语言描述，生成或修改一个完整的 CP2K 输入文件（.inp）。\n"
            "要求：输出完整的 &GLOBAL / &FORCE_EVAL / &SUBSYS(含 CELL/COORD/KIND) / &MOTION 结构；"
            "关键词合法、参数取值合理（截断能、k点、色散、Smearing、SCF 收敛、基组/赝势等）；"
            "只输出 INP 内容本身，不要任何解释或 markdown 代码块标记。\n"
        )
        if base_inp:
            prompt += "以下是 Multiwfn 生成的基础模板，请在此基础上按用户描述修改：\n" + base_inp + "\n\n"
        prompt += "用户描述：" + (description or "（未提供，请给出合理默认）")

        content, err = llm_call([{"role": "user", "content": prompt}], cfg, temperature=0.2)
        if err:
            if base_inp:
                return self._ok({"inp": base_inp, "source": "multiwfn",
                                 "note": "大模型调用失败，已回退到 Multiwfn 模板：" + err})
            return self._err(502, err)
        return self._ok({"inp": extract_inp(content), "source": "llm", "model": cfg.get("model")})

    # ---------------- 知识库整理 / 资料库同步 ----------------
    def _sync(self, method, seg, qs, conn, body):
        if method != "POST":
            return self._err(405, "仅支持 POST")
        # 需求：CP2K 知识库仅保留与 CP2K 相关的资料库内容，去除杂乱信息。
        # 1) 清理既往从 memory 日志 / 非 CP2K 资料导入的"研究资料"类杂乱条目
        cleaned = 0
        try:
            cur = conn.execute("SELECT COUNT(*) c FROM knowledge WHERE category IN ('研究资料','未分类')").fetchone()
            cleaned = cur["c"] if cur else 0
            conn.execute("DELETE FROM knowledge WHERE category IN ('研究资料','未分类')")
        except Exception:
            cleaned = 0
        # 2) 从工作区资料库 md 中提取与 CP2K 计算相关的片段（按关键词过滤），分类归档
        CP2K_KEYWORDS = ("CP2K", "DFT", "基组", "泛函", "赝势", "截断", "k点", "KPOINT",
                         "Smearing", "PDOS", "DOS", "GEO_OPT", "CELL_OPT", "Fukui", "局域软度",
                         "d带", "态密度", "ASE", "AiiDA", "Multiwfn", "MGRID", "SCF", "XC_",
                         "MIXING", "VDW", "色散", "吸附能", "描述符")
        imported = 0
        for label, path in RESOURCE_FILES:
            if not os.path.isfile(path):
                continue
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            # 按标题拆分段落，仅保留含 CP2K 计算关键词的段落
            sections = []
            cur_title, cur_body = label, []
            for line in content.splitlines():
                if line.startswith("#"):
                    if cur_body:
                        sections.append((cur_title, "\n".join(cur_body).strip()))
                    cur_title = line.lstrip("# ").strip() or label
                    cur_body = []
                else:
                    cur_body.append(line)
            if cur_body:
                sections.append((cur_title, "\n".join(cur_body).strip()))
            for title, body_text in sections:
                if not body_text:
                    continue
                if not any(kw in body_text for kw in CP2K_KEYWORDS):
                    continue
                # 去重：按 source + topic
                exist = conn.execute("SELECT COUNT(*) c FROM knowledge WHERE source=? AND topic=?",
                                     (path, title[:120])).fetchone()["c"]
                if exist:
                    continue
                conn.execute(
                    "INSERT INTO knowledge(category,topic,keyword,recommended,rationale,source) VALUES(?,?,?,?,?,?)",
                    ("CP2K 计算流程", title[:120], None, None, body_text[:4000], path))
                imported += 1
        remaining = conn.execute("SELECT COUNT(*) c FROM knowledge").fetchone()["c"]
        return self._ok({"cleaned": cleaned, "imported": imported, "remaining": remaining})

    # ---------------- 导入 ----------------
    def _import(self, method, seg, qs, conn, body):
        if method != "POST":
            return self._err(405, "仅支持 POST")
        kind = body.get("kind")
        rows = body.get("rows") or []
        cnt = 0
        if kind == "systems":
            for s in rows:
                _insert_system(conn, s); cnt += 1
        elif kind == "literature":
            for it in rows:
                _insert_literature(conn, it); cnt += 1
        elif kind == "knowledge":
            for it in rows:
                _insert_knowledge(conn, it); cnt += 1
        else:
            return self._err(400, "kind 需为 systems/literature/knowledge")
        return self._ok({"imported": cnt})

def interpret_literature_core(conn, lid, archived_by=None):
    """用大模型深度解读文献：全面具体的结论总结 + 亮点/可取之处陈列，回写并归档。

    archived_by：归档用户名标签。网页端由当前登录账号提供；AI 助手调用记「AI 助手(休go)」。
    解读产生的知识库条目一律带该标签，便于追溯是谁归档解读结论。
    """
    r = conn.execute("SELECT * FROM literature WHERE id=?", (lid,)).fetchone()
    if not r:
        return {"error": "文献不存在"}
    cfg = load_llm_config()
    if not (cfg.get("base_url") and cfg.get("model") and cfg.get("api_key")):
        return {"error": "未配置大模型 API，无法解读（请在「平台设置 → 大模型」中配置后重试）"}
    title = r["title"] or ""
    abstract = r["abstract"] or ""
    journal = r["journal"] or ""
    year = r["year"] or ""
    prompt = (
        "你是催化材料文献深度分析专家。请根据下面的文献标题、期刊与摘要，用 JSON 输出以下字段（只输出 JSON，不要多余文字）：\n"
        "{\n"
        "  \"material_system\": \"材料体系（如 NiFe-LDH/NF，含组分与载体）\",\n"
        "  \"electrolyzer\": \"电解槽类型(PEM酸性/碱性/AEM/其他/未知)\",\n"
            "  \"overpotential_10\": 数值或null, \"tafel_slope\": 数值或null, \"stability_hours\": 数值或null,\n"
            "  \"stability_condition\": \"稳定性测试条件（如：恒流 10 mA/cm² 下 0.5M H₂SO₄ 酸性电解液中运行 30h，电位上升 <20mV），无则 null\",\n"
            "  \"dft_code\": \"计算软件(VASP/CP2K/QE/实验/未知)\", \"uses_cp2k\": true或false,\n"
        "  \"conclusion\": \"结论总结：要求全面具体、层次分明，用 3-5 句话分点概括（① 研究问题与创新点 ② 关键实验结果与数值 ③ 机理解释/理论计算结论 ④ 性能对比与意义），中文，总字数 200-400 字\",\n"
        "  \"highlights\": [\"可取之处/亮点1（如：实验与DFT结合揭示机理）\", \"亮点2\", \"亮点3\", \"亮点4\"],  // 3-5 条，每条 15-40 字，聚焦方法论与可借鉴之处\n"
        "  \"tags\": [\"标签1\",\"标签2\",\"标签3\"]\n"
        "}\n"
        "标题：%s\n期刊：%s（%s）\n摘要：%s" % (title, journal, year, (abstract or "（无摘要）")[:3000])
    )
    content, err = llm_call([{"role": "user", "content": prompt}], cfg, temperature=0.2)
    if err:
        return {"error": err}
    m = re.search(r"\{.*\}", content, re.S)
    try:
        data = json.loads(m.group(0)) if m else json.loads(content)
    except Exception:
        return {"error": "解读结果解析失败", "raw": content[:400]}
    # 回写文献
    sets, vals = [], []
    for k in ("material_system", "electrolyzer", "conclusion", "dft_code"):
        if data.get(k):
            sets.append(k + "=?"); vals.append(data[k])
    for k in ("overpotential_10", "tafel_slope", "stability_hours"):
        if data.get(k) is not None:
            try:
                vals.append(float(data[k]))
                sets.append(k + "=?")
            except (TypeError, ValueError):
                pass
    sets.append("uses_cp2k=?"); vals.append(1 if data.get("uses_cp2k") else 0)
    sets.append("verified=?"); vals.append(1)
    if data.get("tags"):
        sets.append("tags=?"); vals.append(json.dumps(data["tags"], ensure_ascii=False))
    hl = data.get("highlights")
    if isinstance(hl, list):
        sets.append("highlights=?"); vals.append(json.dumps(hl, ensure_ascii=False))
    if data.get("stability_condition"):
        sets.append("stability_condition=?"); vals.append(str(data["stability_condition"]))
    # 完整解读文本（结论 + 亮点陈列，供详情页展示）
    interpret_full = (data.get("conclusion") or "") + "\n\n【可取之处与亮点】\n" + \
        "\n".join("• " + str(h) for h in (hl or []))
    sets.append("interpret_full=?"); vals.append(interpret_full)
    sets.append("interpreted_by=?"); vals.append(archived_by)
    vals.append(lid)
    conn.execute("UPDATE literature SET " + ",".join(sets) + " WHERE id=?", vals)
    # 归档到知识库（作为 CP2K 相关结论条目，带归档用户名标签便于追溯）
    conn.execute(
        "INSERT INTO knowledge(category,topic,keyword,recommended,rationale,source,origin,archived_by) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("文献解读", title[:120], data.get("material_system"), interpret_full[:400],
         "材料体系 %s · 电解槽 %s · η₁₀ %s · 亮点 %s" % (
             data.get("material_system", "—"), data.get("electrolyzer", "—"),
             data.get("overpotential_10", "—"), len(hl or [])),
         "DOI " + (r["doi"] or ""), "文献", archived_by))
    return {"material_system": data.get("material_system"),
            "conclusion": data.get("conclusion"),
            "highlights": hl or [],
            "stability_condition": data.get("stability_condition"),
            "interpret_full": interpret_full,
            "archived": True,
            "archived_by": archived_by}




class ApiError(Exception):
    def __init__(self, code, msg):
        self.code = code
        self.msg = msg


# ==================== 数据库备份（整改 [高3] P0-3）====================
def backup_db(tag=None):
    """把数据库备份到 backups/catmat_YYYYMMDD[_tag].db。
    优先用 VACUUM INTO 生成一致性快照；旧版 SQLite 降级为 WAL checkpoint + 文件拷贝。"""
    try:
        if not os.path.exists(DB_PATH):
            return None
        os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
        try:
            os.chmod(BACKUP_DIR, 0o700)
            for existing in os.listdir(BACKUP_DIR):
                if existing.endswith(".db"):
                    _harden_private_file(os.path.join(BACKUP_DIR, existing))
        except OSError:
            pass
        stamp = time.strftime("%Y%m%d")
        name = "catmat_%s%s.db" % (stamp, ("_" + tag) if tag else "")
        dst = os.path.join(BACKUP_DIR, name)
        conn = sqlite3.connect(DB_PATH)
        try:
            try:
                conn.execute("VACUUM INTO ?", (dst,))
            except sqlite3.OperationalError:
                # 无 VACUUM INTO（SQLite < 3.27）：先截断 WAL 再整体拷贝
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                shutil.copy2(DB_PATH, dst)
        finally:
            conn.close()
        _harden_private_file(dst)
        try:
            os.chmod(BACKUP_DIR, 0o700)
        except OSError:
            pass
        LOG.info("数据库已备份 → %s", dst)
        return dst
    except Exception:
        log_exc("数据库备份失败")
        return None


def daily_backup_if_needed():
    """每次启动检查：今日备份不存在则自动备份一次（同日重复启动不重复备份）。"""
    try:
        if not os.path.exists(DB_PATH):
            return None
        stamp = time.strftime("%Y%m%d")
        dst = os.path.join(BACKUP_DIR, "catmat_%s.db" % stamp)
        if os.path.exists(dst):
            LOG.info("今日备份已存在，跳过：%s", dst)
            return dst
        return backup_db()
    except Exception:
        log_exc("每日备份检查失败")
        return None


# ==================== 目录卫生（整改 [低3] P2-5）====================
# 只清理平台自身产生的临时产物：.tmp 下超过保留期的项 + 根目录 __pycache__。
# 绝不触碰 cp2k_runs（计算输入/输出，用户资产）与 backups（备份，数据安全底线）。
TMP_RETAIN_DAYS = 7


def cleanup_dirs(retain_days=TMP_RETAIN_DAYS):
    """启动时执行一次目录卫生清理。返回移除的条目数。"""
    removed = 0
    try:
        cutoff = time.time() - retain_days * 86400
        if os.path.isdir(TMP_DIR):
            for name in os.listdir(TMP_DIR):
                p = os.path.join(TMP_DIR, name)
                try:
                    if os.stat(p).st_mtime >= cutoff:
                        continue          # 保留期内，跳过（避免误删正在使用的临时文件）
                    if os.path.isdir(p):
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        os.remove(p)
                    removed += 1
                except Exception:
                    pass
        pc = os.path.join(BASE_DIR, "__pycache__")
        if os.path.isdir(pc):
            shutil.rmtree(pc, ignore_errors=True)
            removed += 1
        if removed:
            LOG.info("目录卫生：移除 %d 项（.tmp 保留 %d 天 + __pycache__）", removed, retain_days)
    except Exception:
        log_exc("目录卫生清理失败")
    return removed


def _startup_sync_memory():
    """启动时把平台知识库增量同步进休go记忆（后台线程，不阻塞服务启动）。"""
    try:
        c = get_db()
        try:
            sync_knowledge_to_agent_memory(c)
        finally:
            c.close()
    except Exception:
        log_exc("启动时知识库 → 休go记忆 同步失败")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--reset", action="store_true", help="删除数据库重新初始化")
    ap.add_argument("--yes", action="store_true",
                    help="与 --reset 配合：跳过交互式确认（用于脚本/自动化）")
    ap.add_argument("--seed", action="store_true", help="导入 data/ 下的种子数据（默认空白初始状态）")
    args = ap.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1") and not AUTH_ENABLED:
        ap.error("非回环地址监听必须启用 CATMAT_AUTH_ENABLED=1 并配置鉴权")

    # 鉴权启用时启动即校验必要配置，避免服务处于“门禁开启但无人可登录”的锁死状态。
    config_error = auth_config_error()
    if config_error:
        ap.error("鉴权配置错误：%s。生产环境至少需 CATMAT_JWT_SECRET，并启用本地账号或其它登录通道。" % config_error)

    # 日志落盘（[高2]）：最早初始化，之后所有异常/关键动作均有迹可循
    setup_logging()

    if args.reset:
        if not os.path.exists(DB_PATH):
            print("未发现数据库，跳过 --reset：%s" % DB_PATH)
        else:
            if not args.yes:
                print("⚠ 即将删除数据库：%s" % DB_PATH)
                print("  该操作不可逆；执行前会自动备份一份到 backups/。")
                try:
                    ans = input("  确认请输入 yes（其它任意输入取消）：").strip().lower()
                except EOFError:      # 无交互终端（如 nohup）：按取消处理
                    ans = ""
                if ans != "yes":
                    print("已取消，未做任何改动。")
                    LOG.warning("--reset 被用户取消或缺少交互终端")
                    return
            b = backup_db(tag="before_reset")
            if not b:
                print("⚠ 删前备份失败，为安全起见已中止 reset（数据库未被删除）。")
                LOG.error("--reset 中止：删前备份失败")
                return
            print("删前备份完成：%s" % b)
            # [高5] 审计：删库前留痕（库删除后 audit 表随之消失，故必须提前写）
            try:
                _c = get_db()
                audit(_c, "db_reset", target=DB_PATH,
                      params={"backup": b, "auto_yes": bool(args.yes)}, ok=True)
                _c.close()
            except Exception:
                log_exc("审计 db_reset 写入失败")
            os.remove(DB_PATH)
            print("已删除数据库：%s" % DB_PATH)
            LOG.warning("数据库已被 --reset 删除（删前备份：%s）", b)

    init_db()
    if args.seed:
        seed_if_empty()
    # 后台守护线程：每日首次启动自动备份数据库（[高3]），避免阻塞启动
    try:
        import threading
        threading.Thread(target=daily_backup_if_needed, daemon=True).start()
    except Exception:
        log_exc("每日备份线程启动失败")

    # 后台守护线程：目录卫生（[低3]，清理 .tmp 过期产物与 __pycache__）
    try:
        import threading
        threading.Thread(target=cleanup_dirs, daemon=True).start()
    except Exception:
        log_exc("目录卫生线程启动失败")

    # 后台守护线程：预热版本探测缓存（[中2]，避免首个请求等待 10+ 秒）
    try:
        import threading
        threading.Thread(target=warm_versions, daemon=True).start()
    except Exception:
        log_exc("版本预热线程启动失败")

    # 后台守护线程：把平台知识库同步进休go记忆（知识库 ↔ 记忆 双向同步）
    try:
        import threading
        threading.Thread(target=_startup_sync_memory, daemon=True).start()
    except Exception:
        log_exc("知识库同步线程启动失败")

    # 后台守护线程：预刷新资源指标缓存（避免 2.5s 轮询触发 nvidia-smi 阻塞全局锁）
    try:
        import threading
        threading.Thread(target=_metrics_updater, daemon=True).start()
    except Exception:
        log_exc("指标预刷新线程启动失败")

    # 后台守护线程：CM- 内置任务队列调度（无外部调度器时按 FIFO + 资源余量执行）
    try:
        import threading
        threading.Thread(target=queue_scheduler_loop, daemon=True).start()
    except Exception:
        log_exc("CM- 队列调度线程启动失败")

    class _CatmatHTTPServer(ThreadingHTTPServer):
        """公网部署加固。

        - request_queue_size：socketserver 默认 backlog 仅 5，公网并发或经反向代理建连时
          极易丢弃新连接，表现为“时好时坏 / 后端离线”；此处提高到 128。
        - daemon_threads：工作线程设为守护线程，进程退出/重启时不必等待，避免重启卡住。
        - allow_reuse_address：重启后立即重绑端口，避免 TIME_WAIT 导致启动失败。
        """
        daemon_threads = True
        request_queue_size = 128
        allow_reuse_address = True

    srv = _CatmatHTTPServer((args.host, args.port), Handler)
    print("催化智构 CataMind 后端已启动 → http://%s:%d  (Ctrl+C 停止)" % (args.host, args.port))
    print("数据库: %s   AiiDA: %s" % (DB_PATH, AII_PY))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()


if __name__ == "__main__":
    main()
