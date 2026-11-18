import JsBarcode from 'jsbarcode';
import QRCode from 'qrcode';
import { prettyCode } from '@/lib/format';
import { post } from '@/api/client';
import { buildingOf, isShelf, placeText } from '@/lib/places';

/*
 * Labels print from the browser at the exact label size (@page), one label per page.
 * Barcodes are Code 128 (every scanner reads it, and FNSKU labels use it). Box and location
 * labels on 4x6 also carry a QR code, which phone cameras read faster.
 * Set the printer's paper size to match once, and turn off "fit to page".
 */

export type LabelSize = '4x6' | '2x1' | '2.25x1.25';
export const LABEL_SIZES: { value: LabelSize; label: string }[] = [
  { value: '4x6', label: '4 x 6 in (shipping label)' },
  { value: '2x1', label: '2 x 1 in' },
  { value: '2.25x1.25', label: '2.25 x 1.25 in' },
];

export interface Label {
  code: string; // barcode content
  big: string; // large text: box number, location code
  lines?: string[]; // smaller text under it
  kind: 'box' | 'location' | 'unit' | 'fnsku';
}

export function getLabelSize(kind: Label['kind']): LabelSize {
  try {
    const v = localStorage.getItem(`boxloft-label-${kind}`);
    if (v === '4x6' || v === '2x1' || v === '2.25x1.25') return v;
  } catch {
    // ignore
  }
  return kind === 'box' ? '4x6' : '2x1';
}

export function setLabelSize(kind: Label['kind'], size: LabelSize) {
  try {
    localStorage.setItem(`boxloft-label-${kind}`, size);
  } catch {
    // ignore
  }
}

function barcodeSvg(value: string, height: number, width: number): string {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  JsBarcode(svg, value, { format: 'CODE128', displayValue: false, margin: 0, height, width });
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.style.width = '100%';
  svg.style.height = '100%';
  return svg.outerHTML;
}

const esc = (s: string) => s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]!);

async function labelHtml(l: Label, size: LabelSize): Promise<string> {
  const [w, h] = size.split('x').map(Number) as [number, number];
  const human = l.kind === 'fnsku' ? l.code : prettyCode(l.code);
  if (size === '4x6') {
    const qr = await QRCode.toString(l.code, { type: 'svg', margin: 0, errorCorrectionLevel: 'M' });
    return `<div class="label-page" style="width:${w}in;height:${h}in;padding:0.25in;display:flex;flex-direction:column;gap:0.12in">
      <div style="font-size:14pt;font-weight:600;letter-spacing:0.08em;text-transform:uppercase">Boxloft · ${esc(l.kind)}</div>
      <div style="font-size:${Math.min(64, (3.4 * 72) / Math.max(2, l.big.length * 0.62)).toFixed(0)}pt;font-weight:700;line-height:1;white-space:nowrap">${esc(l.big)}</div>
      ${(l.lines ?? []).map((x) => `<div style="font-size:14pt;line-height:1.2;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">${esc(x)}</div>`).join('')}
      <div style="flex:1"></div>
      <div style="display:flex;gap:0.2in;align-items:flex-end">
        <div style="width:1.6in;height:1.6in">${qr.replace('<svg', '<svg style="width:100%;height:100%"')}</div>
        <div style="flex:1;font-size:12pt">${esc(human)}</div>
      </div>
      <div style="height:1.1in">${barcodeSvg(l.code, 100, 3)}</div>
    </div>`;
  }
  if (l.kind === 'location' || l.kind === 'box') {
    // Shelf and box labels on small stock: the name (S2, A31) as big as it fits (read from across the room), the barcode under it.
    const big = Math.min(h * 0.42, (w * 0.9) / Math.max(2, l.big.length * 0.62)) * 72;
    return `<div class="label-page" style="width:${w}in;height:${h}in;padding:0.05in 0.1in;display:flex;flex-direction:column;align-items:center">
      <div style="font-size:${big.toFixed(0)}pt;font-weight:800;line-height:1;white-space:nowrap">${esc(l.big)}</div>
      <div style="flex:1;width:100%;margin-top:0.03in">${barcodeSvg(l.code, 50, 2)}</div>
      <div style="font-size:6pt;line-height:1.1;margin-top:0.02in">${esc(human)}${l.lines?.[0] ? ` · ${esc(l.lines[0])}` : ''}</div>
    </div>`;
  }
  // Small labels: barcode on top, text below. Keep a 0.1 in quiet margin.
  const fs = h <= 1 ? 7 : 8;
  return `<div class="label-page" style="width:${w}in;height:${h}in;padding:0.06in 0.1in;display:flex;flex-direction:column">
    <div style="height:${(h * 0.5).toFixed(2)}in">${barcodeSvg(l.code, 60, 2)}</div>
    <div style="font-size:${fs + 2}pt;font-weight:700;line-height:1.15;margin-top:0.03in;white-space:nowrap;overflow:hidden">${esc(human)}${l.big !== human ? ` · ${esc(l.big)}` : ''}</div>
    ${(l.lines ?? []).slice(0, 2).map((x) => `<div style="font-size:${fs}pt;line-height:1.15;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(x)}</div>`).join('')}
  </div>`;
}

/*
 * Print queue (Alex 11-11): phones have no label printer. On a device that doesn't print, printLabels sends the
 * labels to the server's queue instead, and the computer with the printer prints them (PrintQueueBar in Shell).
 * Default: a touch screen sends to the queue, a computer prints. Settings -> This device can change it.
 */
export function printsHere(): boolean {
  try {
    const v = localStorage.getItem('boxloft-prints');
    if (v === 'yes') return true;
    if (v === 'no') return false;
  } catch {
    // ignore
  }
  return !window.matchMedia('(pointer: coarse)').matches;
}

export function setPrintsHere(v: boolean) {
  try {
    localStorage.setItem('boxloft-prints', v ? 'yes' : 'no');
  } catch {
    // ignore
  }
}

export interface PrintJob {
  id: number; created_at: string; user_id: number; user_name: string; device: string; title: string; labels: Label[]; n: number;
  printed_at: string | null; printed_by_name: string | null; printed_device: string | null; removed_at: string | null;
}

/** Print here, or (on a phone) send to the print queue. The queued case tells the shell to show a toast. */
export async function printLabels(labels: Label[], size?: LabelSize) {
  if (!labels.length) return;
  if (!printsHere()) {
    try {
      const job = await post<PrintJob>('/api/print-jobs', { labels });
      window.dispatchEvent(new CustomEvent('boxloft:queued', { detail: job }));
    } catch (e) {
      window.dispatchEvent(new CustomEvent('boxloft:queue-failed', { detail: (e as Error).message }));
    }
    return;
  }
  await printNow(labels, size);
}

/** Print on this device: one print per label size, in order (the computer's sizes decide). */
export async function printBySize(labels: Label[]) {
  const groups = new Map<LabelSize, Label[]>();
  for (const l of labels) {
    const s = getLabelSize(l.kind);
    groups.set(s, [...(groups.get(s) ?? []), l]);
  }
  for (const [s, ls] of groups) await printNow(ls, s);
}

export async function printNow(labels: Label[], size?: LabelSize) {
  if (!labels.length) return;
  const s = size ?? getLabelSize(labels[0]!.kind);
  const [w, h] = s.split('x');
  const root = document.getElementById('print-root')!;
  const pages = await Promise.all(labels.map((l) => labelHtml(l, s)));
  root.innerHTML = `<style>@page { size: ${w}in ${h}in; margin: 0; }</style>${pages.join('')}`;
  const cleanup = () => {
    root.innerHTML = '';
    window.removeEventListener('afterprint', cleanup);
  };
  window.addEventListener('afterprint', cleanup);
  // Let the browser lay out the SVGs before the dialog opens.
  await new Promise((r) => requestAnimationFrame(() => setTimeout(r, 50)));
  window.print();
}

// Alex 11-11: a label shows the name big (A1), and under it the building it is in (Main warehouse).
export const boxLabel = (b: { scan_code: string; id: number; name?: string; location_code?: string; location_site?: string;
  location_name?: string; kind?: string }): Label => {
  const word = b.kind === 'tote' ? 'TOTE' : b.kind === 'pallet' ? 'PLT' : 'BOX';
  const where = b.location_code ? placeText({ code: b.location_code, site: b.location_site, name: b.location_name }) : '';
  return {
    kind: 'box',
    code: b.scan_code,
    big: b.name ? b.name.toUpperCase() : `${word} ${b.id}`,
    lines: [where].filter(Boolean),
  };
};

// A shelf label: its name big (S2), the building under it. A place that is not a named shelf prints its name
// ("Alex's basement"), never its made-up code (ALEXSBASEM, Fable review 11-17).
export const locationLabel = (l: { scan_code: string; code: string; name?: string; site?: string; kind?: string }): Label => {
  const shelf = isShelf(l);
  return { kind: 'location', code: l.scan_code, big: shelf ? l.code : placeText(l), lines: shelf ? [buildingOf(l)].filter(Boolean) : [] };
};

/** The label of a box or shelf the server just made or found (sorting.target): name big, building under it. */
export const targetLabel = (t: { type: 'box' | 'shelf'; code: string; scan_code: string; where: string }): Label => ({
  kind: t.type === 'box' ? 'box' : 'location',
  code: t.scan_code,
  big: t.code,
  lines: [t.where].filter(Boolean),
});

export const unitLabel = (u: { scan_code: string; sku: string; title: string; condition?: string }): Label => ({
  kind: 'unit',
  code: u.scan_code,
  big: u.sku,
  lines: [u.title, u.condition || ''].filter(Boolean),
});

export const fnskuLabel = (fnsku: string, title: string, condition: string): Label => ({
  kind: 'fnsku',
  code: fnsku,
  big: fnsku,
  lines: [title.length > 48 ? title.slice(0, 22) + '...' + title.slice(-22) : title, condition || 'New'],
});
