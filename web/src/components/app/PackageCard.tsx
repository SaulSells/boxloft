/** What is inside a box that came in, from its carrier label (Alex 11-16, backend/labels.py).
 * One card per scan: the carrier and tracking number, each order the box holds, and each item with how many are in
 * this box and how many are still to put away. On a box page each item has "Put N in A31". */
import { PackageX, RotateCcw, Truck, Undo2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Thumb } from '@/components/app/common';
import { day, money, when } from '@/lib/format';

export interface LabelItem {
  line_id: number; title: string; sku: string | null; image_url: string | null; photo_id: number | null; asin: string | null;
  product_id: number | null; in_package: number; left: number; cost_cents: number | null;
}
export interface LabelOrder {
  po_id: number; source: string; order_id: string; ordered_at: string; state: string; delivered_on: string | null;
  photo_url: string | null; items: LabelItem[];
}
export interface LabelCard {
  kind: 'label'; tracking_id: string; carrier: string; orders: LabelOrder[]; units: number; left: number;
  scanned_before: { at: string; by: string }[];
}
/** A buyer's return label (Alex 11-16, backend/buyer_returns.py): the item, why it came back, where it shipped from. */
export interface ReturnItem {
  return_id: number; order_id: string; ordered_on: string | null; requested_on: string | null; title: string; asin: string; sku: string;
  qty: number; back: number; left: number; reason_code: string; reason: string; resolution: string; status: string;
  refunded_cents: number | null; product_id: number | null; photo_id: number | null; image_url: string | null;
  shipped_from: { box_id: number | null; location_id: number | null; name: string; shipped_at: string } | null;
  taken: { qty: number; condition: 'good' | 'damaged'; at: string; by: string; action_id: number }[];
}
export interface ReturnCard {
  kind: 'label_return'; tracking_id: string; carrier: string; channel: string; channel_name: string; items: ReturnItem[]; units: number; left: number;
}
export type LabelAnswer = LabelCard | ReturnCard
  | { kind: 'label_ambiguous'; code: string; choices: LabelCard[] }
  | { kind: 'label_unknown'; code: string; carrier: string; routing?: boolean };

export const isLabel = (r: { kind: string }): r is LabelAnswer => r.kind === 'label' || r.kind === 'label_return' || r.kind === 'label_ambiguous' || r.kind === 'label_unknown';

const store = (s: string) => (s === 'amazon' ? 'Amazon' : s === 'walmart.com' ? 'Walmart' : s);

function Contents({ c, boxName, busy, onPut }: { c: LabelCard; boxName?: string; busy?: boolean; onPut?: (i: LabelItem, qty: number) => void }) {
  return (
    <div className="space-y-3">
      {c.orders.map((o) => (
        <div key={o.po_id} className="space-y-2">
          <div className="flex items-start gap-2 text-xs text-muted-foreground">
            <div className="min-w-0 flex-1">
              {store(o.source)} order <span className="font-mono">{o.order_id}</span> · ordered {day(o.ordered_at)}
              {o.delivered_on ? ` · delivered ${day(o.delivered_on)}` : ''}
            </div>
            {o.photo_url && <a href={o.photo_url} target="_blank" rel="noreferrer" className="shrink-0"><img src={o.photo_url} alt="Delivery photo" className="h-12 w-9 rounded border object-cover" /></a>}
          </div>
          {o.items.map((i) => (
            <div key={i.line_id} className="space-y-2">
              <div className="flex items-center gap-3">
                <Thumb photoId={i.photo_id} url={i.image_url} />
                <div className="min-w-0 flex-1">
                  <div className="line-clamp-2 text-sm font-medium leading-snug" title={i.title}>{i.title}</div>
                  <div className="text-xs text-muted-foreground">
                    <b className="text-foreground">{i.in_package}</b> in this box · {i.left ? <><b className="text-foreground">{i.left}</b> to put away</> : 'all put away'}
                  </div>
                </div>
              </div>
              {onPut && i.left > 0 && (
                <div className="flex gap-2">
                  <Button className="h-11 flex-1" disabled={busy} onClick={() => onPut(i, i.left)}>Put {i.left} in {boxName}</Button>
                  {i.left > 1 && <Button variant="outline" className="h-11 px-4" disabled={busy} onClick={() => onPut(i, 1)}>Just 1</Button>}
                </div>
              )}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

function ReturnView({ r, boxName, busy, onReturn, onUndo, onClose }: {
  r: ReturnCard; boxName?: string; busy?: boolean;
  onReturn?: (i: ReturnItem, qty: number, condition: 'good' | 'damaged') => void; onUndo?: (actionId: number) => void; onClose?: () => void;
}) {
  return (
    <div className="space-y-3 rounded-lg border p-3" role="region" aria-label="Buyer return">
      <div>
        <div className="flex items-center gap-2 text-sm font-semibold"><RotateCcw className="h-4 w-4" />{r.channel_name} buyer return</div>
        <div className="font-mono text-xs text-muted-foreground">{r.carrier} {r.tracking_id}</div>
        <div className="mt-0.5 text-sm">{r.left ? <><b>{r.left}</b> of {r.units} to take back in</> : <>All {r.units} taken back in</>}</div>
      </div>
      {r.items.map((i) => (
        <div key={i.return_id} className="space-y-2">
          <div className="flex items-center gap-3">
            <Thumb photoId={i.photo_id} url={i.image_url} />
            <div className="min-w-0 flex-1">
              <div className="line-clamp-2 text-sm font-medium leading-snug" title={i.title}>{i.title}</div>
              <div className="text-xs text-muted-foreground"><b className="text-foreground">{i.qty}</b> sent back · order <span className="font-mono">{i.order_id}</span></div>
            </div>
          </div>
          <div className="rounded-md bg-[hsl(var(--warning)/0.12)] px-3 py-2">
            <div className="text-xs text-muted-foreground">Buyer's reason</div>
            <div className="text-sm font-semibold">{i.reason}</div>
            <div className="text-xs text-muted-foreground">
              {i.requested_on ? `Return started ${day(i.requested_on)}` : ''}{i.resolution ? ` · ${i.resolution}` : ''}{i.refunded_cents ? ` · ${money(i.refunded_cents)}` : ''}
            </div>
          </div>
          {i.shipped_from && <div className="text-xs text-muted-foreground">Shipped from <b className="text-foreground">{i.shipped_from.name}</b> on {day(i.shipped_from.shipped_at)}</div>}
          {i.taken.map((t) => (
            <div key={t.action_id} className="flex items-center gap-2 text-xs">
              <span className="min-w-0 flex-1">{t.qty} {t.condition === 'good' ? 'back on the shelf' : 'marked damaged'} · {when(t.at)}{t.by ? ` by ${t.by}` : ''}</span>
              {onUndo && <Button variant="outline" size="sm" className="h-8" disabled={busy} onClick={() => onUndo(t.action_id)}><Undo2 className="mr-1 h-3.5 w-3.5" />Undo</Button>}
            </div>
          ))}
          {onReturn && i.left > 0 && !i.product_id && <div className="text-xs text-muted-foreground">No item in Boxloft has {i.asin || i.sku} yet. Add the item, then scan the label again.</div>}
          {onReturn && i.left > 0 && i.product_id && (
            <div className="flex gap-2">
              {boxName && <Button className="h-11 flex-1" disabled={busy} onClick={() => onReturn(i, i.left, 'good')}>Put {i.left} back in {boxName}</Button>}
              <Button variant="outline" className={boxName ? 'h-11 px-4' : 'h-11 flex-1'} disabled={busy} onClick={() => onReturn(i, 1, 'damaged')}><PackageX className="mr-1.5 h-4 w-4" />Damaged</Button>
            </div>
          )}
        </div>
      ))}
      {onClose && <Button variant="outline" className="h-11 w-full" onClick={onClose}>Done with this box</Button>}
    </div>
  );
}

export function PackageCard({ r, boxName, busy, onPut, onPick, onClose, onReturn, onUndo }: {
  r: LabelAnswer; boxName?: string; busy?: boolean;
  onPut?: (i: LabelItem, qty: number, tracking: string) => void; onPick?: (tracking: string) => void; onClose?: () => void;
  onReturn?: (i: ReturnItem, qty: number, condition: 'good' | 'damaged') => void; onUndo?: (actionId: number) => void;
}) {
  if (r.kind === 'label_return') return <ReturnView r={r} boxName={boxName} busy={busy} onReturn={onReturn} onUndo={onUndo} onClose={onClose} />;
  if (r.kind === 'label_unknown' && r.routing) {
    return (
      <div className="space-y-2 rounded-lg border p-3 text-sm" role="status">
        <div className="flex items-center gap-2 font-semibold"><Truck className="h-4 w-4" />That is the ZIP code barcode</div>
        <div className="text-muted-foreground">Scan the long barcode on the label, the one with the tracking number under it.</div>
        {onClose && <Button variant="outline" className="h-11 w-full" onClick={onClose}>OK</Button>}
      </div>
    );
  }
  if (r.kind === 'label_unknown') {
    return (
      <div className="space-y-2 rounded-lg border p-3 text-sm" role="status">
        <div className="flex items-center gap-2 font-semibold"><Truck className="h-4 w-4" />{r.carrier} label <span className="font-mono text-xs font-normal">{r.code}</span></div>
        <div className="text-muted-foreground">No order in Boxloft has this tracking number. Boxloft gets tracking numbers from Amazon's Track pages (packages shipped since Nov 12), Walmart's order pages and the stores' shipping emails. Open the box and scan the items.</div>
        {onClose && <Button variant="outline" className="h-11 w-full" onClick={onClose}>OK</Button>}
      </div>
    );
  }
  if (r.kind === 'label_ambiguous') {
    return (
      <div className="space-y-3 rounded-lg border border-warning/60 p-3 text-sm" role="alertdialog" aria-label="Which package">
        <div className="font-semibold">This label fits {r.choices.length} packages. Which one is it?</div>
        {r.choices.map((c) => (
          <div key={c.tracking_id} className="space-y-2 rounded-md border p-2.5">
            <div className="font-mono text-xs">{c.carrier} {c.tracking_id}</div>
            <Contents c={c} />
            {onPick && <Button className="h-11 w-full" disabled={busy} onClick={() => onPick(c.tracking_id)}>This one</Button>}
          </div>
        ))}
        {onClose && <Button variant="outline" className="h-11 w-full" onClick={onClose}>Cancel</Button>}
      </div>
    );
  }
  const first = r.scanned_before[0];   // shown once it is 10+ minutes old: a box someone opened before, not this scan
  const before = first && Date.now() - Date.parse(first.at) > 10 * 60_000 ? first : null;
  return (
    <div className="space-y-3 rounded-lg border p-3" role="region" aria-label="What is in this box">
      <div>
        <div className="flex items-center gap-2 text-sm font-semibold"><Truck className="h-4 w-4" />{r.carrier || 'Carrier'} box <span className="font-mono text-xs font-normal">{r.tracking_id}</span></div>
        <div className="mt-0.5 text-sm">
          {r.units ? (r.left ? <><b>{r.left}</b> of {r.units} items still to put away</> : <>All {r.units} items are put away</>) : 'Boxloft has this tracking number but not which items it holds.'}
        </div>
        {before && <div className="text-xs text-muted-foreground">Label first scanned {when(before.at)}{before.by ? ` by ${before.by}` : ''}</div>}
      </div>
      <Contents c={r} boxName={boxName} busy={busy} onPut={onPut && ((i, q) => onPut(i, q, r.tracking_id))} />
      {onClose && <Button variant="outline" className="h-11 w-full" onClick={onClose}>Done with this box</Button>}
    </div>
  );
}
