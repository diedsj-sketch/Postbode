"""Cash planning only. No bank transfers, creditor sends or assumed dividends."""
import calendar
import datetime as dt
import json
from zoneinfo import ZoneInfo


def today():
    return dt.datetime.now(ZoneInfo('Europe/Amsterdam')).date()


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS finance_plan_settings (
        id INTEGER PRIMARY KEY CHECK(id=1), reserve_cents INTEGER NOT NULL,
        updated_at TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS finance_plan_audit (
        id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, details TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS finance_plan_rules (
        id TEXT PRIMARY KEY, ledger_id TEXT NOT NULL, name TEXT NOT NULL,
        amount_cents INTEGER NOT NULL, day INTEGER, estimated INTEGER NOT NULL DEFAULT 0,
        end_date TEXT, transfer_id TEXT, active INTEGER NOT NULL DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS finance_plan_baseline (
        id INTEGER PRIMARY KEY CHECK(id=1), applied_at TEXT NOT NULL)''')
    c.execute("INSERT OR IGNORE INTO finance_plan_settings VALUES(1,50000,?)",
              (dt.datetime.now(dt.timezone.utc).isoformat(),))


def baseline(c, stamp):
    """Apply explicitly supplied inputs once; never reset later reconciled balances."""
    if c.execute('SELECT 1 FROM finance_plan_baseline').fetchone():
        return False
    for ident, amount in [('personal-rabobank',20000),('personal-bunq',75000),('cloudstep-adyen',0)]:
        c.execute('UPDATE finance_accounts SET balance_cents=?,updated_at=? WHERE id=?',
                  (amount,stamp,ident))
    # These IDs replace the earlier seed assumptions in this planner only.
    rules = [
        ('personal-salary','personal','Salary from Cloudstep',350000,23,0,None,'salary'),
        ('personal-tax-returns','personal','Tax refund',15000,15,0,None,None),
        ('personal-denise','personal','Household contribution',100000,25,0,None,None),
        ('personal-cloudstep-rent','personal','Rent from Cloudstep',250000,22,0,'2026-11-30','rent'),
        ('cloudstep-openprovider','cloudstep','Fixed monthly receipts',1000000,22,0,None,None),
        ('cloudstep-salary','cloudstep','Salary to personal',-350000,23,0,None,'salary'),
        ('cloudstep-rent','cloudstep','Rent to personal',-250000,22,0,'2026-11-30','rent'),
        ('cloudstep-employee','cloudstep','Employee salary',-170000,25,0,None,None),
        ('cloudstep-payroll','cloudstep','Payroll tax',-200000,25,0,None,None),
        ('cloudstep-misc','cloudstep','Miscellaneous allowance',-50000,None,1,None,None),
    ]
    # Four monthly estimates, not a perpetual 625 EUR weekly recurrence.
    for day in (7,14,21,28):
        rules.append((f'cloudstep-airbnb-{day}','cloudstep','Airbnb estimate (date provisional)',
                      62500,day,1,None,None))
    c.executemany('''INSERT INTO finance_plan_rules
        (id,ledger_id,name,amount_cents,day,estimated,end_date,transfer_id)
        VALUES(?,?,?,?,?,?,?,?)''',rules)
    c.execute('INSERT INTO finance_plan_baseline VALUES(1,?)',(stamp,))
    c.execute('INSERT INTO finance_plan_audit(recorded_at,details) VALUES(?,?)',
              (stamp,json.dumps({'action':'user-baseline','pending':'none known',
               'rent_end':'November is last assumed payment; December unconfirmed',
               'airbnb_dates':'provisional monthly distribution, not confirmed payout dates'})))
    return True


def occurrences(day, start, end):
    y,m = start.year,start.month
    while (y,m) <= (end.year,end.month):
        date = dt.date(y,m,min(day,calendar.monthrange(y,m)[1]))
        if start <= date <= end:
            yield date
        y,m = (y+1,1) if m==12 else (y,m+1)


def forecast(c, start=None, days=90, include_estimates=False):
    start = start or today()
    end = start + dt.timedelta(days=days)
    accounts = list(c.execute('SELECT * FROM finance_accounts WHERE active=1'))
    ledgers = list(c.execute('SELECT * FROM finance_ledgers'))
    balances = {l['id']:sum(a['balance_cents'] or 0 for a in accounts if a['ledger_id']==l['id']) for l in ledgers}
    opening = dict(balances)
    issues = []
    for a in accounts:
        if a['balance_cents'] is None:
            issues.append(f"Missing balance: {a['name']} ({a['ledger_id']})")
        elif a['updated_at'][:10] < start.isoformat():
            issues.append(f"Balance needs reconciliation: {a['name']} ({a['ledger_id']}), as of {a['updated_at'][:10]}")
    events=[]
    rules=list(c.execute('SELECT * FROM finance_plan_rules WHERE active=1'))
    replaced={r['id'] for r in rules}|({'cloudstep-airbnb'} if rules else set())
    for r in rules:
        if r['day'] is None:
            issues.append(f"Payment timing unknown: {r['name']} ({r['ledger_id']})")
            continue
        if r['estimated'] and r['amount_cents'] > 0 and not include_estimates:
            continue
        stop=min(end,dt.date.fromisoformat(r['end_date'])) if r['end_date'] else end
        for date in occurrences(r['day'],start,stop):
            events.append((date,r['ledger_id'],r['amount_cents'],r['name'],r['estimated']))
        if r['end_date'] and end > dt.date.fromisoformat(r['end_date']):
            issues.append(f"After {r['end_date']}: {r['name']} is unconfirmed")
    for r in c.execute('SELECT * FROM finance_recurring WHERE active=1'):
        if r['id'] in replaced:
            continue
        if r['amount_cents'] is None or not r['next_date']:
            issues.append(f"Amount or payment date missing: {r['name']} ({r['ledger_id']})")
            continue
        sign=1 if r['direction']=='income' else -1
        anchor=dt.date.fromisoformat(r['next_date'])
        if r['frequency']=='monthly':
            dates=occurrences(anchor.day,max(start,anchor),end)
        else:
            date=anchor
            while date < start:
                date+=dt.timedelta(days=7)
            dates=[]
            while date<=end:
                dates.append(date)
                date+=dt.timedelta(days=7)
        for date in dates:
            events.append((date,r['ledger_id'],sign*r['amount_cents'],r['name'],0))
    # Preserve the existing commitments, never substitute a new proposal.
    for r in c.execute("SELECT * FROM finance_obligations WHERE status='confirmed'"):
        if not r['ledger_id'] or r['outstanding_cents'] is None or not r['due_date']:
            issues.append(f"Incomplete existing commitment: {r['creditor']}")
            continue
        date=max(start,dt.date.fromisoformat(r['due_date']))
        if date<=end:
            events.append((date,r['ledger_id'],-r['outstanding_cents'],r['creditor'],0))
    count=c.execute('SELECT COUNT(*) FROM finance_gmail_cases').fetchone()[0]
    if count:
        issues.append(f'{count} Gmail cases and their negotiated instalments are not yet reconciled into this forecast. Do not use it as a payment instruction.')
    reserve=c.execute('SELECT reserve_cents FROM finance_plan_settings WHERE id=1').fetchone()[0]
    minimum=dict(balances)
    rows=[]
    # Debit-first is conservative where only a date, not bank posting time, is known.
    for date,ledger,amount,name,estimated in sorted(events,key=lambda e:(e[0],e[2])):
        balances[ledger]=balances.get(ledger,0)+amount
        minimum[ledger]=min(minimum.get(ledger,0),balances[ledger])
        rows.append({'date':date,'ledger':ledger,'amount':amount,'name':name,
                     'balance':balances[ledger],'estimated':bool(estimated)})
    personal_ids={l['id'] for l in ledgers if l['kind']=='personal'}
    running=sum(opening.get(i,0) for i in personal_ids)
    low=running
    breach=start if running<reserve else None
    for r in rows:
        if r['ledger'] in personal_ids:
            running+=r['amount']
            low=min(low,running)
            if breach is None and running<reserve:
                breach=r['date']
    return {'rows':rows,'opening':opening,'closing':balances,'minimum':minimum,
            'reserve':reserve,'personal_minimum':low,'first_breach':breach,
            'funding_gap':max(0,reserve-low),'issues':list(dict.fromkeys(issues))}


def action(c, form, stamp):
    if form.get('action')=='plan-baseline':
        baseline(c,stamp)
    elif form.get('action')=='plan-reserve':
        from dashboard import cents
        amount=cents(form.get('amount'))
        if amount is None:
            raise ValueError('Enter the personal reserve')
        c.execute('UPDATE finance_plan_settings SET reserve_cents=?,updated_at=? WHERE id=1',(amount,stamp))
        c.execute('INSERT INTO finance_plan_audit(recorded_at,details) VALUES(?,?)',
                  (stamp,json.dumps({'action':'reserve','amount_cents':amount})))
    else:
        raise ValueError('Unknown planning action')


def panel(token):
    from mailroom import db
    from dashboard import esc,money,amount_input
    with db() as c:
        enabled=c.execute('SELECT applied_at FROM finance_plan_baseline').fetchone()
        result=forecast(c)
        estimated=forecast(c,include_estimates=True)
    if not enabled:
        return f'''<section id="planning"><h2>Cashflow planning setup</h2>
        <p>Apply the balances you supplied: Rabobank €200, personal bunq €750, Cloudstep €0.
        This also saves income, paired salary/rent transfers and company payroll costs.
        Do not apply if you have already entered newer balances.</p>
        <form method="post" action="/dashboard/action">{token}
        <input type="hidden" name="action" value="plan-baseline"><button>Apply agreed starting position once</button></form></section>'''
    issues=''.join(f'<li>{esc(i)}</li>' for i in result['issues'])
    rows=''.join(f'<tr><td>{r["date"]}</td><td>{esc(r["ledger"])}</td><td>{esc(r["name"])}</td><td>{money(r["amount"])}</td><td>{money(r["balance"])}</td></tr>' for r in result['rows'])
    return f'''<section id="planning"><h2>Personal reserve and cashflow</h2>
    <p>Manual payments. Creditor messages draft-only. No dividend is assumed.</p>
    <form method="post" action="/dashboard/action">{token}<input type="hidden" name="action" value="plan-reserve">
    <label>Combined personal reserve (€)<input name="amount" value="{amount_input(result['reserve'])}" required></label><button>Save reserve</button></form>
    <p>First projected reserve breach: {esc(result['first_breach'] or 'None in dated items')}.
    90-day funding gap in dated items: {money(result['funding_gap'])}. This is not an approved dividend or a monthly amount.</p>
    <p>Cloudstep lowest projected balance excluding estimated Airbnb: {money(result['minimum'].get('cloudstep'))}.
    Including estimated Airbnb: {money(estimated['minimum'].get('cloudstep'))}.</p>
    <p>Same-day debits are shown before credits until posting times are known. Balances are manually reconciled, not live bank feeds.</p>
    <details open><summary>Forecast incomplete: missing inputs and existing arrangements</summary><ul>{issues}</ul></details>
    <div class="table-wrap"><table><thead><tr><th>Date</th><th>Ledger</th><th>Item</th><th>Movement</th><th>Balance</th></tr></thead><tbody>{rows}</tbody></table></div></section>'''
