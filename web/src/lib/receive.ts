import { post } from '@/api/client';
import { navigate } from '@/lib/router';

type Toast = (message: string, opts?: { tone?: 'error' }) => void;

/** Open the receive form for a purchase line. If Boxloft has no product for the item yet, it is made
 *  from the order (name, Amazon code, cost, photo), so nobody fills in a form. */
export async function receiveLine(l: { id: number; product_id: number | null }, toast: Toast) {
  try {
    let pid = l.product_id;
    if (!pid) {
      const r = await post<{ product_id: number; made: boolean; title: string }>(`/api/purchases/line/${l.id}/product`);
      pid = r.product_id;
      if (r.made) return navigate(`/receive?product=${pid}&line=${l.id}&new=1`);   // the form says it was just added
    }
    navigate(`/receive?product=${pid}&line=${l.id}`);
  } catch (e) {
    toast(e instanceof Error ? e.message : 'Could not open that order line', { tone: 'error' });
  }
}
