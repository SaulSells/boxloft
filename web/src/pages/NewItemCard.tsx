import { useRef, useState, type MutableRefObject, type ReactNode } from 'react';
import { Check, PackagePlus } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { TapAction, tapTile, Thumb } from '@/components/app/common';
import { day, money } from '@/lib/format';
import { cn } from '@/lib/utils';
import type { AmazonOk } from '@/components/app/AmazonNote';

/* A new item is a card, not a form (flow audit 11-12, redesign 2). Before: a code nothing knew opened New product,
 * 15 inputs over 3 screens, even when the catalog had filled them, and from Home it ended on the product page with
 * receiving to start over (4 to 7 steps). Now this card: the catalog's photo and name, the purchase it most likely
 * came on, a look-alike item to add the barcode to. Yes (or a box label scan) makes the item and puts 1 in the box,
 * one save with one Undo. Not this opens the full form.
 *
 * Listings on the card (Alex 11-12 3:20 PM): the Hydro Flask had an Amazon FBM listing and the card showed one Yes
 * that never named it; the Minecraft armadillo's listing was never linked. Now the listing is a green Link choice,
 * and every choice says what a tap does. An exact listing or item (same ASIN or barcode) is the one sure choice: Yes
 * goes away and the box label takes it. Look-alike listings (name only) wait for a tap; "make a new item" says it
 * links nothing.
 * Alex 11-24 (Funko Queen 4-pack): on Put away a tap only picks, and the box choices sat off screen, so "nothing
 * happens". A tap now scrolls to the box choices and the picked choice says Picked. */

/** What a picked choice shows in place of its Link / Use arrow. */
function Picked() {
  return <span className="flex shrink-0 items-center gap-1 text-sm font-semibold text-primary"><Check className="h-4 w-4" />Picked</span>;
}

export type CardLine = { id: number; title: string; order_id: string; source: string; ordered_at: string | null;
  left_to_receive: number; package_left?: number | null; why?: string | null; unit_cost_cents: number | null };
/** A listing the scan may be: same ASIN or barcode = exact; 'name' = looks alike, a person checks the photo. */
export type ListingOffer = { listing_id: number; channel: string; market: string; sku: string; asin: string; title: string;
  fulfillment: string; qty: number; live: boolean; match: 'asin' | 'barcode' | 'name'; score: number; image_url: string | null;
  product_id: number | null; product_title: string | null };
export type NewCard = { kind: 'new_item'; code: string; title: string; brand: string; image_url: string | null; asin: string;
  lines: CardLine[]; twins: { id: number; title: string; sku: string; photo_id: number | null; score: number; exact?: boolean }[];
  listings?: ListingOffer[]; amazon?: AmazonOk;
  delivered: (CardLine & { image_url: string | null; delivered_on: string | null })[] };
export type NewPick = { title: string; line_id: number | null; listing_id?: number | null; product_id?: number | null };

const store = (s: string) => (s === 'amazon' ? 'Amazon' : s);
const kindOf = (o: ListingOffer) => `${o.market}${o.channel === 'amazon' ? ` ${o.fulfillment}` : ''}`;
const MATCH = { asin: 'same ASIN', barcode: 'same barcode', name: 'name looks alike: check the photo' };

export function NewItemCard({ card, boxName, busy, onSave, onTwin, onNotThis, onCancel, boxSlot, pickRef }: {
  card: NewCard; boxName: string | null; busy: boolean;
  onSave: (p: NewPick) => void; onTwin: (productId: number) => void; onNotThis: () => void; onCancel: () => void;
  /** With no box set (Put page): the box choice goes here in place of the Yes button. null = nothing chosen yet. */
  boxSlot?: (pick: NewPick | null) => ReactNode;
  /** A box label scan saves the card: the page reads what the card holds now. */
  pickRef?: MutableRefObject<(() => NewPick | null) | null>;
}) {
  const choices = card.lines.length ? card.lines : card.delivered;
  const [line, setLine] = useState<number | null>(card.lines[0]?.id ?? null);
  const [title, setTitle] = useState(card.title);
  const picked = choices.find((l) => l.id === line);
  const name = picked && !card.title ? picked.title : title;
  const ready = !!(name.trim() || line);
  const plain = (): NewPick => ({ title: name.trim(), line_id: line });
  // On an order: the order line makes the item (cost, ASIN) and its ASIN links the listing, so listings are a note.
  const offers = card.lines.length ? [] : (card.listings ?? []);
  const exact = offers.filter((o) => o.match !== 'name');
  const alike = offers.filter((o) => o.match === 'name');
  const exactTwins = exact.length ? [] : card.twins.filter((t) => t.exact);
  const otherTwins = card.twins.filter((t) => !t.exact);
  const offerPick = (o: ListingOffer): NewPick => ({ title: o.product_title || o.title, line_id: null, listing_id: o.listing_id });
  const sure: NewPick | null = card.lines.length ? null : exact[0] ? offerPick(exact[0])
    : exactTwins[0] ? { title: exactTwins[0].title, line_id: null, product_id: exactTwins[0].id } : null;
  // What the box slot and a box label scan take: the sure choice, a tapped one (Put page), or the plain new item.
  const [mode, setMode] = useState<'plain' | NewPick | null>(sure ?? (alike.length ? null : 'plain'));
  const current = mode === 'plain' ? (ready ? plain() : null) : mode;
  if (pickRef) pickRef.current = () => current;
  const where = boxName ? `puts 1 in ${boxName}` : 'puts 1 in the box you pick';
  const slotRef = useRef<HTMLDivElement>(null);
  const choose = (p: NewPick | 'plain') => {
    if (boxSlot) {   // the save is the box: take the person to the box choices
      setMode(p);
      requestAnimationFrame(() => slotRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
    }
    else if (p === 'plain') { if (ready) onSave(plain()); }
    else onSave(p);
  };
  const isOn = (p: NewPick) => !!boxSlot && !!mode && mode !== 'plain'
    && (p.listing_id ? mode.listing_id === p.listing_id : !!p.product_id && mode.product_id === p.product_id);
  const fbmNote = card.lines.length ? (card.listings ?? []).filter((o) => o.match !== 'name') : [];
  return (
    <div className="space-y-3" data-testid="new-item-card">
      <div className="flex gap-3">
        <Thumb photoId={null} url={card.image_url || (picked as { image_url?: string | null } | undefined)?.image_url} size="focus" />
        <div className="min-w-0 flex-1 text-sm">
          <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground"><PackagePlus className="h-3.5 w-3.5" />Not in Boxloft yet</div>
          {card.title || picked ? (
            <div className="mt-0.5 font-medium leading-snug">{name}</div>
          ) : (
            <div className="mt-1 space-y-1">
              <Label htmlFor="new-title">What is it?</Label>
              <Input id="new-title" className="h-11 text-base" placeholder="Brand, model, size" value={title} onChange={(e) => setTitle(e.target.value)} />
            </div>
          )}
          <div className="mt-0.5 text-xs text-muted-foreground">{[card.brand, card.code].filter(Boolean).join(' · ')}</div>
        </div>
      </div>
      {offers.length > 0 && (
        <div className="space-y-1.5" data-testid="listing-offers">
          <Label>{exact.length ? 'It is on your listing. Tap to link it:' : 'Is it one of your listings? The names look alike, so check the photo:'}</Label>
          {[...exact, ...alike].map((o) => {
            const p = offerPick(o);
            return (
              <button key={o.listing_id} type="button" disabled={busy} onClick={() => choose(p)}
                className={cn('flex min-h-16 w-full items-start gap-3 p-2.5', tapTile, isOn(p) && 'ring-2 ring-primary')}>
                <Thumb photoId={null} url={o.image_url || (o.match !== 'name' ? card.image_url : null)} />
                <span className="min-w-0 flex-1 text-sm">
                  <span className="block text-xs font-semibold text-primary">
                    {o.product_id ? `The item on your ${kindOf(o)} listing` : `Your ${kindOf(o)} listing`}
                  </span>
                  <b className="line-clamp-2 leading-snug">{o.product_title || o.title}</b>
                  <span className="block text-xs text-muted-foreground">SKU {o.sku} · {o.live ? `${o.qty} for sale` : 'not for sale now'} · {MATCH[o.match]}</span>
                  <span className="mt-1 block text-xs">
                    {o.product_id ? `Link adds this barcode to that item and ${where}.`
                      : `Link makes the item from this listing, links the two, and ${where}. Orders on this listing then find it there.`}
                  </span>
                </span>
                {isOn(p) ? <Picked /> : <TapAction label="Link" />}
              </button>
            );
          })}
        </div>
      )}
      {[...exactTwins, ...otherTwins].map((t) => {
        const p: NewPick = { title: t.title, line_id: null, product_id: t.id };
        return (
          <button key={t.id} type="button" className={cn('flex min-h-14 w-full items-center gap-3 p-2.5', tapTile, isOn(p) && 'ring-2 ring-primary')}
            disabled={busy} onClick={() => (t.exact ? choose(p) : onTwin(t.id))}>
            <Thumb photoId={t.photo_id} />
            <span className="min-w-0 flex-1 text-sm"><b>It's {t.title}</b>
              <span className="block text-xs text-muted-foreground">{t.exact ? 'Same ASIN. ' : ''}Adds this barcode to that item and {where}</span></span>
            {isOn(p) ? <Picked /> : <TapAction label="Use" />}
          </button>
        );
      })}
      {fbmNote[0] && (
        <p className="text-xs text-muted-foreground">Also on your {kindOf(fbmNote[0])} listing (SKU {fbmNote[0].sku}): the item links to it by its ASIN.</p>
      )}
      {choices.length > 0 && (
        <div className="space-y-1.5" role="radiogroup">
          <Label>{card.lines.length ? 'Bought on' : 'No match for this code. Delivered and not scanned in:'}</Label>
          {choices.map((l) => (
            <button key={l.id} type="button" role="radio" aria-checked={line === l.id}
              className={cn('min-h-11 w-full rounded-lg border p-2.5 text-left text-sm', line === l.id ? 'border-primary bg-primary/5 ring-1 ring-primary' : 'hover:bg-muted/50')}
              onClick={() => setLine(line === l.id ? null : l.id)}>
              {!card.lines.length && <div className="line-clamp-2 font-medium">{l.title}</div>}
              <div className={card.lines.length ? 'font-medium' : 'text-xs text-muted-foreground'}>
                {store(l.source)} order of {day(l.ordered_at)}, {money(l.unit_cost_cents, 'cost not entered')}, {l.left_to_receive} coming
              </div>
            </button>
          ))}
        </div>
      )}
      {!sure && (offers.length > 0 ? (
        <div className="space-y-1">
          <Button variant="outline" className={cn('h-12 w-full text-base', boxSlot && mode === 'plain' && 'ring-2 ring-primary')}
            disabled={busy || !ready} onClick={() => choose('plain')}>None of these: make a new item</Button>
          <p className="text-xs text-muted-foreground">Makes a new item from this barcode, linked to no listing, and {where}.</p>
        </div>
      ) : !boxSlot && (
        <div className="space-y-1">
          <Button className="h-12 w-full text-base" disabled={busy || !ready} onClick={() => onSave(plain())}>Yes, put 1 in {boxName}</Button>
          <p className="text-xs text-muted-foreground">Yes makes it a new item in Boxloft.</p>
        </div>
      ))}
      {boxSlot && <div ref={slotRef} className="scroll-mt-24">{boxSlot(current)}</div>}
      {!boxSlot && (
        <p className="text-xs text-muted-foreground">
          {sure ? `Or scan the ${boxName} label to link it.` : alike.length ? 'Tap one of the choices above.' : `Or scan the ${boxName} label.`} A photo can go on after.
        </p>
      )}
      <div className="flex gap-2">
        <Button variant="ghost" className="h-11 flex-1" disabled={busy} onClick={onNotThis}>Not this: fill in the form</Button>
        <Button variant="outline" className="h-11" disabled={busy} onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  );
}
