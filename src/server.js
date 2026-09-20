import http from 'node:http';
import { Registry } from './state.js';
import { Runtime } from './runtime.js';
import { GuestService } from './guest.js';
import { runAction } from './automation.js';
import { operatorToken, authorised, present, validateSpec, validateAction } from './api.js';
import { desktopPage, attachDesktop, mintTicket, serveNovnc } from './desktop.js';
import { guestKey } from './keys.js';
import { HostApi, hostToken } from './host-api.js';
import { SNAPSHOT_CHUNK_BODY_BYTES } from './host-storage.js';
import { attachSsh } from './ssh.js';
import { SnapshotTransfer } from './snapshot-transfer.js';

const json = (response, status, body) => {
  const payload = JSON.stringify(body, null, 2);
  response.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(payload) });
  response.end(payload);
};

async function readBody(request, limit = 2 * 1024 * 1024) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > limit) throw new Error('Request body too large.');
    chunks.push(chunk);
  }
  if (!chunks.length) return {};
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

export async function createServer({ host, port, registry = new Registry(), runtime = new Runtime(host), keys = guestKey() }) {
  const guests = new GuestService(registry);
  const token = operatorToken();
  const hostApi = new HostApi({
    registry, runtime, publicKey: keys.publicKey, token: hostToken(token),
    desktop: (id, described, binding) => ({
      desktop_url: `http://127.0.0.1:${port}/desktop#t=${mintTicket(id, described, {
        ...binding, validate: captured => hostApi.validateDesktop(id, captured),
      })}`, expires_in: 60,
    }),
    action: runAction,
    snapshotTransfer: new SnapshotTransfer(),
  });

  await runtime.start();

  // Adopt anything the runtime is holding that the registry has forgotten.
  //
  // The engine is not the only thing that can die: a hard kill leaves QEMU
  // running and the record unwritten. Reconciling on start means an orphan is
  // named and can be deleted, rather than silently consuming the host until
  // someone reads a process list.
  try {
    // The runtime answers with {id: status}, not a list. Iterating it as an
    // array silently adopts nothing, which is the quietest possible way for a
    // reconciler to do no work at all.
    const held = await runtime.list();
    const known = new Set(registry.all().map((record) => record.id));
    for (const id of Object.keys(held ?? {})) {
      if (!known.has(id)) {
        registry.records[id] = {
          id,
          name: `adopted-${id.slice(0, 8)}`,
          vcpus: 0,
          memory_mb: 0,
          disk_gb: 0,
          created_at: new Date().toISOString(),
          adopted: true,
        };
      }
    }
    if (Object.keys(registry.records).length !== known.size) registry.flush();
  } catch {
    // A runtime that cannot list is a problem for the first request, not here.
  }

  const describe = async (id) => {
    try { return await runtime.describe(id); } catch { return null; }
  };

  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, `http://localhost:${port}`);
    const parts = url.pathname.split('/').filter(Boolean);
    const method = request.method;

    try {
      // ---- unauthenticated: health and the desktop viewer -------------------
      if (method === 'GET' && url.pathname === '/health') {
        return json(response, 200, { ok: true, service: 'mola-engine', runtime: await runtime.healthy() });
      }
      if (method === 'GET' && url.pathname.startsWith('/novnc/')) {
        return serveNovnc(url.pathname, response);
      }
      if (method === 'GET' && url.pathname === '/desktop') {
        response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
        return response.end(desktopPage());
      }

      // ---- the guest protocol: authenticated by machine credentials ---------
      if (parts[0] === 'guest') {
        const body = await readBody(request);
        if (parts[1] === 'register') {
          const result = guests.register(body);
          return json(response, result.status, result.body);
        }
        const bearer = (request.headers.authorization ?? '').replace(/^Bearer /, '');
        const record = guests.authenticate(bearer);
        if (!record) return json(response, 401, { message: 'Unauthenticated.' });
        if (parts[1] === 'heartbeat') {
          const result = guests.heartbeat(record, body);
          return json(response, result.status, result.body);
        }
        if (parts[1] === 'shutdown-ack') {
          registry.update(record.id, { desired_state: 'stopped' });
          return json(response, 204, {});
        }
        return json(response, 404, { message: 'Not found.' });
      }

      if (parts[0] === 'internal' && parts[1] === 'v1') {
        // Authenticate before parsing a potentially expensive request body.
        if (!hostApi.token) return json(response, 404, { message: 'Not found.' });
        if (!authorised(request, hostApi.token)) return json(response, 401, { message: 'Unauthenticated.' });
        const internalParts = parts.slice(2);
        const bodyLimit = internalParts[2] === 'snapshot-write'
          ? SNAPSHOT_CHUNK_BODY_BYTES
          : 2 * 1024 * 1024;
        const result = await hostApi.handle(request, internalParts, method === 'POST' ? await readBody(request, bodyLimit) : {});
        return json(response, result.status, result.body);
      }

      // ---- everything else needs the operator token ------------------------
      if (!authorised(request, token)) return json(response, 401, { message: 'Unauthenticated. Send Authorization: Bearer <token>.' });

      if (parts[0] !== 'v1') return json(response, 404, { message: 'Not found. The API is served under /v1.' });

      if (method === 'GET' && parts.length === 1) {
        return json(response, 200, {
          service: 'mola-engine',
          version: '0.1.0',
          host: { platform: host.platform, arch: host.arch, accelerator: host.accelerator },
          endpoints: {
            'GET /v1/machines': 'list machines',
            'POST /v1/machines': 'create a new Omarchy machine',
            'GET /v1/machines/{id}': 'describe one machine',
            'POST /v1/machines/{id}/start': 'start it',
            'POST /v1/machines/{id}/stop': 'ask it to shut down (force=true to cut power)',
            'POST /v1/machines/{id}/actions': 'exec, read_file, write_file, screenshot, click, move, scroll, type, key',
            'POST /v1/machines/{id}/desktop': 'mint a browser URL for the desktop',
            'DELETE /v1/machines/{id}': 'destroy it and its disk',
          },
        });
      }

      if (parts[1] !== 'machines') return json(response, 404, { message: 'Not found.' });

      if (method === 'GET' && parts.length === 2) {
        const machines = await Promise.all(registry.all().filter(record => !record.cloud?.deleted).map(async (record) => present(record, await describe(record.id))));
        return json(response, 200, { data: machines });
      }

      if (method === 'POST' && parts.length === 2) {
        const spec = validateSpec(await readBody(request));
        const record = registry.create(spec);
        registry.update(record.id, { authorized_keys: [keys.publicKey] });
        try {
          // Every machine is a fresh Omarchy: the runtime clones the base image
          // rather than reviving anything a previous machine left behind.
          await runtime.create({
            computer_id: record.id,
            name: record.name,
            vcpus: record.vcpus,
            memory_mb: record.memory_mb,
            disk_gb: record.disk_gb,
            registration_token: record.registration_token,
            authorized_keys: [keys.publicKey],
          });
          await runtime.startMachine(record.id);
        } catch (error) {
          // Compensate, and only forget the machine once the runtime confirms
          // it is gone. Dropping the record on a failed create is how a host
          // ends up running a machine nothing knows about: the caller sees an
          // error, the disk and the process live on, and no future request can
          // ever name them.
          try {
            await runtime.settle(record.id);
            await runtime.destroy(record.id, true);
            registry.remove(record.id);
          } catch {
            registry.update(record.id, { needs_cleanup: true, cleanup_reason: 'create failed and rollback failed' });
          }
          throw error;
        }
        return json(response, 201, { data: present(record, await describe(record.id)) });
      }

      const record = parts.length >= 3 ? registry.get(parts[2]) : null;
      if (!record || record.cloud?.deleted) return json(response, 404, { message: 'No such machine.' });
      if (record.cloud && method !== 'GET') return json(response, 409, { message: 'Cloud-managed machines must use the private host API.' });

      if (method === 'GET' && parts.length === 3) {
        return json(response, 200, { data: present(record, await describe(record.id)) });
      }

      if (method === 'DELETE' && parts.length === 3) {
        // A delete that could not tear the machine down must stay visible.
        // Swallowing the failure and removing the row leaves a running VM with
        // no name — the operator's only clue would be the memory it consumes.
        try {
          // Delete means delete: stop it first rather than making the caller
          // discover that the runtime refuses to remove a live machine.
          await runtime.settle(record.id);
          await runtime.destroy(record.id, true);
        } catch (error) {
          registry.update(record.id, { needs_cleanup: true, cleanup_reason: error.message });
          return json(response, 502, {
            message: `The machine could not be destroyed: ${error.message}. It is still registered so it can be retried.`,
          });
        }
        registry.remove(record.id);
        return json(response, 200, { data: { id: record.id, status: 'deleted' } });
      }

      if (method === 'POST' && parts.length === 4) {
        const verb = parts[3];
        if (verb === 'start') {
          await runtime.startMachine(record.id);
          registry.update(record.id, { desired_state: 'running' });
          return json(response, 200, { data: present(registry.get(record.id), await describe(record.id)) });
        }
        if (verb === 'stop') {
          const body = await readBody(request);
          registry.update(record.id, { desired_state: 'stopped' });
          if (body?.force) await runtime.forceStop(record.id);
          else await runtime.shutdown(record.id);
          return json(response, 200, { data: present(registry.get(record.id), await describe(record.id)) });
        }
        if (verb === 'desktop') {
          const described = await describe(record.id);
          if (!described) return json(response, 409, { message: 'Machine is not running.' });
          const ticket = mintTicket(record.id, described);
          return json(response, 201, {
            data: { desktop_url: `http://127.0.0.1:${port}/desktop#t=${ticket}`, expires_in: 60 },
          });
        }
        if (verb === 'actions') {
          const body = validateAction(await readBody(request));
          const described = await describe(record.id);
          if (!described) return json(response, 409, { message: 'Machine is not running.' });
          const result = await runAction({ id: record.id, ...described }, body);
          return json(response, 200, { data: result });
        }
        return json(response, 404, { message: 'Unknown action.' });
      }

      return json(response, 405, { message: 'Method not allowed.' });
    } catch (error) {
      const status = error.status && error.status >= 400 && error.status <= 599 ? error.status
        : (url.pathname.startsWith('/internal/v1/') && !(error instanceof SyntaxError) ? 502 : 400);
      return json(response, status, { message: error.message || 'Request failed.',
        ...(error.code === 'machine_not_found' ? { code: error.code } : {}),
      });
    }
  });

  attachDesktop(server);
  attachSsh(server);

  return { server, runtime, registry, token };
}
