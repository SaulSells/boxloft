import { useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ExternalLink, RefreshCw, Search } from 'lucide-react';
import { ApiError, get, post } from '@/api/client';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Chips, Empty, PageHeader, SearchInput, Stat, TapAction, tapTile, Thumb, SectionTitle } from '@/components/app/common';
import { useToast } from '@/components/app/toast';
import { Link } from '@/lib/router';
import { ago, money, plural, when } from '@/lib/format';
import { sellUrl } from '@/lib/links';
import { CopySku, type SellSku } from '@/components/app/CopySku';
import { cn } from '@/lib/utils';
import type { AmazonOk } from '@/components/app/AmazonNote';
import { EbayToMatch } from '@/pages/EbayItems';

interface Listing {
  id: number; channel: string; sku: string; asin: string; title: string; fulfillment: 'FBM' | 'FBA'; status: string; qty: number;
  price_cents: number | null; opened_on: string | null; condition: string; fba_inbound: number; fba_reserved: number;
  listing_id: string; code_type: string; code: string;   // eBay: listing_id = item ID, code = Custom label (code_type 'SKU')
  fba_unfulfillable: number; product_id: number | null; audit_id: number | null; image_url: string | null;
  on_hand: number;          // in every box
  on_hand_market: number;   // in this channel's own boxes (A boxes for Amazon)
  confirmed: number; gone_at: string | null;
  live: boolean;   // for sale right now, decided by the server (listings.is_live)
}
interface Manual { id: number; item_id: number; platform: string; platform_label: string; qty: number; url: string; note: string; title: string; asin: string; at: string }
interface Summary { listings: number; active_fbm: number; active_fba: number; units_fbm: number; units_fba: number; units_fba_inbound: number; fbm_over: number; manual: number }
interface Sync { running?: boolean; text?: string; last_run_at?: string; last_ok_at?: string; last_error?: string | null; connected?: boolean }
interface Res { listings: Listing[]; manual: Manual[]; summary: Summary; sync: Sync; syncs?: Record<string, Sync> }
/** Markets whose listings Boxloft reads (server LISTING_CHANNELS). */
const READ = new Set(['amazon', 'ebay']);

/** Stock in a market's boxes against that market's listings (listings.stock_vs_listed). */
type Reason = 'not_listed' | 'inactive' | 'no_asin' | 'short' | 'listed';
interface Suggestion { listing_id: number; sku: string; asin: string; title: string; fulfillment: string; status: string; qty: number; price_cents: number | null; live: boolean; score: number }
interface StockRow {
  product_id: number; sku: string; title: string; asin: string; updated_at: string; photo_id: number | null; image_url: string | null;
  on_hand: number; boxes: { name: string; qty: number; box_id: number | null }[]; listed: number; listed_fba: number; unlisted: number;
  reason: Reason; suggestions: Suggestion[];
  approval?: AmazonOk | null;   // Amazon market only: may we sell this ASIN (Alex 11-13)
  sell_sku?: SellSku | null;     // Amazon market only: the SKU to paste into Seller Central (Alex 11-24)
}
interface Group { products: number; units: number; unlisted?: number }
interface StockRes {
  channel: string; name: string; letter: string; synced: boolean; rows: StockRow[];
  summary: Record<Reason | 'all' | 'none_for_sale', Group> & { not_approved?: Group };
}

const MARKETS = [{ key: 'amazon', name: 'Amazon' }, { key: 'ebay', name: 'eBay' }, { key: 'walmart', name: 'Walmart' }] as const;
type Market = (typeof MARKETS)[number]['key'];

const live = (l: Listing) => l.live;
const FILTERS: { key: string; label: string; match: (l: Listing) => boolean }[] = [
  { key: 'live', label: 'For sale now', match: live },
  { key: 'over', label: 'Listed, not enough here', match: (l) => l.fulfillment === 'FBM' && live(l) && l.qty > l.on_hand },
  { key: 'noaudit', label: 'Not in the audit', match: (l) => live(l) && !l.audit_id },
  { key: 'fba', label: 'FBA', match: (l) => l.fulfillment === 'FBA' && (l.qty > 0 || l.fba_inbound > 0) },
  { key: 'off', label: 'Inactive or 0', match: (l) => !live(l) },
  { key: 'all', label: 'All', match: () => true },
];
/** The two views that start from our boxes, not from the listings. */
const STOCK: Record<string, (r: StockRow) => boolean> = {
  unlisted: (r) => r.reason === 'not_listed' || r.reason === 'inactive' || r.reason === 'no_asin',
  short: (r) => r.reason === 'short',
  noamazon: (r) => r.approval?.can_sell === false,
};
const WHY: Record<Reason, string> = {
  not_listed: 'No listing', inactive: 'Listing inactive or at 0', no_asin: 'No ASIN on the product', short: 'Listed short', listed: 'Listed',
};
const PAGE = 60;


type Recheck = { asked: number; failed: number; now_ok: string[]; still: number; left: number; checked_at: string };
type RecheckState = { run: string; running: boolean; result?: Recheck; error?: string; unknown?: boolean };

/** The phone lost the POST's connection (Alex 11-24: "Load failed", though the check worked): the server keeps going,
 * so read its answer by run id. null when the server has no trace of it (never reached it, or restarted since). */
async function waitForRun(run: string): Promise<Recheck | string | null> {
  for (let i = 0; i < 90; i++) {
    await new Promise((res) => setTimeout(res, 2000));
    try {
      const st = await get<RecheckState>(`/api/amazon/approval/recheck?run=${encodeURIComponent(run)}`);
      if (!st.unknown && !st.running) return st.result ?? st.error ?? 'The check stopped. Try again.';
      if (st.unknown && i >= 4) return null;
    } catch { /* still no signal: try again */ }
  }
  return null;
}

/** Check again (Alex 11-24): Amazon is asked now, whatever the cache says. No asins = the whole gated list. */
async function askAmazon(asins: string[] | null, toast: ReturnType<typeof useToast>) {
  const run = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
  try {
    let r: Recheck;
    try {
      r = await post<Recheck>('/api/amazon/approval/recheck', { ...(asins ? { asins } : {}), run });
    } catch (e) {
      if (e instanceof ApiError) throw e;
      const got = await waitForRun(run);
      if (got === null) throw new Error('Lost the connection, so Boxloft cannot tell what Amazon said. The list reloads: check it, or press again.');
      if (typeof got === 'string') throw new Error(got);
      r = got;
    }
    const noAnswer = (r.failed ? ` ${plural(r.failed, 'item')} got no answer: try again.` : '') + (r.left ? ` ${r.left} more not asked yet: press again.` : '');
    if (asins?.length === 1) toast(r.failed ? 'Amazon did not answer. Try again.' : r.still ? 'Asked Amazon: still gated.' : 'Asked Amazon: you may sell it now.');
    else toast(`Asked Amazon about ${plural(r.asked, 'item')}: ${r.now_ok.length ? `${r.now_ok.length} you may sell now` : 'none opened up'}, ${r.still} still gated.${noAnswer}`);
  } catch (e) {
    toast((e as Error).message, { tone: 'error' });
  }
}

export function Listings() {
  const qc = useQueryClient();
  const toast = useToast();
  const [market, setMarket] = useState<Market>('amazon');
  const [filter, setFilter] = useState('live');
  const [q, setQ] = useState('');
  const [limit, setLimit] = useState(PAGE);
  const res = useQuery({
    queryKey: ['listings'], queryFn: () => get<Res>('/api/listings'),
    refetchInterval: (query) => (Object.values(query.state.data?.syncs ?? {}).some((x) => x?.running) ? 3000 : false),
  });
  const stock = useQuery({ queryKey: ['listings-stock', market], queryFn: () => get<StockRes>(`/api/listings/stock?channel=${market}`) });
  const name = MARKETS.find((m) => m.key === market)!.name;
  const s = res.data?.summary;
  const sync: Sync | undefined = market === 'amazon' ? res.data?.sync : res.data?.syncs?.[market];
  const reads = READ.has(market) && (market === 'amazon' || !!sync?.connected || !!sync?.last_ok_at);
  const ss = stock.data?.summary;
  const mine = useMemo(() => (res.data?.listings ?? []).filter((l) => l.channel === market), [res.data, market]);
  const words = q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const hit = (text: string) => words.every((w) => text.toLowerCase().includes(w));
  const fromStock = filter in STOCK;
  const rows = useMemo(() => {
    const f = FILTERS.find((x) => x.key === filter);
    return f ? mine.filter((l) => f.match(l) && hit(`${l.title} ${l.sku} ${l.asin}`)) : [];
  }, [mine, filter, q]);   // eslint-disable-line react-hooks/exhaustive-deps
  const stockRows = useMemo(
    () => (fromStock ? (stock.data?.rows ?? []).filter((r) => STOCK[filter]!(r) && hit(`${r.title} ${r.sku} ${r.asin} ${r.boxes.map((b) => b.name).join(' ')}`)) : []),
    [stock.data, filter, q]);   // eslint-disable-line react-hooks/exhaustive-deps
  const count = (key: string) => mine.filter(FILTERS.find((x) => x.key === key)!.match).length;
  const chips = [
    { key: 'live', label: 'For sale now', count: count('live') },
    { key: 'unlisted', label: `In ${name} boxes, not listed`, count: ss?.none_for_sale.products },
    { key: 'short', label: 'Listed fewer than we have', count: ss?.short.products },
    ...(market === 'amazon' ? [{ key: 'noamazon', label: "In A boxes, Amazon won't let us sell", count: ss?.not_approved?.products }] : []),
    ...FILTERS.slice(1).map((f) => ({ key: f.key, label: f.label, count: count(f.key) })),
  ];
  const pickMarket = (m: Market) => { setMarket(m); setLimit(PAGE); if (m !== 'amazon') setFilter('unlisted'); };
  const refresh = async () => {
    try {
      await post('/api/listings/refresh', { channel: market });
      toast(market === 'amazon' ? 'Asking Amazon. The report takes a few minutes.' : `Reading ${name} listings.`);
      void qc.invalidateQueries({ queryKey: ['listings'] });
    } catch (e) { toast((e as Error).message, { tone: 'error' }); }
  };
  const [checking, setChecking] = useState(false);
  // The oldest answer on the gated list: how stale "still gated" may be.
  const gatedAsked = (stock.data?.rows ?? []).filter((r) => r.approval?.can_sell === false).map((r) => r.approval?.checked_at ?? '').filter(Boolean).sort()[0] ?? null;
  const reload = () => { void qc.invalidateQueries({ queryKey: ['listings'] }); void qc.invalidateQueries({ queryKey: ['listings-stock'] }); };
  const total = fromStock ? stockRows.length : rows.length;
  return (
    <div className="max-w-5xl space-y-4">
      <PageHeader
        title="Listings"
        subtitle="What is for sale, next to what we have in the boxes."
        about="Amazon and eBay listings are read every hour. A box's letter is its market: A for Amazon, E for eBay, W for Walmart. An item counts as listed when a listing we ship is active with 1 or more for sale. Items listed on other sites are marked by hand on the Stock audit."
        actions={reads && <Button variant="outline" onClick={refresh} disabled={sync?.running}><RefreshCw className={cn('mr-1.5 h-4 w-4', sync?.running && 'animate-spin')} />{sync?.running ? 'Reading…' : 'Refresh now'}</Button>}
      />
      <div className="grid grid-cols-3 gap-1.5 sm:max-w-sm" role="tablist" aria-label="Market">
        {MARKETS.map((m) => (
          <button key={m.key} type="button" role="tab" aria-selected={market === m.key} onClick={() => pickMarket(m.key)}
            className={cn('h-11 rounded-md border text-sm font-medium', market === m.key ? 'border-primary bg-primary text-primary-foreground' : 'bg-card hover:bg-accent')}>
            {m.name}
          </button>
        ))}
      </div>
      {reads ? (
        <div className="text-xs text-muted-foreground">
          {sync?.running ? sync.text : sync?.last_ok_at ? `Read from ${name} ${ago(sync.last_ok_at)}` : `Not read from ${name} yet`}
          {sync?.last_error && !sync.running && <span className="text-destructive"> · Last try failed: {sync.last_error}</span>}
        </div>
      ) : stock.data && !stock.data.synced && (
        <div className="rounded-lg border bg-muted/40 px-3 py-2 text-sm">
          Boxloft does not read {name} listings yet{market === 'ebay' ? ' (connect eBay in Settings, Connections)' : ''}. This shows what sits in {stock.data.letter} boxes: {plural(stock.data.summary.all.products, 'product')}, {plural(stock.data.summary.all.units, 'unit')}.
        </div>
      )}
      {market === 'ebay' && <EbayToMatch />}
      {s && market === 'amazon' && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat label="Amazon FBM" value={s.active_fbm} hint={`${plural(s.units_fbm, 'unit')} for sale`} />
          <Stat label="Amazon FBA" value={s.active_fba} hint={`${plural(s.units_fba, 'unit')} at Amazon${s.units_fba_inbound ? `, ${s.units_fba_inbound} on the way` : ''}`} />
          <Stat label="In Amazon boxes, not listed" value={ss ? ss.none_for_sale.products : '…'} tone={ss?.none_for_sale.products ? 'warn' : undefined}
            hint={ss ? `${plural(ss.none_for_sale.units, 'unit')} with nothing for sale. ${ss.short.products ? `${plural(ss.short.products, 'more product')} listed ${ss.short.unlisted} short.` : ''}` : ''} />
          <Stat label="Not enough on our shelves" value={s.fbm_over} tone={s.fbm_over ? 'warn' : undefined} hint="Listings we ship that offer more than Boxloft has in any box." />
        </div>
      )}
      {market === 'amazon' && !!ss?.not_approved?.products && filter !== 'noamazon' && (
        <button type="button" className="min-h-11 w-full rounded-lg border border-warning/60 bg-warning/15 px-3 py-2 text-left text-sm"
          onClick={() => { setFilter('noamazon'); setLimit(PAGE); }}>
          <b>{plural(ss.not_approved.products, 'item')} in A boxes that Amazon won't let us sell</b> ({plural(ss.not_approved.units, 'unit')}). Tap to see them and where they are.
        </button>
      )}
      <Chips items={chips} value={filter} onChange={(k) => { setFilter(k); setLimit(PAGE); }} />
      <SearchInput placeholder={fromStock ? 'Search name, SKU, ASIN or box' : 'Search name, SKU or ASIN'} value={q} onChange={(e) => { setQ(e.target.value); setLimit(PAGE); }} />
      {fromStock && ss && stock.data?.synced && !!stockRows.length && !q && (
        <p className="text-sm text-muted-foreground">
          {filter === 'noamazon'
            ? `${plural(ss.not_approved?.products ?? 0, 'product')}, ${plural(ss.not_approved?.units ?? 0, 'unit')} in A boxes that Amazon won't let us sell new. Move them to eBay boxes, or ask Amazon for approval where it takes applications. Amazon is asked by itself every day${gatedAsked ? `; oldest answer ${when(gatedAsked)}` : ''}.`
            : filter === 'unlisted'
            ? `${plural(ss.none_for_sale.products, 'product')}, ${plural(ss.none_for_sale.units, 'unit')} in ${name} boxes with nothing for sale: ${ss.not_listed.products} with no listing, ${ss.inactive.products} inactive or at 0${market === 'amazon' ? `, ${ss.no_asin.products} with no ASIN` : ''}.`
            : `${plural(ss.short.products, 'product')} with ${plural(ss.short.unlisted ?? 0, 'unit')} in ${name} boxes that the listing does not offer.`}
        </p>
      )}
      {filter === 'noamazon' && !!ss?.not_approved?.products && (
        <Button variant="outline" className="h-11 w-full" disabled={checking} onClick={async () => { setChecking(true); await askAmazon(null, toast); setChecking(false); reload(); }}>
          <RefreshCw className={cn('mr-1.5 h-4 w-4', checking && 'animate-spin')} />
          {checking ? `Asking Amazon about ${plural(ss.not_approved.products, 'item')}…` : `Check all ${ss.not_approved.products} with Amazon now`}
        </Button>
      )}
      {(fromStock ? stock.isLoading : res.isLoading) && <Empty>Loading…</Empty>}
      {(fromStock ? stock.data : res.data) && !total && (
        <Empty>{fromStock
          ? (filter === 'noamazon' ? 'Every item in A boxes that Amazon has answered for, we may sell.' : filter === 'unlisted' ? `Nothing in ${name} boxes is waiting for a listing.` : `No item in ${name} boxes is listed short.`)
          : market !== 'amazon' ? `No ${name} listings yet. Boxloft does not read them yet.`
            : res.data!.listings.length ? 'Nothing here.' : 'No listings yet. Connect Amazon in Settings, then Refresh now.'}</Empty>
      )}
      {!!total && (
        <div className="divide-y rounded-lg border bg-card">
          {fromStock
            ? stockRows.slice(0, limit).map((r) => <StockLine key={r.product_id} r={r} name={name} market={market} onChange={reload} />)
            : rows.slice(0, limit).map((l) => <Row key={l.id} l={l} name={name} />)}
        </div>
      )}
      {total > limit && <Button variant="outline" className="h-11 w-full" onClick={() => setLimit(limit + PAGE)}>Show {Math.min(PAGE, total - limit)} more of {total - limit}</Button>}
      {!!res.data?.manual.length && (
        <div className="space-y-2">
          <SectionTitle>Listed on other sites (marked by hand)</SectionTitle>
          <div className="divide-y rounded-lg border bg-card">
            {res.data.manual.map((m) => (
              <div key={m.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 p-3 text-sm">
                <Badge variant="outline">{m.platform_label}</Badge>
                <span className="min-w-0 flex-1 truncate">{m.title || m.asin}</span>
                <span className="tabular-nums">{m.qty} listed</span>
                {m.url && <a href={m.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 text-xs underline">listing<ExternalLink className="h-3 w-3" /></a>}
                <Link to={`/audit?q=${encodeURIComponent(m.asin.startsWith('LINE-') ? m.title : m.asin)}`} className="text-xs underline">audit</Link>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/** A product in the market's boxes: what is here, what is listed, and why it is not (fully) for sale. */
function StockLine({ r, name, market, onChange }: { r: StockRow; name: string; market: Market; onChange: () => void }) {
  const toast = useToast();
  const [checking, setChecking] = useState(false);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const link = async (sg: Suggestion) => {
    setBusy(true);
    try {
      // seen: the product as this screen showed it. A newer save is refused (409) and the screen reloads.
      const k = await post<{ link_id: number; asin: string }>('/api/listings/link', {
        product_id: r.product_id, listing_id: sg.listing_id, seen: { updated_at: r.updated_at, asin: r.asin },
      });
      setOpen(false);
      toast(k.asin ? `Linked. ASIN ${k.asin} is on the product now.` : `Linked. Its ${name} sales find this item now.`, {
        undo: async () => {
          try { await post(`/api/listings/link/${k.link_id}/undo`, {}); } catch (e) { toast((e as Error).message, { tone: 'error' }); }
          onChange();
        },
      });
    } catch (e) { toast((e as Error).message, { tone: 'error' }); }
    setBusy(false);
    onChange();
  };
  return (
    <div className="space-y-2 p-3">
      <div className="flex gap-3">
        <Link to={`/inventory/${r.product_id}`} className="shrink-0"><Thumb photoId={r.photo_id} url={r.image_url} /></Link>
        <div className="min-w-0 flex-1 space-y-1">
          <Link to={`/inventory/${r.product_id}`} className="line-clamp-2 text-sm font-medium hover:underline">{r.title}</Link>
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
            <Badge variant="outline" className={cn('text-xs', r.reason !== 'listed' && 'border-warning/60 text-warning-ink')}>{WHY[r.reason]}</Badge>
            {r.asin && <span className="font-mono">{r.asin}</span>}
            {r.listed_fba > 0 && <span>{r.listed_fba} for sale at {name} FBA</span>}
            {r.approval?.can_sell === false && (
              <Badge variant="outline" className="border-destructive/60 text-xs text-destructive">{r.approval.text}</Badge>
            )}
            {r.approval?.can_sell === false && r.approval.apply_url && (
              <a href={r.approval.apply_url} target="_blank" rel="noreferrer" className="text-primary underline">Ask Amazon for approval</a>
            )}
            {r.approval?.can_sell === false && (
              <button type="button" disabled={checking} className="inline-flex min-h-8 items-center gap-1 text-primary underline"
                onClick={async () => { setChecking(true); await askAmazon([r.asin], toast); setChecking(false); onChange(); }}>
                <RefreshCw className={cn('h-3 w-3', checking && 'animate-spin')} />{checking ? 'Asking Amazon…' : 'Check again'}
              </button>
            )}
            {market === 'amazon' && <CopySku s={r.sell_sku} />}
            {market === 'amazon' && r.asin && (
              <a href={sellUrl(r.asin)} target="_blank" rel="noreferrer" className="inline-flex min-h-8 items-center gap-0.5 text-primary underline">
                List in Seller Central<ExternalLink className="h-3 w-3" />
              </a>
            )}
          </div>
          <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs">
            {r.boxes.map((b) => (b.box_id
              ? <Link key={b.name} to={`/boxes/${b.box_id}`} className="underline">{b.name} <span className="text-muted-foreground no-underline">({b.qty})</span></Link>
              : <span key={b.name}>{b.name} <span className="text-muted-foreground">({b.qty})</span></span>))}
          </div>
        </div>
        <div className="shrink-0 space-y-0.5 text-right text-sm">
          <div><b className="tabular-nums">{r.on_hand}</b> here</div>
          <div className="text-xs text-muted-foreground"><span className="tabular-nums">{r.listed}</span> listed</div>
          {r.reason === 'short' && <div className="text-xs font-medium text-warning-ink"><span className="tabular-nums">{r.unlisted}</span> not listed</div>}
        </div>
      </div>
      {(r.reason === 'no_asin' || (r.reason === 'not_listed' && r.suggestions.length > 0)) && (
        <div className="space-y-2">
          <Button variant="outline" className="h-11 w-full sm:w-auto" onClick={() => setOpen(!open)}>
            <Search className="mr-1.5 h-4 w-4" />Find its listing{r.suggestions.length ? ` (${r.suggestions.length})` : ''}
          </Button>
          {open && (
            <div className="space-y-1.5 rounded-md border bg-muted/40 p-2">
              {r.suggestions.length ? <>
                <div className="text-xs text-muted-foreground">{name} listings with a close name and no product. Tap the right one. {sg0asin(r) ? 'Its ASIN goes on the product.' : 'Boxloft remembers it, and its sales find this item.'}</div>
                {r.suggestions.map((sg) => (
                  <button key={sg.listing_id} type="button" disabled={busy} onClick={() => link(sg)}
                    className={cn('flex min-h-11 w-full items-center gap-2 p-2.5 text-sm', tapTile)}>
                    <span className="min-w-0 flex-1">
                      <span className="line-clamp-2">{sg.title}</span>
                      <span className="block text-xs text-muted-foreground"><span className="font-mono">{sg.asin || sg.sku.replace(/^ITEM-/, 'item ')}</span> · name match {sg.score}%</span>
                    </span>
                    <span className="shrink-0 text-right text-xs">
                      <span className="block">{sg.live ? `${sg.qty} for sale` : `${sg.status.toLowerCase() || 'inactive'}, ${sg.qty}`}</span>
                      <span className="block text-muted-foreground">{money(sg.price_cents)}</span>
                    </span>
                    <TapAction label="Link" />
                  </button>
                ))}
              </> : (
                <div className="text-sm">
                  No {name} listing has a close name. <Link to={`/inventory/${r.product_id}`} className="underline">Open the product</Link> and add its ASIN.
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** True when the suggestions are Amazon listings (they carry an ASIN). */
const sg0asin = (r: StockRow) => !!r.suggestions[0]?.asin;

function Row({ l, name }: { l: Listing; name: string }) {
  const over = l.fulfillment === 'FBM' && live(l) && l.qty > l.on_hand;
  const other = l.on_hand - l.on_hand_market;
  const ebay = l.channel === 'ebay';
  const url = ebay ? `https://www.ebay.com/itm/${l.listing_id}` : `https://www.amazon.com/dp/${l.asin}`;
  return (
    <div className="flex gap-3 p-3">
      <a href={url} target="_blank" rel="noreferrer" className="shrink-0">
        {l.image_url ? <Thumb photoId={null} url={l.image_url} /> : <div className="h-14 w-14 rounded-md bg-muted" />}
      </a>
      <div className="min-w-0 flex-1 space-y-1">
        <a href={url} target="_blank" rel="noreferrer" className="line-clamp-2 text-sm font-medium hover:underline">{l.title || l.asin}</a>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          <Badge variant="outline" className="text-xs">{name} {l.fulfillment}</Badge>
          {l.asin && <span className="font-mono">{l.asin}</span>}
          {ebay
            ? <><span className="font-mono">item {l.listing_id}</span><span className="break-all">{l.code ? `Custom label ${l.code}` : 'no Custom label'}</span></>
            : <span className="break-all">SKU {l.sku}</span>}
          {l.condition && l.condition !== 'New' && <span>{l.condition}</span>}
          {l.gone_at && <span className="text-destructive">deleted on {name}</span>}
          {l.audit_id ? <Link to={`/audit?q=${l.asin}`} className="underline">in the audit</Link> : null}
          {l.product_id ? <Link to={`/inventory/${l.product_id}`} className="underline">in Boxloft</Link> : null}
        </div>
      </div>
      <div className="shrink-0 space-y-0.5 text-right text-sm">
        <div><b className="tabular-nums">{l.qty}</b> {l.status === 'Active' ? 'for sale' : <span className="text-muted-foreground">{l.status.toLowerCase()}</span>}</div>
        <div className="text-xs text-muted-foreground">{money(l.price_cents)}</div>
        {l.fulfillment === 'FBA'
          ? (l.fba_inbound > 0 || l.fba_reserved > 0) && <div className="text-xs text-muted-foreground">{[l.fba_inbound && `${l.fba_inbound} inbound`, l.fba_reserved && `${l.fba_reserved} reserved`].filter(Boolean).join(' · ')}</div>
          : live(l) && (
            <div className={cn('text-xs', over ? 'text-warning-ink' : 'text-muted-foreground')}>
              <div>{l.on_hand_market} in {name} boxes</div>
              {other > 0 && <div>{other} in other boxes</div>}
            </div>
          )}
      </div>
    </div>
  );
}
