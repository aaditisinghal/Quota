// Shared paths for the run-from-source scripts (they mirror the Dockerfile's steps).
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
export const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
export const BACKEND = resolve(ROOT, 'app/backend/backend-generation-v1');
export const FRONTEND = resolve(ROOT, 'app/frontend/react-clone');
export const DATA = resolve(ROOT, '.local-data');
