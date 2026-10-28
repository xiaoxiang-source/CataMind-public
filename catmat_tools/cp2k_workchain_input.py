#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""高通量筛选 AiiDA 工作流「输入文件构建」唯一基线模块。

以 irru_adsorption_workchain.py 的参数设置作为**唯一基准**，将 CP2K 输入拆分为
两套相互独立的参数集：

  ① 结构优化参数  build_geo_opt_params()
     - RUN_TYPE = GEO_OPT，仅优化结构并输出优化后的结构；
     - 绝大多数计算级别参数沿用脚本原值；
     - 严禁包含任何 PDOS / Bader / d 带 输出请求。

  ② 单点能参数    build_single_point_params()
     - RUN_TYPE = ENERGY，承接优化后的结构；
     - 基组 / 赝势 / CUTOFF 等计算级别关键字与脚本完全一致；
     - 在此基础之上追加 PDOS / Bader 电荷（电子密度 cube）/ d 带中心 的输出请求；
     - PDOS / Bader / d 带 的**后处理**作为本任务之后的接续节点执行，不写入本参数集。

铁律：
  - 基组 / 赝势 / CUTOFF / MGRID / QS / XC / VDW / KIND 等「计算级别」参数固化于
    TEMPLATE 常量，禁止在流程中被任何选项覆盖（见 _reject_overrides / TemplateConflictError）；
  - PDOS / Bader / d 带 输出请求只允许写入单点能参数集，绝不写入结构优化参数集；
  - 当体系涉及 OT 算法时，仅调整两套参数中的「自洽求解（SCF）」设置，不改动任何计算级别参数；
  - 关键参数（elements）缺失，或元素不在固化 KIND_BASIS 模板中时，抛出明确异常。

两套参数数据结构（字段映射）
---------------------------------
TEMPLATE["OPT"] / TEMPLATE["SP"]  ──►  &FORCE_EVAL &DFT.*
  BASIS_SET_FILE_NAME / POTENTIAL_FILE_NAME / QS / MGRID(CUTOFF,REL_CUTOFF,NGRIDS)
  / XC / VDW / POISSON / SCF
TEMPLATE["OPT"]["MOTION_GEO_OPT"]  ──►  &MOTION &GEO_OPT   （仅结构优化任务提供）
KIND_BASIS[element]["OPT"|"SP"]     ──►  &SUBSYS &KIND（按元素 BASIS_SET + POTENTIAL）

文件命名规则（输出 / 后处理节点）
---------------------------------
GEO_OPT 任务标签 : GEO_OPT_{label}
单点能 任务标签 : SP_PDOS_{label}
PDOS 输出文件   : aiida-PDOS-{index}.pdos          （单点能任务产出，d 带中心输入）
电子密度 cube   : aiida-E_DENSITY_CUBE-{index}.cube（单点能任务产出，Bader 电荷输入）
Bader 后处理输出: bader_charge_{label}.dat          （接续节点）
d 带中心后处理  : d_band_center_{label}.dat         （接续节点）
"""
import copy
import re

__all__ = [
    "TEMPLATE", "KIND_BASIS", "NAMING", "FIELD_MAP",
    "build_geo_opt_params", "build_single_point_params", "build_cp2k_parameters",
    "WorkchainInputError", "TemplateConflictError", "MissingParamError",
    "resolve_site_kinds", "build_retrieve_settings",
]

# 位点级 KIND 名合法性（CP2K 标签：字母开头，字母/数字/下划线）
_KIND_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


# ---------------------------------------------------------------------------
# 异常：输入构建相关的明确报错
# ---------------------------------------------------------------------------
class WorkchainInputError(Exception):
    """输入构建失败的基础异常。"""


class TemplateConflictError(WorkchainInputError):
    """试图覆盖已固化的模板（计算级别）常量时抛出。"""


class MissingParamError(WorkchainInputError):
    """关键参数缺失或元素不在固化模板中时抛出。"""


# ---------------------------------------------------------------------------
# 固化模板常量（来自 irru_adsorption_workchain.py，禁止覆盖）
# ---------------------------------------------------------------------------
TEMPLATE = {
    # 结构优化（GEO_OPT）计算级别
    "OPT": {
        "BASIS_SET_FILE_NAME": "BASIS_MOLOPT",
        "POTENTIAL_FILE_NAME": "POTENTIAL",
        "MGRID": {"NGRIDS": 5, "CUTOFF": 400, "REL_CUTOFF": 55},
        "QS_EPS_DEFAULT": 1.0E-12,
        "XC": "PBE",
        "VDW": {"TYPE": "DFTD3(BJ)", "PARAMETER_FILE_NAME": "dftd3.dat",
                "REFERENCE_FUNCTIONAL": "PBE"},
        "POISSON": {"PERIODIC": "XY", "PSOLVER": "MT"},
        "SCF": {
            "MAX_SCF": 200, "EPS_SCF": "1.0e-6", "SCF_GUESS": "ATOMIC",
            "IGNORE_CONVERGENCE_FAILURE": "T", "ADDED_MOS": -1,
            "DIAGONALIZATION": {"ALGORITHM": "STANDARD"},
            "MIXING": {"METHOD": "BROYDEN_MIXING", "ALPHA": 0.4, "BETA": 1.5, "NBROYDEN": 8},
            "SMEAR": {"METHOD": "FERMI_DIRAC", "ELECTRONIC_TEMPERATURE": 300},
            "PRINT": {"RESTART": {"BACKUP_COPIES": 0}},
        },
        "MOTION_GEO_OPT": {
            "TYPE": "MINIMIZATION", "OPTIMIZER": "BFGS", "MAX_ITER": 1000,
            "MAX_DR": 3E-3, "RMS_DR": 1.5E-3, "MAX_FORCE": 4.5E-4, "RMS_FORCE": 3E-4,
            "BFGS": {"TRUST_RADIUS": 0.2},
        },
    },
    # 单点能（ENERGY）计算级别
    "SP": {
        "BASIS_SET_FILE_NAME": "BASIS_MOLOPT_UZH",
        "POTENTIAL_FILE_NAME": "POTENTIAL",
        "MGRID": {"NGRIDS": 5, "CUTOFF": 350, "REL_CUTOFF": 50},
        "QS_EPS_DEFAULT": 1.0e-11,
        "XC": "PBE",
        "VDW": {"TYPE": "DFTD3(BJ)", "PARAMETER_FILE_NAME": "dftd3.dat",
                "REFERENCE_FUNCTIONAL": "PBE"},
        "POISSON": {"PERIODIC": "XY", "PSOLVER": "MT"},
        "SCF": {
            "MAX_SCF": 200, "EPS_SCF": "5.0e-6", "SCF_GUESS": "ATOMIC",
            "IGNORE_CONVERGENCE_FAILURE": "T", "ADDED_MOS": -1,
            "DIAGONALIZATION": {"ALGORITHM": "STANDARD"},
            "MIXING": {"METHOD": "BROYDEN_MIXING", "ALPHA": 0.4, "BETA": 1.5, "NBROYDEN": 8},
            "SMEAR": {"METHOD": "FERMI_DIRAC", "ELECTRONIC_TEMPERATURE": 300},
            "PRINT": {"RESTART": {"BACKUP_COPIES": 0}},
        },
    },
}

# 固化 KIND 基组 / 赝势（来自脚本，禁止覆盖）。key = 元素符号。
# 基组名含价电子数（q17/q16/q6），与脚本严格一致。
KIND_BASIS = {
    "Ir": {"OPT": {"BASIS_SET": "DZVP-MOLOPT-SR-GTH-q17", "POTENTIAL": "GTH-PBE"},
           "SP":  {"BASIS_SET": "TZV2P-MOLOPT-PBE-GTH-q17", "POTENTIAL": "GTH-PBE"}},
    "Ru": {"OPT": {"BASIS_SET": "DZVP-MOLOPT-SR-GTH-q16", "POTENTIAL": "GTH-PBE"},
           "SP":  {"BASIS_SET": "TZV2P-MOLOPT-PBE-GTH-q16", "POTENTIAL": "GTH-PBE"}},
    "O":  {"OPT": {"BASIS_SET": "DZVP-MOLOPT-SR-GTH-q6",  "POTENTIAL": "GTH-PBE"},
           "SP":  {"BASIS_SET": "TZV2P-MOLOPT-PBE-GTH-q6",  "POTENTIAL": "GTH-PBE"}},
}

# 文件命名规则
NAMING = {
    "geo_opt_label": "GEO_OPT_{label}",
    "single_point_label": "SP_PDOS_{label}",
    "pdos_file": "aiida-PDOS-{index}.pdos",
    "density_cube": "aiida-E_DENSITY_CUBE-{index}.cube",
    "bader_out": "bader_charge_{label}.dat",
    "d_band_out": "d_band_center_{label}.dat",
}

# 字段映射（CP2K 输入层级 <- 模板常量）
FIELD_MAP = {
    "GLOBAL.RUN_TYPE": "GEO_OPT(优化) / ENERGY(单点能)",
    "FORCE_EVAL.DFT.BASIS_SET_FILE_NAME": "TEMPLATE[*].BASIS_SET_FILE_NAME",
    "FORCE_EVAL.DFT.POTENTIAL_FILE_NAME": "TEMPLATE[*].POTENTIAL_FILE_NAME",
    "FORCE_EVAL.DFT.MGRID": "TEMPLATE[*].MGRID (CUTOFF/REL_CUTOFF/NGRIDS)",
    "FORCE_EVAL.DFT.QS.EPS_DEFAULT": "TEMPLATE[*].QS_EPS_DEFAULT",
    "FORCE_EVAL.DFT.XC": "TEMPLATE[*].XC + VDW",
    "FORCE_EVAL.DFT.POISSON": "TEMPLATE[*].POISSON",
    "FORCE_EVAL.DFT.SCF": "TEMPLATE[*].SCF（OT 时仅替换自洽求解部分）",
    "FORCE_EVAL.SUBSYS.KIND": "KIND_BASIS[element][level]",
    "MOTION.GEO_OPT": "TEMPLATE.OPT.MOTION_GEO_OPT（仅结构优化）",
    "FORCE_EVAL.DFT.PRINT.DOS/PDOS": "单点能请求 pdos/d_band 时追加",
    "FORCE_EVAL.DFT.PRINT.E_DENSITY_CUBE": "单点能请求 bader 时追加",
}

# CP2K 的 PROJECT 名由 aiida_cp2k 插件固定为 "aiida"，因此原始输出文件名是固定的。
# 第 1 个数字是 RUN 序号（单点能恒为 1）。以下为**实测**的真实文件名（CP2K 2026.2）：
#   · 总 DOS        : aiida-1.dos
#   · 分 k 点 PDOS  : aiida-k{1..N}-1.pdos   ← 每个 k 点一个文件，不是合并的 PDOS-1.pdos！
#   · 电荷密度 cube : aiida-ELECTRON_DENSITY-1_0.cube
# 注意：不同 CP2K 版本 / PRINT 设置下文件名可能不同，所以这里用 glob 通配符匹配，
# 避免写死文件名导致 retrieve 静默失败（AiiDA 对不存在的 retrieve 项不报错，直接跳过）。
_RAW_DOS_GLOB = "aiida-1.dos"
_RAW_PDOS_GLOB = "aiida-k*-1.pdos"
_RAW_CUBE_GLOB = "aiida-ELECTRON_DENSITY-1_0.cube"

# 最多检索多少个 k 点的 PDOS 文件（金属体系 k 点通常 5~20 个；取上限保证覆盖）
_MAX_KPOINTS_RETRIEVE = 32

# 我们希望在 AiiDA 的 retrieved FolderData 中看到的规范化文件名
RETRIEVE_TARGETS = {
    "dos": "aiida-1.dos",
    "pdos": "aiida-PDOS-1.pdos",
    "density_cube": "aiida-E_DENSITY_CUBE-1.cube",
}


def build_retrieve_settings(requests=("pdos", "bader", "d_band")):
    """构建 settings.additional_retrieve_list，把 PDOS / 电荷密度 cube / DOS 捞回 AiiDA。

    为什么必须显式设置
    ------------------
    aiida_cp2k 的 retrieve_list 是**硬编码**的固定列表，只含：
        aiida.out / aiida-1.restart / aiida-pos-1.dcd
        aiida-pos-1.xyz / aiida-frc-1.xyz / aiida-1.cell
    PDOS 与 cube 只有在 settings.additional_retrieve_list 中列出才会被 retrieve。
    若漏设，CP2K 会正常算出文件、然后 AiiDA **直接丢弃**——表现为任务 Finished [0]、
    能量可读，但 retrieved 里只有 aiida.out，拿不到任何 PDOS/电荷密度数据。

    另一个坑：**retrieve 项若在远端不存在，AiiDA 不报错、静默跳过**。
    所以文件名必须写对（见上方实测文件名），否则会「成功但没文件」。

    :param requests: 输出请求集合（pdos / bader / d_band）
    :returns: dict，形如 {"additional_retrieve_list": [[src, dst, depth], ...]}

    注：retrieve_list 中的重命名指令必须是**三元组** [源, 目标, 深度]，
    二元组会触发 ValueError('invalid directive, not a list or tuple of length three')。
    """
    reqs = set(requests or [])
    lst = []
    if "pdos" in reqs or "d_band" in reqs:
        lst.append([_RAW_DOS_GLOB, RETRIEVE_TARGETS["dos"], 0])
        # 分 k 点 PDOS：逐个 k 点 retrieve，保持独立（后处理时按需求和）
        for ik in range(1, _MAX_KPOINTS_RETRIEVE + 1):
            lst.append(["aiida-k%d-1.pdos" % ik,
                        "aiida-k%d-1.pdos" % ik, 0])
    if "bader" in reqs:
        lst.append([_RAW_CUBE_GLOB, RETRIEVE_TARGETS["density_cube"], 0])
    return {"additional_retrieve_list": lst}


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
_PERIODIC_OK = ("XYZ", "XY", "NONE", "X", "Y", "Z", "XZ", "YZ")


def _norm_periodic(periodic):
    p = str(periodic or "XY").upper()
    if p not in _PERIODIC_OK:
        raise TemplateConflictError("非法周期边界 PERIODIC=%r（允许: %s）" % (periodic, "/".join(_PERIODIC_OK)))
    return p


def _validate_elements(elements):
    elems = [e.strip() for e in (elements or []) if e and str(e).strip()]
    if not elems:
        raise MissingParamError("构建 CP2K 输入缺少关键参数 elements（结构元素列表为空）")
    for e in elems:
        if not (e[0].isupper() and e.isalpha()):
            raise MissingParamError("elements 含非法元素标识: %r" % e)
    return elems


def _base_element(kind_name):
    """从位点级 KIND 名解析出真实元素：Ir1/Ir2/Ir_bulk -> Ir；Ru1 -> Ru；O -> O。

    CP2K 中 &KIND 的**名字可任意取**，元素由 &KIND 内的 ELEMENT 决定。
    因此同一种元素可按晶体学不等价位点拆成多个 KIND，各自输出独立 PDOS，
    从而得到「位点分辨」的局域软度 s_i 与 Fukui 函数 f_i。
    """
    s = str(kind_name)
    if s in KIND_BASIS:
        return s
    base = s.split("_")[0]
    for e in KIND_BASIS:
        if base.startswith(e):
            return e
    raise MissingParamError("无法从位点名 %r 解析元素（已知元素: %s）" % (kind_name, "/".join(sorted(KIND_BASIS))))


def _resolve_kinds(elements, level, kind_plan=None):
    """生成 &SUBSYS &KIND。

    两种模式：
      1. 元素级（默认，向后兼容）：kind_plan=None 时按元素建 KIND，
         `Ir/Ru/O` 各一个，PDOS 为元素平均——与原 PK 2015 行为完全一致。
      2. 位点级（kind_plan 提供时）：按位点名建 KIND，如 Ir1（表面层 Ir）、
         Ir2（次表层 Ir）、Ir_bulk（体相 Ir）。基组/赝势严格沿用其真实元素，
         保证化学一致性；CP2K 会为每个 KIND 单独输出一份 PDOS 文件。

    kind_plan 支持两种写法：
      · ["Ir1", "Ir2", "Ir_bulk", "Ru1", "Ru2", "Ru_bulk", "O"]  （显式位点列表）
      · {"Ir": ["Ir1", "Ir2"], ...}                              （按元素分组）
    位点名必须是合法 CP2K 标识符（字母/数字/下划线，字母开头）。
    """
    if not kind_plan:
        kinds = []
        for e in sorted(set(_validate_elements(elements))):
            if e not in KIND_BASIS:
                raise MissingParamError(
                    "元素 %s 不在固化模板 KIND_BASIS 中（当前仅覆盖 %s）。"
                    "基组/赝势为唯一基线，禁止自动推断；请扩展 cp2k_workchain_input.KIND_BASIS。"
                    % (e, "/".join(sorted(KIND_BASIS))))
            kinds.append({"_": e, **KIND_BASIS[e][level]})
        return kinds

    # 归一化为位点名列表
    names = []
    if isinstance(kind_plan, dict):
        for _e, lst in kind_plan.items():
            for n in (lst or []):
                names.append(str(n))
    else:
        names = [str(n) for n in kind_plan]
    if not names:
        raise MissingParamError("kind_plan 为空：未提供任何位点级 KIND 名")

    kinds = []
    seen = set()
    for name in names:
        if name in seen:
            raise TemplateConflictError("位点级 KIND 重名: %r" % name)
        seen.add(name)
        if not _KIND_NAME_RE.match(name):
            raise TemplateConflictError(
                "非法位点 KIND 名 %r（只允许字母/数字/下划线，且以字母开头）" % name)
        el = _base_element(name)
        if el not in KIND_BASIS:
            raise MissingParamError(
                "位点 %s 解析出的元素 %s 不在 KIND_BASIS 中（当前仅覆盖 %s）"
                % (name, el, "/".join(sorted(KIND_BASIS))))
        # CP2K 写法：&KIND Ir1 / ELEMENT Ir / BASIS_SET ... / POTENTIAL ...
        kinds.append({"_": name, "ELEMENT": el, **KIND_BASIS[el][level]})
    return kinds


# 禁止在流程中被覆盖的计算级别关键字（出现在调用方试图传入的 overrides 中即报错）
_BLOCKED_OVERRIDES = {
    "cutoff", "rel_cutoff", "ngrids", "max_scf", "eps_scf",
    "basis_set_file_name", "potential_file_name", "basis_set", "potential",
    "mgrid", "qs", "xc", "vdw", "periodic",
}


def _reject_overrides(overrides):
    bad = {k: v for k, v in (overrides or {}).items() if str(k).lower() in _BLOCKED_OVERRIDES}
    if bad:
        raise TemplateConflictError(
            "计算级别参数已固化为模板常量，禁止在流程中覆盖: %s" % bad)


def _build_xc(t):
    v = t["VDW"]
    return {
        "XC_FUNCTIONAL": {"_": t["XC"]},
        "VDW_POTENTIAL": {
            "POTENTIAL_TYPE": "PAIR_POTENTIAL",
            "PAIR_POTENTIAL": {
                "PARAMETER_FILE_NAME": v["PARAMETER_FILE_NAME"],
                "TYPE": v["TYPE"],
                "REFERENCE_FUNCTIONAL": v["REFERENCE_FUNCTIONAL"],
            },
        },
    }


def _build_scf(level, scf_method):
    """构建 SCF 段。scf_method='diag' 用对角化（模板默认）；'ot' 用 OT 求解。

    仅调整自洽求解相关设置；任何计算级别参数均不改动。
    """
    t = TEMPLATE[level]["SCF"]
    if (scf_method or "diag") == "ot":
        # OT：保留 MAX_SCF / EPS_SCF / SCF_GUESS / IGNORE_CONVERGENCE_FAILURE /
        # ADDED_MOS / PRINT（这些不是「计算级别」），仅以 OT + OUTER_SCF 替换
        # DIAGONALIZATION + MIXING + SMEAR。
        return {
            "MAX_SCF": t["MAX_SCF"],
            "EPS_SCF": t["EPS_SCF"],
            "SCF_GUESS": "ATOMIC",
            "IGNORE_CONVERGENCE_FAILURE": "T",
            "ADDED_MOS": -1,
            "OT": {
                "PRECONDITIONER": "FULL_ALL",
                "MINIMIZER": "DIIS",
                "LINESEARCH": "2PNT",
                "ALGORITHM": "STRICT",
            },
            "OUTER_SCF": {"MAX_SCF": 20, "EPS_SCF": "1.0e-6"},
            "PRINT": {"RESTART": {"BACKUP_COPIES": 0}},
        }
    # 对角化（模板默认）：完整沿用，禁止外部改动
    return copy.deepcopy(t)


# ---------------------------------------------------------------------------
# ① 结构优化参数集（GEO_OPT）
# ---------------------------------------------------------------------------
def build_geo_opt_params(elements, periodic="XY", scf_method="diag", label=None, **overrides):
    """构建结构优化（GEO_OPT）参数集。

    - 仅优化结构并输出优化后的结构；
    - 计算级别完全沿用 TEMPLATE["OPT"]；
    - 严禁包含 PDOS / Bader / d 带 等输出请求（它们属于单点能阶段）。
    """
    elems = _validate_elements(elements)
    _reject_overrides(overrides)  # 计算级别不可覆盖
    t = TEMPLATE["OPT"]
    dft = {
        "BASIS_SET_FILE_NAME": t["BASIS_SET_FILE_NAME"],
        "POTENTIAL_FILE_NAME": t["POTENTIAL_FILE_NAME"],
        "QS": {"EPS_DEFAULT": t["QS_EPS_DEFAULT"]},
        "MGRID": copy.deepcopy(t["MGRID"]),
        "XC": _build_xc(t),
        "POISSON": copy.deepcopy(t["POISSON"]),
        "SCF": _build_scf("OPT", scf_method),
    }
    params = {
        "GLOBAL": {"RUN_TYPE": "GEO_OPT", "PRINT_LEVEL": "LOW", "EXTENDED_FFT_LENGTHS": "T"},
        "FORCE_EVAL": {
            "METHOD": "Quickstep",
            "DFT": dft,
            "SUBSYS": {
                "CELL": {"PERIODIC": _norm_periodic(periodic)},
                "KIND": _resolve_kinds(elems, "OPT"),
            },
        },
        "MOTION": {"GEO_OPT": copy.deepcopy(t["MOTION_GEO_OPT"])},
    }
    return params


# ---------------------------------------------------------------------------
# ② 单点能参数集（ENERGY）
# ---------------------------------------------------------------------------
def build_single_point_params(elements, periodic="XY", scf_method="diag",
                              requests=("pdos", "bader", "d_band"), label=None,
                              kind_plan=None, cube_stride=1, **overrides):
    """构建单点能（ENERGY）参数集，承接优化后的结构。

    - 计算级别（基组 / 赝势 / CUTOFF 等）与 TEMPLATE["SP"] 完全一致；
    - requests 指定要在本任务 &DFT &PRINT 中追加的输出请求：
        'pdos'  -> &DOS(&PDOS COMPONENTS=T, NLUMO=-1)        （亦为 d 带中心所需）
        'bader' -> &E_DENSITY_CUBE STRIDE <cube_stride>      （Bader 电荷分析输入）
        'd_band'-> 依赖 PDOS（d 轨道投影），不另加 CP2K 打印
      PDOS / Bader / d 带 的**后处理**作为本任务之后的接续节点执行，不写入本参数集。
    - kind_plan：位点级 KIND 拆分（如 ["Ir1","Ir2","Ir3","Ru1","Ru2","Ru3","O"]）。
      为 None 时退化为元素级 KIND（与原 PK 2015 行为一致）；提供时按位点名建 KIND，
      每个位点单独输出 PDOS，用于位点分辨的局域软度 s_i 与 Fukui f_i。

      ⚠️ KIND 命名硬约束：kind 名必须是「元素符号」或「元素符号+纯整数」
      （如 Ir / Ir1 / Ir2）。AiiDA 的 Site.get_ase() 由 kind 名推导 ASE tag，
      继而决定 aiida.coords.xyz 里的原子标签；形如 "Ir_bulk" 的名字无法解析出
      整数 tag，会被 AiiDA 自动重新编号，导致 xyz 标签与 &KIND 定义不匹配、
      CP2K 报错。需要区分环境时请用纯整数编号（1=surface, 2=subsurf, 3=bulk）。

    - cube_stride：&E_DENSITY_CUBE 的 STRIDE。电荷密度 cube 体积随 STRIDE³ 反比
      减小。含大真空层（c 轴 70+ Å）的 slab 单胞在 STRIDE=1 下单个 cube 可达
      数 GB，128 结构即数百 GB —— 若仅需吸附位点附近的差分电荷，建议 STRIDE=2
      （体积降至 1/8）甚至 3。注意：total/slab/ads 三个 cube 必须用同一 STRIDE。
    - 计算级别常量禁止覆盖。
    """
    elems = _validate_elements(elements)
    _reject_overrides(overrides)
    requests = set(requests or [])
    unknown = requests - {"pdos", "bader", "d_band"}
    if unknown:
        raise TemplateConflictError("未知的输出请求类型: %s（允许: pdos/bader/d_band）" % sorted(unknown))
    if isinstance(cube_stride, bool) or not isinstance(cube_stride, (int, str)):
        raise WorkchainInputError(
            "cube_stride 必须是正整数（不允许 float/bool），收到: %r" % (cube_stride,))
    try:
        cube_stride = int(cube_stride)
    except (TypeError, ValueError):
        raise WorkchainInputError("cube_stride 必须是正整数，收到: %r" % (cube_stride,))
    if cube_stride < 1:
        raise WorkchainInputError("cube_stride 必须 >= 1，收到: %d" % cube_stride)
    # 位点名合法性预检（避免 "_bulk" 这类 AiiDA 无法解析 tag 的名字）
    if kind_plan:
        for kn in kind_plan:
            if not _KIND_NAME_RE.match(str(kn)):
                raise WorkchainInputError("非法 KIND 名: %r" % (kn,))
            if str(kn) != _base_element(str(kn)) and not re.match(
                    r"^%s\d+$" % re.escape(_base_element(str(kn))), str(kn)):
                raise WorkchainInputError(
                    "KIND 名 %r 不符合 AiiDA 标签推导规则：必须是「元素符号」或"
                    "「元素符号+纯整数」（如 Ir / Ir1 / Ir2）。"
                    "形如 'Ir_bulk' 会被 AiiDA 自动重新编号，导致 xyz 与 &KIND 不一致。"
                    % (kn,))
    t = TEMPLATE["SP"]
    # 注意：&RESTART 只能出现在 &SCF &PRINT 之下（已在 TEMPLATE 的 SCF 内）。
    # 直接放在 &DFT &PRINT 下会导致 CP2K 报
    #   "unknown subsection RESTART of section PRINT" 并 ABORT。
    # 因此这里只追加 &DFT &PRINT 层的合法子段（DOS / E_DENSITY_CUBE）。
    dft_print = {}
    if "pdos" in requests or "d_band" in requests:
        dft_print["DOS"] = {"NLUMO": -1, "PDOS": {"_": "T", "COMPONENTS": "T"}}
    if "bader" in requests:
        dft_print["E_DENSITY_CUBE"] = {"STRIDE": cube_stride}
    dft = {
        "BASIS_SET_FILE_NAME": t["BASIS_SET_FILE_NAME"],
        "POTENTIAL_FILE_NAME": t["POTENTIAL_FILE_NAME"],
        "QS": {"EPS_DEFAULT": t["QS_EPS_DEFAULT"]},
        "MGRID": copy.deepcopy(t["MGRID"]),
        "XC": _build_xc(t),
        "POISSON": copy.deepcopy(t["POISSON"]),
        "SCF": _build_scf("SP", scf_method),
        "PRINT": dft_print,
    }
    params = {
        "GLOBAL": {"RUN_TYPE": "ENERGY", "PRINT_LEVEL": "LOW", "EXTENDED_FFT_LENGTHS": "T"},
        "FORCE_EVAL": {
            "METHOD": "Quickstep",
            "DFT": dft,
            "SUBSYS": {
                "CELL": {"PERIODIC": _norm_periodic(periodic)},
                "KIND": _resolve_kinds(elems, "SP", kind_plan=kind_plan),
            },
        },
    }
    return params


# ---------------------------------------------------------------------------
# 位点级 KIND 自动推导（供 HTS / 单点能输入生成复用）
# ---------------------------------------------------------------------------
SURF_DEPTH_FRAC = 0.82
SUB_DEPTH_FRAC = 0.62


def resolve_site_kinds(symbols, positions, surf_frac=SURF_DEPTH_FRAC, sub_frac=SUB_DEPTH_FRAC):
    """由结构几何自动推导位点级 KIND。

    按**归一化几何深度**划分（对重构与未重构 slab 统一稳健，不依赖 z 分层）：

        depth = (z - z_min) / (z_max - z_min)      （z 统计仅取金属原子）
        surface : depth >= surf_frac      →  <元素>1
        subsurf : sub_frac <= depth < surf_frac → <元素>2
        bulk    : depth < sub_frac        →  <元素>_bulk
        O       : 吸附物种                →  O

    参数
      symbols  : 元素符号序列（len = natoms）
      positions: Nx3 直角坐标（Å）
      surf_frac/sub_frac : 深度阈值

    返回 (kinds_per_atom, kind_list)
      kinds_per_atom : 每个原子的 KIND 名（与输入同序，供写入 &COORD 标签）
      kind_list      : 去重排序后的 KIND 名列表（供 &KIND 段）
    """
    symbols = list(symbols)
    pos = [[float(x) for x in p] for p in positions]
    metal_mask = [s not in ("O",) for s in symbols]
    zs = [pos[i][2] for i in range(len(symbols)) if metal_mask[i]]
    if not zs:
        return list(symbols), sorted(set(symbols))
    zmin, zmax = min(zs), max(zs)
    span = max(zmax - zmin, 1e-9)

    per_atom = []
    for i, s in enumerate(symbols):
        if s == "O":
            per_atom.append("O")
            continue
        depth = (pos[i][2] - zmin) / span
        if depth >= surf_frac:
            per_atom.append("%s1" % s)
        elif depth >= sub_frac:
            per_atom.append("%s2" % s)
        else:
            per_atom.append("%s_bulk" % s)
    return per_atom, sorted(set(per_atom))


# ---------------------------------------------------------------------------
# 兼容分发器（供 server.py 既有调用路径复用）
# ---------------------------------------------------------------------------
_RUN_MAP = {"SCF": "ENERGY", "ENERGY": "ENERGY", "ENERGY_FORCE": "ENERGY_FORCE",
            "GEO_OPT": "GEO_OPT", "CELL_OPT": "CELL_OPT", "PDOS": "ENERGY", "DOS": "ENERGY",
            "MD": "MD", "BAND": "ENERGY", "VIB": "VIBRATIONAL_ANALYSIS",
            "CHARGE": "ENERGY", "CUBE": "ENERGY", "BADER": "ENERGY"}


def build_cp2k_parameters(calc_type, scf_method="diag", dispersion="d3bj", smearing=True,
                          elements=None, pdos=False, charge_density=False, periodic="XYZ",
                          cutoff=None, rel_cutoff=None, max_scf=None, eps_scf=None,
                          ngrids=None, vdw_parameter_file=None, requests=None, band_center=False):
    """兼容原签名的分发器：按 calc_type 路由到两套独立参数构建器。

    - GEO_OPT / CELL_OPT -> build_geo_opt_params（严禁 PDOS/Bader/d 带）；
    - 其余（ENERGY / SCF / PDOS / DOS / CHARGE / BADER / BAND...）-> build_single_point_params；
    - 基组 / 赝势 / CUTOFF / MGRID / SCF(收敛) / XC / VDW 等已固化为模板，禁止覆盖；
      传入非默认 dispersion / smearing 或与模板冲突的覆盖参数将抛出 TemplateConflictError。
    """
    ct = (calc_type or "GEO_OPT").upper()
    run_type = _RUN_MAP.get(ct, ct)
    elems = _validate_elements(elements)

    # 计算级别（基组/赝势/CUTOFF/MGRID/SCF 收敛盘/XC/VDW）已固化为模板常量。
    # 为兼容既有调用（前端默认仍可能下发与模板相同的 CUTOFF/REL_CUTOFF），
    # 仅当传入值与相应 level 的模板取值「不同」时才视为覆盖并报错；
    # 与模板一致或为 None 视为冗余，直接忽略，保证流程不被打断。
    level = "OPT" if run_type in ("GEO_OPT", "CELL_OPT") else "SP"
    t = TEMPLATE[level]
    _frozen = {
        "cutoff": (cutoff, t["MGRID"]["CUTOFF"]),
        "rel_cutoff": (rel_cutoff, t["MGRID"]["REL_CUTOFF"]),
        "ngrids": (ngrids, t["MGRID"]["NGRIDS"]),
        "max_scf": (max_scf, t["SCF"]["MAX_SCF"]),
        "eps_scf": (eps_scf, t["SCF"]["EPS_SCF"]),
        "vdw_parameter_file": (vdw_parameter_file, t["VDW"]["PARAMETER_FILE_NAME"]),
    }
    _conflict = {k: got for k, (got, exp) in _frozen.items()
                 if got is not None and str(got) != str(exp)}
    if _conflict:
        raise TemplateConflictError(
            "计算级别参数已固化为模板常量，禁止在流程中覆盖（与模板取值不同即视为覆盖）: %s" % _conflict)
    if dispersion not in (None, "d3bj"):
        raise TemplateConflictError("色散已固化为 DFTD3(BJ)（dftd3.dat），禁止覆盖: dispersion=%r" % dispersion)
    if smearing not in (None, True):
        raise TemplateConflictError("展宽已固化为 FERMI_DIRAC 300K，禁止覆盖: smearing=%r" % smearing)

    if run_type in ("GEO_OPT", "CELL_OPT"):
        if pdos or charge_density or ct in ("BADER", "CHARGE", "CUBE", "BAND"):
            raise TemplateConflictError("结构优化任务(GEO_OPT)禁止包含 PDOS/Bader/d 带 输出请求")
        return build_geo_opt_params(elems, periodic=periodic, scf_method=scf_method)

    # 单点能阶段：由标志/类型推导输出请求
    reqs = set(requests) if requests else set()
    if pdos or ct in ("PDOS", "DOS"):
        reqs.add("pdos")
    if charge_density or ct in ("CHARGE", "CUBE", "BADER"):
        reqs.add("bader")
    if band_center or ct in ("BAND",):
        reqs.add("d_band")
    return build_single_point_params(elems, periodic=periodic, scf_method=scf_method, requests=reqs)
