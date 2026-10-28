"""Bounded asynchronous postprocessing jobs; persistence owned by platform API."""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from catmat_tools import postprocess_toolkit as toolkit
from catmat_tools.external_runner import run_json

SCHEMA='''
CREATE TABLE IF NOT EXISTS postprocess_jobs (
 id TEXT PRIMARY KEY, owner_id INTEGER, tool TEXT NOT NULL, status TEXT NOT NULL,
 config TEXT NOT NULL, result TEXT, error TEXT, created_at TEXT NOT NULL, finished_at TEXT
);
'''
SLOTS=threading.BoundedSemaphore(2)


def validate(config,allowed_roots):
    if not isinstance(config,dict):raise ValueError('请求必须为对象')
    if set(config)-{'tool','inputs','params'}:raise ValueError('未知请求字段')
    tool=config.get('tool')
    if tool not in toolkit.TOOLS:raise ValueError('未知或尚未开放的工具')
    spec=toolkit.TOOLS[tool]
    inputs=config.get('inputs') or {};params=config.get('params') or {}
    if not isinstance(inputs,dict) or not isinstance(params,dict):raise ValueError('inputs/params 必须为对象')
    if set(inputs)-set(spec['inputs']+spec.get('optional_inputs',[])):raise ValueError('未知输入字段')
    if set(params)-set(spec['params']):raise ValueError('未知参数')
    if tool=='bader_run' and not toolkit.bader_binary():raise ValueError('未配置可执行的 CATMAT_BADER_BIN')
    roots=[Path(p).resolve() for p in allowed_roots]
    resolved={}
    for key in spec['inputs']+spec.get('optional_inputs',[]):
        value=inputs.get(key)
        if not value:
            if key in spec['inputs']:raise ValueError('缺少输入: '+key)
            continue
        path=Path(str(value)).expanduser().resolve()
        if not any(path==root or root in path.parents for root in roots):raise ValueError('输入不在允许的计算/数据目录内')
        if any(part.startswith('.') for part in path.parts):raise ValueError('不接受隐藏文件或目录')
        if key=='directory':
            if not path.is_dir():raise ValueError('PDOS 目录不存在')
            for child in path.glob('*.pdos'):
                if not any(root in child.resolve().parents for root in roots):raise ValueError('PDOS 符号链接越界')
        elif not path.is_file():raise ValueError('输入文件不存在: '+key)
        resolved[key]=str(path)
    return dict(tool=tool,inputs=resolved,params=params)


def recover(conn):
    conn.execute("UPDATE postprocess_jobs SET status='interrupted',error='平台重启，需检查产物后重新执行',finished_at=? WHERE status IN ('queued','running')",(time.strftime('%Y-%m-%d %H:%M:%S'),))


def start(conn,db_path,root,python,config,owner_id):
    if not SLOTS.acquire(blocking=False):raise RuntimeError('后处理并发已满（2），请稍后重试')
    jid=uuid.uuid4().hex
    wd=Path(root)/jid
    try:
        wd.mkdir(parents=True,exist_ok=False)
        cfg=wd/'request.json';cfg.write_text(json.dumps(config,ensure_ascii=False,allow_nan=False))
        conn.execute('INSERT INTO postprocess_jobs(id,owner_id,tool,status,config,created_at) VALUES(?,?,?,?,?,?)',
                     (jid,owner_id,config['tool'],'queued',json.dumps(config,ensure_ascii=False),time.strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        worker=threading.Thread(target=execute,args=(db_path,wd,python,config,jid),daemon=True)
        worker.start()
    except Exception:
        SLOTS.release()
        raise
    return dict(id=jid,status='queued',tool=config['tool'])


def execute(db_path,wd,python,config,jid):
    def update(**fields):
        with contextlib.closing(sqlite3.connect(db_path,timeout=30)) as conn:
            conn.execute('UPDATE postprocess_jobs SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',list(fields.values())+[jid])
            conn.commit()
    try:
        update(status='running')
        env=dict(os.environ,OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1',NUMEXPR_NUM_THREADS='1')
        obj=run_json([python,str(Path(toolkit.__file__).resolve()),'run',str(wd/'request.json'),'--workdir',str(wd)],
                     cwd=str(wd),env=env,timeout=300)
        result=obj.get('result')
        if not isinstance(result,dict) or result.get('tool')!=config['tool'] or result.get('review_status')!='preview':
            raise ValueError('后处理结果协议不匹配')
        needs_review = any(c.get('ok') is False for c in result.get('checks',[])) or (config['tool']=='output_audit' and result.get('execution_status')!='ended')
        status='needs_review' if needs_review else 'done'
        update(status=status,result=json.dumps(result,ensure_ascii=False,allow_nan=False),finished_at=time.strftime('%Y-%m-%d %H:%M:%S'))
    except Exception as exc:
        update(status='failed',error=str(exc)[-2000:],finished_at=time.strftime('%Y-%m-%d %H:%M:%S'))
    finally:SLOTS.release()


def view(row,detail=False):
    d={k:row[k] for k in ('id','tool','status','error','created_at','finished_at')}
    if detail:
        d['config']=json.loads(row['config']);d['result']=json.loads(row['result']) if row['result'] else None
    return d


UPLOAD_LIMIT = 32 * 1024 * 1024
INPUT_EXTENSIONS = {
    'total_cube': ('.cube',), 'slab_cube': ('.cube',), 'ads_cube': ('.cube',),
    'density_cube': ('.cube',), 'reference_cube': ('.cube',),
    'acf_file': ('.dat',), 'directory': ('.pdos',), 'output_file': ('.out', '.log', '.output'),
}


def browse_inputs(field, path, allowed_roots):
    """List only scientific inputs within explicit roots; never follow escaping links."""
    if field not in INPUT_EXTENSIONS:
        raise ValueError('未知输入字段')
    roots = [Path(p).resolve() for p in allowed_roots]
    def permitted(p):
        return not any(x.startswith('.') for x in p.parts) and any(p == r or r in p.parents for r in roots)
    if not path:
        return dict(path='', parent=None, entries=[dict(name=str(r), path=str(r), directory=True)
                    for r in roots if r.is_dir() and permitted(r)], truncated=False)
    folder = Path(str(path)).expanduser().resolve()
    if not permitted(folder) or not folder.is_dir():
        raise ValueError('目录不存在或不在允许浏览的范围内')
    entries = []
    truncated = False
    with os.scandir(folder) as children:
        for i, child in enumerate(children):
            if i >= 10000:
                truncated = True
                break
            if child.name.startswith('.'):
                continue
            try:
                target = Path(child.path).resolve()
                if not permitted(target):
                    continue
                directory = child.is_dir()
                if not directory and not (child.is_file() and child.name.lower().endswith(INPUT_EXTENSIONS[field])):
                    continue
                entries.append(dict(name=child.name, path=str(target), directory=directory))
            except OSError:
                continue
    entries.sort(key=lambda e: (not e['directory'], e['name'].lower()))
    return dict(path=str(folder), parent=str(folder.parent) if permitted(folder.parent) else '',
                entries=entries, truncated=truncated)


def upload_inputs(field, files, upload_root):
    """Validate the complete batch before publishing unique, non-overwriting inputs."""
    import base64
    import binascii
    if field not in INPUT_EXTENSIONS or not isinstance(files, list) or not files or len(files) > 100:
        raise ValueError('请选择 1–100 个输入文件')
    if field != 'directory' and len(files) != 1:
        raise ValueError('该输入只允许一个文件')
    decoded, names, total = [], set(), 0
    for item in files:
        if not isinstance(item, dict):
            raise ValueError('文件格式错误')
        name = item.get('name', '')
        if (not isinstance(name, str) or not name or name.startswith('.') or '/' in name or '\\' in name
                or any(ord(c) < 32 for c in name) or len(name.encode('utf-8')) > 240
                or not name.lower().endswith(INPUT_EXTENSIONS[field]) or name.casefold() in names):
            raise ValueError('文件名、扩展名不符合要求或文件重名')
        try:
            raw = base64.b64decode(item.get('content_b64', ''), validate=True)
        except (ValueError, TypeError, binascii.Error):
            raise ValueError('文件编码无效') from None
        total += len(raw)
        if not raw or total > UPLOAD_LIMIT:
            raise ValueError('文件不能为空，每批上传总量不得超过 32 MiB；大文件请从服务器选择')
        names.add(name.casefold())
        decoded.append((name, raw))
    folder = Path(upload_root) / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=False)
    try:
        for name, raw in decoded:
            (folder / name).write_bytes(raw)
    except Exception:
        import shutil
        shutil.rmtree(folder)
        raise
    return dict(path=str(folder if field == 'directory' else folder / decoded[0][0]),
                count=len(decoded), size_bytes=total)
