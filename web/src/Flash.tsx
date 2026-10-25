import { useEffect, useState, type ReactNode } from "react";
import { Money } from "./format";

// R7.5: a figure that changed since it was last shown flashes for 600 ms and counts from the old value to the new.
// Last values are kept per figure for the whole page, so a change made on another tab still shows on return.
// Under prefers-reduced-motion the new value just appears.

const LAST = new Map<string, number | string>();
const MS = 600;

function reduced(): boolean {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}

/** Track `value` under `id`; returns the value to draw now and a counter that bumps on each change. */
function useChange<T extends number | string>(id: string, value: T, tween: boolean): [T, number] {
  const [shown, setShown] = useState<T>(value);
  const [flashes, setFlashes] = useState(0);
  useEffect(() => {
    const from = LAST.get(id);
    LAST.set(id, value);
    if (from === undefined || from === value || reduced()) {
      setShown(value);
      return;
    }
    setFlashes((n) => n + 1);
    if (!tween || typeof from !== "number" || typeof value !== "number") {
      setShown(value);
      return;
    }
    const start = performance.now();
    let frame = 0;
    const tick = (t: number) => {
      const p = Math.min(1, (t - start) / MS);
      const eased = 1 - (1 - p) ** 3;
      setShown((p < 1 ? Math.round(from + (value - from) * eased) : value) as T);
      if (p < 1) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [id, value, tween]);
  return [shown, flashes];
}

/** A dollar figure that flashes and tweens when it changes. `blankZero` draws nothing at zero (a Dr/Cr column). */
export function FlashMoney({ id, cents, trace, blankZero }: { id: string; cents: number; trace?: string | null; blankZero?: boolean }) {
  const [shown, flashes] = useChange(id, cents, true);
  if (blankZero && shown === 0 && cents === 0) return null;
  return (
    <span key={flashes} className={flashes ? "flash" : undefined}>
      <Money cents={shown} trace={trace} />
    </span>
  );
}

/** Any block whose text changed flashes (no tween): a cascade line, a status. */
export function FlashText({ id, text, children }: { id: string; text: string; children: ReactNode }) {
  const [, flashes] = useChange(id, text, false);
  return (
    <span key={flashes} className={flashes ? "flash" : undefined}>
      {children}
    </span>
  );
}
