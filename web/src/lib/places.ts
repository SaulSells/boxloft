import { APP } from '@/lib/appConfig';

/** Where things are, the way people say it (Alex 11-11): the building first ("Main warehouse"), then the spot.
 *  Boxes sit on the shelves of a building; open shelving is a named shelf (S1) in it, with items loose. */
export const HOME_SITE = APP.site;
/** A box or shelf name as written on it: A31, F7, S2. */
export const NAME_RE = /^[A-Z]{1,3}-?\d{1,4}$/i;

type Place = { code: string; name?: string | null; site?: string | null };

const readable = (t: string) => (t === t.toUpperCase() && /\s/.test(t) ? t.charAt(0) + t.slice(1).toLowerCase() : t);

/** The building or room: "Main warehouse", "Alex's house", "Collectibles room (breakroom)". */
export const buildingOf = (l: Place) => l.site || readable(l.name || l.code);

/** The spot inside the building: "Shelf S2", "Receiving dock", or '' when the building is the place. */
export const spotOf = (l: Place) => (l.site && l.name && l.name !== l.site ? l.name : '');

/** A named shelf of open shelving (S1): a spot in a building whose code is its name. */
export const isShelf = (l: Place & { kind?: string }) => !!l.site && !!l.name && l.name !== l.site && l.kind !== 'dock' && NAME_RE.test(l.code);

/** The warehouse floor itself, where the boxes sit (Alex 11-17, Fable review): never a place to put items loose.
 *  Every other place is, whatever it holds (Alex's house with a box in it still takes loose items). */
export const isBoxFloor = (l: Place) => l.site === HOME_SITE && (l.name || l.site) === HOME_SITE;

/** "Main warehouse · Receiving dock", "Main warehouse · Shelf S2", "Alex's house". */
export const placeText = (l: Place) => [buildingOf(l), spotOf(l)].filter(Boolean).join(' · ');

/** "A31", or "Box 7" for a box with no name. */
export const boxName = (b: { id: number; name?: string | null; kind?: string }) =>
  b.name ? b.name.toUpperCase() : `${b.kind === 'tote' ? 'Tote' : b.kind === 'pallet' ? 'Pallet' : 'Box'} ${b.id}`;
