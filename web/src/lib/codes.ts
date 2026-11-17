// Mirror of backend/codes.py, for screens that check a scan without a server round trip.
const PREFIX: Record<string, 'box' | 'location' | 'unit'> = { SB: 'box', SL: 'location', SU: 'unit' };

function luhn(digits: string): number {
  let total = 0;
  [...digits].reverse().forEach((ch, i) => {
    let d = Number(ch);
    if (i % 2 === 0) { d *= 2; if (d > 9) d -= 9; }
    total += d;
  });
  return (10 - (total % 10)) % 10;
}

export function parseCode(raw: string): { kind: 'box' | 'location' | 'unit'; id: number } | { kind: 'product' | 'bad_check'; id?: undefined } {
  const m = raw.trim().toUpperCase().match(/^(SB|SL|SU)-?(\d{7})-?(\d)$/);
  if (!m) return { kind: 'product' };
  if (luhn(m[2]!) !== Number(m[3])) return { kind: 'bad_check' };
  return { kind: PREFIX[m[1]!]!, id: Number(m[2]) };
}
