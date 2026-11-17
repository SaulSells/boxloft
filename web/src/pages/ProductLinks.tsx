/**
 * Alex 11-12: the Crocs B0NHJH131B page showed only "Not in stock", while Orders had today's sale, the Stock audit
 * expected 1 and its order from 2025-06-18 was waiting to be received. This card puts all of it on the product
 * page: open sales to ship, the buys (and what is left to receive), the Stock audit row and the listings.
 */
import { useQuery } from '@tanstack/react-query';
import { get } from '@/api/client';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { money } from '@/lib/format';
import { Link, navigate } from '@/lib/router';
import { cn } from '@/lib/utils';
import { PackagePlus, Truck } from 'lucide-react';

type Buy = { id: number; qty: number; unit_cost_cents: number | null; cancelled: number; received: number; returned: number;
  presold: number; left_to_receive: number; source: string; order_id: string; ordered_at: string; state: string; hidden: number };
type Sale = { id: number; qty: number; price_cents: number | null; sku: string; shipped_here: number; channel: string;
  order_id: string; ordered_at: string; status: string; fulfilled_by: string; market_status: string };
type AuditRow = { id: number; asin: string; match_asin: string; expected: number; accounted: number; closed: number;
  bought: number; amazon_sold: number; returned_before: number };
type Links = { asins: string[]; buys: Buy[]; sales: Sale[]; audit: AuditRow[] };
type Listed = { channels: { channel: string; name: string; state: string; listed: number; listed_fba: number;
  listings: { id: number; sku: string; status: string; qty: number; fulfillment: string; live: boolean }[] }[] };

const CHANNEL: Record<string, string> = { amazon: 'Amazon', ebay: 'eBay', walmart: 'Walmart', shopify: 'Shopify' };
const orderUrl = (b: Buy) => (b.source === 'amazon' ? `https://www.amazon.com/your-orders/order-details?orderID=${b.order_id}` : '');

export function ProductLinks({ productId, onHand }: { productId: number; onHand: number }) {
  const links = useQuery({ queryKey: ['product-links', productId], queryFn: () => get<Links>(`/api/products/${productId}/links`) });
  const listed = useQuery({ queryKey: ['product-listed', productId], queryFn: () => get<Listed>(`/api/products/${productId}/listed`) });
  const d = links.data;
  if (!d) return null;
  const open = d.sales.filter((s) => s.status === 'open' && !s.shipped_here && s.fulfilled_by !== 'amazon');
  const done = d.sales.filter((s) => !open.includes(s));
  const buys = d.buys.filter((b) => !b.hidden);
  const left = buys.filter((b) => !b.cancelled && b.state !== 'cancelled').reduce((a, b) => a + b.left_to_receive, 0);
  const channels = (listed.data?.channels ?? []).filter((c) => c.listings.length || c.state !== 'none');
  if (!buys.length && !d.sales.length && !d.audit.length && !channels.length) return null;
  const openUnits = open.reduce((a, s) => a + s.qty, 0);

  return (
    <Card>
      <CardHeader className="pb-2"><CardTitle className="text-base">Orders, buys and audit</CardTitle></CardHeader>
      <CardContent className="space-y-4 text-sm">
        {open.length > 0 && (
          <div className="space-y-2 rounded-md border border-warning/50 bg-warning/10 p-3">
            <div className="font-medium">
              {openUnits} sold, not shipped yet{onHand < openUnits ? ` · ${onHand} on the shelves here` : ''}
            </div>
            {open.map((s) => (
              <div key={s.id} className="text-xs text-muted-foreground">
                {CHANNEL[s.channel] ?? s.channel} order <span className="font-mono">{s.order_id}</span> · {s.ordered_at} · {s.qty}
                {s.price_cents != null ? ` at ${money(s.price_cents)}` : ''}{s.market_status ? ` · ${s.market_status}` : ''}
              </div>
            ))}
            <Button size="sm" onClick={() => navigate('/orders')}><Truck className="mr-1.5 h-4 w-4" />Ship it from Orders</Button>
          </div>
        )}

        {d.audit.map((a) => (
          <div key={a.id} className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <Badge variant="outline">Stock audit</Badge>
            <span><b>{a.expected}</b> should be here ({a.bought} bought{a.amazon_sold ? `, ${a.amazon_sold} sold and shipped on Amazon` : ''}{a.returned_before ? `, ${a.returned_before} sent back` : ''})
              {' · '}{a.closed ? 'closed' : `${a.accounted} of ${a.expected} found or accounted`}</span>
            <Link to={`/audit?q=${encodeURIComponent(a.match_asin || a.asin)}`} className="text-xs text-primary underline">Open the audit row</Link>
          </div>
        ))}

        {buys.length > 0 && (
          <div className="space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">Bought</span>
              {left > 0 && <span className="text-xs text-muted-foreground">{left} left to receive</span>}
              {left > 0 && <Button size="sm" variant="outline" className="h-7 px-2" onClick={() => navigate(`/receive?product=${productId}`)}><PackagePlus className="mr-1 h-3.5 w-3.5" />Receive</Button>}
            </div>
            {buys.map((b) => {
              const url = orderUrl(b);
              const bits = [`${b.qty} bought`, b.received && `${b.received} received`, b.presold && `${b.presold} sold on Amazon before being scanned in`,
                b.returned && `${b.returned} sent back`, b.left_to_receive && `${b.left_to_receive} left`, (b.cancelled || b.state === 'cancelled') && 'cancelled'];
              return (
                <div key={b.id} className={cn('text-xs text-muted-foreground', (b.cancelled || b.state === 'cancelled') && 'line-through')}>
                  {b.ordered_at} · {url ? <a href={url} target="_blank" rel="noreferrer" className="font-mono underline">{b.order_id}</a> : <span className="font-mono">{b.order_id}</span>}
                  {b.source !== 'amazon' ? ` (${b.source})` : ''} · {bits.filter(Boolean).join(' · ')}{b.unit_cost_cents ? ` · ${money(b.unit_cost_cents)} each` : ''}
                </div>
              );
            })}
          </div>
        )}

        {done.length > 0 && (
          <div className="space-y-1">
            <div className="font-medium">Sold</div>
            {done.map((s) => (
              <div key={s.id} className="text-xs text-muted-foreground">
                {s.ordered_at} · {CHANNEL[s.channel] ?? s.channel} <span className="font-mono">{s.order_id}</span> · {s.qty}
                {s.price_cents != null ? ` at ${money(s.price_cents)}` : ''} · {s.status === 'cancelled' ? 'cancelled' : s.fulfilled_by === 'amazon' ? 'FBA' : s.shipped_here ? 'shipped from Boxloft' : 'shipped (not scanned out here)'}
              </div>
            ))}
          </div>
        )}

        {channels.length > 0 && (
          <div className="space-y-1">
            <div className="font-medium">Listings</div>
            {channels.flatMap((c) => c.listings.map((l) => (
              <div key={l.id} className="text-xs text-muted-foreground">
                {c.name} {l.fulfillment} · SKU <span className="font-mono">{l.sku}</span> · {l.live ? `${l.qty} for sale` : l.status === 'Active' ? '0 for sale' : l.status}
              </div>
            )))}
            {channels.filter((c) => !c.listings.length).map((c) => <div key={c.channel} className="text-xs text-muted-foreground">{c.name}: {c.state === 'none' ? 'not listed' : c.state}</div>)}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
