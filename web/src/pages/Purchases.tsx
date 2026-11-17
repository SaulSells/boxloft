import { useRef, useState, type FormEvent } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { FileUp, Plus } from 'lucide-react';
import { get, post, ApiError } from '@/api/client';
import type { PurchaseLine } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Empty, PageHeader, SearchInput, Stat } from '@/components/app/common';
import { useToast } from '@/components/app/toast';
import { receiveLine } from '@/lib/receive';
import { centsFromInput, money, plural } from '@/lib/format';

interface Totals { lines: number; bought: number; spend: number; received: number; returned?: number; presold?: number; stale_units: number; supplies?: { orders: number; units: number; spend: number } }

export function Purchases() {
  const qc = useQueryClient();
  const toast = useToast();
  const file = useRef<HTMLInputElement>(null);
  const [status, setStatus] = useState('open');
  const [q, setQ] = useState('');
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [closeDate, setCloseDate] = useState('');
  const data = useQuery({
    queryKey: ['purchases', status, q],
    queryFn: () => get<{ lines: PurchaseLine[]; totals: Totals; stale_days: number }>(`/api/purchases?status=${status}&q=${encodeURIComponent(q)}`),
  });
  const t = data.data?.totals;

  const importFile = async (f: File | undefined) => {
    if (!f) return;
    setBusy(true);
    try {
      const res = await fetch('/api/purchases/import', { method: 'POST', credentials: 'same-origin', headers: { 'X-Boxloft': '1', 'Content-Type': 'text/csv' }, body: await f.text() });
      const r = await res.json();
      if (!res.ok) throw new ApiError(r.error || 'Import failed', res.status);
      qc.invalidateQueries({ queryKey: ['purchases'] });
      toast(r.mode === 'fix' ? `Amazon's file corrected ${r.corrected} lines and confirmed ${r.already_right}` : `Imported ${plural(r.new_lines, 'new line')} (${money(r.spend_cents)})${r.already_had ? `, ${r.already_had} already here` : ''}`);
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Import failed', { tone: 'error' });
    } finally {
      setBusy(false);
      if (file.current) file.current.value = '';
    }
  };

  const receive = (l: PurchaseLine) => receiveLine(l, toast);
  const closeLine = async (l: PurchaseLine) => { await post(`/api/purchases/${l.id}/cancel`, { cancelled: true, seen: { cancelled: 0, qty: l.qty } }); qc.invalidateQueries({ queryKey: ['purchases'] }); };
  const closeOld = async () => {
    try { const r = await post<{ closed: number }>('/api/purchases/close-before', { date: closeDate }); toast(`Closed ${plural(r.closed, 'line')}`); qc.invalidateQueries({ queryKey: ['purchases'] }); }
    catch (e) { toast((e as Error).message, { tone: 'error' }); }
  };

  return (
    <div className="max-w-5xl space-y-4 sm:space-y-6">
      <PageHeader title="Purchases" subtitle="What you bought, when and for how much. Receiving against a line fills in the cost and date."
        actions={<>
          <Button variant="outline" onClick={() => setAdding(!adding)}><Plus className="mr-1.5 h-4 w-4" />Add by hand</Button>
          <Button onClick={() => file.current?.click()} disabled={busy}><FileUp className="mr-1.5 h-4 w-4" />{busy ? 'Importing...' : 'Import CSV'}</Button>
          <input ref={file} type="file" accept=".csv,.txt,.tsv,text/csv" hidden onChange={(e) => importFile(e.target.files?.[0])} />
        </>} />
      {t && (
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <Stat label="Units bought" value={t.bought.toLocaleString()} hint={plural(t.lines, 'line')} />
          <Stat label="Spend" value={money(t.spend)} hint={t.supplies?.orders ? `Stock only. Supplies not counted: ${money(t.supplies.spend)} on ${plural(t.supplies.orders, 'order')}` : 'Stock only'} />
          <Stat label="Received" value={t.received.toLocaleString()} hint={`${t.bought ? Math.round((100 * t.received) / t.bought) : 0}% of units${t.presold ? ` · ${t.presold.toLocaleString()} sold on Amazon before Boxloft` : ''}${t.returned ? ` · ${t.returned.toLocaleString()} sent back` : ''}`} />
          <Stat label={`Not here after ${data.data?.stale_days} days`} value={t.stale_units.toLocaleString()} hint="units still open" tone={t.stale_units ? 'warn' : undefined} />
        </div>
      )}
      {adding && <AddPurchase onDone={() => { setAdding(false); qc.invalidateQueries({ queryKey: ['purchases'] }); }} />}
      <Card>
        <CardContent className="space-y-1 p-4 text-sm text-muted-foreground">
          <p><b className="text-foreground">Amazon:</b> import the tracker export first (it says which orders are inventory). Then import Amazon's Order History.csv; it only corrects quantity and cost on those orders and skips personal buys.</p>
          <p><b className="text-foreground">Other stores:</b> any CSV with an order number, date, item (or UPC/ASIN), quantity and unit cost. A Retailer column names the store.</p>
        </CardContent>
      </Card>
      <div className="flex flex-wrap items-center gap-3">
        <SearchInput placeholder="Search item, ASIN, UPC, order #" value={q} onChange={(e) => setQ(e.target.value)} className="h-10 w-full sm:max-w-sm" />
        <Select value={status} onValueChange={setStatus}>
          <SelectTrigger className="h-10 w-40"><SelectValue /></SelectTrigger>
          <SelectContent><SelectItem value="open">Not received</SelectItem><SelectItem value="received">Received</SelectItem><SelectItem value="returned">Sent back</SelectItem><SelectItem value="all">All</SelectItem></SelectContent>
        </Select>
        <div className="flex w-full flex-wrap items-center gap-2 text-sm sm:w-auto">
          <span className="text-muted-foreground">Close open lines bought before</span>
          <Input type="date" value={closeDate} onChange={(e) => setCloseDate(e.target.value)} className="h-10 w-40" />
          <Button variant="outline" size="sm" disabled={!closeDate} onClick={closeOld}>Close</Button>
        </div>
      </div>
      {data.data && !data.data.lines.length ? <Empty>{status === 'open' ? 'Nothing waiting to be received.' : 'No purchases yet. Import a CSV to start.'}</Empty> : (<>
        <div className="space-y-2 md:hidden">
          {data.data?.lines.map((l) => (
            <div key={l.id} className="rounded-lg border bg-card p-3">
              <div className="line-clamp-2 text-sm font-medium">{l.title || l.asin || l.upc}</div>
              <div className="mt-0.5 text-xs text-muted-foreground"><span className="capitalize">{l.source}</span> · bought {l.ordered_at ?? '?'} · <span className="font-mono">{l.asin || l.upc}</span></div>
              <div className="mt-2 flex items-center gap-2">
                <span className="text-sm tabular-nums"><b>{l.received}</b>/{l.qty} in{l.returned ? ` · ${l.returned} sent back` : ''} · {money(l.unit_cost_cents)} each</span>
                {(l.left_to_receive ?? l.qty - l.received) > 0 && <><Button size="sm" className="ml-auto" onClick={() => receive(l)}>Receive</Button><Button size="sm" variant="ghost" onClick={() => closeLine(l)}>Close</Button></>}
              </div>
            </div>
          ))}
        </div>
        <div className="hidden rounded-lg border md:block">
          <Table>
            <TableHeader><TableRow>
              <TableHead>Bought</TableHead><TableHead>Item</TableHead><TableHead className="hidden md:table-cell">Order</TableHead>
              <TableHead className="text-right">Qty</TableHead><TableHead className="text-right">Unit cost</TableHead><TableHead />
            </TableRow></TableHeader>
            <TableBody>
              {data.data?.lines.map((l) => (
                <TableRow key={l.id}>
                  <TableCell className="whitespace-nowrap text-sm">{l.ordered_at ?? '—'}</TableCell>
                  <TableCell className="max-w-md"><div className="line-clamp-2 text-sm">{l.title || l.asin || l.upc}</div><div className="font-mono text-xs text-muted-foreground">{l.asin || l.upc}</div></TableCell>
                  <TableCell className="hidden text-xs md:table-cell"><div className="capitalize">{l.source}{l.account ? ` · ${l.account}` : ''}</div><div className="font-mono text-muted-foreground">{l.order_id}</div></TableCell>
                  <TableCell className="text-right tabular-nums"><b>{l.received}</b>/{l.qty}{l.returned ? <div className="text-xs text-warning-ink">{l.returned} sent back</div> : null}</TableCell>
                  <TableCell className="text-right tabular-nums">{money(l.unit_cost_cents)}</TableCell>
                  <TableCell className="text-right">
                    {(l.left_to_receive ?? l.qty - l.received) > 0 && (
                      <div className="flex justify-end gap-1">
                        <Button size="sm" onClick={() => receive(l)}>Receive</Button>
                        <Button size="sm" variant="ghost" onClick={() => closeLine(l)}>Close</Button>
                      </div>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </>)}
    </div>
  );
}

function AddPurchase({ onDone }: { onDone: () => void }) {
  const toast = useToast();
  const [f, setF] = useState({ source: 'walmart', order_id: '', date: new Date().toISOString().slice(0, 10), title: '', upc: '', asin: '', qty: '1', cost: '' });
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement>) => setF({ ...f, [k]: e.target.value });
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    try { await post('/api/purchases', { ...f, qty: Number(f.qty), unit_cost_cents: centsFromInput(f.cost) ?? 0 }); toast('Purchase added'); onDone(); }
    catch (err) { toast((err as Error).message, { tone: 'error' }); }
  };
  return (
    <Card><CardHeader className="pb-2"><CardTitle className="text-base">Add a purchase</CardTitle></CardHeader>
      <CardContent>
        <form onSubmit={submit} className="grid gap-3 sm:grid-cols-4">
          <div className="space-y-1"><Label>Store</Label><Input value={f.source} onChange={set('source')} className="h-10" /></div>
          <div className="space-y-1"><Label>Order #</Label><Input value={f.order_id} onChange={set('order_id')} className="h-10" placeholder="optional" /></div>
          <div className="space-y-1"><Label>Date</Label><Input type="date" value={f.date} onChange={set('date')} className="h-10" /></div>
          <div className="space-y-1"><Label>UPC or ASIN</Label><Input data-scan-input value={f.upc || f.asin} onChange={(e) => { const v = e.target.value.trim(); setF({ ...f, upc: /^\d+$/.test(v) ? v : '', asin: /^\d+$/.test(v) ? '' : v.toUpperCase() }); }} className="h-10 font-mono" /></div>
          <div className="space-y-1 sm:col-span-2"><Label>Item</Label><Input value={f.title} onChange={set('title')} className="h-10" required /></div>
          <div className="space-y-1"><Label>Qty</Label><Input inputMode="numeric" value={f.qty} onChange={set('qty')} className="h-10" /></div>
          <div className="space-y-1"><Label>Unit cost ($)</Label><Input inputMode="decimal" value={f.cost} onChange={set('cost')} className="h-10" /></div>
          <Button type="submit" className="h-10 sm:col-span-4 sm:w-40">Add</Button>
        </form>
      </CardContent>
    </Card>
  );
}
