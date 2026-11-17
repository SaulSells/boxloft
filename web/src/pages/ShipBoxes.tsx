import { useState, type FormEvent } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Minus, Plus } from 'lucide-react';
import { get, post } from '@/api/client';
import type { ShipBox } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Empty, PageHeader } from '@/components/app/common';
import { useToast } from '@/components/app/toast';
import { centsFromInput, money } from '@/lib/format';
import { cn } from '@/lib/utils';

export function ShipBoxes() {
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({ queryKey: ['ship-boxes'], queryFn: () => get<{ boxes: ShipBox[] }>('/api/ship-boxes') });
  const [f, setF] = useState({ name: '', l: '', w: '', h: '', stock: '', cost: '' });
  const save = async (b: ShipBox, patch: Partial<ShipBox>) => {
    try { await post(`/api/ship-boxes/${b.id}`, { ...b, ...patch, seen: b }); qc.invalidateQueries({ queryKey: ['ship-boxes'] }); }
    catch (e) { toast((e as Error).message, { tone: 'error' }); }
  };
  const add = async (e: FormEvent) => {
    e.preventDefault();
    try {
      await post('/api/ship-boxes', { name: f.name, inner_l_in: f.l, inner_w_in: f.w, inner_h_in: f.h, stock: Number(f.stock || 0), cost_cents: centsFromInput(f.cost) });
      setF({ name: '', l: '', w: '', h: '', stock: '', cost: '' });
      qc.invalidateQueries({ queryKey: ['ship-boxes'] });
    } catch (err) { toast((err as Error).message, { tone: 'error' }); }
  };
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement>) => setF({ ...f, [k]: e.target.value });
  return (
    <div className="max-w-3xl space-y-4 sm:space-y-6">
      <PageHeader title="Ship boxes" subtitle="The mailers and boxes you pack orders in." about="Orders suggest the smallest one that fits, with half an inch of room each way." />
      <Card><CardHeader className="pb-2"><CardTitle className="text-base">Add a box size (inside measurements, inches)</CardTitle></CardHeader>
        <CardContent>
          <form onSubmit={add} className="grid grid-cols-3 gap-2 sm:grid-cols-7">
            <Input placeholder="Name" value={f.name} onChange={set('name')} className="col-span-3 h-10 sm:col-span-2" />
            <Input placeholder="L" inputMode="decimal" value={f.l} onChange={set('l')} className="h-10" required />
            <Input placeholder="W" inputMode="decimal" value={f.w} onChange={set('w')} className="h-10" required />
            <Input placeholder="H" inputMode="decimal" value={f.h} onChange={set('h')} className="h-10" required />
            <Input placeholder="On hand" inputMode="numeric" value={f.stock} onChange={set('stock')} className="h-10" />
            <Input placeholder="$ each" inputMode="decimal" value={f.cost} onChange={set('cost')} className="h-10" />
            <Button type="submit" className="col-span-3 h-10 sm:col-span-7 sm:w-32">Add</Button>
          </form>
        </CardContent>
      </Card>
      <Incoming onChange={() => qc.invalidateQueries({ queryKey: ['ship-boxes'] })} />
      {q.data && !q.data.boxes.length ? <Empty>No ship boxes yet. Add the sizes you keep on the shelf.</Empty> : (
        <div className="divide-y rounded-lg border bg-card">
          {q.data?.boxes.map((b) => (
            <div key={b.id} className="flex flex-wrap items-center gap-3 p-3">
              <div className="min-w-0 flex-1">
                <div className="font-medium">{b.name}</div>
                <div className="text-xs text-muted-foreground">{b.inner_l_in} × {b.inner_w_in} × {b.inner_h_in} in{b.cost_cents !== null ? ` · ${money(b.cost_cents)} each` : ''}</div>
                {!!b.coming && <div className="text-xs font-medium text-primary">{b.coming.toLocaleString()} coming</div>}
              </div>
              <div className="flex items-center gap-1">
                <Button size="icon" variant="outline" className="h-9 w-9" onClick={() => save(b, { stock: Math.max(0, b.stock - 1) })} aria-label="One less"><Minus className="h-4 w-4" /></Button>
                <Input inputMode="numeric" defaultValue={b.stock} key={b.stock} onBlur={(e) => { const n = Number(e.target.value); if (n !== b.stock && n >= 0) void save(b, { stock: n }); }}
                  className={cn('h-9 w-20 text-center font-bold tabular-nums', b.stock <= 5 && 'border-warning/50 text-warning-ink')} />
                <Button size="icon" variant="outline" className="h-9 w-9" onClick={() => save(b, { stock: b.stock + 1 })} aria-label="One more"><Plus className="h-4 w-4" /></Button>
              </div>
              <Button size="sm" variant="ghost" onClick={() => save(b, { archived: 1 })}>Remove</Button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

interface SupplyLine { id: number; title: string; qty: number; supply_received: number; ship_box_name: string | null }
interface SupplyOrder { id: number; store: string; order_id: string; ordered_at: string | null; status_text: string; lines: SupplyLine[] }

/** Box orders (The Boxery, read from email) not received yet. Received adds the count to that box size. */
function Incoming({ onChange }: { onChange: () => void }) {
  const toast = useToast();
  const q = useQuery({ queryKey: ['supplies-incoming'], queryFn: () => get<{ orders: SupplyOrder[] }>('/api/supplies/incoming') });
  const [qty, setQty] = useState<Record<number, string>>({});
  const orders = q.data?.orders ?? [];
  if (!orders.length) return null;
  const receive = async (l: SupplyLine) => {
    const n = Number(qty[l.id] ?? l.qty - l.supply_received);
    try {
      await post(`/api/supplies/${l.id}/receive`, { qty: n });
      toast(`${n.toLocaleString()} added${l.ship_box_name ? ` to ${l.ship_box_name}` : ''}`);
      void q.refetch(); onChange();
    } catch (e) { toast((e as Error).message, { tone: 'error' }); }
  };
  return (
    <Card><CardHeader className="pb-2"><CardTitle className="text-base">Coming in</CardTitle></CardHeader>
      <CardContent className="space-y-4">
        {orders.map((o) => (
          <div key={o.id} className="space-y-2">
            <div className="text-sm"><span className="font-semibold">{o.store}</span> <span className="text-muted-foreground">#{o.order_id} · ordered {o.ordered_at}</span></div>
            {o.lines.map((l) => {
              const left = l.qty - l.supply_received;
              return (
                <div key={l.id} className="flex flex-wrap items-center gap-2 text-sm">
                  <div className="min-w-0 flex-1">{l.ship_box_name ? <b>{l.ship_box_name}</b> : l.title}
                    <span className="text-muted-foreground"> · {left.toLocaleString()} coming{l.supply_received ? ` (${l.supply_received.toLocaleString()} in)` : ''}</span></div>
                  <Input inputMode="numeric" className="h-9 w-24 text-center" value={qty[l.id] ?? String(left)}
                    onChange={(e) => setQty({ ...qty, [l.id]: e.target.value })} aria-label="How many came in" />
                  <Button size="sm" variant="outline" onClick={() => void receive(l)}>Received</Button>
                </div>
              );
            })}
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
