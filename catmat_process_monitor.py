"""Read-only shared process sampling. No probing at import time."""
import copy
import os
import shlex
import subprocess
import threading
import time

SCRIPTS = {
    "postprocess_toolkit.py": "后处理工具集",
    'ml_train.py': '机器学习', 'ml_compare.py': '多模型对比',
    'al_train.py': '主动学习', 'al_multidim.py': '多描述符主动学习',
    'postprocess_pdos.py': 'PDOS 后处理', 'bader_charge.py': 'Bader 后处理',
    'chart_gen.py': '图表生成', 'dpgen_adapter.py': 'DPGEN',
}


def parse_ps(output):
    rows = {}
    for line in output.splitlines():
        p = line.split(None, 8)
        if len(p) != 9:
            continue
        try:
            rows[int(p[0])] = dict(pid=int(p[0]), ppid=int(p[1]), cpu=float(p[2]),
                mem=float(p[3]), rss_mb=round(int(p[4])/1024, 1),
                etime=p[6], comm=p[7], args=p[8])
        except ValueError:
            continue
    return rows


def aggregate(rows):
    launchers = {pid for pid, p in rows.items()
                 if p['comm'] in ('mpirun', 'mpiexec', 'prterun', 'srun')}
    cp2k = [p for p in rows.values() if p['comm'].startswith('cp2k')]
    roots = {}
    for pid, p in rows.items():
        if not p['comm'].startswith(('python', 'dpgen', 'lmp')):
            continue
        try:
            args = shlex.split(p['args'])
        except ValueError:
            continue
        # Only executable/script position; never shell command text or arguments.
        script = os.path.basename(args[1]) if len(args) > 1 else ''
        if script in SCRIPTS:
            roots[pid] = SCRIPTS[script]
    def ancestor(pid, candidates):
        seen = set()
        while pid in rows and pid not in seen:
            if pid in candidates:
                return pid
            seen.add(pid)
            pid = rows[pid]['ppid']
        return None
    groups = {}
    for p in cp2k:
        root = ancestor(p['ppid'], launchers) or p['pid']
        groups.setdefault(root, []).append(p)
    def job(root, members, label):
        p = rows[root]
        return dict(launcher_pid=root, pid=root, input_file=label, module=label,
                    nprocs=len(members), total_cpu=round(sum(x['cpu'] for x in members), 1),
                    total_rss_mb=round(sum(x['rss_mb'] for x in members), 1), etime=p['etime'])
    jobs = [job(root, members, 'CP2K · PID %s' % root) for root, members in groups.items()]
    modules = []
    for root, label in roots.items():
        members = [p for p in rows.values() if ancestor(p['pid'], roots) == root]
        modules.append(job(root, members, label))
    def public(p):
        return {k: v for k, v in p.items() if k != 'args'}
    return dict(time=time.strftime('%Y-%m-%d %H:%M:%S'), cpu_basis='ps_lifetime_average',
        note='CPU 为进程生命周期平均值，100% 代表一个逻辑 CPU；RSS 求和可能重复计算共享页。',
        summary=dict(cp2k_procs=len(cp2k), cp2k_total_cpu=round(sum(p['cpu'] for p in cp2k), 1),
                     cp2k_total_rss_mb=round(sum(p['rss_mb'] for p in cp2k), 1),
                     jobs=len(jobs), launchers=len(launchers)),
        jobs=jobs, modules=modules, cp2k=[public(p) for p in sorted(cp2k, key=lambda p: -p['cpu'])[:80]])


class ProcessSampler:
    def __init__(self, ttl=3):
        self.ttl, self.lock, self.sampled, self.value = ttl, threading.Lock(), 0, None

    def snapshot(self):
        with self.lock:
            if self.value is not None and time.monotonic() - self.sampled < self.ttl:
                return copy.deepcopy(self.value), None
            try:
                result = subprocess.run(
                    ['ps', '-eo', 'pid,ppid,pcpu,pmem,rss,vsz,etime,comm,args', '--no-headers'],
                    capture_output=True, text=True, timeout=5, check=True)
                rows = parse_ps(result.stdout)
                if not rows:
                    raise ValueError('未采集到有效进程')
                self.value = aggregate(rows)
                # Restricted PID namespaces must not look like an idle host.
                self.value['visibility_warning'] = '进程可见性有限，请勿用于资源准入' if len(rows) < 32 else None
                self.sampled = time.monotonic()
                return copy.deepcopy(self.value), None
            except Exception as exc:
                return None, '进程采样失败: %s' % exc
