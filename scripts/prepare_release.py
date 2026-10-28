#!/usr/bin/env python3
"""Build a fresh, allowlisted source tree; never copy deployment state or Git history."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ['.gitignore', '.env.example', 'README.md', 'ARCHITECTURE.md',
              'catmat.py', 'server.py', 'catmat_postprocessing.py',
              'catmat_process_monitor.py', 'catmat_resource_governor.py']
DOCS = ['user-manual.md', 'admin-manual.md', 'auth-security-design.md',
        'llm-vendors.md', 'ml-agent-integration.md', 'role-naming-rules.md',
        'tencent-cloud-deploy.md', '公网中转访问方案_20260904.md', 'RELEASING.md']
TREES = {'catmat_tools': {'.py'}, 'static': {'.js', '.svg', '.png', '.ico', '.txt'},
         'tests': {'.py', '.js', '.json', '.csv', '.inp', '.md'},
         'scripts': {'.py'}, '.github/workflows': {'.yml'}, 'docs/examples': {'.inp'}}
SECRET_PATTERNS = [re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
                   re.compile(rb'gh[pousr]_[A-Za-z0-9]{30,}'),
                   re.compile(rb'github_pat_[A-Za-z0-9_]{40,}'),
                   re.compile(rb'\bsk-[A-Za-z0-9_-]{32,}'),
                   re.compile(rb'\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}')]


TEXT_EXTENSIONS = {'.py', '.js', '.html', '.md', '.json', '.svg', '.yml', '.txt', '.example', '.inp', '.csv', '.vim'}


def public_bytes(relative, raw):
    """Remove deployment identifiers in exported text, without modifying live files."""
    if relative == 'scripts/prepare_release.py' or Path(relative).suffix not in TEXT_EXTENSIONS:
        return raw
    text = raw.decode('utf-8')
    text = re.sub(r'[\u4e00-\u9fff]ICP[备證证]\d+号(?:-\d+)?', 'ICP备案号请由部署者配置', text, flags=re.I)
    text = re.sub(r'[\u4e00-\u9fff]{1,3}公网安备\s*\d+号', '公安备案号请由部署者配置', text)
    text = re.sub(r'cat[a-z]*mind\.online', 'catamind.example.com', text, flags=re.I)
    text = re.sub(r'/home/[^/\s"\'`<>]+', '/home/user', text)
    text = re.sub(r'(?m)^(User|Group)=[A-Za-z0-9_-]+$', r'\1=catamind', text)
    text = re.sub(r'(?<![\d.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?![\d.])', '192.0.2.10', text)
    if relative == 'index.html':
        text = re.sub(r'<footer class="site-footer">.*?</footer>',
                      '<footer class="site-footer"><!-- 部署者在此配置自己的备案信息 --></footer>',
                      text, flags=re.S)
    return text.encode('utf-8')


def source_files(root):
    selected = [root / p for p in ROOT_FILES] + [root / 'index.html']
    selected += [root / 'docs' / p for p in DOCS]
    for directory, extensions in TREES.items():
        selected += [p for p in (root / directory).rglob('*') if p.is_file()
                     and p.suffix in extensions and '__pycache__' not in p.parts
                     and not any(x.startswith('.') for x in p.relative_to(root / directory).parts)]
    # Seed data is opt-in runtime functionality, not live research datasets.
    selected += list((root / 'data').glob('seed_*.json'))
    selected += list((root / 'data' / 'vim').glob('*.vim'))
    if (root / 'LICENSE').is_file():
        selected.append(root / 'LICENSE')
    return sorted(set(selected))


def audit(root):
    records, errors = [], []
    for path in source_files(root):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or root.resolve() not in path.resolve().parents:
            errors.append(relative + ': symbolic link or path escape')
            continue
        if not path.is_file():
            errors.append(relative + ': required file missing')
            continue
        if path.stat().st_size > 5 * 1024 * 1024:
            errors.append(relative + ': source file exceeds 5 MiB')
            continue
        raw = path.read_bytes()
        if any(pattern.search(raw) for pattern in SECRET_PATTERNS):
            errors.append(relative + ': potential credential (value withheld)')
        records.append(dict(path=relative, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()))
    return dict(files=records, errors=errors, bytes=sum(r['bytes'] for r in records),
                license_present=(root / 'LICENSE').is_file(),
                scope='Working-tree source only; no Git history, live data, private configuration, backups or desktop binaries. Pattern scan is not a comprehensive security audit.')


def export(root, target, report):
    if report['errors']:
        raise ValueError('Release audit failed')
    if target.exists():
        raise ValueError('Output already exists; choose a new directory (no overwrite)')
    target.mkdir(parents=True)
    public_records = []
    for record in report['files']:
        source = root / record['path']
        raw = source.read_bytes()
        if source.is_symlink() or hashlib.sha256(raw).hexdigest() != record['sha256']:
            raise ValueError('Source changed during export: ' + record['path'])
        destination = target / record['path']
        destination.parent.mkdir(parents=True, exist_ok=True)
        public = public_bytes(record['path'], raw)
        destination.write_bytes(public)
        public_records.append(dict(path=record['path'], bytes=len(public), sha256=hashlib.sha256(public).hexdigest()))
    report = dict(report, files=public_records, bytes=sum(r['bytes'] for r in public_records),
                  privacy='Exported text has deployment identifiers replaced; live source is unchanged. Hashes describe exported bytes.')
    (target / 'SOURCE_MANIFEST.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Fresh destination directory; omit for read-only audit')
    args = parser.parse_args()
    report = audit(ROOT)
    print(json.dumps({k:v for k,v in report.items() if k != 'files'} | {'file_count':len(report['files'])}, ensure_ascii=False, indent=2))
    if report['errors']:
        return 1
    if args.output:
        export(ROOT, args.output.resolve(), report)
        print('Exported:', args.output.resolve())
    return 0

if __name__ == '__main__':
    sys.exit(main())
