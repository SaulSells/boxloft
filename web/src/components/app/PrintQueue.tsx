import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Printer } from 'lucide-react';
import { get, post } from '@/api/client';
import { Button } from '@/components/ui/button';
import { useToast } from '@/components/app/toast';
import { Link, useLocation } from '@/lib/router';
import { plural } from '@/lib/format';
import { printBySize, printsHere, setPrintsHere, type PrintJob } from '@/lib/labels';

/*
 * Label print queue (Alex 11-11). Phones send labels to the server (lib/labels printLabels). The computer with the
 * label printer shows a bar with what is waiting and prints it in one click. Auto-print prints new jobs as they
 * arrive: with Chrome started with --kiosk-printing it prints with no dialog at all.
 */

export interface Queue { waiting: PrintJob[]; done: PrintJob[]; labels_waiting: number }

const AUTO_KEY = 'boxloft-autoprint';
const read = (k: string) => { try { return localStorage.getItem(k); } catch { return null; } };

/** This device's print settings, shared by the bar and the queue page (an event keeps them in step). */
export function usePrintSettings() {
  const [prints, setP] = useState(printsHere);
  const [auto, setA] = useState(() => read(AUTO_KEY) === 'yes');
  useEffect(() => {
    const sync = () => { setP(printsHere()); setA(read(AUTO_KEY) === 'yes'); };
    window.addEventListener('boxloft:print-settings', sync);
    return () => window.removeEventListener('boxloft:print-settings', sync);
  }, []);
  const changed = () => window.dispatchEvent(new Event('boxloft:print-settings'));
  return {
    prints, auto,
    setPrints: (v: boolean) => { setPrintsHere(v); changed(); },
    setAuto: (v: boolean) => { try { localStorage.setItem(AUTO_KEY, v ? 'yes' : 'no'); } catch { /* ignore */ } changed(); },
  };
}

export function useQueue(enabled: boolean, background = false) {
  return useQuery({
    queryKey: ['print-jobs'], queryFn: () => get<Queue>('/api/print-jobs'), enabled,
    refetchInterval: enabled ? 5000 : false, refetchIntervalInBackground: background,
  });
}

let printing = false;   // one print run at a time on this device (auto-print and a click can't overlap)

/** Claim the jobs (so nobody else prints them), then print them here, one print per label size. */
export async function printJobs(ids: number[]): Promise<number> {
  if (printing || !ids.length) return 0;
  printing = true;
  try {
    const r = await post<{ jobs: PrintJob[] }>('/api/print-jobs/claim', { ids });
    const labels = r.jobs.flatMap((j) => j.labels);
    if (labels.length) await printBySize(labels);
    return labels.length;
  } finally {
    printing = false;
  }
}

/** Toasts for a phone that sent labels to the queue, with Undo (takes the job back off). */
export function QueueToaster() {
  const toast = useToast();
  const qc = useQueryClient();
  useEffect(() => {
    const queued = (e: Event) => {
      const job = (e as CustomEvent<PrintJob>).detail;
      qc.invalidateQueries({ queryKey: ['print-jobs'] });
      toast(`Sent to the print queue: ${job.title}. Print it from the computer.`, {
        undo: async () => { await post(`/api/print-jobs/${job.id}/remove`); qc.invalidateQueries({ queryKey: ['print-jobs'] }); },
      });
    };
    const failed = (e: Event) => toast(`Could not send to the print queue: ${(e as CustomEvent<string>).detail}`, { tone: 'error' });
    window.addEventListener('boxloft:queued', queued);
    window.addEventListener('boxloft:queue-failed', failed);
    return () => { window.removeEventListener('boxloft:queued', queued); window.removeEventListener('boxloft:queue-failed', failed); };
  }, [toast, qc]);
  return null;
}

/** On the computer with the printer: "5 labels waiting" under the header, Print all, and auto-print. */
export function PrintQueueBar() {
  const { prints, auto } = usePrintSettings();
  const { path } = useLocation();
  const q = useQueue(prints, auto);
  const qc = useQueryClient();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const waiting = q.data?.waiting ?? [];
  const run = async (ids: number[]) => {
    setBusy(true);
    try {
      const n = await printJobs(ids);
      if (n) toast(`Printed ${plural(n, 'label')}`);
    } catch (e) {
      toast((e as Error).message, { tone: 'error' });
    } finally {
      setBusy(false);
      qc.invalidateQueries({ queryKey: ['print-jobs'] });
    }
  };
  const seen = useRef('');
  useEffect(() => {
    const key = waiting.map((j) => j.id).join(',');
    if (!auto || !prints || !key || key === seen.current || busy) return;
    seen.current = key;
    void run(waiting.map((j) => j.id));
  }, [auto, prints, waiting, busy]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!prints || !waiting.length || path === '/print-queue') return null;
  const people = [...new Set(waiting.map((j) => j.user_name))].join(' and ');
  return (
    <div className="sticky top-14 z-20 flex flex-wrap items-center gap-x-3 gap-y-2 border-b bg-amber-50 px-3 py-2 text-sm dark:bg-amber-950/40 sm:px-4">
      <Printer className="h-4 w-4 shrink-0" />
      <span className="min-w-0 flex-1">
        <b>{plural(q.data?.labels_waiting ?? 0, 'label')} waiting to print</b> from {people}:{' '}
        <span className="text-muted-foreground">{waiting.slice(0, 3).map((j) => j.title).join(', ')}{waiting.length > 3 ? `, +${waiting.length - 3} more` : ''}</span>
      </span>
      <Link to="/print-queue" className="text-sm underline">See queue</Link>
      <Button size="sm" disabled={busy} onClick={() => void run(waiting.map((j) => j.id))}><Printer className="mr-1.5 h-4 w-4" />{busy ? 'Printing...' : 'Print all'}</Button>
    </div>
  );
}
