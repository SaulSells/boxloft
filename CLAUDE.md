# Boxloft: notes for coding agents

A warehouse app for resellers and small 3PLs. Stock sells on Amazon (FBA and FBM), eBay and Walmart; client stock (3PL) is kept apart by owner. Read this whole file before a change. The comments in the code name Alex (the owner) and Sam (floor staff) and the date each rule came from; keep that habit: the reason next to the rule.

## Stack

- `backend/`: Python standard library only. `http.server` with threads, SQLite in WAL mode. Don't add Flask, FastAPI or an ORM.
- `web/`: React 19, Vite, Tailwind 4, shadcn/ui.
- `extension/`: Chrome MV3 extension, loaded unpacked (see README).
- `tests/`: pytest, about 600 tests. `.venv/bin/python -m pytest -q tests`. Tests never call the network: every outside service is faked.
- Settings: `backend/config.py` (business values from env), `backend/ai.py` (which AI service). The web app gets the settings as `window.__APP__` in index.html (`web/src/lib/appConfig.ts`).

Local dev:

```bash
BOXLOFT_DATA=./data python3 scripts/setup.py admin "Your Name" --demo   # first admin + sample warehouse
BOXLOFT_DATA=./data BOXLOFT_DEV=1 python3 backend/server.py           # API + built app on :5101
cd web && npm run dev                                                  # Vite on :5173, proxies /api
```

## The ledger (read before touching wms.py)

- Every change is an `action` made of `stock_move` rows (a product or a unit, from a holder to a holder) and `box_move` rows (a box from one location to another).
- A holder is a box or a location. Virtual locations (VENDOR, CUSTOMER, FBA, WFS, CLIENT, ADJUST, DAMAGED) stand for the outside world, so per product all holders sum to 0.
- `stock_move`, `box_move` and `action` are append-only. Triggers abort any UPDATE or DELETE, except that `action.reversed_by` can be set once.
- `stock_level` is a cache written in the same transaction as the move. `check_integrity()` recomputes it from the ledger every night at 3:15 AM, compares it with the cache and shows the result on the Overview. It repairs nothing: a difference is a bug to find.
- Mistakes get fixed with `undo()`, which writes the opposite moves. Never edit a move.
- Avg cost is the weighted average over receipts that weren't undone. One-off units (`tracking='unit'`) carry their own cost.
- Cost is optional. A receipt with no cost saves 0 with `receipt.cost_missing = 1`; Avg cost leaves it out, and the item's "Usual unit cost" fills it later (`Wms._fill_costs`, each fill a `cost_fill` row). A cost a person typed is never overwritten.
- Every product has an owner (the business itself is owner 1, or a 3PL client), so client stock never mixes into your value.
- Many other tables are append-only too, with the same triggers (`box_add`, `audit_outcome`, `audit_count`, `audit_extra`, `listing_mark`, `cost_set`, `cost_fill`, `receipt_void`, `product_merge`). Some logs are append-only by habit only (`label_scan`, `ship_unscanned`). When a table's comment says append-only, add rows; never update them. A new append-only table gets the triggers.

## Two people at once

The app is used by several people on phones at the same time. Nothing may overwrite or double count.

- Every POST runs under `wms.op_lock` (`server.locked`) from its first check to its last write. Background writers take it too. Routes that wait on the network (sign-in, connections, listing refresh, photo ID) are in `UNLOCKED` and never take the lock. Locked scan routes (`/api/put`, `/api/audit-sort/*`) do their catalog and Amazon lookups in `server._prefetch`, before the lock, so one person's slow lookup never holds up the other's save.
- A save from a screen that showed other numbers is refused with HTTP 409, the name of whoever changed it and what's left. The page sends `seen`, the values it showed. `server.EDIT_GUARDS` (regex to table) checks them for edit routes; audit saves use `audit.check_seen`. **A new edit route gets a line in `EDIT_GUARDS` and a `seen` in its page.**
- The 409 check is opt-in: a POST that sends no `seen` is saved without it (old open tabs keep working). Every page in `web/` sends it; a script or new client that wants the check must send it too.
- Ledger moves (receive, move, remove, ship) are checked under the lock against what is really there, `seen` or not.
- Scans carry a client `scan_id`, so a resent request never adds a unit twice.
- The web app reloads on a 409 and polls `/api/changes` every 10 s, so the other person's saves show up.
- `tests/test_cover_all.py` runs one stale save per guarded screen against a real server, signed in as 2 people. `tests/test_two_people.py` checks the same rules at the function level. Add a case for any new guarded screen.

HTTP server rule: one `Handler` object serves many requests on a kept-alive connection, from either person. Never cache anything on `self` across requests (`tests/test_keepalive.py`). Database connections come from a per-thread pool released after each request (`backend/dbpool.py`); a thread you start that touches a database ends with `release_db()`.

## Security rules

- A URL a person or a marketplace gives us is fetched only with `netguard.fetch_public` (https, a public address checked on the connected socket before TLS, https-only redirects, no proxy). A new fetch of a user-given URL uses it too.
- `Handler.ip` takes the last `X-Forwarded-For` value, and only from a loopback peer: the hop your own proxy wrote. The sign-in lockout counts per (username, IP) and per IP. Keep the proxy on the same machine.
- Every POST needs the `X-Boxloft: 1` header (a cross-site form can't send it). Server-to-server callbacks that can't send it are listed in `WEBHOOKS`.
- Device tokens (`bl_...`, the Chrome extension) work only under `/api/inbound/`. Admin-only routes say `admin=True` in `@route`.
- `tests/test_auth.py` covers sign-in, lockout, expiry, roles and token scope over real HTTP. A new admin route or token scope gets a line there.

## Counting what's still coming

- Left to receive on a purchase line is `wms.LINE_LEFT_SQL` (bought, minus returned before receiving, minus received, minus units Amazon sold before they were scanned in). Use it anywhere you ask "how many are still coming". Never `qty - received`.
- `Commerce.bought_tally` is the one count of bought vs. in. A scan that would add extra stock past what was bought asks first.
- `Audit.unaccounted(inbound)` must stay 0: every purchase line with units still expected is in the store review queue, on Arriving or in the Stock audit. Any new place a purchase can sit has to be added there.

## Modules

- `wms.py`: products, boxes, locations, the ledger, receive/move/remove/count, undo, integrity.
- `commerce.py`: catalog lookup (Keepa, then UPCitemdb; every answer cached forever), purchases and their CSV imports, sales (`import_sales`, `add_sale`), `ship_line`, `auto_ship`, ship box choice (`recommend_box`).
- `sales_sync.py`: Amazon SP-API and eBay orders on a 15-minute loop, keyed on (channel, external id), so reruns add nothing. `auto_ship` scans out orders the marketplace shipped, from the first place on the pick list; when that default can't be right it leaves the order for a person with the reason.
- `inbound.py`, `pagesync.py`, `pages.py`: what the Chrome extension reads. `pagesync` plans each run, `pages` parses. Fix a page layout change in `pages.py`. The extension keeps its own safety rules in `sync.js` (URL allowlist, pace, page cap, stop on sign-in or captcha); never move them to the server.
- `store_mail.py`, `ship_mail.py`: store orders, cancellations and shipping emails from Gmail (IMAP, read-only). Code finds candidate values; the AI judge (`ai.py`) only picks among them. A tracking number counts only next to a tracking label or as a carrier URL parameter, and must pass its check digit.
- `labels.py`, `buyer_returns.py`: scan a carrier label and see what's inside; a buyer's return label opens its return.
- `returns.py`: returns to the seller, one per purchase line, every change a `return_event`.
- `listings.py`, `ebay_link.py`, `approval.py`: what is for sale on each market, eBay listings made into items, and whether Amazon lets you sell an ASIN (SP-API restrictions, cached per ASIN).
- `audit.py`, `sorting.py`: the Stock audit and scanning into boxes. Per ASIN: bought, minus sent back, minus sold on Amazon, is what should be here (`Audit._base`; what Amazon sold has one definition, the SQL view `line_presold`). Match sales and buys by ASIN, never by title words.
- `merge.py`: "Same item as..." is switched off (`Merge.enabled = False`, `MERGE_ON` in `Product.tsx`) until its review is clean. Undo of past merges still works.
- `printq.py`: phones send labels to a queue; the computer with the printer claims and prints them, so no job prints twice.
- `connections.py`: marketplace keys, stored in `DATA/secrets/connections.json` (600), never in the database. The API shows only the last 4 characters.

## Boxes and scan codes

- No bins. Boxes sit on shelves, and each box has its name written on it (A31). Names are unique among boxes in use, ignoring case and a dash. The letter is the market: A Amazon, E eBay, W Walmart, L local (`sorting.MARKETS`, `listings.MARKET_CHANNEL`). Moving an item to another letter's box changes its market.
- Open shelving is a named shelf (S1): a location in a building whose code is its name, items loose.
- `backend/codes.py`: SB = box, SL = location, SU = unit, then a 7-digit id and a Luhn check digit (`SB00000018` is box 1). Anything else is a product barcode (UPC, EAN, FNSKU, ASIN, SKU).
- The web app takes scans from a keyboard-wedge scanner (keys under 35 ms apart, ending in Enter), the phone camera (BarcodeDetector, ZXing fallback) and the header box. A page claims scans with `useScan`.
- Each box page line has its own Undo. A new kind of line or a new action on lines gets a row in `tests/test_box_line_matrix.py`.
- Refused saves (every 400 and 409 a signed-in person gets) go to `DATA/refused.jsonl` and show on the Overview. Read it when someone says a save failed.

## Scope

US only today: `US_MARKETPLACE` and the North America SP-API host, eBay site 0 (US), amazon.com pages, prices in cents of a dollar. Another market means touching `sales_sync.py`, `listings.py`, `approval.py`, `connections.py` and `pages.py`.

## Writing for the floor

People read this app on a phone with a box in one hand. Short sentences, numbers first, the name of the box. Errors say what to do next ("Only 1 of ... in A61, cannot take 3"). Times show in the warehouse's time zone (`config.TZ`, label `config.TZ_LABEL`); the database stores UTC.

## Before you call a change done

1. Run the whole test suite. Add a test that fails without your change.
2. For anything that changes counts, run `check_integrity()` on a copy of real data and compare the Stock audit before and after.
3. A change to a guarded screen: add its 409 case.
4. A change that adds state people act on in sequence (Undo, Change, a resent save, a count change, two people at once) ships with a Hypothesis state machine, `tests/test_<feature>_model.py`, built like `tests/test_photo_line_model.py`. It runs the real actions in random order and checks the rules after every step, 1,000 sequences by default (`BOXLOFT_MODEL_EXAMPLES`). A sequence bug goes into the model as an action or a rule first, then gets its fix.
