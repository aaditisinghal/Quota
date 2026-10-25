import { useEffect, useState } from "react";
import { FlashMoney, FlashText } from "./Flash";
import { api, type Health, type Principal, type Today } from "./api";
import { Money, Rich, TraceLink, When, personName, pt, usd } from "./format";

// Balances at the previous clock the health screen saw, so a change (an injected churn) shows what moved.
let seen: { clock: string; balances: Record<string, number> } | null = null;
let baseline: { clock: string; balances: Record<string, number> } | null = null;

export function TodayHealth({ version, people }: { version: number; people: Principal[] }) {
  const [today, setToday] = useState<Today | null>(null);
  const [health, setHealth] = useState<Health | null>(null);

  useEffect(() => {
    api.today().then(setToday).catch(() => setToday(null));
    api
      .health()
      .then((h) => {
        const balances = Object.fromEntries(h.trial_balance.accounts.map((a) => [a.account, a.balance_cents]));
        if (seen && seen.clock !== h.clock) baseline = seen;
        seen = { clock: h.clock, balances };
        setHealth(h);
      })
      .catch(() => setHealth(null));
  }, [version]);

  return (
    <section className="today-health">
      {today && <TodayPanel today={today} people={people} />}
      {health && <HealthPanel health={health} />}
    </section>
  );
}

function TodayPanel({ today, people }: { today: Today; people: Principal[] }) {
  const waitingTotal = today.waiting.reduce((s, w) => s + w.total_cents, 0);
  return (
    <div className="panel">
      <h2>Today, {today.day}</h2>
      <div className="grid3">
        <div className={`col ${today.waiting.length ? "human" : ""}`}>
          <h3>
            Waiting on people <span className="total">{usd(waitingTotal)}</span>
          </h3>
          {today.waiting.length === 0 && <p className="muted">Nobody. Nothing is waiting for a decision.</p>}
          {today.waiting.map((w) => (
            <div key={w.approver} className="who-block">
              <div className="who-name">
                {w.name}, {w.title} <span className="total">{usd(w.total_cents)}</span>
              </div>
              <ul>
                {w.items.map((i) => (
                  <li key={i.id}>
                    <TraceLink id={i.id} /> {i.kind.replace("_", " ")} <TraceLink id={i.subject} />{" "}
                    <Money cents={i.amount_cents} trace={i.id} /> <span className="muted">since <When ts={i.since} /></span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
        <div className={`col ${today.held.length ? "human" : ""}`}>
          <h3>
            Held <span className="total">{usd(today.held_total_cents)}</span>
          </h3>
          {today.held.length === 0 && <p className="muted">Nothing is held.</p>}
          <ul>
            {today.held.map((h) => (
              <li key={h.decision}>
                <TraceLink id={h.decision} /> {h.kind.replace("_", " ")} {h.subject}{" "}
                <Money cents={h.amount_cents} trace={h.lines[0] ?? h.decision} />
                <div className="muted">
                  <Rich text={h.reason} trace={h.decision} />
                </div>
              </li>
            ))}
          </ul>
        </div>
        <div className="col">
          <h3>Done today</h3>
          {today.done.length === 0 && today.posted.length === 0 && <p className="muted">Nothing yet today.</p>}
          <ul>
            {today.done.map((d) => (
              <li key={d.id}>
                <TraceLink id={d.id} /> {d.kind.replace("_", " ")} <span className="tag">{d.outcome}</span>{" "}
                <span className="muted">{personName(d.seat, people)}</span>
              </li>
            ))}
            {today.posted.map((e) => (
              <li key={e.id}>
                <TraceLink id={e.id} /> posted <Money cents={e.amount_cents} trace={e.id} />{" "}
                <span className="muted">{e.memo}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}

function HealthPanel({ health }: { health: Health }) {
  const tb = health.trial_balance;
  const before = baseline?.balances ?? null;
  return (
    <div className="panel">
      <h2>Ledger health</h2>
      {health.cascades.map((c) => (
        <div key={c.deal} className="cascade">
          <h3>
            One event, eight numbers: {c.deal} churned <span className="muted">every check below stays green</span>
          </h3>
          <ol>
            {c.items.map((item) => (
              <li key={item.label}>
                <span className="cascade-label">{item.label}</span>
                <span>
                  <FlashText id={`cascade:${c.deal}:${item.label}`} text={item.text}>
                    <Rich text={item.text} trace={item.trace} />
                  </FlashText>
                </span>
              </li>
            ))}
          </ol>
        </div>
      ))}
      <div className="checks">
        {health.invariants.map((i) => (
          <div key={i.name} className={`check ${i.ok ? "ok" : "bad"}`} title={i.violations.map((v) => `${v.subject}: ${v.detail}`).join("\n")}>
            <span className="mark">{i.ok ? "✓" : "✗"}</span>
            <span>{i.name}</span>
          </div>
        ))}
        <div className={`check ${health.chain.ok ? "ok" : "bad"}`}>
          <span className="mark">{health.chain.ok ? "✓" : "✗"}</span>
          <span>
            chain {health.chain.rows} rows{health.chain.first_bad_seq !== null && `, breaks at ${health.chain.first_bad_seq}`}
          </span>
        </div>
      </div>
      <p className="muted">
        INV1 balanced · INV2 locked periods untouched · INV3 every subledger ties to its GL account · INV4 every entry
        decided · INV5 approvals by the right person within limit · INV6 payroll equals approved lines · INV7 splits add
        to 100% · INV8 hash chain intact
      </p>

      <div className="grid2">
        <div>
          <h3>Subledger tie-outs</h3>
          <table className="table">
            <thead>
              <tr>
                <th>Account</th>
                <th className="num">GL</th>
                <th className="num">Subledger</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {health.tie_outs.map((t) => (
                <tr key={t.name}>
                  <td>
                    {t.name}
                    <div className="muted">{t.subledger}</div>
                  </td>
                  <td className="num">
                    <FlashMoney id={`tie:${t.name}:gl`} cents={t.gl_cents} trace={`account/${t.name.match(/\((\d{4})\)/)?.[1] ?? ""}`} />
                  </td>
                  <td className="num">
                    <FlashMoney id={`tie:${t.name}:sub`} cents={t.subledger_cents} />
                  </td>
                  <td className={t.ok ? "ok" : "bad"}>{t.ok ? "✓ ties" : "✗"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>Periods</h3>
          <div className="periods">
            {health.periods.map((p) => (
              <span key={p.period} className={`period ${p.status}`} title={`${p.status} since ${pt(p.since)} by ${p.by}`}>
                {p.period} {p.status === "locked" ? "🔒" : ""}
              </span>
            ))}
          </div>
        </div>
        <div>
          <h3>
            Trial balance <span className="muted">as of <When ts={health.clock} /></span>
          </h3>
          <table className="table">
            <thead>
              <tr>
                <th>Account</th>
                <th className="num">Debit</th>
                <th className="num">Credit</th>
                <th className="num" title={before ? `since ${pt(baseline!.clock)}` : ""}>
                  {before ? `Moved since ${pt(baseline!.clock).split(",")[0]}` : "Moved"}
                </th>
              </tr>
            </thead>
            <tbody>
              {tb.accounts.map((a) => {
                const delta = before ? a.balance_cents - (before[a.account] ?? 0) : 0;
                return (
                  <tr key={a.account} className={delta ? "changed" : ""}>
                    <td>
                      <span className="mono">{a.account}</span> {a.name}
                    </td>
                    <td className="num">
                      <FlashMoney id={`tb:${a.account}:dr`} cents={Math.max(a.balance_cents, 0)} trace={`account/${a.account}`} blankZero />
                    </td>
                    <td className="num">
                      <FlashMoney id={`tb:${a.account}:cr`} cents={Math.max(-a.balance_cents, 0)} trace={`account/${a.account}`} blankZero />
                    </td>
                    <td className="num delta">{delta ? `${delta > 0 ? "Dr" : "Cr"} ${usd(Math.abs(delta))}` : ""}</td>
                  </tr>
                );
              })}
            </tbody>
            <tfoot>
              <tr>
                <td>Total</td>
                <td className="num">
                  <FlashMoney id="tb:debits" cents={tb.debits_cents} />
                </td>
                <td className="num">
                  <FlashMoney id="tb:credits" cents={tb.credits_cents} />
                </td>
                <td className={tb.debits_cents === tb.credits_cents ? "ok" : "bad"}>
                  {tb.debits_cents === tb.credits_cents ? "✓ balanced" : "✗"}
                </td>
              </tr>
            </tfoot>
          </table>
        </div>
      </div>
    </div>
  );
}
