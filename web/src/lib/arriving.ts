// Which Arriving section a card goes in. Plain TypeScript with no imports, so tests/test_arriving_bucket.py runs it
// with node.
export interface Dated { state: string; expected_from: string | null; expected_to: string | null }

const daysBetween = (a: string, b: string) => Math.round((Date.parse(b) - Date.parse(a)) / 86400000);

export function bucket(o: Dated, today: string): string {
  if (o.state === 'delivered') return 'delivered';
  if (!o.expected_to) return 'nodate';
  if (o.expected_to < today) return 'late';
  // A window that has started (Nov 12 - Nov 24) is "today" only once Amazon has shipped it (sweep C5: unshipped
  // orders sat under Arriving today for 13 days).
  if (o.expected_to === today || (o.state === 'shipped' && o.expected_from && o.expected_from <= today)) return 'today';
  if (daysBetween(today, o.expected_to) <= 7) return 'week';
  return 'later';
}
