"""Sparse temporary disk mutation/failure/crash fixture; never invokes QEMU."""
import hashlib
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('host',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
ID='11111111-1111-4111-8111-111111111111'

def runner(root):
    r=m.Runner.__new__(m.Runner);r.root=root;r.machines=root/'machines';r.machines.mkdir(parents=True);r.sockets=root/'sockets';r.sockets.mkdir()
    r.config={'max_memory_mb':8192};r.arch='x86_64';r.accel='kvm';r.processes={}
    folder=r.folder(ID);folder.mkdir();m.write_json(folder/'machine.json',{'id':ID,'managed_by':'mola-native-v1','vcpus':2,'memory_mb':2048,'vnc_port':1234,'ssh_port':1235,'storage_incarnation':'fixture'})
    disk=folder/'root.ext4';disk.write_bytes(b'preserve customer content')
    r.capabilities=lambda:{'resize_disk_grow':True}
    # Sparse 16GB stages are real, while fixture checksums deliberately inspect
    # only these fixture bytes + size. Production hashes the entire disk.
    r.file_digest=lambda path:hashlib.sha256(path.open('rb').read(100)+str(path.stat().st_size).encode()).hexdigest()
    return r

def run(r,payload):
    path=r.operation_path(ID,'resize',payload['operation_id'])
    m.write_json(path,{'status':'pending','payload':payload})
    return path,r.resize(ID,{**payload,'_receipt_path':path})

def rejects(callback):
    try:callback()
    except (ValueError,subprocess.CalledProcessError):pass
    else:raise AssertionError('unsafe resize accepted')

with tempfile.TemporaryDirectory() as temporary:
    root=pathlib.Path(temporary);r=runner(root/'good');disk=r.folder(ID)/'root.ext4';original=disk.read_bytes()
    commands=[]
    def transforms(argv,**kwargs):
        commands.append(argv)
        if argv[0]=='cp':shutil.copyfile(argv[-2],argv[-1])
        return subprocess.CompletedProcess(argv,0,b'',b'')
    body={'operation_id':'resize-once','generation':2,'vcpus':4,'memory_mb':4096,'disk_gb':16}
    with patch.object(m.subprocess,'run',side_effect=transforms):
        path,result=run(r,body)
        assert result['disk_gb']==16 and disk.stat().st_size==16*1024**3
        assert disk.open('rb').read(len(original))==original
        assert r.metadata(ID)['vcpus']==4 and r.metadata(ID)['memory_mb']==4096
        assert [c[0] for c in commands]==['cp','e2fsck','resize2fs','e2fsck']
        assert commands[1][1]=='-fp' and commands[3][1]=='-fn'
    # Crash after disk swap but before receipt completion. Reconcile prepared
    # disk/metadata without another resize2fs invocation.
    with patch.object(m.subprocess,'run',side_effect=AssertionError('duplicate transform')):
        assert r.resize(ID,{**body,'_receipt_path':path})==result
    # Raw disk shrinking rejected even if native metadata omitted disk_gb.
    rejects(lambda:r.resize(ID,{**body,'disk_gb':15,'_receipt_path':path}))
    assert disk.stat().st_size==16*1024**3 and disk.open('rb').read(len(original))==original
    # CPU/memory-only resize never copies or transforms the retained disk.
    with patch.object(m.subprocess,'run',side_effect=AssertionError('unexpected disk writer')):
        _,result=run(r,{**body,'operation_id':'cpu-only','vcpus':2,'memory_mb':2048})
        assert result['vcpus']==2
    for failure in ['repair','resize','final']:
        r2=runner(root/failure);disk2=r2.folder(ID)/'root.ext4';before=disk2.read_bytes()
        def fail(argv,**kwargs):
            if argv[0]=='cp':shutil.copyfile(argv[-2],argv[-1]);return subprocess.CompletedProcess(argv,0,b'',b'')
            if (failure=='repair' and argv[:2]==['e2fsck','-fp']) or (failure=='final' and argv[:2]==['e2fsck','-fn']):return subprocess.CompletedProcess(argv,4,b'',b'')
            if failure=='resize' and argv[0]=='resize2fs':raise subprocess.CalledProcessError(1,argv)
            return subprocess.CompletedProcess(argv,0,b'',b'')
        with patch.object(m.subprocess,'run',side_effect=fail):rejects(lambda:run(r2,body))
        assert disk2.read_bytes()==before and not disk2.with_suffix('.resize').exists()
        assert r2.metadata(ID)['vcpus']==2
    # Durable command replay binds operation payload and disk incarnation.
    r3=runner(root/'receipts');r3.launch_storage_worker=lambda identifier,path:None
    receipt=r3.submit_storage(ID,'resize',body);assert receipt['status']=='pending'
    assert r3.submit_storage(ID,'resize',body)==receipt
    rejects(lambda:r3.submit_storage(ID,'resize',{**body,'memory_mb':8192}))
    rejects(lambda:r3.submit_storage(ID,'resize',{**body,'operation_id':'overtake','generation':3}))
    assert m.Runner.capabilities(r3)['running_checkpoint'] is False
print('native resize staging, failure preservation and prepared replay passed')
