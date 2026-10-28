"""Checked external execution for platform-owned short workers (stdlib only)."""
import json
import os
import signal
import subprocess
import tempfile


def run_checked(argv, *, cwd=None, env=None, timeout=120, input_text=None, max_output=16*1024*1024, own_group=True):
    if not argv or not isinstance(argv, (list, tuple)):
        raise ValueError('命令必须为参数数组')
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                stdout=stdout, stderr=stderr, start_new_session=own_group)
        try:
            proc.communicate(None if input_text is None else input_text.encode(), timeout=timeout)
        except subprocess.TimeoutExpired:
            # Only this newly created process group belongs to this call.
            try:
                os.killpg(proc.pid, signal.SIGKILL) if own_group else proc.kill()
            except ProcessLookupError:
                pass
            proc.wait()
            raise TimeoutError('外部程序超时（%ss）: %s' % (timeout, os.path.basename(str(argv[0]))))
        def read(stream):
            size = stream.tell()
            stream.seek(max(0, size-max_output))
            return stream.read().decode('utf-8', errors='replace'), size > max_output
        out, truncated = read(stdout)
        err, _ = read(stderr)
        if proc.returncode:
            raise RuntimeError('外部程序退出码 %s: %s' % (proc.returncode, (err or out)[-1500:]))
        if truncated:
            raise RuntimeError('外部程序输出超过限制，拒绝解析截断结果')
        return out, err


def run_json(argv, **kwargs):
    out, err = run_checked(argv, **kwargs)
    try:
        obj = json.loads(out.strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise ValueError('外部程序未返回有效 JSON: ' + (err or out)[-500:])
    if not isinstance(obj, dict) or obj.get('ok') is not True:
        raise ValueError(obj.get('error', '外部程序未确认成功') if isinstance(obj, dict) else '结果不是对象')
    return obj
