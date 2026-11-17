import { APP } from '@/lib/appConfig';

const ET = APP.tz;
const TZ = APP.tzLabel;

export function money(cents: number | null | undefined, empty = '—'): string {
  if (cents === null || cents === undefined) return empty;
  return (cents / 100).toLocaleString('en-US', { style: 'currency', currency: 'USD' });
}

export function centsFromInput(v: string): number | null {
  const n = Number(v.replace(/[$,\s]/g, ''));
  if (!v.trim() || !Number.isFinite(n) || n < 0) return null;
  return Math.round(n * 100);
}

export function dollarsForInput(cents: number | null | undefined): string {
  return cents === null || cents === undefined ? '' : (cents / 100).toFixed(2);
}

function parseUtc(ts: string): Date {
  return new Date(ts.endsWith('Z') ? ts : ts + 'Z');
}

/** "3:42 PM ET", "Yesterday 9:10 AM ET", "Oct 28, 9:10 AM ET" */
export function when(ts: string | null | undefined): string {
  if (!ts) return '—';
  const d = parseUtc(ts);
  const day = (x: Date) => x.toLocaleDateString('en-US', { timeZone: ET });
  const time = d.toLocaleTimeString('en-US', { timeZone: ET, hour: 'numeric', minute: '2-digit' });
  const now = new Date();
  if (day(d) === day(now)) return `${time} ${TZ}`;
  if (day(d) === day(new Date(now.getTime() - 86400000))) return `Yesterday ${time} ${TZ}`;
  const date = d.toLocaleDateString('en-US', { timeZone: ET, month: 'short', day: 'numeric' });
  return `${date}, ${time} ${TZ}`;
}

/** A plain date from the database ("2026-03-30") as "Mar 30, 2026". */
export function day(iso: string | null | undefined): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso ?? '');
  if (!m) return iso || 'no date';
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

export function ago(ts: string | null | undefined): string {
  if (!ts) return 'never';
  const s = (Date.now() - parseUtc(ts).getTime()) / 1000;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export function prettyCode(code: string): string {
  return code.length === 10 ? `${code.slice(0, 2)}-${code.slice(2, 9)}-${code.slice(9)}` : code;
}

/** Short form people say out loud: SB00001234 -> "Box 123". */
export function shortName(code: string): string {
  const n = Number(code.slice(2, 9));
  const kind = code.startsWith('SB') ? 'Box' : code.startsWith('SU') ? 'Unit' : 'Loc';
  return `${kind} ${n}`;
}

export function plural(n: number, one: string, many = one + 's'): string {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}

/** Counts of stock say units; counts of different products say items (flow audit 11-12, finding 3). */
export const units = (n: number) => plural(n, 'unit');
export const kinds = (n: number) => plural(n, 'item');

/** One wording for Arriving's counts on every screen: "12 packages on 10 orders", or "3 orders" when each order is one
 *  package (inbound.counts on the server). */
export function packagesText(c: { packages: number; orders: number } | undefined): string {
  if (!c) return '0 orders';
  return c.packages === c.orders ? plural(c.orders, 'order') : `${plural(c.packages, 'package')} on ${plural(c.orders, 'order')}`;
}
