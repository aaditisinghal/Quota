// The CFO presenter script (DEMO_FLOW_TASKS D6). Unlike the default script, which the server lists at
// GET /demo/steps, this one lives in the client: each step names the screen to show (a location hash) and,
// when the server's state must change there, what changes it. A step without `server` keeps the state of the
// nearest earlier step that has one.
//
// The server's cfo steps (POST /demo/step {n, script: "cfo"}): 0-5 the morning (one state), 6 June (cp_end),
// 7 the May close (Birch churns is run from there), 8 June with nothing left that needs a person.

export type CfoServerAction =
  | { cfoStep: number } // POST /demo/step {n, script: "cfo"}
  | { demoStep: number } // POST /demo/step {n}: a checkpoint of the default script
  | { scenario: string } // POST /scenarios/{name}/run, on top of the state before it
  | { reset: true }; // POST /demo/reset

/** `server` is one action, or several run in order (a checkpoint, then a scenario on top of it). */
export type CfoStep = { narration: string; hash: string; server?: CfoServerAction | CfoServerAction[] };

export const CFO_SCRIPT: CfoStep[] = [
  { narration: "A normal morning: everything Quota reads, and what needs a person.", hash: "home", server: { cfoStep: 0 } },
  { narration: "The Acme contract, opened in the raw viewer: what Quota read, word for word.", hash: "home/source/contracts" },
  { narration: "One email, exactly as it arrived.", hash: "home/source/messages" },
  { narration: "Every rep, needs-attention first.", hash: "reps" },
  { narration: "Jordan Kim: each deal, what he is owed, and what is still open.", hash: "rep/jordan.kim" },
  {
    narration: "Acme: CRM and contract disagree, settled by the ranking: 80, cleared with a note; Jordan's dispute thread.",
    hash: "rep/jordan.kim/acme/calibration",
  },
  { narration: "Orbit: one split bar, Alex 60% and Riley 40%.", hash: "rep/alex.chen/orbit" },
  { narration: "Riley's view of the same contract.", hash: "rep/riley.shah/orbit" },
  { narration: "The May amendment: incremental commission only, credited on the same split.", hash: "rep/alex.chen/orbit+v2" },
  { narration: "Needs you: the Summit side letter and the Acme exception chain, by person.", hash: "home" },
  { narration: "June: Acme churns; one event, eight numbers.", hash: "today", server: { cfoStep: 6 } },
  {
    narration: "Ctrl-K: Birch churns; both chargebacks to Priya.",
    hash: "today",
    server: [{ cfoStep: 7 }, { scenario: "birch_churns" }],
  },
  { narration: "Trace with as-of.", hash: "trace/CL-0002" },
  { narration: "The scorecard.", hash: "inject" },
  { narration: "Close: Needs you: 0.", hash: "home", server: { cfoStep: 8 } },
];

/** The step whose server action sets the state seen at step i (the nearest one at or before it), or -1. */
function owner(script: CfoStep[], i: number): number {
  for (let k = Math.min(i, script.length - 1); k >= 0; k--) if (script[k].server) return k;
  return -1;
}

/**
 * The actions that take the server from the state of step `from` to the state of step `to`, or [] when the
 * two share a state (Back included: it re-runs the nearest earlier step's actions). Every step's actions
 * set the whole state on their own, so nothing has to be replayed. `force` runs them even when the state
 * is shared, for Reset.
 */
export function actionsBetween(script: CfoStep[], from: number, to: number, force = false): CfoServerAction[] {
  const target = owner(script, to);
  if (target < 0) return [];
  if (!force && owner(script, from) === target) return [];
  const server = script[target].server!;
  return Array.isArray(server) ? server : [server];
}
