// Quota's company (CUA_TASKS C1): when QUOTA_ROSTER names a roster file, the generated Sales department gives way
// to Quota's cast. The generated Sales people move to Customer Success (other tables reference them), and the
// roster's people are added in Sales, Finance, Sales Operations and Partners with the rows HR keeps for them:
// job info, employment status, compensation, commission and bonus. Pure and deterministic: same roster, same rows.
import { readFileSync } from 'node:fs';
import { DEFAULT_NOW } from '../platform/clock.mjs';

const DEPT = { Sales: 18249, Finance: 18259, 'Sales Operations': 18901, Partners: 18902, 'Customer Success': 18253 };
const STATUS = { 'Full-Time': 18158, Contractor: 18160, Terminated: 18161 };
const NORTH_AMERICA = 18233, REMOTE = 18352;
const JERRY_USER = 2385;

export function loadRoster(path = process.env.QUOTA_ROSTER) {
  return path ? JSON.parse(readFileSync(path, 'utf8')) : null;
}

export function applyQuotaRoster(T, roster) {
  if (!roster) return T;
  // departments Quota needs that the generated company lacks
  const have = new Set(T.ListValue.filter((l) => l.list === 'department').map((l) => l.label));
  let sort = Math.max(...T.ListValue.filter((l) => l.list === 'department').map((l) => l.sort)) + 1;
  for (const label of roster.departments) {
    if (!have.has(label)) T.ListValue.push({ id: DEPT[label], list: 'department', label, sort: sort++, archived: false, meta: null });
  }
  // the generated Sales department moves aside, intact
  for (const e of T.Employee) if (e.departmentId === DEPT.Sales) e.departmentId = DEPT['Customer Success'];
  for (const r of T.EmployeeTableRow) if (r.table === 'jobInfo' && r.data?.department === 'Sales') r.data = { ...r.data, department: 'Customer Success' };

  const template = Object.fromEntries(Object.keys(T.Employee[0]).map((k) => [k, null]));
  let nextId = Math.max(...T.Employee.map((e) => e.id)) + 1;
  let nextNumber = Math.max(...T.Employee.map((e) => Number(e.employeeNumber) || 0)) + 1;
  const people = [...roster.people].sort((a, b) => a.key.localeCompare(b.key));
  const ids = Object.fromEntries(people.map((p) => [p.key, nextId++]));
  const byKey = Object.fromEntries(people.map((p) => [p.key, p]));
  const row = (employeeId, table, effectiveDate, data) => T.EmployeeTableRow.push({ employeeId, table, effectiveDate, data, createdBy: JERRY_USER, createdAt: DEFAULT_NOW });

  for (const p of people) {
    const id = ids[p.key];
    const boss = p.reportsTo ? byKey[p.reportsTo] : null;
    const last = p.employmentStatus[p.employmentStatus.length - 1];
    T.Employee.push({
      ...template, id, employeeNumber: String(nextNumber++), firstName: p.firstName, lastName: p.lastName, preferredName: p.firstName,
      workEmail: p.workEmail, state: p.state, country: 'United States', hireDate: p.hireDate,
      status: p.terminationDate ? 'inactive' : 'active', terminationDate: p.terminationDate,
      terminationReasonId: p.terminationDate ? 4400 : null, jobTitle: p.jobTitle, departmentId: DEPT[p.department],
      divisionId: NORTH_AMERICA, locationId: REMOTE, employmentStatusId: STATUS[last.status], reportsToId: boss ? ids[boss.key] : null,
      payType: p.payType, timezone: 'America/Los_Angeles', hasPhoto: false, customFields: { quotaKey: p.key },
    });
    row(id, 'jobInfo', p.hireDate, { jobTitle: p.jobTitle, reportsTo: boss ? `${boss.lastName}, ${boss.firstName}` : null, reportsToId: boss ? ids[boss.key] : null, department: p.department, division: 'North America', location: 'Remote Worker' });
    for (const s of p.employmentStatus) row(id, 'employmentStatus', s.effectiveDate, { status: s.status, comment: s.comment, fte: 1 });
    for (const c of p.compensation) row(id, 'compensation', c.effectiveDate, { payRate: c.payRate, payType: c.payType, payPer: c.payPer, paySchedule: c.paySchedule, changeReason: 'Hire', currency: 'USD', comment: null, overtimeStatus: 'Exempt', overtimeRate: null });
    for (const c of p.commission) row(id, 'commission', c.date, { amount: c.amount_cents / 100, currency: 'USD', comment: c.comment });
    for (const b of p.bonus) row(id, 'bonus', b.date, { amount: b.amount_cents / 100, currency: 'USD', reason: b.reason, comment: b.comment });
  }
  return T;
}
