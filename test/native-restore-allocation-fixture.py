"""Disposable sparse fixtures; no real guest disks or QEMU."""
import hashlib
import importlib.util
import pathlib
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('native',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
ID='11111111-1111-4111-8111-111111111111';SNAP='22222222-2222-4222-8222-222222222222'
GIB=1024**3
def runner(root,allocated=False):
    r=m.Runner.__new__(m.Runner);r.root=root;r.machines=root/'machines';r.machines.mkdir(parents=True);r.sockets=root/'sockets';r.sockets.mkdir()
    r.config={};r.arch='x86_64';r.accel='fixture';r.processes={};r.capabilities=lambda:{'resize_disk_grow':True}
    p=r.folder(ID);p.mkdir();data={'id':ID,'managed_by':'mola-native-v1','storage_incarnation':'owned-fixture','vcpus':2,'memory_mb':2048,'vnc_port':1234,'ssh_port':1235}
    if allocated:data['disk_gb']=16
    m.write_json(p/'machine.json',data);disk=p/'root.ext4'
    with disk.open('wb') as f:f.write(b'keep current disk');f.truncate(16*GIB if allocated else 100)
    return r
def cheap_digest(path):
    if path.name.endswith('.gz'):return m.Runner.file_digest(path)
    with path.open('rb') as f:return hashlib.sha256(f.read(128)+str(path.stat().st_size).encode()).hexdigest()
def rejects(call):
    try:call()
    except (ValueError,subprocess.CalledProcessError):return
    raise AssertionError('unsafe restore accepted')
with tempfile.TemporaryDirectory() as temp:
    root=pathlib.Path(temp);source=runner(root/'source');original=(source.folder(ID)/'root.ext4').read_bytes();manifest=source.snapshot(ID,{'snapshot_id':SNAP})
    artifact=source.storage_path(ID,SNAP)/'disk.gz';archive_sha=m.Runner.file_digest(artifact)
    def target(name):
        r=runner(root/name,True);r.file_digest=cheap_digest
        shutil.copytree(source.storage_path(ID,SNAP),r.storage_path(ID,SNAP));p=r.operation_path(ID,'restore',name)
        m.write_json(p,{'status':'pending'});return r,p
    good,receipt=target('good');disk=good.folder(ID)/'root.ext4';before=cheap_digest(disk);commands=[]
    def grow(argv,**kwargs):
        commands.append(argv);assert cheap_digest(disk)==before,'old disk was modified in place'
        assert m.Runner.file_digest(artifact)==archive_sha
        stage=pathlib.Path(argv[-1]);assert stage.suffix=='.restore' and stage.stat().st_size==16*GIB
        with stage.open('rb') as f:assert f.read(len(original))==original
        return subprocess.CompletedProcess(argv,2 if argv[:2]==['e2fsck','-fp'] else 0,b'',b'')
    body={'snapshot_id':SNAP,'_receipt_path':receipt}
    with patch.object(m.subprocess,'run',side_effect=grow):result=good.restore_snapshot(ID,body)
    assert [v[:2] for v in commands]==[['e2fsck','-fp'],['resize2fs',str(disk.with_suffix('.restore'))],['e2fsck','-fn']]
    assert disk.stat().st_size==16*GIB and good.metadata(ID)['disk_gb']==16
    with disk.open('rb') as f:assert f.read(len(original))==original
    assert __import__('json').loads(receipt.read_text())['prepared']['size_bytes']==16*GIB
    with patch.object(m.subprocess,'run',side_effect=AssertionError('duplicate expensive restore')):assert good.restore_snapshot(ID,body)==result
    prepared=__import__('json').loads(receipt.read_text());prepared['prepared']['size_bytes']=8*GIB;m.write_json(receipt,prepared)
    saved=cheap_digest(disk);rejects(lambda:good.restore_snapshot(ID,body));assert cheap_digest(disk)==saved
    for failure in ['raw-checksum','repair','resize','strict']:
        r,p=target(failure);disk=r.folder(ID)/'root.ext4';before=cheap_digest(disk);calls=[]
        if failure=='raw-checksum':m.write_json(r.storage_path(ID,SNAP)/'manifest.json',{**manifest,'sha256':'0'*64})
        def broken(argv,**kwargs):
            calls.append(argv)
            if failure=='resize' and argv[0]=='resize2fs':raise subprocess.CalledProcessError(1,argv)
            code=4 if failure=='repair' and argv[:2]==['e2fsck','-fp'] or failure=='strict' and argv[:2]==['e2fsck','-fn'] else 0
            return subprocess.CompletedProcess(argv,code,b'',b'')
        with patch.object(m.subprocess,'run',side_effect=broken):rejects(lambda:r.restore_snapshot(ID,{'snapshot_id':SNAP,'_receipt_path':p}))
        assert cheap_digest(disk)==before and not disk.with_suffix('.restore').exists()
        if failure=='raw-checksum':assert not calls,'unverified raw snapshot was transformed'
        assert m.Runner.file_digest(artifact)==archive_sha
    # Legacy prepared smaller stages must never replace an upgraded live disk.
    legacy,p=target('legacy');disk=legacy.folder(ID)/'root.ext4';before=cheap_digest(disk);stage=disk.with_suffix('.restore');stage.write_bytes(original)
    m.write_json(p,{'status':'pending','prepared':{'sha256':cheap_digest(stage),'result':result}})
    rejects(lambda:legacy.restore_snapshot(ID,{'snapshot_id':SNAP,'_receipt_path':p}));assert cheap_digest(disk)==before
print('restore allocation, original checksum ordering, failure preservation and prepared replay passed')
