"""Scope forecast uncertainty without treating unrelated registers as cash movements."""
from collections import defaultdict


def dependencies(c, ledger):
    result={ledger}
    rules=list(c.execute('SELECT * FROM finance_plan_rules WHERE active=1'))
    changed=True
    while changed:
        changed=False
        for incoming in rules:
            if incoming['ledger_id'] not in result or incoming['amount_cents']<=0 or not incoming['transfer_id']:continue
            for outgoing in rules:
                if outgoing['transfer_id']==incoming['transfer_id'] and outgoing['amount_cents']<0 and outgoing['ledger_id'] not in result:
                    result.add(outgoing['ledger_id']);changed=True
    return result


def classify(c, result, ledger):
    relevant=dependencies(c,ledger)
    groups=defaultdict(list); blocking=[]; warnings=[]
    owners={}
    for r in c.execute('SELECT id,ledger_id,creditor,reference FROM finance_cases'):
        owners[r['creditor']+' / '+r['reference']]=r['ledger_id']
    for r in c.execute('SELECT i.id,c.ledger_id,c.creditor FROM finance_instalments i JOIN finance_cases c ON c.id=i.case_id'):
        owners[r['creditor']+' / '+r['id']]=r['ledger_id']
    all_ledgers=[r[0] for r in c.execute('SELECT id FROM finance_ledgers')]
    for issue in result['issues']:
        suffix=issue.split(': ',1)[-1]
        if suffix in owners:
            owner=owners[suffix]
            if owner and owner not in relevant:continue
            if issue.startswith('Payment window reserved'):
                groups['Payment windows — amounts already reserved'].append(issue)
            elif issue.startswith('Existing proposal awaiting reply'):
                groups['Awaiting replies — existing proposals preserved'].append(issue)
            else:
                groups['Case records to reconcile — not payment instructions'].append(issue)
            continue
        if any('('+x+')' in issue for x in all_ledgers if x not in relevant):continue
        if issue.startswith('After '):warnings.append(issue);continue
        blocking.append(issue)
    # Unknown obligations are not assumed free cash. One summary replaces a wall
    # of individual claims; amounts and payment instructions remain distinct.
    pending=groups.get('Case records to reconcile — not payment instructions',[])
    if pending:blocking.append(f'{len(pending)} case records need reconciliation before a reliable spending allowance can be calculated')
    return {'blocking':blocking,'warnings':warnings,'groups':dict(groups),'dependencies':relevant}


def shortfall(rows, opening, ledger, floor, start):
    balance=opening; first=None; lowest=opening; lowest_date=start
    if balance<floor:first={'date':start,'name':'Opening balance','balance':balance,'gap':floor-balance}
    for r in rows:
        if r['ledger']!=ledger:continue
        balance+=r['amount']
        if balance<lowest:lowest=balance;lowest_date=r['date']
        if first is None and balance<floor:
            first={'date':r['date'],'name':r['name'],'balance':balance,'gap':floor-balance}
    if first:first.update(lowest=lowest,lowest_date=lowest_date,total_gap=floor-lowest)
    return first


def describe(breach, label):
    return (f"{label}: {breach['date'].isoformat()} after {breach['name']}; "
            f"projected balance €{breach['balance']/100:,.2f}, shortfall €{breach['gap']/100:,.2f}. "
            f"Largest gap €{breach['total_gap']/100:,.2f} on {breach['lowest_date'].isoformat()} "
            '(same-day payments before receipts).')
