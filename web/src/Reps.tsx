import { useEffect } from "react";
import { api, type Band, type ContractCard, type ContractChild, type OpenDeal, type Principal, type Recipient, type RepDetail } from "./api";
import { CalibrationDrawer } from "./Calibration";
import { Money, When, personName, pt, usd } from "./format";
import { ConfidenceBadge, Simulated, Unavailable, pct, useApi } from "./ui";

// D3 + D4: every rep at a glance, in the server's needs-attention order, then one rep's open pipeline and
// booked contracts, with everyone else paid on each contract one click away.

const band = (status: string): Band => (status.toLowerCase() === "held" ? "held" : status.toLowerCase() === "note" ? "note" : "cleared");

export function RepsBoard({ version }: { version: number }) {
  const reps = useApi(() => api.reps(), [version]);
  if (reps.missing) return <Unavailable what="The reps board" path="/api/reps" />;
  if (!reps.data) return <p className="muted">{reps.error ?? "Loading reps…"}</p>;
  const stages = Object.keys(reps.data[0]?.pipeline.by_stage ?? {});
  return (
    <section className="reps">
      <div className="panel">
        <h2>
          Reps <span className="muted">needing attention first · open pipeline is <Simulated /></span>
        </h2>
        <div className="table-wrap">
          <table className="table reps-table">
            <thead>
              <tr>
                <th>Rep</th>
                <th>Open pipeline</th>
                <th className="num">Booked {reps.data[0]?.quarter ?? "this quarter"}</th>
                <th className="num">Attainment</th>
                <th className="num">Pending commission</th>
                <th>Lowest confidence</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {reps.data.map((r) => (
                <tr key={r.rep} className={band(r.status) === "held" ? "human-row" : ""}>
                  <td>
                    <a href={`#rep/${encodeURIComponent(r.rep)}`}>
                      <strong>{r.name}</strong>
                    </a>
                    {r.left && <div className="muted">left {pt(r.left, true)}</div>}
                  </td>
                  <td>
                    <span className="money plain">{usd(r.pipeline.total_cents)}</span>{" "}
                    <span className="muted">{r.pipeline.deals} deals</span>
                    <div className="stage-mini muted">
                      {stages.filter((s) => r.pipeline.by_stage[s]).map((s) => (
                        <span key={s}>
                          {s} {usd(r.pipeline.by_stage[s])}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="num">
                    <a className="money" href={`#rep/${encodeURIComponent(r.rep)}`} title="The contracts behind it">
                      {usd(r.booked_quarter_cents)}
                    </a>
                  </td>
                  <td className="num">{pct(r.attainment_bps)}</td>
                  <td className="num">
                    <a className="money" href={`#rep/${encodeURIComponent(r.rep)}`} title="The lines behind it">
                      {usd(r.pending_cents)}
                    </a>
                  </td>
                  <td>
                    <ConfidenceBadge score={r.lowest_confidence} band={band(r.status)} />
                  </td>
                  <td>
                    <span className={`status status-${band(r.status)}`}>{r.status}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

type DetailProps = { rep: string; contract: string | null; calibrate: boolean; people: Principal[]; version: number; onChange: () => void };

export function RepView({ rep, contract, calibrate, people, version, onChange }: DetailProps) {
  const detail = useApi<RepDetail>(() => api.rep(rep), [rep, version]);
  useEffect(() => {
    if (!contract || !detail.data) return;
    document.getElementById(`contract-${contract}`)?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [contract, detail.data]);

  if (detail.missing) return <Unavailable what="Rep detail" path={`/api/reps/${rep}`} />;
  if (!detail.data) return <p className="muted">{detail.error ?? `Loading ${rep}…`}</p>;
  const d = detail.data;
  const open = contract ? d.contracts.find((c) => c.id === contract) : undefined;
  const mine = open ? viewerRecipient(open, rep) : undefined;

  return (
    <section className="rep">
      <div className="case-head">
        <a href="#reps" className="muted">
          ← Reps
        </a>
        <h2>{d.name ?? personName(rep, people)}</h2>
        <span className="muted">
          {[d.title, d.state, d.manager && `manager ${personName(d.manager, people)}`].filter(Boolean).join(" · ")}
          {d.left && ` · left ${pt(d.left, true)}`}
        </span>
      </div>

      <div className="panel">
        <h3>
          Open pipeline <Simulated>forecast commission at the plan rate, not earned</Simulated>
        </h3>
        <div className="lanes">
          {d.pipeline.stages.map((stage) => {
            const deals = d.pipeline.deals.filter((p) => p.stage === stage);
            return (
              <div key={stage} className="lane">
                <div className="lane-head">
                  {stage} <span className="muted">{deals.length}</span>
                </div>
                {deals.map((p) => (
                  <PipelineCard key={p.id} deal={p} />
                ))}
              </div>
            );
          })}
        </div>
      </div>

      <h3>Contracts</h3>
      <div className="contracts">
        {d.contracts.map((c) => (
          <div key={c.id}>
            <ContractView c={c} rep={rep} people={people} focused={contract === c.id} />
            {c.children.map((child) => (
              <div key={child.id} className="child">
                <ChildView child={child} parent={c} />
              </div>
            ))}
          </div>
        ))}
        {d.contracts.length === 0 && <p className="muted">No booked contracts.</p>}
      </div>
      {calibrate && open && mine?.line && (
        <CalibrationDrawer line={mine.line} title={`${open.customer} · ${personName(rep, people)}`} back={`rep/${rep}/${open.id}`}
                           people={people} version={version} onChange={onChange} rep={rep} customer={open.customer} />
      )}
    </section>
  );
}

/** The recipient this view is for: the rep whose page it is, else the first rep on the contract. */
function viewerRecipient(c: ContractCard, rep: string): Recipient | undefined {
  return c.recipients.find((r) => r.who === rep && r.line) ?? c.recipients.find((r) => r.kind === "rep" && r.line);
}

function PipelineCard({ deal }: { deal: OpenDeal }) {
  return (
    <div className="card pipeline-card">
      <strong>{deal.account}</strong>
      <div className="money plain">{usd(deal.amount_cents)}</div>
      <div className="muted">
        forecast {usd(deal.forecast_commission_cents)} · not earned
        {deal.expected_close && <> · close {pt(deal.expected_close, true)}</>}
      </div>
    </div>
  );
}

function clauseList(clauses: ContractCard["clauses"]): string[] {
  if (!clauses) return [];
  return Object.entries(clauses)
    .filter(([k]) => k !== "billing")
    .map(([k, v]) => {
      const name = k.replace(/_/g, " ");
      if (v && typeof v === "object") {
        const o = v as Record<string, unknown>;
        return [o.window_days ? `${o.window_days}-day ${name}` : name, o.refund ? `${String(o.refund).replace(/_/g, " ")} refund` : ""]
          .filter(Boolean)
          .join(", ");
      }
      return typeof v === "string" ? `${name}: ${v.replace(/_/g, " ")}` : name;
    });
}

function ContractView({ c, rep, people, focused }: { c: ContractCard; rep: string; people: Principal[]; focused: boolean }) {
  const self = `#rep/${encodeURIComponent(rep)}/${encodeURIComponent(c.id)}`;
  const mine = viewerRecipient(c, rep);
  const myLine = c.lines.find((l) => l.id === mine?.line);
  const clauses = clauseList(c.clauses);
  return (
    <div id={`contract-${c.id}`} className={`card contract ${focused ? "focused" : ""}`}>
      <div className="card-head">
        <strong className="contract-name">{c.customer}</strong>
        <a className="ref" href={`#trace/deal/${encodeURIComponent(c.id)}`}>
          {c.id}
        </a>
        {mine?.confidence && <ConfidenceBadge score={mine.confidence.score} band={mine.confidence.band} href={`${self}/calibration`} />}
      </div>
      <dl>
        <div className="field">
          <dt>ACV</dt>
          <dd>
            <Money cents={c.acv_cents} trace={`deal/${c.id}`} /> · {c.term_months} months
            {c.billing && <> · {c.billing.replace(/_/g, " ")}</>}
            {c.ramp && <> · ramp {c.ramp}</>}
          </dd>
        </div>
        {c.countersigned_at && (
          <div className="field">
            <dt>Countersigned</dt>
            <dd>
              <When ts={c.countersigned_at} />
            </dd>
          </div>
        )}
        {clauses.length > 0 && (
          <div className="field">
            <dt>Clauses</dt>
            <dd>{clauses.join(" · ")}</dd>
          </div>
        )}
        <div className="field">
          <dt>Collection</dt>
          <dd className={c.overdue ? "bad" : ""}>{c.overdue ? "overdue" : c.paid ? "paid" : "not paid yet (pay on collection)"}</dd>
        </div>
        {c.lines.map((l) => (
          <div key={l.id} className="field">
            <dt>{l.rep === rep ? "Commission" : personName(l.rep, people)}</dt>
            <dd>
              <Money cents={l.amount_cents} trace={l.id} />{" "}
              <a className="ref" href={`#trace/${encodeURIComponent(l.id)}`}>
                {l.id}
              </a>
              <span className="muted">
                {" "}
                · {l.quarter} · {l.status.replace(/_/g, " ")}
              </span>
            </dd>
          </div>
        ))}
      </dl>
      {c.split_bar.length > 1 && <SplitBar c={c} people={people} />}
      {c.recipients.length > 0 && (
        <div className="chips connected">
          {c.recipients.map((r) => (
            <PersonChip key={`${r.kind}:${r.who}`} r={r} contract={c.id} rep={rep} people={people} />
          ))}
        </div>
      )}
      {myLine && (
        <a className="calibrate-link" href={`${self}/calibration`}>
          How Quota settled this →
        </a>
      )}
    </div>
  );
}

/** An amendment under its parent: only the incremental commission it adds (E12). */
function ChildView({ child, parent }: { child: ContractChild; parent: ContractCard }) {
  const lines = child.incremental_lines.map((l) => (typeof l === "string" ? l : l.id));
  return (
    <div className="card contract expansion">
      <div className="card-head">
        <span className="tag">expansion v{child.version}</span>
        <strong>{parent.customer}</strong>
        <span className="muted">
          signed <When ts={child.signed_at} />
        </span>
      </div>
      <dl>
        <div className="field">
          <dt>ACV</dt>
          <dd>
            <Money cents={child.acv_cents} trace={`deal/${parent.id}`} />
          </dd>
        </div>
        <div className="field">
          <dt>Incremental commission</dt>
          <dd>
            <Money cents={child.incremental_commission_cents} trace={lines[0] ?? `deal/${parent.id}`} />{" "}
            {lines.map((id) => (
              <a key={id} className="ref" href={`#trace/${encodeURIComponent(id)}`}>
                {id}
              </a>
            ))}
          </dd>
        </div>
      </dl>
    </div>
  );
}

const KIND: Record<string, string> = {
  rep: "rep",
  manager_override: "manager override",
  se_pool: "SE pool",
  partner: "partner",
  account_manager: "account manager",
};

/** Someone else paid on this contract: their share, and their own view of the same contract. */
function PersonChip({ r, contract, rep, people }: { r: Recipient; contract: string; rep: string; people: Principal[] }) {
  const share = r.kind === "manager_override" && r.rate_bps !== undefined ? `${pct(r.rate_bps)} override` : pct(r.share_bps);
  const href =
    r.kind === "rep" || r.kind === "account_manager"
      ? `#rep/${encodeURIComponent(r.who)}/${encodeURIComponent(contract)}`
      : r.line
        ? `#trace/${encodeURIComponent(r.line)}`
        : r.transfer
          ? `#trace/${encodeURIComponent(r.transfer)}`
          : null;
  const body = (
    <>
      <strong>{r.name ?? personName(r.who, people)}</strong> <span className="muted">{KIND[r.kind] ?? r.kind.replace(/_/g, " ")}</span>{" "}
      <span className="num">{share}</span>
      {r.paid_via && r.paid_via !== "payroll" && <span className="muted"> · {r.paid_via.replace(/_/g, " ")}</span>}
      {r.confidence && <ConfidenceBadge score={r.confidence.score} band={r.confidence.band} />}
    </>
  );
  const here = r.who === rep;
  return href && !here ? (
    <a className="chip person-chip" href={href} title={r.note ?? undefined}>
      {body}
    </a>
  ) : (
    <span className={`chip person-chip ${here ? "on" : ""}`} title={r.note ?? undefined}>
      {body}
    </span>
  );
}

/** One 100% bar for a contract with several recipients; each segment opens its payroll line. */
function SplitBar({ c, people }: { c: ContractCard; people: Principal[] }) {
  return (
    <div className="split-bar" role="list" aria-label="Who is paid on this contract">
      {c.split_bar.map((s, i) => {
        const label = `${personName(s.who, people)} ${pct(s.share_bps)}`;
        return (
          <a key={s.who} role="listitem" className={`segment seg-${i % 6}`} style={{ flexGrow: s.share_bps }}
             href={s.line ? `#trace/${encodeURIComponent(s.line)}` : undefined} title={label}>
            <span>{label}</span>
          </a>
        );
      })}
    </div>
  );
}
