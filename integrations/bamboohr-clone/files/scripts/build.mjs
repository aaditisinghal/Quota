// bun run build: build the frontend into app/frontend/react-clone/dist (served as STATIC_DIR).
import { FRONTEND } from './_paths.mjs';
import { run } from './run.mjs';
run('bun', ['run', 'build'], FRONTEND);
