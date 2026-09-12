"""Income-cycle presentation. Queuing never changes an agreed date or executes payment."""
import datetime as dt


def window(day):
    anchor=day.replace(day=22)
    if day.day>25:
        anchor=(anchor.replace(day=28)+dt.timedelta(days=4)).replace(day=22)
    end=(anchor.replace(day=28)+dt.timedelta(days=4)).replace(day=22)
    return anchor,anchor.replace(day=25),end


def model(c,ledger,day):
    from payment_planner import schema
    schema(c)
    first,last,end=window(day)
    rows=c.execute('''SELECT i.*,c.creditor,c.reference,c.arrangement,
        p.proposed_date,p.state AS plan_state,p.reason
        FROM finance_instalments i JOIN finance_cases c ON c.id=i.case_id
        LEFT JOIN finance_payment_plan p ON p.id=i.id
        WHERE c.ledger_id=? AND i.locally_paid_at IS NULL ORDER BY i.due_date,i.id''',(ledger,)).fetchall()
    groups={'Check before paying':[],'Next income window':[],'Communication to review':[],'Later commitments':[]}
    for row in rows:
        r=dict(row);status=r['status'].lower()
        if 'paid' in status and not any(x in status for x in ('unpaid','not paid','partially paid','part paid','uncredited','not matched')):continue
        date=dt.date.fromisoformat(r['due_date']) if r['due_date'] else None
        r['planned']=None
        if r['arrangement']=='awaiting' or r['plan_state']=='awaiting-reply':
            r['explanation']='Existing proposal awaiting reply. No repeat request.'
            group='Communication to review'
        elif r['plan_state']=='proposal-draft':
            r['planned']=r['proposed_date'];r['explanation']='Prepared alternative; original agreement remains until accepted.'
            group='Communication to review'
        elif not date or date<day or any(x in status for x in ('disputed','uncredited','not accepted','cancel')):
            r['explanation']='Check source or payment history. This is not a new payment instruction.'
            group='Check before paying'
        elif date<first:
            r['explanation']='Due before the income window. Funding or a change of agreement is needed; no new promise has been made.'
            group='Communication to review'
        elif date<end:
            r['planned']=r['due_date'] if r['plan_state']=='scheduled' else None
            r['explanation']='Existing date fits the recorded forecast.' if r['planned'] else 'Queued for the cycle; affordability is not cleared.'
            group='Next income window'
        else:
            r['explanation']='Outside the upcoming income cycle.';group='Later commitments'
        groups[group].append(r)
    return {'first':first,'last':last,'end':end,'groups':groups}


def allocate(result, candidates, ledger, day, horizon=90):
    """Separate conditional scenario; never write agreements, bank balances or sends."""
    from planning import dividend_scenario, include_dividend
    names={r['event_name'] for r in candidates}
    base=[dict(r) for r in result['rows'] if not r.get('assumed_funding') and r['name'] not in names]
    def calculate(rows):
        trial=dict(result,rows=sorted([dict(r) for r in rows],key=lambda r:(r['date'],r['amount'])))
        trial['dividend']=dividend_scenario(trial,day,result.get('dividend',{}).get('balances_known',False))
        return include_dividend(trial,day)
    selected=[];unfunded=[]
    for item in sorted(candidates,key=lambda r:(r['due_date'],r['id'])):
        earliest=max(day,dt.date.fromisoformat(item['due_date']))
        found=None
        for offset in range(max(0,(day+dt.timedelta(days=horizon)-earliest).days+1)):
            date=earliest+dt.timedelta(days=offset)
            event=dict(date=date,ledger=ledger,amount=-item['amount_cents'],name=item['event_name'],estimated=False)
            trial=calculate(base+[event])
            floor=result['reserve'] if ledger=='personal' else 0
            if trial['minimum'].get(ledger,0)>=floor and (ledger!='personal' or trial['minimum'].get('cloudstep',0)>=0):
                found=date;base.append(event);break
        (selected if found else unfunded).append(dict(item,proposed=found))
    return dict(selected=selected,unfunded=unfunded,forecast=calculate(base))


def action_panel(c,ledger,day,esc,money):
    from planning import forecast
    result=forecast(c,start=day)
    items=[]
    for r in c.execute("SELECT i.*,c.creditor,c.reference,c.arrangement FROM finance_instalments i JOIN finance_cases c ON c.id=i.case_id WHERE c.ledger_id=? AND i.locally_paid_at IS NULL",(ledger,)):
        r=dict(r);r['event_name']=r['creditor']+' / '+r['id']
        if not r['due_date'] or not r['amount_cents'] or r['arrangement']=='awaiting':continue
        if c.execute("SELECT 1 FROM finance_case_outreach WHERE case_id=? AND status='user-reported-sent'",(r['case_id'],)).fetchone():continue
        if any(e['name']==r['event_name'] and e['ledger']==ledger for e in result['rows']):items.append(r)
    accounts=list(c.execute('SELECT balance_cents FROM finance_accounts WHERE ledger_id=?',(ledger,)))
    if not accounts or any(r['balance_cents'] is None for r in accounts):
        return '<section class="cc-surface"><h2>Your payment options</h2><p>Enter the missing account balances to calculate dated payment options.</p></section>'
    scenario=allocate(result,items,ledger,day)
    incoming=[r for r in result['rows'] if r['ledger']==ledger and r['amount']>0 and not r.get('assumed_funding')]
    next_income=min(incoming,key=lambda r:r['date']) if incoming else None
    floor=result['reserve'] if ledger=='personal' else 0
    gap=max(0,floor-result['minimum'].get(ledger,0))
    lead=('Next recorded income: '+money(next_income['amount'])+' on '+next_income['date'].strftime('%d %B')+' · '+esc(next_income['name'])) if next_income else 'No incoming payment is recorded in the next 90 days.'
    rows=[]
    for r in scenario['selected']:
        changed=r['proposed'].isoformat()!=r['due_date']
        action='Send proposed change' if changed else 'Keep existing date'
        draft=''
        if changed:
            text=('Beste heer/mevrouw,\n\nBetreft: '+r['reference']+'\n\nVanwege de timing van mijn inkomsten stel ik voor de termijn van '+money(r['amount_cents'])+' van '+r['due_date']+' te betalen op '+r['proposed'].isoformat()+'. Kunt u bevestigen of u hiermee akkoord gaat?\n\nMet vriendelijke groet,\nDiederik Sjardijn')
            draft='<details><summary>Copy draft</summary><pre style="white-space:pre-wrap">'+esc(text)+'</pre></details>'
        rows.append('<tr><td>'+esc(r['creditor'])+'</td><td>'+money(r['amount_cents'])+'</td><td>'+r['proposed'].strftime('%d %B')+'</td><td>'+action+draft+'</td></tr>')
    excluded=sum(r['amount_cents'] for r in scenario['unfunded'])
    remainder=('<p><strong>'+money(excluded)+' could not be allocated.</strong> Those instalments remain outstanding and are excluded from this alternative schedule.</p>') if excluded else ''
    if scenario['unfunded']:
        remainder+='<details><summary>Unfunded instalments</summary>'+''.join('<p>'+esc(r['creditor'])+' · '+money(r['amount_cents'])+' · existing date '+esc(r['due_date'])+'</p>' for r in scenario['unfunded'])+'</details>'
    table=('<div class="table-wrap"><table><thead><tr><th>Payee</th><th>Amount</th><th>Suggested payment date</th><th>Your action</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></div>') if rows else '<p>No instalment fits the recorded cashflow yet. Fixed costs and retained commitments alone may exhaust the available money.</p>'
    return '<section class="cc-surface"><h2>Your payment options</h2><p>'+lead+'</p><p>Existing schedule: '+money(gap)+' below the reserve at its lowest point.</p>'+table+remainder+'<p>Calculated automatically from recorded cashflow, with fixed costs reserved and affordable Cloudstep funding included. Changed dates are proposals until accepted. Unresolved claims are not treated as agreed payments. Confirm receipts before paying.</p></section>'


def panel(c,ledger,day,esc,money):
    m=model(c,ledger,day)
    parts=[]
    for title,items in m['groups'].items():
        if not items:continue
        rows=''.join('<tr><td>'+esc(r['creditor'])+'<small> '+esc(r['reference'])+'</small></td><td>'+money(r['amount_cents'])+'</td><td>'+esc(r['due_date'] or 'Source window / date unresolved')+'</td><td>'+esc(r['planned'] or 'Not cleared')+'</td><td>'+esc(r['explanation'])+'</td></tr>' for r in items)
        parts.append('<details><summary>'+esc(title)+' ('+str(len(items))+')</summary><div class="table-wrap"><table><thead><tr><th>Creditor</th><th>Amount</th><th>Existing date</th><th>Planned date</th><th>Next step</th></tr></thead><tbody>'+rows+'</tbody></table></div></details>')
    return (action_panel(c,ledger,day,esc,money)+'<section class="cc-surface" id="payment-cycle"><h2>Your next income cycle</h2><p><strong>'+m['first'].strftime('%d %B')+'–'+m['last'].strftime('%d %B')+'</strong> · plan through '+m['end'].strftime('%d %B')+'</p><p>Confirm incoming money in your bank first. Fixed costs, existing instalments and the personal reserve remain in the forecast. Queued items are not automatically affordable.</p>'+''.join(parts)+'<p><a href="/dashboard/reference#planning">Review funding gaps and the full forecast</a></p></section>')
