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


def panel(c,ledger,day,esc,money):
    m=model(c,ledger,day)
    parts=[]
    for title,items in m['groups'].items():
        if not items:continue
        rows=''.join('<tr><td>'+esc(r['creditor'])+'<small> '+esc(r['reference'])+'</small></td><td>'+money(r['amount_cents'])+'</td><td>'+esc(r['due_date'] or 'Source window / date unresolved')+'</td><td>'+esc(r['planned'] or 'Not cleared')+'</td><td>'+esc(r['explanation'])+'</td></tr>' for r in items)
        parts.append('<details><summary>'+esc(title)+' ('+str(len(items))+')</summary><div class="table-wrap"><table><thead><tr><th>Creditor</th><th>Amount</th><th>Existing date</th><th>Planned date</th><th>Next step</th></tr></thead><tbody>'+rows+'</tbody></table></div></details>')
    return ('<section class="cc-surface" id="payment-cycle"><h2>Your next income cycle</h2><p><strong>'+m['first'].strftime('%d %B')+'–'+m['last'].strftime('%d %B')+'</strong> · plan through '+m['end'].strftime('%d %B')+'</p><p>Confirm incoming money in your bank first. Fixed costs, existing instalments and the personal reserve remain in the forecast. Queued items are not automatically affordable.</p>'+''.join(parts)+'<p><a href="/dashboard/reference#planning">Review funding gaps and the full forecast</a></p></section>')
