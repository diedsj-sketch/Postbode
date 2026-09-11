"""Private financial dashboard for reviewed Postbode correspondence."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import hmac
import html
import json
import os
import secrets
import time
import urllib.parse
from http.cookies import SimpleCookie

from mailroom import db, now


SESSION_COOKIE = 'postbode_dashboard'


def esc(value):
    return html.escape('' if value is None else str(value), quote=True)


def cents(value):
    if not value:
        return None
    try:
        result = Decimal(value.replace('€', '').replace(' ', '').replace(',', '.'))
        if not result.is_finite() or result < 0:
            raise ValueError
        return int((result * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        raise ValueError('Use a positive euro amount')


def money(value):
    if value is None:
        return 'Not set'
    return f'€{value / 100:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


def amount_input(value):
    return '' if value is None else f'{value / 100:.2f}'


def signing_key():
    password = os.environ.get('DASHBOARD_PASSWORD', '')
    return hashlib.sha256(('postbode-dashboard:' + password).encode()).digest() if password else None


def new_session():
    expiry = str(int(time.time()) + 12 * 3600)
    nonce = secrets.token_hex(12)
    payload = expiry + '.' + nonce
    signature = hmac.new(signing_key(), payload.encode(), hashlib.sha256).hexdigest()
    return payload + '.' + signature


def session(environ):
    key = signing_key()
    if not key:
        return None
    cookie = SimpleCookie(environ.get('HTTP_COOKIE', ''))
    morsel = cookie.get(SESSION_COOKIE)
    if not morsel:
        return None
    parts = morsel.value.split('.')
    if len(parts) != 3 or not parts[0].isdigit() or int(parts[0]) < time.time():
        return None
    payload = parts[0] + '.' + parts[1]
    expected = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
    return morsel.value if hmac.compare_digest(expected, parts[2]) else None


def csrf(token):
    return hmac.new(signing_key(), ('csrf:' + token).encode(), hashlib.sha256).hexdigest()


def response(start_response, status, body, headers=()):
    raw = body if isinstance(body, bytes) else body.encode()
    base = [('Content-Type', 'text/html; charset=utf-8'), ('Content-Length', str(len(raw))),
            ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'),
            ('Referrer-Policy', 'no-referrer'), ('X-Frame-Options', 'DENY'),
            ('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")]
    start_response(status, base + list(headers))
    return [raw]


def read_form(environ):
    try:
        length = int(environ.get('CONTENT_LENGTH') or '0')
    except ValueError:
        raise ValueError('Invalid request')
    if length <= 0 or length > 64 * 1024:
        raise ValueError('Invalid request')
    raw = environ['wsgi.input'].read(length).decode('utf-8')
    return {key: values[-1] for key, values in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}


def valid_date(value):
    if not value:
        return None
    return dt.date.fromisoformat(value).isoformat()


def require_known(connection, table, ident):
    if not ident or connection.execute(f'SELECT 1 FROM {table} WHERE id=?', (ident,)).fetchone() is None:
        raise ValueError('Unknown record')


def apply_action(form):
    stamp = now()
    action = form.get('action')
    with db() as c:
        if action in ('case-paid','case-draft-sent'):
            from cases import action as case_action
            case_action(c,form,stamp)
        elif action in ('plan-baseline', 'plan-reserve'):
            from planning import action as planning_action
            planning_action(c, form, stamp)
        elif action == 'gmail-case':
            from finance_sync import action as gmail_action
            gmail_action(c, form, stamp)
        elif action == 'account':
            ident = form.get('id')
            require_known(c, 'finance_accounts', ident)
            c.execute('UPDATE finance_accounts SET balance_cents=?,updated_at=? WHERE id=?',
                      (cents(form.get('amount')), stamp, ident))
        elif action == 'recurring':
            ident = form.get('id')
            require_known(c, 'finance_recurring', ident)
            c.execute('''UPDATE finance_recurring SET amount_cents=?,next_date=?,active=?
                         WHERE id=?''', (cents(form.get('amount')), valid_date(form.get('next_date')),
                                        1 if form.get('active') == 'on' else 0, ident))
        elif action == 'obligation':
            ident = form.get('id')
            require_known(c, 'finance_obligations', ident)
            status = form.get('status')
            if status not in ('review', 'confirmed', 'paid', 'dismissed'):
                raise ValueError('Invalid status')
            ledger = form.get('ledger_id') or None
            if ledger:
                require_known(c, 'finance_ledgers', ledger)
            amount = cents(form.get('amount'))
            if status == 'confirmed' and (amount is None or not ledger):
                raise ValueError('Confirm the ledger and amount before confirming a debt')
            c.execute('''UPDATE finance_obligations SET ledger_id=?,amount_cents=?,
                         outstanding_cents=?,due_date=?,status=?,updated_at=?,paid_at=? WHERE id=?''',
                      (ledger, amount, 0 if status == 'paid' else amount,
                       valid_date(form.get('due_date')), status, stamp,
                       stamp if status == 'paid' else None, ident))
        elif action == 'new-recurring':
            ledger = form.get('ledger_id')
            require_known(c, 'finance_ledgers', ledger)
            direction = form.get('direction')
            frequency = form.get('frequency')
            if direction not in ('income', 'expense') or frequency not in ('weekly', 'monthly'):
                raise ValueError('Invalid recurring item')
            name = (form.get('name') or '').strip()
            if not name or len(name) > 160:
                raise ValueError('Enter a name')
            ident = secrets.token_hex(16)
            c.execute('''INSERT INTO finance_recurring
                         (id,ledger_id,name,direction,amount_cents,frequency,next_date,active)
                         VALUES(?,?,?,?,?,?,?,1)''',
                      (ident, ledger, name, direction, cents(form.get('amount')),
                       frequency, valid_date(form.get('next_date'))))
        else:
            raise ValueError('Unknown action')
        from payment_planner import refresh
        refresh(c,stamp)


def add_month(value):
    year = value.year + (value.month == 12)
    month = 1 if value.month == 12 else value.month + 1
    last = (dt.date(year + (month == 12), 1 if month == 12 else month + 1, 1) - dt.timedelta(days=1)).day
    return dt.date(year, month, min(value.day, last))


def forecast(connection, ledger_id, opening, days=60):
    today = dt.date.today()
    end = today + dt.timedelta(days=days)
    events = []
    recurring = connection.execute(
        '''SELECT * FROM finance_recurring WHERE ledger_id=? AND active=1
           AND amount_cents IS NOT NULL AND next_date IS NOT NULL''', (ledger_id,)).fetchall()
    for item in recurring:
        date = dt.date.fromisoformat(item['next_date'])
        while date <= end:
            if date >= today:
                sign = 1 if item['direction'] == 'income' else -1
                events.append((date, sign * item['amount_cents'], item['name']))
            date = date + dt.timedelta(days=7) if item['frequency'] == 'weekly' else add_month(date)
    obligations = connection.execute(
        '''SELECT * FROM finance_obligations WHERE ledger_id=? AND status='confirmed'
           AND outstanding_cents IS NOT NULL AND due_date IS NOT NULL''', (ledger_id,)).fetchall()
    for item in obligations:
        date = dt.date.fromisoformat(item['due_date'])
        if date <= end:
            events.append((max(date, today), -item['outstanding_cents'], item['creditor']))
    balance = opening
    result = []
    for date, amount, label in sorted(events, key=lambda event: (event[0], -event[1])):
        balance += amount
        result.append((date, label, amount, balance))
    return result


def page_data():
    with db() as c:
        ledgers = [dict(row) for row in c.execute('SELECT * FROM finance_ledgers ORDER BY kind DESC,name')]
        accounts = [dict(row) for row in c.execute('SELECT * FROM finance_accounts WHERE active=1 ORDER BY name')]
        recurring = [dict(row) for row in c.execute('SELECT * FROM finance_recurring ORDER BY direction DESC,name')]
        obligations = [dict(row) for row in c.execute(
            '''SELECT o.*,m.drive_id FROM finance_obligations o LEFT JOIN mail m ON m.id=o.mail_id
               WHERE o.mail_id IS NULL OR o.mail_id NOT IN (SELECT mail_id FROM finance_case_mail)
               ORDER BY CASE o.status WHEN 'review' THEN 0 WHEN 'confirmed' THEN 1 ELSE 2 END,
               COALESCE(o.due_date,'9999-12-31'),o.created_at DESC''')]
        forecasts = {}
        for ledger in ledgers:
            values = [a['balance_cents'] for a in accounts if a['ledger_id'] == ledger['id']]
            opening = sum(value for value in values if value is not None)
            forecasts[ledger['id']] = forecast(c, ledger['id'], opening)
        if c.execute('SELECT 1 FROM finance_plan_baseline').fetchone():
            from planning import forecast as plan_forecast
            plan=plan_forecast(c,days=60)
            forecasts={l['id']:[(r['date'],r['name'],r['amount'],r['balance'])
                       for r in plan['rows'] if r['ledger']==l['id']] for l in ledgers}
    return ledgers, accounts, recurring, obligations, forecasts


def shell(content, title='Financial control'):
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>
    <style>{STYLES}</style></head><body>{content}</body></html>'''


def login_page(error=''):
    note = f'<p class="error">{esc(error)}</p>' if error else ''
    return shell(f'''<main class="login"><p class="eyebrow">POSTBODE CONTROL</p>
      <h1>Private financial dashboard</h1><p>Sign in to review obligations and cashflow.</p>{note}
      <form method="post" action="/dashboard/login"><label>Password
      <input type="password" name="password" autocomplete="current-password" required autofocus></label>
      <button type="submit">Open dashboard</button></form></main>''', 'Sign in')


def dashboard_page(token, notice=''):
    from finance_sync import panel as gmail_panel
    from planning import panel as planning_panel
    from cases import panel as cases_panel
    ledgers, accounts, recurring, obligations, forecasts = page_data()
    token_field = f'<input type="hidden" name="csrf" value="{csrf(token)}">'
    ledger_options = '<option value="">Needs assignment</option>' + ''.join(
        f'<option value="{esc(row["id"])}">{esc(row["name"])}</option>' for row in ledgers)
    review = [row for row in obligations if row['status'] == 'review']
    confirmed = [row for row in obligations if row['status'] == 'confirmed']
    cards = []
    for ledger in ledgers:
        own = [a for a in accounts if a['ledger_id'] == ledger['id']]
        known = [a['balance_cents'] for a in own if a['balance_cents'] is not None]
        incomplete = len(known) != len(own)
        opening = sum(known)
        due = sum(row['outstanding_cents'] or 0 for row in confirmed if row['ledger_id'] == ledger['id'])
        projected = forecasts[ledger['id']][-1][3] if forecasts[ledger['id']] else opening
        cards.append(f'''<article class="metric"><span>{esc(ledger['name'])}</span>
          <strong>{money(opening)}</strong><small>{'Balance incomplete' if incomplete else 'Current balance'}</small>
          <div><b>{money(due)}</b> confirmed outstanding</div><div><b>{money(projected)}</b> projected in 60 days</div></article>''')
    review_rows = ''.join(obligation_form(row, token_field, ledger_options, ledgers) for row in review)
    confirmed_rows = ''.join(obligation_form(row, token_field, ledger_options, ledgers) for row in confirmed)
    account_forms = ''.join(account_form(row, token_field, ledgers) for row in accounts)
    recurring_forms = ''.join(recurring_form(row, token_field, ledgers) for row in recurring)
    forecast_rows = []
    for ledger in ledgers:
        for date, label, amount, balance in forecasts[ledger['id']][:20]:
            forecast_rows.append(f'<tr><td>{esc(date)}</td><td>{esc(ledger["name"])}</td><td>{esc(label)}</td><td class="num {"in" if amount >= 0 else "out"}">{money(amount)}</td><td class="num">{money(balance)}</td></tr>')
    notice_html = f'<div class="notice">{esc(notice)}</div>' if notice else ''
    return shell(f'''<header><div><p class="eyebrow">POSTBODE CONTROL</p><h1>Money overview</h1></div>
      <form method="post" action="/dashboard/logout">{token_field}<button class="quiet">Sign out</button></form></header>
      <main>{notice_html}<nav><a href="#overview">Overview</a><a href="#review">Review <b>{len(review)}</b></a>
      <a href="#planning">Payment planning</a><a href="#cases">Cases</a><a href="#obligations">Debts</a><a href="#gmail">Gmail</a><a href="#accounts">Accounts</a><a href="#recurring">Recurring</a></nav>
      {planning_panel(token_field)}
      <section id="overview"><div class="section-title"><div><p class="eyebrow">SEPARATE LEDGERS</p><h2>Available cash and exposure</h2></div><p>Forecasts depend on current balances, known dates and recorded arrangements. Missing inputs are flagged below.</p></div>
      <div class="metrics">{''.join(cards)}</div></section>
      <section id="review"><div class="section-title"><div><p class="eyebrow">INCOMING MAIL</p><h2>Needs review</h2></div><p>Approve, correct or dismiss each proposed obligation.</p></div>
      <div class="stack">{review_rows or '<div class="empty">No mail-derived obligations need review.</div>'}</div></section>
      <section id="obligations"><div class="section-title"><div><p class="eyebrow">COMMITTED</p><h2>Outstanding debts</h2></div></div>
      <div class="stack">{confirmed_rows or '<div class="empty">No confirmed debts.</div>'}</div></section>
      <section><div class="section-title"><div><p class="eyebrow">NEXT 60 DAYS</p><h2>Cashflow schedule</h2></div></div>
      <div class="table-wrap"><table><thead><tr><th>Date</th><th>Ledger</th><th>Item</th><th>Movement</th><th>Balance</th></tr></thead>
      <tbody>{''.join(forecast_rows) or '<tr><td colspan="5">Add balances, amounts and next dates to generate a forecast.</td></tr>'}</tbody></table></div></section>
      {gmail_panel(token_field, ledger_options)}
      {cases_panel(token_field)}
      <section id="accounts"><div class="section-title"><div><p class="eyebrow">OPENING POSITION</p><h2>Bank balances</h2></div><p>Update these whenever you reconcile the dashboard.</p></div>
      <div class="grid">{account_forms}</div></section>
      <section id="recurring"><div class="section-title"><div><p class="eyebrow">PLANNED</p><h2>Recurring income and fixed costs</h2></div></div>
      <div class="grid">{recurring_forms}</div>{new_recurring(token_field, ledger_options)}</section>
      </main>''')


def ledger_name(ident, ledgers):
    return next((row['name'] for row in ledgers if row['id'] == ident), 'Unassigned')


def obligation_form(row, token_field, ledger_options, ledgers):
    options = ledger_options.replace(f'value="{esc(row["ledger_id"])}"', f'value="{esc(row["ledger_id"])}" selected') if row['ledger_id'] else ledger_options
    link = f'<a target="_blank" rel="noopener" href="https://drive.google.com/file/d/{esc(row["drive_id"])}/view">Open PDF</a>' if row.get('drive_id') else ''
    return f'''<form class="obligation" method="post" action="/dashboard/action">{token_field}<input type="hidden" name="action" value="obligation"><input type="hidden" name="id" value="{esc(row['id'])}">
      <div class="obligation-head"><div><small>{esc(ledger_name(row['ledger_id'], ledgers))}</small><h3>{esc(row['creditor'])}</h3><p>{esc(row['description'])}</p></div><strong>{money(row['outstanding_cents'])}</strong></div>
      <div class="fields"><label>Ledger<select name="ledger_id">{options}</select></label><label>Amount<input name="amount" inputmode="decimal" value="{amount_input(row['amount_cents'])}"></label><label>Due date<input type="date" name="due_date" value="{esc(row['due_date'])}"></label></div>
      <div class="actions">{link}<button name="status" value="confirmed">Confirm</button><button class="quiet" name="status" value="paid">Mark paid</button><button class="danger" name="status" value="dismissed">Dismiss</button></div></form>'''


def account_form(row, token_field, ledgers):
    return f'''<form class="panel" method="post" action="/dashboard/action">{token_field}<input type="hidden" name="action" value="account"><input type="hidden" name="id" value="{esc(row['id'])}">
      <small>{esc(ledger_name(row['ledger_id'], ledgers))}</small><h3>{esc(row['name'])}</h3>
      <label>Current balance<input name="amount" inputmode="decimal" value="{amount_input(row['balance_cents'])}" placeholder="0,00"></label><button>Update balance</button></form>'''


def recurring_form(row, token_field, ledgers):
    return f'''<form class="panel" method="post" action="/dashboard/action">{token_field}<input type="hidden" name="action" value="recurring"><input type="hidden" name="id" value="{esc(row['id'])}">
      <small>{esc(ledger_name(row['ledger_id'], ledgers))} · {esc(row['direction'])} · {esc(row['frequency'])}</small><h3>{esc(row['name'])}</h3>
      <div class="fields"><label>Amount<input name="amount" inputmode="decimal" value="{amount_input(row['amount_cents'])}" placeholder="0,00"></label><label>Next date<input type="date" name="next_date" value="{esc(row['next_date'])}"></label></div>
      <label class="check"><input type="checkbox" name="active" {'checked' if row['active'] else ''}> Active</label><button>Save schedule</button></form>'''


def new_recurring(token_field, ledger_options):
    return f'''<details><summary>Add recurring income or fixed cost</summary><form class="panel add" method="post" action="/dashboard/action">{token_field}<input type="hidden" name="action" value="new-recurring">
      <div class="fields"><label>Name<input name="name" required></label><label>Ledger<select name="ledger_id" required>{ledger_options}</select></label>
      <label>Type<select name="direction"><option value="expense">Fixed cost</option><option value="income">Income</option></select></label>
      <label>Frequency<select name="frequency"><option value="monthly">Monthly</option><option value="weekly">Weekly</option></select></label>
      <label>Amount<input name="amount" inputmode="decimal"></label><label>Next date<input type="date" name="next_date"></label></div><button>Add item</button></form></details>'''


def handle(environ, start_response):
    path = environ.get('PATH_INFO', '')
    if not path.startswith('/dashboard'):
        return None
    if not signing_key():
        return response(start_response, '503 Service Unavailable', shell('<main class="login"><h1>Dashboard not configured</h1></main>'))
    method = environ.get('REQUEST_METHOD', 'GET')
    current = session(environ)
    if path == '/dashboard/login':
        if method == 'GET':
            return response(start_response, '200 OK', login_page())
        if method == 'POST':
            try:
                form = read_form(environ)
            except ValueError:
                return response(start_response, '400 Bad Request', login_page('Invalid request'))
            if not hmac.compare_digest(form.get('password', ''), os.environ['DASHBOARD_PASSWORD']):
                return response(start_response, '401 Unauthorized', login_page('Incorrect password'))
            token = new_session()
            cookie = f'{SESSION_COOKIE}={token}; Path=/dashboard; Max-Age=43200; Secure; HttpOnly; SameSite=Strict'
            return response(start_response, '303 See Other', b'', [('Location', '/dashboard'), ('Set-Cookie', cookie)])
    if not current:
        return response(start_response, '303 See Other', b'', [('Location', '/dashboard/login')])
    if method == 'POST':
        try:
            form = read_form(environ)
            if not hmac.compare_digest(form.get('csrf', ''), csrf(current)):
                raise ValueError('Invalid session')
            if path == '/dashboard/logout':
                return response(start_response, '303 See Other', b'', [('Location', '/dashboard/login'),
                    ('Set-Cookie', f'{SESSION_COOKIE}=; Path=/dashboard; Max-Age=0; Secure; HttpOnly; SameSite=Strict')])
            if path != '/dashboard/action':
                raise ValueError('Unknown request')
            apply_action(form)
            return response(start_response, '303 See Other', b'', [('Location', '/dashboard?saved=1')])
        except (ValueError, KeyError) as exc:
            return response(start_response, '400 Bad Request', dashboard_page(current, str(exc)))
    if method == 'GET' and path == '/dashboard':
        notice = 'Saved.' if urllib.parse.parse_qs(environ.get('QUERY_STRING', '')).get('saved') else ''
        return response(start_response, '200 OK', dashboard_page(current, notice))
    return response(start_response, '404 Not Found', shell('<main class="login"><h1>Not found</h1></main>'))


STYLES = '''
:root{color-scheme:light;--ink:#17202b;--muted:#637083;--line:#d9e0e8;--paper:#fff;--bg:#f1f4f7;--nav:#142b35;--cyan:#18a7c5;--red:#b22b35;--green:#087f5b}*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 Inter,ui-sans-serif,system-ui,sans-serif}header{position:sticky;top:0;z-index:2;display:flex;align-items:center;justify-content:space-between;padding:18px max(24px,calc((100vw - 1380px)/2));background:var(--nav);color:#fff;box-shadow:0 8px 24px #17202b22}h1,h2,h3,p{margin-top:0}h1{font-size:1.75rem;margin-bottom:0}h2{font-size:1.6rem;margin-bottom:0}h3{font-size:1.08rem;margin-bottom:4px}.eyebrow{font-weight:800;font-size:.75rem;letter-spacing:.12em;color:var(--cyan);margin-bottom:4px}main{max-width:1380px;margin:auto;padding:0 24px 80px}nav{display:flex;gap:8px;overflow:auto;padding:18px 0;position:sticky;top:94px;background:var(--bg);z-index:1}nav a{white-space:nowrap;text-decoration:none;color:var(--ink);padding:9px 13px;border:1px solid var(--line);border-radius:999px;background:#fff}nav b{background:var(--red);color:#fff;border-radius:99px;padding:1px 6px;margin-left:3px}section{scroll-margin-top:160px;margin:12px 0 38px}.section-title{display:flex;align-items:end;justify-content:space-between;gap:20px;margin:0 0 14px}.section-title>p{color:var(--muted);margin-bottom:2px}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.metric,.panel,.obligation,.empty,details{background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:18px;box-shadow:0 2px 8px #17202b0a}.metric span,.panel small,.obligation small{display:block;color:var(--muted);font-size:.82rem;font-weight:700}.metric strong{display:block;font-size:1.9rem;margin:8px 0 0}.metric small{display:block;color:var(--muted);margin-bottom:18px}.metric div{border-top:1px solid var(--line);padding-top:9px;margin-top:9px;color:var(--muted);font-size:.88rem}.metric b{color:var(--ink)}.stack{display:grid;gap:10px}.obligation-head{display:flex;justify-content:space-between;gap:24px}.obligation-head strong{font-size:1.35rem;white-space:nowrap}.obligation p{color:var(--muted);margin-bottom:12px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.fields{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}label{display:grid;gap:5px;color:var(--muted);font-size:.82rem;font-weight:700}input,select{width:100%;font:inherit;color:var(--ink);padding:10px 11px;border:1px solid #bac5d2;border-radius:8px;background:#fff}input:focus,select:focus{outline:3px solid #18a7c544;border-color:var(--cyan)}button{font:inherit;font-weight:750;border:0;border-radius:8px;background:var(--nav);color:#fff;padding:10px 14px;cursor:pointer}.quiet{background:#fff;color:var(--ink);border:1px solid var(--line)}.danger{background:#fff;color:var(--red);border:1px solid #e6b7bc}.actions{display:flex;justify-content:flex-end;align-items:center;gap:8px;margin-top:12px}.actions a{margin-right:auto;color:#006b85}.panel button{margin-top:12px}.check{display:flex;align-items:center;margin-top:10px}.check input{width:auto}.table-wrap{overflow:auto;background:#fff;border:1px solid var(--line);border-radius:14px}table{width:100%;border-collapse:collapse;min-width:720px}th,td{text-align:left;padding:12px 14px;border-bottom:1px solid var(--line)}th{font-size:.78rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}.num{text-align:right;font-variant-numeric:tabular-nums}.in{color:var(--green)}.out{color:var(--red)}details{margin-top:12px}summary{cursor:pointer;font-weight:800}.add{box-shadow:none;margin-top:14px}.notice{background:#d9f5e9;color:#075e45;border:1px solid #a8dfca;border-radius:10px;padding:10px 14px;margin-bottom:12px}.error{color:var(--red)}.login{max-width:460px;margin:10vh auto;background:#fff;padding:36px;border:1px solid var(--line);border-radius:16px;box-shadow:0 20px 60px #17202b1f}.login form{display:grid;gap:14px}.login button{margin-top:8px}@media(max-width:1000px){.metrics{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:650px){header{padding:14px 16px}main{padding:0 14px 60px}nav{top:82px}.metrics,.grid,.fields{grid-template-columns:1fr}.section-title{display:block}.obligation-head{display:block}.obligation-head strong{display:block;margin:10px 0}.actions{flex-wrap:wrap;justify-content:flex-start}.actions a{width:100%;margin-bottom:4px}.metric strong{font-size:1.55rem}}
'''
