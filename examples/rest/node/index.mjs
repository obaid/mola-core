const api = (process.env.MOLA_API || 'http://127.0.0.1:4141/v1').replace(/\/$/, '');
const token = process.env.MOLA_TOKEN;
if (!token) throw new Error('Set MOLA_TOKEN.');
const request = async (path, options = {}) => {
  const response = await fetch(api + path, { ...options, headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json', ...options.headers } });
  const body = await response.json();
  if (!response.ok) throw new Error(body.message);
  return body.data;
};
const machine = await request('/machines', { method: 'POST', body: JSON.stringify({ name: 'rest-example', vcpus: 1, memory_mb: 2048, disk_gb: 20 }) });
for (;;) {
  const current = await request(`/machines/${machine.id}`);
  if (current.status === 'ready') break;
  if (!['booting', 'starting'].includes(current.status)) throw new Error(`Machine became ${current.status}`);
  await new Promise(resolve => setTimeout(resolve, 1000));
}
const action = body => request(`/machines/${machine.id}/actions`, { method: 'POST', body: JSON.stringify(body) });
console.log(await action({ action: 'exec', command: 'uname -a' }));
await action({ action: 'write_file', path: '~/mola-example.txt', content: 'hello from Mola\n' });
console.log(await action({ action: 'read_file', path: '~/mola-example.txt' }));
console.log(await action({ action: 'screenshot' }));
await request(`/machines/${machine.id}/stop`, { method: 'POST', body: '{}' });
console.log({ id: machine.id, status: 'stopped', note: 'Reuse this id to resume.' });
