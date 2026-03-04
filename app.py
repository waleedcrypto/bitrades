import os
import re
import json
import sqlite3
import hashlib
import base64
import io
import time
import math
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   session, jsonify, g, flash)

app = Flask(__name__)
app.secret_key = 'bitrade_ultra_secure_key_2024_xK9mP2'

DB_PATH = 'bitrade.db'
DEPOSIT_ADDRESS = 'TCxzW5RzSdzHxzaVXFcYeNH5VWdf6oGcgL'
ADMIN_WALLET = 'TDo2pnvaAZRNzmL1a47SmYbLcBftJF7pYa'
REFERRAL_COMMISSION = 0.02  # 2%
PLAN_DURATION_DAYS = 20
MIN_WITHDRAWAL = 20
MIN_REFERRALS_FOR_WITHDRAWAL = 5

PLANS = {
    'basic':   {'name': 'Basic Plan',   'min_deposit': 10,  'daily_rate': 0.045, 'color': '#00d4ff'},
    'premium': {'name': 'Premium Plan', 'min_deposit': 50,  'daily_rate': 0.055, 'color': '#a855f7'},
    'elite':   {'name': 'Elite Plan',   'min_deposit': 100, 'daily_rate': 0.065, 'color': '#f59e0b'},
}

# ─── DB ────────────────────────────────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db: db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT UNIQUE NOT NULL,
            referral_code TEXT UNIQUE NOT NULL,
            referred_by TEXT,
            balance REAL DEFAULT 0,
            referral_earnings REAL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_wallet TEXT NOT NULL,
            plan_key TEXT NOT NULL,
            amount REAL NOT NULL,
            txid TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            submitted_at TEXT NOT NULL,
            approved_at TEXT,
            admin_note TEXT
        );
        CREATE TABLE IF NOT EXISTS plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_wallet TEXT NOT NULL,
            plan_key TEXT NOT NULL,
            amount REAL NOT NULL,
            daily_rate REAL NOT NULL,
            start_time TEXT,
            end_time TEXT,
            status TEXT DEFAULT 'pending',
            deposit_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_wallet TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            submitted_at TEXT NOT NULL,
            processed_at TEXT,
            admin_note TEXT
        );
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_wallet TEXT NOT NULL,
            referred_wallet TEXT NOT NULL,
            commission REAL DEFAULT 0,
            deposit_id INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admin_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            details TEXT,
            performed_at TEXT NOT NULL
        );
        ''')
        db.commit()

# ─── HELPERS ───────────────────────────────────────────────────────────────────

def is_valid_trc20(addr):
    return bool(re.match(r'^T[1-9A-HJ-NP-Za-km-z]{33}$', addr))

def gen_referral_code(wallet):
    return hashlib.md5(wallet.encode()).hexdigest()[:8].upper()

def now_str():
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

def make_qr_svg(data):
    """Generate a simple QR-like visual SVG (grid pattern based on hash)"""
    size = 21
    h = hashlib.sha256(data.encode()).hexdigest()
    bits = bin(int(h, 16))[2:].zfill(256)
    cells = []
    for i in range(size):
        for j in range(size):
            # Fixed finder patterns
            in_finder = (
                (i < 7 and j < 7) or
                (i < 7 and j >= size-7) or
                (i >= size-7 and j < 7)
            )
            if in_finder:
                # Draw finder pattern borders
                if (i in [0,6] and j < 7) or (j in [0,6] and i < 7):
                    cells.append(f'<rect x="{j*10}" y="{i*10}" width="10" height="10" fill="#000"/>')
                elif (i in [1,5] and 1<=j<=5) or (j in [1,5] and 1<=i<=5):
                    cells.append(f'<rect x="{j*10}" y="{i*10}" width="10" height="10" fill="#fff"/>')
                elif 2<=i<=4 and 2<=j<=4:
                    cells.append(f'<rect x="{j*10}" y="{i*10}" width="10" height="10" fill="#000"/>')
            else:
                idx = (i * size + j) % len(bits)
                if bits[idx] == '1':
                    cells.append(f'<rect x="{j*10}" y="{i*10}" width="10" height="10" fill="#000"/>')
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="210" height="210" viewBox="0 0 210 210">
<rect width="210" height="210" fill="white"/>
{''.join(cells)}
</svg>'''
    return base64.b64encode(svg.encode()).decode()

def calc_earnings(plan_row):
    if not plan_row['start_time'] or plan_row['status'] == 'pending':
        return 0, 0
    start = datetime.strptime(plan_row['start_time'], '%Y-%m-%d %H:%M:%S')
    now = datetime.utcnow()
    end = datetime.strptime(plan_row['end_time'], '%Y-%m-%d %H:%M:%S') if plan_row['end_time'] else start + timedelta(days=PLAN_DURATION_DAYS)
    effective = min(now, end)
    days_elapsed = max(0, (effective - start).total_seconds() / 86400)
    daily = plan_row['amount'] * plan_row['daily_rate']
    total = daily * min(days_elapsed, PLAN_DURATION_DAYS)
    return round(daily, 4), round(total, 4)

def get_user(wallet):
    db = get_db()
    return db.execute('SELECT * FROM users WHERE wallet=?', (wallet,)).fetchone()

def log_admin(action, details=''):
    db = get_db()
    db.execute('INSERT INTO admin_logs (action,details,performed_at) VALUES (?,?,?)',
               (action, details, now_str()))
    db.commit()

# ─── AUTH DECORATORS ───────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'wallet' not in session:
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('wallet') != ADMIN_WALLET:
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated

# ─── ROUTES: AUTH ──────────────────────────────────────────────────────────────

@app.route('/', methods=['GET'])
def index():
    if 'wallet' in session:
        if session['wallet'] == ADMIN_WALLET:
            return redirect(url_for('admin_dashboard'))
        return redirect(url_for('dashboard'))
    return render_template('auth.html')

@app.route('/auth', methods=['POST'])
def auth():
    wallet = request.form.get('wallet', '').strip()
    if not is_valid_trc20(wallet):
        return render_template('auth.html', error='Invalid TRC20 wallet address format.')
    db = get_db()
    user = get_user(wallet)
    if not user:
        ref_code = gen_referral_code(wallet)
        referred_by = request.form.get('ref', '').strip() or None
        if referred_by:
            referrer = db.execute('SELECT wallet FROM users WHERE referral_code=?', (referred_by,)).fetchone()
            referred_by = referrer['wallet'] if referrer else None
        db.execute('INSERT INTO users (wallet,referral_code,referred_by,created_at) VALUES (?,?,?,?)',
                   (wallet, ref_code, referred_by, now_str()))
        db.commit()
    session['wallet'] = wallet
    if wallet == ADMIN_WALLET:
        return redirect(url_for('admin_dashboard'))
    return redirect(url_for('dashboard'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# ─── ROUTES: USER DASHBOARD ────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    wallet = session['wallet']
    db = get_db()
    user = get_user(wallet)
    if not user:
        ref_code = gen_referral_code(wallet)
        db.execute('INSERT INTO users (wallet,referral_code,created_at) VALUES (?,?,?)',
                   (wallet, ref_code, now_str()))
        db.commit()
        user = get_user(wallet)
    active_plans = db.execute(
        "SELECT * FROM plans WHERE user_wallet=? AND status='active' ORDER BY id DESC", (wallet,)).fetchall()
    completed_plans = db.execute(
        "SELECT * FROM plans WHERE user_wallet=? AND status='completed' ORDER BY id DESC", (wallet,)).fetchall()
    deposits = db.execute(
        "SELECT * FROM deposits WHERE user_wallet=? ORDER BY id DESC LIMIT 10", (wallet,)).fetchall()
    withdrawals = db.execute(
        "SELECT * FROM withdrawals WHERE user_wallet=? ORDER BY id DESC LIMIT 10", (wallet,)).fetchall()
    referrals = db.execute(
        "SELECT * FROM referrals WHERE referrer_wallet=? ORDER BY id DESC", (wallet,)).fetchall()
    referred_wallets = db.execute(
        "SELECT wallet FROM users WHERE referred_by=?", (wallet,)).fetchall()
    active_ref_count = 0
    for ru in referred_wallets:
        has_active = db.execute(
            "SELECT id FROM plans WHERE user_wallet=? AND status='active'", (ru['wallet'],)).fetchone()
        if has_active:
            active_ref_count += 1
    plan_data = []
    for p in active_plans:
        daily, total = calc_earnings(p)
        start = datetime.strptime(p['start_time'], '%Y-%m-%d %H:%M:%S')
        end = datetime.strptime(p['end_time'], '%Y-%m-%d %H:%M:%S')
        now = datetime.utcnow()
        remaining_secs = max(0, int((end - now).total_seconds()))
        plan_info = PLANS.get(p['plan_key'], {})
        plan_data.append({
            'id': p['id'],
            'plan_key': p['plan_key'],
            'plan_name': plan_info.get('name', p['plan_key']),
            'color': plan_info.get('color', '#00d4ff'),
            'amount': p['amount'],
            'daily_rate': p['daily_rate'],
            'daily_earnings': daily,
            'total_earnings': total,
            'start_time': p['start_time'],
            'end_time': p['end_time'],
            'remaining_secs': remaining_secs,
            'status': p['status'],
        })
    ref_link = request.host_url + '?ref=' + user['referral_code']
    return render_template('dashboard.html',
        user=user,
        active_plans=plan_data,
        completed_plans=completed_plans,
        deposits=deposits,
        withdrawals=withdrawals,
        referrals=referrals,
        active_ref_count=active_ref_count,
        total_referrals=len(referred_wallets),
        ref_link=ref_link,
        plans=PLANS,
        deposit_address=DEPOSIT_ADDRESS,
    )

@app.route('/deposit', methods=['GET', 'POST'])
@login_required
def deposit():
    wallet = session['wallet']
    if request.method == 'POST':
        plan_key = request.form.get('plan_key')
        amount = float(request.form.get('amount', 0))
        txid = request.form.get('txid', '').strip()
        if plan_key not in PLANS:
            return jsonify({'error': 'Invalid plan'}), 400
        if amount < PLANS[plan_key]['min_deposit']:
            return jsonify({'error': f"Minimum deposit is ${PLANS[plan_key]['min_deposit']}"}), 400
        if not txid:
            return jsonify({'error': 'TXID required'}), 400
        db = get_db()
        db.execute('INSERT INTO deposits (user_wallet,plan_key,amount,txid,submitted_at) VALUES (?,?,?,?,?)',
                   (wallet, plan_key, amount, txid, now_str()))
        db.commit()
        return jsonify({'success': True, 'message': 'Deposit submitted! Awaiting admin approval (avg 15 min).'})
    plan_key = request.args.get('plan', 'basic')
    if plan_key not in PLANS:
        plan_key = 'basic'
    qr_data = make_qr_svg(DEPOSIT_ADDRESS)
    return render_template('deposit.html',
        plan_key=plan_key,
        plan=PLANS[plan_key],
        plans=PLANS,
        deposit_address=DEPOSIT_ADDRESS,
        qr_data=qr_data,
    )

@app.route('/withdraw', methods=['POST'])
@login_required
def withdraw():
    wallet = session['wallet']
    amount = float(request.form.get('amount', 0))
    db = get_db()
    user = get_user(wallet)
    # Check conditions
    has_completed = db.execute(
        "SELECT id FROM plans WHERE user_wallet=? AND status='completed'", (wallet,)).fetchone()
    referred_wallets = db.execute(
        "SELECT wallet FROM users WHERE referred_by=?", (wallet,)).fetchall()
    active_ref_count = 0
    for ru in referred_wallets:
        has_active = db.execute(
            "SELECT id FROM plans WHERE user_wallet=? AND status='active'", (ru['wallet'],)).fetchone()
        if has_active:
            active_ref_count += 1
    if not has_completed or active_ref_count < MIN_REFERRALS_FOR_WITHDRAWAL:
        return jsonify({'eligible': False,
            'message': 'Withdrawal available after plan completion and required referral criteria.'})
    if amount < MIN_WITHDRAWAL:
        return jsonify({'eligible': False, 'message': f'Minimum withdrawal is ${MIN_WITHDRAWAL}.'})
    if amount > user['balance']:
        return jsonify({'eligible': False, 'message': 'Insufficient balance.'})
    db.execute('INSERT INTO withdrawals (user_wallet,amount,submitted_at) VALUES (?,?,?)',
               (wallet, amount, now_str()))
    db.execute('UPDATE users SET balance=balance-? WHERE wallet=?', (amount, wallet))
    db.commit()
    return jsonify({'eligible': True, 'success': True,
        'message': 'Withdrawal request submitted! Processing in 15 min – 24 hrs.'})

@app.route('/api/plan_status')
@login_required
def plan_status():
    wallet = session['wallet']
    db = get_db()
    plans_rows = db.execute(
        "SELECT * FROM plans WHERE user_wallet=? AND status='active'", (wallet,)).fetchall()
    result = []
    for p in plans_rows:
        daily, total = calc_earnings(p)
        end = datetime.strptime(p['end_time'], '%Y-%m-%d %H:%M:%S')
        remaining = max(0, int((end - datetime.utcnow()).total_seconds()))
        if remaining == 0 and p['status'] == 'active':
            db.execute("UPDATE plans SET status='completed' WHERE id=?", (p['id'],))
            db.execute('UPDATE users SET balance=balance+? WHERE wallet=?', (total + p['amount'], wallet))
            db.commit()
        result.append({'id': p['id'], 'daily': daily, 'total': total, 'remaining': remaining})
    return jsonify(result)

# ─── ROUTES: ADMIN ─────────────────────────────────────────────────────────────

@app.route('/admin')
@admin_required
def admin_dashboard():
    db = get_db()
    total_users = db.execute('SELECT COUNT(*) as c FROM users').fetchone()['c']
    total_deposits = db.execute("SELECT COALESCE(SUM(amount),0) as s FROM deposits WHERE status='approved'").fetchone()['s']
    total_withdrawals = db.execute("SELECT COALESCE(SUM(amount),0) as s FROM withdrawals WHERE status='approved'").fetchone()['s']
    pending_deposits = db.execute("SELECT COUNT(*) as c FROM deposits WHERE status='pending'").fetchone()['c']
    pending_withdrawals = db.execute("SELECT COUNT(*) as c FROM withdrawals WHERE status='pending'").fetchone()['c']
    active_plans = db.execute("SELECT COUNT(*) as c FROM plans WHERE status='active'").fetchone()['c']
    return render_template('admin.html',
        total_users=total_users,
        total_deposits=total_deposits,
        total_withdrawals=total_withdrawals,
        pending_deposits=pending_deposits,
        pending_withdrawals=pending_withdrawals,
        active_plans=active_plans,
    )

@app.route('/admin/deposits')
@admin_required
def admin_deposits():
    db = get_db()
    deposits = db.execute('SELECT * FROM deposits ORDER BY id DESC').fetchall()
    return render_template('admin_deposits.html', deposits=deposits, plans=PLANS)

@app.route('/admin/deposit/approve/<int:dep_id>', methods=['POST'])
@admin_required
def admin_approve_deposit(dep_id):
    db = get_db()
    dep = db.execute('SELECT * FROM deposits WHERE id=?', (dep_id,)).fetchone()
    if not dep or dep['status'] != 'pending':
        return jsonify({'error': 'Invalid deposit'}), 400
    now = now_str()
    end_time = (datetime.utcnow() + timedelta(days=PLAN_DURATION_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
    db.execute("UPDATE deposits SET status='approved', approved_at=? WHERE id=?", (now, dep_id))
    plan_info = PLANS.get(dep['plan_key'], PLANS['basic'])
    db.execute('INSERT INTO plans (user_wallet,plan_key,amount,daily_rate,start_time,end_time,status,deposit_id) VALUES (?,?,?,?,?,?,?,?)',
               (dep['user_wallet'], dep['plan_key'], dep['amount'], plan_info['daily_rate'],
                now, end_time, 'active', dep_id))
    # Handle referral commission
    user = get_user(dep['user_wallet'])
    if user and user['referred_by']:
        commission = round(dep['amount'] * REFERRAL_COMMISSION, 4)
        db.execute('UPDATE users SET balance=balance+?, referral_earnings=referral_earnings+? WHERE wallet=?',
                   (commission, commission, user['referred_by']))
        db.execute('INSERT INTO referrals (referrer_wallet,referred_wallet,commission,deposit_id,created_at) VALUES (?,?,?,?,?)',
                   (user['referred_by'], dep['user_wallet'], commission, dep_id, now))
    db.commit()
    log_admin('APPROVE_DEPOSIT', f'Deposit ID {dep_id} approved for {dep["user_wallet"]} - ${dep["amount"]}')
    return jsonify({'success': True})

@app.route('/admin/deposit/reject/<int:dep_id>', methods=['POST'])
@admin_required
def admin_reject_deposit(dep_id):
    db = get_db()
    note = request.form.get('note', '')
    db.execute("UPDATE deposits SET status='rejected', admin_note=? WHERE id=?", (note, dep_id))
    db.commit()
    log_admin('REJECT_DEPOSIT', f'Deposit ID {dep_id} rejected. Note: {note}')
    return jsonify({'success': True})

@app.route('/admin/withdrawals')
@admin_required
def admin_withdrawals():
    db = get_db()
    withdrawals = db.execute('SELECT * FROM withdrawals ORDER BY id DESC').fetchall()
    return render_template('admin_withdrawals.html', withdrawals=withdrawals)

@app.route('/admin/withdrawal/approve/<int:wid>', methods=['POST'])
@admin_required
def admin_approve_withdrawal(wid):
    db = get_db()
    db.execute("UPDATE withdrawals SET status='approved', processed_at=? WHERE id=?", (now_str(), wid))
    db.commit()
    log_admin('APPROVE_WITHDRAWAL', f'Withdrawal ID {wid} approved')
    return jsonify({'success': True})

@app.route('/admin/withdrawal/reject/<int:wid>', methods=['POST'])
@admin_required
def admin_reject_withdrawal(wid):
    db = get_db()
    w = db.execute('SELECT * FROM withdrawals WHERE id=?', (wid,)).fetchone()
    if w:
        db.execute('UPDATE users SET balance=balance+? WHERE wallet=?', (w['amount'], w['user_wallet']))
    note = request.form.get('note', '')
    db.execute("UPDATE withdrawals SET status='rejected', admin_note=?, processed_at=? WHERE id=?",
               (note, now_str(), wid))
    db.commit()
    log_admin('REJECT_WITHDRAWAL', f'Withdrawal ID {wid} rejected. Note: {note}')
    return jsonify({'success': True})

@app.route('/admin/users')
@admin_required
def admin_users():
    db = get_db()
    users = db.execute('SELECT * FROM users ORDER BY id DESC').fetchall()
    return render_template('admin_users.html', users=users)

@app.route('/admin/user/balance', methods=['POST'])
@admin_required
def admin_adjust_balance():
    wallet = request.form.get('wallet')
    amount = float(request.form.get('amount', 0))
    db = get_db()
    db.execute('UPDATE users SET balance=balance+? WHERE wallet=?', (amount, wallet))
    db.commit()
    log_admin('ADJUST_BALANCE', f'Balance adjusted by ${amount} for {wallet}')
    return jsonify({'success': True})

@app.route('/admin/plans')
@admin_required
def admin_plans():
    db = get_db()
    plans = db.execute('SELECT * FROM plans ORDER BY id DESC').fetchall()
    return render_template('admin_plans.html', plans=plans, plan_info=PLANS)

@app.route('/admin/plan/complete/<int:pid>', methods=['POST'])
@admin_required
def admin_complete_plan(pid):
    db = get_db()
    p = db.execute('SELECT * FROM plans WHERE id=?', (pid,)).fetchone()
    if p:
        _, total = calc_earnings(p)
        db.execute("UPDATE plans SET status='completed' WHERE id=?", (pid,))
        db.execute('UPDATE users SET balance=balance+? WHERE wallet=?', (total + p['amount'], p['user_wallet']))
        db.commit()
        log_admin('COMPLETE_PLAN', f'Plan ID {pid} manually completed for {p["user_wallet"]}')
    return jsonify({'success': True})

@app.route('/admin/logs')
@admin_required
def admin_logs():
    db = get_db()
    logs = db.execute('SELECT * FROM admin_logs ORDER BY id DESC LIMIT 200').fetchall()
    return render_template('admin_logs.html', logs=logs)

@app.route('/admin/referrals')
@admin_required
def admin_referrals():
    db = get_db()
    referrals = db.execute('SELECT * FROM referrals ORDER BY id DESC').fetchall()
    return render_template('admin_referrals.html', referrals=referrals)

if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000)
