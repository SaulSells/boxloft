import { useEffect, useRef, useState } from 'react';
import { Box as BoxIcon } from 'lucide-react';
import { post } from '@/api/client';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { PageHeader } from '@/components/app/common';
import { useToast } from '@/components/app/toast';
import { PackageCard, isLabel, type LabelAnswer } from '@/components/app/PackageCard';
import { navigate, useQueryParam } from '@/lib/router';
import { useScan, useScanBus } from '@/lib/scan';
import { parseCode } from '@/lib/codes';
import { curPath, useCurrentBox } from '@/lib/currentBox';

/* A carrier label scanned with no box open (Alex 11-16): what the box holds. The scan is logged as "in hand".
 * A box label scanned here opens that box with this card, ready for "Put 2 in A31". Item scans go on to the shell. */
export function LabelPage() {
  const code = useQueryParam('code') || '';
  const cur = useCurrentBox();
  const toast = useToast();
  const { feedback } = useScanBus();
  const [r, setR] = useState<LabelAnswer | null>(null);
  const done = useRef('');
  useEffect(() => {
    if (!code || done.current === code) return;
    done.current = code;
    setR(null);
    post<LabelAnswer | { kind: 'not_label' }>('/api/labels/scan', { code })
      .then((x) => { if (isLabel(x)) setR(x); else navigate(`/put?code=${encodeURIComponent(code)}`, true); })
      .catch((e) => toast(e instanceof Error ? e.message : 'Could not read the label', { tone: 'error' }));
  }, [code]); // eslint-disable-line react-hooks/exhaustive-deps
  const tracking = r?.kind === 'label' || r?.kind === 'label_return' ? r.tracking_id : code;
  const toPage = (path: string) => navigate(`${path}?label=${encodeURIComponent(tracking)}`);
  useScan((c) => {
    const p = parseCode(c);
    if (p.kind === 'box' || p.kind === 'location') { feedback(true); toPage(`/${p.kind === 'box' ? 'boxes' : 'locations'}/${p.id}`); return; }
    navigate(`/label?code=${encodeURIComponent(c)}`, true);   // another label (or an item: the server sends it on)
  });
  const damaged = async (returnId: number, left: number) => {   // a buyer return that can't be sold again
    try { setR(await post<LabelAnswer>(`/api/buyer-returns/${returnId}/take`, { qty: 1, condition: 'damaged', seen_left: left })); } catch (e) { toast(e instanceof Error ? e.message : 'Could not save it', { tone: 'error' }); }
  };
  const pick = async (t: string) => {
    try { setR(await post<LabelAnswer>('/api/labels/pick', { code, tracking_id: t })); } catch (e) { toast(e instanceof Error ? e.message : 'Could not pick it', { tone: 'error' }); }
  };
  return (
    <div className="space-y-4">
      <PageHeader title="What is in this box" subtitle="From its carrier label" />
      <Card>
        <CardContent className="space-y-3 p-4">
          {r ? <PackageCard r={r} onPick={(t) => void pick(t)} onReturn={(i) => void damaged(i.return_id, i.left)} /> : <div className="text-sm text-muted-foreground">Reading the label…</div>}
          {r?.kind === 'label_return' && r.left > 0 && (
            <div className="space-y-2">
              <div className="text-sm">Can it be sold again? Scan the box label it goes in.</div>
              {r.items.filter((i) => i.left > 0 && i.shipped_from?.box_id).slice(0, 1).map((i) => (
                <Button key={i.return_id} className="h-12 w-full text-base" onClick={() => toPage(`/boxes/${i.shipped_from!.box_id!}`)}><BoxIcon className="mr-1.5 h-4 w-4" />Back in {i.shipped_from!.name}, where it shipped from</Button>
              ))}
            </div>
          )}
          {r?.kind === 'label' && r.left > 0 && (
            <div className="space-y-2">
              <div className="text-sm">Scan the box label you are putting these in.</div>
              {cur && <Button className="h-12 w-full text-base" onClick={() => toPage(curPath(cur))}><BoxIcon className="mr-1.5 h-4 w-4" />Put them in {cur.name}</Button>}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
