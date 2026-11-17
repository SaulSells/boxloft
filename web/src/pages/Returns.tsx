import { useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, ExternalLink, FileUp, PackageMinus, Undo2 } from 'lucide-react';
import { get, post } from '@/api/client';
import type { Holder } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Chips, Empty, PageHeader, SearchInput, Stat, Thumb } from '@/components/app/common';
import { useToast } from '@/components/app/toast';
import { useAuth } from '@/context/AuthContext';
import { Link } from '@/lib/router';
import { centsFromInput, dollarsForInput, money, plural, when } from '@/lib/format';
import { cn } from '@/lib/utils';

export type RState = 'requested' | 'sent' | 'seller_received' | 'refunded' | 'no_refund' | 'kept';
export interface Ret {
  id: number; line_id: number; qty: number; state: RState; state_label: string; reason: string; status_text: string;
  expected_cents: number; refund_cents: number; owed_cents: number; opened_on: string | null; closed_on: string | null;
  age_days: number | null; source: string; note: string; asin: string; title: string; image_url: string | null;
  unit_cost_cents: number; line_qty: number; product_id: number | null; received: number; pulled: number;
  left_to_receive: number; to_pull: number; order_id: string; order_source: string; ordered_at: string | null; open: boolean;
}
interface RetDetail extends Ret {
  events: { at: string; source: string; state: RState; qty: number; refund_cents: number; text: string }[];
  in_stock?: { box_id: number; location_id: number; qty: number; place: string }[];
}
interface Summary {
  open: number; owed_cents: number; refunded_cents: number; lost_cents: number; to_pull: number; units: number;
  oldest_open_days: number; by_state: Record<RState, number>; by_reason: Record<string, number>;
}

export const RETURN_REASONS = ['Wrong item sent', 'Damaged or defective', 'Arrived empty', 'Not as described', 'Never arrived',
  'Missing parts', 'Better price elsewhere', 'No longer needed', 'Other'];
const TONE: Record<RState, string> = {
  requested: 'border-warning/50 text-warning-ink',
  sent: 'border-info/50 text-info-ink',
  seller_received: 'border-info/50 text-info-ink',
  refunded: 'border-success/50 text-success-ink',
  no_refund: 'border-destructive/60 text-destructive',
  kept: '',
};
const TABS = [
  { key: 'open', label: 'Waiting on a refund' },
  { key: 'pull', label: 'To pull from stock' },
  { key: 'done', label: 'Done' },
  { key: 'all', label: 'All' },
];
const isAmazonOrder = (id: string) => /^\d{3}-\d{7}-\d{7}$/.test(id);
const STATE_LABEL: Record<string, string> = { requested: 'Return started', sent: 'Sent back', seller_received: 'Seller has it',
  refunded: 'Refunded', no_refund: 'Closed, no refund', kept: 'Kept (return cancelled)' };

function PullForm({ r, onDone }: { r: RetDetail; onDone: () => void }) {
  const toast = useToast();
  const spots = r.in_stock ?? [];
  const [pick, setPick] = useState(0);
  const [qty, setQty] = useState(String(Math.min(r.to_pull, spots[0]?.qty ?? r.to_pull)));
  if (!spots.length) return <p className="text-xs text-muted-foreground">Boxloft has none of these on a shelf. If it already left, mark the return below.</p>;
  const s = spots[pick]!;
  const from: Holder = s.box_id ? { box_id: s.box_id } : { location_id: s.location_id };
  const pull = async () => {
    try {
      await post(`/api/returns/${r.id}/pull`, { qty: Number(qty), from, seen: { state: r.state, qty: r.qty } });
      toast(`${qty} taken off the shelf and sent back to the seller`);
      onDone();
    } catch (e) { toast((e as Error).message, { tone: 'error' }); }
  };
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        {spots.map((x, i) => (
          <Button key={`${x.box_id}-${x.location_id}`} size="sm" variant={i === pick ? 'default' : 'outline'} onClick={() => { setPick(i); setQty(String(Math.min(r.to_pull, x.qty))); }}>
            {x.box_id ? `Box ${x.box_id}` : 'Loose'} {x.place && `on ${x.place}`} ({x.qty})
          </Button>
        ))}
      </div>
      <div className="flex items-center gap-2">
        <Input type="number" min={1} max={r.to_pull} value={qty} onChange={(e) => setQty(e.target.value)} className="h-9 w-20" />
        <Button size="sm" onClick={pull} disabled={!Number(qty)}><PackageMinus className="mr-1.5 h-4 w-4" />Pull from stock</Button>
      </div>
    </div>
  );
}

function StateButtons({ r, onDone }: { r: Ret; onDone: () => void }) {
  const toast = useToast();
  const [refunding, setRefunding] = useState(false);
  const [amount, setAmount] = useState(dollarsForInput(r.refund_cents || r.expected_cents));
  const set = async (state: RState, refund?: number | null) => {
    try {
      await post(`/api/returns/${r.id}`, { state, ...(refund != null ? { refund_cents: refund } : {}), seen: { state: r.state, qty: r.qty, refund_cents: r.refund_cents } });
      toast(state === 'kept' ? 'Return cancelled. Those units count as ours again.' : 'Saved');
      setRefunding(false);
      onDone();
    } catch (e) { toast((e as Error).message, { tone: 'error' }); }
  };
  if (refunding) {
    return (
      <div className="flex flex-wrap items-end gap-2">
        <div className="space-y-1"><Label className="text-xs">Refund received ($)</Label>
          <Input inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} className="h-9 w-32" autoFocus /></div>
        <Button size="sm" onClick={() => set('refunded', centsFromInput(amount))}><Check className="mr-1 h-4 w-4" />Save</Button>
        <Button size="sm" variant="ghost" onClick={() => setRefunding(false)}>Cancel</Button>
      </div>
    );
  }
  return (
    <div className="flex flex-wrap gap-2">
      {r.state === 'requested' && <Button size="sm" variant="outline" onClick={() => set('sent')}>Shipped it back</Button>}
      {r.open && r.state !== 'seller_received' && <Button size="sm" variant="outline" onClick={() => set('seller_received')}>Seller has it</Button>}
      {r.state !== 'refunded' && <Button size="sm" variant="outline" onClick={() => setRefunding(true)}>Refunded</Button>}
      {r.open && <Button size="sm" variant="outline" onClick={() => set('no_refund')}>No refund coming</Button>}
      {r.state !== 'kept' && r.state !== 'refunded' && <Button size="sm" variant="ghost" onClick={() => set('kept')}>We kept it</Button>}
      {!r.open && <Button size="sm" variant="ghost" onClick={() => set('requested')}><Undo2 className="mr-1 h-3.5 w-3.5" />Reopen</Button>}
    </div>
  );
}

function ReturnCard({ r, onChange }: { r: Ret; onChange: () => void }) {
  const [open, setOpen] = useState(r.to_pull > 0);
  const d = useQuery({ queryKey: ['return', r.id, r.state, r.to_pull, r.refund_cents], queryFn: () => get<RetDetail>(`/api/returns/${r.id}`), enabled: open });
  return (
    <Card className={cn(!r.open && !r.to_pull && 'opacity-80')}>
      <CardContent className="space-y-3 p-4">
        <div className="flex gap-3">
          {r.image_url ? <Thumb photoId={null} url={r.image_url} />
            : <div className="h-16 w-16 shrink-0 rounded-md bg-muted" />}
          <div className="min-w-0 flex-1 space-y-1">
            <div className="line-clamp-2 text-sm font-medium">
              {r.product_id ? <Link to={`/inventory/${r.product_id}`} className="hover:underline">{r.title || r.asin}</Link> : (r.title || r.asin)}
            </div>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
              <span><b className="text-foreground">{r.qty}</b> of {r.line_qty} going back · {money(r.unit_cost_cents)} each</span>
              {isAmazonOrder(r.order_id)
                ? <a href={`https://www.amazon.com/your-orders/order-details?orderID=${r.order_id}`} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 font-mono hover:underline">{r.order_id}<ExternalLink className="h-3 w-3" /></a>
                : <span className="font-mono">{r.order_id}</span>}
              {r.ordered_at && <span>bought {r.ordered_at}</span>}
            </div>
            <div className="text-xs">
              {r.reason && <span>{r.reason} · </span>}
              <span className="text-muted-foreground">opened {r.opened_on ?? '?'}{r.open && r.age_days != null ? `, ${plural(r.age_days, 'day')} ago` : ''}</span>
            </div>
            {r.status_text && <div className="text-xs text-muted-foreground">Amazon: “{r.status_text}”</div>}
          </div>
          <div className="flex shrink-0 flex-col items-end gap-1 text-right">
            <Badge variant="outline" className={TONE[r.state]}>{r.state_label}</Badge>
            {r.open ? <div className="text-sm"><b>{money(r.owed_cents)}</b><div className="text-xs text-muted-foreground">owed</div></div>
              : r.refund_cents ? <div className="text-sm text-muted-foreground">{money(r.refund_cents)} back</div> : null}
          </div>
        </div>
        {r.to_pull > 0 && (
          <div className="space-y-2 rounded-lg bg-warning/10 px-3 py-2">
            <div className="text-sm font-medium">{plural(r.to_pull, 'unit')} still on our shelves. Pull {r.to_pull === 1 ? 'it' : 'them'} when {r.to_pull === 1 ? 'it ships' : 'they ship'} back.</div>
            {d.data ? <PullForm r={d.data} onDone={onChange} /> : <Button size="sm" variant="outline" onClick={() => setOpen(true)}>Show where</Button>}
          </div>
        )}
        <StateButtons r={r} onDone={onChange} />
        <button className="text-xs text-muted-foreground underline" onClick={() => setOpen(!open)}>{open ? 'Hide history' : 'History'}</button>
        {open && d.data && (
          <div className="space-y-0.5 text-xs text-muted-foreground">
            {d.data.events.slice().reverse().map((e, i) => (
              <div key={i}>{when(e.at)} · {e.source === 'amazon' ? 'Amazon page' : e.source === 'export' ? 'Amazon data file' : e.source === 'audit' ? 'Stock audit' : 'Boxloft'} · {e.text || STATE_LABEL[e.state] || e.state}</div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ImportAmazonFiles({ onDone }: { onDone: () => void }) {
  const toast = useToast();
  const file = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const go = async (list: FileList | null) => {
    if (!list?.length) return;
    setBusy(true);
    try {
      const files: Record<string, string> = {};
      for (const f of Array.from(list)) files[f.name] = await f.text();
      const r = await post<{ refunds: number; returns: number; refunded_cents: number; unmatched: string[] }>('/api/returns/import', { files });
      toast(`${plural(r.refunds, 'refund')} (${money(r.refunded_cents)}) and ${plural(r.returns, 'open return')} matched to your orders` +
        (r.unmatched.length ? `. ${r.unmatched.length} orders had more than one item and no amount to tell them apart.` : ''));
      onDone();
    } catch (e) { toast((e as Error).message, { tone: 'error' }); }
    finally { setBusy(false); if (file.current) file.current.value = ''; }
  };
  return (
    <>
      <Button variant="outline" onClick={() => file.current?.click()} disabled={busy}><FileUp className="mr-1.5 h-4 w-4" />{busy ? 'Reading...' : 'Import Amazon files'}</Button>
      <input ref={file} type="file" multiple accept=".csv,text/csv" hidden onChange={(e) => go(e.target.files)} />
    </>
  );
}

export function Returns() {
  const qc = useQueryClient();
  const { user } = useAuth();
  const [tab, setTab] = useState('open');
  const [q, setQ] = useState('');
  const res = useQuery({ queryKey: ['returns', tab, q], queryFn: () => get<{ returns: Ret[]; summary: Summary }>(`/api/returns?status=${tab}&q=${encodeURIComponent(q)}`) });
  const s = res.data?.summary;
  const refresh = () => { qc.invalidateQueries({ queryKey: ['returns'] }); qc.invalidateQueries({ queryKey: ['return'] }); };
  return (
    <div className="max-w-5xl space-y-4">
      <PageHeader title="Returns" subtitle="Things we bought and sent back to the seller." about="What's still owed, and what still has to come off the shelf."
        actions={user?.is_admin ? <ImportAmazonFiles onDone={refresh} /> : undefined} />
      {s && (
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <Stat label="Refunds owed" value={money(s.owed_cents)} hint={`${plural(s.open, 'open return')}${s.oldest_open_days ? `, oldest ${s.oldest_open_days} days` : ''}`} tone={s.owed_cents ? 'warn' : undefined} />
          <Stat label="To pull from stock" value={s.to_pull} hint="returned, still on a shelf" tone={s.to_pull ? 'warn' : undefined} />
          <Stat label="Refunded" value={money(s.refunded_cents)} hint={`${plural(s.units, 'unit')} sent back in all`} />
          <Stat label="Lost (no refund)" value={money(s.lost_cents)} tone={s.lost_cents ? 'bad' : undefined} />
        </div>
      )}
      <Chips items={TABS.map((t) => ({ key: t.key, label: t.label }))} value={tab} onChange={setTab} />
      <SearchInput placeholder="Search item, ASIN or order #" value={q} onChange={(e) => setQ(e.target.value)} />
      {res.isLoading && <Empty>Loading…</Empty>}
      {res.data && !res.data.returns.length && <Empty>{tab === 'open' ? 'No refunds waiting.' : tab === 'pull' ? 'Nothing to pull.' : 'Nothing here.'}</Empty>}
      <div className="space-y-3">{res.data?.returns.map((r) => <ReturnCard key={r.id} r={r} onChange={refresh} />)}</div>
      <p className="text-xs text-muted-foreground">
        Amazon returns come in by themselves: the Chrome extension reads each package's status on the order page.
        Older ones come from Amazon's data request (Your Returns &amp; Refunds folder: Returns Status.csv and Refund Details.csv).
        To send back something that's already on a shelf, open the product and press "Send back to seller".
      </p>
    </div>
  );
}

/** Product page: send some of this product back to the seller it was bought from. */
export function SendBackForm({ productId, where, onDone, onCancel }: {
  productId: number;
  where: { box_id: number; location_id: number; qty: number; location_code: string; unit_id: number }[];
  onDone: () => void;
  onCancel: () => void;
}) {
  const toast = useToast();
  const lines = useQuery({ queryKey: ['return-lines', productId], queryFn: () => get<{ lines: { id: number; order_id: string; ordered_at: string; qty: number; received: number; returned: number; unit_cost_cents: number }[] }>(`/api/returns/for-product/${productId}`) });
  const [lineId, setLineId] = useState<number | null>(null);
  const [qty, setQty] = useState('1');
  const [reason, setReason] = useState('');
  const [note, setNote] = useState('');
  const [leaving, setLeaving] = useState(false);
  const spots = where.filter((w) => !w.unit_id);
  const [spot, setSpot] = useState(0);
  const ls = lines.data?.lines ?? [];
  const line = ls.find((l) => l.id === lineId) ?? ls[0];
  const save = async () => {
    if (!line) return;
    try {
      const r = await post<Ret>('/api/returns', { line_id: line.id, qty: Number(qty), reason, note, on_shelf: true });
      if (leaving && spots[spot]) {
        const s = spots[spot]!;
        await post(`/api/returns/${r.id}/pull`, { qty: Number(qty), from: s.box_id ? { box_id: s.box_id } : { location_id: s.location_id }, seen: { state: r.state, qty: r.qty } });
      }
      toast(leaving ? `${qty} sent back and taken off the shelf` : `Return started. It's on Returns until the refund comes.`);
      onDone();
    } catch (e) { toast((e as Error).message, { tone: 'error' }); }
  };
  if (lines.isLoading) return <Empty>Loading…</Empty>;
  if (!ls.length) {
    return (
      <div className="space-y-2 rounded-lg border bg-muted/30 p-3 text-sm">
        <p>This wasn't received against a purchase, so there's no order to send it back on. Receive it against its order first (Purchases), or take it out with Remove → Damaged.</p>
        <Button size="sm" variant="ghost" onClick={onCancel}>Close</Button>
      </div>
    );
  }
  return (
    <div className="space-y-3 rounded-lg border bg-muted/30 p-3">
      <div className="space-y-1">
        <Label className="text-xs">Bought on</Label>
        <div className="flex flex-wrap gap-2">
          {ls.map((l) => (
            <Button key={l.id} size="sm" variant={line?.id === l.id ? 'default' : 'outline'} onClick={() => setLineId(l.id)}>
              <span className="font-mono">{l.order_id}</span>&nbsp;· {l.ordered_at} · {l.qty} × {money(l.unit_cost_cents)}{l.returned ? ` · ${l.returned} already back` : ''}
            </Button>
          ))}
        </div>
      </div>
      <div className="grid gap-3 sm:grid-cols-[100px_1fr]">
        <div className="space-y-1"><Label className="text-xs">How many</Label><Input type="number" min={1} value={qty} onChange={(e) => setQty(e.target.value)} className="h-10" /></div>
        <div className="space-y-1">
          <Label className="text-xs">Why</Label>
          <div className="flex flex-wrap gap-1.5">
            {RETURN_REASONS.map((x) => <Button key={x} type="button" size="sm" variant={reason === x ? 'default' : 'outline'} className="h-8" onClick={() => setReason(x)}>{x}</Button>)}
          </div>
        </div>
      </div>
      <Input placeholder="Note (optional): what was wrong, return label #" value={note} onChange={(e) => setNote(e.target.value)} className="h-10" />
      {spots.length > 0 && (
        <div className="space-y-2">
          <div className="flex flex-wrap gap-2">
            <Button type="button" size="sm" variant={!leaving ? 'default' : 'outline'} onClick={() => setLeaving(false)}>Still on the shelf</Button>
            <Button type="button" size="sm" variant={leaving ? 'default' : 'outline'} onClick={() => setLeaving(true)}>Leaving today: take it off now</Button>
          </div>
          {leaving && (
            <div className="flex flex-wrap gap-2">
              {spots.map((w, i) => <Button key={`${w.box_id}-${w.location_id}`} size="sm" variant={spot === i ? 'default' : 'outline'} onClick={() => setSpot(i)}>{w.box_id ? `Box ${w.box_id}` : 'Loose'} on {w.location_code} ({w.qty})</Button>)}
            </div>
          )}
        </div>
      )}
      <div className="flex gap-2">
        <Button onClick={save} disabled={!reason || !Number(qty)}><Check className="mr-1.5 h-4 w-4" />Start return</Button>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
      <p className="text-xs text-muted-foreground">The refund owed shows on Returns until it comes. If it stays on the shelf for now, Returns lists it under "To pull from stock".</p>
    </div>
  );
}
