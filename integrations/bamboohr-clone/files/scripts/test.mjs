// bun run test: the backend's own test suite.
import { BACKEND } from './_paths.mjs';
import { run } from './run.mjs';
run('bun', ['test', 'tests'], BACKEND);
