import { spawnSync } from 'node:child_process';
export function run(cmd, args, cwd, env = {}) {
  const r = spawnSync(cmd, args, { cwd, stdio: 'inherit', shell: process.platform === 'win32', env: { ...process.env, ...env } });
  if (r.status !== 0) process.exit(r.status ?? 1);
}
