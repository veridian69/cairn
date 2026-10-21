"""Shared integrity, SQLite snapshot and retention operations (no dependencies)."""
import hashlib
import os
import re
import sqlite3
import shutil
from datetime import datetime
from pathlib import Path

SET_NAME = re.compile(r'^\d{8}T\d{6}Z$')

def sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''): h.update(block)
    return h.hexdigest()

def write_checksums(root):
    root=Path(root)
    files=sorted(p for p in root.rglob('*') if p.is_file() and p.name!='SHA256SUMS')
    if any(p.is_symlink() for p in root.rglob('*')): raise ValueError('symlinks forbidden')
    text=''.join(f'{sha256(p)}  {p.relative_to(root).as_posix()}\n' for p in files)
    (root/'SHA256SUMS').write_text(text)
    os.chmod(root/'SHA256SUMS',0o600)

def verify_checksums(root):
    root=Path(root); seen=set()
    for line in (root/'SHA256SUMS').read_text().splitlines():
        digest, separator, name=line.partition('  ')
        rel=Path(name)
        if not separator or not re.fullmatch('[0-9a-f]{64}',digest) or not name or rel.is_absolute() or '..' in rel.parts or name in seen:
            raise ValueError('unsafe or duplicate checksum entry')
        path=root/rel
        if any(p.is_symlink() for p in [path,*path.parents]) or not path.is_file(): raise ValueError('missing/unsafe checksum file')
        if sha256(path)!=digest: raise ValueError(f'checksum mismatch: {name}')
        seen.add(name)
    actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and p.name!='SHA256SUMS'}
    if not seen or actual!=seen: raise ValueError('checksum coverage mismatch')
    return seen

def sqlite_backup(source,dest):
    source=Path(source).resolve(); dest=Path(dest)
    if not source.is_file(): raise ValueError(f'missing database: {source}')
    src=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=20)
    dst=sqlite3.connect(dest)
    try:
        src.backup(dst)
        dst.execute('PRAGMA journal_mode=DELETE')
        result=dst.execute('PRAGMA integrity_check').fetchall()
        if result!=[('ok',)]: raise ValueError('SQLite integrity check failed')
    finally: dst.close(); src.close()
    os.chmod(dest,0o600)

def retained_names(names,daily=30,weekly=8,monthly=6):
    ordered=sorted((n for n in names if SET_NAME.fullmatch(n)),reverse=True)
    keep=set()
    for count,bucket in [(daily,lambda d:d.date()),(weekly,lambda d:d.isocalendar()[:2]),(monthly,lambda d:(d.year,d.month))]:
        chosen=set()
        for name in ordered:
            key=bucket(datetime.strptime(name,'%Y%m%dT%H%M%SZ'))
            if key not in chosen:
                if len(chosen)>=count: break
                chosen.add(key); keep.add(name)
    return keep

def cleanup_incoming(root):
    """Remove only our unpublished staging directories, while caller holds its lock."""
    root=Path(root)
    if not root.exists(): return
    for path in root.glob('.incoming-*'):
        if path.is_dir() and not path.is_symlink(): shutil.rmtree(path)
