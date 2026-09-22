import { AsyncLocalStorage } from 'node:async_hooks';

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
    const task = { id, inputs: [input], replies: 0 };
    this.active = task;
    void this.context.run(task, async () => {
      let error;
      try {
        while (task.inputs.length) await this.execute(task.inputs.shift());
        if (!task.replies) error = 'Pi handled the input without producing an assistant result';
      } catch (exc) {
        error = String(exc);
      } finally {
        task.inputs.length = 0;
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
    owner.inputs.push(input);
  }

  observe(event) {
    if (event.type === 'message_end' && event.message?.role === 'assistant' && this.active) {
      this.active.replies++;
    }
  }
}
