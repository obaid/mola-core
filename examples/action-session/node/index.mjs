import WebSocket from 'ws';
const api=(process.env.MOLA_API||'http://127.0.0.1:4141/v1').replace(/\/$/,'');
const token=process.env.MOLA_TOKEN, machine=process.env.MOLA_MACHINE;
if(!token||!machine) throw new Error('Set MOLA_TOKEN and MOLA_MACHINE.');
const response=await fetch(`${api}/machines/${encodeURIComponent(machine)}/session`,{method:'POST',headers:{authorization:`Bearer ${token}`}});
const grant=(await response.json()).data;
const ws=new WebSocket(grant.session_url);
await new Promise((resolve,reject)=>{ws.once('open',resolve);ws.once('error',reject)});
const pending=new Map();
ws.on('message',(data,binary)=>{if(binary)return;const message=JSON.parse(data);pending.get(message.id)?.(message);});
const action=(id,body)=>new Promise(resolve=>{pending.set(id,resolve);ws.send(JSON.stringify({id,op:'action',action:body}));});
for(let i=0;i<100;i+=1){const result=await action(String(i),{action:'exec',command:'true'});if(!result.ok)throw new Error(result.error.message);}
console.log('100 actions completed over one session');
ws.close();
