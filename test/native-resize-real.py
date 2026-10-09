"""Explicit Linux acceptance: real ext4 tools, sparse disposable disk, no QEMU.
Run in an isolated Linux container with python3 and e2fsprogs, not a live host.
"""
import hashlib
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile

spec=importlib.util.spec_from_file_location('native',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
ID='11111111-1111-4111-8111-111111111111'
with tempfile.TemporaryDirectory(prefix='mola-resize-real-') as temporary:
    root=pathlib.Path(temporary);r=m.Runner.__new__(m.Runner);r.root=root;r.machines=root/'machines';r.machines.mkdir();r.sockets=root/'sockets';r.sockets.mkdir();r.config={'max_memory_mb':8192};r.arch='x86_64';r.accel='fixture';r.processes={}
    folder=r.folder(ID);folder.mkdir();disk=folder/'root.ext4'
    with disk.open('wb') as stream:stream.truncate(32*1024*1024)
    subprocess.run(['mkfs.ext4','-F','-q',str(disk)],capture_output=True,check=True)
    marker=root/'marker';marker.write_text('owned real filesystem marker')
    subprocess.run(['debugfs','-w','-R','write '+str(marker)+' /marker',str(disk)],capture_output=True,check=True)
    original_sha=hashlib.sha256(disk.read_bytes()).hexdigest()
    m.write_json(folder/'machine.json',{'id':ID,'managed_by':'mola-native-v1','vcpus':1,'memory_mb':1024,'vnc_port':1234,'ssh_port':1235,'storage_incarnation':'real-fixture'})
    payload={'operation_id':'grow-real','generation':2,'vcpus':2,'memory_mb':2048,'disk_gb':16}
    path=r.operation_path(ID,'resize',payload['operation_id']);m.write_json(path,{'status':'pending'})
    result=r.resize(ID,{**payload,'_receipt_path':path})
    assert disk.stat().st_size==16*1024**3 and result['disk_gb']==16
    dumped=subprocess.run(['debugfs','-R','cat /marker',str(disk)],capture_output=True,text=True,check=True).stdout
    assert dumped.strip()==marker.read_text()
    assert subprocess.run(['e2fsck','-fn',str(disk)],capture_output=True).returncode==0
    # Validate durable prepared replay using the already committed 16GB disk.
    original_run=m.subprocess.run
    def no_transform(args,**kwargs):
        assert args[0] not in ['cp','resize2fs','e2fsck'],'replayed expensive resize';return original_run(args,**kwargs)
    m.subprocess.run=no_transform
    assert r.resize(ID,{**payload,'_receipt_path':path})==result
    print(json.dumps({'outcome':'passed','actual_tools':['mkfs.ext4','e2fsck','resize2fs','debugfs'],'source_size_bytes':32*1024*1024,'resized_bytes':disk.stat().st_size,'marker_preserved':True,'strict_filesystem_check':True,'prepared_replay_no_repeat_transform':True,'qemu_used':False}))
