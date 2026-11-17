import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Box as BoxIcon, ScanLine } from 'lucide-react';
import { ApiError, get, post } from '@/api/client';
import type { ScanResult } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { PageHeader, Thumb } from '@/components/app/common';
import { MarketPicker } from '@/components/app/MarketPicker';
import { useToast } from '@/components/app/toast';
import { Link, navigate, useQueryParam } from '@/lib/router';
import { useScan, useScanBus } from '@/lib/scan';
import { clearCurrentBox, isFresh, useCurrentBox } from '@/lib/currentBox';
import { SoldNote, rememberPut, type PutAnswer, type Sale } from '@/pages/BoxAdd';
import { NewItemCard, type NewCard, type NewPick } from '@/pages/NewItemCard';
import { AmazonNote, type AmazonOk } from '@/components/app/AmazonNote';
import { parseCode } from '@/lib/codes';

/* Put away with no box set yet (flow audit 11-12, redesign 1). An item scanned shows what it is and the box to put
 * it in as one big button: the box that already holds it, else the last-filled box of the market it is listed on.
 * A tap, or a box label scan, saves it there and that box becomes the box being filled. With a box set, an item scan
 * on any page goes straight into it (Shell), so this page is only the first step of a session. */

type Look = { kind: 'look'; code: string; title: string; image_url: string | null; photo_id: number | null;
  product: { id: number; sku: string; title: string } | null; goes_as: { kind: string; label: string; sub: string; left: number | null } | null;
  question: string; boxes: { box_id: number; code: string; qty: number | null; why: string }[]; letter: string | null; open_sales: Sale[]; amazon?: AmazonOk };
type LookAnswer = Look | { kind: 'label_code'; code: string } | { kind: 'new_product'; code: string } | { kind: 'target'; type: 'box' | 'location'; id: number } | { kind: 'wrong'; message: string };

export function PutPage() {
  const code = useQueryParam('code');
  const product = useQueryParam('product');
  const cur = useCurrentBox();
  const toast = useToast();
  const { feedback } = useScanBus();
  const want = code || product;
  const q = useQuery({
    queryKey: ['put-look', code, product],
    queryFn: () => get<LookAnswer>(`/api/put/look?${new URLSearchParams(code ? { code } : { product_id: product! })}`),
    enabled: !!want,
  });
  const look = q.data?.kind === 'look' ? q.data : null;
  const isNew = q.data?.kind === 'new_product';
  // A code nothing knows (redesign 2): the new item card, and the box it goes in is the save.
  const nc = useQuery({
    queryKey: ['put-new-card', code],
    queryFn: () => get<NewCard>(`/api/put/new-card?code=${encodeURIComponent(code || '')}`),
    enabled: isNew && !!code,
  });
  const [busy, setBusy] = useState(false);
  const pickRef = useRef<(() => NewPick | null) | null>(null);
  const scanId = useRef(`${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`);

  /** Make the item and put 1 in this box: one save (resent with the same scan_id on a lost reply), then the box page. */
  const saveNew = async (boxId: number, p: NewPick | null) => {
    const c = nc.data;
    if (!c || busy) return;
    if (!p) { feedback(false, 'Choose on the card first, or type what it is', true); return; }
    setBusy(true);
    try {
      let r: PutAnswer | { kind: string } | undefined;
      for (let attempt = 0; ; attempt++) {
        try { r = await post<PutAnswer>('/api/put/new', { box_id: boxId, scan_id: scanId.current, code: c.code, title: p.title, line_id: p.line_id, listing_id: p.listing_id ?? null, product_id: p.product_id ?? null }); break; } catch (e) {
          if ((!(e instanceof ApiError) || e.status >= 502) && attempt < 2) { await new Promise((res) => setTimeout(res, 700)); continue; }
          throw e;
        }
      }
      if (r?.kind === 'added') {
        rememberPut(boxId, { ...(r as PutAnswer), code: c.code });
        navigate(`/boxes/${boxId}`, true);
      } else navigate(`/boxes/${boxId}?put=${encodeURIComponent(c.code)}`, true);   // it needs a choice: the box page asks
    } catch (e) {
      toast(e instanceof ApiError ? e.message : 'Not saved: no signal. Pick the box again when the signal is back.', { tone: 'error' });
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    const r = q.data;
    if (!r) return;
    if (r.kind === 'new_product' && !r.code) navigate('/inventory/new', true);
    else if (r.kind === 'label_code') navigate(`/label?code=${encodeURIComponent(r.code)}`, true);   // a carrier label
    else if (r.kind === 'target') navigate(r.type === 'box' ? `/boxes/${r.id}` : `/locations/${r.id}`, true);
    else if (r.kind === 'wrong') toast(r.message, { tone: 'error' });
  }, [q.data]); // eslint-disable-line react-hooks/exhaustive-deps

  /** Into this box: the box page saves it (one scan_id, Undo) and keeps the box for the next scans. */
  const into = (boxId: number) => navigate(`/boxes/${boxId}?${look?.code ? `put=${encodeURIComponent(look.code)}` : `add=${look?.product?.id ?? product}`}`);

  useScan(async (scanned) => {
    if (nc.data) {   // the new item card waits: a box label is where it goes
      const pc = parseCode(scanned);
      if (pc.kind === 'box') { feedback(true); await saveNew(pc.id, pickRef.current?.() ?? null); return; }
      if (code && scanned.trim().toUpperCase() === code.trim().toUpperCase()) { feedback('next', 'Scan the box it goes in', true); return; }
    }
    let r: ScanResult;
    try { r = await get<ScanResult>(`/api/scan?code=${encodeURIComponent(scanned)}`); } catch {
      feedback(false, 'Not read: no signal. Scan again');
      return;
    }
    if (r.kind === 'box') {
      feedback(true);
      if (want) into(r.box.id); else navigate(`/boxes/${r.box.id}`);
    } else if (r.kind === 'location') {
      feedback(true);
      navigate(`/locations/${r.location.id}`);
    } else if (r.kind === 'product' || r.kind === 'unknown') {
      feedback(true);
      if (isFresh(cur)) navigate(`/boxes/${cur!.box_id}?put=${encodeURIComponent(scanned)}`);
      else navigate(`/put?code=${encodeURIComponent(scanned)}`, true);
    } else {
      feedback(false, r.kind === 'bad_check' ? r.message : 'Scan the item barcode or a box label');
    }
  });

  return (
    <div className="max-w-xl space-y-4">
      <PageHeader title="Put away" subtitle={want ? undefined : 'Scan a box, then scan items into it. Each scan saves 1.'} />
      {!want && (
        <Card><CardContent className="space-y-3 p-4">
          {cur ? (
            <>
              <div className="text-sm text-muted-foreground">{isFresh(cur) ? 'Filling' : 'Last filled'}</div>
              <div className="text-3xl font-bold">{cur.name}</div>
              <div className="flex gap-2">
                <Button className="h-12 flex-1 text-base" onClick={() => navigate(`/boxes/${cur.box_id}`)}><BoxIcon className="mr-1.5 h-4 w-4" />Keep filling {cur.name}</Button>
                <Button variant="outline" className="h-12" onClick={clearCurrentBox}>Done</Button>
              </div>
              <p className="text-sm text-muted-foreground">Or scan another box label.</p>
            </>
          ) : (
            <div className="flex items-center gap-2 text-base font-medium"><ScanLine className="h-5 w-5" />Scan a box label, or an item</div>
          )}
          <div className="space-y-1.5 border-t pt-3">
            <div className="text-sm font-medium">No label at hand? Pick the box:</div>
            <MarketPicker onPick={(b) => navigate(`/boxes/${b.id}`)} />
          </div>
          <p className="border-t pt-3 text-sm text-muted-foreground">A store run with no order, a client delivery or a typed cost: <Link to="/receive" className="text-primary underline">Receive with the form</Link>.</p>
        </CardContent></Card>
      )}
      {want && !look && q.isLoading && <Card><CardContent className="p-6 text-sm">Looking it up...</CardContent></Card>}
      {isNew && nc.isLoading && <Card><CardContent className="p-6 text-sm">Looking it up...</CardContent></Card>}
      {isNew && nc.data && (
        <Card><CardContent className="space-y-3 p-4">
          <AmazonNote a={nc.data.amazon} pre={nc.data.amazon?.can_sell === false} />
          <NewItemCard card={nc.data} boxName={null} busy={busy} pickRef={pickRef} onSave={() => undefined}
            onTwin={(id) => navigate(`/put?product=${id}`, true)}
            onNotThis={() => navigate(`/inventory/new?code=${encodeURIComponent(nc.data!.code)}`, true)}
            onCancel={() => navigate('/put', true)}
            boxSlot={(p) => (
              <div className="space-y-2 rounded-md border border-dashed p-3">
                <div className="flex items-center gap-2 text-sm font-medium"><ScanLine className="h-4 w-4" />Scan the box label it goes into, or pick the box:</div>
                {cur && <Button className="h-12 w-full text-base" disabled={busy || !p} onClick={() => void saveNew(cur.box_id, p)}><BoxIcon className="mr-1.5 h-4 w-4" />Put 1 in {cur.name}</Button>}
                <MarketPicker onPick={(b) => void saveNew(b.id, p)} />
              </div>
            )} />
        </CardContent></Card>
      )}
      {look && (
        <Card><CardContent className="space-y-3 p-4">
          <div className="flex gap-3">
            <Thumb photoId={look.photo_id} url={look.image_url} size="focus" />
            <div className="min-w-0 flex-1">
              <div className="font-semibold leading-snug">{look.title || look.code}</div>
              {look.goes_as && <div className="mt-1 text-sm text-muted-foreground">{look.goes_as.label}</div>}
              {look.product && <Link to={`/inventory/${look.product.id}`} className="text-sm text-primary underline">Item page</Link>}
            </div>
          </div>
          <SoldNote sales={look.open_sales} />
          <AmazonNote a={look.amazon} pre={look.amazon?.can_sell === false} />
          {look.boxes.map((b, i) => (
            <Button key={b.box_id} variant={i ? 'outline' : 'default'} className="h-14 w-full justify-start text-left text-base" onClick={() => into(b.box_id)}>
              <BoxIcon className="mr-2 h-5 w-5 shrink-0" /><span className="min-w-0"><b>Put in {b.code}</b> <span className="text-sm opacity-80">({b.why})</span></span>
            </Button>
          ))}
          <div className="flex items-center gap-2 rounded-md border border-dashed p-3 text-sm"><ScanLine className="h-4 w-4" />{look.boxes.length ? 'Or scan another box label.' : 'Scan the box label it goes into.'}</div>
          {!look.boxes.length && (
            <div className="space-y-1.5">
              <div className="text-sm font-medium">Or pick the box:</div>
              <MarketPicker suggest={look.letter} onPick={(b) => into(b.id)} />
            </div>
          )}
        </CardContent></Card>
      )}
    </div>
  );
}
