"""Canonical cases, immutable source history and preserved payment arrangements.

Source claims do not authorize money transfers or creditor messages.
"""
import datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re


def norm(value):
    return re.sub(r'[^a-z0-9]', '', str(value or '').casefold())


def amount(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number=Decimal(str(value))
        if not number.is_finite() or number<0:
            return None
        return int((number*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return None


def date(value):
    if not isinstance(value,str):
        return None
    try:
        return dt.date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def ledger(fields, sheet):
    # Legal debtor identity is never inferred from who receives or funds a letter.
    name=norm(fields.get('Debtor / liability holder'))
    names={'dmjsjardijn':'personal','diederiksjardijn':'personal',
           'dmjsjardijnhousehold':'personal','cloudstepholdingbv':'cloudstep',
           'growthtechnologygroupbv':'growth','growthtechnologygroup':'growth',
           'mesdaghbeheerbv':'mesdagh'}
    result=names.get(name)
    if sheet=='Unclear Liability':
        return None
    if sheet=='Company Debts' and result=='personal':
        return None
    if sheet=='Personal Debts' and result!='personal':
        return None
    return result


def arrangement(value):
    s=str(value or '').lower()
    if any(x in s for x in ('awaiting reply','not accepted','arrangement requested','proposed','proposal')):
        return 'awaiting' if 'unsent' not in s else 'draft'
    if 'accepted' in s and not any(x in s for x in ('quote','engagement')):
        return 'accepted'
    if 'default' in s or 'enforcement' in s:
        return 'enforcement'
    return 'unknown'


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS finance_cases (
        id TEXT PRIMARY KEY, ledger_id TEXT, creditor TEXT NOT NULL,
        reference TEXT NOT NULL, fields TEXT NOT NULL, arrangement TEXT NOT NULL,
        source_at TEXT NOT NULL, exception TEXT, origin TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS finance_case_evidence (
        source_id TEXT NOT NULL, fingerprint TEXT NOT NULL, case_id TEXT NOT NULL,
        payload TEXT NOT NULL, recorded_at TEXT NOT NULL,
        PRIMARY KEY(source_id,fingerprint))''')
    c.execute('''CREATE TABLE IF NOT EXISTS finance_case_mail (
        mail_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, conflict TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS finance_instalments (
        id TEXT PRIMARY KEY, case_id TEXT NOT NULL, amount_cents INTEGER,
        due_date TEXT, status TEXT NOT NULL, fields TEXT NOT NULL,
        source_at TEXT NOT NULL, locally_paid_at TEXT, retained INTEGER NOT NULL DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS finance_case_outreach (
        case_id TEXT PRIMARY KEY, draft TEXT NOT NULL, status TEXT NOT NULL,
        source_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')


def evidence(c,source,ident,fields,stamp):
    raw=json.dumps(fields,sort_keys=True,ensure_ascii=False,allow_nan=False)
    digest=hashlib.sha256(raw.encode()).hexdigest()
    c.execute('INSERT OR IGNORE INTO finance_case_evidence VALUES(?,?,?,?,?)',
              (source,digest,ident,raw,stamp))


def import_snapshot(c,payload,stamp):
    """Carry every case forward without asking the user to reapprove it."""
    sheets=payload['sheets']
    seen=set()
    for sheet in ('Personal Debts','Company Debts','Unclear Liability'):
        for f in sheets[sheet]:
            ident=f['Debt ID']; seen.add(ident)
            owner=ledger(f,sheet)
            state=arrangement(f.get('Arrangement status'))
            problem=None if owner else 'Legal debtor needs resolution'
            c.execute('''INSERT INTO finance_cases VALUES(?,?,?,?,?,?,?,?, 'gmail')
                ON CONFLICT(id) DO UPDATE SET ledger_id=excluded.ledger_id,
                creditor=excluded.creditor,reference=excluded.reference,fields=excluded.fields,
                arrangement=excluded.arrangement,source_at=excluded.source_at,exception=excluded.exception''',
                (ident,owner,str(f.get('Original creditor') or 'Unknown'),str(f.get('Case / contract') or ''),
                 json.dumps(f,ensure_ascii=False),state,payload['generated_at'],problem))
            evidence(c,'gmail:'+ident,ident,f,stamp)
    schedules=set()
    for sheet in ('Personal Repayments','Company Repayments'):
        for f in sheets[sheet]:
            ident=f.get('Schedule ID'); case=f.get('Debt ID')
            if not isinstance(ident,str) or not ident or case not in seen:
                raise ValueError('Schedule lacks stable identity or a current parent case')
            if ident in schedules:
                raise ValueError('Duplicate schedule identity')
            schedules.add(ident)
            status=str(f.get('Status') or '')
            c.execute('''INSERT INTO finance_instalments
                (id,case_id,amount_cents,due_date,status,fields,source_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET case_id=excluded.case_id,
                amount_cents=excluded.amount_cents,due_date=excluded.due_date,status=excluded.status,
                fields=excluded.fields,source_at=excluded.source_at,retained=0''',
                (ident,case,amount(f.get('Amount')) if f.get('Currency')=='EUR' else None,
                 date(f.get('Exact due date')),status,json.dumps(f,ensure_ascii=False),payload['generated_at']))
            evidence(c,'instalment:'+ident,case,f,stamp)
    # An omitted instalment is not evidence of payment or cancellation.
    for r in c.execute('SELECT id FROM finance_instalments').fetchall():
        if r['id'] not in schedules:
            c.execute('UPDATE finance_instalments SET retained=1 WHERE id=?',(r['id'],))
    for sheet in ('Personal Payment History','Company Payment History','Disputes & Reconciliation'):
        for f in sheets[sheet]:
            ident=str(f.get('Debt ID') or f.get('Issue ID') or 'unassigned')
            evidence(c,sheet+':'+ident,ident,f,stamp)
    for r in c.execute("SELECT * FROM mail WHERE analysis IS NOT NULL").fetchall():
        attach_mail(c,r,stamp)


def references(value):
    # Require a substantial identifier containing a digit, never surname-only matching.
    return {norm(s) for s in re.findall(r'[A-Za-z0-9][A-Za-z0-9.\-]{3,}',str(value or ''))
            if any(ch.isdigit() for ch in s) and len(norm(s))>=5}


def attach_mail(c,row,stamp):
    linked=c.execute('''SELECT m.case_id,c.origin FROM finance_case_mail m
        JOIN finance_cases c ON c.id=m.case_id WHERE m.mail_id=?''',(row['id'],)).fetchone()
    if linked and linked['origin']=='gmail':
        return
    try:
        a=json.loads(row['analysis']); payload=json.loads(row['payload'])
        from mailroom import recipient_profiles,RECIPIENT_LEDGER_NAMES
        profile=recipient_profiles().get(payload.get('recipient',{}).get('uuid'),{})
        owner=RECIPIENT_LEDGER_NAMES.get(profile.get('name'))
        ref=a.get('reference',{}).get('value')
        refs=references(ref)
        sender=norm(a.get('sender'))
    except (ValueError,TypeError):
        return
    if not refs or not owner or not sender or a.get('review_required'):
        return
    matches=[]
    for case in c.execute('SELECT * FROM finance_cases WHERE ledger_id=?',(owner,)):
        f=json.loads(case['fields'])
        known={norm(case['creditor']),norm(f.get('Collector'))}-{''}
        if sender in known and refs.intersection(references(case['reference'])):
            matches.append(case)
    gmail_matches=[m for m in matches if m['origin']=='gmail']
    if gmail_matches:
        matches=gmail_matches
    if linked and (len(matches)!=1 or matches[0]['origin']!='gmail'):
        return
    if len(matches)>1:
        return
    if not matches:
        # A new, evidenced letter gets a case, not a second manually linked inbox.
        ident='physical:'+hashlib.sha256((owner+':'+sender+':'+','.join(sorted(refs))).encode()).hexdigest()
        fields={'Original creditor':a.get('sender'),'Case / contract':ref,
                'Latest claimed balance':a.get('amount',{}).get('value'),
                'Currency':a.get('currency',{}).get('value'),
                'Next amount':a.get('amount',{}).get('value'),
                'Next due date':a.get('deadline',{}).get('value'),
                'Next action':a.get('action_detail'),'Arrangement status':'New physical correspondence',
                'Payment status':'Unconfirmed','Summary':a.get('summary')}
        c.execute('''INSERT OR IGNORE INTO finance_cases VALUES(?,?,?,?,?,?,?,?, 'physical')''',
                  (ident,owner,a.get('sender'),ref,json.dumps(fields),'unknown',stamp,
                   'Check for an existing agreement before any new payment proposal'))
        c.execute('INSERT INTO finance_case_mail VALUES(?,?,NULL)',(row['id'],ident))
        evidence(c,'mail:'+row['id'],ident,a,stamp)
        if a.get('draft_reply'):
            c.execute('INSERT OR IGNORE INTO finance_case_outreach VALUES(?,?,?,?,?)',
                      (ident,a['draft_reply'],'draft',stamp,stamp))
        return
    case=matches[0]
    # Link evidence, but a reminder cannot cancel a negotiated arrangement.
    conflict=None
    claimed=amount(a.get('amount',{}).get('value'))
    existing=amount(json.loads(case['fields']).get('Latest claimed balance'))
    if claimed is not None and existing is not None and claimed!=existing:
        conflict='New letter amount differs from the case balance; existing arrangement preserved'
    c.execute('''INSERT INTO finance_case_mail VALUES(?,?,?) ON CONFLICT(mail_id)
        DO UPDATE SET case_id=excluded.case_id,conflict=excluded.conflict''',(row['id'],case['id'],conflict))
    if linked and linked['case_id']!=case['id']:
        c.execute('UPDATE finance_case_evidence SET case_id=? WHERE case_id=?',(case['id'],linked['case_id']))
        # Superseded physical-only drafts must not restart a Gmail negotiation.
        c.execute("UPDATE finance_case_outreach SET status='superseded' WHERE case_id=?",(linked['case_id'],))
        if not c.execute('SELECT 1 FROM finance_case_mail WHERE case_id=?',(linked['case_id'],)).fetchone():
            c.execute('DELETE FROM finance_cases WHERE id=? AND origin=\'physical\'',(linked['case_id'],))
    evidence(c,'mail:'+row['id'],case['id'],a,stamp)


def payments(c,start,end):
    """Return commitments separately from proposals, preserving every original due date."""
    events=[]; issues=[]; scheduled=set()
    for r in c.execute('''SELECT i.*,c.ledger_id,c.creditor,c.arrangement FROM finance_instalments i
                          JOIN finance_cases c ON c.id=i.case_id'''):
        scheduled.add(r['case_id'])
        s=r['status'].lower()
        if r['locally_paid_at'] or ('paid' in s and not any(x in s for x in ('unpaid','not paid','partially paid','part paid'))):
            if not r['locally_paid_at'] and any(x in s for x in ('uncredited','still counts','not matched')):
                issues.append(f"Payment reconciliation, do not pay twice: {r['creditor']} / {r['id']}")
            continue
        if r['retained'] or not r['ledger_id'] or r['amount_cents'] is None or not r['due_date']:
            issues.append(f"Existing instalment needs a date, amount or source check: {r['creditor']} / {r['id']}")
            continue
        if any(x in s for x in ('proposed','not accepted','cancel','disputed','uncredited')):
            issues.append(f"Existing proposal or disputed instalment retained: {r['creditor']} / {r['id']}")
            continue
        due=dt.date.fromisoformat(r['due_date'])
        if due < start:
            issues.append(f"Past-due instalment, verify payment before paying again: {r['creditor']} / {r['id']}")
            continue
        if due<=end:
            events.append((due,r['ledger_id'],-r['amount_cents'],r['creditor']+' / '+r['id'],0))
    for r in c.execute('SELECT * FROM finance_cases'):
        if r['id'] in scheduled:
            continue
        f=json.loads(r['fields'])
        if r['arrangement']=='awaiting':
            issues.append(f"Existing proposal awaiting reply, no new request: {r['creditor']} / {r['reference']}")
            continue
        if r['arrangement']!='accepted':
            f=json.loads(r['fields'])
            if f.get('Next amount') or f.get('Latest claimed balance'):
                issues.append(f"Unscheduled claim, not a payment instruction: {r['creditor']} / {r['reference']}")
            continue
        value=amount(f.get('Next amount')); due=date(f.get('Next due date'))
        if not r['ledger_id'] or value is None or not due:
            issues.append(f"Accepted arrangement incomplete: {r['creditor']} / {r['reference']}")
        elif start<=dt.date.fromisoformat(due)<=end:
            events.append((dt.date.fromisoformat(due),r['ledger_id'],-value,r['creditor']+' / '+r['id'],0))
    return events,issues


def action(c,form,stamp):
    ident=form.get('id')
    if form.get('action')=='case-paid':
        r=c.execute('SELECT * FROM finance_instalments WHERE id=?',(ident,)).fetchone()
        if not r:raise ValueError('Unknown instalment')
        if r['locally_paid_at']:return
        # Record only the instalment, never mark the whole case paid.
        c.execute('UPDATE finance_instalments SET locally_paid_at=? WHERE id=?',(stamp,ident))
        evidence(c,'user-payment:'+ident,r['case_id'],{'confirmation':'User marked this instalment paid',
                 'amount_cents':r['amount_cents'],'date':stamp},stamp)
    elif form.get('action')=='case-draft-sent':
        if not c.execute('SELECT 1 FROM finance_case_outreach WHERE case_id=?',(ident,)).fetchone():
            raise ValueError('Unknown draft')
        c.execute("UPDATE finance_case_outreach SET status='user-reported-sent',updated_at=? WHERE case_id=?",(stamp,ident))
    else:raise ValueError('Unknown case action')


def panel(token):
    from mailroom import db
    from dashboard import esc,money
    from finance_sync import source_fields
    with db() as c:
        rows=c.execute('SELECT * FROM finance_cases ORDER BY ledger_id,creditor,id').fetchall()
        instalments=c.execute('SELECT * FROM finance_instalments ORDER BY due_date,id').fetchall()
        links=c.execute('SELECT * FROM finance_case_mail').fetchall()
        drafts=c.execute("SELECT * FROM finance_case_outreach WHERE status IN ('draft','planner-draft')").fetchall()
        from payment_planner import panel as payment_panel
        queue=payment_panel(c,esc,money)
    cards=[]
    for r in rows:
        schedule=''
        for i in instalments:
            if i['case_id']!=r['id']:continue
            schedule+=f'<li>{esc(i["due_date"] or "Date not established")}: {money(i["amount_cents"])}. {esc(i["status"])}'+(' · payment confirmed by you' if i['locally_paid_at'] else '')+'</li>'
            if not i['locally_paid_at']:
                schedule+=f'''<form method="post" action="/dashboard/action">{token}
                <input type="hidden" name="action" value="case-paid"><input type="hidden" name="id" value="{esc(i['id'])}">
                <button>I paid this instalment</button></form>'''
        attachments=[x for x in links if x['case_id']==r['id']]
        conflicts=''.join(f'<p class="error">{esc(x["conflict"])}</p>' for x in attachments if x['conflict'])
        cards.append(f'''<details class="panel"><summary>{esc(r['ledger_id'] or 'Unclear liability')} · {esc(r['creditor'])} · {esc(r['reference'])}</summary>
        <p>Arrangement: {esc(r['arrangement'])}. {len(attachments)} matched physical letters.</p>
        {conflicts}<ul>{schedule}</ul>{source_fields(json.loads(r['fields']))}</details>''')
    draft_cards=''
    for d in drafts:
        draft_cards+=f'''<details class="panel"><summary>Draft to review and send: {esc(d['case_id'])}</summary>
        <p>Not sent. Check the case history before sending. This is not an accepted payment arrangement.</p>
        <textarea readonly rows="12" style="width:100%">{esc(d['draft'])}</textarea>
        <form method="post" action="/dashboard/action">{token}<input type="hidden" name="action" value="case-draft-sent">
        <input type="hidden" name="id" value="{esc(d['case_id'])}"><button>I sent this myself</button></form></details>'''
    return f'''<section id="cases"><h2>Consolidated cases and existing arrangements</h2>
    <p>{len(rows)} cases, {len(instalments)} preserved instalments. Proposals awaiting replies are not restarted. No creditor messages are sent.</p>
    {queue}<div class="stack">{''.join(cards)}</div><h3>Drafts, not sent</h3>{draft_cards or '<p>No new draft requires sending. Existing proposals remain in their original case history.</p>'}</section>'''
