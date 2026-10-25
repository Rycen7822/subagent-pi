"""Single-use Windows bootstrap/receipt pipe. Payloads never touch disk or env."""
import asyncio
import uuid

from . import ipc, windows
from .common import AgentError, MAX_FRAME, close_writer, read_frame


class BootstrapChannel:
    @classmethod
    async def open(cls):
        self=cls()
        self.name=rf"\\.\pipe\subagent-pi-bootstrap-{uuid.uuid4().hex}"
        self.pid=None
        loop=asyncio.get_running_loop()
        self.payload=loop.create_future()
        self.sent=loop.create_future()
        self.receipt=loop.create_future()
        self.task=None
        self.writer=None
        self.server=await ipc.PipeServer(self.name,self.handle,MAX_FRAME).start()
        return self

    async def handle(self,reader,writer):
        task=asyncio.current_task()
        try:
            pid=ipc.peer_pid(writer)
            if not self.pid or pid not in windows.job_members(self.pid) or self.task:
                return
            self.task=task
            self.writer=writer
            self.server.close()
            data=await asyncio.shield(self.payload)
            writer.write(data+b'\n')
            await writer.drain()
            if not self.sent.done(): self.sent.set_result('ok')
            value=await read_frame(reader)
            if not self.receipt.done(): self.receipt.set_result(value)
        except (OSError,ValueError,AgentError,windows.JobUnavailable):
            if not self.sent.done(): self.sent.set_result('broken')
            if not self.receipt.done(): self.receipt.set_result(None)
        finally:
            await close_writer(writer)

    async def send(self,data):
        if not self.payload.done(): self.payload.set_result(data)
        try:
            return await asyncio.wait_for(asyncio.shield(self.sent),30)
        except TimeoutError:
            self.close()
            return 'timeout'

    async def read(self,timeout):
        try:
            value=await asyncio.wait_for(asyncio.shield(self.receipt),timeout)
            if value is None: raise AgentError('bridge_unavailable','Bootstrap pipe closed without a receipt')
            return value
        except TimeoutError:
            raise AgentError('bridge_unavailable','Bootstrap receipt timed out') from None
        finally:
            self.close()

    def close(self):
        self.server.close()
        if self.writer: self.writer.transport.abort()
        if self.task and self.task is not asyncio.current_task(): self.task.cancel()
        for future in (self.payload,self.sent,self.receipt):
            if not future.done(): future.cancel()
