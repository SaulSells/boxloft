import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { Printer, RotateCcw, Trash2 } from 'lucide-react';
import { post } from '@/api/client';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Empty, PageHeader, SectionTitle } from '@/components/app/common';
import { printJobs, usePrintSettings, useQueue } from '@/components/app/PrintQueue';
import { useToast } from '@/components/app/toast';
import { plural, when } from '@/lib/format';
import { printBySize, type PrintJob } from '@/lib/labels';

/** Alex 11-11: labels tapped on a phone wait here until the computer with the printer prints them. */
export function PrintQueuePage() {
  const s = usePrintSettings();
  const q = useQueue(true, s.auto);
  const qc = useQueryClient();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const waiting = q.data?.waiting ?? [];
  const done = q.data?.done ?? [];
  const refresh = () => qc.invalidateQueries({ queryKey: ['print-jobs'] });
  const print = async (ids: number[]) => {
    setBusy(true);
    try { const n = await printJobs(ids); toast(n ? `Printed ${plural(n, 'label')}` : 'Already printed on another device'); }
    catch (e) { toast((e as Error).message, { tone: 'error' }); }
    finally { setBusy(false); refresh(); }
  };
  const remove = async (j: PrintJob) => {
    try {
      await post(`/api/print-jobs/${j.id}/remove`);
      toast(`Removed: ${j.title}`, { undo: async () => { await post(`/api/print-jobs/${j.id}/back`); refresh(); } });
    } catch (e) { toast((e as Error).message, { tone: 'error' }); }
    refresh();
  };
  const who = (j: PrintJob) => `${j.user_name}${j.device ? ` (${j.device})` : ''}, ${when(j.created_at)}`;

  return (
    <div className="max-w-3xl space-y-4 sm:space-y-6">
      <PageHeader title="Print queue" subtitle="Labels tapped on a phone wait here. The computer with the label printer prints them." />

      <Card>
        <CardHeader className="pb-2"><CardTitle className="text-base">This device</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          <div className="flex items-start gap-3">
            <Switch id="prints" checked={s.prints} onCheckedChange={s.setPrints} />
            <div className="space-y-0.5"><Label htmlFor="prints" className="leading-snug">The label printer is connected to this device</Label>
              <p className="text-xs text-muted-foreground">{s.prints ? 'Print buttons print here, and labels sent from phones show in a bar at the top.' : 'Print buttons send labels to this queue.'}</p></div>
          </div>
          {s.prints && (
            <div className="flex items-start gap-3">
              <Switch id="auto" checked={s.auto} onCheckedChange={s.setAuto} />
              <div className="space-y-0.5"><Label htmlFor="auto" className="leading-snug">Print new labels as soon as they arrive</Label>
                <p className="text-xs text-muted-foreground">Keep a Boxloft tab open. If Chrome was started with --kiosk-printing, labels print with no dialog.</p></div>
            </div>
          )}
        </CardContent>
      </Card>

      <div className="space-y-2">
        <SectionTitle count={q.data?.labels_waiting} hint={waiting.length ? undefined : 'Nothing waiting'}>Waiting to print</SectionTitle>
        {waiting.length > 0 && s.prints && (
          <Button disabled={busy} onClick={() => void print(waiting.map((j) => j.id))}><Printer className="mr-1.5 h-4 w-4" />Print all {plural(q.data?.labels_waiting ?? 0, 'label')}</Button>
        )}
        {waiting.length === 0 && <Empty>Nothing waiting. Labels tapped on a phone show up here within a few seconds.</Empty>}
        {waiting.map((j) => (
          <div key={j.id} className="flex flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-sm">
            <div className="min-w-0 flex-1"><div className="font-medium">{j.title}</div><div className="text-xs text-muted-foreground">{who(j)}</div></div>
            {s.prints && <Button size="sm" variant="outline" disabled={busy} onClick={() => void print([j.id])}><Printer className="mr-1 h-3.5 w-3.5" />Print</Button>}
            <Button size="sm" variant="ghost" onClick={() => void remove(j)}><Trash2 className="mr-1 h-3.5 w-3.5" />Remove</Button>
          </div>
        ))}
      </div>

      {done.length > 0 && (
        <div className="space-y-2">
          <SectionTitle>Printed or removed</SectionTitle>
          {done.map((j) => (
            <div key={j.id} className="flex flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-sm text-muted-foreground">
              <div className="min-w-0 flex-1">
                <div className="font-medium text-foreground">{j.title}</div>
                <div className="text-xs">{who(j)} · {j.removed_at ? `removed ${when(j.removed_at)}` : `printed ${when(j.printed_at)}${j.printed_by_name ? ` by ${j.printed_by_name}` : ''}`}</div>
              </div>
              {s.prints && !j.removed_at && <Button size="sm" variant="ghost" onClick={() => void printBySize(j.labels)}><RotateCcw className="mr-1 h-3.5 w-3.5" />Reprint</Button>}
              {j.removed_at && !j.printed_at && <Button size="sm" variant="ghost" onClick={async () => { await post(`/api/print-jobs/${j.id}/back`); refresh(); }}>Put back</Button>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
