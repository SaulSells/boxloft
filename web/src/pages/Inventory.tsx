import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Plus, Download } from 'lucide-react';
import { get } from '@/api/client';
import type { Owner, ProductRow } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Switch } from '@/components/ui/switch';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Empty, PageHeader, SearchInput, Thumb } from '@/components/app/common';
import { navigate, useQueryParam, Link } from '@/lib/router';
import { money } from '@/lib/format';
import { QuickLinks } from '@/lib/links';

const MARKET: Record<string, string> = { amazon: 'Amazon', ebay: 'eBay', walmart: 'Walmart' };
const Markets = ({ m }: { m?: string[] }) => (m?.length ? <>{m.map((k) => <Badge key={k} variant="secondary" className="mr-1 text-xs">{MARKET[k] ?? k}</Badge>)}</> : null);

export function Inventory() {
  const initial = useQueryParam('q') ?? '';
  const [q, setQ] = useState(initial);
  const [debounced, setDebounced] = useState(initial);
  const [owner, setOwner] = useState('all');
  const [inStock, setInStock] = useState(true);   // sold-out products (made when a sale was recorded) stay out of the way
  const [archived, setArchived] = useState(false);   // Alex 11-11: archived products come back here to be found
  const [market, setMarket] = useState('all');   // Alex 11-12: by marketplace; an item listed on two shows under both
  const pickMarket = (m: string) => { setMarket(m); if (m !== 'all') setInStock(false); };   // listed, not boxed yet, shows too
  useEffect(() => setQ(initial), [initial]);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(q), 200);
    return () => clearTimeout(t);
  }, [q]);

  const owners = useQuery({ queryKey: ['owners'], queryFn: () => get<{ owners: Owner[] }>('/api/owners') });
  const params = new URLSearchParams({ q: debounced, ...(owner !== 'all' ? { owner_id: owner } : {}), ...(inStock ? { in_stock: '1' } : {}), ...(archived ? { archived: '1' } : {}), ...(market !== 'all' ? { market } : {}) });
  const products = useQuery({ queryKey: ['products', params.toString()], queryFn: () => get<{ products: ProductRow[] }>(`/api/products?${params}`) });
  const rows = products.data?.products ?? [];
  const totalUnits = rows.reduce((a, p) => a + p.on_hand, 0);
  const totalValue = rows.reduce((a, p) => a + (p.value_cents || 0), 0);

  return (
    <div>
      <PageHeader
        title="Inventory"
        subtitle={products.data ? `${rows.length} products · ${totalUnits.toLocaleString()} units · ${money(totalValue)} at cost` : 'Loading...'}
        actions={
          <>
            <Button variant="outline" asChild><a href="/api/export/products.csv"><Download className="mr-1.5 h-4 w-4" />CSV</a></Button>
            <Button onClick={() => navigate('/inventory/new')}><Plus className="mr-1.5 h-4 w-4" />New product</Button>
          </>
        }
      />
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <SearchInput placeholder="Search title, SKU, UPC, ASIN, FNSKU..." value={q} onChange={(e) => setQ(e.target.value)} className="h-10 w-full sm:w-auto sm:max-w-md sm:flex-1" />
        <Select value={owner} onValueChange={setOwner}>
          <SelectTrigger className="h-10 w-44"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All owners</SelectItem>
            {owners.data?.owners.map((o) => <SelectItem key={o.id} value={String(o.id)}>{o.name}</SelectItem>)}
          </SelectContent>
        </Select>
        <Select value={market} onValueChange={pickMarket}>
          <SelectTrigger className="h-10 w-44" aria-label="Marketplace"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All marketplaces</SelectItem>
            {Object.entries(MARKET).map(([k, v]) => <SelectItem key={k} value={k}>Listed on {v}</SelectItem>)}
            <SelectItem value="none">Not listed anywhere</SelectItem>
          </SelectContent>
        </Select>
        <div className="flex items-center gap-2"><Switch id="instock" checked={inStock} onCheckedChange={setInStock} /><Label htmlFor="instock">In stock only</Label></div>
        <div className="flex items-center gap-2"><Switch id="archived" checked={archived} onCheckedChange={setArchived} /><Label htmlFor="archived">Show archived</Label></div>
      </div>

      {products.data && !rows.length ? (
        <Empty>{debounced ? `Nothing matches "${debounced}".` : 'No products yet.'} <Link to={`/inventory/new${debounced ? `?code=${encodeURIComponent(debounced)}` : ''}`} className="text-primary underline">Create one</Link></Empty>
      ) : (
        <>
          <div className="space-y-2 md:hidden">
            {rows.map((p) => (
              <div key={p.id} className="rounded-lg border bg-card p-3">
                <Link to={`/inventory/${p.id}`} className="flex items-center gap-3">
                  <Thumb photoId={p.photo_id} />
                  <div className="min-w-0 flex-1">
                    <div className="line-clamp-2 text-sm font-medium">{p.archived ? <Badge variant="destructive" className="mr-1">archived</Badge> : null}{p.title}</div>
                    <div className="font-mono text-xs text-muted-foreground">{p.sku}</div>
                    {p.markets?.length ? <div className="mt-1"><Markets m={p.markets} /></div> : null}
                  </div>
                  <div className="text-right">
                    <div className="text-lg font-bold tabular-nums">{p.on_hand}</div>
                    <div className="text-xs text-muted-foreground">{money(p.avg_cost_cents)}</div>
                  </div>
                </Link>
                <QuickLinks title={p.title} ids={p.identifiers} className="mt-2 text-xs" />
              </div>
            ))}
          </div>
          <div className="hidden rounded-lg border md:block">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-14" />
                  <TableHead>Product</TableHead>
                  <TableHead>Codes</TableHead>
                  <TableHead>Owner</TableHead>
                  <TableHead className="text-right">On hand</TableHead>
                  <TableHead className="text-right">Boxes</TableHead>
                  <TableHead className="text-right">Avg cost</TableHead>
                  <TableHead className="text-right">Value</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((p) => (
                  <TableRow key={p.id} className="cursor-pointer" onClick={() => navigate(`/inventory/${p.id}`)}>
                    <TableCell><Thumb photoId={p.photo_id} size="dense" /></TableCell>
                    <TableCell className="max-w-md">
                      <div className="truncate font-medium">{p.archived ? <Badge variant="destructive" className="mr-1">archived</Badge> : null}<Markets m={p.markets} />{p.title}</div>
                      <div className="font-mono text-xs text-muted-foreground">{p.sku}{p.tracking === 'unit' && ' · one-off units'}{p.condition !== 'New' && ` · ${p.condition}`}</div>
                      <QuickLinks title={p.title} ids={p.identifiers} className="mt-0.5 text-xs" />
                    </TableCell>
                    <TableCell>
                      <div className="flex max-w-xs flex-wrap gap-1">
                        {p.identifiers.slice(0, 3).map((i) => <Badge key={i.kind + i.value} variant="outline" className="font-mono text-xs">{i.kind} {i.value}</Badge>)}
                      </div>
                    </TableCell>
                    <TableCell className="text-sm">{p.owner}</TableCell>
                    <TableCell className="text-right font-bold tabular-nums">{p.on_hand}</TableCell>
                    <TableCell className="text-right tabular-nums">{p.boxes || ''}</TableCell>
                    <TableCell className="text-right tabular-nums">{money(p.avg_cost_cents)}</TableCell>
                    <TableCell className="text-right tabular-nums">{p.value_cents ? money(p.value_cents) : ''}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </>
      )}
    </div>
  );
}
