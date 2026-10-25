import { test, expect } from 'bun:test';
import { createHash } from 'node:crypto';
import { generateWorld } from '../src/seed/generate.mjs';
import { applyQuotaRoster } from '../src/seed/quota-roster.mjs';

const h = (x) => createHash('sha256').update(JSON.stringify(x)).digest('hex');
const ROSTER = {
  departments: ['Sales', 'Finance', 'Sales Operations', 'Partners'],
  people: [
    { key: 'jordan.kim', firstName: 'Jordan', lastName: 'Kim', workEmail: 'jordan.kim@fernhill.example', jobTitle: 'Account Executive', department: 'Sales', reportsTo: 'morgan.diaz', hireDate: '2024-03-01', state: 'CA', payType: 'Salary', terminationDate: null, employmentStatus: [{ effectiveDate: '2024-03-01', status: 'Full-Time', comment: 'Hire' }], compensation: [{ effectiveDate: '2024-03-01', payRate: 98000, payType: 'Salary', payPer: 'Year', paySchedule: 'Twice a month' }], commission: [{ date: '2026-04-15', amount_cents: 1160000, comment: 'onyx-motors 2026Q1' }], bonus: [] },
    { key: 'morgan.diaz', firstName: 'Morgan', lastName: 'Diaz', workEmail: 'morgan.diaz@fernhill.example', jobTitle: 'Sales Manager', department: 'Sales', reportsTo: null, hireDate: '2022-09-12', state: 'CA', payType: 'Salary', terminationDate: null, employmentStatus: [{ effectiveDate: '2022-09-12', status: 'Full-Time', comment: 'Hire' }], compensation: [], commission: [], bonus: [] },
    { key: 'ana.ruiz', firstName: 'Ana', lastName: 'Ruiz (agent)', workEmail: 'ana.ruiz@agents.example', jobTitle: 'Independent Sales Agent', department: 'Partners', reportsTo: null, hireDate: '2025-07-01', state: 'CA', payType: 'Contract', terminationDate: null, employmentStatus: [{ effectiveDate: '2025-07-01', status: 'Contractor', comment: 'Partner agreement' }], compensation: [], commission: [], bonus: [] },
  ],
};
const world = () => applyQuotaRoster(generateWorld({ seed: 42, size: 100 }), structuredClone(ROSTER));

test('the Quota roster is deterministic: same roster, same world', () => {
  expect(h(world())).toBe(h(world()));
});
test('the roster replaces the Sales department and keeps the generated people elsewhere', () => {
  const T = world();
  const sales = T.Employee.filter((e) => e.departmentId === 18249);
  expect(sales.map((e) => `${e.firstName} ${e.lastName}`).sort()).toEqual(['Jordan Kim', 'Morgan Diaz']);
  expect(T.Employee.length).toBe(103);                                   // nobody generated was removed
  const jordan = sales.find((e) => e.firstName === 'Jordan');
  expect(jordan.reportsToId).toBe(sales.find((e) => e.firstName === 'Morgan').id);
  const rows = T.EmployeeTableRow.filter((r) => r.employeeId === jordan.id);
  expect(rows.find((r) => r.table === 'commission').data).toEqual({ amount: 11600, currency: 'USD', comment: 'onyx-motors 2026Q1' });
  expect(rows.find((r) => r.table === 'compensation').data.payRate).toBe(98000);
  expect(T.ListValue.some((l) => l.list === 'department' && l.label === 'Partners')).toBe(true);
  const ana = T.Employee.find((e) => e.lastName === 'Ruiz (agent)');
  expect([ana.departmentId, ana.employmentStatusId]).toEqual([18902, 18160]);
});
test('without a roster nothing changes', () => {
  expect(h(applyQuotaRoster(generateWorld({ seed: 42, size: 100 }), null))).toBe(h(generateWorld({ seed: 42, size: 100 })));
});
