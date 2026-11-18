import { useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, Copy, ExternalLink, MapPin, PackagePlus, RotateCcw, SearchX, ShoppingCart, Store, Undo2, X } from 'lucide-react';
import { get, post } from '@/api/client';
import type { Holder, LocationRow } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Progress } from '@/components/ui/progress';
import { Chips, Empty, HolderPicker, PageHeader, SearchInput, Thumb } from '@/components/app/common';
import { MarketPicker } from '@/components/app/MarketPicker';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { useToast } from '@/components/app/toast';
import { Link, useQueryParam } from '@/lib/router';
import { centsFromInput, kinds, money, plural, when } from '@/lib/format';
import { ebayQuery } from '@/lib/links';
import { RETURN_REASONS } from '@/pages/Returns';
import { cn } from '@/lib/utils';
import { curHolder, isFresh, readCurrentBox } from '@/lib/currentBox';
import { SortBar } from '@/pages/AuditSort';

type Status = 'todo' | 'partial' | 'found' | 'short' | 'missing';
interface Item {
  id: number; asin: string; match_asin: string; line_id: number | null; store: string; title: string; brand: string; category: string; image_url: string | null;
  expected: number; exp_listed: number; exp_unlisted: number; exp_problem: number; exp_transit: number; exp_delivered: number; exp_cancelled: number; exp_cut: number;
  cost_cents: number; avg_cost_cents: number; first_buy: string; last_buy: string;
  orders: { id: string; date: string; units: number; delivered?: boolean; delivered_on?: string; cancelled?: boolean; cut?: number; units_snapshot?: number; amazon?: string; amazon_on?: string }[];
  /** Snapshot rows: where the units stand by Amazon's live order status (audit.py _where). */
  where?: { kind: 'delivered' | 'on_way' | 'returned' | 'no_record'; units: number; from: string; to: string }[];
  found: number; received: number; sold: number; sent_back: number; accounted: number; seller_returns: number;
  /** bought - returned_before - amazon_sold = expected (should be here). amazon_sold: every Amazon sale nothing here
   *  shipped, oldest buy first, including sales a save tied to this row (audit.py line_presold, 11-11 night). */
  bought: number; amazon_sold: number; returned_before: number;
  /** Open orders for this ASIN that nothing here shipped yet (audit.open_sales): the unit is still ours to ship. */
  open_sales?: { line_id: number; qty: number; channel: string; order_id: string; ordered_at: string }[];
  /** 'own' = counted from Boxloft's order lines; sheet_expected = the 10-02 sheet's number, kept as history. */
  count?: 'own' | 'sheet' | 'orders'; sheet_expected?: number | null;
  closed: boolean; close_reason: string; close_note: string; status: Status; product_id: number | null;
  listed?: Listed[];
  mark_seq?: number;
}
export interface Listed {
  state: 'confirmed' | 'manual' | 'suggested'; platform: string; label: string; qty: number | null; sku?: string; asin?: string;
  status?: string; price_cents?: number | null; listing_id?: number; mark_id: number | null; match?: string; title?: string;
  url?: string; note?: string; fba_inbound?: number;
  live?: boolean;   // for sale right now: set by the server (listings.is_live), the one definition
}
interface HistoryRow {
  id: number; at: string; kind: string; qty: number; box_id?: number | null; location_id?: number | null; location_name?: string | null;
  reason: string; note: string; undone: number; outcome_id?: number; channel_label?: string; price_cents?: number | null;
  sold_on?: string | null; order_ref?: string; return_state?: string | null;
}
interface Summary {
  items: number; units: number; cost_cents: number; found_units: number; found_cents: number; checked: number;
  sold_units: number; sent_back_units: number; accounted_units: number; by_status: Record<Status, number>; snapshot: string;
  rows_for_sale?: number; rows_for_sale_confirmed?: number; units_listed?: number;
  bought_units?: number; amazon_sold_units?: number; returned_before_units?: number;
  /** Starting counts from Boxloft's records (audit.py _base), 11-11. */
  counted?: { buys_through: string; rows: number; differ: number } | null;
}
interface Outcomes {
  outcomes: { outcome: string; channel: string; channel_label: string; units: number; cost_cents: number; sales_cents: number | null; priced_units: number | null; priced_cost_cents: number | null }[];
  closed: Record<string, { units: number; cost_cents: number }>;
  channels: Record<string, string>;
}

const STATUS: Record<Status, { label: string; cls: string }> = {
  todo: { label: 'To check', cls: '' },
  partial: { label: 'Some found', cls: 'border-warning/50 text-warning-ink' },
  found: { label: 'All accounted for', cls: 'border-success/50 text-success-ink' },
  short: { label: 'Done, some missing', cls: 'border-warning/50 text-warning-ink' },
  missing: { label: 'Not found', cls: 'border-destructive/60 text-destructive' },
};
const listedNow = (x: Listed) => !!x.live;
const TABS: { key: string; label: string; match: (it: Item) => boolean }[] = [
  { key: 'open', label: 'To check', match: (it) => it.status === 'todo' || it.status === 'partial' },
  { key: 'found', label: 'Done', match: (it) => it.status === 'found' },
  { key: 'missing', label: 'Missing', match: (it) => it.status === 'missing' || it.status === 'short' },
  { key: 'listed', label: 'For sale now', match: (it) => (it.listed ?? []).some(listedNow) },
  { key: 'all', label: 'All', match: () => true },
];
const REASONS = [
  { key: 'not_found', label: 'Not found' },

  { key: 'damaged', label: 'Damaged or thrown out' },
  { key: 'other', label: 'Other' },
];
const PAGE = 40;

/** What this screen showed for the row. A save is refused (409) if the row has changed since, for example because
 *  the other person just found or closed it (audit.check_seen); the page then reloads. */
// The newest listing decision the card showed: a confirm, reject, mark or undo made from an older screen is refused (409).
const seenMarks = (it: Pick<Item, 'mark_seq'>) => it.mark_seq ?? 0;
const seenOf = (it: Pick<Item, 'accounted' | 'expected'> & { closed?: boolean }) =>
  ({ accounted: it.accounted, expected: it.expected, ...(it.closed === undefined ? {} : { closed: it.closed }) });

const span = (a: string, b: string) => (a === b ? a : `${a} to ${b}`);
const WHERE: Record<string, (n: number, a: string, b: string) => string> = {
  delivered: (n, a, b) => (a ? `${n} delivered ${span(a, b)}` : `${n} delivered`),
  on_way: (n, _a, b) => `${n} not delivered yet (Amazon, as of ${b})`,
  returned: (n) => `${n} on an order Amazon shows returned`,
  no_record: (n, a, b) => `${n} bought ${span(a, b)}, no delivery record in Boxloft`,
};

/** bought - sent back - sold on Amazon = should be here. Rows made from orders count their delivered units as bought. */
function countLine(it: Item): string {
  return [
    `${it.bought} bought`,
    it.returned_before && `${it.returned_before} sent back to the seller`,
    it.amazon_sold && `${it.amazon_sold} sold on Amazon`,
  ].filter(Boolean).join(', ');
}

function where(it: Item): string {
  // Amazon's live order status; the 10-02 sheet's labels ("never listed", "never marked received") were often out of date.
  const parts = [
    ...(it.where ?? []).map((w) => WHERE[w.kind]?.(w.units, w.from, w.to)),
    it.exp_problem && `old tracker flagged a problem with ${it.exp_problem}`,
    it.exp_delivered && (it.orders.some((o) => o.delivered)
      ? `${it.exp_delivered} delivered ${it.orders.find((o) => o.delivered)?.delivered_on ?? ''}, not scanned in`
      : `${it.exp_delivered} ordered ${it.orders[0]?.date ?? ''}, no delivery news, not scanned in`),
  ].filter(Boolean);
  const out = parts.join(', ');
  const gone = [
    it.exp_cut && `${it.exp_cut} of those Amazon cancelled, not counted`,
    it.exp_cancelled && `${it.exp_cancelled} more on an order Amazon cancelled, not counted`,
  ].filter(Boolean).join('; ');
  return gone ? `${out}${out ? '; ' : ''}${gone}` : out;
}

/** Found it: count, box or place, note. Also used on an order card (Orders), so both save through the same call. */
export function FoundForm({ it, onDone, onCancel }: { it: Pick<Item, 'id' | 'expected' | 'accounted'>; onDone: () => void; onCancel: () => void }) {
  // What this form was opened against: if the other person's saves land first, the save gets a 409, not a total past expected.
  const [seen0] = useState(() => seenOf(it));
  const toast = useToast();
  const qc = useQueryClient();
  // Flow audit 11-12 (finding 8): every audit form starts at 1 (Found was 55, Sold elsewhere 55, Sent back 1 on
  // one row), and the box being filled (header chip "Into A36") is already the place.
  const left = Math.max(1, it.expected - it.accounted);
  const [qty, setQty] = useState('1');
  const [cur] = useState(() => { const c = readCurrentBox(); return isFresh(c) ? c : null; });
  const [to, setTo] = useState<Holder | null>(cur ? curHolder(cur) : null);
  const [toName, setToName] = useState(cur?.name ?? '');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [placing, setPlacing] = useState(false);
  const [site, setSite] = useState('');
  const [newSite, setNewSite] = useState('');
  const [room, setRoom] = useState('');
  const locs = useQuery({ queryKey: ['locations'], queryFn: () => get<{ locations: LocationRow[] }>('/api/locations') });
  const sites = [...new Set((locs.data?.locations ?? []).map((l) => l.site || '').filter(Boolean))].sort();
  const pickedSite = site === '__new' ? newSite.trim() : site || sites[0] || '';
  const addPlace = async () => {
    try {
      const l = await post<{ id: number; name: string; site: string }>('/api/places', { site: pickedSite, room });
      const label = l.name && l.name !== l.site ? `${l.site} · ${l.name}` : l.site;
      setTo({ location_id: l.id }); setToName(label); setPlacing(false); setRoom(''); setNewSite('');
      qc.invalidateQueries({ queryKey: ['locations'] });
      toast(`${label} added. It's in the list for every item now.`);
    } catch (e) { toast((e as Error).message, { tone: 'error' }); }
  };
  const save = async () => {
    if (!to) { toast('Scan or pick a box or place first', { tone: 'error' }); return; }
    setBusy(true);
    try {
      await post(`/api/audit/${it.id}/found`, { qty: Number(qty), to, note, seen: seen0 });
      toast(`${qty} in ${toName}`);
      onDone();
    } catch (e) { if ((e as { status?: number }).status === 409) onCancel();
      toast((e as Error).message, { tone: 'error' });
    } finally { setBusy(false); }
  };
  return (
    <div className="space-y-3 rounded-lg border bg-muted/30 p-3">
      <div className="grid gap-3 sm:grid-cols-[120px_1fr]">
        <div className="space-y-1">
          <Label htmlFor={`q${it.id}`}>How many here</Label>
          <Input id={`q${it.id}`} type="number" inputMode="numeric" min={1} value={qty} onChange={(e) => setQty(e.target.value)} className="h-10" />
          {left > 1 && qty !== String(left) && <Button type="button" size="sm" variant="ghost" className="h-8 px-2" onClick={() => setQty(String(left))}>All {left}</Button>}
        </div>
        <div className="space-y-1">
          <MarketPicker className="mb-2" current={to && 'box_id' in to ? toName : null} onPick={(b) => { setTo({ box_id: b.id }); setToName(b.code); }} />
          <HolderPicker value={to} onChange={(h, name) => { setTo(h); setToName(name); }} label="Where (scan a box label, or pick a box or place)" />
          <div className="flex flex-wrap gap-1">
            <Button type="button" size="sm" variant="ghost" className="h-7 px-2" onClick={() => setPlacing(true)}><MapPin className="mr-1 h-3.5 w-3.5" />New place (room or site)</Button>
          </div>
          {placing && (
            <div className="space-y-2 rounded-md border bg-background p-2">
              <div className="grid gap-2 sm:grid-cols-2">
                <div className="space-y-1">
                  <Label className="text-xs">Site</Label>
                  <Select value={site || sites[0] || '__new'} onValueChange={setSite}>
                    <SelectTrigger className="h-9 w-full"><SelectValue placeholder="Pick a site" /></SelectTrigger>
                    <SelectContent>
                      {sites.map((x) => <SelectItem key={x} value={x}>{x}</SelectItem>)}
                      <SelectItem value="__new">New site…</SelectItem>
                    </SelectContent>
                  </Select>
                  {(site === '__new' || !sites.length) && <Input autoFocus placeholder="Site name, e.g. Alex's house" value={newSite} onChange={(e) => { setNewSite(e.target.value); setSite('__new'); }} className="h-9" />}
                </div>
                <div className="space-y-1">
                  <Label className="text-xs">Room or area (optional)</Label>
                  <Input placeholder="e.g. Back room" value={room} onChange={(e) => setRoom(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); void addPlace(); } }} className="h-9" />
                </div>
              </div>
              <div className="flex gap-2">
                <Button type="button" size="sm" onClick={addPlace} disabled={pickedSite.length < 2}>Add place</Button>
                <Button type="button" size="sm" variant="ghost" onClick={() => setPlacing(false)}>Cancel</Button>
              </div>
            </div>
          )}
        </div>
      </div>
      <Input placeholder="Note (optional): condition, where it was" value={note} onChange={(e) => setNote(e.target.value)} className="h-10" />
      <div className="flex gap-2">
        <Button onClick={save} disabled={busy || !Number(qty)}><Check className="mr-1.5 h-4 w-4" />Save</Button>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
      <p className="text-xs text-muted-foreground">Saving puts them in Boxloft's stock there, at the cost from the Amazon order. Some here, some somewhere else? Save once for each.</p>
    </div>
  );
}

const CHANNELS: [string, string][] = [['ebay', 'eBay'], ['amazon', 'Amazon'], ['walmart', 'Walmart'], ['facebook', 'Facebook Marketplace'],
  ['mercari', 'Mercari'], ['whatnot', 'Whatnot'], ['tcgplayer', 'TCGplayer'], ['local', 'In person'], ['other', 'Other']];

function SoldForm({ it, onDone, onCancel }: { it: Item; onDone: () => void; onCancel: () => void }) {
  // What this form was opened against: if the other person's saves land first, the save gets a 409, not a total past expected.
  const [seen0] = useState(() => seenOf(it));
  const toast = useToast();
  const [qty, setQty] = useState('1');
  const [channel, setChannel] = useState('');
  const [price, setPrice] = useState('');
  const [soldOn, setSoldOn] = useState('');
  const [ref, setRef] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try {
      await post(`/api/audit/${it.id}/sold`, { qty: Number(qty), channel, price_cents: centsFromInput(price), sold_on: soldOn, order_ref: ref, note, seen: seen0 });
      toast(`${qty} sold on ${CHANNELS.find((c) => c[0] === channel)?.[1]}`);
      onDone();
    } catch (e) { if ((e as { status?: number }).status === 409) onCancel(); toast((e as Error).message, { tone: 'error' }); }
    finally { setBusy(false); }
  };
  const q = encodeURIComponent(ebayQuery(it.title, []));
  return (
    <div className="space-y-3 rounded-lg border bg-muted/30 p-3">
      <div className="space-y-1">
        <Label className="text-xs">Where did it sell?</Label>
        <div className="flex flex-wrap gap-1.5">
          {CHANNELS.map(([k, label]) => <Button key={k} type="button" size="sm" className="h-8" variant={channel === k ? 'default' : 'outline'} onClick={() => setChannel(k)}>{label}</Button>)}
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div className="space-y-1"><Label className="text-xs">How many</Label><Input type="number" min={1} value={qty} onChange={(e) => setQty(e.target.value)} className="h-10" /></div>
        <div className="space-y-1"><Label className="text-xs">Sold for, each ($)</Label><Input inputMode="decimal" placeholder="optional" value={price} onChange={(e) => setPrice(e.target.value)} className="h-10" /></div>
        <div className="space-y-1"><Label className="text-xs">Date sold</Label><Input type="date" value={soldOn} onChange={(e) => setSoldOn(e.target.value)} className="h-10" /></div>
        <div className="space-y-1"><Label className="text-xs">Order #</Label><Input placeholder="optional" value={ref} onChange={(e) => setRef(e.target.value)} className="h-10" /></div>
      </div>
      <Input placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} className="h-10" />
      <div className="flex flex-wrap items-center gap-2">
        <Button onClick={save} disabled={busy || !channel || !Number(qty)}><Check className="mr-1.5 h-4 w-4" />Save</Button>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
        {channel === 'ebay' && <a className="text-xs underline" target="_blank" rel="noreferrer" href={`https://www.ebay.com/sh/ord?search=${q}`}>Find it in your eBay orders</a>}
      </div>
      <p className="text-xs text-muted-foreground">Saving records it as received and sold, at the cost from the Amazon order, with the price for profit by platform. Undo takes it all back.</p>
    </div>
  );
}

function SentBackForm({ it, onDone, onCancel }: { it: Item; onDone: () => void; onCancel: () => void }) {
  // What this form was opened against: if the other person's saves land first, the save gets a 409, not a total past expected.
  const [seen0] = useState(() => seenOf(it));
  const toast = useToast();
  const [qty, setQty] = useState('1');
  const [reason, setReason] = useState('');
  const [state, setState] = useState('requested');
  const [refund, setRefund] = useState('');
  const [note, setNote] = useState('');
  const save = async () => {
    try {
      await post(`/api/audit/${it.id}/sent-back`, { qty: Number(qty), reason, state, refund_cents: state === 'refunded' ? centsFromInput(refund) : null, note, seen: seen0 });
      toast(state === 'refunded' ? 'Saved as sent back and refunded' : 'Saved. The refund owed shows on Returns.');
      onDone();
    } catch (e) { if ((e as { status?: number }).status === 409) onCancel(); toast((e as Error).message, { tone: 'error' }); }
  };
  return (
    <div className="space-y-3 rounded-lg border bg-muted/30 p-3">
      {it.seller_returns > 0 && <p className="text-sm">Returns already lists {plural(it.seller_returns, 'unit')} of this going back (from Amazon). Saving counts those first, so nothing is doubled.</p>}
      <div className="grid gap-3 sm:grid-cols-[100px_1fr]">
        <div className="space-y-1"><Label className="text-xs">How many</Label><Input type="number" min={1} value={qty} onChange={(e) => setQty(e.target.value)} className="h-10" /></div>
        <div className="space-y-1">
          <Label className="text-xs">Why</Label>
          <div className="flex flex-wrap gap-1.5">
            {RETURN_REASONS.map((x) => <Button key={x} type="button" size="sm" className="h-8" variant={reason === x ? 'default' : 'outline'} onClick={() => setReason(x)}>{x}</Button>)}
          </div>
        </div>
      </div>
      <div className="space-y-1">
        <Label className="text-xs">Refund</Label>
        <div className="flex flex-wrap items-center gap-1.5">
          {[['requested', 'Not yet'], ['refunded', 'Refunded'], ['no_refund', 'None coming']].map(([k, label]) => (
            <Button key={k} type="button" size="sm" className="h-8" variant={state === k ? 'default' : 'outline'} onClick={() => setState(k!)}>{label}</Button>
          ))}
          {state === 'refunded' && <Input inputMode="decimal" placeholder="$ amount" value={refund} onChange={(e) => setRefund(e.target.value)} className="h-8 w-28" />}
        </div>
      </div>
      <Input placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} className="h-10" />
      <div className="flex gap-2">
        <Button onClick={save} disabled={!reason || !Number(qty)}><Check className="mr-1.5 h-4 w-4" />Save</Button>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  );
}

function CloseForm({ it, onDone, onCancel }: { it: Item; onDone: () => void; onCancel: () => void }) {
  // What this form was opened against: if the other person's saves land first, the save gets a 409, not a total past expected.
  const [seen0] = useState(() => seenOf(it));
  const toast = useToast();
  const [reason, setReason] = useState('not_found');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const left = it.expected - it.accounted;
  const save = async () => {
    setBusy(true);
    try {
      await post<Item>(`/api/audit/${it.id}/close`, { reason, note, seen: seen0 });
      onDone();
    } catch (e) { if ((e as { status?: number }).status === 409) onCancel(); toast((e as Error).message, { tone: 'error' }); } finally { setBusy(false); }
  };
  return (
    <div className="space-y-3 rounded-lg border bg-muted/30 p-3">
      <div className="text-sm font-medium">{it.accounted ? `Mark done with ${left} missing` : 'None here'}: why?</div>
      <div className="flex flex-wrap gap-2">
        {REASONS.map((r) => (
          <Button key={r.key} size="sm" variant={reason === r.key ? 'default' : 'outline'} onClick={() => setReason(r.key)}>{r.label}</Button>
        ))}
      </div>
      <Input placeholder="Note (optional): where you looked" value={note} onChange={(e) => setNote(e.target.value)} className="h-10" />
      <div className="flex flex-wrap gap-2">
        <Button disabled={busy} onClick={save}><Check className="mr-1.5 h-4 w-4" />Save</Button>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  );
}

// The page loads every shown card's saves in one request (Audit, /api/audit/history). One request per card queued
// 40 at a time and again on each refresh, and Inventory and Orders waited behind them (Alex 11-12 1:05 PM).
function History({ it, history, onChange }: { it: Item; history?: HistoryRow[]; onChange: () => void }) {
  const toast = useToast();
  const [all, setAll] = useState(false);
  const rows = (history ?? []).filter((h) => h.kind !== 'reopen');
  if (!rows.length) return null;
  const undo = async (h: HistoryRow) => {
    try {
      if (h.outcome_id) await post(`/api/audit/${it.id}/undo-outcome/${h.outcome_id}`);
      else await post(`/api/audit/${it.id}/undo/${h.id}`);
      toast('Undone.'); onChange();
    } catch (e) { toast((e as Error).message, { tone: 'error' }); }
  };
  const text = (h: HistoryRow) => {
    if (h.kind === 'found') return `Found ${h.qty} in ${h.box_id ? `Box ${h.box_id}` : h.location_name || 'a place'}`;
    if (h.kind === 'sold') return `Sold ${h.qty} on ${h.channel_label}${h.price_cents != null ? ` for ${money(h.price_cents)} each` : ''}${h.sold_on ? ` (${h.sold_on})` : ''}`;
    if (h.kind === 'returned') return `Sent ${h.qty} back to the seller${h.reason ? `: ${h.reason}` : ''}`;
    return `Marked: ${h.reason}`;
  };
  return (
    <div className="space-y-1 text-xs text-muted-foreground">
      {rows.slice(0, all ? rows.length : 6).map((h) => (
        <div key={`${h.kind}${h.id}`} className={cn('flex flex-wrap items-center gap-x-2', h.undone && 'line-through opacity-60')}>
          <span>{text(h)}</span>
          {h.kind === 'sold' && h.order_ref && <span className="font-mono">{h.order_ref}</span>}
          {h.note && <span>“{h.note}”</span>}
          <span>{when(h.at)}</span>
          {h.kind !== 'close' && !h.undone && <button className="underline" onClick={() => undo(h)}><Undo2 className="mr-0.5 inline h-3 w-3" />Undo</button>}
        </div>
      ))}
      {!all && rows.length > 6 && <button className="underline" onClick={() => setAll(true)}>Show all {rows.length} saves (each has its own Undo)</button>}
    </div>
  );
}

function AuditCard({ it, history, onChange }: { it: Item; history?: HistoryRow[]; onChange: () => void }) {
  const toast = useToast();
  const [mode, setMode] = useState<'none' | 'found' | 'sold' | 'back' | 'close' | 'listed'>('none');
  const [more, setMore] = useState(false);
  const [showOrders, setShowOrders] = useState(false);
  const st = STATUS[it.status];
  const done = () => { setMode('none'); onChange(); };
  const reopen = async () => {
    try { await post(`/api/audit/${it.id}/reopen`, { seen: seenOf(it) }); } catch (e) { toast((e as Error).message, { tone: 'error' }); }
    onChange();
  };
  // Store-order rows (no ASIN) link to the store's search instead of Amazon.
  const page = it.line_id ? `https://www.google.com/search?q=${encodeURIComponent(`${it.store} ${it.title}`)}` : `https://www.amazon.com/dp/${it.asin}`;
  return (
    <Card className={cn(it.status === 'found' && 'opacity-80')}>
      <CardContent className="space-y-3 p-4">
        <div className="flex gap-3">
          {/* Alex 11-11: photo and title open the item in Boxloft; with no product yet, its Amazon (or store) page. */}
          {it.product_id ? (
            <Link to={`/inventory/${it.product_id}`} className="shrink-0">
              {it.image_url ? <Thumb photoId={null} url={it.image_url} size="focus" />
                : <div className="h-20 w-20 rounded-md bg-muted sm:h-24 sm:w-24" />}
            </Link>
          ) : (
            <a href={page} target="_blank" rel="noreferrer" className="shrink-0">
              {it.image_url ? <Thumb photoId={null} url={it.image_url} size="focus" />
                : <div className="h-20 w-20 rounded-md bg-muted sm:h-24 sm:w-24" />}
            </a>
          )}
          <div className="min-w-0 flex-1 space-y-1">
            {it.product_id
              ? <Link to={`/inventory/${it.product_id}`} className="line-clamp-2 text-sm font-medium hover:underline">{it.title || it.asin}</Link>
              : <a href={page} target="_blank" rel="noreferrer" className="line-clamp-2 text-sm font-medium hover:underline">{it.title || it.asin}</a>}
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
              {it.line_id && <span className="font-medium text-foreground">Bought at {it.store}</span>}
              {/* A store row a person matched to a listing goes by that listing's ASIN (match_asin) */}
              {(!it.line_id || it.match_asin) && <>
              <button onClick={() => { void navigator.clipboard?.writeText(it.match_asin); toast(`Copied ${it.match_asin}`); }} className="inline-flex items-center gap-1 font-mono text-foreground hover:underline"><Copy className="h-3 w-3" />{it.match_asin}</button>
              {it.brand && <span>{it.brand}</span>}
              </>}
              {it.product_id && <Link to={`/inventory/${it.product_id}`} className="hover:underline">In Boxloft</Link>}
            </div>
            <div className="flex items-center gap-2 sm:hidden"><Badge variant="outline" className={st.cls}>{st.label}</Badge><span className="text-sm"><b>{it.accounted}</b> / {it.expected}</span></div>
            <div className="text-sm"><b>{it.expected}</b> should be here <span className="text-muted-foreground">({countLine(it)})</span></div>
            {it.expected > 0 && where(it) && <div className="text-xs text-muted-foreground">{where(it)}</div>}
            {/* Alex 11-12 (Crocs): today's sale sat on Orders while this card still said "2 sold on Amazon" */}
            {(it.open_sales ?? []).map((o) => (
              <div key={o.line_id} className="rounded-md border border-warning/50 bg-warning/10 px-2 py-1 text-xs">
                <b>Sold {o.qty} on {o.channel === 'amazon' ? 'Amazon' : o.channel} {o.ordered_at}, not shipped yet</b>
                {' '}(order <span className="font-mono">{o.order_id}</span>).{' '}
                <Link to="/orders" className="underline">Ship it from Orders</Link>
              </div>
            ))}
            <div className="text-xs text-muted-foreground">
              {money(it.avg_cost_cents)} each · {money(it.cost_cents)} total · bought {it.first_buy === it.last_buy ? it.first_buy : `${it.first_buy} to ${it.last_buy}`} ·{' '}
              <button className="underline" onClick={() => setShowOrders(!showOrders)}>{plural(it.orders.length, 'order')} and links</button>
            </div>
            {showOrders && (
              <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs">
                {(!it.line_id || it.match_asin) && <a href={`https://www.amazon.com/dp/${it.match_asin}`} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 hover:underline">Amazon<ExternalLink className="h-3 w-3" /></a>}
                {(!it.line_id || it.match_asin) && <a href={`https://keepa.com/#!product/1-${it.match_asin}`} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 hover:underline">Keepa<ExternalLink className="h-3 w-3" /></a>}
                <a href={`https://www.ebay.com/sch/i.html?_nkw=${encodeURIComponent(ebayQuery(it.title, []))}&LH_Sold=1&LH_Complete=1&_sop=13`} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 hover:underline">eBay sold<ExternalLink className="h-3 w-3" /></a>
                {it.orders.map((o) => (
                  <a key={o.id} href={`https://www.amazon.com/your-orders/order-details?orderID=${o.id}`} target="_blank" rel="noreferrer" className="font-mono hover:underline">{o.id} ({o.date}, {o.units}{o.cut ? ` of ${o.units_snapshot}, ${o.cut} cancelled` : ''}{o.delivered ? `, delivered ${o.delivered_on}` : ''}{!o.delivered && o.amazon === 'delivered' ? `, delivered${o.amazon_on ? ` ${o.amazon_on}` : ''}` : ''}{o.amazon === 'returned' ? ', returned' : ''}{o.amazon === 'shipped' || o.amazon === 'ordered' ? ', not delivered yet' : ''}{o.cancelled ? ', cancelled' : ''})</a>
                ))}
              </div>
            )}
          </div>
          <div className="hidden shrink-0 flex-col items-end gap-1 text-right sm:flex">
            <Badge variant="outline" className={st.cls}>{st.label}</Badge>
            <div className="text-sm"><b>{it.accounted}</b> / {it.expected}</div>
            {(it.sold > 0 || it.sent_back > 0 || it.received > 0) && <div className="text-xs text-muted-foreground">{[it.found && `${it.found} here`, it.received && `${it.received} checked in on Receive`, it.sold && `${it.sold} sold elsewhere`, it.sent_back && `${it.sent_back} sent back`].filter(Boolean).join(' · ')}</div>}
          </div>
        </div>
        {it.closed && <div className="text-xs text-muted-foreground">{it.close_reason}{it.close_note && `: “${it.close_note}”`}</div>}
        <ListedLine it={it} onChange={onChange} />
        <History it={it} history={history} onChange={onChange} />
        {mode === 'found' && <FoundForm it={it} onDone={done} onCancel={() => setMode('none')} />}
        {mode === 'sold' && <SoldForm it={it} onDone={done} onCancel={() => setMode('none')} />}
        {mode === 'back' && <SentBackForm it={it} onDone={done} onCancel={() => setMode('none')} />}
        {mode === 'close' && <CloseForm it={it} onDone={done} onCancel={() => setMode('none')} />}
        {mode === 'listed' && <ListedForm it={it} onDone={done} onCancel={() => setMode('none')} />}
        {mode === 'none' && (
          <div className="flex flex-wrap gap-2">
            {!it.closed && <Button size="sm" className="h-10" onClick={() => setMode('found')}><PackagePlus className="mr-1.5 h-4 w-4" />{it.found ? 'Found more' : 'Found it'}</Button>}
            {!it.closed && it.accounted < it.expected && <Button size="sm" variant="outline" className="h-10" onClick={() => setMode('close')}><SearchX className="mr-1.5 h-4 w-4" />{it.accounted ? 'Rest missing' : 'None here'}</Button>}
            {it.closed && <Button size="sm" variant="outline" className="h-10" onClick={reopen}><RotateCcw className="mr-1.5 h-4 w-4" />Reopen</Button>}
            {!more && <Button size="sm" variant="ghost" className="h-10" onClick={() => setMore(true)}>Other answers</Button>}
            {more && !it.closed && it.accounted < it.expected && <Button size="sm" variant="ghost" className="h-10" onClick={() => setMode('sold')}><ShoppingCart className="mr-1.5 h-4 w-4" />Sold elsewhere</Button>}
            {more && !it.closed && it.accounted < it.expected && <Button size="sm" variant="ghost" className="h-10" onClick={() => setMode('back')}><Undo2 className="mr-1.5 h-4 w-4" />Sent back</Button>}
            {more && <Button size="sm" variant="ghost" className="h-10" onClick={() => setMode('listed')}><Store className="mr-1.5 h-4 w-4" />Mark listed</Button>}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

const PLATFORMS: [string, string][] = [['amazon_fbm', 'Amazon FBM'], ['amazon_fba', 'Amazon FBA'], ['ebay', 'eBay'], ['walmart', 'Walmart'],
  ['mercari', 'Mercari'], ['whatnot', 'Whatnot'], ['facebook', 'Facebook Marketplace'], ['tcgplayer', 'TCGplayer'], ['poshmark', 'Poshmark'], ['other', 'Other']];

function ListedLine({ it, onChange }: { it: Item; onChange: () => void }) {
  const toast = useToast();
  const rows = it.listed ?? [];
  if (!rows.length) return null;
  const send = async (body: object, msg: string) => {
    try { await post(`/api/audit/${it.id}/listing`, { ...body, seen_mark_seq: seenMarks(it) }); toast(msg); onChange(); } catch (e) { toast((e as Error).message, { tone: 'error' }); }
  };
  const undo = async (markId: number) => {
    try { await post(`/api/audit/${it.id}/listing/${markId}/undo`, { seen_mark_seq: seenMarks(it) }); onChange(); } catch (e) { toast((e as Error).message, { tone: 'error' }); }
  };
  return (
    <div className="space-y-1.5 rounded-md border px-3 py-2 text-sm">
      {rows.map((x) => {
        const live = listedNow(x);
        const bits = x.state === 'manual'
          ? [`${x.qty} listed`, x.note]
          : [live ? `${x.qty} listed` : x.status === 'Active' ? '0 in stock' : x.status, x.price_cents != null && money(x.price_cents),
             x.fba_inbound ? `${x.fba_inbound} on the way to Amazon` : '', x.sku && `SKU ${x.sku}`];
        const byName = x.match?.startsWith('title');
        if (x.state === 'suggested') {   // Alex 11-12: the link offer did not look tappable. A green box, the question, 2 real buttons.
          return (
            <div key={`s${x.listing_id ?? ''}`} className="space-y-2 rounded-lg border-2 border-[hsl(var(--tap-border))] bg-[hsl(var(--tap))] p-2.5">
              <div className="text-sm font-medium">Is this its {x.label} listing?</div>
              <div className="text-xs">
                {[live ? `${x.qty} listed` : x.status === 'Active' ? '0 in stock' : x.status, x.price_cents != null && money(x.price_cents), x.sku && `SKU ${x.sku}`].filter(Boolean).join(' · ')}
                {' · '}{byName ? `similar name: ${x.title?.slice(0, 70)}` : x.match === 'ASIN' ? 'same ASIN' : `same ${x.match}`}
                {x.url && <> · <a href={x.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 underline">see it<ExternalLink className="h-3 w-3" /></a></>}
              </div>
              <div className="flex gap-2">
                <Button className="h-10 flex-1" onClick={() => send({ kind: 'confirm', listing_id: x.listing_id }, `Linked to its ${x.label} listing`)}><Check className="mr-1 h-4 w-4" />That's it: link</Button>
                <Button variant="outline" className="h-10" onClick={() => send({ kind: 'not_this', listing_id: x.listing_id }, 'Hidden for this item')}><X className="mr-1 h-4 w-4" />Not this</Button>
              </div>
            </div>
          );
        }
        return (
          <div key={`${x.state}${x.listing_id ?? ''}-${x.mark_id ?? ''}`} className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <Badge variant="outline" className={cn('border-success/50 text-success-ink', !live && 'opacity-60')}>{x.label}</Badge>
            <span className={cn(!live && 'text-muted-foreground')}>{bits.filter(Boolean).join(' · ')}</span>
            {x.url && <a href={x.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 text-xs underline">listing<ExternalLink className="h-3 w-3" /></a>}
            <span className="ml-auto flex gap-1">
              {x.mark_id && <Button size="sm" variant="ghost" className="h-7 px-2 text-xs" onClick={() => undo(x.mark_id!)}>Undo</Button>}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function ListedForm({ it, onDone, onCancel }: { it: Item; onDone: () => void; onCancel: () => void }) {
  const toast = useToast();
  const [platform, setPlatform] = useState('ebay');
  const [qty, setQty] = useState(String(Math.max(1, it.expected - it.sold - it.sent_back)));
  const [url, setUrl] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try {
      await post(`/api/audit/${it.id}/listing`, { kind: 'manual', platform, qty: Number(qty), url: url.trim(), note, seen_mark_seq: seenMarks(it) });
      toast(`Marked listed on ${PLATFORMS.find((p) => p[0] === platform)?.[1]}`);
      onDone();
    } catch (e) { toast((e as Error).message, { tone: 'error' }); } finally { setBusy(false); }
  };
  return (
    <div className="space-y-3 rounded-lg border bg-muted/30 p-3">
      <div className="grid gap-3 sm:grid-cols-[1fr_120px]">
        <div className="space-y-1">
          <Label>Listed on</Label>
          <Select value={platform} onValueChange={setPlatform}>
            <SelectTrigger className="h-10 w-full"><SelectValue /></SelectTrigger>
            <SelectContent>{PLATFORMS.map(([k, l]) => <SelectItem key={k} value={k}>{l}</SelectItem>)}</SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label htmlFor={`lq${it.id}`}>How many</Label>
          <Input id={`lq${it.id}`} type="number" inputMode="numeric" min={1} value={qty} onChange={(e) => setQty(e.target.value)} className="h-10" />
        </div>
      </div>
      <Input placeholder="Link to the listing (optional)" value={url} onChange={(e) => setUrl(e.target.value)} className="h-10" />
      <Input placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} className="h-10" />
      <div className="flex gap-2">
        <Button onClick={save} disabled={busy || !Number(qty)}><Check className="mr-1.5 h-4 w-4" />Save</Button>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
      <p className="text-xs text-muted-foreground">This notes where it's for sale. It doesn't move stock. Amazon listings show up on their own: Boxloft reads them every hour.</p>
    </div>
  );
}

export function Audit() {
  const qc = useQueryClient();
  const [tab, setTab] = useState('open');
  const [q, setQ] = useState(useQueryParam('q') ?? '');
  const [limit, setLimit] = useState(PAGE);
  const res = useQuery({ queryKey: ['audit'], queryFn: () => get<{ items: Item[]; summary: Summary }>('/api/audit') });
  const s = res.data?.summary;
  const out = useQuery({ queryKey: ['audit', 'outcomes', s?.accounted_units], queryFn: () => get<Outcomes>('/api/audit/outcomes'), enabled: !!s });
  const items = useMemo(() => {
    const t = TABS.find((x) => x.key === tab)!;
    const words = q.trim().toLowerCase().split(/\s+/).filter(Boolean);
    return (res.data?.items ?? []).filter((it) => t.match(it) &&
      words.every((w) => `${it.title} ${it.asin} ${it.brand} ${it.store} ${it.orders.map((o) => o.id).join(' ')}`.toLowerCase().includes(w)));
  }, [res.data, tab, q]);
  const ids = items.slice(0, limit).map((it) => it.id).join(',');
  const hist = useQuery({ queryKey: ['audit', 'history', ids], queryFn: () => get<{ histories: Record<string, HistoryRow[]> }>(`/api/audit/history?ids=${ids}`),
                          enabled: !!ids, placeholderData: (prev) => prev });
  // A save here changes stock and products, so Listings (on our shelves) and Orders (pick from) reload too.
  const refresh = () => { for (const k of ['audit', 'listings', 'orders', 'audit-target', 'audit-extras']) void qc.invalidateQueries({ queryKey: [k] }); };
  const counts = (key: string) => (res.data ? res.data.items.filter((it) => TABS.find((x) => x.key === key)!.match(it)).length : undefined);
  return (
    <div className="max-w-5xl space-y-4">
      <PageHeader title="Stock audit" subtitle="Find each item: where it is, or where it went. Biggest cost first." about="Everything bought in the last 24 months that hadn't sold on Amazon, plus Amazon orders delivered over 5 days ago and not scanned in yet." />
      <SortBar onChange={refresh} />
      {s && (
        <details className="group rounded-xl border bg-card">
          <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2 px-4 text-sm [&::-webkit-details-marker]:hidden">
            <b>{s.checked}</b> of {s.items} items checked · {s.accounted_units.toLocaleString()} of {s.units.toLocaleString()} units
            <span className="ml-auto text-xs text-muted-foreground underline group-open:hidden">Totals</span>
          </summary>
          <div className="space-y-3 px-4 pb-4">
            <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
              <div><div className="text-xs text-muted-foreground">Items checked</div><b className="text-lg">{s.checked}</b> / {s.items}</div>
              <div><div className="text-xs text-muted-foreground">Units accounted for</div><b className="text-lg">{s.accounted_units.toLocaleString()}</b> / {s.units.toLocaleString()}</div>
              <div><div className="text-xs text-muted-foreground">Cost found</div><b className="text-lg">{money(s.found_cents)}</b> / {money(s.cost_cents)}</div>
              <div><div className="text-xs text-muted-foreground">Not found</div><b className="text-lg">{s.by_status.missing + s.by_status.short}</b> items</div>
            </div>
            <Progress value={s.items ? (100 * s.checked) / s.items : 0} />
            <details className="text-xs">
              <summary className="inline-flex min-h-9 cursor-pointer list-none items-center font-medium underline underline-offset-4 [&::-webkit-details-marker]:hidden">About these numbers</summary>
              <div className="space-y-2 text-muted-foreground">
            {s.counted ? (
              <p>
                Boxloft counts each item from its own records: every Amazon buy through {s.counted.buys_through} at the quantity Amazon charged for, plus later orders delivered over 5 days ago, less units sent back to the seller and cancelled orders, less every Amazon sale that nothing here shipped. The oldest buy sells first, and a sale only counts against a buy that had reached us by then. New Amazon sales come off by themselves.
                {s.bought_units != null && ` On this list: ${s.bought_units.toLocaleString()} bought, ${(s.amazon_sold_units ?? 0).toLocaleString()} sold on Amazon${s.returned_before_units ? `, ${s.returned_before_units.toLocaleString()} sent back` : ''}, ${s.units.toLocaleString()} should be here.`}
                {' '}eBay and other sales are not in Boxloft yet, so an item sold there still shows here: use "Sold elsewhere" and pick where.
              </p>
            ) : (
              <p>{s.snapshot}. Amazon sales come off by themselves. eBay and other sales are not in Boxloft yet, so an item sold there still shows here: use "Sold elsewhere" and pick where.</p>
            )}
            <p>"Delivered" dates and "not delivered yet" come from Amazon's order pages, read by the sync on the house computer. Older orders, bought before the sync read them, have no delivery record in Boxloft.</p>
            {s.rows_for_sale != null && (
              <p>
                For sale right now: {kinds(s.rows_for_sale)} on this list (an active Amazon listing with stock, or marked by hand).{' '}
                {s.rows_for_sale_confirmed ?? 0} confirmed by a person, {s.rows_for_sale - (s.rows_for_sale_confirmed ?? 0)} matched by Amazon and not checked yet. Tab "For sale now" shows them.{' '}
                <Link to="/listings" className="underline">See all listings</Link>
              </p>
            )}
              </div>
            </details>
            {out.data && <WhereItWent o={out.data} />}
          </div>
        </details>
      )}
      <Chips items={TABS.map((t) => ({ key: t.key, label: t.label, count: counts(t.key) }))} value={tab} onChange={(k) => { setTab(k); setLimit(PAGE); }} />
      <SearchInput placeholder="Search name, ASIN, brand or order #" value={q} onChange={(e) => { setQ(e.target.value); setLimit(PAGE); }} />
      {res.isLoading && <Empty>Loading…</Empty>}
      {res.data && !items.length && <Empty>{tab === 'open' && !q ? 'Everything is checked.' : 'Nothing here.'}</Empty>}
      <div className="space-y-3">
        {items.slice(0, limit).map((it) => <AuditCard key={it.id} it={it} history={hist.data?.histories[it.id]} onChange={refresh} />)}
      </div>
      {items.length > limit && <Button variant="outline" className="w-full" onClick={() => setLimit(limit + PAGE)}>Show {Math.min(PAGE, items.length - limit)} more of {items.length - limit}</Button>}
    </div>
  );
}

function WhereItWent({ o }: { o: Outcomes }) {
  const label: Record<string, string> = { in_stock: 'On our shelves', sold_elsewhere: 'Sold', sent_back: 'Sent back to seller' };
  const rows = [
    ...o.outcomes.map((r) => ({
      key: r.outcome + r.channel, what: r.outcome === 'sold_elsewhere' ? `Sold on ${r.channel_label}` : label[r.outcome] ?? r.outcome,
      units: r.units, cost: r.cost_cents,
      extra: r.sales_cents != null ? `${money(r.sales_cents)} sales on ${r.priced_units} priced, ${money(r.sales_cents - (r.priced_cost_cents ?? 0))} over cost` : '',
    })),
    ...Object.entries(o.closed).map(([k, v]) => ({ key: k, what: k, units: v.units, cost: v.cost_cents, extra: '' })),
  ];
  if (!rows.length) return null;
  return (
    <div className="space-y-1 border-t pt-3 text-sm">
      <div className="text-xs font-medium text-muted-foreground">Where it went so far</div>
      {rows.map((r) => (
        <div key={r.key} className="flex flex-wrap justify-between gap-x-3">
          <span>{r.what}</span>
          <span className="tabular-nums text-muted-foreground">{plural(r.units, 'unit')} · {money(r.cost)} cost{r.extra && ` · ${r.extra}`}</span>
        </div>
      ))}
    </div>
  );
}
