import { AsyncLocalStorage } from 'node:async_hooks';

const MAX_QUEUED_INPUTS = 256;
const MAX_QUEUED_BYTES = 8 * 1024 * 1024;

/** One daemon task owns all continuations accepted during its SDK calls.
 * Nothing enters AgentSession concurrently. Async callbacks keep their original
 * owner, so a late timer cannot attach itself to the next daemon task. */
export class TaskQueue {
  context = new AsyncLocalStorage();
  active;

  constructor(execute, complete) {
    this.execute = execute;
    this.complete = complete;
  }

  start(id, input) {
    if (this.active) throw new Error('A managed task is already active');
    const task = { id, inputs: [input], sizes: [0], queuedBytes: 0, replies: 0 };
    this.active = task;
    void this.context.run(task, async () => {
      let error;
      try {
        while (task.inputs.length) {
          const next = task.inputs.shift();
          task.queuedBytes -= task.sizes.shift();
          await this.execute(next);
        }
        if (!task.replies) error = 'Pi handled the input without producing an assistant result';
      } catch (exc) {
        error = String(exc);
      } finally {
        task.inputs.length = 0; task.sizes.length = 0; task.queuedBytes = 0;
        this.active = undefined;
        this.complete({ type: 'managed_task_end', runId: id, error });
      }
    });
  }

  enqueue(input, owner = this.context.getStore()) {
    if (!owner || owner !== this.active) {
      const error = 'Input rejected: its managed task has already ended or no task owns it';
      throw new Error(error);
    }
    const size = Buffer.byteLength(JSON.stringify(input));
    if (owner.inputs.length >= MAX_QUEUED_INPUTS || owner.queuedBytes + size > MAX_QUEUED_BYTES) {
      const error = new Error('Managed SDK input queue capacity exceeded');
      error.code = 'managed_queue_full';
      throw error;
    }
    owner.inputs.push(input);
    owner.sizes.push(size);
    owner.queuedBytes += size;
  }

  observe(event) {
    if (event.type === 'message_end' && event.message?.role === 'assistant' && this.active) {
      this.active.replies++;
    }
  }
}
