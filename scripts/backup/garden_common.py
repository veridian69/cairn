"""Garden producer archive validation; private extraction, stdlib only."""
import json
import os
import re
import shutil
import tarfile
import tempfile
from pathlib import Path,PurePosixPath
from backup_common import sha256

GARDEN=re.compile(r'^garden-backup-(?P<stamp>\d{8}T\d{6}Z)$')
# Snapshot IDs: creation time, a host label, then the export stamp.
LABELLED_SNAPSHOT=re.compile(r'^(?P<stamp>\d{8}T\d{6}Z)-(?P<label>[a-z0-9]{1,16})-(?P<export_stamp>\d{8}t\d{6}z)$')
REQUIRED={'garden.json','garden-daemon.service','garden-mcp.service',
          'tls/garden-key.pem','tls/garden-chain.pem','tls/internal-ca.pem'}
DAEMON_REQUIRED=REQUIRED
HOST_REQUIRED={'host.json','tls/server.crt','tls/server.key'}
HOST_UNIT=re.compile(r'^unit/cairn-install-[a-z0-9][a-z0-9-]*-garden\.service$')

def relative(name):
    path=PurePosixPath(name)
    if name in ('','.') or path.is_absolute() or '..' in path.parts or str(path)!=name:
        raise ValueError('unsafe Garden relative path')
    return path

def archive_name(snapshot_id):
    match=LABELLED_SNAPSHOT.fullmatch(snapshot_id)
    if not match: raise ValueError('unexpected Garden snapshot ID')
    return 'garden-backup-'+match['export_stamp'].upper()

def extract_garden(directory,name,destination):
    """Extract only ordinary files/directories; verify the complete snapshot."""
    if not GARDEN.fullmatch(name): raise ValueError('unexpected Garden archive name')
    tar_path=directory/f'{name}.tar'
    digest,separator,listed=(directory/f'{name}.tar.sha256').read_text().strip().partition('  ')
    if not separator or listed!=f'{name}.tar' or not re.fullmatch('[0-9a-f]{64}',digest) or sha256(tar_path)!=digest:
        raise ValueError(f'Garden export digest mismatch: {name}')
    seen=set(); files=set(); roots=set()
    with tarfile.open(tar_path,'r:') as archive:
        for member in archive:
            raw=member.name.rstrip('/') if member.isdir() else member.name
            path=relative(raw)
            if raw in seen or not (member.isdir() or member.isfile()): raise ValueError('duplicate or unsafe Garden tar member')
            seen.add(raw); top=path.parts[0]
            if LABELLED_SNAPSHOT.fullmatch(top): roots.add(top)
            elif top not in ('tls','garden-mcp.service.d','unit') and raw not in {'garden.json','garden-daemon.service','garden-mcp.service','host.json','compose.yaml'}:
                raise ValueError('unexpected Garden tar member')
            target=destination/path
            target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            for parent in target.parents:
                if parent==destination: break
                parent.chmod(0o700)
            if member.isdir():
                target.mkdir(mode=0o700,exist_ok=True); target.chmod(0o700)
            else:
                with archive.extractfile(member) as source,target.open('xb') as output:
                    os.chmod(target,0o600); shutil.copyfileobj(source,output,1024*1024)
                files.add(raw)
    if len(roots)!=1: raise ValueError('Garden archive must contain one snapshot')
    snapshot_id=roots.pop()
    if archive_name(snapshot_id)!=name: raise ValueError('Garden snapshot/export name mismatch')
    root=destination/snapshot_id
    doc=json.loads((root/'manifest.json').read_text())
    if doc.get('format_version')!=1 or doc.get('id')!=snapshot_id or not doc.get('binary_version'):
        raise ValueError('unsupported Garden snapshot manifest')
    config_field=doc.get('config_file','')
    if config_field not in ('','host.json'): raise ValueError('unsupported Garden snapshot config_file')
    config_file=config_field or 'config.yaml'
    if config_file=='config.yaml':
        required=DAEMON_REQUIRED
        deployment_allowed=DAEMON_REQUIRED
    elif config_file=='host.json':
        required=HOST_REQUIRED
        deployment_allowed=HOST_REQUIRED | {'tls/ca.crt','compose.yaml'}
    else:
        raise ValueError('unsupported Garden snapshot config_file')
    deployment_files={n for n in files if not n.startswith(snapshot_id+'/')}
    if any(not (n in deployment_allowed or (config_file=='config.yaml' and re.fullmatch(r'garden-mcp\.service\.d/[^/]+\.conf',n)) or (config_file=='host.json' and HOST_UNIT.fullmatch(n))) for n in deployment_files):
        raise ValueError('unexpected Garden deployment member')
    if not required<=deployment_files:
        raise ValueError('Garden archive lacks required deployment members')
    if config_file=='host.json':
        host_units={n for n in deployment_files if HOST_UNIT.fullmatch(n)}
        if int('compose.yaml' in deployment_files)+len(host_units)!=1:
            raise ValueError('Garden host archive requires exactly one backend deployment file')
    if config_file=='host.json':
        snapshot_host=root/'host.json'
        if not snapshot_host.is_file() or snapshot_host.read_bytes()!=(destination/'host.json').read_bytes():
            raise ValueError('Garden host config differs from deployment config')
    # restore.go accepts schema version 1 for both stores, as JSON integers.
    for key in ('state_schema_version','memory_schema_version'):
        if type(doc.get(key)) is not int or doc[key]!=1: raise ValueError('unsupported Garden snapshot schema versions')
    # files.go inventory sorts by path; restore.go sameFiles compares by index.
    paths=[entry['path'] for entry in doc.get('files',[])]
    if paths!=sorted(paths): raise ValueError('Garden manifest inventory is not in path order')
    expected=set(); total=0
    for entry in doc.get('files',[]):
        path=relative(entry['path']); key=str(path); target=root/path
        if key=='manifest.json' or key in expected or not target.is_file() or target.is_symlink():
            raise ValueError('unsafe or missing Garden manifest member')
        if target.stat().st_size!=entry['size'] or sha256(target)!=entry['sha256']:
            raise ValueError('Garden manifest member mismatch')
        # Garden copyTree always writes 0600; retain that mode for CLI validation.
        if entry.get('mode')!=0o600: raise ValueError('unexpected Garden snapshot file mode')
        expected.add(key); total+=entry['size']
    actual={n[len(snapshot_id)+1:] for n in files if n.startswith(snapshot_id+'/')} - {'manifest.json'}
    if expected!=actual or not {config_file,'data/.garden-owned'}<=expected or total!=doc.get('total_bytes'):
        raise ValueError('Garden snapshot manifest coverage mismatch')
    if sha256(root/config_file)!=doc.get('config_sha256'): raise ValueError('Garden config digest mismatch')
    return doc

def verify_garden(directory,name):
    with tempfile.TemporaryDirectory(prefix='.check-garden-',dir=directory) as temp:
        return extract_garden(directory,name,Path(temp))
