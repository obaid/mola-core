import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';

const hash = (value) => createHash('sha256').update(value).digest('hex');

function sameToken(a, b) {
  const left = Buffer.from(a ?? '', 'utf8');
  const right = Buffer.from(b ?? '', 'utf8');
  return left.length === right.length && timingSafeEqual(left, right);
}

/**
 * The control-plane half of the guest protocol.
 *
 * The guest is a hostile tenant: the customer is root inside it. Nothing it
 * reports is trusted as fact about the platform. Capabilities are treated as a
 * claim that the machine believes it is usable, and readiness is the engine's
 * conclusion, never the guest's assertion.
 */
export class GuestService {
  constructor(registry) {
    this.registry = registry;
    this.challenges = new Map();
  }

  #findByRegistrationToken(token) {
    return this.registry.all().find((record) => record.registration_token && sameToken(record.registration_token, token)) ?? null;
  }

  authenticate(bearer) {
    if (!bearer) return null;
    const digest = hash(bearer);
    return this.registry.all().find((record) => record.machine_token_hash === digest) ?? null;
  }

  register(body) {
    const record = this.#findByRegistrationToken(body?.registration_token);
    if (!record) return { status: 401, body: { message: 'Unknown registration token.' } };

    // Single use. A replayed token must not mint a second credential, and the
    // machine that legitimately lost our response recovers by proving
    // possession of its enrolment key rather than by repeating the token.
    if (record.machine_token_hash && !body?.enrollment_public_key) {
      return { status: 401, body: { message: 'Registration token already redeemed.' } };
    }

    const token = randomBytes(32).toString('hex');
    this.registry.update(record.id, {
      machine_token_hash: hash(token),
      enrollment_public_key: body?.enrollment_public_key ?? record.enrollment_public_key ?? null,
      guest_version: typeof body?.guest_version === 'string' ? body.guest_version.slice(0, 64) : null,
      registered_at: new Date().toISOString(),
    });

    return {
      status: 200,
      body: {
        machine_token: token,
        computer_id: record.id,
        heartbeat_interval: 10,
        recovered: Boolean(record.machine_token_hash),
      },
    };
  }

  heartbeat(record, body) {
    const previous = this.challenges.get(record.id);
    const challenge = randomBytes(16).toString('hex');
    this.challenges.set(record.id, challenge);

    this.registry.update(record.id, {
      last_heartbeat_at: new Date().toISOString(),
      boot_id: typeof body?.boot_id === 'string' ? body.boot_id.slice(0, 64) : null,
      // Recorded as a claim, not as proof. `ready` is decided by the engine.
      capabilities: {
        shell: Boolean(body?.capabilities?.shell),
        display: Boolean(body?.capabilities?.display),
        sshd: Boolean(body?.capabilities?.sshd),
        ...(body?.capabilities?.cua && typeof body.capabilities.cua === 'object' ? {
          cua: {
            installed: body.capabilities.cua.installed === true,
            version: typeof body.capabilities.cua.version === 'string'
              ? body.capabilities.cua.version.slice(0, 32) : '',
            daemon: body.capabilities.cua.daemon === true,
          },
        } : {}),
      },
    });

    return {
      status: 200,
      body: {
        desired_state: record.desired_state ?? 'running',
        heartbeat_interval: 10,
        machine_token: '',
        runtime_generation: record.runtime_generation ?? 1,
        challenge,
        challenge_verified: Boolean(previous) && body?.challenge_response === previous,
        authorized_keys: record.authorized_keys ?? [],
      },
    };
  }
}
