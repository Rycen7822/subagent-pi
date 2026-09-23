import { test } from 'node:test';
import assert from 'node:assert/strict';
import { TaskQueue } from '../runtime/task-queue.mjs';
const gate = () => { let resolve; const promise = new Promise(r => resolve = r); return { promise, resolve }; };

test('one owner serializes continuations and completes once after all of them', async () => {
  const done = gate(), block = gate(), started = gate(), seen = [], terminal = [];
  const queue = new TaskQueue(async input => {
    seen.push(input);
    if (input === 'root') { queue.enqueue('one'); queue.enqueue('two'); started.resolve(); await block.promise; }
    if (input === 'one') queue.enqueue('three');
    queue.observe({ type: 'message_end', message: { role: 'assistant' } });
  }, event => { terminal.push(event); done.resolve(); });
  queue.start('a', 'root');
  await started.promise;
  assert.throws(() => queue.start('b', 'intruder'), /already active/);
  assert.deepEqual(seen, ['root']);
  assert.equal(terminal.length, 0);
  block.resolve(); await done.promise;
  assert.deepEqual(seen, ['root', 'one', 'two', 'three']);
  assert.deepEqual(terminal, [{ type: 'managed_task_end', runId: 'a', error: undefined }]);
});

test('callbacks from a completed owner cannot join the next task', async () => {
  const first = gate(), second = gate(), block = gate(), attempted = gate();
  let stale;
  const seen = [];
  const queue = new TaskQueue(async input => {
    seen.push(input);
    if (input === 'first') {
      const owner = queue.context.getStore();
      stale = () => queue.context.run(owner, () => queue.enqueue('stale'));
    } else { attempted.resolve(); await block.promise; }
    queue.observe({ type: 'message_end', message: { role: 'assistant' } });
  }, event => (event.runId === 'a' ? first : second).resolve());
  queue.start('a', 'first'); await first.promise;
  queue.start('b', 'second'); await attempted.promise;
  assert.throws(stale, /already ended/);
  block.resolve(); await second.promise;
  assert.deepEqual(seen, ['first', 'second']);
});

test('handled input completes visibly as failure without waiting for a host event', async () => {
  const done = gate();
  const queue = new TaskQueue(async () => {}, done.resolve);
  queue.start('handled', 'text');
  const result = await done.promise;
  assert.match(result.error, /without producing/);
  assert.equal(result.runId, 'handled');
});

test('SDK rejection discards its queued continuations and releases the owner', async () => {
  const done = gate(); const seen = [];
  const queue = new TaskQueue(async input => {
    seen.push(input); queue.enqueue('must not execute'); throw new Error('SDK failed');
  }, done.resolve);
  queue.start('failure', 'first');
  assert.match((await done.promise).error, /SDK failed/);
  assert.deepEqual(seen, ['first']); assert.equal(queue.active, undefined);
});

test('accepted extension inputs have both count and byte limits', async () => {
  async function blockedQueue() {
    const started = gate(), release = gate(), done = gate();
    const queue = new TaskQueue(async input => {
      if (input === 'root') { started.resolve(); await release.promise; }
    }, done.resolve);
    queue.start('owner', 'root'); await started.promise;
    return { queue, release, done };
  }
  const count = await blockedQueue();
  count.queue.context.run(count.queue.active, () => {
    for (let i = 0; i < 256; i++) count.queue.enqueue('x');
    assert.throws(() => count.queue.enqueue('extra'), /capacity/);
  });
  count.release.resolve(); await count.done.promise;
  assert.equal(count.queue.active, undefined);

  const bytes = await blockedQueue();
  bytes.queue.context.run(bytes.queue.active, () => {
    const chunk = 'x'.repeat(3 * 1024 * 1024);
    bytes.queue.enqueue(chunk); bytes.queue.enqueue(chunk);
    assert.throws(() => bytes.queue.enqueue(chunk), /capacity/);
  });
  bytes.release.resolve(); await bytes.done.promise;
  assert.equal(bytes.queue.active, undefined);
});
