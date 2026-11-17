import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Combine } from 'lucide-react';
import { ApiError, get, post } from '@/api/client';
import type { Product as ProductT } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { SearchInput, Thumb, tapTile } from '@/components/app/common';
import { useToast } from '@/components/app/toast';
import { navigate } from '@/lib/router';

type Found = { products: { id: number; sku: string; title: string; on_hand: number; photo_id: number | null }[];
  rows: { id: number; title: string; image_url: string | null; expected: number; accounted: number }[] };
type Target = { product_id?: number; audit_id?: number; title: string };
type Preview = { source: { on_hand: number }; target_id: number; row: { id: number; asin: string; left: number } | null;
  to_row: number; to_lines: number; extra: number; as_is: number };

/** Alex 11-17 "we need to be able to merge items": a scan made a second item for something already in Boxloft (or on
 *  the Stock audit under another ASIN). Pick what it really is: its barcodes, open orders and units move there, the
 *  Stock audit row counts the units as found, and this item is archived. One save, one Undo. */
export function MergeDialog({ p, onClose }: { p: ProductT; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [q, setQ] = useState('');
  const [target, setTarget] = useState<Target | null>(null);
  const [busy, setBusy] = useState(false);
  const found = useQuery({
    queryKey: ['merge-search', q.trim()], enabled: q.trim().length >= 2,
    queryFn: () => get<Found>(`/api/box-add/search?q=${encodeURIComponent(q.trim())}`),
  });
  const key = target ? (target.product_id ? `product_id=${target.product_id}` : `audit_id=${target.audit_id}`) : '';
  const preview = useQuery({ queryKey: ['merge-preview', p.id, key], enabled: !!target,
    queryFn: () => get<Preview>(`/api/products/${p.id}/merge-preview?${key}`) });
  const pv = preview.data;
  const merge = async () => {
    if (!target) return;
    setBusy(true);
    try {
      const r = await post<{ merge_id: number; target_id: number }>(`/api/products/${p.id}/merge`, {
        target: { product_id: target.product_id ?? null, audit_id: target.audit_id ?? null }, seen: { on_hand: p.on_hand, target_id: pv?.target_id, to_row: pv?.to_row, to_lines: pv?.to_lines, extra: pv?.extra } });
      qc.invalidateQueries();
      toast(`Merged into ${target.title.slice(0, 40)}.`, {
        undo: async () => {
          await post(`/api/merges/${r.merge_id}/undo`);
          qc.invalidateQueries();
          navigate(`/inventory/${p.id}`);
          toast('Undone: the two are apart again.');
        },
      });
      onClose();
      if (r.target_id !== p.id) navigate(`/inventory/${r.target_id}`);
    } catch (e) {
      toast(e instanceof ApiError ? e.message : 'Merge failed', { tone: 'error' });
      if (e instanceof ApiError && e.status === 409) { qc.invalidateQueries({ queryKey: ['product', p.id] }); qc.invalidateQueries({ queryKey: ['merge-preview', p.id] }); }
    } finally {
      setBusy(false);
    }
  };
  const products = (found.data?.products ?? []).filter((x) => x.id !== p.id);
  const rows = found.data?.rows ?? [];
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>Same item as...</DialogTitle></DialogHeader>
        {!target ? (
          <div className="space-y-3">
            <p className="text-sm text-muted-foreground">Find the item or Stock audit row this really is. This one's barcodes, open orders and {p.on_hand} on hand move there.</p>
            <SearchInput autoFocus placeholder="Type a name, SKU or ASIN" value={q} onChange={(e) => setQ(e.target.value)} className="h-11" />
            {q.trim().length >= 2 && !found.isLoading && !products.length && !rows.length && <p className="text-sm text-muted-foreground">Nothing matches.</p>}
            <div className="space-y-2">
              {products.map((x) => (
                <button key={`p${x.id}`} type="button" className={`${tapTile} flex w-full items-center gap-3 p-2 text-left`}
                  onClick={() => setTarget({ product_id: x.id, title: x.title })}>
                  <Thumb photoId={x.photo_id} size="dense" />
                  <span className="min-w-0 flex-1"><span className="line-clamp-2 text-sm">{x.title}</span>
                    <span className="block text-xs text-muted-foreground">{x.sku} · {x.on_hand} on hand</span></span>
                </button>
              ))}
              {rows.map((r) => (
                <button key={`r${r.id}`} type="button" className={`${tapTile} flex w-full items-center gap-3 p-2 text-left`}
                  onClick={() => setTarget({ audit_id: r.id, title: r.title })}>
                  {r.image_url ? <img src={r.image_url} alt="" className="h-10 w-10 rounded object-cover" /> : <Thumb photoId={null} size="dense" />}
                  <span className="min-w-0 flex-1"><span className="line-clamp-2 text-sm">{r.title}</span>
                    <span className="block text-xs text-muted-foreground">Stock audit · {Math.max(0, r.expected - r.accounted)} to find</span></span>
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="space-y-3 text-sm">
            <p><b>{p.title.slice(0, 60)}</b> becomes <b>{target.title.slice(0, 60)}</b>.</p>
            {preview.isLoading ? <p className="text-muted-foreground">Checking...</p> : preview.error ? (
              <p className="text-destructive">{(preview.error as Error).message}</p>
            ) : pv && (
              <ul className="list-disc space-y-1 pl-5">
                <li>{pv.source.on_hand} on hand stay in the same boxes, saved as that item.</li>
                {pv.row && <li>The Stock audit row counts {pv.to_row} of them as found ({pv.row.left} left to find).</li>}
                {pv.to_lines > 0 && <li>{pv.to_lines} go on its open orders as received.</li>}
                {pv.as_is > 0 && pv.target_id !== p.id && <li>{pv.as_is} came in on an order or the Stock audit already: they move over as they are, counted once.</li>}
                <li>Its barcodes and open orders go with it{pv.target_id !== p.id ? ', and this item is archived' : ''}.</li>
              </ul>
            )}
            <p className="text-muted-foreground">One save. Undo puts it back.</p>
          </div>
        )}
        <DialogFooter className="gap-2">
          {target && <Button variant="outline" onClick={() => setTarget(null)}>Back</Button>}
          {target && <Button disabled={busy || !pv} onClick={() => void merge()}><Combine className="mr-1.5 h-4 w-4" />{busy ? 'Merging...' : 'Merge'}</Button>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
