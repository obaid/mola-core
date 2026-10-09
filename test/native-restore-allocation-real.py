"""Explicit isolated Linux/ext4 acceptance; sparse disposable files, no QEMU."""
import hashlib
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile

spec=importlib.util.spec_from_file_location('native',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
ID='11111111-1111-4111-8111-111111111111';SNAP='22222222-2222-4222-8222-222222222222'
with tempfile.TemporaryDirectory(prefix='mola-restore-allocation-real-') as temp:
    root=pathlib.Path(temp);r=m.Runner.__new__(m.Runner);r.root=root;r.machines=root/'machines';r.machines.mkdir();r.sockets=root/'sockets';r.sockets.mkdir();r.config={'max_memory_mb':8192};r.arch='x86_64';r.accel='fixture';r.processes={}
    folder=r.folder(ID);folder.mkdir();disk=folder/'root.ext4'
    with disk.open('wb') as f:f.truncate(32*1024**2)
    subprocess.run(['mkfs.ext4','-F','-q',str(disk)],capture_output=True,check=True)
    marker=root/'marker';marker.write_text('original snapshot marker')
    subprocess.run(['debugfs','-w','-R','write '+str(marker)+' /marker',str(disk)],capture_output=True,check=True)
    m.write_json(folder/'machine.json',{'id':ID,'managed_by':'mola-native-v1','vcpus':1,'memory_mb':1024,'vnc_port':1234,'ssh_port':1235,'storage_incarnation':'real-restore-fixture'})
    manifest=r.snapshot(ID,{'snapshot_id':SNAP});artifact=r.storage_path(ID,SNAP)/'disk.gz';archive_sha=r.file_digest(artifact)
    path=r.operation_path(ID,'resize','grow');m.write_json(path,{'status':'pending'})
    r.resize(ID,{'operation_id':'grow','generation':2,'vcpus':2,'memory_mb':2048,'disk_gb':16,'_receipt_path':path})
    changed=root/'changed';changed.write_text('changed current marker')
    subprocess.run(['debugfs','-w','-R','rm /marker',str(disk)],capture_output=True,check=True)
    subprocess.run(['debugfs','-w','-R','write '+str(changed)+' /marker',str(disk)],capture_output=True,check=True)
    path=r.operation_path(ID,'restore','restore-old');m.write_json(path,{'status':'pending'})
    result=r.restore_snapshot(ID,{'snapshot_id':SNAP,'_receipt_path':path})
    assert disk.stat().st_size==16*1024**3 and r.metadata(ID)['disk_gb']==16
    assert subprocess.run(['debugfs','-R','cat /marker',str(disk)],capture_output=True,text=True,check=True).stdout.strip()==marker.read_text()
    assert subprocess.run(['e2fsck','-fn',str(disk)],capture_output=True).returncode==0
    assert r.file_digest(artifact)==archive_sha and manifest['size_bytes']==32*1024**2
    previous=m.subprocess.run
    def no_repeat(argv,**kwargs):
        assert argv[0] not in ['e2fsck','resize2fs'],'replayed expensive transform';return previous(argv,**kwargs)
    m.subprocess.run=no_repeat
    assert r.restore_snapshot(ID,{'snapshot_id':SNAP,'_receipt_path':path})==result
    print(json.dumps({'outcome':'passed','actual_tools':['mkfs.ext4','e2fsck','resize2fs','debugfs'],'snapshot_raw_bytes':32*1024**2,'restored_bytes':disk.stat().st_size,'marker_restored':True,'source_archive_unchanged':True,'strict_filesystem_check':True,'prepared_replay_no_repeat_transform':True,'qemu_used':False}))
