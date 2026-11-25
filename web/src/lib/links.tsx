import { ExternalLink } from 'lucide-react';
import type { Identifier } from '@/api/types';
import { cn } from '@/lib/utils';

/** Outside links for a product: its Amazon listing, eBay sold listings, Keepa, and the orders it came from. */

export const isAmazonOrder = (id: string | null | undefined) => !!id && /^\d{3}-\d{7}-\d{7}$/.test(id.trim());
export const amazonOrderUrl = (id: string) => `https://www.amazon.com/your-orders/order-details?orderID=${id.trim()}`;

/** Seller Central's "Add offer" page for an ASIN (price, SKU, condition). Checked 11-24: /abis/listing/syh?asin= lands
 * on /interactive/listing/workflow/offer/offer?asin=. Alex 11-24: on Listings rows and on the item page. */
export const sellUrl = (asin: string) => `https://sellercentral.amazon.com/abis/listing/syh?asin=${encodeURIComponent(asin)}`;

function asinOf(ids: Identifier[]): string | undefined {
  return ids.find((i) => i.kind === 'ASIN')?.value;
}

/** eBay search words: the title without bracketed bits and pack counts, first 10 words. A UPC alone if there's no title. */
export function ebayQuery(title: string, ids: Identifier[]): string {
  const t = title.replace(/\([^)]*\)|\[[^\]]*\]/g, ' ').replace(/[|,–—-]+/g, ' ').replace(/\s+/g, ' ').trim();
  if (t) return t.split(' ').slice(0, 10).join(' ');
  return ids.find((i) => i.kind === 'UPC' || i.kind === 'EAN')?.value ?? '';
}

export function productLinks(title: string, ids: Identifier[]) {
  const asin = asinOf(ids);
  const q = ebayQuery(title, ids);
  return [
    asin && { label: 'Amazon', href: `https://www.amazon.com/dp/${asin}` },
    q && { label: 'eBay sold', href: `https://www.ebay.com/sch/i.html?_nkw=${encodeURIComponent(q)}&LH_Sold=1&LH_Complete=1&_sop=13` },
    asin && { label: 'Keepa', href: `https://keepa.com/#!product/1-${asin}` },
    asin && { label: 'List in Seller Central', href: sellUrl(asin) },
  ].filter(Boolean) as { label: string; href: string }[];
}

export function OutLink({ href, children, className }: { href: string; children: React.ReactNode; className?: string }) {
  return (
    <a href={href} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}
      className={cn('inline-flex items-center gap-0.5 hover:underline', className)}>
      {children}<ExternalLink className="h-3 w-3" />
    </a>
  );
}

/** Amazon, eBay sold, Keepa, List in Seller Central, then one link per Amazon order the product was bought on. */
export function QuickLinks({ title, ids, orders = [], className }: { title: string; ids: Identifier[]; orders?: string[]; className?: string }) {
  const links = productLinks(title, ids);
  const amz = [...new Set(orders.filter(isAmazonOrder).map((o) => o.trim()))];
  if (!links.length && !amz.length) return null;
  return (
    <div className={cn('flex flex-wrap items-center gap-x-3 gap-y-1 text-sm', className)}>
      {links.map((l) => <OutLink key={l.label} href={l.href} className="text-primary">{l.label}</OutLink>)}
      {amz.length > 0 && <span className="text-muted-foreground">{amz.length === 1 ? 'Order' : 'Orders'}:</span>}
      {amz.map((o) => <OutLink key={o} href={amazonOrderUrl(o)} className="font-mono text-xs text-primary">{o}</OutLink>)}
    </div>
  );
}
