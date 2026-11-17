import { useState } from 'react';
import { ExternalLink, ShieldAlert, ShieldCheck } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

/* Alex 11-13: "I'd like Sam to know when she scans an item to box it if we can sell it or not." The server asks
 * Amazon (SP-API Listings Restrictions, cached per ASIN) and every scan answer carries `amazon`. A no in an A box is
 * a warning with the eBay box to move it to; it never blocks the save (Sam's call). */

export type AmazonOk = {
  asin: string; state: 'ok' | 'approval' | 'not_eligible' | 'not_found' | 'restricted' | 'no_asin' | 'unknown';
  can_sell: boolean | null; text: string; detail: string; apply_url: string; checked_at: string | null;
  in_a_box?: boolean; other_box?: { id: number; code: string; units: number } | null;
};

/** An A box by its name, as sorting.market_of reads it (A31, A-31). */
export const isABox = (name: string | null | undefined) => /^A-?\d+$/i.test((name ?? '').trim());

/** One line on a scan card or a just-put-in line. `inA` overrides the server's in_a_box (cards shown before a box is
 * known). `onMove` moves what was just saved to the eBay box Amazon's no suggests. */
export function AmazonNote({ a, inA, boxName, onMove, compact, pre }: {
  a: AmazonOk | undefined | null; inA?: boolean; boxName?: string | null; pre?: boolean;
  onMove?: (box: { id: number; code: string }) => Promise<void> | void; compact?: boolean;
}) {
  /* pre: no box picked yet (Put page): the warning says which box not to use. */
  const [busy, setBusy] = useState(false);
  if (!a || a.state === 'no_asin' || a.state === 'unknown') return null;
  const inABox = pre || (inA ?? !!a.in_a_box);
  if (a.can_sell) {
    return (
      <div className="flex items-center gap-1.5 text-xs font-medium text-success-ink">
        <ShieldCheck className="h-3.5 w-3.5 shrink-0" />{a.text}
      </div>
    );
  }
  if (!inABox) {   // not going to Amazon anyway: say it quietly
    return (
      <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <ShieldAlert className="h-3.5 w-3.5 shrink-0" />{a.text}
      </div>
    );
  }
  const move = a.other_box && onMove;
  return (
    <div className={cn('space-y-2 rounded-md border border-warning/60 bg-warning/15 px-2.5 py-2 text-sm', compact && 'py-1.5')} role="alert">
      <div className="flex gap-2">
        <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
        <div className="min-w-0 flex-1">
          <b>{a.text}.</b>{' '}
          {pre ? 'Not an A box: ' : boxName ? `${boxName} is an Amazon box. ` : 'This is an Amazon box. '}
          {a.other_box ? `${pre ? 'p' : 'Sell it on eBay instead: p'}ut it in ${a.other_box.code}.` : `${pre ? 's' : 'S'}ell it on eBay or Walmart instead.`}
          {a.detail && !compact && <div className="mt-0.5 text-xs text-muted-foreground">Amazon: {a.detail}</div>}
        </div>
      </div>
      {(move || a.apply_url) && (
        <div className="flex flex-wrap gap-2">
          {move && (
            <Button size="sm" className="h-9" disabled={busy}
              onClick={async () => { setBusy(true); try { await onMove!(a.other_box!); } finally { setBusy(false); } }}>
              Move it to {a.other_box!.code}
            </Button>
          )}
          {a.apply_url && (
            <Button size="sm" variant="outline" className="h-9" asChild>
              <a href={a.apply_url} target="_blank" rel="noreferrer"><ExternalLink className="mr-1 h-3.5 w-3.5" />Ask Amazon for approval</a>
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
