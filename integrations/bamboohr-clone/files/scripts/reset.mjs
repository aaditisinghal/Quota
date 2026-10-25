// bun run reset: delete the local database so the next start re-seeds it.
import { rmSync } from 'node:fs';
import { DATA } from './_paths.mjs';
rmSync(DATA, { recursive: true, force: true });
console.log('removed', DATA);
