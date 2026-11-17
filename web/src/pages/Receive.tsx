import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, Plus, Printer, ReceiptText, ScanLine } from 'lucide-react';
import { get, post } from '@/api/client';
import type { BoxRow, Holder, PhotoMatch, Product, ProductRow, PurchaseLine, ScanResult } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent } from '@/components/ui/card';
import { HolderPicker, PageHeader, SearchInput, Thumb } from '@/components/app/common';
import { MarketPicker } from '@/components/app/MarketPicker';
import { AmazonNote, isABox, type AmazonOk } from '@/components/app/AmazonNote';
import { BuyMatchList, PhotoStrip } from '@/pages/Product';
import { useToast } from '@/components/app/toast';
import { navigate, useQueryParam } from '@/lib/router';
import { receiveLine } from '@/lib/receive';
import { useScan, useScanBus } from '@/lib/scan';
import { centsFromInput, day, dollarsForInput, money, plural } from '@/lib/format';
import { cn } from '@/lib/utils';
import { isFresh, readCurrentBox } from '@/lib/currentBox';
import { boxName } from '@/lib/places';
import { boxLabel, printLabels, unitLabel } from '@/lib/labels';

const LAST_BOX = 'boxloft-receive-to';

/** The box the last receipt went into, and for which product. It is filled in again only for that same
 *  product (10 more of the same item); any other product starts with no box, so nothing lands in the wrong one. */
interface LastBox { holder: Holder; name: string; product_id: number | null }
function readLast(): LastBox | null {
  try {
    const v = JSON.parse(localStorage.getItem(LAST_BOX) || 'null');
    if (!v) return null;
    if (v.holder) return v as LastBox;
    return { holder: v, name: v.box_id ? `Box ${v.box_id}` : 'the last place', product_id: null };   // saved by an older version
  } catch { return null; }
}
const sameHolder = (a: Holder | null, b: Holder | null) => !!a && !!b && JSON.stringify(a) === JSON.stringify(b);

/* Alex 11-11 2:44 PM "when we scan a box after scanning a product": the item's scan was still on the network (or
 * still making the product from its order), so the search screen was the page that got the box label, and it said
 * "Scan the product barcode first" and dropped the box. Now a box or shelf scanned before the item's form is open
 * is held here and fills "Into which box" when the form opens, whichever answer comes back first. It lives in this
 * module (not the page's state), so it also waits through a detour to New product. It is used once, and it is gone
 * after 10 minutes. */
interface NextPlace { holder: Holder; name: string; at: number }
let nextPlace: NextPlace | null = null;
const NEXT_EVENT = 'boxloft:receive-next-place';
const NEXT_MS = 10 * 60_000;
function setNextPlace(v: NextPlace | null) {
  nextPlace = v;
  window.dispatchEvent(new Event(NEXT_EVENT));
}
function useNextPlace(): NextPlace | null {
  const [, bump] = useState(0);
  useEffect(() => {
    const f = () => bump((n) => n + 1);
    window.addEventListener(NEXT_EVENT, f);
    return () => window.removeEventListener(NEXT_EVENT, f);
  }, []);
  return nextPlace && Date.now() - nextPlace.at < NEXT_MS ? nextPlace : null;
}
/** A box or shelf scan, as a place to hold. null for anything else. */
function placeOf(r: ScanResult): NextPlace | null {
  if (r.kind === 'box') return { holder: { box_id: r.box.id }, name: boxName(r.box), at: Date.now() };
  if (r.kind === 'location') return { holder: { location_id: r.location.id }, name: r.location.code, at: Date.now() };
  return null;
}

const GROUP_SHOWN = 4;

/** Open purchase lines that match the words. Lines for the same item sit under one name, and each order
 *  shows what tells it apart: the day it was bought, the order number, how many are still to come, the cost. */
function PurchaseFinder({ seed }: { seed: string }) {
  const toast = useToast();
  const [own, setOwn] = useState('');
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  const q = own.trim().length > 1 ? own.trim() : seed.trim();
  const res = useQuery({
    queryKey: ['purchases', 'find', q],
    queryFn: () => get<{ lines: PurchaseLine[] }>(`/api/purchases?status=open&q=${encodeURIComponent(q)}&limit=80`),
    enabled: q.length > 1,
  });
  const groups = useMemo(() => {
    const m = new Map<string, PurchaseLine[]>();
    for (const l of res.data?.lines ?? []) {
      const key = l.asin || l.upc || l.title.toLowerCase();
      m.set(key, [...(m.get(key) ?? []), l]);
    }
    return [...m.entries()];
  }, [res.data]);
  const left = (l: PurchaseLine) => l.left_to_receive ?? l.qty - l.received;
  const pick = async (l: PurchaseLine) => { setBusy(true); await receiveLine(l, toast); setBusy(false); };
  return (
    <div className="space-y-2 border-t pt-3">
      <div className="flex items-center gap-2 text-sm font-medium"><ReceiptText className="h-4 w-4" /> No barcode? Find it in your purchases</div>
      <SearchInput placeholder="Words from the item name, or the order #" value={own} onChange={(e) => setOwn(e.target.value)} />
      {q.length > 1 && res.data && (groups.length ? (
        <p className="text-xs text-muted-foreground">Purchases that match "{q}" and are not all received. Tap the order your item came from.</p>
      ) : (
        <p className="text-sm text-muted-foreground">No purchase waiting to be received matches "{q}". Try one word from the name, or the order number.</p>
      ))}
      {groups.map(([key, lines]) => {
        const shown = open[key] ? lines : lines.slice(0, GROUP_SHOWN);
        return (
          <div key={key} className="overflow-hidden rounded-lg border">
            <div className="bg-muted/40 px-3 py-2">
              <div className="line-clamp-3 text-sm font-medium">{lines[0]!.title}</div>
              {/* The store may have renamed the item between orders ("128 K-Cup" became "32 Count Boxes (Pack of 4)"). */}
              {[...new Set(lines.map((l) => l.title))].slice(1, 3).map((t) => <div key={t} className="line-clamp-2 text-xs text-muted-foreground">Also bought as: {t}</div>)}
              <div className="text-xs text-muted-foreground">{plural(lines.length, 'order')} · {lines.reduce((n, l) => n + left(l), 0)} still to receive{lines[0]!.asin ? ` · ${lines[0]!.asin}` : ''}</div>
            </div>
            <div className="divide-y">
              {shown.map((l) => (
                <button key={l.id} type="button" disabled={busy} onClick={() => pick(l)} className="flex w-full items-center justify-between gap-3 px-3 py-2.5 text-left text-sm hover:bg-muted/50">
                  <span className="min-w-0">
                    <span className="block font-medium">Bought {day(l.ordered_at)}</span>
                    <span className="block text-xs text-muted-foreground"><span className="capitalize">{l.source}</span> order <span className="whitespace-nowrap font-mono">{l.order_id}</span></span>
                  </span>
                  <span className="shrink-0 text-right tabular-nums">
                    <span className="block font-medium">{left(l)} to receive</span>
                    <span className="block text-xs text-muted-foreground">{l.received ? `${l.received} of ${l.qty} in · ` : ''}{money(l.unit_cost_cents)} each</span>
                  </span>
                </button>
              ))}
            </div>
            {lines.length > shown.length && <button type="button" className="w-full border-t px-3 py-2 text-left text-xs underline" onClick={() => setOpen({ ...open, [key]: true })}>Show {lines.length - shown.length} older orders of this item</button>}
          </div>
        );
      })}
    </div>
  );
}

function ProductFinder({ onPick }: { onPick: (id: number) => void }) {
  const [q, setQ] = useState('');
  const toast = useToast();
  const { feedback } = useScanBus();
  const res = useQuery({
    queryKey: ['products', 'find', q],
    queryFn: () => get<{ products: ProductRow[] }>(`/api/products?q=${encodeURIComponent(q)}`),
    enabled: q.trim().length > 1,
  });
  const next = useNextPlace();
  const opening = useRef(0);   // item scans still being looked up (or still making the product from its order)
  useScan(async (code) => {
    let r: ScanResult;
    opening.current += 1;
    try { r = await get<ScanResult>(`/api/scan?code=${encodeURIComponent(code)}`); } catch (e) {
      opening.current -= 1;
      feedback(false, 'Scan failed: check the signal, then scan again');
      toast(e instanceof Error ? e.message : 'Scan failed', { tone: 'error' });
      return;
    }
    const place = placeOf(r);
    if (place) {
      // A box or shelf: held for the item, not refused. Scanned while the item is opening, it is that item's box.
      opening.current -= 1;
      setNextPlace(place);
      if (opening.current > 0) feedback(true, `Into ${place.name}`);
      else feedback('next', `${place.name} is set. Now scan the item`);
      return;
    }
    try {
      // The next step is the receive form (or a pick from the list): close the camera so it shows.
      if (r.kind === 'product' && r.products.length === 1) { feedback(true, r.products[0]!.title.slice(0, 60), true); onPick(r.products[0]!.id); }
      else if (r.kind === 'product') { feedback(true, 'Several products match: pick one', true); setQ(r.value); }
      else if (r.kind === 'unknown' && r.lines?.length) {
        feedback(true, `On order: ${r.lines[0]!.title.slice(0, 50)}`);
        toast(`On order ${r.lines[0]!.order_id}: ${r.lines[0]!.title.slice(0, 60)}`);
        await receiveLine(r.lines[0]!, toast);
      }
      else if (r.kind === 'unknown' && r.value) { feedback(true); navigate(`/inventory/new?return=receive&code=${encodeURIComponent(r.value)}`); toast('New code. Create the product first.'); }
      else { feedback(false, r.kind === 'bad_check' || r.kind === 'unknown' ? r.message : 'Scan the item\'s barcode, or a box'); toast(r.kind === 'bad_check' || r.kind === 'unknown' ? r.message : 'That is a unit label. Scan the item\'s barcode (UPC, FNSKU), or the box it goes into.', { tone: 'error' }); }
    } finally {
      opening.current -= 1;
    }
  });
  const none = q.trim().length > 1 && !!res.data && !res.data.products.length;
  return (
    <Card><CardContent className="space-y-3 p-4 sm:p-6">
      <div className="flex items-center gap-2 text-sm font-medium"><ScanLine className="h-4 w-4" /> Scan the product's barcode, or search</div>
      {next && (
        <div className="flex items-center gap-2 rounded-md border border-primary bg-primary/5 px-3 py-2 text-sm">
          <span className="min-w-0 flex-1">Into <b>{next.name}</b>. Now scan or find the item.</span>
          <button type="button" className="text-sm underline" onClick={() => setNextPlace(null)}>Clear</button>
        </div>
      )}
      <SearchInput autoFocus placeholder="Title, SKU, UPC, FNSKU..." value={q} onChange={(e) => setQ(e.target.value)} />
      <div className="divide-y rounded-lg border empty:hidden">
        {(res.data?.products ?? []).slice(0, 12).map((p) => (
          <button key={p.id} className="flex w-full items-center gap-3 p-3 text-left hover:bg-muted/50" onClick={() => onPick(p.id)}>
            <Thumb photoId={p.photo_id} />
            <div className="min-w-0 flex-1"><div className="truncate text-sm font-medium">{p.title}</div><div className="font-mono text-xs text-muted-foreground">{p.sku} · {p.owner}</div></div>
            <div className="text-sm tabular-nums">{p.on_hand} on hand</div>
          </button>
        ))}
      </div>
      {none && (
        <div className="rounded-lg border border-dashed p-3 text-sm">
          <b>Nothing in Boxloft is named "{q.trim()}" yet.</b> That is normal for an item nobody has received before.
          If you bought it online, pick its order from the purchases below. If it is not there, tap New product.
        </div>
      )}
      {/* Nothing matched: the same words search the purchases, so the next step is already on the screen. */}
      <PurchaseFinder seed={none ? q : ''} />
      <Button variant="outline" onClick={() => navigate(`/inventory/new?return=receive${q ? `&code=${encodeURIComponent(q)}` : ''}`)}><Plus className="mr-1.5 h-4 w-4" />New product (not in your purchases)</Button>
    </CardContent></Card>
  );
}

interface ListedOn { channel: string; name: string; letter: string; state: 'live' | 'fba' | 'inactive' | 'none'; listed: number; listed_fba: number; in_market: number; fills: boolean }
interface ListedRes { channels: ListedOn[]; suggest: string | null; amazon?: AmazonOk }
/** Amazon said no (Alex 11-13): never suggest an A box for it. */
const suggestOf = (d: ListedRes | undefined) => (d?.amazon?.can_sell === false && (d.suggest ?? 'A') === 'A' ? 'E' : d?.suggest ?? null);

/** Alex 11-11: the intake screen says where the item is listed, and whether these units are what that listing sells. */
function ListedNote({ data }: { data: ListedRes | undefined }) {
  if (!data?.channels.length && !data?.amazon) return null;
  return (
    <div className="mb-4 space-y-1.5">
      <AmazonNote a={data.amazon} pre={data.amazon?.can_sell === false} />
      {data.channels.map((c) => {
        if (c.state === 'none') return <p key={c.channel} className="text-sm text-muted-foreground">Not listed on {c.name} yet.</p>;
        const empty = c.state === 'live' && c.in_market === 0;
        return (
          <div key={c.channel} className={cn('rounded-lg border px-3 py-2 text-sm', empty ? 'border-warning/60 bg-warning/15' : 'bg-muted/40')}>
            {c.state === 'live' && <>
              <b>Listed on {c.name}: {c.listed} for sale</b> (you ship), {c.in_market} in {c.name} boxes.{' '}
              {c.fills ? `These units fill that listing. Put them in ${c.letter === 'A' || c.letter === 'E' ? 'an' : 'a'} ${c.letter} box.` : ''}
            </>}
            {c.state === 'fba' && <><b>Listed on {c.name} FBA only.</b> {c.name} holds the {c.listed_fba} for sale. No listing that you ship.</>}
            {c.state === 'inactive' && <><b>On {c.name}, but not for sale now.</b> The listing is inactive or at 0.</>}
          </div>
        );
      })}
    </div>
  );
}

export function Receive() {
  const productParam = useQueryParam('product');
  const pid = productParam ? Number(productParam) : null;
  const lineParam = useQueryParam('line');
  const justMade = useQueryParam('new') === '1';
  const [lineId, setLineId] = useState<number | null>(lineParam ? Number(lineParam) : null);
  const [purchasedAt, setPurchasedAt] = useState<string | null>(null);
  // Alex 11-11: a scan that shows 2 orders of the same item takes both at once. Tapping more orders adds them; the
  // quantity fills the oldest first, and each order keeps its own cost and date.
  const [more, setMore] = useState<number[]>([]);
  const openLines = useQuery({ queryKey: ['purchases', 'open-for', pid], queryFn: () => get<{ lines: PurchaseLine[] }>(`/api/purchases/open?product_id=${pid}`), enabled: !!pid });
  const qc = useQueryClient();
  const toast = useToast();
  const product = useQuery({ queryKey: ['product', pid], queryFn: () => get<Product>(`/api/products/${pid}`), enabled: !!pid });
  const listed = useQuery({ queryKey: ['product-listed', pid], queryFn: () => get<ListedRes>(`/api/products/${pid}/listed`), enabled: !!pid });
  // Alex 11-11 (Dreamcatcher album): a product made from another listing's code finds no order. Offer the buys whose
  // titles look like it; a tap puts that buy's ASIN on the product, and its order shows up below.
  const noOrder = !!pid && openLines.isSuccess && openLines.data.lines.length === 0;
  const alike = useQuery({
    queryKey: ['buy-match', 'product', pid],
    queryFn: () => get<{ matches: PhotoMatch[] }>(`/api/buy-match?product_id=${pid}`),
    enabled: noOrder,
  });
  const [linking, setLinking] = useState(false);
  const linkBuy = async (m: PhotoMatch) => {
    setLinking(true);
    try {
      const r = await post<{ added: { kind: string; value: string }[]; line_id: number | null }>(`/api/products/${pid}/link-buy`, { kind: m.kind, id: m.id });
      const refresh = () => Promise.all([openLines.refetch(), product.refetch(), qc.invalidateQueries({ queryKey: ['buy-match'] })]);
      toast(`Linked to "${m.title.slice(0, 40)}". Pick its order below.`, {
        undo: async () => { await post(`/api/products/${pid}/unlink-buy`, r); await refresh(); },
      });
      await refresh();
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not link it', { tone: 'error' });
    } finally {
      setLinking(false);
    }
  };
  const p = product.data;
  const [qty, setQty] = useState('1');
  const [cost, setCost] = useState('');
  const [supplier, setSupplier] = useState('');
  const [reference, setReference] = useState('');
  const [unitCondition, setUnitCondition] = useState('');
  // The box being filled (header "Into A36") is the default place (flow audit 11-12).
  const [cur0] = useState(() => { const c = readCurrentBox(); return isFresh(c) ? c : null; });
  const [to, setTo] = useState<Holder | null>(cur0 ? { box_id: cur0.box_id } : null);
  const [toName, setToName] = useState(cur0?.name ?? '');
  const [fields, setFields] = useState(false);       // cost, store and order # (the ticked order has them)
  const [otherPlace, setOtherPlace] = useState(false); // a shelf or a building instead of a box
  const [last, setLast] = useState<LastBox | null>(null);
  const [error, setError] = useState<string | null>(null);

  // A different product on the form: the box is filled in only if the last receipt was this same product.
  useEffect(() => {
    const l = readLast();
    const same = !!pid && !!l && l.product_id === pid;
    setLast(l);
    const cur = readCurrentBox();   // else the box being filled (flow audit 11-12)
    setTo(same ? l.holder : isFresh(cur) ? { box_id: cur!.box_id } : null);
    setToName(same ? l.name : isFresh(cur) ? cur!.name : '');
    setError(null);
  }, [pid]);

  // A box scanned before this form opened (on the search screen, or while the item was still opening) goes in here.
  // Declared after the effect above, so it wins over "no box for a new product".
  const next = useNextPlace();
  useEffect(() => {
    if (!pid || !next) return;
    setTo(next.holder);
    setToName(next.name);
    setNextPlace(null);
  }, [pid, next]);
  // While the item loads there is no box picker yet: hold a box scan for the form instead of letting the shell open
  // the box's page.
  const { feedback } = useScanBus();
  useScan(async (code) => {
    const r = await get<ScanResult>(`/api/scan?code=${encodeURIComponent(code)}`);
    const place = placeOf(r);
    if (place) { setNextPlace(place); feedback(true, `Into ${place.name}`); }
    else feedback(false, 'Still opening the item. Scan again in a second');
  }, !!pid && !product.data);

  const leftOf = (l: PurchaseLine) => Math.max(0, l.left_to_receive ?? l.qty - l.received);
  const pickLine = (l: PurchaseLine | null) => {
    setMore([]);
    setLineId(l?.id ?? null);
    setPurchasedAt(l?.ordered_at ?? null);
    if (l) {
      // Flow audit 11-12 (finding 1): one item in hand is 1. "All N" next to the quantity takes the package.
      setQty('1');
      setCost(dollarsForInput(l.unit_cost_cents));
      setSupplier(`${l.source}${l.account ? ` (${l.account})` : ''}`);
      setReference(l.order_id);
    } else if (p) {
      setCost(dollarsForInput(p.default_cost_cents ?? p.avg_cost_cents));
      setSupplier('');
      setReference('');
    }
  };

  const all = openLines.data?.lines ?? [];
  const chosen = all.filter((l) => l.id === lineId || more.includes(l.id));   // oldest first, as listed
  const multi = chosen.length > 1;
  const toggleLine = (l: PurchaseLine) => {
    const on = chosen.some((c) => c.id === l.id);
    const next = on ? chosen.filter((c) => c.id !== l.id) : [...chosen, l];
    if (next.length <= 1) { pickLine(next[0] ?? null); return; }
    const ordered = all.filter((x) => next.some((c) => c.id === x.id));
    setLineId(ordered[0]!.id);
    setMore(ordered.slice(1).map((x) => x.id));
    setQty(String(ordered.reduce((a, x) => a + leftOf(x), 0)));
  };
  // The quantity spread over the chosen orders, oldest first.
  const spread = (() => {
    let n = Number(qty) || 0;
    return chosen.map((l) => { const take = Math.min(n, leftOf(l)); n -= take; return { l, take }; });
  })();
  const roomOnOrders = chosen.reduce((a, l) => a + leftOf(l), 0);

  // Once both the product and its open order lines are loaded: use the line from the link,
  // else the oldest open line, else the product's usual cost.
  useEffect(() => {
    const lines = openLines.data?.lines;
    if (!p || !lines) return;
    // A refresh (the other person saved something) keeps the orders already ticked.
    if (more.length && lineId && [lineId, ...more].every((id) => lines.some((l) => l.id === id))) return;
    const want = lineParam ? Number(lineParam) : lineId;
    pickLine(lines.find((l) => l.id === want) ?? lines[0] ?? null);
  }, [p?.id, openLines.data, lineParam]); // eslint-disable-line react-hooks/exhaustive-deps

  const newBox = useMutation({
    mutationFn: () => post<{ boxes: BoxRow[] }>('/api/boxes', { count: 1 }),
    onSuccess: (r) => {
      const b = r.boxes[0]!;
      setTo({ box_id: b.id });
      setToName(`Box ${b.id}`);
      qc.invalidateQueries({ queryKey: ['boxes'] });
      void printLabels([boxLabel(b)]);
    },
  });

  // Past what was bought, the server asks first (Sam 11-12: 6 Batmobiles of 3 bought). "Receive anyway" sends over_ok.
  const [over, setOver] = useState<string | null>(null);
  const overOk = useRef(false);
  const submit = useMutation({
    mutationFn: () => multi
      ? post<{ action_ids: number[]; units: { id: number; scan_code: string }[] }>('/api/receive-lines', {
        product_id: pid, to, unit_condition: unitCondition,
        lines: spread.filter((x) => x.take > 0).map((x) => ({ purchase_line_id: x.l.id, qty: x.take })),
      })
      : post<{ action_id: number; units: { id: number; scan_code: string }[]; confirm?: string }>('/api/receive', {
        product_id: pid, qty: Number(qty), unit_cost_cents: centsFromInput(cost), to, supplier, reference, unit_condition: unitCondition,
        purchase_line_id: lineId, purchased_at: purchasedAt, over_ok: overOk.current,
      }).then((r) => {
        overOk.current = false;
        if (r.confirm) throw new Error(`confirm:${r.confirm}`);
        return { ...r, action_ids: [r.action_id] };
      }),
    onSuccess: (r) => {
      setOver(null);
      try { localStorage.setItem(LAST_BOX, JSON.stringify({ holder: to, name: toName || 'the last place', product_id: pid })); } catch { /* ignore */ }
      qc.invalidateQueries();
      const orders = r.action_ids.length > 1 ? ` from ${r.action_ids.length} orders` : '';
      toast(`Received ${qty} × ${p!.sku}${orders} into ${toName || 'the box'}`, {
        undo: async () => {
          for (const id of [...r.action_ids].reverse()) await post('/api/undo', { action_id: id });
          qc.invalidateQueries(); toast('Receipt undone');
        },
      });
      if (r.units.length) void printLabels(r.units.map((u) => unitLabel({ scan_code: u.scan_code, sku: p!.sku, title: p!.title, condition: unitCondition || p!.condition })));
      setQty('1');
      setReference('');
      setLineId(null);
      setMore([]);
      navigate('/receive', true);
    },
    onError: (e: Error) => (e.message.startsWith('confirm:') ? setOver(e.message.slice(8)) : setError(e.message)),
  });

  const go = (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setOver(null);
    if (!to) return setError('Scan or pick the box it goes into');
    if (multi && Number(qty) > roomOnOrders) return setError(`Those orders have ${roomOnOrders} left to receive. Lower the quantity, or receive the rest as "Not from an imported order".`);
    if (multi && spread.some((x) => !x.take)) return setError('The quantity does not reach every order you picked. Untick the ones not in this pile.');
    if (!multi && cost.trim() && centsFromInput(cost) === null) return setError('Type the cost as a number, like 12.50, or leave it blank');
    submit.mutate();
  };

  return (
    <div className="max-w-3xl space-y-4">
      <PageHeader title="Receive" />
      {!pid ? <ProductFinder onPick={(id) => navigate(`/receive?product=${id}`)} /> : !p ? <Card><CardContent className="p-6">Loading...</CardContent></Card> : (
        <Card><CardContent className="p-4 sm:p-6">
          <div className="mb-4 flex items-start gap-3">
            <Thumb photoId={p.photos[0]?.id} size="focus" />
            <div className="min-w-0 flex-1">
              <div className="font-semibold">{p.title}</div>
              <div className="font-mono text-xs text-muted-foreground">{p.sku} · {p.owner} · {p.on_hand} on hand{p.avg_cost_cents !== null ? ` · avg ${money(p.avg_cost_cents)}` : ''}</div>
            </div>
            <Button variant="ghost" size="sm" onClick={() => navigate('/receive')}>Change</Button>
          </div>
          <ListedNote data={listed.data} />
          {(openLines.data?.lines.length ?? 0) > 0 && (
            <div className="mb-4 space-y-1.5">
              <div className="text-sm font-medium">From which order? {all.length > 1 ? 'Tap every order in this pile.' : '(fills in cost and purchase date)'}</div>
              {all.map((l) => {
                const on = chosen.some((c) => c.id === l.id);
                const take = spread.find((x) => x.l.id === l.id)?.take;
                return (
                  <button key={l.id} type="button" role="checkbox" aria-checked={on} onClick={() => toggleLine(l)}
                    className={cn('flex w-full items-start gap-2.5 rounded-md border p-2.5 text-left text-sm', on && 'border-primary bg-primary/10')}>
                    <span className={cn('mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-sm border', on ? 'border-primary bg-primary text-primary-foreground' : 'border-muted-foreground/50')}>{on && <Check className="h-3 w-3" />}</span>
                    <span className="min-w-0 flex-1"><span className="capitalize">{l.source}</span> {l.order_id} · bought {l.ordered_at ?? '?'}{l.why ? <span className="block text-xs text-muted-foreground">{l.why}</span> : null}</span>
                    <span className="shrink-0 text-right tabular-nums">{leftOf(l)} left · {money(l.unit_cost_cents)}{multi && on ? <span className="block text-xs font-semibold">{take} from here</span> : null}</span>
                  </button>
                );
              })}
              <button type="button" onClick={() => pickLine(null)} className={cn('w-full rounded-md border p-2.5 text-left text-sm', lineId === null && 'border-primary bg-primary/10')}>Not from an imported order</button>
            </div>
          )}
          {noOrder && (alike.data?.matches.length ?? 0) > 0 && (
            <div className="mb-4 rounded-lg border border-warning/50 bg-warning/10 p-3">
              <BuyMatchList matches={alike.data!.matches.filter((m) => m.kind !== 'product')} onPick={linkBuy} busy={linking} action="Link"
                title="No order is linked to this item. Did you buy it as one of these? Tap it to link."
                footer="Not one of these? Receive it as below: it goes in as stock with no order." />
            </div>
          )}
          <form onSubmit={go} className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-1.5">
                <Label htmlFor="qty">Quantity{multi ? ` (up to ${roomOnOrders})` : ''}</Label>
                <Input id="qty" inputMode="numeric" value={qty} onChange={(e) => { setQty(e.target.value.replace(/\D/g, '')); setOver(null); }} className="h-12 text-lg font-bold" required />
                {(() => {
                  const n = multi ? roomOnOrders : (chosen[0]?.package_left ?? (chosen[0] ? leftOf(chosen[0]) : 0));
                  return n > 1 && Number(qty) !== n
                    ? <button type="button" className="h-11 rounded-md border px-3 text-sm font-medium" onClick={() => setQty(String(n))}>All {n}{!multi && chosen[0]?.delivered_on ? ' from this package' : ''}</button>
                    : null;
                })()}
              </div>
              {multi ? (
                <div className="space-y-1.5 text-sm">
                  <div className="font-medium">Cost and order</div>
                  <p className="text-muted-foreground">Each order keeps its own cost and order number.</p>
                </div>
              ) : chosen.length === 1 && !fields ? (
                <div className="space-y-1 text-sm">
                  <div className="font-medium">Cost and order</div>
                  <div className="text-muted-foreground">{money(chosen[0]!.unit_cost_cents, 'cost not entered')} from order {chosen[0]!.order_id}</div>
                  <button type="button" className="min-h-9 text-sm text-primary underline" onClick={() => setFields(true)}>Change</button>
                </div>
              ) : <>
              <div className="space-y-1.5">
                <Label htmlFor="cost">Unit cost ($, blank if unknown)</Label>
                <Input id="cost" inputMode="decimal" value={cost} onChange={(e) => setCost(e.target.value)} className="h-12 text-lg" placeholder="0.00" />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="sup">Supplier / store</Label>
                <Input id="sup" value={supplier} onChange={(e) => setSupplier(e.target.value)} className="h-10" placeholder="Optional" />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="ref">Order # / note</Label>
                <Input id="ref" value={reference} onChange={(e) => setReference(e.target.value)} className="h-10" placeholder="Optional" />
              </div>
              </>}
              {p.tracking === 'unit' && (
                <div className="col-span-2 space-y-1.5">
                  <Label htmlFor="ucond">Unit condition note (each unit gets its own label)</Label>
                  <Input id="ucond" value={unitCondition} onChange={(e) => setUnitCondition(e.target.value)} className="h-10" placeholder={p.condition} />
                </div>
              )}
            </div>
            <div className="space-y-1.5"><div className="text-sm font-medium">Which market?</div>
              <MarketPicker suggest={suggestOf(listed.data)} current={to && 'box_id' in to ? toName : null} onPick={(b) => { setTo({ box_id: b.id }); setToName(b.code); }} /></div>
            {otherPlace || (to && !('box_id' in to))
              ? <HolderPicker value={to} onChange={(h, n) => { setTo(h); setToName(n); }} label="Into which box or place" />
              : <div className="text-sm">{to ? <>Into <b>{toName}</b>. </> : 'Scan the box label, or pick the market above. '}
                  <button type="button" className="min-h-9 text-primary underline" onClick={() => setOtherPlace(true)}>Other box, shelf or place</button></div>}
            {to && 'box_id' in to && isABox(toName) && listed.data?.amazon?.can_sell === false && (
              <AmazonNote a={listed.data.amazon} inA boxName={toName} />
            )}
            {to && last && last.product_id === pid && sameHolder(to, last.holder) && (
              <p className="text-sm text-warning-ink">{last.name} is filled in because the last ones of this item went there. Going somewhere else? Scan or pick the other box.</p>
            )}
            <div className="flex flex-wrap gap-2">
              {!to && last && (
                <Button type="button" variant="outline" size="sm" className="h-auto min-h-8 whitespace-normal py-1.5 text-left" onClick={() => { setTo(last.holder); setToName(last.name); }}>Use {last.name} again (the last one you received into)</Button>
              )}
              <Button type="button" variant="outline" size="sm" onClick={() => newBox.mutate()} disabled={newBox.isPending}><Printer className="mr-1.5 h-3.5 w-3.5" />New box + print its label</Button>
            </div>
            {multi ? <p className="text-sm text-muted-foreground">Total {money(spread.reduce((a, x) => a + x.take * (x.l.unit_cost_cents ?? 0), 0))} from {plural(spread.filter((x) => x.take).length, 'order')}</p>
              : qty && cost && centsFromInput(cost) !== null && <p className="text-sm text-muted-foreground">Total {money(Number(qty) * centsFromInput(cost)!)}</p>}
            {error && <p className="text-sm text-destructive">{error}</p>}
            {over && (
              <div className="space-y-2 rounded-md border border-warning bg-warning/10 p-3 text-sm">
                <p>{over}</p>
                <div className="flex flex-wrap gap-2">
                  <Button type="button" className="h-11" disabled={submit.isPending} onClick={() => { overOk.current = true; setOver(null); submit.mutate(); }}>Receive anyway</Button>
                  <Button type="button" variant="ghost" className="h-11" onClick={() => setOver(null)}>Cancel</Button>
                </div>
              </div>
            )}
            <Button type="submit" className="h-12 w-full text-base" disabled={submit.isPending}>{submit.isPending ? 'Saving...' : `Receive ${qty || 0}`}</Button>
          </form>
          {p.photos.length === 0 && <div className="mt-4"><div className="mb-1.5 text-sm font-medium">No photo yet{justMade ? ' (new to Boxloft)' : ''}. Take one:</div><PhotoStrip photos={[]} uploadTo={`/api/photos?product_id=${p.id}`} onChange={() => product.refetch()} /></div>}
        </CardContent></Card>
      )}
    </div>
  );
}
