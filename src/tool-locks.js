const queues = new Map();

/** The HTTP, stdio, Cua and warm action transports share one interactive lane
 * per computer. A background install has its own guest-side installer lease. */
export async function serializedComputerTool(id, work) {
  let entry = queues.get(id);
  if (!entry) { entry = { tail: Promise.resolve(), count: 0 }; queues.set(id, entry); }
  if (entry.count >= 64) throw Object.assign(new Error('Computer tool queue is full.'), { status: 429, code: 'computer_tool_busy' });
  entry.count += 1;
  const previous = entry.tail;
  const next = previous.catch(() => {}).then(work);
  entry.tail = next;
  try { return await next; }
  finally { entry.count -= 1; if (!entry.count) queues.delete(id); }
}
