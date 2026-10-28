#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DP-GEN / 势函数延拓（DPGEN）闭环适配器。

平台「势函数延拓」模块与 DP-GEN 开源栈的解耦层。职责：
  1. 对 dpmd conda 环境中 dpgen / dp / lammps 命令的封装与调用；
  2. 中间产物解析（model_devi.out、lcurve.out）；
  3. 高偏差构型筛选（exploration 阶段）；
  4. 势函数精度评估与收敛判定（training / validation）；
  5. 配置校验与迭代状态机 drive_iteration（接收 executor 回调，便于单元测试注入 mock）。

平台侧（server.py 的 _dpgen_orchestrate 后台编排线程）通过本模块驱动
「训练 → 探索采样 → 智能筛选 → CP2K 高精度标注 → 扩充数据集 → 评估/收敛」闭环。
本模块只依赖标准库 + 外部二进制（subprocess），因此可在任意 Python 环境 import 测试纯逻辑。

执行协议（与 ml_train.py 一致）：作为脚本被 dpmd python 执行时，输出
    PROGRESS=<n> <msg>
末尾输出一行 JSON {"ok": true, "result": {...}} 或 {"ok": false, "error": "..."}
"""
import os
import re
import json
import math
import subprocess
from pathlib import Path

# ---------------------------------------------------------------------------
# dpmd 环境（DP-GEN 栈运行于此，与平台 base 环境隔离）
# ---------------------------------------------------------------------------
DPMD_PY = os.environ.get("CATMAT_DPMD_PY", os.path.expanduser("~/anaconda3/envs/dpmd/bin/python"))
DPGEN_BIN = os.environ.get("CATMAT_DPGEN_BIN", os.path.expanduser("~/anaconda3/envs/dpmd/bin/dpgen"))
DP_BIN = os.environ.get("CATMAT_DP_BIN", os.path.expanduser("~/anaconda3/envs/dpmd/bin/dp"))
LMP_BIN = os.environ.get("CATMAT_LMP_BIN", os.path.expanduser("~/anaconda3/envs/dpmd/bin/lmp"))

# 默认训练超参（经验值，用户可在平台配置覆盖；保持与 Ir–Ru 案例相近）
DEFAULT_TRAINING_PARAMS = {
    "descriptor": {"type": "se_e2_a", "sel": [32, 32], "rcut": 6.0, "rcut_smth": 1.0,
                   "neuron": [25, 50, 100], "axis_neuron": 16},
    "training": {"numb_steps": 400000, "batch_size": 8, "disp_file": "lcurve.out",
                 "disp_freq": 1000, "seed": 20240901},
    "backend": "tensorflow",          # 平台 dpmd 默认 TF；与 torch2.8 解耦，避免 CUDA 冲突
}

# 探索阶段默认信任区间（DPGEN 经典取法：对数空间均匀分布取 lo/hi）
DEFAULT_TRUST_LO = 0.05
DEFAULT_TRUST_HI = 0.15

# 收敛判据默认值（可在平台配置覆盖）
DEFAULT_CONVERGE_CRITERIA = {
    "rmse_e_max": 0.05,        # eV/atom：探索级；可用级可放宽至 0.08，生产级收紧至 0.03
    "rmse_f_max": 0.05,        # eV/Å
    "min_iters": 2,
    "patience": 3,             # 连续 N 轮平台期即判定收敛
    "val_train_ratio_max": 3.0  # 验证/训练误差比，超过疑似过拟合
}


# ---------------------------------------------------------------------------
# 配置校验
# ---------------------------------------------------------------------------
def validate_dpgen_config(cfg):
    """校验势函数延拓任务配置，返回 (ok, errors)。"""
    errors = []
    cfg = cfg or {}
    if not (cfg.get("run_name") or "").strip():
        errors.append("缺少 run_name（势函数延拓任务名称）")
    if not cfg.get("seed") and not cfg.get("system_elements"):
        errors.append("至少需要提供 seed（初始数据集路径/结构集）或 system_elements")
    iters = cfg.get("max_iter")
    if iters is not None:
        try:
            iters = int(iters)
            if iters < 1 or iters > 50:
                errors.append("max_iter 需为 1–50 之间的整数")
        except (TypeError, ValueError):
            errors.append("max_iter 必须为整数")
    tlo = cfg.get("trust_lo", DEFAULT_TRUST_LO)
    thi = cfg.get("trust_hi", DEFAULT_TRUST_HI)
    try:
        if not (0 <= float(tlo) < float(thi)):
            errors.append("trust_lo 必须小于 trust_hi 且均非负")
    except (TypeError, ValueError):
        errors.append("trust_lo / trust_hi 必须为数值")
    if cfg.get("cp2k_nproc") is not None:
        try:
            if int(cfg["cp2k_nproc"]) < 1:
                errors.append("cp2k_nproc 必须 ≥ 1")
        except (TypeError, ValueError):
            errors.append("cp2k_nproc 必须为整数")
    return (len(errors) == 0, errors)


# ---------------------------------------------------------------------------
# model_devi 解析与候选筛选
# ---------------------------------------------------------------------------
def parse_model_devi(text):
    """解析 dp model-devi 输出的 model_devi.out，返回每帧最大偏差序列。

    文件格式：首列 step，其后每原子一列偏差（单位 eV）。按帧取所有原子偏差的最大值
    作为该构型的模型偏差（DPGEN 常用策略）。忽略注释行与非法行。
    """
    frames = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) < 2:
            continue
        try:
            step = int(float(parts[0]))
            devs = [float(x) for x in parts[1:]]
        except ValueError:
            continue
        if not devs:
            continue
        frames.append({"step": step, "max_devi": max(devs)})
    return frames


def screen_candidates(model_devi_frames, trust_lo, trust_hi, stride=1):
    """在 (trust_lo, trust_hi) 之间筛选高偏差构型候选，按 stride 降采样避免冗余。

    返回候选构型在 frames 中的索引列表（供 02.fp 标注阶段提交 CP2K 单点）。
    """
    cand = [i for i, f in enumerate(model_devi_frames)
            if trust_lo < f["max_devi"] < trust_hi]
    if stride and stride > 1:
        cand = cand[::stride]
    return cand


# ---------------------------------------------------------------------------
# lcurve 解析与收敛判定
# ---------------------------------------------------------------------------
_LCURVE_KEYS = ["step", "rmse_val_e", "rmse_val_f", "rmse_test_e", "rmse_test_f",
                "rmse_e_train", "rmse_f_train"]


def parse_lcurve(text):
    """解析 lcurve.out，返回按步排序的指标字典列表（每行一个）。"""
    rows = []
    header = None
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#"):
            header = s.lstrip("#").split()
            continue
        parts = s.split()
        try:
            vals = list(map(float, parts))
        except ValueError:
            continue
        cols = header or _LCURVE_KEYS
        if len(vals) < len(cols):
            continue
        rows.append(dict(zip(cols, vals)))
    return rows


def assess_convergence(history, user_criteria=None):
    """基于 lcurve 历史与判据评估是否收敛。

    history: parse_lcurve 返回的字典列表（每轮训练末行，或每轮 lcurve 最后一行）。
    返回 {"converged": bool, "reasons": [str], "metrics": last_dict|None, "iters": int}。

    user_criteria: 可选自定义判据（如 {"rmse_e_max": 0.05, "patience": 3}），
        会覆盖 DEFAULT_CONVERGE_CRITERIA 中的同名键。
    """
    criteria = dict(DEFAULT_CONVERGE_CRITERIA)
    if user_criteria:
        criteria.update(user_criteria)
    reasons = []
    if not history:
        return {"converged": False, "reasons": ["尚无训练指标"], "metrics": None, "iters": 0}
    last = history[-1]
    n = len(history)
    converged = True

    if n < criteria["min_iters"]:
        converged = False
        reasons.append("迭代轮次 %d < 最少 %d 轮" % (n, criteria["min_iters"]))

    e = last.get("rmse_e_train")
    f = last.get("rmse_f_train")
    if e is not None and e > criteria["rmse_e_max"]:
        converged = False
        reasons.append("训练能量 RMSE %.4f > %.4f eV/atom" % (e, criteria["rmse_e_max"]))
    if f is not None and f > criteria["rmse_f_max"]:
        converged = False
        reasons.append("训练力 RMSE %.5f > %.5f eV/Å" % (f, criteria["rmse_f_max"]))

    ve = last.get("rmse_val_e")
    if ve is not None and e is not None and e > 0 and ve / e > criteria["val_train_ratio_max"]:
        converged = False
        reasons.append("验证/训练能量误差比 %.2f > %.2f（疑似过拟合）"
                       % (ve / e, criteria["val_train_ratio_max"]))

    # 平台期检测：近 patience 轮能量 RMSE 变化 < 阈值 → 收敛
    if converged and n >= criteria["patience"]:
        recent = [r.get("rmse_e_train") for r in history[-criteria["patience"]:]
                  if r.get("rmse_e_train") is not None]
        if len(recent) == criteria["patience"] and max(recent) - min(recent) < criteria["rmse_e_max"] * 0.2:
            reasons.append("近 %d 轮能量 RMSE 进入平台期，判定收敛" % criteria["patience"])
        else:
            converged = False
            reasons.append("误差仍在下降，继续迭代")

    return {"converged": converged, "reasons": reasons, "metrics": last, "iters": n}


# ---------------------------------------------------------------------------
# CP2K 高精度单点（能量 + 力 + 应力）输入生成 —— DPGEN 02.fp 标注阶段
# ---------------------------------------------------------------------------
def build_cp2k_fp_inp(structure, params=None):
    """生成 CP2K 单点（ENERGY_FORCE，含 FORCES / STRESS_TENSOR）输入 .inp。

    供 DPGEN 标注阶段对每个高偏差候选构型做 DFT 高精度计算，产出能量、力、应力，
    用于扩充机器学习势训练集。直接复用平台 CP2K 单点计算输入风格，
    但强制 RUN_TYPE ENERGY_FORCE 并开启 &PRINT &FORCES ON / &STRESS_TENSOR ON。

    structure: dict
        { "elements": [..],          # 每原子元素符号（与 coords 等长）
          "coords": [[x,y,z], ...],  # 笛卡尔坐标（Å）
          "cell": [[a1],[a2],[a3]],  # 晶胞矢量（Å）
          "pbc": True }
    params: dict {basis_set, pseudo_pot, xc, cutoff, ...}（可选，含合理默认值）
    """
    params = params or {}
    basis = params.get("basis_set", "DZVP-MOLOPT-SR-GTH")
    pseudo = params.get("pseudo_pot", "GTH-PBE")
    xc = params.get("xc", "PBE")
    cutoff = int(params.get("cutoff", 400))
    rel_cutoff = int(params.get("rel_cutoff", 60))
    max_scf = int(params.get("max_scf", 200))
    proj = params.get("project_name", "dpgen_fp")

    els = structure.get("elements") or []
    coords = structure.get("coords") or []
    cell = structure.get("cell") or [[0, 0, 0], [0, 0, 0], [0, 0, 0]]
    pbc = structure.get("pbc", True)

    # 种类去重，保持首次出现顺序
    kinds = []
    for el in els:
        if el not in kinds:
            kinds.append(el)

    cell_lines = "\n".join("    %20.12f %20.12f %20.12f" % tuple(v) for v in cell)
    coord_lines = "\n".join(
        "    %-4s %18.10f %18.10f %18.10f" % (els[i], coords[i][0], coords[i][1], coords[i][2])
        for i in range(len(els)))
    kind_blocks = "\n".join(
        "    &KIND %s\n      BASIS_SET %s\n      POTENTIAL %s\n    &END KIND" % (el, basis, pseudo)
        for el in kinds)

    inp = f"""&GLOBAL
  PROJECT_NAME {proj}
  RUN_TYPE ENERGY_FORCE
  PRINT_LEVEL LOW
&END GLOBAL

&FORCE_EVAL
  METHOD QUICKSTEP
  &DFT
    BASIS_SET_FILE_NAME BASIS_MOLOPT
    POTENTIAL_FILE_NAME GTH_POTENTIALS
    &MGRID
      CUTOFF {cutoff}
      REL_CUTOFF {rel_cutoff}
    &END MGRID
    &XC
      &XC_FUNCTIONAL {xc}
      &END XC_FUNCTIONAL
    &END XC
    &SCF
      MAX_SCF {max_scf}
      SCF_GUESS ATOMIC
      &DIAGONALIZATION
        ALGORITHM STANDARD
      &END DIAGONALIZATION
      &SCF_GUESS RESTART
      &END SCF
    &END DFT
    &PRINT
      &FORCES ON
      &END FORCES
      &STRESS_TENSOR ON
      &END STRESS_TENSOR
    &END PRINT
  &END FORCE_EVAL
  &SUBSYS
    &CELL
      A {cell_lines.splitlines()[0].strip()}
      B {cell_lines.splitlines()[1].strip() if len(cell_lines.splitlines())>1 else '0.0 0.0 0.0'}
      C {cell_lines.splitlines()[2].strip() if len(cell_lines.splitlines())>2 else '0.0 0.0 0.0'}
      PERIODIC {'XYZ' if pbc else 'NONE'}
    &END CELL
    &COORD
{coord_lines}
    &END COORD
{kind_blocks}
  &END SUBSYS
&END FORCE_EVAL
"""
    return inp


# ---------------------------------------------------------------------------
# 外部二进制调用封装（真实执行；环境缺失时抛 RuntimeError，由编排线程捕获降级）
# ---------------------------------------------------------------------------
def run_dp_train(workdir, param_path, machine_path=None, env=None, timeout=None):
    """调用 dpmd 环境的 dpgen 运行训练阶段，返回 (ok, out, err)。"""
    cmd = [DPGEN_BIN, "run", param_path]
    if machine_path:
        cmd.append(machine_path)
    return _run(cmd, workdir, env, timeout)


def run_lammps_model_devi(workdir, lammps_input, graph_files, env=None, timeout=None):
    """调用 dpmd 环境的 lmp 做 model_devi 探索，返回 (ok, out, err)。"""
    workdir = Path(workdir).resolve()
    inp = (workdir / lammps_input).resolve()
    if not inp.is_file() or '${models}' not in inp.read_text():
        raise ValueError('LAMMPS 输入必须存在并通过 ${models} 引用模型列表')
    graphs = [(workdir / str(g)).resolve() for g in graph_files]
    if not graphs or any(not g.is_file() or any(c.isspace() for c in str(g)) for g in graphs):
        raise ValueError('模型文件缺失或路径包含空白')
    cmd = [LMP_BIN, '-var', 'models', ' '.join(map(str, graphs)), '-in', str(inp), '-log', 'none']
    return _run(cmd, str(workdir), env, timeout)


def _run(cmd, cwd, env, timeout):
    env = env or dict(os.environ)
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        return (p.returncode == 0, p.stdout, p.stderr)
    except FileNotFoundError as e:
        raise RuntimeError("未找到 DP-GEN 执行环境，请确认 dpmd 环境已安装并配置 CATMAT_DPMD_PY：%s" % e)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("执行超时（%ss）：%s" % (timeout, " ".join(cmd)))


# ---------------------------------------------------------------------------
# 迭代状态机（核心）：驱动一轮「训练 → 探索 → 筛选 → 标注 → 评估」
# executor: {"train": fn(cfg)->lcurve_text,
#            "explore": fn(graphs, iter_no)->model_devi_text,
#            "label": fn(candidates, iter_no)->{"queue_ids":[...], "structures":[...]}}
# 返回新的 run_state（含 iters 推进记录与收敛结论），便于 server 落库与单测注入 mock。
# ---------------------------------------------------------------------------
def drive_iteration(run_state, executor, criteria=None, log_fn=None):
    """推进一轮 DPGEN 迭代，返回 (new_state, iteration_record)。

    run_state: dict {iter_no, max_iter, trust_lo, trust_hi, stride, lcurve_history:[]}
    executor: 真实执行器（server 提供）或测试 mock。
    """
    def log(msg):
        if log_fn:
            log_fn(msg)

    iter_no = run_state.get("iter_no", 0)
    max_iter = run_state.get("max_iter", 10)
    if iter_no >= max_iter:
        return run_state, {"phase": "done", "note": "已达最大迭代轮次 %d" % max_iter}

    log("迭代 #%d：训练势函数模型…" % (iter_no + 1))
    lcurve_text = executor["train"](run_state)
    history = parse_lcurve(lcurve_text)
    run_state.setdefault("lcurve_history", []).extend(history)
    conv = assess_convergence(run_state["lcurve_history"], criteria)

    # 先评估收敛（训练已充分则无需再探索）
    if conv["converged"]:
        run_state["iter_no"] = iter_no + 1
        run_state["converged"] = True
        return run_state, {"phase": "train", "iter_no": iter_no + 1,
                           "converged": True, "metrics": conv["metrics"],
                           "reasons": conv["reasons"]}

    # 探索：用当前模型在构型空间采样，得到 model_devi
    log("迭代 #%d：LAMMPS model_devi 探索采样…" % (iter_no + 1))
    graphs = executor.get("graphs", [])
    model_devi_text = executor["explore"](graphs, iter_no + 1)
    frames = parse_model_devi(model_devi_text)
    cand = screen_candidates(frames, run_state.get("trust_lo", DEFAULT_TRUST_LO),
                            run_state.get("trust_hi", DEFAULT_TRUST_HI),
                            run_state.get("stride", 1))

    if not cand:
        # 探索未找到新候选 → 势函数已覆盖采样空间，判定收敛
        run_state["iter_no"] = iter_no + 1
        run_state["converged"] = True
        return run_state, {"phase": "explore", "iter_no": iter_no + 1,
                           "converged": True, "note": "探索阶段无高偏差候选，势函数已覆盖",
                           "n_candidates": 0}

    # 标注：对候选构型提交 CP2K 高精度计算（server 侧走 queue_enqueue）
    log("迭代 #%d：对 %d 个高偏差构型提交 CP2K 标注…" % (iter_no + 1, len(cand)))
    label_res = executor["label"](cand, iter_no + 1)

    run_state["iter_no"] = iter_no + 1
    return run_state, {"phase": "label", "iter_no": iter_no + 1,
                       "converged": False, "n_candidates": len(cand),
                       "queue_ids": label_res.get("queue_ids", []),
                       "metrics": conv["metrics"]}


# ---------------------------------------------------------------------------
# CLI（被 dpmd python 作为脚本执行时，按子命令拼装并运行外部二进制）
# ---------------------------------------------------------------------------
def _cli():
    import sys
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "缺少子命令（train/explore）"}))
        return
    cmd = sys.argv[1]
    if cmd == "train":
        # sys.argv[2]=param_path [sys.argv[3]=machine_path]
        ok, out, err = run_dp_train(os.getcwd(), sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
        print(json.dumps({"ok": ok, "out": out[-4000:], "err": err[-2000:] if err else None}))
    elif cmd == "explore":
        # sys.argv[2]=lammps_input sys.argv[3:]=graph files
        ok, out, err = run_lammps_model_devi(os.getcwd(), sys.argv[2], sys.argv[3:])
        print(json.dumps({"ok": ok, "out": out[-4000:], "err": err[-2000:] if err else None}))
    else:
        print(json.dumps({"ok": False, "error": "未知子命令: %s" % cmd}))


if __name__ == "__main__":
    _cli()
