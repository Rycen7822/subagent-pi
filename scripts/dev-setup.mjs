#!/usr/bin/env node
/**
 * One-time development setup for TypeScript type checking (never needed at
 * runtime): locates the globally installed Pi distribution and stages its
 * type declarations under node_modules/pi-host so `npm run typecheck` can
 * resolve `@earendil-works/pi-coding-agent`, `typebox` and Node's types.
 * Nothing here is packaged, cached at runtime, or downloaded by the product.
 */
import { execSync } from 'node:child_process';
import { existsSync, mkdirSync, cpSync, rmSync } from 'node:fs';
import { join } from 'node:path';

const which = (cmd) => {
  try { return execSync(`command -v ${cmd}`, { shell: '/bin/bash' }).toString().trim(); }
  catch { return null; }
};
const exe = which('pi');
if (!exe) throw new Error('pi executable not found; install Pi first');
const real = execSync(`readlink -f ${exe}`).toString().trim();
let piDir = null;
for (const parent of [real, ...Array(8).fill().map((_, i) => real.split('/').slice(0, -(i + 1)).join('/'))]) {
  if (existsSync(join(parent, 'dist', 'index.d.ts'))) { piDir = parent; break; }
}
if (!piDir) throw new Error('could not locate the Pi distribution (dist/index.d.ts) from ' + real);
rmSync('node_modules/pi-host', { recursive: true, force: true });
mkdirSync(join('node_modules', 'pi-host', 'node_modules'), { recursive: true });
cpSync(join(piDir, 'dist'), join('node_modules', 'pi-host', 'dist'), { recursive: true });
cpSync(join(piDir, 'node_modules', 'typebox'), join('node_modules', 'pi-host', 'node_modules', 'typebox'), { recursive: true });
cpSync(join(piDir, 'node_modules', '@types', 'node'), join('node_modules', 'pi-host', 'node_modules', '@types', 'node'), { recursive: true });
mkdirSync(join('node_modules', '@types'), { recursive: true });
rmSync(join('node_modules', '@types', 'node'), { force: true });
cpSync(join(piDir, 'node_modules', '@types', 'node'), join('node_modules', '@types', 'node'), { recursive: true });
console.log('staged Pi types from', piDir);
