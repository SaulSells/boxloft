import { createContext, useCallback, useContext, useState, type ReactNode } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { CheckCircle2, AlertTriangle, Undo2 } from 'lucide-react';
import { Button } from '@/components/ui/button';

interface Toast {
  id: number;
  text: string;
  tone: 'ok' | 'error';
  undo?: () => Promise<void> | void;
}

const Ctx = createContext<{ toast: (text: string, opts?: { tone?: 'ok' | 'error'; undo?: Toast['undo'] }) => void } | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const dismiss = (id: number) => setToasts((t) => t.filter((x) => x.id !== id));
  const toast = useCallback((text: string, opts?: { tone?: 'ok' | 'error'; undo?: Toast['undo'] }) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t.slice(-2), { id, text, tone: opts?.tone ?? 'ok', undo: opts?.undo }]);
    setTimeout(() => dismiss(id), opts?.undo ? 10000 : opts?.tone === 'error' ? 7000 : 3500);
  }, []);
  return (
    <Ctx.Provider value={{ toast }}>
      {children}
      <div className="toast-stack pointer-events-none fixed inset-x-0 bottom-4 z-[60] flex flex-col items-center gap-2 px-4">
        <AnimatePresence>
          {toasts.map((t) => (
            <motion.div
              key={t.id}
              initial={{ opacity: 0, y: 16 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: 16 }}
              className={`pointer-events-auto flex w-full max-w-md items-center gap-3 rounded-lg border px-4 py-3 text-sm shadow-lg ${
                t.tone === 'error' ? 'border-destructive/40 bg-red-50 text-red-900 dark:bg-red-950 dark:text-red-100' : 'bg-card'
              }`}
            >
              {t.tone === 'error' ? <AlertTriangle className="h-4 w-4 shrink-0" /> : <CheckCircle2 className="h-4 w-4 shrink-0 text-primary" />}
              <span className="flex-1">{t.text}</span>
              {t.undo && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={async () => {
                    dismiss(t.id);
                    await t.undo!();
                  }}
                >
                  <Undo2 className="mr-1 h-3.5 w-3.5" /> Undo
                </Button>
              )}
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </Ctx.Provider>
  );
}

export function useToast() {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('useToast outside ToastProvider');
  return ctx.toast;
}
