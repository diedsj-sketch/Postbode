"""Deterministic draft-only payment planning. Never modifies creditor agreements."""
import datetime as dt
import json


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS finance_payment_plan (
        id TEXT PRIMARY KEY, case_id TEXT NOT NULL, original_date TEXT,
        proposed_date TEXT, amount_cents INTEGER, state TEXT NOT NULL,
        reason TEXT NOT NULL, updated_at TEXT NOT NULL)''')


def safe_date(rows, opening, ledger, amount, earliest, end, reserve):
    """Earliest debit-first date whose entire remaining horizon stays above reserve."""
    relevant=sorted((r for r in rows if r['ledger']==ledger),key=lambda r:(r['date'],r['amount']))
    day=earliest
    while day<=end:
        balance=opening
        for r in relevant:
            if r['date']<day:balance+=r['amount']
        low=balance-amount
        balance=low
        for r in relevant:
            if r['date']>=day:
                balance+=r['amount'];low=min(low,balance)
        if low>=reserve:return day
        day+=dt.timedelta(days=1)
    return None


def refresh(c,stamp,start=None):
    from planning import forecast,today
    from cases import evidence
    schema(c)
    start=start or today();end=start+dt.timedelta(days=90)
    result=forecast(c,start=start)
    from forecast_checks import classify,shortfall,describe
    c.execute('DELETE FROM finance_payment_plan')
    # Clear only machine-generated unsent drafts. Human-reported sends are immutable.
    c.execute("UPDATE finance_case_outreach SET status='superseded' WHERE status='planner-draft'")
    rows=list(result['rows'])
    grouped={}
    for i in c.execute('''SELECT i.*,c.ledger_id,c.creditor,c.reference,c.arrangement
        FROM finance_instalments i JOIN finance_cases c ON c.id=i.case_id
        ORDER BY i.due_date,i.id''').fetchall():
        state='preserved';reason='Existing arrangement retained';proposed=None
        event=next((r for r in rows if r['name']==i['creditor']+' / '+i['id']),None)
        sent=c.execute("SELECT 1 FROM finance_case_outreach WHERE case_id=? AND status='user-reported-sent'",(i['case_id'],)).fetchone()
        if i['locally_paid_at']:
            state='paid';reason='Payment recorded by you; reconcile bank balance'
        elif sent or i['arrangement']=='awaiting':
            state='awaiting-reply';reason='Existing communication preserved; no repeat proposal'
        elif not event:
            state='check-source';reason='Payment status, date or amount needs reconciliation; no duplicate payment instruction'
        else:
            reserve=result['reserve'] if i['ledger_id']=='personal' else 0
            without=[r for r in rows if r is not event]
            original=event['date']
            candidate=safe_date(without,result['opening'].get(i['ledger_id'],0),i['ledger_id'],i['amount_cents'],original,end,reserve)
            checks=classify(c,result,i['ledger_id'])
            blockers=list(checks['blocking'])
            if not c.execute('SELECT 1 FROM finance_plan_baseline').fetchone():blockers.insert(0,'Starting position has not been applied')
            for dependency in checks['dependencies']-{i['ledger_id']}:
                breach=shortfall(rows,result['opening'].get(dependency,0),dependency,0,start)
                if breach:blockers.append(describe(breach,dependency+' funding'))
            if blockers:
                state='forecast-incomplete';reason='No new promise: '+ '; '.join(blockers[:3])
            elif candidate==original:
                state='scheduled';reason='Existing date fits the dated forecast; pay manually'
            elif candidate:
                proposed=candidate.isoformat();state='proposal-draft'
                reason='Alternative only; original agreement remains binding until accepted'
                rows.remove(event);rows.append(dict(event,date=candidate))
                grouped.setdefault(i['case_id'],[]).append((dict(i),proposed))
            else:
                state='funding-shortfall';reason='No affordable replacement date in the next 90 days; no invented promise'
        c.execute('INSERT INTO finance_payment_plan VALUES(?,?,?,?,?,?,?,?)',
                  (i['id'],i['case_id'],i['due_date'],proposed,i['amount_cents'],state,reason,stamp))
    for ident,items in grouped.items():
        case=items[0][0]
        lines='\n'.join('• €'+f"{i['amount_cents']/100:.2f}".replace('.',',')+' op '+d+' (oorspronkelijk '+i['due_date']+')' for i,d in items)
        draft=('Beste heer/mevrouw,\n\nBetreft: '+case['reference']+
               '\n\nIk wil een wijziging van onze bestaande betalingsregeling voorstellen vanwege mijn beschikbare kasstroom. Mijn voorstel is:\n'+lines+
               '\n\nKunt u schriftelijk bevestigen of u hiermee akkoord gaat? Tot uw bevestiging beschouw ik de bestaande regeling niet als gewijzigd.\n\nMet vriendelijke groet,\nDiederik Sjardijn')
        c.execute('''INSERT INTO finance_case_outreach VALUES(?,?,?,?,?)
            ON CONFLICT(case_id) DO UPDATE SET draft=excluded.draft,status=excluded.status,
            source_at=excluded.source_at,updated_at=excluded.updated_at
            WHERE finance_case_outreach.status!='user-reported-sent' ''',
                  (ident,draft,'planner-draft',stamp,stamp))
        evidence(c,'planner-draft:'+ident,ident,{'draft':draft},stamp)
    return result


def panel(c,esc,money):
    schema(c)
    rows=c.execute('SELECT * FROM finance_payment_plan ORDER BY original_date,id').fetchall()
    body=''.join('<tr><td>'+esc(r['case_id'])+'</td><td>'+money(r['amount_cents'])+'</td><td>'+esc(r['original_date'] or 'Unknown')+'</td><td>'+esc(r['proposed_date'] or 'Unchanged')+'</td><td>'+esc(r['state'])+': '+esc(r['reason'])+'</td></tr>' for r in rows)
    return '<h3>Manual payment queue and proposed changes</h3><p>Proposed dates never replace accepted arrangements automatically. Bank balances require reconciliation after payment.</p><div class="table-wrap"><table><thead><tr><th>Case</th><th>Amount</th><th>Agreed date</th><th>Proposed date</th><th>Status</th></tr></thead><tbody>'+body+'</tbody></table></div>'
