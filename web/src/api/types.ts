export interface User {
  id: number;
  username: string;
  email: string | null;
  display_name: string;
  role: 'admin' | 'staff';
  is_admin: boolean;
}

export interface Owner { id: number; name: string; is_client: number }
export interface Identifier { kind: string; value: string; id?: number }

export interface ProductRow {
  /** Marketplaces it is for sale on now ('amazon', 'ebay', 'walmart'); one item can be on several (Alex 11-12). */
  markets?: string[];
  id: number;
  sku: string;
  title: string;
  tracking: 'qty' | 'unit';
  condition: string;
  owner_id: number;
  owner: string;
  archived: number;
  on_hand: number;
  boxes: number;
  photo_id: number | null;
  identifiers: Identifier[];
  avg_cost_cents: number | null;
  value_cents: number;
  updated_at: string;
  edited_at: string;   // changes only when someone edits the product (stock moves change updated_at)
}

export interface WhereRow {
  box_id: number;
  location_id: number;
  unit_id: number;
  qty: number;
  location_code: string;
  location_site?: string | null;
  location_name?: string | null;
  box_name: string | null;
  box_code: string | null;
  unit_code: string | null;
  unit_condition: string | null;
  unit_cost_cents: number | null;
}

export interface ActionRow {
  id: number;
  at: string;
  user_id: number;
  user_name?: string;
  kind: string;
  summary: string;
  note: string;
  reversed_by: number | null;
  reverses_action_id: number | null;
}

export interface Product extends Omit<ProductRow, 'photo_id' | 'boxes' | 'value_cents'> {
  notes: string;
  brand: string;
  image_url: string | null;
  pkg_l_mm: number | null;
  pkg_w_mm: number | null;
  pkg_h_mm: number | null;
  pkg_weight_g: number | null;
  default_cost_cents: number | null;
  photos: { id: number; unit_id: number | null }[];
  /** The SKU to paste into Seller Central: newest buy's date and price (Alex 11-24). null with no dated, priced buy. */
  sell_sku?: { product_id: number; sku: string; saved: boolean; day: string; cost_cents: number | null; from: string; estimated?: boolean } | null;
  where: WhereRow[];
  receipts: { id: number; qty: number; unit_cost_cents: number | null; supplier: string; reference: string; at: string; purchased_at: string | null; reversed_by: number | null; user_name?: string }[];
  history: ActionRow[];
}

export interface ContentRow {
  product_id: number;
  unit_id: number;
  qty: number;
  sku: string;
  title: string;
  tracking: 'qty' | 'unit';
  condition: string;
  owner: string;
  unit_condition: string | null;
  unit_cost_cents: number | null;
  unit_code: string | null;
  photo_id: number | null;
}

export interface BoxRow {
  id: number;
  name: string;
  kind: string;
  notes: string;
  archived: number;
  created_at: string;
  location_id: number;
  location_code: string;
  location_site?: string;
  location_name?: string;
  units: number;
  skus: number;
  last_activity: string | null;
  last_counted: string | null;
  scan_code: string;
}

export interface Box extends BoxRow {
  contents: ContentRow[];
  history: ActionRow[];
  /** Newest first. A label printed before a rename still shows the old name. */
  renames?: { old_name: string; new_name: string; at: string; user_name?: string }[];
}

export interface LocationRow {
  id: number;
  code: string;
  name: string;
  site?: string;
  kind: 'shelf' | 'dock' | 'virtual';
  archived: number;
  boxes: number;
  units: number;
  scan_code: string;
}

export interface Location extends Omit<LocationRow, 'boxes' | 'units'> {
  boxes: BoxRow[];
  loose: ContentRow[];
}

export interface Unit {
  id: number;
  product_id: number;
  sku: string;
  title: string;
  condition: string;
  notes: string;
  cost_cents: number | null;
  scan_code: string;
  created_at: string;
  photos: { id: number }[];
  holder: { box_id: number; location_id: number; location_code: string; location_kind: string } | null;
}

export type Holder = { box_id: number } | { location_id: number };

export type ScanResult =
  | { kind: 'box'; box: BoxRow }
  | { kind: 'location'; location: { id: number; code: string; name: string; kind: string; scan_code: string } }
  | { kind: 'unit'; unit: Unit }
  | { kind: 'product'; value: string; products: ProductRow[] }
  /** A carrier label (Alex 11-16): the Package page, or the box being filled, shows what is inside. */
  | { kind: 'label'; value: string; message: string }
  | { kind: 'bad_check' | 'unknown'; code?: string; value?: string; message: string;
      /** unknown only: open order lines for this code (oldest first), so the scan can go straight to receiving */
      lines?: { id: number; product_id: number | null; title: string; order_id: string; left_to_receive: number }[] };

export interface Integrity {
  at: string;
  ok: boolean;
  moves_checked: number;
  problem_count: number;
  problems: Record<string, unknown>[];
}

export interface Summary {
  skus_in_stock: number;
  units: number;
  value_cents: number;
  boxes: number;
  empty_boxes: number;
  boxes_never_counted: number;
  locations: number;
  by_owner: { owner: string; skus: number; units: number; value_cents: number }[];
  needs_attention: { missing_cost: number; no_photo: number; loose_on_dock: number };
  integrity: Integrity | null;
  unaccounted: { lines: number; units: number; cost_cents: number;
    items: { id: number; source: string; order_id: string; ordered_at: string | null; state: string; title: string; units: number }[] };
  /** eBay (Alex 11-12): both should be 0. null until eBay is connected. */
  ebay: { unmatched: number; shipped_open: number } | null;
  /** Alex 11-16: saves the server refused today (ET). 409s are someone else saving first, by design. */
  refused?: { count: number; conflicts: number;
    recent: { at: string; user: string; method: string; path: string; status: number; message: string }[] };
  recent: ActionRow[];
  open_orders: number;
  purchases: { lines: number; bought: number; spend: number; received: number; stale_units: number };
  low_ship_boxes: ShipBox[];
  /** The signed-in person's last 5 saves (staff Home). */
  mine?: ActionRow[];
  arriving: { today: number; open: number; delivered_not_in: number; delivered_packages: number; counts: Record<string, { packages: number; orders: number }>; last_sync: string | null };
}

export interface CatalogItem {
  asin: string | null;
  title: string;
  brand: string;
  model: string;
  image_url: string | null;
  upcs: string[];
  eans: string[];
  pkg_l_mm: number | null;
  pkg_w_mm: number | null;
  pkg_h_mm: number | null;
  pkg_weight_g: number | null;
  source?: string;
}

/** New product, photo -> product (Alex 11-11): what Gemini read and the buys it may be. */
export interface PhotoMatch {
  kind: 'product' | 'audit' | 'line';
  id: number;
  product_id: number | null;
  title: string;
  asin: string;
  photo_id: number | null;
  image_url: string | null;
  /** Audit rows: units bought. Order lines: units still coming. */
  left: number | null;
  where?: string;
  score: number;
  by?: 'barcode';
}

export interface PhotoRead {
  photo: string;
  read: { is_product: boolean; title: string; brand: string; model: string; barcode_digits: string; confidence: string; seconds: number } | null;
  matches: PhotoMatch[];
  catalog: CatalogItem | null;
  error: string | null;
}

export interface PurchaseLine {
  id: number;
  asin: string;
  upc: string;
  title: string;
  qty: number;
  received: number;
  returned?: number;
  left_to_receive?: number;
  /** Receive only (commerce.rank_for_receiving): units of this kind still to scan in, and why this order is first. */
  package_left?: number;
  delivered_on?: string | null;
  why?: string;
  return_state?: string | null;
  unit_cost_cents: number;
  product_id: number | null;
  source: string;
  order_id: string;
  account: string;
  ordered_at: string | null;
}

export interface ShipBox {
  id: number;
  coming?: number;
  name: string;
  inner_l_in: number;
  inner_w_in: number;
  inner_h_in: number;
  stock: number;
  cost_cents: number | null;
  archived: number;
}

export interface PickWhere {
  box_id: number;
  location_id: number;
  unit_id: number;
  qty: number;
  box_name: string | null;
  location_code: string;
  unit_condition: string | null;
  box_code: string | null;
  unit_code: string | null;
}

export interface SalesLine {
  id: number;
  /** Shipped with none on the shelves (never scanned in): its Undo id. */
  unscanned_id?: number | null;
  sku: string;
  asin: string;
  title: string;
  qty: number;
  product_id: number | null;
  product_title: string | null;
  product_sku: string | null;
  tracking: 'qty' | 'unit' | null;
  photo_id: number | null;
  /** The product's Amazon photo URL: shown until the saved copy (photo_id) is in. */
  image_url?: string | null;
  shipped_action_id: number | null;
  ship_box_name: string | null;
  where: PickWhere[];
  /** Open orders, lines with nothing to pick: this item's open stock-audit row (id = audit_item, for Found it), and units bought but not received. */
  audit?: { id: number; asin: string; left: number; expected: number; accounted: number } | null;
  coming?: number;
}

export interface SalesOrder {
  id: number;
  channel: string;
  external_id: string;
  ordered_at: string | null;
  ship_by: string | null;
  status: string;
  note: string;
  fulfilled_by?: string;
  market_status?: string;
  review?: string;
  lines: SalesLine[];
  recommendation: { box: ShipBox | null; reason: string; item_in?: number[] } | null;
}
