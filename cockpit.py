"""Daily cashflow cockpit. Forecast advice, never a payment execution service."""
import calendar
import datetime as dt
import hashlib
from decimal import Decimal, InvalidOperation
import json
from zoneinfo import ZoneInfo


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS cockpit_budget (
        id INTEGER PRIMARY KEY CHECK(id=1), start_month TEXT NOT NULL,
        opening_cents INTEGER NOT NULL, monthly_cents INTEGER NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS cockpit_events (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, amount_cents INTEGER,
        details TEXT NOT NULL, recorded_at TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS cockpit_reconciliations (
        id INTEGER PRIMARY KEY, ledger_id TEXT NOT NULL, difference_cents INTEGER NOT NULL,
        recorded_at TEXT NOT NULL, resolved_at TEXT, classification TEXT)''')


def euros(value,signed=False):
    try:
        v=Decimal(str(value).replace('€','').replace(' ','').replace(',','.'))
        if not v.is_finite() or (v<0 and not signed) or abs(v)>Decimal('100000000'):
            raise ValueError()
        if v*100!=(v*100).to_integral_value():raise ValueError()
        return int(v*100)
    except (InvalidOperation,ValueError):
        raise ValueError('Enter a valid euro amount with at most two decimals')


def version(accounts):
    return hashlib.sha256(json.dumps([(a['id'],a['balance_cents'],a['updated_at']) for a in accounts]).encode()).hexdigest()


def remaining(c,date):
    row=c.execute('SELECT * FROM cockpit_budget WHERE id=1').fetchone()
    if not row:return None
    start=dt.date.fromisoformat(row['start_month']+'-01')
    months=(date.year-start.year)*12+date.month-start.month
    if months<0:return None
    spent=c.execute("SELECT COALESCE(SUM(amount_cents),0) FROM cockpit_events WHERE kind='spending'").fetchone()[0]
    return row['opening_cents']+months*row['monthly_cents']-spent


def action(c,f,stamp):
    from planning import today
    schema(c)
    action=f['action']
    key=f.get('request_id','')
    if not key or len(key)>100:raise ValueError('Reload the page before saving')
    if c.execute('SELECT 1 FROM cockpit_events WHERE id=?',(key,)).fetchone():return
    value=None; details={}
    if action=='cockpit-balances':
        accounts=list(c.execute('SELECT * FROM finance_accounts WHERE active=1 ORDER BY id'))
        if f.get('version')!=version(accounts):raise ValueError('Balances changed. Reload before updating them.')
        changes={}; totals={}; complete={}
        for a in accounts:
            raw=f.get('balance:'+a['id'],'').strip()
            value_now=euros(raw,True) if raw else None
            changes[a['id']]=value_now
            complete[a['ledger_id']]=complete.get(a['ledger_id'],True) and a['balance_cents'] is not None and value_now is not None
            totals[a['ledger_id']]=totals.get(a['ledger_id'],0)+(value_now or 0)-(a['balance_cents'] or 0)
        for ledger,delta in totals.items():
            if delta and complete[ledger]:
                c.execute('INSERT INTO cockpit_reconciliations(ledger_id,difference_cents,recorded_at) VALUES(?,?,?)',(ledger,delta,stamp))
        for a in accounts:
            c.execute('UPDATE finance_accounts SET balance_cents=?,updated_at=? WHERE id=?',(changes[a['id']],stamp,a['id']))
        details={'balances':changes}
    elif action=='cockpit-budget':
        if c.execute('SELECT 1 FROM cockpit_budget').fetchone():raise ValueError('Budget already initialized; existing rollover preserved')
        value=euros(f.get('amount',''))
        c.execute('INSERT INTO cockpit_budget VALUES(1,?,?,150000)',(today().strftime('%Y-%m'),value))
        details={'meaning':'Remaining current-month living allowance, including prior carryover'}
    elif action=='cockpit-movement':
        from planning import forecast
        matches=[r for r in forecast(c)['rows'] if movement_id(r)==f.get('movement_id') and r['date']<=today() and not r['name'].startswith('Reserve only · ')]
        if len(matches)!=1:raise ValueError('Movement changed or is already confirmed. Reload first.')
        r=matches[0]
        details={'date':r['date'].isoformat(),'ledger':r['ledger'],'amount':r['amount'],'name':r['name']}
        value=r['amount'];action='movement'
    elif action=='cockpit-reconcile':
        r=c.execute('SELECT * FROM cockpit_reconciliations WHERE id=? AND resolved_at IS NULL',(f.get('id'),)).fetchone()
        if not r:raise ValueError('This balance movement was already resolved')
        classification=f.get('classification')
        if classification not in ('spending','known','mixed'):raise ValueError('Choose how to classify the movement')
        if classification=='mixed':raise ValueError('Leave this open until the component payments are reconciled')
        if classification=='spending':
            if r['ledger_id']!='personal' or r['difference_cents']>=0:raise ValueError('Only a personal net decrease can be classified as living spending')
            if remaining(c,today()) is None:raise ValueError('Set your starting living allowance first')
            value=-r['difference_cents'];action='spending'
        c.execute('UPDATE cockpit_reconciliations SET resolved_at=?,classification=? WHERE id=?',(stamp,classification,r['id']))
        details={'reconciliation':r['id'],'classification':classification,'note':f.get('note','')[:500]}
    else:raise ValueError('Unknown cockpit action')
    c.execute('INSERT INTO cockpit_events VALUES(?,?,?,?,?)',(key,action,value,json.dumps(details),stamp))


def movement_id(r):
    return hashlib.sha256(json.dumps([r['date'].isoformat(),r['ledger'],r['amount'],r['name']]).encode()).hexdigest()


def model(c,ledger,start=None):
    from planning import today,forecast
    schema(c);start=start or today()
    result=forecast(c,start=start)
    accounts=list(c.execute('SELECT * FROM finance_accounts WHERE active=1 ORDER BY id'))
    own=[a for a in accounts if a['ledger_id']==ledger]
    available=sum(a['balance_cents'] or 0 for a in own) if own and all(a['balance_cents'] is not None for a in own) else None
    from forecast_checks import classify,shortfall,describe
    checks=classify(c,result,ledger)
    problems=list(checks['blocking'])
    latest_paid=c.execute('''SELECT MAX(i.locally_paid_at) FROM finance_instalments i
        JOIN finance_cases c ON c.id=i.case_id WHERE c.ledger_id=?''',(ledger,)).fetchone()[0]
    for e in c.execute("SELECT recorded_at,details FROM cockpit_events WHERE kind='movement'"):
        if json.loads(e['details'])['ledger']==ledger:
            latest_paid=max(latest_paid or '',e['recorded_at'])
    if latest_paid and any(a['updated_at']<latest_paid for a in own):
        problems.insert(0,'Update bank balances after recording a payment or receipt')
    reconciliations=list(c.execute('SELECT * FROM cockpit_reconciliations WHERE resolved_at IS NULL ORDER BY recorded_at'))
    if any(r['ledger_id'] in checks['dependencies'] for r in reconciliations):problems.insert(0,'Balance movements need reconciliation')
    if not c.execute('SELECT 1 FROM finance_plan_baseline').fetchone():problems.insert(0,'Starting income and commitments need setup')
    budget=remaining(c,start)
    if ledger=='personal' and budget is None:problems.insert(0,'Set the remaining living allowance for this month')
    for dependency in checks['dependencies']-{ledger}:
        breach=shortfall(result['rows'],result.get('opening',{}).get(dependency,0),dependency,0,start)
        if breach:problems.insert(0,describe(breach,dependency+' funding needed for transfers'))
    # The forecast already reserves living expenses. Add back only this month's
    # living-budget debit before capping by the actual remaining allowance.
    rows=result['rows'];low=available or 0;running=low
    for row in rows:
        if row['ledger']!=ledger:continue
        if ledger=='personal' and row['name']=='Food and discretionary spending' and row['date'].strftime('%Y-%m')==start.strftime('%Y-%m'):continue
        running+=row['amount'];low=min(low,running)
    floor=result['reserve'] if ledger=='personal' else 0
    adjusted=[r for r in rows if not (ledger=='personal' and r['name']=='Food and discretionary spending' and r['date'].strftime('%Y-%m')==start.strftime('%Y-%m'))]
    breach=shortfall(adjusted,available or 0,ledger,floor,start)
    if breach and available is not None:problems.insert(0,describe(breach,'Reserve breach'))
    allowance=None if problems else max(0,min(budget,low-floor) if ledger=='personal' else low)
    upcoming=[r for r in rows if r['ledger']==ledger and start<=r['date']<=start+dt.timedelta(days=7)]
    incomes=[r['date'] for r in rows if r['ledger']==ledger and r['amount']>0 and r['date']>start and not r['estimated']]
    until=min(incomes) if incomes else None
    return dict(result=result,accounts=accounts,own=own,available=available,problems=list(dict.fromkeys(problems)),
                budget=budget,allowance=allowance,upcoming=upcoming,until=until,reconciliations=reconciliations,checks=checks,breach=breach)


def page(token,notice='',ledger='personal'):
    from mailroom import db
    from dashboard import esc,money,csrf,amount_input,shell
    from planning import today
    import secrets
    with db() as c:
        ledgers=list(c.execute('SELECT * FROM finance_ledgers'))
        if ledger not in {l['id'] for l in ledgers}:ledger='personal'
        m=model(c,ledger)
        from payment_cycle import panel as cycle_panel
        cycle_html=cycle_panel(c,ledger,today(),esc,money)
        instalments=list(c.execute('''SELECT i.*,c.creditor,c.ledger_id,c.reference FROM finance_instalments i JOIN finance_cases c ON c.id=i.case_id
             WHERE c.ledger_id=? AND i.locally_paid_at IS NULL AND i.due_date=?''',(ledger,today().isoformat())))
        drafts=list(c.execute("SELECT o.*,c.creditor,c.ledger_id FROM finance_case_outreach o JOIN finance_cases c ON c.id=o.case_id WHERE o.status IN ('draft','planner-draft') AND c.ledger_id=?",(ledger,)))
    def fields(action):
        return f'<input type="hidden" name="csrf" value="{csrf(token)}"><input type="hidden" name="action" value="{action}"><input type="hidden" name="return_ledger" value="{esc(ledger)}"><input type="hidden" name="request_id" value="{secrets.token_hex(16)}">'
    balances=''.join(f'<label>{esc(next(l["name"] for l in ledgers if l["id"]==a["ledger_id"]))} · {esc(a["name"])}<input name="balance:{esc(a["id"])}" value="{amount_input(a["balance_cents"])}" inputmode="decimal"><small>Last updated {esc(a["updated_at"][:10])}</small></label>' for a in m['accounts'])
    forms=f'<details id="balances"><summary>Update balances</summary><form method="post" action="/dashboard/action">{fields("cockpit-balances")}<input type="hidden" name="version" value="{version(m["accounts"])}"><div class="cc-fields">{balances}</div><p>Use available bank balances. Leave unknown accounts blank. Saving does not infer which bills were paid.</p><button>Save all balances</button></form></details>'
    actions=[]
    for i in instalments:
        label='Due today · forecast not cleared' if m['problems'] else 'Due today · recorded instalment'
        actions.append(f'<article class="cc-task"><div class="cc-row"><span>{label}</span><strong>{money(i["amount_cents"])}</strong></div><h3>{esc(i["creditor"])}</h3><p>{esc(i["reference"])}</p><small>{esc(i["status"])}. Verify the original agreement and payment details.</small><form method="post" action="/dashboard/action">{fields("case-paid")}<input type="hidden" name="id" value="{esc(i["id"])}"><button>I paid this</button></form></article>')
    for d in drafts:
        actions.append(f'<article class="cc-task"><span>REVIEW &amp; SEND · DRAFT ONLY</span><h3>{esc(d["creditor"])}</h3><details><summary>Review prepared message</summary><textarea readonly rows="10">{esc(d["draft"])}</textarea><form method="post" action="/dashboard/action">{fields("case-draft-sent")}<input type="hidden" name="id" value="{esc(d["case_id"])}"><button>I sent this myself</button></form></details></article>')
    case_names={i['creditor']+' / '+i['id'] for i in instalments}
    for r in m['upcoming']:
        if r['name'].startswith('Reserve only · ') or r['date']!=today() or r['name'] in case_names or r['name']=='Food and discretionary spending':continue
        label='Confirm receipt' if r['amount']>0 else ('Due today · forecast not cleared' if m['problems'] else 'Pay manually')
        actions.append(f'<article class="cc-task"><span>{label}</span><h3>{esc(r["name"])}</h3><p>{money(r["amount"])}</p><form method="post" action="/dashboard/action">{fields("cockpit-movement")}<input type="hidden" name="movement_id" value="{movement_id(r)}"><button>{"Received in my bank account" if r["amount"]>0 else "I paid this"}</button></form><small>Confirm only after checking your bank. Update balances separately.</small></article>')
    questions=[]
    if m['budget'] is None and ledger=='personal':
        questions.append(f'<details class="cc-question"><summary>What living allowance remains this month?</summary><p>Include any unused carryover. This is a budget, not your bank balance. Future months add €1,500; unused amounts and overspending carry forward.</p><form method="post" action="/dashboard/action">{fields("cockpit-budget")}<label>Remaining allowance (€)<input name="amount" inputmode="decimal" required></label><button>Set starting allowance</button></form></details>')
    for r in m['reconciliations']:
        if r['ledger_id']!=ledger:continue
        spending='<option value="spending">Entire decrease was everyday spending</option>' if ledger=='personal' and r['difference_cents']<0 else ''
        questions.append(f'<details class="cc-question"><summary>Explain balance movement: {money(r["difference_cents"])}</summary><p>Since {esc(r["recorded_at"][:10])}. Check the transactions first; a net change can contain both income and spending.</p><form method="post" action="/dashboard/action">{fields("cockpit-reconcile")}<input type="hidden" name="id" value="{r["id"]}"><select name="classification">{spending}<option value="known">Reconciled with known payments / income / transfers</option><option value="mixed">Mixed or still unknown: keep open</option></select><label>Reconciliation note<input name="note" maxlength="500"></label><button>Confirm classification</button></form></details>')
    nextrows=''.join(f'<div class="cc-next"><span>{r["date"].strftime("%d %b")}</span><div>{esc(r["name"])}<p>{money(r["amount"])} · {"estimated" if r["estimated"] else "recorded, not bank-confirmed"}</p></div></div>' for r in m['upcoming'])
    title='Available for everyday spending' if ledger=='personal' else 'Unallocated cash after commitments'
    amount='Not yet calculated' if m['allowance'] is None else money(m['allowance'])
    subtitle='Resolve the items below before relying on a spending allowance.' if m['problems'] else ('Until '+m['until'].strftime('%d %B') if m['until'] else 'No next income date confirmed')
    background=''.join('<details><summary>'+esc(k)+' ('+str(len(v))+')</summary><ul>'+''.join('<li>'+esc(x)+'</li>' for x in v)+'</ul></details>' for k,v in m['checks']['groups'].items())
    background+='<details><summary>Future assumptions</summary><ul>'+''.join('<li>'+esc(x)+'</li>' for x in m['checks']['warnings'])+'</ul></details>' if m['checks']['warnings'] else ''
    reasons=''.join('<li>'+esc(p)+'</li>' for p in m['problems'])
    options=''.join(f'<option value="{l["id"]}" {"selected" if l["id"]==ledger else ""}>{esc(l["name"])}</option>' for l in ledgers)
    banner=f'<p class="notice">{esc(notice)}</p>' if notice else ''
    return shell(f'''<div id="cockpit"><header><div><p class="eyebrow">POSTBODE</p><h2>Cashflow cockpit</h2></div><form method="get" action="/dashboard"><label>Account group<select name="ledger">{options}</select></label><button>Show</button></form><form method="post" action="/dashboard/logout">{fields('logout')}<button class="quiet">Sign out</button></form></header>
    <main>{banner}<div class="cc-row cc-intro"><div><h2>Your day, under control.</h2><p>{today().strftime('%A %d %B')} · manual payments</p></div></div>{forms}
    <section class="cc-hero"><div><p class="eyebrow">{title}</p><h1>{amount}</h1><p>{subtitle}</p><details><summary>How this allowance works</summary><p>Remaining living budget: {money(m['budget']) if ledger=='personal' else 'Separate company ledger'}. Your allowance is capped by the recorded 90-day cashflow, not just today’s balance. Future income is not cash already received.</p><p>Manual balances and incomplete information cannot guarantee an actual minimum bank balance.</p></details></div><aside><p>Current available balance<strong>{money(m['available'])}</strong></p><p>{'Protected personal minimum' if ledger=='personal' else 'Company cash floor'}<strong>{money(m['result']['reserve'] if ledger=='personal' else 0)}</strong></p><p>{'Unused living allowance rolls forward' if ledger=='personal' else 'Not automatically available for dividends'}</p></aside></section>
    {cycle_html}<div class="cc-columns"><section class="cc-surface"><h2>Today’s actions</h2>{''.join(actions) or '<p>No dated case actions identified for today. This does not mean all obligations are resolved.</p>'}<details><summary>Other payments and receipts due today</summary>{''.join('<p>'+esc(r['name'])+' · '+money(r['amount'])+'</p>' for r in m['upcoming'] if r['date']==today()) or '<p>No additional dated items.</p>'}</details></section>
    <aside><section class="cc-surface"><h2>Next 7 days</h2>{nextrows or '<p>No dated movements recorded.</p>'}<a href="/dashboard/reference#planning">Full cashflow outlook</a></section><section class="cc-surface"><h2>Needs your clarification</h2>{''.join(questions[:3]) or '<p>No direct questions identified.</p>'}{'<details><summary>More clarification items</summary>'+''.join(questions[3:])+'</details>' if len(questions)>3 else ''}<details><summary>Forecast checks ({len(m['problems'])})</summary><ul>{reasons}</ul><a href="/dashboard/reference#recurring">Review bill dates and assumptions</a></details></section></aside></div>
    <div class="cc-reference"><details><summary>Case follow-up and future assumptions</summary>{background}</details><a href="/dashboard/reference">Reference: cases, agreements, payment history and settings →</a></div><p class="cc-foot">Messages remain drafts. Payments are manual. Reconcile balances after paying.</p></main></div><style>{CSS}</style>''','Cashflow cockpit')


CSS='''
body{background:#f4f5f2;color:#202c29}#cockpit{font-family:Inter,ui-sans-serif,system-ui,sans-serif}#cockpit header{background:#fff;color:#202c29;position:static;box-shadow:none;padding:20px max(24px,calc((100vw - 1120px)/2));border-bottom:1px solid #dce3dd;gap:18px;flex-wrap:wrap}#cockpit header form{display:flex;align-items:end;gap:10px}#cockpit main{max-width:1120px;padding:24px 28px 40px}#cockpit .eyebrow{color:#365e49}#cockpit h2{font-size:1.3rem;font-weight:550;margin-bottom:12px}#cockpit h3{font-weight:550;margin:8px 0}#cockpit p{color:#5d6e65}#cockpit button{background:#234f40;font-weight:500}#cockpit button.quiet{background:#fff;color:#234f40}#cockpit input,#cockpit select{border-color:#cbd5cf;font-size:16px}#cockpit details{box-shadow:none;border:0;padding:14px 0;border-radius:0;background:transparent}#cockpit summary{font-weight:500}#cockpit .cc-row{display:flex;justify-content:space-between;gap:18px;align-items:center;flex-wrap:wrap}#cockpit .cc-intro{margin-bottom:6px}#cockpit #balances{background:#fff;border-radius:12px;padding:16px 20px;margin-bottom:24px}#cockpit .cc-fields{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:20px 0}#cockpit .cc-hero{background:#e3eee5;padding:28px;border-radius:16px;display:grid;grid-template-columns:1.6fr 1fr;gap:24px;margin:0 0 24px}#cockpit .cc-hero h1{font-size:clamp(2rem,5vw,3.6rem);letter-spacing:-.04em;margin:14px 0}#cockpit .cc-hero aside{border-left:1px solid #bdcebf;padding-left:24px}#cockpit .cc-hero strong{display:block;color:#202c29;font-size:1.35rem;margin-top:6px}#cockpit .cc-columns{display:grid;grid-template-columns:1.4fr 1fr;gap:24px}#cockpit .cc-surface{background:#fff;border-radius:14px;padding:24px;margin:0 0 24px}#cockpit .cc-task{padding:20px 0;border-top:1px solid #e3e8e3}#cockpit .cc-task form{margin-top:12px}#cockpit textarea{width:100%;font:inherit;padding:12px;resize:vertical}#cockpit .cc-next{display:flex;gap:16px;border-top:1px solid #e3e8e3;padding:14px 0}#cockpit .cc-next>span{white-space:nowrap;color:#5d6e65}#cockpit .cc-next p{margin:4px 0 0}#cockpit .cc-question{background:#f6efdc;border-radius:12px;padding:16px;margin:12px 0}#cockpit .cc-question form{display:grid;gap:12px}#cockpit a{color:#234f40}#cockpit .cc-reference{background:#fff;padding:20px;border-radius:12px}#cockpit .cc-foot{margin-top:20px;font-size:.85rem}#cockpit li{margin-bottom:8px;overflow-wrap:anywhere}@media(max-width:700px){#cockpit .cc-columns,#cockpit .cc-hero{grid-template-columns:1fr}#cockpit .cc-fields{grid-template-columns:1fr}#cockpit .cc-hero aside{border-left:0;border-top:1px solid #bdcebf;padding:16px 0 0}#cockpit main{padding:18px 16px}#cockpit header{padding:18px 16px}#cockpit .cc-surface{padding:18px}}
'''
