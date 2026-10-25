// bun run start: one port serves the SPA and the API. QUOTA_ROSTER (a path from where you run this) seeds Quota's
// cast; the database lives in .local-data/clone.db (POST /__clone/reset, or delete the file, to re-seed).
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { BACKEND, FRONTEND, DATA } from './_paths.mjs';
import { run } from './run.mjs';
mkdirSync(DATA, { recursive: true });
const env = { STATIC_DIR: resolve(FRONTEND, 'dist'), DATABASE_URL: process.env.DATABASE_URL || `file:${resolve(DATA, 'clone.db')}` };
if (process.env.QUOTA_ROSTER) env.QUOTA_ROSTER = resolve(process.cwd(), process.env.QUOTA_ROSTER);
run('bun', ['server.mjs'], BACKEND, env);
