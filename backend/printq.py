"""Label print queue (Alex 11-11): phones have no label printer. A phone's Print sends its labels here; the computer
with the printer shows "N labels waiting" and prints them. The labels are the same objects the web app prints
(code, big, lines, kind), so the computer draws them at its own label sizes.

A job is claimed before it prints (claim sets printed_at under the write lock), so two computers, or Alex and Sam
clicking at once, never print the same job twice. Reprint is a local print of a job already printed."""
import json

from wms import WmsError, now_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS print_job (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    device TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    labels TEXT NOT NULL,                  -- JSON list of {code, big, lines, kind}
    n INTEGER NOT NULL,
    printed_at TEXT,
    printed_by INTEGER,
    printed_device TEXT,
    removed_at TEXT,
    removed_by INTEGER
);
CREATE INDEX IF NOT EXISTS print_job_waiting ON print_job (printed_at, removed_at);
"""
KINDS = ('box', 'location', 'unit', 'fnsku')
MAX_LABELS = 500   # a whole bin range (299 bin labels) fits in one job


def _clean(labels) -> list:
    if not isinstance(labels, list) or not labels:
        raise WmsError('No labels to print')
    if len(labels) > MAX_LABELS:
        raise WmsError(f'At most {MAX_LABELS} labels in one print')
    out = []
    for lb in labels:
        if not isinstance(lb, dict) or lb.get('kind') not in KINDS or not str(lb.get('code') or '').strip():
            raise WmsError('A label is missing its code or kind')
        lines = lb.get('lines') if isinstance(lb.get('lines'), list) else []
        out.append({'kind': lb['kind'], 'code': str(lb['code']).strip()[:64], 'big': str(lb.get('big') or '')[:80],
                    'lines': [str(x)[:160] for x in lines[:4]]})
    return out


def _title(labels: list) -> str:
    first = labels[0]
    if len(labels) == 1:
        return first['big'] if first['kind'] != 'unit' else f"Unit {first['big']}"
    kinds = {lb['kind'] for lb in labels}
    word = {'box': 'box labels', 'location': 'bin labels', 'unit': 'unit labels', 'fnsku': 'FNSKU labels'}
    return f"{len(labels)} {word[first['kind']] if len(kinds) == 1 else 'labels'}"


class PrintQueue:
    def __init__(self, wms):
        self.wms = wms
        wms._conn().executescript(SCHEMA)

    def add(self, user_id: int, labels, device: str = '', title: str = '') -> dict:
        labels = _clean(labels)
        with self.wms._tx() as c:
            c.execute("""INSERT INTO print_job (created_at, user_id, device, title, labels, n) VALUES (?,?,?,?,?,?)""",
                      (now_utc(), user_id, (device or '')[:60], (title or _title(labels))[:120], json.dumps(labels), len(labels)))
            jid = c.execute('SELECT last_insert_rowid()').fetchone()[0]
        return self.get(jid)

    def get(self, jid: int) -> dict:
        j = self.wms._q1('SELECT * FROM print_job WHERE id = ?', (jid,))
        if not j:
            raise WmsError('No such print job')
        j['labels'] = json.loads(j['labels'])
        return j

    def list(self, recent: int = 30) -> dict:
        """Waiting jobs oldest first (print order), then the newest printed or removed ones (for Reprint)."""
        waiting = self.wms._q('SELECT * FROM print_job WHERE printed_at IS NULL AND removed_at IS NULL ORDER BY id')
        done = self.wms._q("""SELECT * FROM print_job WHERE printed_at IS NOT NULL OR removed_at IS NOT NULL
                              ORDER BY COALESCE(printed_at, removed_at) DESC, id DESC LIMIT ?""", (recent,))
        for j in waiting + done:
            j['labels'] = json.loads(j['labels'])
        return {'waiting': waiting, 'done': done, 'labels_waiting': sum(j['n'] for j in waiting)}

    def claim(self, ids, user_id: int, device: str = '') -> list:
        """Marks waiting jobs printed and returns the ones this call got, oldest first. A job someone else
        already printed or removed is left out, so it never prints twice."""
        ids = [int(i) for i in ids or [] if str(i).isdigit()]
        if not ids:
            return []
        got = []
        with self.wms._tx() as c:
            for jid in sorted(set(ids)):
                cur = c.execute("""UPDATE print_job SET printed_at = ?, printed_by = ?, printed_device = ?
                                   WHERE id = ? AND printed_at IS NULL AND removed_at IS NULL""",
                                (now_utc(), user_id, (device or '')[:60], jid))
                if cur.rowcount:
                    got.append(jid)
        return [self.get(j) for j in got]

    def remove(self, jid: int, user_id: int) -> dict:
        with self.wms._tx() as c:
            cur = c.execute('UPDATE print_job SET removed_at = ?, removed_by = ? WHERE id = ? AND printed_at IS NULL AND removed_at IS NULL',
                            (now_utc(), user_id, jid))
            if not cur.rowcount:
                raise WmsError('That print already went out or was removed')
        return self.get(jid)

    def back(self, jid: int) -> dict:
        """Undo a remove: the job waits again."""
        with self.wms._tx() as c:
            c.execute('UPDATE print_job SET removed_at = NULL, removed_by = NULL WHERE id = ? AND printed_at IS NULL', (jid,))
        return self.get(jid)
