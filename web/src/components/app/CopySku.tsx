import { Check, Copy } from 'lucide-react';
import { useState } from 'react';
import { ApiError, post } from '@/api/client';
import { useToast } from '@/components/app/toast';
import { day, money } from '@/lib/format';
import { cn } from '@/lib/utils';

/** The SKU to paste into Seller Central (Alex 11-24): "date it was purchased - price paid", 10.12.26-10.15. */
export type SellSku = {
  product_id: number; sku: string; saved: boolean; day: string; cost_cents: number | null; from: string;
  /** Alex 11-25: no buy with a date and a price, so the SKU is made from what there is (`from` says what). */
  estimated?: boolean;
};

/** The first copy saves the SKU on the server (it never changes after), and the clipboard gets what the server saved.
 * Safari keeps the tap's permission across the wait only through a ClipboardItem holding a promise. */
async function copySaved(saving: Promise<SellSku>): Promise<SellSku> {
  if (typeof ClipboardItem !== 'undefined' && navigator.clipboard?.write) {
    try {
      await navigator.clipboard.write([new ClipboardItem({ 'text/plain': saving.then((r) => new Blob([r.sku], { type: 'text/plain' })) })]);
      return await saving;
    } catch (e) {
      if (e instanceof ApiError) throw e;   // the save itself failed: say why
    }
  }
  const r = await saving;
  if (!(await copyText(r.sku))) throw new Error(`Could not copy. The SKU is ${r.sku}`);
  return r;
}

/** Copies text on an iPhone too: the Clipboard API on https, else a selected text box (older Safari, plain http). */
async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const t = document.createElement('textarea');
    t.value = text;
    t.setAttribute('readonly', '');
    t.style.position = 'fixed';
    t.style.opacity = '0';
    document.body.appendChild(t);
    t.select();
    t.setSelectionRange(0, text.length);
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    t.remove();
    return ok;
  }
}

/** One tap saves and copies the SKU. The tooltip and the toast say which buy it came from. */
export function CopySku({ s, className }: { s: SellSku | null | undefined; className?: string }) {
  const toast = useToast();
  const [done, setDone] = useState(false);
  const [shown, setShown] = useState<SellSku | null>(null);
  if (!s) return null;
  const cur = shown && shown.product_id === s.product_id ? shown : s;
  const why = (x: SellSku) => x.estimated || x.cost_cents == null
    ? `No buy with a date and a price on file. Made from ${day(x.day)}${x.cost_cents != null ? `, ${money(x.cost_cents)}` : ''} (${x.from})`
    : `Bought ${day(x.day)} for ${money(x.cost_cents)}${x.from ? ` (${x.from})` : ''}`;
  return (
    <button type="button" title={why(cur)}
      onClick={async (e) => {
        e.stopPropagation();
        try {
          const r = await copySaved(post<SellSku>(`/api/products/${s.product_id}/sell-sku`, {}));
          setShown(r);
          setDone(true);
          window.setTimeout(() => setDone(false), 2000);
          toast(`Copied SKU ${r.sku}${r.sku !== cur.sku ? ` (${cur.sku} was taken meanwhile)` : ''}. ${why(r)}.`);
        } catch (err) {
          toast((err as Error).message, { tone: 'error' });
        }
      }}
      className={cn('inline-flex min-h-8 items-center gap-1 rounded-md border px-2 font-mono text-xs text-primary hover:bg-muted', className)}>
      {done ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
      <span className="font-sans">Copy SKU</span> {cur.sku}
    </button>
  );
}
