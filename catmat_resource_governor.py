#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CataMind 资源探测、快照组装与准入策略。

本模块导入时不执行任何探测。探测层只读 ``/proc`` 或调用 ``pgrep``；
``evaluate`` 是不访问文件、环境变量和进程状态的纯函数。

任何喂给安全判据的快照字段未知（非数字）⇒ ``BUDGET_UNKNOWN`` ⇒
``REJECT``。严禁静默跳过判据把“未知”当成“没有占用”。
"""

import math
import os
import subprocess


VISIBILITY_MIN_PROCS = 32

DEFAULT_POLICY = {
    "min_avail_gb": 130,
    "mem_per_rank_gb": 1.2,
    "core_reserve": 16,
    "mem_reserve_ratio": 1.15,
}

_RULES = (
    "INVALID_REQUEST",
    "BUDGET_UNKNOWN",
    "EXCEEDS_HOST_CORES",
    "EXCEEDS_PLATFORM_BUDGET",
    "EXCEEDS_HOST_MEMORY",
    "PLATFORM_CONCURRENCY_FULL",
    "NO_CORE_HEADROOM",
    "NO_MEMORY_HEADROOM",
    "OK",
)


def _pid1_identity():
    """返回规范化后的 PID 1 名称及其来源；失败时抛出最后一个异常。"""
    try:
        with open("/proc/1/comm", "r", encoding="utf-8", errors="replace") as handle:
            identity = handle.read().strip().lower()
        if identity:
            return identity, "/proc/1/comm"
    except Exception as comm_error:
        last_error = comm_error
    else:
        last_error = ValueError("/proc/1/comm 为空")

    try:
        with open("/proc/1/cmdline", "rb") as handle:
            raw = handle.read()
        first_arg = raw.split(b"\0", 1)[0].decode("utf-8", "replace").strip()
        identity = os.path.basename(first_arg).lower()
        if not identity:
            raise ValueError("/proc/1/cmdline 为空")
        return identity, "/proc/1/cmdline"
    except Exception as cmdline_error:
        raise RuntimeError("读取 PID 1 身份失败: comm=%s; cmdline=%s" %
                           (last_error, cmdline_error))


def host_process_visibility():
    """返回 ``(reliable, reason)``，表示宿主进程视图是否可信。

    PID 1 必须是 systemd/init，且 ``/proc`` 中的数字进程目录不少于
    :data:`VISIBILITY_MIN_PROCS`；任一检查失败或发生异常均不可信。
    """
    try:
        identity, source = _pid1_identity()
    except Exception as exc:
        return False, "PID 1 身份探测失败：%s" % exc

    if identity not in {"systemd", "init"}:
        return False, "%s 实测 PID 1 为 %r（非 systemd/init），疑似隔离的 PID namespace" % (
            source, identity)

    try:
        with os.scandir("/proc") as entries:
            process_count = sum(1 for entry in entries
                                if entry.name.isdigit() and entry.is_dir())
    except Exception as exc:
        return False, "/proc 进程数量探测失败：%s" % exc

    if process_count < VISIBILITY_MIN_PROCS:
        return False, "/proc 下仅 %d 个进程（<%d），疑似隔离的 PID namespace" % (
            process_count, VISIBILITY_MIN_PROCS)
    return True, "PID 1=%s，/proc 下 %d 个进程（>=%d），宿主进程视图可信" % (
        identity, process_count, VISIBILITY_MIN_PROCS)


def cp2k_process_count():
    """返回 ``(count, reason)``，统计宿主上的 ``cp2k.psmp`` 进程数。

    每个进程近似表示一个已占物理核。本探测使用
    ``pgrep -c -x cp2k.psmp``，且**只有** ``host_process_visibility()``
    通过时才有意义；可见性不可信或探测失败时返回 ``(None, 原因)``。
    ``pgrep`` 的“退出码 1 + 输出 0”按正常的零匹配处理。
    """
    reliable, visibility_reason = host_process_visibility()
    if reliable is not True:
        return None, "宿主进程视图不可信：%s" % visibility_reason

    try:
        result = subprocess.run(
            ["pgrep", "-c", "-x", "cp2k.psmp"],
            capture_output=True, text=True, timeout=5, check=False)
    except Exception as exc:
        return None, "pgrep 探测 cp2k.psmp 失败：%s" % exc

    output = (result.stdout or "").strip()
    if result.returncode == 1 and output == "0":
        return 0, "pgrep 未发现 cp2k.psmp（count=0）"
    if result.returncode != 0:
        detail = (result.stderr or output or "无输出").strip()
        return None, "pgrep 探测 cp2k.psmp 失败：退出码 %d，%s" % (
            result.returncode, detail)
    try:
        count = int(output)
        if count < 0:
            raise ValueError("负数")
    except (TypeError, ValueError):
        return None, "pgrep 返回无法解析的计数：%r" % output
    return count, "pgrep 实测 cp2k.psmp=%d" % count


def _meminfo_gb(field):
    try:
        with open("/proc/meminfo", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                key, separator, value = line.partition(":")
                if key == field and separator:
                    parts = value.split()
                    if not parts:
                        break
                    kb = float(parts[0])
                    if not math.isfinite(kb) or kb < 0:
                        break
                    gb = kb / (1024.0 * 1024.0)
                    return gb, "%s=%.2f GB" % (field, gb)
        return None, "/proc/meminfo 缺少有效的 %s" % field
    except Exception as exc:
        return None, "读取 /proc/meminfo 的 %s 失败：%s" % (field, exc)


def mem_available_gb():
    """返回 ``(gb, reason)``，读取 ``/proc/meminfo`` 的 MemAvailable。"""
    return _meminfo_gb("MemAvailable")


def mem_total_gb():
    """返回 ``(gb, reason)``，读取 ``/proc/meminfo`` 的 MemTotal。"""
    return _meminfo_gb("MemTotal")


def _effective_policy(policy):
    effective = dict(DEFAULT_POLICY)
    if policy is not None:
        effective.update(dict(policy))
    return effective


def build_snapshot(physical_cores, cores_reliable, platform_running_jobs,
                   platform_running_nproc, max_running, max_total_nproc,
                   policy=None):
    """将调用方注入的预算与即时只读探测结果组装为资源快照。

    读不到的探测项保留为 ``None``，绝不以零冒充。``policy`` 的有效副本
    随快照保存供 API/UI 展示；策略判定仍由 ``evaluate`` 的参数明确控制。
    """
    visibility_reliable, visibility_reason = host_process_visibility()
    if visibility_reliable is True:
        cores_in_use, cores_reason = cp2k_process_count()
        # 可见性自查通过但计数器本身失败时，占核预算仍不可确定。将快照
        # 标为不可信，确保 evaluate 命中 BUDGET_UNKNOWN，而不是把 None
        # 静默当作零占用。
        if cores_in_use is None:
            visibility_reliable = False
            visibility_reason = "%s；cp2k 占核探测失败：%s" % (
                visibility_reason, cores_reason)
    else:
        cores_in_use = None
        cores_reason = "未统计 cp2k：%s" % visibility_reason
    available_gb, available_reason = mem_available_gb()
    total_gb, total_reason = mem_total_gb()
    return {
        "physical_cores": physical_cores,
        "cores_reliable": cores_reliable,
        "cores_in_use": cores_in_use,
        "cores_in_use_reason": cores_reason,
        "mem_available_gb": available_gb,
        "mem_available_reason": available_reason,
        "mem_total_gb": total_gb,
        "mem_total_reason": total_reason,
        "platform_running_jobs": platform_running_jobs,
        "platform_running_nproc": platform_running_nproc,
        "max_running": max_running,
        "max_total_nproc": max_total_nproc,
        "visibility_reliable": visibility_reliable,
        "visibility_reason": visibility_reason,
        "policy": _effective_policy(policy),
    }


def _is_positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _fmt(value):
    if value is None:
        return "未知"
    if isinstance(value, float):
        return ("%.2f" % value).rstrip("0").rstrip(".")
    return str(value)


def evaluate(request, snapshot, policy=None):
    """纯函数：按固定九条规则评估资源请求并返回可解释结果。

    ``REJECT`` 表示请求本身不可能安全执行，或预算无法确定，因而拒绝；
    ``QUEUE`` 表示请求本身在预算内、但当前资源不足，应保持既有排队语义；
    ``ADMIT`` 表示当前可以准入。不能把临时资源不足改成拒绝，也不能把
    超预算请求放入队列。

    判定顺序固定为 INVALID_REQUEST、BUDGET_UNKNOWN、EXCEEDS_HOST_CORES、
    EXCEEDS_PLATFORM_BUDGET、EXCEEDS_HOST_MEMORY、PLATFORM_CONCURRENCY_FULL、
    NO_CORE_HEADROOM、NO_MEMORY_HEADROOM、OK，并返回第一个命中的规则。
    ``max_total_nproc`` 为 ``0`` 或 ``None`` 时表示平台总核数不限；此时
    显式忽略总核数相关判据，``platform_running_nproc`` 未知也不构成
    ``BUDGET_UNKNOWN``。
    本函数不读取环境变量、文件或任何全局运行状态。
    """
    request = request if isinstance(request, dict) else {}
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    effective = _effective_policy(policy)

    nproc = request.get("nproc")
    omp_threads = request.get("omp_threads", 1)
    request_valid = _is_positive_int(nproc) and _is_positive_int(omp_threads)
    requested_cores = nproc * omp_threads if request_valid else None
    est_mem_gb = (float(nproc) * float(effective["mem_per_rank_gb"])
                  * float(effective["mem_reserve_ratio"])) if request_valid else 0.0

    physical_cores = snapshot.get("physical_cores")
    cores_reliable = snapshot.get("cores_reliable")
    cores_in_use = snapshot.get("cores_in_use")
    mem_available = snapshot.get("mem_available_gb")
    mem_total = snapshot.get("mem_total_gb")
    visibility_reliable = snapshot.get("visibility_reliable")
    visibility_reason = snapshot.get("visibility_reason")
    platform_jobs = snapshot.get("platform_running_jobs")
    platform_nproc = snapshot.get("platform_running_nproc")
    max_running = snapshot.get("max_running")
    max_total_nproc = snapshot.get("max_total_nproc")

    physical_known = _is_number(physical_cores) and physical_cores > 0
    memory_known = _is_number(mem_available) and _is_number(mem_total)
    total_nproc_unlimited = (max_total_nproc is None or
                             (_is_number(max_total_nproc)
                              and max_total_nproc == 0))
    total_nproc_known = total_nproc_unlimited or _is_number(max_total_nproc)
    total_nproc_limited = (_is_number(max_total_nproc)
                           and max_total_nproc > 0)
    budget_unknown = (
        visibility_reliable is not True
        or not physical_known
        or cores_reliable is not True
        or not memory_known
        or not _is_number(cores_in_use)
        or not _is_number(platform_jobs)
        or not _is_number(max_running)
        or not total_nproc_known
        or (total_nproc_limited and not _is_number(platform_nproc))
    )

    host_cores_exceeded = False
    platform_budget_exceeded = False
    host_memory_exceeded = False
    concurrency_full = False
    no_core_headroom = False
    no_memory_headroom = False
    if request_valid and not budget_unknown:
        host_cores_exceeded = requested_cores > physical_cores
        platform_budget_exceeded = (
            total_nproc_limited and requested_cores > max_total_nproc)
        host_memory_exceeded = (
            est_mem_gb + float(effective["min_avail_gb"]) > mem_total)
        concurrency_full = (
            platform_jobs >= max_running
            or (total_nproc_limited
                and platform_nproc + requested_cores > max_total_nproc))
        no_core_headroom = (
            physical_cores - cores_in_use - requested_cores
            < float(effective["core_reserve"]))
        no_memory_headroom = (
            mem_available - est_mem_gb < float(effective["min_avail_gb"]))

    failed = {
        "INVALID_REQUEST": not request_valid,
        "BUDGET_UNKNOWN": budget_unknown,
        "EXCEEDS_HOST_CORES": host_cores_exceeded,
        "EXCEEDS_PLATFORM_BUDGET": platform_budget_exceeded,
        "EXCEEDS_HOST_MEMORY": host_memory_exceeded,
        "PLATFORM_CONCURRENCY_FULL": concurrency_full,
        "NO_CORE_HEADROOM": no_core_headroom,
        "NO_MEMORY_HEADROOM": no_memory_headroom,
    }
    all_checks_passed = not any(failed.values())

    details = {
        "INVALID_REQUEST": "nproc=%r、omp_threads=%r，二者必须为非 bool 的正整数" %
                           (nproc, omp_threads),
        "BUDGET_UNKNOWN": "可见性=%r，物理核=%s（可靠=%r），已占核=%s，MemAvailable=%s GB，MemTotal=%s GB，平台任务=%s/%s，平台已计核=%s，总核上限=%s；%s" %
                          (visibility_reliable, _fmt(physical_cores), cores_reliable,
                           _fmt(cores_in_use), _fmt(mem_available), _fmt(mem_total),
                           _fmt(platform_jobs), _fmt(max_running), _fmt(platform_nproc),
                           _fmt(max_total_nproc), visibility_reason or "无可见性说明"),
        "EXCEEDS_HOST_CORES": "请求 %s 核，宿主物理核上限 %s" %
                              (_fmt(requested_cores), _fmt(physical_cores)),
        "EXCEEDS_PLATFORM_BUDGET": "请求 %s 核，平台总核上限 %s" %
                                   (_fmt(requested_cores), _fmt(max_total_nproc)),
        "EXCEEDS_HOST_MEMORY": "预计内存 %.2f GB + 保底 %.2f GB，宿主总内存 %s GB" %
                               (est_mem_gb, float(effective["min_avail_gb"]), _fmt(mem_total)),
        "PLATFORM_CONCURRENCY_FULL": "平台运行 %s/%s 个任务、已计 %s 核；请求再占 %s 核，总核上限 %s" %
                                     (_fmt(platform_jobs), _fmt(max_running),
                                      _fmt(platform_nproc), _fmt(requested_cores),
                                      _fmt(max_total_nproc)),
        "NO_CORE_HEADROOM": "物理核 %s - 已占 %s - 请求 %s = 余量 %s 核，要求至少 %.2f 核" %
                            (_fmt(physical_cores), _fmt(cores_in_use),
                             _fmt(requested_cores),
                             _fmt(physical_cores - cores_in_use - requested_cores)
                             if request_valid and physical_known and _is_number(cores_in_use)
                             else "未知", float(effective["core_reserve"])),
        "NO_MEMORY_HEADROOM": "MemAvailable %s GB - 预计 %.2f GB = 余量 %s GB，要求至少 %.2f GB" %
                              (_fmt(mem_available), est_mem_gb,
                               _fmt(mem_available - est_mem_gb)
                               if request_valid and _is_number(mem_available) else "未知",
                               float(effective["min_avail_gb"])),
        "OK": "九项检查通过：请求 %s 核、预计内存 %.2f GB，可准入" %
              (_fmt(requested_cores), est_mem_gb),
    }

    code = next((rule for rule in _RULES[:-1] if failed[rule]), "OK")
    if code in {"INVALID_REQUEST", "BUDGET_UNKNOWN", "EXCEEDS_HOST_CORES",
                "EXCEEDS_PLATFORM_BUDGET", "EXCEEDS_HOST_MEMORY"}:
        action = "REJECT"
    elif code in {"PLATFORM_CONCURRENCY_FULL", "NO_CORE_HEADROOM",
                  "NO_MEMORY_HEADROOM"}:
        action = "QUEUE"
    else:
        action = "ADMIT"

    cores_free = (physical_cores - cores_in_use
                  if physical_known and _is_number(cores_in_use) else None)
    budget = {
        "physical_cores": physical_cores,
        "cores_reliable": cores_reliable,
        "cores_in_use": cores_in_use,
        "cores_free": cores_free,
        "core_reserve": effective["core_reserve"],
        "mem_total_gb": mem_total,
        "mem_available_gb": mem_available,
        "min_avail_gb": effective["min_avail_gb"],
        "mem_per_rank_gb": effective["mem_per_rank_gb"],
        "mem_reserve_ratio": effective["mem_reserve_ratio"],
        "platform_running_jobs": platform_jobs,
        "platform_running_nproc": platform_nproc,
        "max_running": max_running,
        "max_total_nproc": max_total_nproc,
        "visibility_reliable": visibility_reliable,
        "visibility_reason": visibility_reason,
    }
    checks = [
        {"rule": rule,
         "ok": all_checks_passed if rule == "OK" else not failed[rule],
         "detail": details[rule]}
        for rule in _RULES
    ]
    return {
        "action": action,
        "code": code,
        "message": details[code],
        "request": {"nproc": nproc, "omp_threads": omp_threads,
                    "cores": requested_cores},
        "est_mem_gb": est_mem_gb,
        "budget": budget,
        "checks": checks,
    }
