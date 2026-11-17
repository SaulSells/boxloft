import { useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { FileUp, Link2, PackageCheck, PackagePlus, Plus, SearchCheck, Truck } from 'lucide-react';
import { get, post, ApiError } from '@/api/client';
import type { PickWhere, ProductRow, SalesLine, SalesOrder, ShipBox } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent } from '@/components/ui/card';
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Chips, Empty, PageHeader, SearchInput, Thumb } from '@/components/app/common';
import { useToast } from '@/components/app/toast';
import { FoundForm } from '@/pages/Audit';
import { useScan, useScanBus } from '@/lib/scan';
import { Link, navigate } from '@/lib/router';
import { parseCode } from '@/lib/codes';
import { plural } from '@/lib/format';
import { cn } from '@/lib/utils';

const CHANNELS = ['amazon', 'ebay', 'walmart', 'shopify', 'other'];

function whereText(w: PickWhere) {
  // Boxes go by the name written on them (A31); loose stock by its shelf (S2) or place.
  if (!w.box_id) return `loose on ${w.location_code}`;
  return `Box ${w.box_name ? w.box_name.toUpperCase() : w.box_id}`;
}

function ProductSearch({ onPick }: { onPick: (p: ProductRow) => void }) {
  const [q, setQ] = useState('');
  const res = useQuery({ queryKey: ['products', 'pick', q], queryFn: () => get<{ products: ProductRow[] }>(`/api/products?q=${encodeURIComponent(q)}`), enabled: q.trim().length > 1 });
  return (
    <div className="space-y-2">
      <SearchInput autoFocus placeholder="Search title, SKU, UPC..." value={q} onChange={(e) => setQ(e.target.value)} />
      <div className="max-h-64 divide-y overflow-y-auto rounded-md border empty:hidden">
        {res.data?.products.slice(0, 10).map((p) => (
          <button key={p.id} className="flex w-full items-center gap-2 p-2 text-left text-sm hover:bg-muted/50" onClick={() => onPick(p)}>
            <Thumb photoId={p.photo_id} size="dense" /><span className="flex-1 truncate">{p.title}</span><span className="font-mono text-xs text-muted-foreground">{p.on_hand} here</span>
          </button>
        ))}
      </div>
      {q.trim().length > 1 && res.data && !res.data.products.length && <p className="text-sm text-muted-foreground">Nothing in Boxloft matches "{q.trim()}".</p>}
    </div>
  );
}

function ShipDialog({ order, line, boxes, onClose }: { order: SalesOrder; line: SalesLine; boxes: ShipBox[]; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const { feedback } = useScanBus();
  const [from, setFrom] = useState<PickWhere | null>(line.where[0] ?? null);
  const [shipBox, setShipBox] = useState(String(order.recommendation?.box?.id ?? ''));
  const [fit, setFit] = useState('');
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  // Scan the box (or the unit label) you are taking it from to confirm the pick.
  useScan((code) => {
    const c = parseCode(code);
    const hit = line.where.find((w) => (c.kind === 'box' && w.box_id === c.id) || (c.kind === 'unit' && w.unit_id === c.id)
      || (c.kind === 'location' && !w.box_id && w.location_id === c.id));
    if (hit) { setFrom(hit); feedback(true, `Confirmed: ${whereText(hit)}`, true); setMsg(`Confirmed: ${whereText(hit)}`); }   // next: tap Packed and shipped
    else { feedback(false); setMsg('That is not where this item is. Check the list.'); }
  });

  const ship = async () => {
    if (!from) return;
    setBusy(true);
    try {
      const r = await post<{ action_id: number; order_done: boolean }>(`/api/orders/lines/${line.id}/ship`, {
        from: from.box_id ? { box_id: from.box_id } : { location_id: from.location_id },
        unit_id: from.unit_id || 0, ship_box_id: shipBox ? Number(shipBox) : null, box_fit: fit,
      });
      qc.invalidateQueries();
      toast(r.order_done ? `Order ${order.external_id} done` : 'Line shipped', { undo: async () => { await post('/api/undo', { action_id: r.action_id }); qc.invalidateQueries(); toast('Stock put back. Reopen the order if needed.'); } });
      onClose();
    } catch (e) {
      toast(e instanceof ApiError ? e.message : 'Ship failed', { tone: 'error' });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-h-[92vh] overflow-y-auto">
        <DialogHeader><DialogTitle>Ship {line.qty} × {line.product_title ?? line.title}</DialogTitle></DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label>Take it from (scan the box or shelf to confirm)</Label>
            {line.where.length ? line.where.map((w) => (
              <button key={`${w.box_id}-${w.location_id}-${w.unit_id}`} onClick={() => setFrom(w)}
                className={cn('flex w-full justify-between rounded-md border p-2.5 text-left text-sm', from === w && 'border-primary bg-primary/10')}>
                <span>{whereText(w)}{w.unit_id ? ` · unit ${w.unit_id}${w.unit_condition ? ` (${w.unit_condition})` : ''}` : ''}</span><span className="tabular-nums">{w.qty} here</span>
              </button>
            )) : <p className="text-sm text-destructive">None in stock. Receive it first or cancel the order.</p>}
            {msg && <p className="text-sm">{msg}</p>}
          </div>
          <div className="space-y-1.5">
            <Label>Ship box{order.recommendation?.box ? ` (suggested: ${order.recommendation.box.name})` : ''}</Label>
            <Select value={shipBox} onValueChange={setShipBox}>
              <SelectTrigger className="h-11 w-full"><SelectValue placeholder="Pick a box (optional)" /></SelectTrigger>
              <SelectContent>{boxes.map((b) => <SelectItem key={b.id} value={String(b.id)}>{b.name} · {b.inner_l_in}×{b.inner_w_in}×{b.inner_h_in} in{boxes.some((x) => x.stock > 0) ? ` · ${b.stock} left` : ''}</SelectItem>)}</SelectContent>
            </Select>
            {order.recommendation?.reason && <p className="text-xs text-muted-foreground">{order.recommendation.reason}</p>}
          </div>
          {shipBox && (
            <div className="space-y-1.5">
              <Label>How did it fit? (teaches the suggestions)</Label>
              <div className="flex gap-2">{[['ok', 'Good fit'], ['small', 'Too small'], ['big', 'Too big']].map(([k, l]) => (
                <Button key={k} type="button" size="sm" variant={fit === k ? 'default' : 'outline'} onClick={() => setFit(k!)}>{l}</Button>
              ))}</div>
            </div>
          )}
        </div>
        <DialogFooter><Button className="h-11 w-full" disabled={!from || busy} onClick={ship}><Truck className="mr-1.5 h-4 w-4" />Packed and shipped</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function LinkDialog({ line, onMake, onClose }: { line: SalesLine; onMake: () => void; onClose: () => void }) {
  const qc = useQueryClient();
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>Which product is "{line.title || line.sku}"?</DialogTitle></DialogHeader>
        <p className="text-sm text-muted-foreground">Search the products already in Boxloft and tap the right one. Boxloft remembers the match for the next order.</p>
        <ProductSearch onPick={async (p) => { await post(`/api/orders/lines/${line.id}/link`, { product_id: p.id, seen: { product_id: line.product_id } }); qc.invalidateQueries({ queryKey: ['orders'] }); onClose(); }} />
        <DialogFooter><Button variant="outline" onClick={() => { onClose(); onMake(); }}><Plus className="mr-1.5 h-4 w-4" />Not there? Add it from this order</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** A line that can't be picked yet always says why and what to do next. */
function NotReady({ order, line, busy, onMake, onLink, onFound }: {
  order: SalesOrder; line: SalesLine; busy: boolean; onMake: () => void; onLink: () => void; onFound: () => void;
}) {
  const [finding, setFinding] = useState(false);
  const [shipping, setShipping] = useState(false);
  const qc = useQueryClient();
  const toast = useToast();
  /* Flow audit 11-12 (redesign 3): 0 on the shelves and the audit or a delivered order says it is here, so it is
   * leaving from the package. One tap: supplier -> customer, tied to the sale and the audit row. No box is asked. */
  /* Alex 11-17 (College Football 27): "purchased on Amazon, we just didn't have it received yet. Since it's sold it
   * doesn't need to go into a box." Its buy is on no order Boxloft reads, so nothing says it was ever here. A person
   * says it sold before it came in: one save, supplier -> customer, order # and cost optional (Usual unit cost fills it). */
  const [outside, setOutside] = useState(false);
  const [cost, setCost] = useState('');
  const [boughtOn, setBoughtOn] = useState('');
  const costCents = cost.trim() ? Math.round(parseFloat(cost.replace(/[$,]/g, '')) * 100) : null;
  const costBad = costCents !== null && !(costCents > 0);
  const shipNow = async (asOutside = false) => {
    setShipping(true);
    try {
      const r = await post<{ order_done: boolean }>(`/api/orders/lines/${line.id}/ship-unscanned`, {
        ship_box_id: order.recommendation?.box?.id ?? null,
        ...(asOutside ? { outside: true, cost_cents: costCents, bought_on: boughtOn.trim() } : {}),
      });
      qc.invalidateQueries();
      toast(`${r.order_done ? `Order ${order.external_id} done` : 'Line shipped'}: ${line.qty} × ${(line.product_title ?? line.title).slice(0, 40)}, ${asOutside ? 'sold before it came in' : 'never scanned in'}.`, {
        undo: async () => {
          await post(`/api/orders/lines/${line.id}/unship-unscanned`);
          qc.invalidateQueries();
          toast('Undone: the order is open again.');
        },
      });
    } catch (e) {
      toast(e instanceof ApiError ? e.message : 'Ship failed', { tone: 'error' });
    } finally {
      setShipping(false);
    }
  };
  const box = 'space-y-2 rounded-lg bg-warning/10 px-3 py-2 text-sm';
  const wrap = 'h-auto min-h-8 max-w-full shrink whitespace-normal py-1.5 text-left';   // long labels wrap on a phone
  if (!line.product_id) {
    return (
      <div className={box}>
        <div><b>Boxloft has no product for this item yet</b>, so it can't show which box to pick from.</div>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" className={wrap} disabled={busy} onClick={onMake}><Plus className="mr-1.5 h-3.5 w-3.5 shrink-0" />Add it from this order</Button>
          <Button size="sm" variant="outline" className={wrap} disabled={busy} onClick={onLink}><Link2 className="mr-1.5 h-3.5 w-3.5 shrink-0" />It is already in Boxloft</Button>
        </div>
      </div>
    );
  }
  const canShip = !!(line.audit && line.audit.left >= line.qty) || (line.coming ?? 0) >= line.qty;
  return (
    <div className={box}>
      {line.audit ? <div><b>Not scanned in.</b> The stock audit expects {line.audit.left}.</div>
        : line.coming ? <div><b>Not scanned in.</b> {line.coming} bought and not received yet.</div>
        : <div><b>Boxloft shows 0 of this on the shelves.</b> No buy in Boxloft has it either. If you have it, receive it first. If it sold and went out before it was received, tap "Sold before it came in". If you don't have it, cancel the order here and on <span className="capitalize">{order.channel}</span>.</div>}
      {!canShip && !line.audit && outside ? (
        <div className="space-y-2">
          <Label htmlFor={`bought-${line.id}`}>Order it was bought on (optional)</Label>
          <Input id={`bought-${line.id}`} placeholder="Order #, if you know it" value={boughtOn} onChange={(e) => setBoughtOn(e.target.value)} className="h-11" />
          <Label htmlFor={`cost-${line.id}`}>What did 1 cost? (leave blank if you don't know)</Label>
          <Input id={`cost-${line.id}`} inputMode="decimal" placeholder="$" value={cost} onChange={(e) => setCost(e.target.value)} className="h-11" />
          {costBad && <p className="text-xs text-destructive">Type a price above $0, or leave it blank.</p>}
          <p className="text-xs text-muted-foreground">Saves {line.qty} as bought and shipped in one step. Nothing goes in a box.{cost.trim() ? '' : ' The item\'s Usual unit cost fills the price later.'}</p>
          <div className="flex flex-wrap gap-2">
            <Button className="h-11 flex-1" disabled={shipping || costBad} onClick={() => void shipNow(true)}><Truck className="mr-1.5 h-4 w-4" />Packed and shipped</Button>
            <Button variant="outline" className="h-11" onClick={() => setOutside(false)}>Cancel</Button>
          </div>
        </div>
      ) : line.audit && finding ? (
        // The Stock audit's own form and call (POST /api/audit/<id>/found), so the audit row counts it as found.
        <FoundForm it={line.audit} onCancel={() => setFinding(false)} onDone={() => { setFinding(false); onFound(); }} />
      ) : (
        <div className="flex flex-wrap gap-2">
          {canShip && <Button className="h-11 w-full" disabled={shipping} onClick={() => void shipNow()}><Truck className="mr-1.5 h-4 w-4" />Packed and shipped</Button>}
          {line.audit && <Button size="sm" variant="outline" className={wrap} onClick={() => setFinding(true)}><PackagePlus className="mr-1.5 h-3.5 w-3.5 shrink-0" />It's in a box</Button>}
          {!canShip && <Button size="sm" className={wrap} onClick={() => navigate(`/receive?product=${line.product_id}`)}><PackagePlus className="mr-1.5 h-3.5 w-3.5" />Receive it</Button>}
          {!canShip && !line.audit && <Button size="sm" variant="outline" className={wrap} onClick={() => setOutside(true)}><Truck className="mr-1.5 h-3.5 w-3.5 shrink-0" />Sold before it came in</Button>}
          {line.audit && <Button size="sm" variant="ghost" className={wrap} onClick={() => navigate(`/audit?q=${encodeURIComponent(line.audit!.asin)}`)}><SearchCheck className="mr-1.5 h-3.5 w-3.5 shrink-0" />Not here? Open the audit row</Button>}
        </div>
      )}
    </div>
  );
}

function AddSaleDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [p, setP] = useState<ProductRow | null>(null);
  const [qty, setQty] = useState('1');
  const [channel, setChannel] = useState('ebay');
  const [orderId, setOrderId] = useState('');
  const save = async () => {
    try { await post('/api/orders', { product_id: p!.id, qty: Number(qty), channel, order_id: orderId }); qc.invalidateQueries({ queryKey: ['orders'] }); onClose(); }
    catch (e) { toast((e as Error).message, { tone: 'error' }); }
  };
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>Add a sale</DialogTitle></DialogHeader>
        {!p ? <ProductSearch onPick={setP} /> : (
          <div className="space-y-3">
            <div className="flex items-center gap-2 rounded-md border p-2 text-sm"><Thumb photoId={p.photo_id} size="dense" /><span className="flex-1">{p.title}</span><Button size="sm" variant="ghost" onClick={() => setP(null)}>Change</Button></div>
            <div className="grid grid-cols-3 gap-2">
              <div className="space-y-1"><Label>Qty</Label><Input inputMode="numeric" value={qty} onChange={(e) => setQty(e.target.value)} className="h-10" /></div>
              <div className="space-y-1"><Label>Where</Label>
                <Select value={channel} onValueChange={setChannel}><SelectTrigger className="h-10 w-full"><SelectValue /></SelectTrigger>
                  <SelectContent>{CHANNELS.map((c) => <SelectItem key={c} value={c} className="capitalize">{c}</SelectItem>)}</SelectContent></Select></div>
              <div className="space-y-1"><Label>Order #</Label><Input value={orderId} onChange={(e) => setOrderId(e.target.value)} className="h-10" /></div>
            </div>
          </div>
        )}
        <DialogFooter><Button disabled={!p} onClick={save}>Add to pick list</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function Orders() {
  const qc = useQueryClient();
  const toast = useToast();
  const file = useRef<HTMLInputElement>(null);
  const [status, setStatus] = useState('open');
  const [channel, setChannel] = useState('amazon');
  const [shipping, setShipping] = useState<{ order: SalesOrder; line: SalesLine } | null>(null);
  const [linking, setLinking] = useState<SalesLine | null>(null);
  const [adding, setAdding] = useState(false);
  const [making, setMaking] = useState<number | null>(null);
  // An order line with no product: make the product from the line (or find the one with its code) and link it.
  const make = async (l: SalesLine) => {
    setMaking(l.id);
    try {
      const r = await post<{ product_id: number; made: boolean }>(`/api/orders/lines/${l.id}/make-product`);
      await qc.invalidateQueries({ queryKey: ['orders'] });
      toast(r.made ? 'Added to Boxloft and matched to this order. Next: get it into a box.' : 'Matched to the product already in Boxloft.');
    } catch (e) {
      toast(e instanceof ApiError ? e.message : 'Could not add it', { tone: 'error' });
    } finally { setMaking(null); }
  };
  const orders = useQuery({ queryKey: ['orders', status], queryFn: () => get<{ orders: SalesOrder[] }>(`/api/orders?status=${status}`) });
  // Marked found on the card: reload, then go straight to shipping it from the box it was just put in.
  const afterFound = async (orderId: number, lineId: number) => {
    await qc.invalidateQueries();
    const o = qc.getQueryData<{ orders: SalesOrder[] }>(['orders', status])?.orders.find((x) => x.id === orderId);
    const l = o?.lines.find((x) => x.id === lineId);
    if (o && l && l.where.length && !l.shipped_action_id) setShipping({ order: o, line: l });
  };
  const boxes = useQuery({ queryKey: ['ship-boxes'], queryFn: () => get<{ boxes: ShipBox[] }>('/api/ship-boxes') });

  const importFile = async (f: File | undefined) => {
    if (!f) return;
    try {
      const res = await fetch(`/api/orders/import?channel=${channel}`, { method: 'POST', credentials: 'same-origin', headers: { 'X-Boxloft': '1', 'Content-Type': 'text/csv' }, body: await f.text() });
      const r = await res.json();
      if (!res.ok) throw new Error(r.error || 'Import failed');
      qc.invalidateQueries({ queryKey: ['orders'] });
      toast(`${plural(r.new_orders, 'new order')}, ${plural(r.new_lines, 'line')}${r.unmatched ? `. ${r.unmatched} need a product link.` : ''}`);
    } catch (e) {
      toast((e as Error).message, { tone: 'error' });
    } finally {
      if (file.current) file.current.value = '';
    }
  };

  const list = orders.data?.orders ?? [];
  return (
    <div className="max-w-5xl space-y-4">
      <PageHeader title="Orders to ship" subtitle="Each sale shows its photo, the box it's in, and the ship box that fits."
        actions={<>
          <Button variant="outline" onClick={() => setAdding(true)}><Plus className="mr-1.5 h-4 w-4" />Add sale</Button>
          <Select value={channel} onValueChange={setChannel}><SelectTrigger className="w-28 capitalize"><SelectValue /></SelectTrigger>
            <SelectContent>{CHANNELS.map((c) => <SelectItem key={c} value={c} className="capitalize">{c}</SelectItem>)}</SelectContent></Select>
          <Button onClick={() => file.current?.click()}><FileUp className="mr-1.5 h-4 w-4" />Import orders</Button>
          <input ref={file} type="file" accept=".csv,.txt,.tsv" hidden onChange={(e) => importFile(e.target.files?.[0])} />
        </>} />
      <Chips items={[{ key: 'open', label: 'To ship' }, { key: 'shipped', label: 'Shipped' }, { key: 'cancelled', label: 'Cancelled' }]} value={status} onChange={setStatus} />
      {orders.data && !list.length && <Empty>{status === 'open' ? 'Nothing to ship. Import an order report or add a sale.' : 'None'}</Empty>}
      {list.map((o) => (
        <Card key={o.id}>
          <CardContent className="space-y-3 p-4">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <Badge variant="secondary" className="capitalize">{o.channel}</Badge>
              <span className="font-mono">{o.external_id}</span>
              <span className="text-muted-foreground">{o.ordered_at ? `ordered ${o.ordered_at}` : ''}{o.ship_by ? ` · ship by ${o.ship_by}` : ''}</span>
              {o.fulfilled_by === 'amazon' && <Badge variant="outline">FBA</Badge>}
              {o.status === 'open' && <Button size="sm" variant="ghost" className="ml-auto" onClick={async () => { await post(`/api/orders/${o.id}/status`, { status: 'cancelled', seen: { status: o.status } }); qc.invalidateQueries({ queryKey: ['orders'] }); }}>Cancel order</Button>}
            </div>
            {o.review && <div className="rounded-lg bg-warning/10 px-3 py-2 text-sm">{o.review}{o.market_status ? ` (${o.channel}: ${o.market_status})` : ''}. Scan it out from the box it left, or cancel it if it never shipped.</div>}
            {o.lines.map((l) => (
              <div key={l.id} className={cn('flex gap-3', l.shipped_action_id && 'opacity-60')}>
                <Thumb photoId={l.photo_id} url={l.image_url} size="focus" />
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="font-medium leading-snug">{l.product_id ? <Link to={`/inventory/${l.product_id}`} className="hover:underline">{l.product_title}</Link> : l.title}</div>
                  <div className="text-sm"><b>{l.qty}</b> × <span className="font-mono text-xs">{l.product_sku ?? l.sku}</span></div>
                  {l.shipped_action_id ? <div className="flex items-center gap-1 text-sm text-primary"><PackageCheck className="h-4 w-4" />Shipped{l.ship_box_name ? ` in ${l.ship_box_name}` : ''}{l.unscanned_id ? ', never scanned in' : ''}</div>
                    : o.status === 'shipped' ? <div className="flex items-center gap-1 text-sm text-primary"><PackageCheck className="h-4 w-4" />Shipped on {o.channel === 'ebay' ? 'eBay' : o.channel === 'amazon' ? 'Amazon' : o.channel}, not scanned out here</div>
                    : !l.product_id || !l.where.length ? (o.status === 'open' ? <NotReady order={o} line={l} busy={making === l.id} onMake={() => make(l)} onLink={() => setLinking(l)} onFound={() => void afterFound(o.id, l.id)} /> : null)
                    : (
                      <>
                        <div className="text-sm">Pick from <b>{whereText(l.where[0]!)}</b>{l.where.length > 1 ? ` (+${l.where.length - 1} more places)` : ''}</div>
                        <div className="text-sm text-muted-foreground">Ship in: {o.recommendation?.box ? <b className="text-foreground">{o.recommendation.box.name}</b> : (o.recommendation?.reason ?? '—')}</div>
                        <Button size="sm" className="mt-1" onClick={() => setShipping({ order: o, line: l })}><Truck className="mr-1.5 h-3.5 w-3.5" />Ship</Button>
                      </>
                    )}
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      ))}
      {shipping && <ShipDialog order={shipping.order} line={shipping.line} boxes={boxes.data?.boxes ?? []} onClose={() => setShipping(null)} />}
      {linking && <LinkDialog line={linking} onMake={() => make(linking)} onClose={() => setLinking(null)} />}
      {adding && <AddSaleDialog onClose={() => setAdding(false)} />}
    </div>
  );
}
