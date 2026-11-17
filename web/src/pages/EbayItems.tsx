import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ExternalLink, PackagePlus, Plus } from 'lucide-react';
import { get, post } from '@/api/client';
import { Button } from '@/components/ui/button';
import { SearchInput, TapAction, tapTile, Thumb } from '@/components/app/common';
import { useToast } from '@/components/app/toast';
import { money, plural } from '@/lib/format';

/* eBay listings become items (Alex 11-12). Sam lists on eBay from her phone and never copies a SKU: Boxloft reads the
 * listing, makes the item (or asks here when the title looks like one it has), and the E box's page shows what is
 * listed but not in a box yet. One tap puts the listed units in this box. */

/** Alex 11-12 10:41 PM: an item received, in a box or on a shelf, on the Stock audit, delivered and not scanned in, or
 *  listed on Amazon or Walmart now ('listing': one with no item yet). Never another eBay listing's item. `why` says which. */
export type Candidate = { kind: 'product' | 'audit' | 'line' | 'listing'; id: number; title: string; sku: string; image_url: string | null;
  score: number; why?: string | null };
export type EbayRow = {
  listing_row: number; item_id: string; sku: string; title: string; image_url: string | null; qty: number; price_cents: number | null;
  opened_on: string | null; product_id: number | null; url: string; short?: number; in_boxes?: number; candidates?: Candidate[];
};

/** "Same as …" or "New item". Resolves to true once the listing has its item. */
function MatchButtons({ row, busy, onAnswer }: { row: EbayRow; busy: boolean; onAnswer: (body: Record<string, unknown>) => void }) {
  return (
    <div className="space-y-1.5">
      {(row.candidates ?? []).length > 0 && (
        <div className="text-xs text-muted-foreground">Looks like something you have. Same item? Link ties this listing to it.</div>
      )}
      {(row.candidates ?? []).map((c) => (
        <button key={`${c.kind}${c.id}`} type="button" disabled={busy}
          className={`flex min-h-11 w-full items-center gap-2 p-2 text-sm ${tapTile}`}
          onClick={() => onAnswer({ product: { product_id: c.id }, line: { line_id: c.id }, listing: { listing_id: c.id },
            audit: { item_id: c.id } }[c.kind])}>
          <Thumb photoId={null} url={c.image_url} />
          <span className="min-w-0 flex-1"><span className="line-clamp-2">Same as: {c.title}</span>
            <span className="block text-xs text-muted-foreground">{c.why ?? c.sku}</span></span>
          <TapAction label="Link" />
        </button>
      ))}
      <Button variant="outline" className="h-11 w-full" disabled={busy} onClick={() => onAnswer({ new: true })}>
        <Plus className="mr-1 h-4 w-4" />{(row.candidates ?? []).length ? 'No, a new item' : 'Make it a new item'}
      </Button>
    </div>
  );
}

function RowHead({ row, children }: { row: EbayRow; children?: React.ReactNode }) {
  return (
    <div className="flex items-start gap-3">
      <Thumb photoId={null} url={row.image_url} size="focus" />
      <div className="min-w-0 flex-1 text-sm">
        <div className="line-clamp-2 font-medium leading-snug">{row.title}</div>
        <div className="mt-0.5 text-xs text-muted-foreground">
          {row.qty} listed{row.in_boxes ? ` · ${row.in_boxes} in E boxes` : ''}{row.price_cents ? ` · ${money(row.price_cents)}` : ''}
          {' · '}<a href={row.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 underline">eBay<ExternalLink className="h-3 w-3" /></a>
        </div>
        {children}
      </div>
    </div>
  );
}

/** Box page (E boxes): listed on eBay, not in a box yet. `onPut` is the box page's own save (Undo, count, Change). */
export function EbayStrip({ boxName, busy, onPut }: {
  boxName: string; busy: boolean; onPut: (listingRow: number, qty: number, seenShort: number) => Promise<void>;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const [q, setQ] = useState('');
  const [open, setOpen] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const res = useQuery({
    queryKey: ['ebay-to-box', q.trim()],
    queryFn: () => get<{ listings: EbayRow[]; total: number; reading: boolean }>(`/api/ebay-items/to-box?q=${encodeURIComponent(q.trim())}`),
    refetchInterval: (query) => (query.state.data?.reading ? 5000 : false),
  });
  const rows = res.data?.listings ?? [];
  const total = res.data?.total ?? 0;
  if (!res.data || (!total && !q)) return null;

  const putRow = async (row: EbayRow) => {
    setSaving(true);
    try { await onPut(row.listing_row, row.short ?? 1, row.short ?? 1); } finally { setSaving(false); }
    void qc.invalidateQueries({ queryKey: ['ebay-to-box'] });
  };
  const answer = async (row: EbayRow, body: Record<string, unknown>) => {
    setSaving(true);
    try {
      await post(`/api/ebay-items/${row.listing_row}/match`, body);
      setOpen(null);
      await onPut(row.listing_row, row.short ?? 1, row.short ?? 1);
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Not saved', { tone: 'error' });
    } finally {
      setSaving(false);
      void qc.invalidateQueries({ queryKey: ['ebay-to-box'] });
    }
  };

  return (
    <div className="space-y-2 rounded-lg border p-3">
      <div className="flex items-start gap-2 text-sm">
        <PackagePlus className="mt-0.5 h-4 w-4 shrink-0" />
        <span><span className="font-semibold">Listed on eBay, not in a box yet</span>{' '}
          <span className="text-muted-foreground">({plural(total, 'listing')}{res.data.reading ? ', reading eBay…' : ''})</span></span>
      </div>
      {total > 6 && <SearchInput placeholder="Find a listing by name" value={q} onChange={(e) => setQ(e.target.value)} className="h-11" />}
      <div className="space-y-2">
        {rows.map((row) => (
          <div key={row.listing_row} className="rounded-lg border p-2">
            <RowHead row={row}>
              {row.product_id ? (
                <Button className="mt-2 h-11" disabled={busy || saving} onClick={() => void putRow(row)}>
                  Put {row.short} in {boxName}
                </Button>
              ) : open === row.listing_row ? null : (
                <Button variant="outline" className="mt-2 h-11" disabled={busy || saving} onClick={() => setOpen(row.listing_row)}>
                  Put in {boxName}…
                </Button>
              )}
            </RowHead>
            {!row.product_id && open === row.listing_row && (
              <div className="mt-2"><MatchButtons row={row} busy={busy || saving} onAnswer={(b) => void answer(row, b)} /></div>
            )}
          </div>
        ))}
        {!rows.length && <div className="text-sm text-muted-foreground">No listing matches "{q.trim()}".</div>}
        {total > rows.length && <div className="text-xs text-muted-foreground">Newest {rows.length} of {total} shown. Type a name to find the rest.</div>}
      </div>
    </div>
  );
}

/** Listings page (eBay): listings whose title looks like something Boxloft has. A person says which, or new. */
export function EbayToMatch() {
  const qc = useQueryClient();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const res = useQuery({ queryKey: ['ebay-to-match'], queryFn: () => get<{ listings: EbayRow[] }>('/api/ebay-items/to-match') });
  const rows = res.data?.listings ?? [];
  if (!rows.length) return null;
  const answer = async (row: EbayRow, body: Record<string, unknown>) => {
    setBusy(true);
    try {
      const r = await post<{ made: boolean }>(`/api/ebay-items/${row.listing_row}/match`, body);
      toast(r.made ? `New item made: ${row.title.slice(0, 50)}` : `Linked: ${row.title.slice(0, 50)}`);
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Not saved', { tone: 'error' });
    } finally {
      setBusy(false);
      void qc.invalidateQueries({ queryKey: ['ebay-to-match'] });
      void qc.invalidateQueries({ queryKey: ['listings'] });
    }
  };
  return (
    <div className="space-y-2 rounded-lg border p-3">
      <div className="text-sm font-semibold">eBay listings to match ({rows.length})</div>
      <div className="text-xs text-muted-foreground">Their titles look like items you have: received, in a box, on the Stock audit, delivered, or listed on Amazon or Walmart. Say which one, or new. Until then their eBay orders can't find a box.</div>
      {rows.slice(0, 20).map((row) => (
        <div key={row.listing_row} className="space-y-2 rounded-lg border p-2">
          <RowHead row={row} />
          <MatchButtons row={row} busy={busy} onAnswer={(b) => void answer(row, b)} />
        </div>
      ))}
      {rows.length > 20 && <div className="text-xs text-muted-foreground">20 of {rows.length} shown; answer these and the next ones come up.</div>}
    </div>
  );
}
