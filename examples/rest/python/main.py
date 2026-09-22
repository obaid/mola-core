#!/usr/bin/env python3
import json, os, time, urllib.request
api=os.environ.get('MOLA_API','http://127.0.0.1:4141/v1').rstrip('/')
token=os.environ['MOLA_TOKEN']
def call(path, method='GET', body=None):
    data=None if body is None else json.dumps(body).encode()
    req=urllib.request.Request(api+path,data=data,method=method,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    with urllib.request.urlopen(req) as response: return json.load(response)['data']
machine=call('/machines','POST',{'name':'python-example','vcpus':1,'memory_mb':2048,'disk_gb':20})
while call('/machines/'+machine['id'])['status'] not in ('ready','unknown','stopped'): time.sleep(1)
def action(payload): return call('/machines/'+machine['id']+'/actions','POST',payload)
print(action({'action':'exec','command':'uname -a'}))
action({'action':'write_file','path':'~/mola-example.txt','content':'hello from Mola\n'})
print(action({'action':'read_file','path':'~/mola-example.txt'}))
call('/machines/'+machine['id']+'/stop','POST',{})
print({'id':machine['id'],'status':'stopped'})
