#!/usr/bin/env node
/**
 * One-time development setup for TypeScript type checking (never needed at
 * runtime): locates the globally installed Pi distribution and stages its
 * type declarations under node_modules/pi-host so `npm run typecheck` can
 * resolve `@earendil-works/pi-coding-agent`, `typebox` and Node's types.
 * Nothing here is packaged, cached at runtime, or downloaded by the product.
 */
import { execSync } from 'node:child_process';
import { existsSync, realpathSync, lstatSync, mkdirSync, cpSync, rmSync, unlinkSync } from 'node:fs';
import { dirname, join } from 'node:path';

const which = (cmd) => {
  try { return execSync(`command -v ${cmd}`, { shell: '/bin/bash' }).toString().trim(); }
  catch { return null; }
};
const exe = which('pi');
if (!exe) throw new Error('pi executable not found; install Pi first');
const real = realpathSync(exe);
let piDir = null;
for (const parent of [real, ...Array(8).fill().map((_, i) => real.split('/').slice(0, -(i + 1)).join('/'))]) {
  if (existsSync(join(parent, 'dist', 'index.d.ts'))) { piDir = parent; break; }
}
if (!piDir) throw new Error('could not locate the Pi distribution (dist/index.d.ts) from ' + real);
const isLink = (path) => {
  try { return lstatSync(path).isSymbolicLink(); } catch { return false; }
};
/** Replace `dest` with a fresh copy of `src`. A stale directory must go, but a
 *  symlink is only unlinked: removing it recursively could delete the Pi
 *  install it points at (`npm install`/`setup` run this repeatedly). */
const stageDir = (src, dest) => {
  if (isLink(dest)) unlinkSync(dest);
  else if (existsSync(dest)) rmSync(dest, { recursive: true, force: true });
  mkdirSync(dirname(dest), { recursive: true });
  cpSync(src, dest, { recursive: true });
};
if (isLink('node_modules/pi-host')) unlinkSync('node_modules/pi-host');
else rmSync('node_modules/pi-host', { recursive: true, force: true });
stageDir(join(piDir, 'dist'), join('node_modules', 'pi-host', 'dist'));
stageDir(join(piDir, 'node_modules', 'typebox'), join('node_modules', 'pi-host', 'node_modules', 'typebox'));
stageDir(join(piDir, 'node_modules', '@types', 'node'), join('node_modules', 'pi-host', 'node_modules', '@types', 'node'));
stageDir(join(piDir, 'node_modules', '@types', 'node'), join('node_modules', '@types', 'node'));
console.log('staged Pi types from', piDir);
