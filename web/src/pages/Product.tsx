import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Camera, Combine, ImagePlus, LoaderCircle, PackagePlus, Pencil, Printer, ScanLine, Trash2, Plus, Undo2, X } from 'lucide-react';
import { MergeDialog } from './ProductMerge';

// Merge paused 11-17 10:50 AM ET (review found 8 bugs), back on with the fixes (backend/merge.py Merge.enabled).
const MERGE_ON: boolean = false;   // paused 11-17 2:45 PM ET with backend/merge.py Merge.enabled
import { SendBackForm } from '@/pages/Returns';
import { OutLink, QuickLinks, amazonOrderUrl, isAmazonOrder } from '@/lib/links';
import { CopySku } from '@/components/app/CopySku';
import { ApiError, get, post, upload } from '@/api/client';
import type { CatalogItem, Identifier, Owner, PhotoMatch, PhotoRead, Product as ProductT, PurchaseLine } from '@/api/types';
import { ProductLinks } from './ProductLinks';
import { WhereItIs } from './ProductMove';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { Empty, PageHeader, Stat, TapAction, tapTile, Thumb } from '@/components/app/common';
import { ActivityList } from '@/pages/Activity';
import { useToast } from '@/components/app/toast';
import { navigate, useQueryParam } from '@/lib/router';
import { centsFromInput, dollarsForInput, money, when } from '@/lib/format';
import { fnskuLabel, printLabels, unitLabel } from '@/lib/labels';
import { shrinkPhoto } from '@/lib/photo';
import { useScan, useScanBus } from '@/lib/scan';

const KINDS = ['UPC', 'EAN', 'GTIN', 'ASIN', 'FNSKU', 'EBAY_SKU', 'EBAY_ITEM', 'WALMART_SKU', 'WALMART_ITEM', 'SHOPIFY_SKU', 'MPN', 'OTHER'];
const KIND_LABEL: Record<string, string> = { EBAY_SKU: 'eBay Custom Label', EBAY_ITEM: 'eBay item #', WALMART_SKU: 'Walmart SKU', WALMART_ITEM: 'Walmart item ID', SHOPIFY_SKU: 'Shopify SKU' };
const CONDITIONS = ['New', 'Used - Like New', 'Used - Very Good', 'Used - Good', 'Used - Acceptable', 'Refurbished', 'For parts'];

/** Guess what a scanned code is, so a new product starts with it filled in. */
export function guessIdentifier(code: string): Identifier | null {
  const c = code.trim();
  if (/^\d{12}$/.test(c)) return { kind: 'UPC', value: c };
  if (/^\d{13}$/.test(c)) return { kind: 'EAN', value: c };
  if (/^\d{14}$/.test(c)) return { kind: 'GTIN', value: c };
  if (/^X0[0-9A-Z]{8}$/i.test(c)) return { kind: 'FNSKU', value: c.toUpperCase() };
  if (/^B0[0-9A-Z]{8}$/i.test(c)) return { kind: 'ASIN', value: c.toUpperCase() };
  return null;
}

function newSku() {
  const d = new Date();
  const ymd = `${String(d.getFullYear()).slice(2)}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}`;
  return `SS-${ymd}-${Math.random().toString(36).slice(2, 6).toUpperCase()}`;
}

export interface Prefill {
  title?: string;
  brand?: string;
  identifiers?: Identifier[];
  image_url?: string | null;
  pkg_l_mm?: number | null;
  pkg_w_mm?: number | null;
  pkg_h_mm?: number | null;
  pkg_weight_g?: number | null;
  default_cost_cents?: number | null;
}

const toIn = (mm: number | null | undefined) => (mm ? (mm / 25.4).toFixed(1) : '');
const toMm = (v: string) => (Number(v) > 0 ? Math.round(Number(v) * 25.4) : null);

export function ProductForm({ product, prefill, onSaved, notice, onConflict }: {
  product?: ProductT; prefill?: Prefill; onSaved: (p: ProductT) => void;
  /** Shown at the top: why the form just reopened with someone else's newer save. */
  notice?: string;
  /** A newer save beat this one (409): the parent reopens the form on the fresh product. */
  onConflict?: (message: string) => void;
}) {
  const code = useQueryParam('code') ?? '';
  const guessed = !product && code ? guessIdentifier(code) : null;
  const src: Prefill = product ?? prefill ?? {};
  const owners = useQuery({ queryKey: ['owners'], queryFn: () => get<{ owners: Owner[] }>('/api/owners') });
  const qc = useQueryClient();
  const [sku, setSku] = useState(product?.sku ?? (code && !guessed && !prefill?.title ? code : newSku()));
  const [title, setTitle] = useState(src.title ?? '');
  const [brand, setBrand] = useState(src.brand ?? '');
  const [dims, setDims] = useState([toIn(src.pkg_l_mm), toIn(src.pkg_w_mm), toIn(src.pkg_h_mm)]);
  const [ownerId, setOwnerId] = useState(String(product?.owner_id ?? 1));
  const [tracking, setTracking] = useState<'qty' | 'unit'>(product?.tracking ?? 'qty');
  const [condition, setCondition] = useState(product?.condition ?? 'New');
  const [cost, setCost] = useState(dollarsForInput(src.default_cost_cents));
  // Alex 11-13: units saved with no cost take the usual cost when it is saved (wms._fill_costs).
  const noCost = (product?.receipts ?? []).filter((r) => !r.reversed_by && r.unit_cost_cents === null).reduce((n, r) => n + r.qty, 0);
  const [notes, setNotes] = useState(product?.notes ?? '');
  const alsoParam = useQueryParam('also');   // a scanned sticker code carried over from a tapped buy stays on the product
  const also = product ? null : alsoParam;
  const [idents, setIdents] = useState<Identifier[]>(() => {
    const base = src.identifiers?.length ? src.identifiers : guessed ? [guessed] : [{ kind: 'UPC', value: '' }];
    const g = also ? guessIdentifier(also) ?? { kind: 'OTHER', value: also } : null;
    return g && !base.some((i) => i.value.toUpperCase() === g.value.toUpperCase()) ? [...base.filter((i) => i.value.trim()), g] : base;
  });
  const [error, setError] = useState<string | null>(null);
  const [imageUrl, setImageUrl] = useState(prefill?.image_url ?? null);
  const [found, setFound] = useState<string | null>(null);
  // Alex 11-11: a photo fills the name and brand (Gemini) and becomes the product photo on Create product.
  const photoParam = useQueryParam('photo');
  const [photo, setPhoto] = useState<string | null>(product ? null : photoParam);
  const filled = useRef({ title: '', brand: '' });
  const { feedback } = useScanBus();

  const onPhotoRead = (r: PhotoRead) => {
    setPhoto(r.photo);
    const rd = r.read;
    if (!rd?.is_product) return;
    // Fill a box only when it is empty or still holds what an earlier photo put there: typing always wins.
    setTitle((t) => (!t.trim() || t === filled.current.title ? rd.title : t));
    setBrand((b) => (!b.trim() || b === filled.current.brand ? rd.brand : b));
    filled.current = { title: rd.title, brand: rd.brand };
    const add: Identifier[] = [];
    if (rd.barcode_digits) add.push(guessIdentifier(rd.barcode_digits) ?? { kind: 'UPC', value: rd.barcode_digits });
    const c = r.catalog;
    if (c?.asin) add.push({ kind: 'ASIN', value: c.asin });
    if (c) setDims((d) => (d.some(Boolean) ? d : [toIn(c.pkg_l_mm), toIn(c.pkg_w_mm), toIn(c.pkg_h_mm)]));
    setIdents((cur) => {
      let next = cur;
      for (const g of add) {
        if (next.some((i) => i.value.trim().toUpperCase() === g.value.toUpperCase())) continue;
        const empty = next.findIndex((i) => !i.value.trim());
        next = empty >= 0 ? next.map((x, k) => (k === empty ? g : x)) : [...next, g];
      }
      return next;
    });
  };

  // Alex 11-11: the lookup found nothing, so the code has to go in by hand. A scan (camera or scanner) on this form
  // puts the code in the first empty box. On a new product with no name yet, it also asks the catalog again.
  useScan(async (raw) => {
    const c = raw.trim();
    if (/^S[BLU]-?\d/i.test(c)) { feedback(false, 'That is a Boxloft label. Scan the barcode on the item.'); return; }
    const g = guessIdentifier(c) ?? { kind: 'OTHER', value: c };
    if (idents.some((i) => i.value.trim().toUpperCase() === g.value.toUpperCase())) { feedback(false, `${g.value} is already on this product`, true); return; }
    const empty = idents.findIndex((i) => !i.value.trim());
    const next = empty >= 0 ? idents.map((x, k) => (k === empty ? g : x)) : [...idents, g];
    setIdents(next);
    feedback(true, `${KIND_LABEL[g.kind] ?? g.kind} ${g.value} added`, true);   // the camera closes so the form shows
    if (product || title.trim() || g.kind === 'OTHER') return;
    try {
      const hit = (await get<{ products: CatalogItem[] }>(`/api/catalog/lookup?code=${encodeURIComponent(g.value)}`)).products[0];
      if (!hit) { setFound(`The lookup knows nothing about ${g.value} either. Type the name.`); return; }
      setTitle((t) => t || hit.title);
      setBrand((b) => b || hit.brand);
      setDims((d) => (d.some(Boolean) ? d : [toIn(hit.pkg_l_mm), toIn(hit.pkg_w_mm), toIn(hit.pkg_h_mm)]));
      if (hit.image_url) setImageUrl(hit.image_url);
      if (hit.asin && !next.some((i) => i.kind === 'ASIN')) setIdents([...next, { kind: 'ASIN', value: hit.asin }]);
      setFound(`Found: ${hit.title}. Check it, then save.`);
    } catch {
      setFound(null);
    }
  });

  const save = useMutation({
    mutationFn: () =>
      post<ProductT>(product ? `/api/products/${product.id}` : '/api/products', {
        sku, title, brand, owner_id: Number(ownerId), tracking, condition, notes,
        default_cost_cents: centsFromInput(cost),
        pkg_l_mm: toMm(dims[0]!), pkg_w_mm: toMm(dims[1]!), pkg_h_mm: toMm(dims[2]!),
        ...(prefill?.pkg_weight_g ? { pkg_weight_g: prefill.pkg_weight_g } : {}),
        ...(!product && imageUrl ? { image_url: imageUrl } : {}),
        ...(!product && photo ? { photo_token: photo } : {}),   // the photo taken on this form becomes the product photo
        identifiers: idents.filter((i) => i.value.trim()),
        ...(product ? { edited_at: product.edited_at } : {}),   // a newer edit by someone else refuses this one
      }),
    onSuccess: (p) => {
      qc.invalidateQueries({ queryKey: ['products'] });
      qc.invalidateQueries({ queryKey: ['product', p.id] });
      onSaved(p);
    },
    onError: (e: Error) => {
      if (e instanceof ApiError && e.status === 409 && onConflict) {
        qc.invalidateQueries({ queryKey: ['product', product?.id] });
        onConflict(e.message);
        return;
      }
      setError(e.message);
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    save.mutate();
  };

  return (
    <form onSubmit={submit} className="space-y-4">
      {notice && <p role="alert" className="rounded-md border border-warning/50 bg-warning/10 p-3 text-sm text-warning-ink">{notice}</p>}
      {!product && <PhotoFill photo={photo} onRead={onPhotoRead} />}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 [&>*]:min-w-0">
        <div className="space-y-1.5 sm:col-span-2">
          <Label htmlFor="title">Title</Label>
          <Input id="title" value={title} onChange={(e) => setTitle(e.target.value)} required autoFocus={!product && !photo} className="h-11" placeholder="What is it? Brand, model, size, color" />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="brand">Brand</Label>
          <Input id="brand" value={brand} onChange={(e) => setBrand(e.target.value)} className="h-11" />
        </div>
        <div className="space-y-1.5">
          <Label>Package size, inches (for picking the ship box)</Label>
          <div className="flex gap-2">{['L', 'W', 'H'].map((d, i) => (
            <Input key={d} placeholder={d} inputMode="decimal" value={dims[i]} onChange={(e) => setDims(dims.map((x, k) => (k === i ? e.target.value : x)))} className="h-11" />
          ))}</div>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="sku">SKU (your master code; use it as the eBay Custom Label and Amazon seller SKU)</Label>
          <div className="flex gap-2">
            <Input id="sku" value={sku} onChange={(e) => setSku(e.target.value)} required className="h-11 font-mono" />
            {!product && <Button type="button" variant="outline" className="h-11" onClick={() => setSku(newSku())}>New</Button>}
          </div>
        </div>
        <div className="space-y-1.5">
          <Label>Owner</Label>
          <Select value={ownerId} onValueChange={setOwnerId}>
            <SelectTrigger className="h-11 w-full"><SelectValue /></SelectTrigger>
            <SelectContent>{owners.data?.owners.map((o) => <SelectItem key={o.id} value={String(o.id)}>{o.name}{o.is_client ? ' (3PL client)' : ''}</SelectItem>)}</SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label>How do you track it?</Label>
          <Select value={tracking} onValueChange={(v) => setTracking(v as 'qty' | 'unit')}>
            <SelectTrigger className="h-11 w-full"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="qty">By quantity (identical items, e.g. Amazon replens)</SelectItem>
              <SelectItem value="unit">One by one (each unit has its own label, cost, photos)</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label>Condition</Label>
          <Select value={condition} onValueChange={setCondition}>
            <SelectTrigger className="h-11 w-full"><SelectValue /></SelectTrigger>
            <SelectContent>{CONDITIONS.map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}</SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="cost">Usual unit cost ($, optional: blank if you don't know)</Label>
          <Input id="cost" inputMode="decimal" value={cost} onChange={(e) => setCost(e.target.value)} className="h-11" placeholder="0.00" />
          {noCost > 0 && (
            <p className="text-xs text-muted-foreground">{noCost} {noCost === 1 ? 'unit' : 'units'} came in with no cost. Saving a cost here gives {noCost === 1 ? 'it' : 'them'} this cost too.</p>
          )}
        </div>
      </div>

      <div className="space-y-2">
        <Label>Barcodes and marketplace codes (scan the item's barcode to fill it)</Label>
        {found && <p className="text-sm text-muted-foreground">{found}</p>}
        {idents.map((i, n) => (
          <div key={n} className="flex gap-2">
            <Select value={i.kind} onValueChange={(v) => setIdents(idents.map((x, k) => (k === n ? { ...x, kind: v } : x)))}>
              <SelectTrigger className="h-10 w-32 shrink-0 sm:w-44"><SelectValue /></SelectTrigger>
              <SelectContent>{KINDS.map((k) => <SelectItem key={k} value={k}>{KIND_LABEL[k] ?? k}</SelectItem>)}</SelectContent>
            </Select>
            <Input data-scan-input value={i.value} onChange={(e) => setIdents(idents.map((x, k) => (k === n ? { ...x, value: e.target.value } : x)))} className="h-10 min-w-0 flex-1 font-mono" />
            <Button type="button" variant="ghost" size="icon" className="h-10 w-10 shrink-0" onClick={() => setIdents(idents.filter((_, k) => k !== n))} aria-label="Remove code"><X className="h-4 w-4" /></Button>
          </div>
        ))}
        <div className="flex flex-wrap gap-2">
          <Button type="button" size="sm" onClick={() => window.dispatchEvent(new Event('boxloft:camera'))}><ScanLine className="mr-1 h-3.5 w-3.5" />Scan a barcode</Button>
          <Button type="button" variant="outline" size="sm" onClick={() => setIdents([...idents, { kind: 'FNSKU', value: '' }])}><Plus className="mr-1 h-3.5 w-3.5" />Add code</Button>
        </div>
      </div>

      <div className="space-y-1.5">
        <Label htmlFor="notes">Notes</Label>
        <Textarea id="notes" value={notes} onChange={(e) => setNotes(e.target.value)} rows={2} />
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}
      <Button type="submit" className="h-11 w-full sm:w-auto" disabled={save.isPending}>{save.isPending ? 'Saving...' : product ? 'Save changes' : 'Create product'}</Button>
    </form>
  );
}

function matchMeta(m: PhotoMatch) {
  const by = m.by === 'barcode' ? 'Same barcode · ' : '';
  if (m.kind === 'product') return `${by}Already in Boxloft: tap to open it`;
  if (m.kind === 'audit') return `${by}Stock audit${m.asin ? ` · ${m.asin}` : ''} · bought ${m.left ?? '?'}`;
  return `${by}${m.where ?? 'Order'} · ${m.left ?? '?'} still coming`;
}

/** Buys that look like the item, with their pictures: tap the one it is. Used by the photo, a scan on New product
 *  and Receive. */
export function BuyMatchList({ matches, onPick, title, footer, busy, action }: {
  matches: PhotoMatch[]; onPick: (m: PhotoMatch) => void; title: string; footer?: string; busy?: boolean;
  /** The word on each row's right (Alex 11-12: say what a tap does). Default: Open for an item, Use for a buy. */
  action?: string;
}) {
  return (
    <div className="space-y-2">
      <p className="text-sm font-medium">{title}</p>
      {matches.map((m) => (
        <button key={`${m.kind}-${m.id}`} type="button" disabled={busy} onClick={() => onPick(m)}
          className={`flex min-h-14 w-full items-center gap-3 p-2.5 ${tapTile}`}>
          <Thumb photoId={m.photo_id} url={m.image_url} />
          <span className="min-w-0 flex-1">
            <span className="line-clamp-2 text-sm font-medium">{m.title}</span>
            <span className="block text-xs text-muted-foreground">{matchMeta(m)}</span>
          </span>
          <TapAction label={action ?? (m.kind === 'product' ? 'Open' : 'Use')} />
        </button>
      ))}
      {footer && <p className="text-xs text-muted-foreground">{footer}</p>}
    </div>
  );
}

/** Back to the page New product came from: a box page (return=box), or a place's page (return=place, Alex 11-17). */
const placePath = (back: string, id: string) => (back === 'place' ? `/locations/${id}` : `/boxes/${id}`);

/** A tapped buy: open the product (back to the box or Receive when New product came from there), or make the product
 *  from the buy itself (its name, cost and Amazon data), keeping the photo and the scanned code. */
function openBuy(m: PhotoMatch, keep: { back?: string | null; box?: string | null; photo?: string | null; also?: string }) {
  const { back, box } = keep;
  if (m.kind === 'product') {
    navigate((back === 'box' || back === 'place') && box ? `${placePath(back, box)}?add=${m.id}` : back === 'receive' ? `/receive?product=${m.id}` : `/inventory/${m.id}`);
    return;
  }
  const q = new URLSearchParams({ ...(back ? { return: back } : {}), ...(box ? { box } : {}), ...(keep.photo ? { photo: keep.photo } : {}),
    ...(keep.also ? { also: keep.also } : {}) });
  if (m.kind === 'audit') q.set('code', m.asin);
  else q.set('line', String(m.id));
  navigate(`/inventory/new?${q}`);
}

/** New product (Alex 11-11): take a photo, the form fills the name and brand, our buys that look like it show with
 *  their pictures, and the photo saves as the product photo. Gemini reads it on the server in 5-10 s. */
function PhotoFill({ photo, onRead }: { photo: string | null; onRead: (r: PhotoRead) => void }) {
  const camera = useRef<HTMLInputElement>(null);
  const library = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<string | null>(photo ? `/api/photo-id/${photo}` : null);
  const [res, setRes] = useState<PhotoRead | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const back = useQueryParam('return');
  const box = useQueryParam('box');

  const take = async (files: FileList | null) => {
    const f = files?.[0];
    if (camera.current) camera.current.value = '';
    if (library.current) library.current.value = '';
    if (!f) return;
    setBusy(true);
    setErr(null);
    setRes(null);
    try {
      const blob = await shrinkPhoto(f);
      setPreview(URL.createObjectURL(blob));
      const r = await upload<PhotoRead>('/api/photo-id', blob);
      setRes(r);
      onRead(r);
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Could not send the photo. Try again.');
    } finally {
      setBusy(false);
    }
  };

  const open = async (m: PhotoMatch) => {
    if (!res) return;
    if (m.kind === 'product' && !m.photo_id) await post('/api/photo-id/attach', { photo_token: res.photo, product_id: m.id }).catch(() => undefined);
    openBuy(m, { back, box, photo: res.photo });
  };

  const inputs = (
    <>
      <input ref={camera} type="file" accept="image/*" capture="environment" className="hidden" onChange={(e) => take(e.target.files)} />
      <input ref={library} type="file" accept="image/*" className="hidden" onChange={(e) => take(e.target.files)} />
    </>
  );
  if (!preview) {
    return (
      <div className="space-y-1.5 rounded-lg border border-dashed p-3">
        {inputs}
        <Button type="button" className="h-12 w-full text-base" onClick={() => camera.current?.click()}>
          <Camera className="mr-2 h-5 w-5" />Take a photo to fill this in
        </Button>
        <p className="text-xs text-muted-foreground">
          The name and brand come from the box, and anything you bought that looks like it shows here. The photo saves with the product.{' '}
          <button type="button" className="underline" onClick={() => library.current?.click()}>Use a saved photo</button>
        </p>
        {err && <p className="text-sm text-destructive">{err}</p>}
      </div>
    );
  }
  const rd = res?.read;
  return (
    <div className="space-y-3 rounded-lg border p-3">
      {inputs}
      <div className="flex gap-3">
        <img src={preview} alt="Your photo" className="h-20 w-20 shrink-0 rounded-md border bg-white object-cover" />
        <div className="min-w-0 flex-1 text-sm">
          {busy ? (
            <p className="flex items-center gap-2 font-medium"><LoaderCircle className="h-4 w-4 animate-spin" />Reading the box... about 10 seconds</p>
          ) : res?.error ? (
            <p className="text-warning-ink">{res.error}</p>
          ) : rd ? (
            <p><span className="text-muted-foreground">The photo says: </span><span className="font-medium">{rd.title}</span>{rd.brand ? ` (${rd.brand})` : ''}. Check the name below.</p>
          ) : (
            <p className="text-muted-foreground">This photo saves with the product.</p>
          )}
          {err && <p className="text-destructive">{err}</p>}
          {!busy && (
            <Button type="button" variant="ghost" size="sm" className="mt-1 h-8 px-2 text-xs underline" onClick={() => camera.current?.click()}>
              <ImagePlus className="mr-1 h-3.5 w-3.5" />Take it again
            </Button>
          )}
        </div>
      </div>
      {res && res.matches.length > 0 && (
        <BuyMatchList matches={res.matches} onPick={open} title="Did you buy this one? Tap it."
          footer="None of these? Check the name below and tap Create product." />
      )}
      {res && rd?.is_product && res.matches.length === 0 && (
        <p className="text-xs text-muted-foreground">Nothing you bought looks like it. Check the name below and tap Create product.</p>
      )}
    </div>
  );
}

function fromCatalog(c: CatalogItem, scanned: string): Prefill {
  const ids: Identifier[] = [];
  if (c.asin) ids.push({ kind: 'ASIN', value: c.asin });
  const g = guessIdentifier(scanned);
  if (g && g.kind !== 'ASIN') ids.push(g);
  for (const u of c.upcs) if (!ids.some((i) => i.value === u)) ids.push({ kind: 'UPC', value: u });
  return { title: c.title, brand: c.brand, identifiers: ids, image_url: c.image_url, pkg_l_mm: c.pkg_l_mm, pkg_w_mm: c.pkg_w_mm, pkg_h_mm: c.pkg_h_mm, pkg_weight_g: c.pkg_weight_g };
}

export function NewProduct() {
  const back = useQueryParam('return');
  const code = useQueryParam('code') ?? '';
  const lineId = useQueryParam('line');
  const photoParam = useQueryParam('photo');
  const [state, setState] = useState<{ loading: boolean; found: CatalogItem[]; line?: PurchaseLine; note?: string }>({ loading: !!(code || lineId), found: [] });
  const [pick, setPick] = useState(0);

  useEffect(() => {
    // A buy tapped under the photo opens this page again with a new code or line: look it up afresh.
    setState({ loading: !!(code || lineId), found: [] });
    setPick(0);
    (async () => {
      try {
        let line: PurchaseLine | undefined;
        let key = code;
        if (lineId) {
          line = await get<PurchaseLine>(`/api/purchases/line/${lineId}`);
          key = line.asin || line.upc;
        }
        const found = key ? (await get<{ products: CatalogItem[] }>(`/api/catalog/lookup?code=${encodeURIComponent(key)}`)).products : [];
        setState({ loading: false, found, line, note: !key || found.length ? undefined
          : line ? 'This adds a new product from your order. The name and cost come from the order. Check them, then save.'
          : `${key} is new to Boxloft and the product lookup found nothing for it. Type what it is below, then save.` });
      } catch (e) {
        setState({ loading: false, found: [], note: e instanceof Error ? e.message : 'Lookup failed' });
      }
    })();
  }, [code, lineId]);

  const c = state.found[pick];
  const line = state.line;
  let prefill: Prefill | undefined = c ? fromCatalog(c, code) : undefined;
  if (line) {
    prefill = { ...(prefill ?? {}), title: prefill?.title || line.title, default_cost_cents: line.unit_cost_cents,
      identifiers: prefill?.identifiers?.length ? prefill.identifiers : [line.asin ? { kind: 'ASIN', value: line.asin } : { kind: 'UPC', value: line.upc }] };
  }
  // return=box (Alex 11-11): made from a box's page, so go back there and ask how many go in that box.
  const boxParam = useQueryParam('box');
  // Alex 11-11 (Dreamcatcher album): an importer's sticker barcode gave another listing's ASIN, so the product never
  // met its order. When the catalog's ASIN is not one we bought, buys whose titles look like it are offered.
  const alike = useQuery({
    queryKey: ['buy-match', c?.asin, c?.title],
    queryFn: () => get<{ bought: boolean; matches: PhotoMatch[] }>(`/api/buy-match?${new URLSearchParams({ q: c!.title, asin: c!.asin ?? '' })}`),
    enabled: !!c && !!code && !lineId,
  });
  const [linking, setLinking] = useState(false);
  const pickAlike = async (m: PhotoMatch) => {
    if (m.kind === 'product') {   // already in Boxloft: keep the scanned code on it so the next scan finds it
      setLinking(true);
      try { await post(`/api/products/${m.id}/link-buy`, { code }); } catch { /* opening it still helps */ }
      setLinking(false);
    }
    openBuy(m, { back, box: boxParam, photo: photoParam, also: code });
  };
  const done = (p: ProductT) => navigate(back === 'receive' ? `/receive?product=${p.id}${lineId ? `&line=${lineId}` : ''}`
    : (back === 'box' || back === 'place') && boxParam ? `${placePath(back, boxParam)}?add=${p.id}` : `/inventory/${p.id}`, true);
  // Alex 11-11: scanned the wrong item, or the lookup pulled the wrong thing: back out, nothing is saved.
  const cancel = () => { if (window.history.length > 1) window.history.back(); else navigate('/inventory', true); };
  const blank = () => { setState({ ...state, found: [], note: 'Starting blank. Type what it is, then tap Create product.' }); setPick(0); };

  return (
    <div className="max-w-3xl space-y-4">
      <PageHeader title="New product" subtitle={line ? `From your ${line.source} order ${line.order_id}` : code ? `Scanned ${code}. Nothing is saved until you tap Create product.` : 'Nothing is saved until you tap Create product.'}
        actions={<Button variant="outline" onClick={cancel}>Cancel</Button>} />
      {state.loading ? <Card><CardContent className="p-6 text-sm">Looking up {code || 'the order item'}...</CardContent></Card> : (
        <>
          {c && (
            <Card><CardContent className="flex gap-4 p-4">
              {c.image_url && <Thumb photoId={null} url={c.image_url} size="focus" />}
              <div className="min-w-0 flex-1 text-sm">
                <div className="font-medium">{c.title}</div>
                <div className="text-muted-foreground">{c.brand}{c.asin ? ` · ASIN ${c.asin}` : ''}{c.pkg_l_mm ? ` · ${toIn(c.pkg_l_mm)} × ${toIn(c.pkg_w_mm)} × ${toIn(c.pkg_h_mm)} in` : ''}</div>
                <div className="mt-1 text-xs text-muted-foreground">Found in the {c.source === 'upcitemdb' ? 'UPC database' : 'Amazon catalog'}. {photoParam ? 'Your own photo below saves as the product photo.' : 'The photo saves with the product.'}</div>
                <Button size="sm" variant="ghost" className="mt-1 h-8 px-2 text-xs underline" onClick={blank}>Wrong item? Start blank</Button>
                {state.found.length > 1 && (
                  <div className="mt-2 flex flex-wrap gap-1">{state.found.map((f, i) => (
                    <Button key={i} size="sm" variant={i === pick ? 'default' : 'outline'} onClick={() => setPick(i)}>{f.asin ?? `Match ${i + 1}`}</Button>
                  ))}</div>
                )}
              </div>
            </CardContent></Card>
          )}
          {(alike.data?.matches.length ?? 0) > 0 && (
            <Card><CardContent className="p-4">
              <BuyMatchList matches={alike.data!.matches} onPick={pickAlike} busy={linking}
                title={`You bought something like this, under another code. Is it one of these? Tap it.`}
                footer="Tapping one makes the product from that buy, so it links to the order and the Stock audit. None of these? Fill in the form below." />
            </CardContent></Card>
          )}
          {state.note && <p className="text-sm text-muted-foreground">{state.note}</p>}
          <Card><CardContent className="p-4 sm:p-6">
            <ProductForm key={`${code}-${lineId ?? ''}-${pick}-${c?.asin ?? ''}-${state.found.length}`} prefill={prefill} onSaved={done} />
          </CardContent></Card>
          <Button variant="outline" className="h-11 w-full" onClick={cancel}>Cancel: wrong item, don't save</Button>
        </>
      )}
    </div>
  );
}

export function PhotoStrip({ photos, uploadTo, onChange }: { photos: { id: number }[]; uploadTo: string; onChange: () => void }) {
  const input = useRef<HTMLInputElement>(null);
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [big, setBig] = useState<number | null>(null);
  const add = async (files: FileList | null) => {
    if (!files?.length) return;
    setBusy(true);
    try {
      for (const f of Array.from(files)) await upload(uploadTo, await shrinkPhoto(f));
      onChange();
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Upload failed', { tone: 'error' });
    } finally {
      setBusy(false);
      if (input.current) input.current.value = '';
    }
  };
  const remove = async (id: number) => {
    await post(`/api/photos/${id}/remove`);
    setBig(null);
    onChange();
  };
  return (
    <div>
      <div className="flex gap-2 overflow-x-auto pb-1">
        {photos.map((p) => (
          <button key={p.id} type="button" onClick={() => setBig(p.id)} className="shrink-0">
            <Thumb photoId={p.id} size="gallery" />
          </button>
        ))}
        <button type="button" onClick={() => input.current?.click()} disabled={busy}
          className="flex h-28 w-28 shrink-0 flex-col items-center justify-center gap-1 rounded-lg border-2 border-dashed text-sm text-muted-foreground hover:bg-muted/50">
          <Camera className="h-6 w-6" />{busy ? 'Uploading...' : 'Add photo'}
        </button>
        <input ref={input} type="file" accept="image/*" capture="environment" multiple hidden onChange={(e) => add(e.target.files)} />
      </div>
      {big && (
        <div className="fixed inset-0 z-50 flex flex-col bg-black/90 p-4" onClick={() => setBig(null)}>
          <img src={`/api/photos/${big}`} alt="" className="m-auto max-h-[85vh] max-w-full rounded-lg object-contain" />
          <div className="flex justify-center gap-2 pt-3" onClick={(e) => e.stopPropagation()}>
            <Button variant="destructive" onClick={() => remove(big)}><Trash2 className="mr-1.5 h-4 w-4" />Remove photo</Button>
            <Button variant="secondary" onClick={() => setBig(null)}>Close</Button>
          </div>
        </div>
      )}
    </div>
  );
}

interface Usage {
  history: Record<string, number>; pointers: Record<string, number>; on_hand: number;
  places: { place: string; qty: number }[]; can_delete: boolean;
}

// The server names each kind in the plural ("listings", "order lines"); 1 of a kind drops the s.
const listOf = (o: Record<string, number>) => Object.entries(o).map(([k, n]) => `${n} ${n === 1 ? k.replace(/s$/, '') : k}`).join(', ');

/** Alex 11-11 "we need a way to delete a product". The ledger keeps every move, so only a product that never held
 *  stock is erased; one with history is archived (out of Inventory and searches, history kept, can come back). */
function RemoveProduct({ p, onClose }: { p: ProductT; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const usage = useQuery({ queryKey: ['product-usage', p.id], queryFn: () => get<Usage>(`/api/products/${p.id}/usage`) });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const u = usage.data;
  const act = async (what: 'delete' | 'archive') => {
    setBusy(true);
    setError(null);
    try {
      if (what === 'delete') {
        await post(`/api/products/${p.id}/delete`, { seen: p.updated_at });
        qc.invalidateQueries({ queryKey: ['products'] });
        toast(`Deleted ${p.title.slice(0, 50)}`);
        navigate('/inventory', true);
      } else {
        const r = await post<ProductT>(`/api/products/${p.id}/archive`, { archived: true, seen: p.updated_at });
        qc.invalidateQueries({ queryKey: ['products'] });
        qc.invalidateQueries({ queryKey: ['product', p.id] });
        toast('Archived. It is out of Inventory and searches.', {
          undo: async () => { await post(`/api/products/${p.id}/archive`, { archived: false, seen: r.updated_at }); qc.invalidateQueries({ queryKey: ['product', p.id] }); qc.invalidateQueries({ queryKey: ['products'] }); },
        });
        onClose();
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) qc.invalidateQueries({ queryKey: ['product', p.id] });
      setError(e instanceof Error ? e.message : 'That did not work');
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-3 rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm">
      {!u ? <p className="text-muted-foreground">Checking what this product is used by...</p> : u.can_delete ? (
        <>
          <p><b>Delete this product for good?</b> It never held stock, so nothing in the history changes.
            {Object.keys(u.pointers).length > 0 && ` Its ${listOf(u.pointers)} go back to unlinked, so they can point at the right product.`}</p>
          <div className="flex flex-wrap gap-2">
            <Button variant="destructive" disabled={busy} onClick={() => act('delete')}><Trash2 className="mr-1.5 h-4 w-4" />{busy ? 'Deleting...' : 'Delete for good'}</Button>
            <Button variant="outline" disabled={busy} onClick={onClose}>Keep it</Button>
          </div>
        </>
      ) : u.on_hand > 0 ? (
        <>
          <p><b>It can't go yet.</b> {u.on_hand} still on hand: {u.places.slice(0, 4).map((x) => `${x.qty} in ${x.place}`).join(', ')}.
            Move them to the right product, ship them or remove them first. Then it can be archived (it has history: {listOf(u.history)}).</p>
          <Button variant="outline" onClick={onClose}>OK</Button>
        </>
      ) : (
        <>
          <p><b>It has history</b> ({listOf(u.history)}), so it can't be erased. <b>Archive it</b> instead: it leaves Inventory,
            searches and the photo matches, and its history stays. You can bring it back.</p>
          <div className="flex flex-wrap gap-2">
            <Button variant="destructive" disabled={busy} onClick={() => act('archive')}>{busy ? 'Archiving...' : 'Archive'}</Button>
            <Button variant="outline" disabled={busy} onClick={onClose}>Keep it</Button>
          </div>
        </>
      )}
      {error && <p className="text-destructive">{error}</p>}
    </div>
  );
}

export function ProductPage({ id }: { id: number }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [conflict, setConflict] = useState('');
  const [sending, setSending] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [merging, setMerging] = useState(false);
  const toast = useToast();
  const q = useQuery({ queryKey: ['product', id], queryFn: () => get<ProductT>(`/api/products/${id}`) });
  const p = q.data;
  if (q.error) return <Empty>{(q.error as Error).message}</Empty>;
  if (!p) return <Skeleton className="h-64" />;
  const fnsku = p.identifiers.find((i) => i.kind === 'FNSKU')?.value;
  const refresh = () => qc.invalidateQueries({ queryKey: ['product', id] });
  const units = p.where.filter((w) => w.unit_id);

  return (
    <div className="max-w-5xl space-y-4 sm:space-y-6">
      <PageHeader
        title={p.title}
        subtitle={<span className="flex flex-col gap-1.5">
          <span className="flex flex-wrap items-center gap-2"><span className="font-mono">{p.sku}</span><Badge variant="secondary">{p.owner}</Badge><span>{p.condition}</span>{p.tracking === 'unit' && <Badge variant="outline">one-off units</Badge>}{p.archived ? <Badge variant="destructive">archived</Badge> : null}</span>
          <QuickLinks title={p.title} ids={p.identifiers} orders={p.receipts.filter((r) => !r.reversed_by).map((r) => r.reference)} />
          <CopySku s={p.sell_sku} className="self-start" />
        </span>}
        actions={
          <>
            <Button onClick={() => navigate(`/receive?product=${p.id}`)}><PackagePlus className="mr-1.5 h-4 w-4" />Receive</Button>
            {p.on_hand > 0 && <Button variant="outline" onClick={() => setSending(!sending)}><Undo2 className="mr-1.5 h-4 w-4" />Send back to seller</Button>}
            {fnsku && <Button variant="outline" onClick={() => printLabels([fnskuLabel(fnsku, p.title, p.condition)])}><Printer className="mr-1.5 h-4 w-4" />FNSKU label</Button>}
            {units.length > 0 && <Button variant="outline" onClick={() => printLabels(units.map((u) => unitLabel({ scan_code: u.unit_code!, sku: p.sku, title: p.title, condition: u.unit_condition ?? '' })))}><Printer className="mr-1.5 h-4 w-4" />Unit labels ({units.length})</Button>}
            <Button variant="outline" onClick={() => setEditing(!editing)}><Pencil className="mr-1.5 h-4 w-4" />{editing ? 'Close edit' : 'Edit'}</Button>
            {MERGE_ON && !p.archived && <Button variant="outline" onClick={() => setMerging(true)}><Combine className="mr-1.5 h-4 w-4" />Same item as...</Button>}
          </>
        }
      />
      {merging && <MergeDialog p={p} onClose={() => setMerging(false)} />}
      {p.archived ? (
        <div className="flex flex-wrap items-center gap-3 rounded-lg border border-warning/50 bg-warning/10 p-3 text-sm">
          <span className="min-w-0 flex-1"><b>Archived.</b> It is out of Inventory and searches. Its history stays below.</span>
          <Button size="sm" variant="outline" onClick={async () => {
            try { await post(`/api/products/${p.id}/archive`, { archived: false, seen: p.updated_at }); refresh(); qc.invalidateQueries({ queryKey: ['products'] }); }
            catch (e) { toast(e instanceof Error ? e.message : 'That did not work', { tone: 'error' }); refresh(); }
          }}>Bring it back</Button>
        </div>
      ) : null}
      <PhotoStrip photos={p.photos} uploadTo={`/api/photos?product_id=${p.id}`} onChange={refresh} />
      {editing && <Card><CardContent className="space-y-4 p-4 sm:p-6">
        {/* key: a newer save by someone else reopens the form with their version, so a second Save can't put it back */}
        <ProductForm key={p.edited_at} product={p} notice={conflict} onConflict={setConflict} onSaved={() => { setEditing(false); setConflict(''); }} />
        {!p.archived && (removing ? <RemoveProduct key={p.updated_at} p={p} onClose={() => setRemoving(false)} /> : (
          <Button type="button" variant="ghost" className="h-10 w-full text-destructive sm:w-auto" onClick={() => setRemoving(true)}>
            <Trash2 className="mr-1.5 h-4 w-4" />Delete product
          </Button>
        ))}
      </CardContent></Card>}
      {sending && (
        <Card><CardHeader className="pb-2"><CardTitle className="text-base">Send back to seller</CardTitle></CardHeader>
          <CardContent><SendBackForm productId={p.id} where={p.where} onCancel={() => setSending(false)}
            onDone={() => { setSending(false); refresh(); qc.invalidateQueries({ queryKey: ['returns'] }); }} /></CardContent></Card>
      )}

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Stat label="On hand" value={p.on_hand} hint={`in ${new Set(p.where.map((w) => w.box_id || `l${w.location_id}`)).size} places`} />
        <Stat label="Avg cost" value={money(p.tracking === 'unit' && units.length ? Math.round(units.reduce((a, u) => a + (u.unit_cost_cents ?? 0), 0) / units.length) : p.avg_cost_cents)} hint={p.tracking === 'unit' ? 'cost of each unit in stock' : 'weighted over receipts'} />
        <Stat label="Value at cost" value={money(p.tracking === 'unit' ? units.reduce((a, u) => a + (u.unit_cost_cents ?? 0), 0) : (p.avg_cost_cents ?? 0) * p.on_hand)} />
        <Stat label="Codes" value={p.identifiers.length} hint={p.identifiers.map((i) => i.kind).join(', ') || 'none yet'} />
      </div>

      <ProductLinks productId={p.id} onHand={p.on_hand} />

      <div className="grid gap-4 lg:grid-cols-2 sm:gap-6 [&>*]:min-w-0">
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-base">Where it is</CardTitle></CardHeader>
          <CardContent>
            <WhereItIs p={p} />
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-base">Codes</CardTitle></CardHeader>
          <CardContent className="space-y-1.5 text-sm">
            {p.identifiers.length ? p.identifiers.map((i) => (
              <div key={i.kind + i.value} className="flex justify-between gap-2"><span className="text-muted-foreground">{KIND_LABEL[i.kind] ?? i.kind}</span><span className="font-mono">{i.value}</span></div>
            )) : <span className="text-muted-foreground">No barcodes yet. Edit to add UPC, ASIN, FNSKU or marketplace IDs.</span>}
            {p.notes && <p className="pt-2 text-muted-foreground">{p.notes}</p>}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-base">Receipts</CardTitle></CardHeader>
          <CardContent>
            {p.receipts.length ? (
              <div className="divide-y text-sm">
                {p.receipts.map((r) => (
                  <div key={r.id} className={`flex justify-between gap-2 py-2 ${r.reversed_by ? 'text-muted-foreground line-through' : ''}`}>
                    <span>{r.purchased_at ? `Bought ${r.purchased_at} · in ` : ''}{when(r.at)} · {r.user_name}{r.supplier ? ` · ${r.supplier}` : ''}
                      {r.reference ? <> · {isAmazonOrder(r.reference) ? <OutLink href={amazonOrderUrl(r.reference)} className="font-mono">{r.reference}</OutLink> : r.reference}</> : null}</span>
                    <span className="tabular-nums">{r.qty} × {money(r.unit_cost_cents)}</span>
                  </div>
                ))}
              </div>
            ) : <span className="text-sm text-muted-foreground">Nothing received yet.</span>}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-base">History</CardTitle></CardHeader>
          <CardContent><ActivityList actions={p.history} compact onChange={refresh} /></CardContent>
        </Card>
      </div>
    </div>
  );
}
