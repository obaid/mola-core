import { waitForReady } from './engine.js';

/**
 * The tools, and the words that make a model use them well.
 *
 * Descriptions are load bearing. A model that does not know `run_command` exists
 * will drive the GUI to open a terminal and type into it, which is slower, less
 * reliable and much more expensive than one shell call. So each description says
 * not just what the tool does but when to reach for it.
 */

const text = (value) => ({ content: [{ type: 'text', text: value }] });
const json = (value) => text(JSON.stringify(value, null, 2));

/** Where a background task keeps its output, one file per handle. */
const taskLog = (handle) => `/tmp/mola-task-${handle}.log`;

function describeMachine(m) {
  return {
    id: m.id,
    name: m.name,
    status: m.status,
    vcpus: m.vcpus,
    memory_mb: m.memory_mb,
    disk_gb: m.disk_gb,
  };
}

export function buildTools(engine) {
  const tools = [];
  const define = (spec, run) => {
    tools.push({ spec, run });
  };

  const machineArg = {
    machine_id: { type: 'string', description: 'The id returned by create_machine.' },
  };

  // ---- lifecycle -----------------------------------------------------------

  define({
    name: 'create_machine',
    title: 'Create an Omarchy computer',
    description:
      'Create a fresh Linux computer and wait until it can accept commands. It runs Arch '
      + 'Linux with the Hyprland desktop, takes about a second to create and about seven '
      + 'seconds to become usable. Every machine is a clean copy; nothing carries over from '
      + 'a previous one. Call this once at the start of a task and reuse the id.',
    inputSchema: {
      type: 'object',
      properties: {
        name: { type: 'string', description: 'A label to recognise it by. Optional.' },
        vcpus: { type: 'integer', description: 'Optional. Uses the host-configured default (4 unless changed).' },
        memory_mb: { type: 'integer', description: 'Optional. Uses the host-configured default (4096 MB unless changed). Each running machine holds this much RAM on the host.' },
        disk_gb: { type: 'integer', description: 'Optional. Uses the host-configured default (40 GB unless changed).' },
      },
    },
  }, async (args) => {
    const machine = await engine.createMachine({
      ...(args.name ? { name: args.name } : {}),
      ...(args.vcpus ? { vcpus: args.vcpus } : {}),
      ...(args.memory_mb ? { memory_mb: args.memory_mb } : {}),
      ...(args.disk_gb ? { disk_gb: args.disk_gb } : {}),
    });
    const ready = await waitForReady(engine, machine.id);
    return json({
      ...describeMachine(ready),
      note: 'Ready. Prefer run_command for anything a shell can do; use the screen tools only for genuinely graphical work.',
    });
  });

  define({
    name: 'list_machines',
    title: 'List computers',
    description: 'List the computers this engine currently has, with their status.',
    inputSchema: { type: 'object', properties: {} },
  }, async () => {
    const machines = await engine.listMachines();
    return json((Array.isArray(machines) ? machines : []).map(describeMachine));
  });

  define({
    name: 'stop_machine',
    title: 'Stop a computer',
    description:
      'Shut a computer down. Its disk is kept, so starting it again resumes where it left '
      + 'off. Use this rather than delete when the work might continue later.',
    inputSchema: { type: 'object', properties: machineArg, required: ['machine_id'] },
  }, async (a) => json(await engine.stopMachine(a.machine_id)));

  define({
    name: 'start_machine',
    title: 'Start a stopped computer',
    description: 'Start a computer that was stopped, and wait until it accepts commands again.',
    inputSchema: { type: 'object', properties: machineArg, required: ['machine_id'] },
  }, async (a) => {
    await engine.startMachine(a.machine_id);
    return json(describeMachine(await waitForReady(engine, a.machine_id)));
  });

  define({
    name: 'delete_machine',
    title: 'Delete a computer',
    description:
      'Destroy a computer and its disk. This cannot be undone. Do this when the work is '
      + 'finished, because a running machine holds several gigabytes of the host.',
    inputSchema: { type: 'object', properties: machineArg, required: ['machine_id'] },
  }, async (a) => {
    await engine.deleteMachine(a.machine_id);
    return text(`Deleted ${a.machine_id} and its disk.`);
  });

  // ---- shell ---------------------------------------------------------------

  define({
    name: 'run_command',
    title: 'Run a shell command',
    description:
      'Run a shell command and wait for it to finish. This is the tool to reach for first: '
      + 'anything you could do in a terminal is faster and more reliable here than through '
      + 'the screen. Runs as the user "dev", who has passwordless sudo. Capped at 120 '
      + 'seconds, so use start_task for installs, builds and downloads.',
    inputSchema: {
      type: 'object',
      properties: {
        ...machineArg,
        command: { type: 'string', description: 'The command line to run.' },
        timeout: { type: 'integer', description: 'Seconds, 1 to 120. Defaults to 60.' },
      },
      required: ['machine_id', 'command'],
    },
  }, async (a) => {
    const result = await engine.act(a.machine_id, {
      action: 'exec',
      command: a.command,
      timeout: Math.min(Math.max(a.timeout ?? 60, 1), 120),
    });
    if (result.timed_out) {
      return {
        ...text(
          `The command hit its ${a.timeout ?? 60}s limit and was stopped.\n\n`
          + `Partial output:\n${result.stdout}${result.stderr}\n\n`
          + 'Run it with start_task instead, which has no time limit.',
        ),
        isError: true,
      };
    }
    return json({
      exit_code: result.exit_code,
      stdout: result.stdout,
      stderr: result.stderr,
      truncated: result.truncated,
    });
  });

  define({
    name: 'start_task',
    title: 'Start a long command in the background',
    description:
      'Start a command that will take longer than two minutes, such as a package install, '
      + 'a build or a large download. Returns immediately with a handle. Poll check_task '
      + 'to see how it is going. Use this for anything with pacman, npm, pip, cargo, make '
      + 'or curl of a large file.',
    inputSchema: {
      type: 'object',
      properties: {
        ...machineArg,
        command: { type: 'string', description: 'The command line to run.' },
      },
      required: ['machine_id', 'command'],
    },
  }, async (a) => {
    const handle = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
    const log = taskLog(handle);
    // setsid detaches the process from the SSH session, so it survives the exec
    // call returning. Without it the command dies the moment we disconnect.
    const wrapped =
      `setsid nohup sh -c ${shellQuote(a.command)} > ${log} 2>&1 < /dev/null &\n`
      + `echo $! > ${log}.pid`;
    await engine.act(a.machine_id, { action: 'exec', command: wrapped, timeout: 20 });
    return json({
      handle,
      log,
      note: 'Started. Call check_task with this handle. Do not assume it succeeded until check_task says it finished.',
    });
  });

  define({
    name: 'check_task',
    title: 'Check a background command',
    description:
      'Report whether a background command started by start_task is still running, and '
      + 'return the end of its output.',
    inputSchema: {
      type: 'object',
      properties: {
        ...machineArg,
        handle: { type: 'string', description: 'The handle returned by start_task.' },
        lines: { type: 'integer', description: 'How many lines of output to return. Defaults to 40.' },
      },
      required: ['machine_id', 'handle'],
    },
  }, async (a) => {
    const log = taskLog(a.handle);
    const lines = Math.min(Math.max(a.lines ?? 40, 1), 500);
    const result = await engine.act(a.machine_id, {
      action: 'exec',
      timeout: 30,
      command:
        `if [ -f ${log}.pid ] && kill -0 "$(cat ${log}.pid)" 2>/dev/null; `
        + 'then echo RUNNING; else echo FINISHED; fi; '
        + `echo '---'; tail -n ${lines} ${log} 2>/dev/null || echo '(no output yet)'`,
    });
    const [state, ...rest] = result.stdout.split('\n---\n');
    return json({
      running: state.trim() === 'RUNNING',
      output: rest.join('\n---\n').trimEnd(),
    });
  });

  // ---- files ---------------------------------------------------------------

  define({
    name: 'read_file',
    title: 'Read a file',
    description: 'Read a file from the computer. Paths starting with ~ expand in the guest. Refuses files over 1 MiB.',
    inputSchema: {
      type: 'object',
      properties: { ...machineArg, path: { type: 'string' } },
      required: ['machine_id', 'path'],
    },
  }, async (a) => {
    const result = await engine.act(a.machine_id, { action: 'read_file', path: a.path });
    return text(Buffer.from(result.content_base64, 'base64').toString('utf8'));
  });

  define({
    name: 'write_file',
    title: 'Write a file',
    description: 'Write a file on the computer, creating parent directories as needed.',
    inputSchema: {
      type: 'object',
      properties: { ...machineArg, path: { type: 'string' }, content: { type: 'string' } },
      required: ['machine_id', 'path', 'content'],
    },
  }, async (a) => {
    await engine.act(a.machine_id, { action: 'write_file', path: a.path, content: a.content });
    return text(`Wrote ${a.path}.`);
  });

  // ---- screen --------------------------------------------------------------

  define({
    name: 'screenshot',
    title: 'Look at the screen',
    description:
      'Take a picture of the desktop, 1280x800. Use this to see the result of a graphical '
      + 'action, or when you need to find something to click. You do not need it to check '
      + 'whether a shell command worked; run_command already tells you that.',
    inputSchema: { type: 'object', properties: machineArg, required: ['machine_id'] },
  }, async (a) => {
    const shot = await engine.act(a.machine_id, { action: 'screenshot' });
    // The engine's own field names are MCP image content under different
    // spelling, so this is a rename rather than a conversion.
    return { content: [{ type: 'image', data: shot.image_base64, mimeType: shot.mime_type }] };
  });

  define({
    name: 'click',
    title: 'Click the screen',
    description: 'Click at a point on the screen. The screen is 1280x800 with 0,0 at the top left.',
    inputSchema: {
      type: 'object',
      properties: {
        ...machineArg,
        x: { type: 'integer' },
        y: { type: 'integer' },
        button: { type: 'integer', description: '1 left, 2 middle, 3 right. Defaults to 1.' },
      },
      required: ['machine_id', 'x', 'y'],
    },
  }, async (a) => {
    await engine.act(a.machine_id, { action: 'click', x: a.x, y: a.y, button: a.button ?? 1 });
    return text(`Clicked ${a.x},${a.y}.`);
  });

  define({
    name: 'move_mouse',
    title: 'Move the pointer',
    description: 'Move the pointer without clicking, to reveal a hover state or a tooltip.',
    inputSchema: {
      type: 'object',
      properties: { ...machineArg, x: { type: 'integer' }, y: { type: 'integer' } },
      required: ['machine_id', 'x', 'y'],
    },
  }, async (a) => {
    await engine.act(a.machine_id, { action: 'move', x: a.x, y: a.y });
    return text(`Moved to ${a.x},${a.y}.`);
  });

  define({
    name: 'scroll',
    title: 'Scroll',
    description:
      'Scroll the wheel. This acts wherever the pointer currently is, not at a position you '
      + 'give it, so call move_mouse over the pane you mean to scroll first. Take a '
      + 'screenshot afterwards to see what moved.',
    inputSchema: {
      type: 'object',
      properties: {
        ...machineArg,
        direction: { type: 'string', enum: ['up', 'down', 'left', 'right'] },
        amount: { type: 'integer', description: 'Clicks of the wheel. Defaults to 3.' },
      },
      required: ['machine_id', 'direction'],
    },
  }, async (a) => {
    await engine.act(a.machine_id, { action: 'scroll', direction: a.direction, amount: a.amount ?? 3 });
    return text(`Scrolled ${a.direction}.`);
  });

  define({
    name: 'type_text',
    title: 'Type text',
    description: 'Type text into whatever has keyboard focus. Click the thing you want to type into first.',
    inputSchema: {
      type: 'object',
      properties: { ...machineArg, text: { type: 'string' } },
      required: ['machine_id', 'text'],
    },
  }, async (a) => {
    await engine.act(a.machine_id, { action: 'type', text: a.text });
    return text(`Typed ${a.text.length} characters.`);
  });

  define({
    name: 'press_key',
    title: 'Press a key',
    description:
      'Press one key or a combination joined with "-", such as enter, escape, ctrl-c or '
      + 'super-return. In Omarchy: super-return opens a terminal, super-space the launcher, '
      + 'super-k the keybinding list, super-w closes a window.',
    inputSchema: {
      type: 'object',
      properties: { ...machineArg, key: { type: 'string' } },
      required: ['machine_id', 'key'],
    },
  }, async (a) => {
    await engine.act(a.machine_id, { action: 'key', key: a.key });
    return text(`Pressed ${a.key}.`);
  });

  // ---- the human -----------------------------------------------------------

  define({
    name: 'open_desktop',
    title: 'Get a link to watch the screen',
    description:
      'Return a URL the person can open to watch and control this desktop in their browser. '
      + 'Offer it when they would benefit from seeing the work, or when you want them to do '
      + 'something you cannot, such as signing in. The link works once and expires in sixty '
      + 'seconds, so give it to them immediately and call this again if they need another.',
    inputSchema: { type: 'object', properties: machineArg, required: ['machine_id'] },
  }, async (a) => {
    const { desktop_url: url, expires_in: expires } = await engine.desktop(a.machine_id);
    return text(
      `${url}\n\nThis opens the live desktop, mouse and keyboard included. `
      + `It works once and expires in ${expires} seconds.`,
    );
  });

  return tools;
}

/** Quote a command for `sh -c`, so a task's own quoting cannot break the wrapper. */
function shellQuote(command) {
  return `'${command.replaceAll("'", `'\\''`)}'`;
}

export { shellQuote };
