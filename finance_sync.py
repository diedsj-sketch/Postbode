"""Read-only Drive bridge. Source evidence never overwrites reviewed obligations."""
import datetime as dt
import hashlib
import html
import json
import os
import re
import time
import urllib.error

DEBT_SHEETS = ('Personal Debts', 'Company Debts', 'Unclear Liability')
SHEETS = DEBT_SHEETS + ('Personal Repayments', 'Company Repayments',
    'Personal Payment History', 'Company Payment History', 'Disputes & Reconciliation')


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS finance_gmail_cases (
        id TEXT PRIMARY KEY, sheet TEXT NOT NULL, fields TEXT NOT NULL,
        fingerprint TEXT NOT NULL, updated_at TEXT NOT NULL,
        obligation_id TEXT, reviewed_fingerprint TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS finance_gmail_sync (
        id INTEGER PRIMARY KEY CHECK(id=1), checked_at TEXT, imported_at TEXT,
        source_at TEXT, error TEXT, payload TEXT)''')


def validate(payload):
    if not isinstance(payload, dict) or payload.get('schema_version') != 1:
        raise ValueError('Unsupported sync format')
    stamp = dt.datetime.fromisoformat(payload['generated_at'])
    if stamp.tzinfo is None or stamp > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5):
        raise ValueError('Invalid source timestamp')
    sheets = payload.get('sheets')
    if not isinstance(sheets, dict) or set(sheets) != set(SHEETS):
        raise ValueError('Missing source sheets')
    seen = set()
    for name, rows in sheets.items():
        if not isinstance(rows, list) or len(rows) > 2000:
            raise ValueError('Invalid source rows')
        for row in rows:
            if not isinstance(row, dict) or len(row) > 60:
                raise ValueError('Invalid source record')
            if any(not isinstance(k, str) or len(k)>200 or not isinstance(v, (str, int, float, type(None)))
                   or len(str(v)) > 20000 for k,v in row.items()):
                raise ValueError('Invalid source fields')
            if name in DEBT_SHEETS:
                ident = row.get('Debt ID')
                if not isinstance(ident, str) or not re.fullmatch(r'[\w .:/-]{1,160}', ident) or ident in seen:
                    raise ValueError('Missing or duplicate Debt ID')
                seen.add(ident)
    return stamp


def ingest(c, payload, stamp):
    source_time = validate(payload)
    previous = c.execute('SELECT source_at FROM finance_gmail_sync WHERE id=1').fetchone()
    if previous and previous['source_at'] and source_time < dt.datetime.fromisoformat(previous['source_at']):
        raise ValueError('Refusing older snapshot')
    count = 0
    for sheet in DEBT_SHEETS:
        for fields in payload['sheets'][sheet]:
            raw = json.dumps(fields, sort_keys=True, ensure_ascii=False, allow_nan=False)
            digest = hashlib.sha256((sheet + raw).encode()).hexdigest()
            c.execute('''INSERT INTO finance_gmail_cases(id,sheet,fields,fingerprint,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                sheet=excluded.sheet, fields=excluded.fields, fingerprint=excluded.fingerprint,
                updated_at=CASE WHEN fingerprint!=excluded.fingerprint THEN excluded.updated_at ELSE updated_at END''',
                (fields['Debt ID'], sheet, raw, digest, stamp))
            count += 1
    # Omitted cases remain visible, never silently removed or marked paid.
    c.execute('''INSERT INTO finance_gmail_sync(id,checked_at,imported_at,source_at,error,payload)
        VALUES(1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET checked_at=excluded.checked_at,
        imported_at=excluded.imported_at,source_at=excluded.source_at,error=NULL,payload=excluded.payload''',
        (stamp,stamp,payload['generated_at'],None,json.dumps(payload,allow_nan=False)))
    return count


_next_check = 0


def poll():
    global _next_check
    file_id = os.environ.get('GMAIL_FINANCE_SYNC_FILE_ID', '')
    if not file_id or time.monotonic() < _next_check:
        return
    _next_check = time.monotonic() + 300
    from mailroom import Google, db, now
    stamp = now()
    try:
        if not re.fullmatch(r'[A-Za-z0-9_-]{10,200}', file_id):
            raise ValueError('Invalid file ID')
        google = Google()
        meta = google.call('https://www.googleapis.com/drive/v3/files/'+file_id+'?fields=size,mimeType,shared')
        if meta.get('shared') or meta.get('mimeType') != 'application/json' or int(meta.get('size', 0)) > 2000000:
            raise ValueError('Sync file must be private JSON under 2 MB')
        payload = google.call('https://www.googleapis.com/drive/v3/files/'+file_id+'?alt=media')
        with db() as c:
            count = ingest(c, payload, stamp)
        print('Gmail finance sync successful: %d cases' % count, flush=True)
    except Exception as exc:
        error = type(exc).__name__ + (f' HTTP {exc.code}' if isinstance(exc, urllib.error.HTTPError) else '')
        with db() as c:
            c.execute('''INSERT INTO finance_gmail_sync(id,checked_at,error) VALUES(1,?,?)
                ON CONFLICT(id) DO UPDATE SET checked_at=excluded.checked_at,error=excluded.error''', (stamp,error))
        print('Gmail finance sync failed: '+error, flush=True)


def source_fields(fields):
    esc = lambda v: html.escape(str(v), quote=True)
    parts = []
    for key, value in fields.items():
        if value is None or value == '':
            continue
        rendered = esc(value)
        if isinstance(value, str) and re.fullmatch(r'https://mail\.google\.com/[^\s<>"\x00-\x1f]+', value):
            rendered = f'<a href="{esc(value)}" target="_blank" rel="noopener noreferrer">Open Gmail evidence</a>'
        parts.append(f'<dt>{esc(key)}</dt><dd style="overflow-wrap:anywhere">{rendered}</dd>')
    return '<dl>'+''.join(parts)+'</dl>'


def panel(token_field, ledger_options):
    from mailroom import db
    from dashboard import esc
    with db() as c:
        status = c.execute('SELECT * FROM finance_gmail_sync WHERE id=1').fetchone()
        rows = c.execute('SELECT * FROM finance_gmail_cases ORDER BY sheet,id').fetchall()
        obligations = c.execute('SELECT id,creditor,description,ledger_id FROM finance_obligations ORDER BY creditor').fetchall()
    summary = 'Waiting for the first successful import.'
    context = ''
    if status:
        summary = f"Source snapshot: {status['source_at'] or 'none'}. Last successful import: {status['imported_at'] or 'never'}."
        if status['error']:
            summary += ' Latest sync failed: '+status['error']+'. Previous records retained.'
        if status['source_at'] and dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(status['source_at']) > dt.timedelta(hours=36):
            summary += ' SOURCE IS STALE: no new snapshot for over 36 hours.'
        if status['payload']:
            sheets = json.loads(status['payload'])['sheets']
            for name in SHEETS[3:]:
                context += f'<details><summary>{esc(name)} ({len(sheets[name])})</summary>'+''.join(source_fields(r) for r in sheets[name])+'</details>'
    cards = []
    for row in rows:
        f = json.loads(row['fields'])
        changed = row['reviewed_fingerprint'] != row['fingerprint']
        linked = row['obligation_id']
        options = '<option value="">Choose existing debt</option>'+''.join(
            f'<option value="{esc(o["id"])}">{esc(o["ledger_id"])}: {esc(o["creditor"])} / {esc(o["description"])}</option>' for o in obligations)
        cards.append(f'''<details class="panel"><summary>{esc(row['sheet'])}: {esc(f.get('Original creditor',''))}
            / {esc(f.get('Case / contract',row['id']))} {'(needs review)' if changed else '(reviewed)'}</summary>
            <p>Source case: {esc(row['id'])}. Linked dashboard debt: {esc(linked or 'none')}. Source values are claims, not confirmed totals.</p>
            {source_fields(f)}
            <form method="post" action="/dashboard/action">{token_field}
            <input type="hidden" name="action" value="gmail-case"><input type="hidden" name="id" value="{esc(row['id'])}">
            <input type="hidden" name="fingerprint" value="{esc(row['fingerprint'])}">
            <label>Link to the same existing debt (adds no balance)<select name="obligation_id">{options}</select></label>
            <button name="operation" value="link">Link existing debt</button>
            <button name="operation" value="acknowledge" class="quiet">Mark source reviewed</button>
            {'' if linked else f'<label>Ledger for a new candidate<select name="ledger_id">{ledger_options}</select></label><button name="operation" value="create">Create review candidate, amount left blank</button>'}
            </form></details>''')
    return f'''<section id="gmail"><h2>Gmail debt register</h2><p>{esc(summary)}</p>
        <p>{len(rows)} cases. Link reminders to an existing debt before creating a new candidate. Source updates never change confirmed amounts or payment status.</p>
        <div class="stack">{''.join(cards)}</div><h3>Repayments, payments and disputes</h3>{context}</section>'''


def action(c, form, stamp):
    row = c.execute('SELECT * FROM finance_gmail_cases WHERE id=?', (form.get('id'),)).fetchone()
    if not row or row['fingerprint'] != form.get('fingerprint'):
        raise ValueError('Source changed. Reload before reviewing.')
    operation = form.get('operation')
    target = row['obligation_id']
    if operation == 'link':
        target = form.get('obligation_id')
        if not c.execute('SELECT id FROM finance_obligations WHERE id=?',(target,)).fetchone():
            raise ValueError('Choose an existing debt')
        if row['obligation_id'] and row['obligation_id'] != target:
            raise ValueError('Already linked. Review the existing debt first.')
    elif operation == 'create':
        if target:
            raise ValueError('This case already has a linked debt')
        ledger = form.get('ledger_id') or None
        if ledger and not c.execute('SELECT id FROM finance_ledgers WHERE id=?',(ledger,)).fetchone():
            raise ValueError('Invalid ledger')
        f = json.loads(row['fields'])
        target = 'gmail-'+hashlib.sha256(row['id'].encode()).hexdigest()
        c.execute('''INSERT INTO finance_obligations
            (id,ledger_id,creditor,description,status,created_at,updated_at)
            VALUES(?,?,?,?, 'review',?,?)''', (target,ledger,str(f.get('Original creditor') or 'Unknown')[:200],
            str(f.get('Case / contract') or row['id'])[:500],stamp,stamp))
    elif operation != 'acknowledge':
        raise ValueError('Invalid source action')
    c.execute('UPDATE finance_gmail_cases SET obligation_id=?,reviewed_fingerprint=? WHERE id=?',
        (target,row['fingerprint'],row['id']))
