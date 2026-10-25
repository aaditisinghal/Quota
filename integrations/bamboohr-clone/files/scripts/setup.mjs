// bun run setup: install backend and frontend dependencies and generate the Prisma client.
import { BACKEND, FRONTEND } from './_paths.mjs';
import { run } from './run.mjs';
run('bun', ['install', '--frozen-lockfile'], BACKEND);
run('bun', ['x', 'prisma', 'generate', '--schema', 'prisma/schema.prisma'], BACKEND, { CHECKPOINT_DISABLE: '1' });
run('bun', ['install', '--frozen-lockfile'], FRONTEND);
