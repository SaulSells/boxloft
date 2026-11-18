import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Camera, ImagePlus, LoaderCircle, Minus, Plus, Printer, ScanLine, Truck, Undo2 } from 'lucide-react';
import { ApiError, get, post, upload } from '@/api/client';
import type { Holder, PhotoMatch, PhotoRead } from '@/api/types';
import { BuyMatchList } from '@/pages/Product';
import { shrinkPhoto } from '@/lib/photo';
import { fnskuLabel, printLabels } from '@/lib/labels';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent } from '@/components/ui/card';
import { SearchInput, Thumb } from '@/components/app/common';
import { useToast } from '@/components/app/toast';
import { useScan, useScanBus } from '@/lib/scan';
import { navigate, useQueryParam } from '@/lib/router';
import { centsFromInput, dollarsForInput, money, units } from '@/lib/format';
import { touchCurrentBox } from '@/lib/currentBox';
import { cn } from '@/lib/utils';
import { parseCode } from '@/lib/codes';
import { NewItemCard, type NewCard, type NewPick } from '@/pages/NewItemCard';
import { EbayStrip } from '@/pages/EbayItems';
import { AmazonNote, isABox, type AmazonOk } from '@/components/app/AmazonNote';
import { PackageCard, isLabel, type LabelAnswer, type LabelItem, type ReturnItem } from '@/components/app/PackageCard';

/* Put away (flow audit 11-12, redesign 1). The box page is where items go in. Each scan saves 1 unit at once and
 * shows one line: what went in, which record it went to ("order of 10/23, delivered 11/03. 3 still to come"), Undo,
 * the count (tap it for a pile) and Change. The server picks the record with one rule (sorting.put); a card that
 * needs a tap shows only when 2 answers are both likely. Before: scan, pick, Add (2 steps per item, the next item
 * refused until Add), and Receive's form took 4 to 6. */

export type Option = {
  kind: 'audit' | 'order' | 'move' | 'new'; label: string; sub: string; left: number | null;
  item_id?: number; line_id?: number; line_ids?: number[]; product_id?: number; from?: { box_id?: number; location_id?: number }; cost_cents?: number | null;
};
export type Sale = { order_id: number; channel: string; external_id: string; ship_by: string | null; qty: number };
type Choose = { kind: 'choose'; code: string; title: string; image_url: string | null; photo_id: number | null; question: string;
  product: { id: number; sku: string; title: string } | null; here: number; options: Option[]; open_sales: Sale[]; amazon?: AmazonOk };
type Put = { kind: 'added'; add_id: number; how: Option['kind']; qty: number; title: string; sku: string; here: number; repeat: boolean;
  product_id: number; target: { code: string }; said: string; option: Option; others: Option[]; photo_id: number | null;
  image_url: string | null; open_sales: Sale[]; made?: boolean; replaced?: number; amazon?: AmazonOk;
  /** Extra stock (how 'new'): cost each now, null = not entered (Alex 11-17: typed on the line after the save). */
  cost_cents?: number | null };
/** Extra stock past what was bought (Alex 11-12): nothing is saved until Yes. A pile change carries add_id and qty. */
type Over = { kind: 'over'; question: string; detail: string; more: number; bought: number; after: number; title: string;
  image_url?: string | null; photo_id?: number | null; option?: Option; add_id?: number; qty?: number; amazon?: AmazonOk };
type Answer = Put | Choose | Over | LabelAnswer
  | { kind: 'new_product'; code: string; title: string }
  | { kind: 'target'; type: 'box' | 'location'; id: number }
  | { kind: 'wrong'; message: string };
/** photo_token: the line came from the Add card's photo; its Change sends it again (the server moves the photo). */
type Line = Put & { code: string; undone?: boolean; moved?: string; photo_token?: string };
type Found = { products: { id: number; sku: string; title: string; on_hand: number; photo_id: number | null }[];
  rows: { id: number; title: string; image_url: string | null; expected: number; accounted: number; store: string | null }[] };

const newId = () => `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
const same = (a: string, b: string) => a.trim().replace(/^0+/, '').toUpperCase() === b.trim().replace(/^0+/, '').toUpperCase();
const day = (d: string | null) => (d ? new Date(d.slice(0, 10) + 'T12:00:00').toLocaleDateString('en-US', { weekday: 'short', month: 'numeric', day: 'numeric' }) : '');
const offline = (e: unknown) => !(e instanceof ApiError) || e.status >= 502;

/** A sale waiting for this item (finding 9): it should go to the packing table, not into a box. */
export function SoldNote({ sales }: { sales: Sale[] }) {
  if (!sales.length) return null;
  const n = sales.reduce((a, s) => a + s.qty, 0);
  const first = sales[0]!;
  return (
    <div className="flex items-center gap-2 rounded-md border border-warning/60 bg-warning/15 px-2.5 py-2 text-sm">
      <Truck className="h-4 w-4 shrink-0" />
      <span className="min-w-0 flex-1"><b>{n} {n === 1 ? 'is' : 'are'} sold.</b> {first.ship_by ? `Ship by ${day(first.ship_by)}.` : ''} Take {n === 1 ? 'it' : 'them'} to the packing table.</span>
      <Button size="sm" variant="outline" className="h-9 shrink-0" onClick={() => navigate('/orders')}>Ship</Button>
    </div>
  );
}

/** A box page's list is keyed by the box id (as before); a place's by l + its id. */
const holderKey = (h: Holder) => ('box_id' in h ? String(h.box_id) : `l${h.location_id}`);
function storeKey(h: Holder) { return `boxloft-put-${holderKey(h)}`; }
function readLines(h: Holder): Line[] {
  try { return JSON.parse(sessionStorage.getItem(storeKey(h)) || '[]') as Line[]; } catch { return []; }
}
/** A save made on another page (Put away's new item card) shows on the box page's list with its Undo. */
export function rememberPut(h: Holder, line: Line) {
  try { sessionStorage.setItem(storeKey(h), JSON.stringify([line, ...readLines(h)].slice(0, 30))); } catch { /* ignore */ }
}
export type { Line as PutLine, Put as PutAnswer };

/** The page of a box or a place (Alex 11-17: "put items at Alex's house in no box"). */
export const holderPath = (h: Holder) => ('box_id' in h ? `/boxes/${h.box_id}` : `/locations/${h.location_id}`);

/** Scan items into a box, or loose at a place (`to`): the box page and the place page share it. */
export function AddItems({ to, boxName }: { to: Holder; boxName: string }) {
  const boxId = 'box_id' in to ? to.box_id : 0;   // 0 at a place: no box label matches it
  const placeId = 'location_id' in to ? to.location_id : 0;
  const base = holderPath(to);
  const isHere = (pc: ReturnType<typeof parseCode>) => (pc.kind === 'box' && pc.id === boxId) || (pc.kind === 'location' && pc.id === placeId);
  const qc = useQueryClient();
  const toast = useToast();
  const { feedback } = useScanBus();
  const addParam = useQueryParam('add');
  const putParam = useQueryParam('put');
  const labelParam = useQueryParam('label');   // from the Package page: a box scanned there opens here with the card
  const [q, setQ] = useState('');
  const [card, setCard] = useState<{ c: Choose; opt: number; qty: string; cost: string; replaces?: number; photo?: string } | null>(null);
  const [lines, setLinesState] = useState<Line[]>(() => readLines(to));
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<{ id: number; v: string } | null>(null);
  const [costing, setCosting] = useState<{ id: number; v: string } | null>(null);   // a line's cost box, open
  const [fresh, setFresh] = useState<NewCard | null>(null);   // a code nothing knows: the new item card
  // An item with no barcode (Alex 11-17: "Like take a picture"): the photo, Gemini's read and the buys it may be.
  // One scan_id per photo, sent with every tap on its result, so a double tap saves once (Fable review 11-17).
  const [shot, setShot] = useState<{ preview: string; res: PhotoRead | null; err: string | null; scanId: string } | null>(null);
  const [freshPhoto, setFreshPhoto] = useState<string | null>(null);   // the photo a new item card from a photo keeps
  const photoInput = useRef<HTMLInputElement>(null);
  // A carrier label scanned here (Alex 11-16): what is in the box, each item one tap into this box. Stays open while
  // items are scanned, and refreshes after each save.
  const [pkg, setPkg] = useState<LabelAnswer | null>(null);
  const pkgRef = useRef<LabelAnswer | null>(null);
  pkgRef.current = pkg;
  // Past what was bought: the question, what Yes sends, and what No does (put a Changed line back as it was).
  const [over, setOver] = useState<{ a: Over; yes: () => void; no?: () => void } | null>(null);
  const pickRef = useRef<(() => NewPick | null) | null>(null);
  const inFlight = useRef(false);
  const setLines = (f: (l: Line[]) => Line[]) => setLinesState((cur) => {
    const next = f(cur).slice(0, 30);
    try { sessionStorage.setItem(storeKey(to), JSON.stringify(next)); } catch { /* ignore */ }
    return next;
  });
  const found = useQuery({
    queryKey: ['box-add-search', q],
    queryFn: () => get<Found>(`/api/box-add/search?q=${encodeURIComponent(q.trim())}`),
    enabled: q.trim().length > 1 && !card && !fresh && !over,
  });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: boxId ? ['box', boxId] : ['location', placeId] });
    qc.invalidateQueries({ queryKey: ['boxes'] });
    qc.invalidateQueries({ queryKey: ['locations'] });
  };

  /** One save. The same scan_id is sent again up to 2 times on a lost reply, so a weak signal never adds twice. */
  const send = async (body: Record<string, unknown>, path = '/api/put'): Promise<Answer> => {
    const scan_id = newId();
    for (let attempt = 0; ; attempt++) {
      try {
        return await post<Answer>(path, { ...to, scan_id, ...body });
      } catch (e) {
        if (offline(e) && attempt < 2) { await new Promise((r) => setTimeout(r, 700)); continue; }
        throw e;
      }
    }
  };

  /** The new item card for a code nothing knows (or a typed name with no match). */
  const openCard = async (code: string, name = '', photo: string | null = null) => {
    try {
      // The name goes too (Alex 11-17): with no barcode, the card shows the items that look alike before Yes makes one.
      const c = await get<NewCard>(`/api/put/new-card?code=${encodeURIComponent(code)}&title=${encodeURIComponent(name)}&${boxId ? `box_id=${boxId}` : `location_id=${placeId}`}`);
      setFreshPhoto(photo);
      setFresh({ ...c, title: c.title || name, image_url: photo ? `/api/photo-id/${photo}` : c.image_url });
    } catch (e) {
      toast(offline(e) ? 'No signal: could not look the item up. Scan it again.' : (e instanceof Error ? e.message : 'Lookup failed'), { tone: 'error' });
    }
  };

  const put = async (body: Record<string, unknown>, scanned: boolean, code = '', path = '/api/put', onNo?: () => void) => {
    if (inFlight.current) {
      if (scanned) feedback(false, 'Still saving the last scan');
      return;
    }
    inFlight.current = true;
    setBusy(true);
    try {
      const r = await send(body, path);
      if (r.kind === 'added') {
        setFresh(null);
        setFreshPhoto(null);
        setShot(null);
        setOver(null);
        setLines((l) => [{ ...r, code: code || String(body.code ?? ''), ...(typeof body.photo_token === 'string' ? { photo_token: body.photo_token } : {}) },
          ...l.filter((x) => !r.replaced || x.add_id !== r.replaced)]);
        setQ('');
        touchCurrentBox();
        refresh();
        reloadPkg();
        if (r.open_sales.length) feedback('next', `${r.title.slice(0, 40)}: sold, ship it`, true);
        else if (r.amazon?.can_sell === false && r.amazon.in_a_box) feedback('next', `Can't sell on Amazon: ${r.title.slice(0, 30)}`, true);
        else if (scanned) feedback(true, `${r.title.slice(0, 44)} into ${r.target.code}`);
      } else if (isLabel(r)) {   // a carrier label: what is inside, logged as in hand by the server
        if (scanned) feedback(r.kind === 'label' || r.kind === 'label_return' ? true : 'next', r.kind === 'label' ? `${r.carrier || 'Carrier'} box: ${r.left} to put away`
          : r.kind === 'label_return' ? `Buyer return: ${r.items[0]?.reason ?? ''}`.slice(0, 60)
          : r.kind === 'label_unknown' ? 'No order has this tracking number' : 'Which package is it?', r.kind !== 'label' && r.kind !== 'label_return');
        setPkg(r);
      } else if (r.kind === 'over') {   // nothing saved: Yes sends the same save with the record the server picked
        if (scanned) feedback('next', r.question, true);
        setOver({ a: r, yes: () => void put({ ...body, option: r.option, over_ok: true }, false, code, path), no: onNo });
      } else if (r.kind === 'choose') {
        if (scanned) feedback('next', r.question, true);
        setCard({ c: { ...r, code: r.code || code }, opt: 0, qty: String(Number(body.qty) || 1), cost: dollarsForInput(r.options[0]?.cost_cents),
          ...(typeof body.photo_token === 'string' ? { photo: body.photo_token } : {}) });
        setShot(null);
      } else if (r.kind === 'target') {
        if (scanned) feedback(true);
        navigate(r.type === 'box' ? `/boxes/${r.id}` : `/locations/${r.id}`);
      } else if (r.kind === 'new_product') {
        if (scanned) feedback('next', 'New item: is this it?', true);
        await openCard(r.code || code);
      } else {
        if (scanned) feedback(false, r.message);
        toast(r.message, { tone: 'error' });
      }
    } catch (e) {
      const msg = offline(e) ? `Not added: no signal. Nothing was saved. Scan it again when the signal is back.` : (e instanceof Error ? e.message : 'Save failed');
      if (scanned) feedback(false, offline(e) ? 'Not added: no signal' : msg.slice(0, 60));
      toast(msg, { tone: 'error' });
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };

  /** The open package card again, from the server (after a save, here or by the other person). */
  const reloadPkg = () => {
    const cur = pkgRef.current;
    if (cur?.kind !== 'label' && cur?.kind !== 'label_return') return;
    const url = cur.kind === 'label' ? `/api/labels/card?tracking_id=${encodeURIComponent(cur.tracking_id)}` : `/api/labels/card?code=${encodeURIComponent(cur.tracking_id)}`;
    void get<LabelAnswer>(url).then((c) => {
      const now = pkgRef.current;
      if ((now?.kind === 'label' || now?.kind === 'label_return') && now.tracking_id === cur.tracking_id) setPkg(c);
    }).catch(() => { /* the card stays as it was */ });
  };
  /** A buyer return (Alex 11-16): back into this box, or out as damaged. One action each, with Undo on the card. */
  const takeReturn = async (i: ReturnItem, qty: number, condition: 'good' | 'damaged') => {
    setBusy(true);
    try {
      setPkg(await post<LabelAnswer>(`/api/buyer-returns/${i.return_id}/take`, { ...(condition === 'good' ? to : {}), qty, condition, seen_left: i.left }));
      touchCurrentBox();
      refresh();
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not save it', { tone: 'error' });
      reloadPkg();
    } finally {
      setBusy(false);
    }
  };
  const undoReturn = async (actionId: number) => {
    setBusy(true);
    try { await post('/api/undo', { action_id: actionId }); refresh(); reloadPkg(); } catch (e) { toast(e instanceof Error ? e.message : 'Could not undo it', { tone: 'error' }); } finally { setBusy(false); }
  };
  const putFromPkg = (i: LabelItem, qty: number, tracking: string) =>
    void put({ line_id: i.line_id, qty, tracking_id: tracking }, false, '', '/api/labels/put');
  const pickPkg = async (tracking: string) => {
    if (pkg?.kind !== 'label_ambiguous') return;
    try { setPkg(await post<LabelAnswer>('/api/labels/pick', { code: pkg.code, tracking_id: tracking })); } catch (e) { toast(e instanceof Error ? e.message : 'Could not pick it', { tone: 'error' }); }
  };
  useEffect(() => {
    if (!labelParam) return;
    navigate(base, true);
    void get<LabelAnswer | { kind: 'not_label' }>(`/api/labels/card?code=${encodeURIComponent(labelParam)}`)
      .then((c) => { if (isLabel(c)) setPkg(c); }).catch(() => { /* scan it again */ });
  }, [labelParam, base]); // eslint-disable-line react-hooks/exhaustive-deps

  // From the shell (an item scanned on another page with this box set), or back from New product.
  useEffect(() => {
    if (putParam) { navigate(base, true); void put({ code: putParam }, true, putParam); }
    else if (addParam) { navigate(base, true); void put({ product_id: Number(addParam) }, false); }
  }, [putParam, addParam, base]); // eslint-disable-line react-hooks/exhaustive-deps

  const o = card?.c.options[card.opt];
  const max = o?.left ?? 10000;
  const n = Math.max(0, Math.floor(Number(card?.qty) || 0));

  const saveFresh = (p: NewPick | null) => {
    if (!fresh) return;
    if (!p) { feedback(false, 'Choose on the card first, or type what it is', true); return; }
    void put({ code: fresh.code, title: p.title, line_id: p.line_id, listing_id: p.listing_id ?? null, product_id: p.product_id ?? null,
      ...(freshPhoto ? { photo_token: freshPhoto } : {}) }, false, fresh.code, '/api/put/new');
  };

  /** Alex 11-17: an item with no barcode. The photo shows at once; the server reads it (about 5 s) and lists the
   *  items and buys it may be. A tap puts 1 here (the same save as a scan); "New item" opens the new item card. */
  const takePhoto = async (files: FileList | null) => {
    const f = files?.[0];
    if (photoInput.current) photoInput.current.value = '';
    if (!f) return;
    setQ('');
    let blob: Blob;
    try { blob = await shrinkPhoto(f); } catch { toast('Could not read that photo. Take it again.', { tone: 'error' }); return; }
    const scanId = newId();
    setShot({ preview: URL.createObjectURL(blob), res: null, err: null, scanId });
    try {
      const r = await upload<PhotoRead>('/api/photo-id', blob);
      setShot((s) => (s && s.scanId === scanId ? { ...s, res: r } : s));
    } catch (e) {
      const err = offline(e) ? 'No signal: the photo did not send. Take it again when the signal is back.' : (e instanceof Error ? e.message : 'The photo did not send');
      setShot((s) => (s && s.scanId === scanId ? { ...s, err } : s));
    }
  };
  const pickShot = (m: PhotoMatch) => {
    if (!shot?.res) return;
    const tag = { photo_token: shot.res.photo, scan_id: shot.scanId };
    if (m.kind === 'product') void put({ product_id: m.id, ...tag }, false);
    else if (m.kind === 'audit') void put({ item_id: m.id, ...tag }, false);
    else void put({ line_id: m.id, title: m.title, ...tag }, false, '', '/api/put/new');
  };
  const shotNew = () => {
    const rd = shot?.res?.read;
    const photo = shot?.res?.photo ?? null;
    setShot(null);
    void openCard('', rd?.is_product ? rd.title : '', photo);
  };
  const notThis = () => {
    const c = fresh?.code || '';
    setFresh(null);
    navigate(`/inventory/new?${new URLSearchParams({ return: boxId ? 'box' : 'place', box: String(boxId || placeId), ...(c ? { code: c } : {}) })}`);
  };

  useScan(async (code) => {
    if (over) {   // past what was bought: this box's label is Yes; the same item again (a test scan) or another waits
      const pc = parseCode(code);
      if (isHere(pc)) { feedback(true); answerOver(true); return; }
      feedback(false, 'Answer the card first: Yes or No', true);
      return;
    }
    if (fresh) {   // the new item card waits: this box's label or the same code again is Yes
      const pc = parseCode(code);
      if (isHere(pc) || (fresh.code && same(code, fresh.code))) { feedback(true); saveFresh(pickRef.current?.() ?? null); return; }
      if (pc.kind !== 'box' && pc.kind !== 'location') { feedback(false, 'Answer the new item card first', true); return; }
      setFresh(null);
    }
    if (card) {
      if (card.c.code && same(code, card.c.code)) {   // the same item again while the card waits: one more
        const next = Math.min(max, n + 1);
        setCard({ ...card, qty: String(next) });
        feedback(next > n, next > n ? `${next} of this item` : `Only ${max} on that choice`);
        return;
      }
      if (!/^S[BL]/i.test(code)) {
        feedback(false, `Answer the card for ${card.c.title.slice(0, 30)} first`, true);
        return;
      }
    }
    await put({ code }, true, code);
  });

  const choose = async () => {
    if (!card || !o || !n) return;
    // Change: the server takes the first save out and puts this one in, in one request (replaces). Through the line's
    // Undo it archived an item the new item card made, so the new save failed (Alex 11-16). An 'over' answer changes
    // nothing, so No leaves the first save as it was.
    const body = { code: card.c.code, product_id: card.c.product?.id, option: o, qty: n, ...(o.kind === 'new' ? { option: { ...o, cost_cents: centsFromInput(card.cost) } } : {}),
      ...(card.replaces ? { replaces: card.replaces } : {}), ...(card.photo ? { photo_token: card.photo } : {}) };
    setCard(null);
    await put(body, false, card.c.code);
  };

  const answerOver = (yes: boolean) => {
    if (!over) return;
    setOver(null);
    if (yes) over.yes();
    else if (over.no) over.no();
    else toast(over.a.add_id ? 'Count not changed.' : 'Nothing saved.');
  };

  const undo = async (ln: Line) => {
    try {
      const u = await post<{ unmade?: boolean }>(`/api/box-add/${ln.add_id}/undo`);
      setLines((l) => l.map((x) => (x.add_id === ln.add_id ? { ...x, undone: true } : x)));
      toast(u.unmade ? `Undone: ${ln.title.slice(0, 50)} came out of ${ln.target.code}, and the new item is gone from Boxloft.`
        : `Undone: ${units(ln.qty)} of ${ln.title.slice(0, 50)} came back out of ${ln.target.code}.`);
      refresh();
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Undo failed', { tone: 'error' });
    }
  };

  /** Amazon said no (Alex 11-13): what this line put in goes to the eBay box, one move with its own Undo. */
  const moveOut = async (ln: Line, dest: { id: number; code: string }) => {
    try {
      const r = await post<{ action_id: number }>('/api/move', { from: to, to: { box_id: dest.id }, lines: [{ product_id: ln.product_id, qty: ln.qty }] });
      setLines((l) => l.map((x) => (x.add_id === ln.add_id ? { ...x, moved: dest.code } : x)));
      refresh();
      toast(`Moved ${units(ln.qty)} of ${ln.title.slice(0, 40)} to ${dest.code}. Put ${ln.qty === 1 ? 'it' : 'them'} in that box.`, {
        undo: async () => {
          try {
            await post('/api/undo', { action_id: r.action_id });
            setLines((l) => l.map((x) => (x.add_id === ln.add_id ? { ...x, moved: undefined } : x)));
            refresh();
          } catch (e) { toast(e instanceof Error ? e.message : 'Undo failed', { tone: 'error' }); }
        },
      });
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Move failed', { tone: 'error' });
    }
  };

  /** Alex 11-17: "When scanning an item in that isn't in the audit / linked via an order I have no way of adding a buy
   *  cost." Extra stock's cost each, typed on its line after the save; blank = not entered. */
  const saveCost = async (ln: Line, v: string) => {
    const cents = centsFromInput(v);
    if (v.trim() && cents === null) { toast('Type the cost in dollars, like 12.99', { tone: 'error' }); return; }
    setCosting(null);
    if (cents === (ln.cost_cents ?? null)) return;
    try {
      const r = await post<{ cost_cents: number | null; units: number }>(`/api/put/${ln.add_id}/cost`, {
        cost_cents: cents, ...(ln.cost_cents !== undefined ? { seen: { cost_cents: ln.cost_cents } } : {}) });
      setLines((l) => l.map((x) => (x.add_id === ln.add_id ? { ...x, cost_cents: r.cost_cents } : x)));
      toast(r.cost_cents === null ? 'Cost cleared: not entered.' : `Saved: ${money(r.cost_cents)} each for ${units(r.units)}.`);
      refresh();
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not save the cost', { tone: 'error' });
      if (e instanceof ApiError && e.status === 409) {   // someone else typed one: show theirs
        const m = /changed this cost to \$([\d,.]+)/.exec(e.message);
        const theirs = m ? centsFromInput(m[1]!) : /not entered/.test(e.message) ? null : undefined;
        if (theirs !== undefined) setLines((l) => l.map((x) => (x.add_id === ln.add_id ? { ...x, cost_cents: theirs } : x)));
      }
    }
  };

  const setCount = async (ln: Line, v: string, overOk = false) => {
    const want = Math.floor(Number(v) || 0);
    setEditing(null);
    if (!want || want === ln.qty) return;
    try {
      const r = await post<(Put & { extra_line?: Put }) | Over>(`/api/put/${ln.add_id}/qty`, { qty: want, over_ok: overOk });
      if (r.kind === 'over') {   // a bigger pile of extra stock past what was bought: nothing changed until Yes
        setOver({ a: { ...r, image_url: ln.image_url, photo_id: ln.photo_id }, yes: () => void setCount(ln, v, true) });
        return;
      }
      // A pile past its row or orders: they fill up, and the rest is a second line (extra stock) with its own Undo.
      const extra = r.extra_line ? [{ ...r.extra_line, code: ln.code, image_url: ln.image_url, photo_id: ln.photo_id }] : [];
      setLines((l) => [...extra, ...l.map((x) => (x.add_id === ln.add_id ? { ...r, code: ln.code, photo_token: ln.photo_token } : x))]);
      if (r.extra_line) toast(`${units(r.qty)} on its ${r.how === 'order' ? 'orders' : 'Stock audit row'}, ${units(r.extra_line.qty)} as extra stock.`);
      refresh();
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not change the count', { tone: 'error' });
    }
  };

  const change = (ln: Line) => setCard({
    c: { kind: 'choose', code: ln.code, title: ln.title, image_url: ln.image_url, photo_id: ln.photo_id, question: 'What is it instead?',
      product: { id: ln.product_id, sku: ln.sku, title: ln.title }, here: ln.here, options: ln.others, open_sales: ln.open_sales },
    opt: 0, qty: String(ln.qty), cost: dollarsForInput(ln.others[0]?.cost_cents), replaces: ln.add_id,
    ...(ln.photo_token ? { photo: ln.photo_token } : {}),
  });

  const live = lines.filter((l) => !l.undone);
  return (
    <Card>
      <CardContent className="space-y-3 p-4">
        <div className="flex items-center gap-2 text-sm font-semibold"><ScanLine className="h-4 w-4" />{boxId ? `Scan items into ${boxName}` : `Scan items: they go in at ${boxName}, no box`}</div>
        {pkg && !over && <PackageCard r={pkg} boxName={boxName} busy={busy} onPut={putFromPkg} onPick={(t) => void pickPkg(t)} onClose={() => setPkg(null)} onReturn={(i, n, c) => void takeReturn(i, n, c)} onUndo={(a) => void undoReturn(a)} />}
        {over ? (
          <div className="space-y-3" role="alertdialog" aria-label="More than you bought">
            <div className="flex gap-3">
              <Thumb photoId={over.a.photo_id ?? null} url={over.a.image_url ?? null} size="focus" />
              <div className="min-w-0 flex-1 text-sm font-medium leading-snug">{over.a.title}</div>
            </div>
            <AmazonNote a={over.a.amazon} boxName={boxName} />
            <div className="rounded-md border border-warning/60 bg-warning/15 p-3">
              <div className="text-base font-semibold">{over.a.question}</div>
              <div className="mt-1 text-sm text-muted-foreground">{over.a.detail}</div>
            </div>
            <div className="flex gap-2">
              <Button className="h-11 flex-1" disabled={busy} onClick={() => answerOver(true)}>
                {over.a.add_id ? `Yes, make it ${over.a.qty}` : `Yes, put ${over.a.more} in ${boxName}`}
              </Button>
              <Button variant="outline" className="h-11 flex-1" disabled={busy} onClick={() => answerOver(false)}>No</Button>
            </div>
          </div>
        ) : fresh ? (
          <>
          <AmazonNote a={fresh.amazon} inA={isABox(boxName)} boxName={boxName} />
          <NewItemCard card={fresh} boxName={boxName} busy={busy} pickRef={pickRef} onSave={saveFresh}
            onTwin={(id) => void put({ code: fresh.code, product_id: id, option: undefined, ...(freshPhoto ? { photo_token: freshPhoto } : {}) }, false, fresh.code)}
            onNotThis={notThis} onCancel={() => { setFresh(null); setFreshPhoto(null); }} />
          </>
        ) : card ? (
          <div className="space-y-3">
            <div className="flex gap-3">
              <Thumb photoId={card.c.photo_id} url={card.c.image_url} size="focus" />
              <div className="min-w-0 flex-1 text-sm">
                <div className="font-medium leading-snug">{card.c.title}</div>
                <div className="mt-1 text-xs text-muted-foreground">{card.c.here ? `${card.c.here} already in ${boxName}` : `None in ${boxName} yet`}</div>
              </div>
            </div>
            <SoldNote sales={card.c.open_sales ?? []} />
            <AmazonNote a={card.c.amazon} boxName={boxName} />
            <div className="space-y-1.5" role="radiogroup">
              <Label>{card.c.question}</Label>
              {card.c.options.map((x, i) => (
                <button key={i} type="button" role="radio" aria-checked={i === card.opt}
                  className={cn('min-h-11 w-full rounded-lg border p-2.5 text-left text-sm', i === card.opt ? 'border-primary bg-primary/5 ring-1 ring-primary' : 'hover:bg-muted/50')}
                  onClick={() => setCard({ ...card, opt: i, cost: dollarsForInput(x.cost_cents), qty: x.left !== null && n > x.left ? String(x.left) : card.qty })}>
                  <div className="font-medium">{x.label}</div>
                  {x.sub && <div className="text-xs text-muted-foreground">{x.sub}</div>}
                </button>
              ))}
            </div>
            <div className="flex items-end gap-3">
              <div className="space-y-1.5">
                <Label htmlFor="add-qty">How many{o?.left != null ? ` (up to ${o.left})` : ''}</Label>
                <div className="flex items-center gap-1.5">
                  <Button type="button" variant="outline" size="icon" className="h-11 w-11" aria-label="One fewer" disabled={n <= 1} onClick={() => setCard({ ...card, qty: String(Math.max(1, n - 1)) })}><Minus className="h-4 w-4" /></Button>
                  <Input id="add-qty" inputMode="numeric" className="h-11 w-16 text-center text-base font-semibold tabular-nums" value={card.qty}
                    onChange={(e) => setCard({ ...card, qty: e.target.value.replace(/\D/g, '').slice(0, 5) })} />
                  <Button type="button" variant="outline" size="icon" className="h-11 w-11" aria-label="One more" disabled={n >= max} onClick={() => setCard({ ...card, qty: String(Math.min(max, n + 1)) })}><Plus className="h-4 w-4" /></Button>
                </div>
              </div>
              {o?.kind === 'new' && (
                <div className="min-w-0 flex-1 space-y-1.5">
                  <Label htmlFor="add-cost">Cost each</Label>
                  <Input id="add-cost" inputMode="decimal" className="h-11" placeholder="not entered" value={card.cost} onChange={(e) => setCard({ ...card, cost: e.target.value })} />
                </div>
              )}
            </div>
            {n > max && <p className="text-xs text-destructive">Only {max} on this choice.</p>}
            <div className="flex gap-2">
              <Button className="h-11 flex-1" disabled={busy || !n || n > max} onClick={() => void choose()}>Put {n || ''} in {boxName}</Button>
              <Button variant="outline" className="h-11" disabled={busy} onClick={() => setCard(null)}>Cancel</Button>
            </div>
          </div>
        ) : (
          <>
            <Button className="h-11 w-full" onClick={() => window.dispatchEvent(new Event('boxloft:camera'))}><Camera className="mr-1.5 h-4 w-4" />Scan with the camera</Button>
            <input ref={photoInput} type="file" accept="image/*" capture="environment" className="hidden" onChange={(e) => void takePhoto(e.target.files)} />
            <div className="flex gap-2">
              <SearchInput placeholder="No barcode? Type a name" value={q} onChange={(e) => { setQ(e.target.value); if (e.target.value) setShot(null); }} className="h-11 min-w-0 flex-1" />
              <Button type="button" variant="outline" className="h-11 shrink-0" aria-label="No barcode: take a photo of the item" onClick={() => photoInput.current?.click()}>
                <ImagePlus className="mr-1.5 h-4 w-4" />Photo
              </Button>
            </div>
            {shot && (() => {
              const rd = shot.res?.read;
              const matches = shot.res?.matches ?? [];
              const named = rd?.is_product && rd.title ? rd.title : '';
              return (
                <div className="space-y-3 rounded-lg border p-3" data-testid="photo-panel">
                  <div className="flex gap-3">
                    <img src={shot.preview} alt="Your photo" className="h-20 w-20 shrink-0 rounded-md border bg-white object-cover" />
                    <div className="min-w-0 flex-1 space-y-1 text-sm">
                      {!shot.res && !shot.err ? <p className="flex items-center gap-2 font-medium"><LoaderCircle className="h-4 w-4 animate-spin" />Reading the box... about 5 seconds</p>
                        : shot.err ? <p className="text-destructive">{shot.err}</p>
                        : shot.res?.error ? <p className="text-warning-ink">{shot.res.error}</p>
                        : <p><span className="text-muted-foreground">The photo says: </span><span className="font-medium">{named}</span></p>}
                      <div className="flex gap-3">
                        <button type="button" className="min-h-9 text-sm underline" onClick={() => photoInput.current?.click()}>Take it again</button>
                        <button type="button" className="min-h-9 text-sm underline" onClick={() => setShot(null)}>Cancel</button>
                      </div>
                    </div>
                  </div>
                  {matches.length > 0 && (
                    <BuyMatchList matches={matches} busy={busy} onPick={pickShot} action="Put 1"
                      title={`Is it one of these? A tap puts 1 in ${boxName}.`} />
                  )}
                  {(shot.res || shot.err) && (
                    <Button type="button" variant="outline" className="h-11 w-full justify-start" disabled={busy} onClick={shotNew}>
                      <Plus className="mr-1.5 h-4 w-4 shrink-0" /><span className="truncate">{matches.length ? 'None of these: new item' : 'New item'}{named ? `: ${named}` : ''}</span>
                    </Button>
                  )}
                </div>
              );
            })()}
            {q.trim().length > 1 && found.data && (
              <div className="divide-y rounded-lg border">
                {found.data.products.map((p) => (
                  <button key={`p${p.id}`} type="button" disabled={busy} className="flex w-full items-center gap-3 p-2.5 text-left hover:bg-muted/50"
                    onClick={() => void put({ product_id: p.id }, false)}>
                    <Thumb photoId={p.photo_id} />
                    <div className="min-w-0 flex-1"><div className="truncate text-sm font-medium">{p.title}</div><div className="truncate font-mono text-xs text-muted-foreground">{p.sku}</div></div>
                    <div className="shrink-0 text-xs tabular-nums text-muted-foreground">{p.on_hand} on hand</div>
                  </button>
                ))}
                {found.data.rows.map((r) => (
                  <button key={`r${r.id}`} type="button" disabled={busy} className="flex w-full items-center gap-3 p-2.5 text-left hover:bg-muted/50"
                    onClick={() => void put({ item_id: r.id }, false)}>
                    <Thumb photoId={null} url={r.image_url} />
                    <div className="min-w-0 flex-1"><div className="truncate text-sm font-medium">{r.title}</div><div className="text-xs text-muted-foreground">On the Stock audit list{r.store ? ` · ${r.store}` : ''}</div></div>
                    <div className="shrink-0 text-xs tabular-nums text-muted-foreground">{r.expected - r.accounted} to find</div>
                  </button>
                ))}
                {!found.data.products.length && !found.data.rows.length && (
                  <div className="space-y-2 p-3 text-sm">
                    <div>Nothing in Boxloft matches "{q.trim()}".</div>
                    <Button variant="outline" size="sm" onClick={() => { const t = q.trim(); setQ(''); void openCard(/^\d{8,14}$/.test(t) ? t : '', /^\d{8,14}$/.test(t) ? '' : t); }}>
                      <Plus className="mr-1 h-3.5 w-3.5" />New item: put it in {boxName}
                    </Button>
                  </div>
                )}
              </div>
            )}
            {/^E-?\d+$/i.test(boxName) && !q.trim() && (   // an eBay box: what is listed but not in a box yet (Alex 11-12)
              <EbayStrip boxName={boxName} busy={busy}
                onPut={(row, qty, seen) => put({ qty, seen_short: seen }, false, '', `/api/ebay-items/${row}/put`)} />
            )}
          </>
        )}
        {lines.length > 0 && (
          <div className="space-y-2">
            <div className="text-xs text-muted-foreground">Just put in: {units(live.reduce((a, l) => a + l.qty, 0))}. Newest first. A pile? Scan one, then tap its count and type how many.</div>
            {lines.map((ln) => (
              <div key={ln.add_id} className={cn('space-y-1.5 rounded-lg border p-2', ln.undone && 'opacity-50')}>
                <div className="flex items-start gap-2">
                  <Thumb photoId={ln.photo_id} url={ln.image_url} />
                  <div className="min-w-0 flex-1 text-sm">
                    <div className="line-clamp-2 font-medium">{ln.undone ? 'Undone: ' : ''}{ln.title}</div>
                    <div className="text-xs text-muted-foreground">{ln.made ? 'New item made. ' : ''}{ln.how === 'new' ? ln.said.replace(/\.? *Cost not entered\.?$/, '') : ln.said}{ln.repeat ? ' (already saved)' : ''}</div>
                    {ln.moved && <div className="text-xs font-medium">Moved to {ln.moved}: put {ln.qty === 1 ? 'it' : 'them'} in that box.</div>}
                  </div>
                  {!ln.undone && (editing?.id === ln.add_id ? (
                    <form className="flex shrink-0 gap-1" onSubmit={(e) => { e.preventDefault(); void setCount(ln, editing.v); }}>
                      <Input autoFocus inputMode="numeric" aria-label="How many" className="h-11 w-16 text-center text-base font-semibold" value={editing.v}
                        onChange={(e) => setEditing({ id: ln.add_id, v: e.target.value.replace(/\D/g, '').slice(0, 5) })} />
                      <Button type="submit" className="h-11">Save</Button>
                    </form>
                  ) : (
                    <button type="button" aria-label="Change how many" className="flex h-12 min-w-14 shrink-0 flex-col items-center justify-center rounded-md border-2 border-[hsl(var(--tap-border))] bg-[hsl(var(--tap))] px-2 leading-none text-primary"
                      onClick={() => setEditing({ id: ln.add_id, v: String(ln.qty) })}>
                      <span className="text-base font-semibold tabular-nums">{ln.qty}</span>
                      <span className="mt-0.5 text-[11px] font-medium">Change</span>
                    </button>
                  ))}
                </div>
                {!ln.undone && !ln.moved && ln.how === 'new' && (costing?.id === ln.add_id ? (
                  <form className="flex items-center gap-2" onSubmit={(e) => { e.preventDefault(); void saveCost(ln, costing.v); }}>
                    <Label htmlFor={`cost-${ln.add_id}`} className="shrink-0 text-sm">Cost each $</Label>
                    <Input id={`cost-${ln.add_id}`} autoFocus inputMode="decimal" placeholder="not entered" className="h-11 min-w-0 flex-1 text-base"
                      value={costing.v} onChange={(e) => setCosting({ id: ln.add_id, v: e.target.value })} />
                    <Button type="submit" className="h-11">Save</Button>
                  </form>
                ) : (
                  <button type="button" className="flex min-h-11 w-full items-center justify-between rounded-md border px-2.5 text-left text-sm hover:bg-muted/50"
                    onClick={() => setCosting({ id: ln.add_id, v: dollarsForInput(ln.cost_cents) })}>
                    <span>Cost each: {ln.cost_cents == null ? <span className="font-medium text-warning-ink">not entered</span> : <b>{money(ln.cost_cents)}</b>}</span>
                    <span className="font-medium text-primary">{ln.cost_cents == null ? 'Add cost' : 'Change'}</span>
                  </button>
                ))}
                {!ln.undone && <SoldNote sales={ln.open_sales} />}
                {!ln.undone && !ln.moved && <AmazonNote a={ln.amazon} boxName={boxName} compact onMove={(b) => moveOut(ln, b)} />}
                {!ln.undone && !ln.moved && (
                  <div className="flex gap-2">
                    <Button size="sm" variant="outline" className="h-9" onClick={() => void undo(ln)}><Undo2 className="mr-1 h-3.5 w-3.5" />Undo</Button>
                    {ln.others.length > 0 && <Button size="sm" variant="ghost" className="h-9" onClick={() => change(ln)}>Not this? Change</Button>}
                    {ln.made && !ln.photo_id && <Button size="sm" variant="ghost" className="h-9" onClick={() => navigate(`/inventory/${ln.product_id}`)}><ImagePlus className="mr-1 h-3.5 w-3.5" />Add a photo</Button>}
                    {ln.made && !ln.code && (   // no barcode (Alex 11-17): its SKU label, so the next one scans
                      <Button size="sm" variant="ghost" className="h-9" onClick={() => void printLabels(Array.from({ length: Math.max(1, ln.qty) }, () => fnskuLabel(ln.sku, ln.title, 'New')))}>
                        <Printer className="mr-1 h-3.5 w-3.5" />Print {ln.qty > 1 ? `${ln.qty} labels` : 'its label'}
                      </Button>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
