/** Windows bootstrap uses a single-use private pipe admitted by guard Job/SID.
 * Run before loading extensions, so the extension's synchronous loader can use
 * the same cached payload and structured receipt as the inherited-FD path. */
import { createConnection } from 'node:net';

export async function bootstrap() {
  const name=process.env.PI_AGENTS_BOOTSTRAP_PIPE;
  delete process.env.PI_AGENTS_BOOTSTRAP_PIPE;
  if (!name) return;
  const socket=createConnection(name);
  const payload=await new Promise((resolve,reject)=>{
    let data=Buffer.alloc(0);
    const timer=setTimeout(()=>{ socket.destroy(); reject(new Error('Bootstrap timed out')); },30000);
    const fail=(error)=>{ clearTimeout(timer); socket.destroy(); reject(error); };
    const receive=(chunk)=>{
      data=Buffer.concat([data,chunk]);
      if(data.length>8*1024*1024) return fail(new Error('Bootstrap exceeds limit'));
      const end=data.indexOf(10);
      if(end<0) return;
      clearTimeout(timer); socket.off('data',receive);
      try { resolve(JSON.parse(data.subarray(0,end).toString('utf8'))); }
      catch(error) { fail(error); }
    };
    socket.on('error',fail);
    socket.once('end',()=>fail(new Error('Bootstrap pipe closed')));
    socket.on('data',receive);
  });
  if(payload?.v!==1 || !Array.isArray(payload?.mcp?.servers)) { socket.destroy(); throw new Error('Malformed bootstrap'); }
  globalThis.__subagentPiBridgeBootstrap={payload};
  globalThis.__subagentPiBridgeReceipt=(receipt)=>{
    socket.end(JSON.stringify(receipt)+'\n');
    delete globalThis.__subagentPiBridgeReceipt;
  };
}
