# -*- coding: utf-8 -*-
"""
进销存 + 宅急送物流系统 (WMS + TMS)
==================================
脚手架保留: 认证 / RBAC(admin/operator/user) / 用户管理 / 错误页。
本项目扩展: 基础数据 / 采购 / 销售 / 库存三态与流水 / 盘点 / 运单轨迹 / 财务报表。

设计要点
--------
1) 库存三态: stock(账面) / available_stock(可用) / frozen_stock(冻结)
   恒等式: available_stock = stock - frozen_stock
2) 每一次库存变动都写 stock_ledger(before_qty/after_qty), 实现「信息流」可追溯。
3) 运单状态机: created -> picked -> in_transit -> delivering -> signed; exception 从任意非终态进入。
4) 拒收/退货: 回滚出库(stock+, frozen+), 销售单退回 pending, 可重新发货。
5) 金额一律整数「分」; 时间一律 datetime('now','localtime') 字符串。
"""
import os
from datetime import datetime
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, g, abort)
from werkzeug.security import generate_password_hash, check_password_hash
import db as database
from db import query, query_one, execute, transaction

# ================== 项目配置 ==================
APP_NAME = '进销存+宅急送物流'
APP_SUBTITLE = 'WMS+TMS'
PORT = 8003
NAV = [
    ('dashboard',  '仪表盘',   '/',            '📊'),
    ('products',   '商品管理', '/products',    '📦'),
    ('suppliers',  '供应商',   '/suppliers',   '🏭'),
    ('customers',  '客户',     '/customers',   '🤝'),
    ('warehouses', '仓库',     '/warehouses',  '🏬'),
    ('purchases',  '采购管理', '/purchases',   '🛒'),
    ('sales',      '销售管理', '/sales',       '🧾'),
    ('stock',      '库存台账', '/stock',       '🗃️'),
    ('ledger',     '库存流水', '/stock/ledger', '🔍'),
    ('stocktakes', '盘点管理', '/stocktakes',  '📋'),
    ('waybills',   '运单管理', '/waybills',    '🚚'),
    ('finance',    '财务报表', '/finance',     '💰'),
    ('users',      '用户管理', '/users',       '👥'),
]
ROLE_LABELS = {'admin': '管理员', 'operator': '业务操作员', 'user': '客户'}

# 状态字典(展示用)
PO_STATUS = {'pending': ('待收货', 'text-bg-warning'),
             'received': ('已入库', 'text-bg-info'),
             'paid': ('已付款', 'text-bg-success')}
SO_STATUS = {'pending': ('待出库', 'text-bg-warning'),
             'shipped': ('已发货', 'text-bg-info'),
             'finished': ('已完成', 'text-bg-success')}
WB_STATUS = {'created': ('已建单', 'text-bg-secondary'),
             'picked': ('已揽收', 'text-bg-primary'),
             'in_transit': ('运输中', 'text-bg-info'),
             'delivering': ('派送中', 'text-bg-warning'),
             'signed': ('已签收', 'text-bg-success'),
             'exception': ('异常', 'text-bg-danger')}
LEDGER_TYPE = {'in': ('入库', 'text-bg-success'),
               'out': ('出库', 'text-bg-danger'),
               'freeze': ('冻结', 'text-bg-warning'),
               'unfreeze': ('解冻', 'text-bg-info'),
               'adjust': ('盘点调整', 'text-bg-primary')}
WB_FLOW = ['created', 'picked', 'in_transit', 'delivering', 'signed']
CARRIERS = ['宅急送', '顺丰速运', '中通快递', '京东物流']
EXCEPTION_REASONS = ['拒收', '退货', '丢件', '破损']
# ==============================================

app = Flask(__name__)
app.secret_key = 'wms-tms-dev-secret'
database.init_app(app)


# ---------------- 数据模型 ----------------
def ensure_schema():
    execute('''CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        display_name TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user',
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')

    execute('''CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        category TEXT DEFAULT '通用',
        unit TEXT DEFAULT '件',
        price INTEGER NOT NULL DEFAULT 0,
        sale_price INTEGER NOT NULL DEFAULT 0,
        stock INTEGER NOT NULL DEFAULT 0,
        available_stock INTEGER NOT NULL DEFAULT 0,
        frozen_stock INTEGER NOT NULL DEFAULT 0,
        safety_stock INTEGER NOT NULL DEFAULT 0,
        warehouse_id INTEGER DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'on'
    )''')
    execute('''CREATE TABLE IF NOT EXISTS suppliers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        contact TEXT, phone TEXT,
        address TEXT,
        status TEXT NOT NULL DEFAULT 'on'
    )''')
    execute('''CREATE TABLE IF NOT EXISTS customers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        contact TEXT, phone TEXT,
        address TEXT,
        status TEXT NOT NULL DEFAULT 'on'
    )''')
    execute('''CREATE TABLE IF NOT EXISTS warehouses(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        address TEXT
    )''')
    execute('''CREATE TABLE IF NOT EXISTS purchase_orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        no TEXT UNIQUE NOT NULL,
        supplier_id INTEGER NOT NULL,
        total_amount INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        received_at TEXT, paid_at TEXT
    )''')
    execute('''CREATE TABLE IF NOT EXISTS purchase_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        po_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        qty INTEGER NOT NULL DEFAULT 0,
        price INTEGER NOT NULL DEFAULT 0
    )''')
    execute('''CREATE TABLE IF NOT EXISTS sale_orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        no TEXT UNIQUE NOT NULL,
        customer_id INTEGER NOT NULL,
        total_amount INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        shipped_at TEXT, finished_at TEXT
    )''')
    execute('''CREATE TABLE IF NOT EXISTS sale_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        so_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        qty INTEGER NOT NULL DEFAULT 0,
        price INTEGER NOT NULL DEFAULT 0
    )''')
    # 核心: 信息流留痕
    execute('''CREATE TABLE IF NOT EXISTS stock_ledger(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        warehouse_id INTEGER,
        change_qty INTEGER NOT NULL DEFAULT 0,
        before_qty INTEGER NOT NULL DEFAULT 0,
        after_qty INTEGER NOT NULL DEFAULT 0,
        type TEXT NOT NULL,
        ref_no TEXT,
        remark TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        user_id INTEGER
    )''')
    execute('''CREATE TABLE IF NOT EXISTS stocktakes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        no TEXT UNIQUE NOT NULL,
        warehouse_id INTEGER,
        status TEXT NOT NULL DEFAULT 'draft',
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        done_at TEXT
    )''')
    execute('''CREATE TABLE IF NOT EXISTS stocktake_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        take_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        book_qty INTEGER NOT NULL DEFAULT 0,
        actual_qty INTEGER NOT NULL DEFAULT 0,
        diff_qty INTEGER NOT NULL DEFAULT 0
    )''')
    execute('''CREATE TABLE IF NOT EXISTS waybills(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        no TEXT UNIQUE NOT NULL,
        sale_order_id INTEGER,
        carrier TEXT DEFAULT '宅急送',
        receiver TEXT, phone TEXT, address TEXT,
        status TEXT NOT NULL DEFAULT 'created',
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        signed_at TEXT
    )''')
    execute('''CREATE TABLE IF NOT EXISTS waybill_tracks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        waybill_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        remark TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    execute('''CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL,
        ref_type TEXT NOT NULL,
        ref_id INTEGER,
        amount INTEGER NOT NULL DEFAULT 0,
        remark TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')


# ---------------- 认证与权限 ----------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'uid' not in session:
            flash('请先登录', 'warning')
            return redirect(url_for('login', next=request.path))
        g.user = query_one('SELECT * FROM users WHERE id=?', (session['uid'],))
        if not g.user:
            session.clear()
            flash('账号已失效,请重新登录', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


def role_required(*roles):
    def deco(f):
        @wraps(f)
        @login_required
        def wrapper(*args, **kwargs):
            if g.user['role'] not in roles:
                abort(403)
            return f(*args, **kwargs)
        return wrapper
    return deco


@app.context_processor
def inject_globals():
    return {
        'APP_NAME': APP_NAME, 'APP_SUBTITLE': APP_SUBTITLE,
        'NAV': NAV, 'ROLE_LABELS': ROLE_LABELS,
        'PO_STATUS': PO_STATUS, 'SO_STATUS': SO_STATUS, 'WB_STATUS': WB_STATUS,
        'LEDGER_TYPE': LEDGER_TYPE, 'WB_FLOW': WB_FLOW,
        'CARRIERS': CARRIERS, 'EXCEPTION_REASONS': EXCEPTION_REASONS,
        'current_user': getattr(g, 'user', None),
        'request_path': request.path,
    }


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = query_one('SELECT * FROM users WHERE username=?',
                      (request.form.get('username', '').strip(),))
        if u and check_password_hash(u['password_hash'], request.form.get('password', '')):
            session['uid'] = u['id']
            flash(f'欢迎回来, {u["display_name"]}', 'success')
            return redirect(request.args.get('next') or url_for('index'))
        flash('用户名或密码错误', 'danger')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('已退出登录', 'info')
    return redirect(url_for('login'))


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        old = request.form.get('old_pwd', '')
        if not check_password_hash(g.user['password_hash'], old):
            flash('原密码错误', 'danger')
        elif len(request.form.get('new_pwd', '')) < 6:
            flash('新密码至少 6 位', 'warning')
        else:
            execute('UPDATE users SET password_hash=? WHERE id=?',
                    (generate_password_hash(request.form['new_pwd']), g.user['id']))
            flash('密码已修改', 'success')
            return redirect(url_for('index'))
    return render_template('profile.html')


# ---------------- 用户管理(admin) ----------------
@app.route('/users')
@role_required('admin')
def user_list():
    users = query('SELECT * FROM users ORDER BY id')
    return render_template('users.html', users=users)


@app.route('/users/add', methods=['POST'])
@role_required('admin')
def user_add():
    username = request.form.get('username', '').strip()
    if not username or not request.form.get('password', ''):
        flash('用户名和密码必填', 'warning')
    elif query_one('SELECT id FROM users WHERE username=?', (username,)):
        flash('用户名已存在', 'danger')
    else:
        execute('INSERT INTO users(username,password_hash,display_name,role) VALUES(?,?,?,?)',
                (username, generate_password_hash(request.form['password']),
                 request.form.get('display_name') or username, request.form.get('role', 'user')))
        flash('用户已创建', 'success')
    return redirect(url_for('user_list'))


@app.route('/users/<int:uid>/edit', methods=['POST'])
@role_required('admin')
def user_edit(uid):
    if uid == g.user['id'] and request.form.get('role') != 'admin':
        flash('不能降级自己', 'danger')
    else:
        execute('UPDATE users SET display_name=?, role=? WHERE id=?',
                (request.form.get('display_name') or '未命名', request.form.get('role', 'user'), uid))
        flash('已保存', 'success')
    return redirect(url_for('user_list'))


@app.route('/users/<int:uid>/reset', methods=['POST'])
@role_required('admin')
def user_reset(uid):
    execute('UPDATE users SET password_hash=? WHERE id=?',
            (generate_password_hash(request.form.get('password', '123456')), uid))
    flash('密码已重置', 'success')
    return redirect(url_for('user_list'))


@app.route('/users/<int:uid>/delete', methods=['POST'])
@role_required('admin')
def user_delete(uid):
    if uid == g.user['id']:
        flash('不能删除自己', 'danger')
    else:
        execute('DELETE FROM users WHERE id=?', (uid,))
        flash('已删除', 'success')
    return redirect(url_for('user_list'))


# ==========================================================================
#  业务工具函数
# ==========================================================================
def now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def gen_no(prefix, table):
    """生成业务单号: 前缀 + YYYYMMDD + 3位流水"""
    day = datetime.now().strftime('%Y%m%d')
    r = query_one(f'SELECT no FROM {table} WHERE no LIKE ? ORDER BY no DESC LIMIT 1',
                  (prefix + day + '%',))
    seq = 1
    if r and r['no'] and len(r['no']) >= 3:
        try:
            seq = int(r['no'][-3:]) + 1
        except ValueError:
            seq = 1
    return f'{prefix}{day}{seq:03d}'


def uid():
    return session.get('uid')


def write_ledger(db, product_id, warehouse_id, change_qty, before_qty, after_qty,
                 ltype, ref_no, remark):
    db.execute('''INSERT INTO stock_ledger(product_id,warehouse_id,change_qty,before_qty,
                  after_qty,type,ref_no,remark,created_at,user_id)
                  VALUES(?,?,?,?,?,?,?,?,?,?)''',
               (product_id, warehouse_id, change_qty, before_qty, after_qty,
                ltype, ref_no, remark, now_str(), uid()))


def _p(product_id):
    p = query_one('SELECT * FROM products WHERE id=?', (product_id,))
    if not p:
        raise ValueError('商品不存在')
    return p


def stock_in(product_id, qty, ref_no, remark, ltype='in'):
    """入库: 账面 +qty, 可用 +qty"""
    if qty <= 0:
        return
    p = _p(product_id)
    with transaction() as db:
        db.execute('''UPDATE products SET stock=stock+?, available_stock=available_stock+?
                      WHERE id=?''', (qty, qty, product_id))
        write_ledger(db, product_id, p['warehouse_id'], qty, p['stock'],
                     p['stock'] + qty, ltype, ref_no, remark)


def stock_out(product_id, qty, ref_no, remark):
    """出库: 账面 -qty, 冻结 -qty (可用不变, 发货前已冻结)"""
    if qty <= 0:
        return
    p = _p(product_id)
    with transaction() as db:
        cur = db.execute('''UPDATE products SET stock=stock-?, frozen_stock=frozen_stock-?
                            WHERE id=? AND stock>=? AND frozen_stock>=?''',
                         (qty, qty, product_id, qty, qty))
        if cur.rowcount == 0:
            raise ValueError(f'{p["name"]} 冻结库存不足, 无法出库')
        write_ledger(db, product_id, p['warehouse_id'], -qty, p['stock'],
                     p['stock'] - qty, 'out', ref_no, remark)


def stock_freeze(product_id, qty, ref_no, remark):
    """冻结: 可用 -qty, 冻结 +qty (账面不变)"""
    if qty <= 0:
        return
    p = _p(product_id)
    with transaction() as db:
        cur = db.execute('''UPDATE products SET available_stock=available_stock-?,
                            frozen_stock=frozen_stock+?
                            WHERE id=? AND available_stock>=?''', (qty, qty, product_id, qty))
        if cur.rowcount == 0:
            raise ValueError(f'{p["name"]} 可用库存不足 (可用 {p["available_stock"]}, 需要 {qty})')
        write_ledger(db, product_id, p['warehouse_id'], 0, p['stock'], p['stock'],
                     'freeze', ref_no, remark)


def stock_unfreeze(product_id, qty, ref_no, remark):
    """解冻: 冻结 -qty, 可用 +qty"""
    if qty <= 0:
        return
    p = _p(product_id)
    with transaction() as db:
        cur = db.execute('''UPDATE products SET frozen_stock=frozen_stock-?,
                            available_stock=available_stock+?
                            WHERE id=? AND frozen_stock>=?''', (qty, qty, product_id, qty))
        if cur.rowcount == 0:
            raise ValueError('冻结库存不足, 无法解冻')
        write_ledger(db, product_id, p['warehouse_id'], 0, p['stock'], p['stock'],
                     'unfreeze', ref_no, remark)


def stock_rollback(product_id, qty, ref_no, remark):
    """拒收/退货回滚出库: 账面 +qty, 冻结 +qty (恢复到发货前状态)"""
    if qty <= 0:
        return
    p = _p(product_id)
    with transaction() as db:
        db.execute('''UPDATE products SET stock=stock+?, frozen_stock=frozen_stock+?
                      WHERE id=?''', (qty, qty, product_id))
        write_ledger(db, product_id, p['warehouse_id'], qty, p['stock'],
                     p['stock'] + qty, 'in', ref_no, remark)


def stock_adjust(product_id, diff, ref_no, remark):
    """盘点调整: 账面 ±diff, 可用同步, 保证可用不为负"""
    if diff == 0:
        return
    p = _p(product_id)
    new_stock = p['stock'] + diff
    if new_stock < 0:
        new_stock = 0
    new_avail = p['available_stock'] + diff
    new_frozen = p['frozen_stock']
    if new_avail < 0:
        new_avail = 0
        new_frozen = max(0, new_stock)
    with transaction() as db:
        db.execute('''UPDATE products SET stock=?, available_stock=?, frozen_stock=?
                      WHERE id=?''', (new_stock, new_avail, new_frozen, product_id))
        write_ledger(db, product_id, p['warehouse_id'], diff, p['stock'],
                     new_stock, 'adjust', ref_no, remark)


def order_amount(items):
    return sum(i['qty'] * i['price'] for i in items)


def parse_items(prefix):
    """从表单解析明细行: 返回 [{product_id, qty, price}]"""
    rows = []
    ids = request.form.getlist(prefix + 'product_id')
    qtys = request.form.getlist(prefix + 'qty')
    prices = request.form.getlist(prefix + 'price')
    for i, pid in enumerate(ids):
        if not pid:
            continue
        try:
            q = int(qtys[i] or 0)
        except (ValueError, IndexError):
            q = 0
        try:
            pr = int(round(float(prices[i] or 0) * 100))
        except (ValueError, IndexError):
            pr = 0
        if q > 0 and pr >= 0:
            rows.append({'product_id': int(pid), 'qty': q, 'price': pr})
    return rows


# ==========================================================================
#  仪表盘
# ==========================================================================
@app.route('/')
@login_required
def index():
    total_stock = query_one('SELECT COALESCE(SUM(stock),0) v FROM products')['v']
    total_value = query_one('SELECT COALESCE(SUM(stock*price),0) v FROM products')['v']
    warn = query_one('''SELECT COUNT(*) c FROM products
                        WHERE available_stock <= safety_stock AND status='on' ''')['c']
    oos = query_one('SELECT COUNT(*) c FROM products WHERE available_stock<=0')['c']
    in_transit = query_one('''SELECT COUNT(*) c FROM waybills
                              WHERE status IN ('picked','in_transit','delivering')''')['c']
    pending_po = query_one("SELECT COUNT(*) c FROM purchase_orders WHERE status='pending'")['c']
    pending_so = query_one("SELECT COUNT(*) c FROM sale_orders WHERE status='pending'")['c']
    month = datetime.now().strftime('%Y-%m')
    month_sales = query_one('''SELECT COALESCE(SUM(si.qty*si.price),0) v
                               FROM sale_items si JOIN sale_orders so ON so.id=si.so_id
                               WHERE so.status IN ('shipped','finished')
                                 AND strftime('%Y-%m', so.created_at)=?''', (month,))['v']
    signed = query_one('''SELECT COUNT(*) c FROM waybills WHERE status='signed' ''')['c']
    avg_hours = query_one('''SELECT COALESCE(AVG((julianday(signed_at)-julianday(created_at))*24),0) v
                             FROM waybills WHERE status='signed' AND signed_at IS NOT NULL''')['v']

    stats = {
        '库存总量(件)': total_stock,
        '库存总值': f'¥{total_value/100:,.2f}',
        '缺货预警': warn,
        '零可用商品': oos,
        '在途运单': in_transit,
        '已签收运单': signed,
        '待收货采购单': pending_po,
        '待出库销售单': pending_so,
        '本月销售额': f'¥{month_sales/100:,.2f}',
        '平均签收时效': f'{avg_hours:.1f} 小时',
    }

    # 近6月销售趋势
    rows = query('''SELECT strftime('%Y-%m', so.created_at) ym,
                           COALESCE(SUM(si.qty*si.price),0) amt
                    FROM sale_orders so LEFT JOIN sale_items si ON si.so_id=so.id
                    WHERE so.status IN ('shipped','finished')
                    GROUP BY ym ORDER BY ym DESC LIMIT 6''')
    rows = list(reversed(rows))
    trend_x = [r['ym'] for r in rows]
    trend_y = [round(r['amt'] / 100.0, 2) for r in rows]

    top = query('''SELECT name, stock, available_stock FROM products
                   ORDER BY stock DESC LIMIT 10''')
    top_x = [t['name'] for t in top]
    top_y = [t['stock'] for t in top]

    warn_list = query('''SELECT * FROM products
                         WHERE available_stock <= safety_stock AND status='on'
                         ORDER BY (available_stock - safety_stock) LIMIT 8''')
    recent_wb = query('''SELECT w.*, so.no AS so_no, c.name AS customer_name
                         FROM waybills w
                         LEFT JOIN sale_orders so ON so.id=w.sale_order_id
                         LEFT JOIN customers c ON c.id=so.customer_id
                         ORDER BY w.id DESC LIMIT 6''')
    recent_ledger = query('''SELECT l.*, p.name AS product_name FROM stock_ledger l
                             LEFT JOIN products p ON p.id=l.product_id
                             ORDER BY l.id DESC LIMIT 8''')
    return render_template('index.html', stats=stats, trend_x=trend_x, trend_y=trend_y,
                           top_x=top_x, top_y=top_y, warn_list=warn_list,
                           recent_wb=recent_wb, recent_ledger=recent_ledger)


# ==========================================================================
#  基础数据: 商品 / 供应商 / 客户 / 仓库
# ==========================================================================
@app.route('/products')
@login_required
def products():
    kw = request.args.get('kw', '').strip()
    if kw:
        rows = query('''SELECT p.*, w.name AS warehouse_name FROM products p
                        LEFT JOIN warehouses w ON w.id=p.warehouse_id
                        WHERE p.name LIKE ? OR p.code LIKE ? ORDER BY p.id''',
                     (f'%{kw}%', f'%{kw}%'))
    else:
        rows = query('''SELECT p.*, w.name AS warehouse_name FROM products p
                        LEFT JOIN warehouses w ON w.id=p.warehouse_id ORDER BY p.id''')
    warehouses = query('SELECT * FROM warehouses ORDER BY id')
    cats = [r['category'] for r in query(
        'SELECT DISTINCT category FROM products WHERE category IS NOT NULL')]
    return render_template('products.html', rows=rows, warehouses=warehouses,
                           cats=cats, kw=kw)


@app.route('/products/add', methods=['POST'])
@login_required
def product_add():
    f = request.form
    code = f.get('code', '').strip()
    name = f.get('name', '').strip()
    if not code or not name:
        flash('商品编码与名称必填', 'warning')
        return redirect(url_for('products'))
    if query_one('SELECT id FROM products WHERE code=?', (code,)):
        flash('商品编码已存在', 'danger')
        return redirect(url_for('products'))
    try:
        cost = int(round(float(f.get('price') or 0) * 100))
        sale = int(round(float(f.get('sale_price') or 0) * 100))
    except ValueError:
        flash('价格格式不正确', 'warning')
        return redirect(url_for('products'))
    init = int(f.get('stock') or 0)
    execute('''INSERT INTO products(code,name,category,unit,price,sale_price,stock,
               available_stock,frozen_stock,safety_stock,warehouse_id,status)
               VALUES(?,?,?,?,?,?,?,?,0,?,?,?)''',
            (code, name, f.get('category') or '通用', f.get('unit') or '件',
             cost, sale, init, init, int(f.get('safety_stock') or 0),
             int(f.get('warehouse_id') or 1), 'on'))
    if init:
        pid = query_one('SELECT id FROM products WHERE code=?', (code,))['id']
        with transaction() as db:
            write_ledger(db, pid, int(f.get('warehouse_id') or 1), init, 0, init,
                         'in', 'INIT', '期初建账入库')
    flash('商品已创建', 'success')
    return redirect(url_for('products'))


@app.route('/products/<int:pid>/edit', methods=['POST'])
@login_required
def product_edit(pid):
    f = request.form
    try:
        cost = int(round(float(f.get('price') or 0) * 100))
        sale = int(round(float(f.get('sale_price') or 0) * 100))
    except ValueError:
        flash('价格格式不正确', 'warning')
        return redirect(url_for('products'))
    execute('''UPDATE products SET name=?,category=?,unit=?,price=?,sale_price=?,
               safety_stock=?,warehouse_id=?,status=? WHERE id=?''',
            (f.get('name'), f.get('category') or '通用', f.get('unit') or '件',
             cost, sale, int(f.get('safety_stock') or 0),
             int(f.get('warehouse_id') or 1), f.get('status') or 'on', pid))
    flash('商品已更新', 'success')
    return redirect(url_for('products'))


@app.route('/products/<int:pid>/delete', methods=['POST'])
@login_required
def product_delete(pid):
    used = query_one('SELECT COUNT(*) c FROM stock_ledger WHERE product_id=?', (pid,))['c']
    if used:
        flash('该商品已有库存流水, 不能删除(可改为停售)', 'danger')
    else:
        execute('DELETE FROM products WHERE id=?', (pid,))
        flash('商品已删除', 'success')
    return redirect(url_for('products'))


@app.route('/suppliers')
@login_required
def suppliers():
    rows = query('''SELECT s.*,
                    (SELECT COUNT(*) FROM purchase_orders po WHERE po.supplier_id=s.id) po_count,
                    (SELECT COALESCE(SUM(po.total_amount),0) FROM purchase_orders po
                     WHERE po.supplier_id=s.id AND po.status IN ('received','paid')) cost
                    FROM suppliers s ORDER BY s.id''')
    return render_template('suppliers.html', rows=rows)


@app.route('/suppliers/add', methods=['POST'])
@login_required
def supplier_add():
    if not request.form.get('name', '').strip():
        flash('供应商名称必填', 'warning')
    else:
        execute('INSERT INTO suppliers(name,contact,phone,address,status) VALUES(?,?,?,?,?)',
                (request.form['name'].strip(), request.form.get('contact'),
                 request.form.get('phone'), request.form.get('address'), 'on'))
        flash('供应商已创建', 'success')
    return redirect(url_for('suppliers'))


@app.route('/suppliers/<int:sid>/edit', methods=['POST'])
@login_required
def supplier_edit(sid):
    execute('UPDATE suppliers SET name=?,contact=?,phone=?,address=?,status=? WHERE id=?',
            (request.form.get('name'), request.form.get('contact'), request.form.get('phone'),
             request.form.get('address'), request.form.get('status') or 'on', sid))
    flash('供应商已更新', 'success')
    return redirect(url_for('suppliers'))


@app.route('/suppliers/<int:sid>/delete', methods=['POST'])
@login_required
def supplier_delete(sid):
    if query_one('SELECT COUNT(*) c FROM purchase_orders WHERE supplier_id=?', (sid,))['c']:
        flash('该供应商已有采购单, 不能删除', 'danger')
    else:
        execute('DELETE FROM suppliers WHERE id=?', (sid,))
        flash('已删除', 'success')
    return redirect(url_for('suppliers'))


@app.route('/customers')
@login_required
def customers():
    rows = query('''SELECT c.*,
                    (SELECT COUNT(*) FROM sale_orders so WHERE so.customer_id=c.id) so_count,
                    (SELECT COALESCE(SUM(so.total_amount),0) FROM sale_orders so
                     WHERE so.customer_id=c.id AND so.status IN ('shipped','finished')) amount
                    FROM customers c ORDER BY c.id''')
    return render_template('customers.html', rows=rows)


@app.route('/customers/add', methods=['POST'])
@login_required
def customer_add():
    if not request.form.get('name', '').strip():
        flash('客户名称必填', 'warning')
    else:
        execute('INSERT INTO customers(name,contact,phone,address,status) VALUES(?,?,?,?,?)',
                (request.form['name'].strip(), request.form.get('contact'),
                 request.form.get('phone'), request.form.get('address'), 'on'))
        flash('客户已创建', 'success')
    return redirect(url_for('customers'))


@app.route('/customers/<int:cid>/edit', methods=['POST'])
@login_required
def customer_edit(cid):
    execute('UPDATE customers SET name=?,contact=?,phone=?,address=?,status=? WHERE id=?',
            (request.form.get('name'), request.form.get('contact'), request.form.get('phone'),
             request.form.get('address'), request.form.get('status') or 'on', cid))
    flash('客户已更新', 'success')
    return redirect(url_for('customers'))


@app.route('/customers/<int:cid>/delete', methods=['POST'])
@login_required
def customer_delete(cid):
    if query_one('SELECT COUNT(*) c FROM sale_orders WHERE customer_id=?', (cid,))['c']:
        flash('该客户已有销售单, 不能删除', 'danger')
    else:
        execute('DELETE FROM customers WHERE id=?', (cid,))
        flash('已删除', 'success')
    return redirect(url_for('customers'))


@app.route('/warehouses')
@login_required
def warehouses():
    rows = query('''SELECT w.*,
                    (SELECT COUNT(*) FROM products p WHERE p.warehouse_id=w.id) sku_count,
                    (SELECT COALESCE(SUM(p.stock),0) FROM products p WHERE p.warehouse_id=w.id) stock_sum,
                    (SELECT COALESCE(SUM(p.stock*p.price),0) FROM products p WHERE p.warehouse_id=w.id) value
                    FROM warehouses w ORDER BY w.id''')
    return render_template('warehouses.html', rows=rows)


@app.route('/warehouses/add', methods=['POST'])
@login_required
def warehouse_add():
    if not request.form.get('name', '').strip():
        flash('仓库名称必填', 'warning')
    else:
        execute('INSERT INTO warehouses(name,address) VALUES(?,?)',
                (request.form['name'].strip(), request.form.get('address')))
        flash('仓库已创建', 'success')
    return redirect(url_for('warehouses'))


@app.route('/warehouses/<int:wid>/edit', methods=['POST'])
@login_required
def warehouse_edit(wid):
    execute('UPDATE warehouses SET name=?,address=? WHERE id=?',
            (request.form.get('name'), request.form.get('address'), wid))
    flash('仓库已更新', 'success')
    return redirect(url_for('warehouses'))


# ==========================================================================
#  采购管理
# ==========================================================================
@app.route('/purchases')
@login_required
def purchases():
    rows = query('''SELECT po.*, s.name AS supplier_name,
                    (SELECT COUNT(*) FROM purchase_items pi WHERE pi.po_id=po.id) item_count,
                    (SELECT COALESCE(SUM(pi.qty),0) FROM purchase_items pi WHERE pi.po_id=po.id) qty_sum
                    FROM purchase_orders po LEFT JOIN suppliers s ON s.id=po.supplier_id
                    ORDER BY po.id DESC''')
    items = query('''SELECT pi.*, p.name AS product_name, p.code AS product_code, p.unit
                     FROM purchase_items pi LEFT JOIN products p ON p.id=pi.product_id''')
    imap = {}
    for it in items:
        imap.setdefault(it['po_id'], []).append(it)
    return render_template('purchases.html', rows=rows, imap=imap,
                           suppliers=query("SELECT * FROM suppliers WHERE status='on' ORDER BY id"),
                           products=query("SELECT * FROM products WHERE status='on' ORDER BY id"))


@app.route('/purchases/add', methods=['POST'])
@login_required
def purchase_add():
    items = parse_items('pi_')
    if not items:
        flash('请至少填写一行有效采购明细', 'warning')
        return redirect(url_for('purchases'))
    no = gen_no('PO', 'purchase_orders')
    total = order_amount(items)
    with transaction() as db:
        cur = db.execute('''INSERT INTO purchase_orders(no,supplier_id,total_amount,status,created_at)
                            VALUES(?,?,?,'pending',?)''',
                         (no, int(request.form.get('supplier_id') or 1), total, now_str()))
        po_id = cur.lastrowid
        for it in items:
            db.execute('INSERT INTO purchase_items(po_id,product_id,qty,price) VALUES(?,?,?,?)',
                       (po_id, it['product_id'], it['qty'], it['price']))
    flash(f'采购单 {no} 已创建, 状态: 待收货', 'success')
    return redirect(url_for('purchases'))


@app.route('/purchases/<int:po_id>/receive', methods=['POST'])
@login_required
def purchase_receive(po_id):
    po = query_one('SELECT * FROM purchase_orders WHERE id=?', (po_id,))
    if not po:
        abort(404)
    if po['status'] != 'pending':
        flash('该采购单不是待收货状态, 不能重复入库', 'warning')
        return redirect(url_for('purchases'))
    items = query('SELECT * FROM purchase_items WHERE po_id=?', (po_id,))
    try:
        for it in items:
            stock_in(it['product_id'], it['qty'], po['no'],
                     f'采购入库 · 采购单 {po["no"]}', 'in')
    except ValueError as e:
        flash(f'入库失败: {e}', 'danger')
        return redirect(url_for('purchases'))
    execute("UPDATE purchase_orders SET status='received', received_at=? WHERE id=?",
            (now_str(), po_id))
    flash(f'采购单 {po["no"]} 已收货入库, 库存已增加', 'success')
    return redirect(url_for('purchases'))


@app.route('/purchases/<int:po_id>/pay', methods=['POST'])
@login_required
def purchase_pay(po_id):
    po = query_one('SELECT * FROM purchase_orders WHERE id=?', (po_id,))
    if not po:
        abort(404)
    if po['status'] == 'pending':
        flash('请先收货入库再付款', 'warning')
        return redirect(url_for('purchases'))
    if po['status'] == 'paid':
        flash('该采购单已付款', 'info')
        return redirect(url_for('purchases'))
    with transaction() as db:
        db.execute('''INSERT INTO payments(type,ref_type,ref_id,amount,remark,created_at)
                      VALUES('pay','purchase',?,?,?,?)''',
                   (po_id, po['total_amount'], f'采购付款 · {po["no"]}', now_str()))
        db.execute("UPDATE purchase_orders SET status='paid', paid_at=? WHERE id=?",
                   (now_str(), po_id))
    flash(f'采购单 {po["no"]} 付款完成 ¥{po["total_amount"]/100:,.2f}', 'success')
    return redirect(url_for('purchases'))


# ==========================================================================
#  销售管理
# ==========================================================================
def _sale_rows(where='', args=()):
    return query(f'''SELECT so.*, c.name AS customer_name,
                     (SELECT COUNT(*) FROM sale_items si WHERE si.so_id=so.id) item_count,
                     (SELECT w.id FROM waybills w WHERE w.sale_order_id=so.id
                      ORDER BY w.id DESC LIMIT 1) wb_id,
                     (SELECT w.no FROM waybills w WHERE w.sale_order_id=so.id
                      ORDER BY w.id DESC LIMIT 1) wb_no,
                     (SELECT w.status FROM waybills w WHERE w.sale_order_id=so.id
                      ORDER BY w.id DESC LIMIT 1) wb_status
                     FROM sale_orders so LEFT JOIN customers c ON c.id=so.customer_id
                     {where} ORDER BY so.id DESC''', args)


@app.route('/sales')
@login_required
def sales():
    rows = _sale_rows()
    items = query('''SELECT si.*, p.name AS product_name, p.code AS product_code, p.unit
                     FROM sale_items si LEFT JOIN products p ON p.id=si.product_id''')
    imap = {}
    for it in items:
        imap.setdefault(it['so_id'], []).append(it)
    return render_template('sales.html', rows=rows, imap=imap,
                           customers=query("SELECT * FROM customers WHERE status='on' ORDER BY id"),
                           products=query("SELECT * FROM products WHERE status='on' ORDER BY id"))


@app.route('/sales/add', methods=['POST'])
@login_required
def sale_add():
    items = parse_items('si_')
    if not items:
        flash('请至少填写一行有效销售明细', 'warning')
        return redirect(url_for('sales'))
    no = gen_no('SO', 'sale_orders')
    total = order_amount(items)
    # 先校验可用库存, 再落单并冻结
    for it in items:
        p = _p(it['product_id'])
        if p['available_stock'] < it['qty']:
            flash(f'可用库存不足: {p["name"]} 可用 {p["available_stock"]}, 需要 {it["qty"]}', 'danger')
            return redirect(url_for('sales'))
    try:
        with transaction() as db:
            cur = db.execute('''INSERT INTO sale_orders(no,customer_id,total_amount,status,created_at)
                                VALUES(?,?,?,'pending',?)''',
                             (no, int(request.form.get('customer_id') or 1), total, now_str()))
            so_id = cur.lastrowid
            for it in items:
                db.execute('INSERT INTO sale_items(so_id,product_id,qty,price) VALUES(?,?,?,?)',
                           (so_id, it['product_id'], it['qty'], it['price']))
        for it in items:
            stock_freeze(it['product_id'], it['qty'], no, f'销售下单冻结 · 销售单 {no}')
    except ValueError as e:
        execute("DELETE FROM sale_items WHERE so_id=?", (so_id,))
        execute('DELETE FROM sale_orders WHERE id=?', (so_id,))
        flash(f'下单失败: {e}', 'danger')
        return redirect(url_for('sales'))
    flash(f'销售单 {no} 已创建, 库存已冻结 ¥{total/100:,.2f}', 'success')
    return redirect(url_for('sales'))


@app.route('/sales/<int:so_id>/ship', methods=['POST'])
@login_required
def sale_ship(so_id):
    so = query_one('SELECT * FROM sale_orders WHERE id=?', (so_id,))
    if not so:
        abort(404)
    if so['status'] != 'pending':
        flash('该销售单不是待出库状态', 'warning')
        return redirect(url_for('sales'))
    items = query('SELECT * FROM sale_items WHERE so_id=?', (so_id,))
    try:
        for it in items:
            stock_out(it['product_id'], it['qty'], so['no'],
                      f'销售出库 · 销售单 {so["no"]}')
    except ValueError as e:
        flash(f'出库失败: {e}', 'danger')
        return redirect(url_for('sales'))
    cust = dict(query_one('SELECT * FROM customers WHERE id=?', (so['customer_id'],)) or {})
    wb_no = gen_no('JD', 'waybills')
    carrier = request.form.get('carrier') or CARRIERS[0]
    with transaction() as db:
        cur = db.execute('''INSERT INTO waybills(no,sale_order_id,carrier,receiver,phone,address,
                            status,created_at) VALUES(?,?,?,?,?,?,'created',?)''',
                         (wb_no, so_id, carrier, cust.get('name'),
                          cust.get('phone'), cust.get('address'), now_str()))
        wb_id = cur.lastrowid
        db.execute('''INSERT INTO waybill_tracks(waybill_id,status,remark,created_at)
                      VALUES(?,?,?,?)''',
                   (wb_id, 'created', f'运单已生成, 承运商: {carrier}, 待揽收', now_str()))
        db.execute("UPDATE sale_orders SET status='shipped', shipped_at=? WHERE id=?",
                   (now_str(), so_id))
    flash(f'销售单 {so["no"]} 已出库, 运单 {wb_no} 已生成', 'success')
    return redirect(url_for('waybills'))


@app.route('/sales/<int:so_id>/pay', methods=['POST'])
@login_required
def sale_pay(so_id):
    so = query_one('SELECT * FROM sale_orders WHERE id=?', (so_id,))
    if not so:
        abort(404)
    if so['status'] == 'pending':
        flash('请先出库发货再收款', 'warning')
        return redirect(url_for('sales'))
    if query_one("SELECT id FROM payments WHERE type='receive' AND ref_type='sale' AND ref_id=?",
                 (so_id,)):
        flash('该销售单已收款', 'info')
        return redirect(url_for('sales'))
    execute('''INSERT INTO payments(type,ref_type,ref_id,amount,remark,created_at)
               VALUES('receive','sale',?,?,?,?)''',
            (so_id, so['total_amount'], f'销售收款 · {so["no"]}', now_str()))
    flash(f'销售单 {so["no"]} 收款完成 ¥{so["total_amount"]/100:,.2f}', 'success')
    return redirect(url_for('sales'))


# ==========================================================================
#  库存台账 / 库存流水
# ==========================================================================
@app.route('/stock')
@login_required
def stock():
    kw = request.args.get('kw', '').strip()
    wid = request.args.get('warehouse_id', '')
    sql = '''SELECT p.*, w.name AS warehouse_name FROM products p
             LEFT JOIN warehouses w ON w.id=p.warehouse_id WHERE 1=1'''
    args = []
    if kw:
        sql += ' AND (p.name LIKE ? OR p.code LIKE ?)'
        args += [f'%{kw}%', f'%{kw}%']
    if wid:
        sql += ' AND p.warehouse_id=?'
        args.append(int(wid))
    sql += ' ORDER BY (p.available_stock - p.safety_stock), p.id'
    rows = query(sql, tuple(args))
    total_stock = sum(r['stock'] for r in rows)
    total_value = sum(r['stock'] * r['price'] for r in rows)
    warn_count = sum(1 for r in rows if r['available_stock'] <= r['safety_stock'])
    oos_count = sum(1 for r in rows if r['available_stock'] <= 0)
    return render_template('stock.html', rows=rows, kw=kw, wid=wid,
                           warehouses=query('SELECT * FROM warehouses ORDER BY id'),
                           total_stock=total_stock, total_value=total_value,
                           warn_count=warn_count, oos_count=oos_count)


@app.route('/stock/ledger')
@login_required
def stock_ledger():
    pid = request.args.get('product_id', '')
    ltype = request.args.get('type', '')
    sql = '''SELECT l.*, p.name AS product_name, p.code AS product_code,
                    w.name AS warehouse_name, u.display_name AS user_name
             FROM stock_ledger l
             LEFT JOIN products p ON p.id=l.product_id
             LEFT JOIN warehouses w ON w.id=l.warehouse_id
             LEFT JOIN users u ON u.id=l.user_id WHERE 1=1'''
    args = []
    if pid:
        sql += ' AND l.product_id=?'
        args.append(int(pid))
    if ltype:
        sql += ' AND l.type=?'
        args.append(ltype)
    sql += ' ORDER BY l.id DESC LIMIT 300'
    rows = query(sql, tuple(args))
    stat = {}
    for t in LEDGER_TYPE:
        stat[t] = query_one('SELECT COUNT(*) c FROM stock_ledger WHERE type=?', (t,))['c']
    return render_template('ledger.html', rows=rows, pid=pid, ltype=ltype, stat=stat,
                           products=query('SELECT * FROM products ORDER BY id'),
                           total=query_one('SELECT COUNT(*) c FROM stock_ledger')['c'])


# ==========================================================================
#  盘点管理
# ==========================================================================
@app.route('/stocktakes')
@login_required
def stocktakes():
    rows = query('''SELECT st.*, w.name AS warehouse_name,
                    (SELECT COUNT(*) FROM stocktake_items si WHERE si.take_id=st.id) item_count,
                    (SELECT COUNT(*) FROM stocktake_items si WHERE si.take_id=st.id AND si.diff_qty<>0) diff_count
                    FROM stocktakes st LEFT JOIN warehouses w ON w.id=st.warehouse_id
                    ORDER BY st.id DESC''')
    items = query('''SELECT si.*, p.name AS product_name, p.code AS product_code, p.unit
                     FROM stocktake_items si LEFT JOIN products p ON p.id=si.product_id
                     ORDER BY si.id''')
    imap = {}
    for it in items:
        imap.setdefault(it['take_id'], []).append(it)
    adjust = None
    aid = request.args.get('adjust', type=int)
    if aid:
        take = query_one('SELECT * FROM stocktakes WHERE id=?', (aid,))
        if take:
            take['items'] = imap.get(aid, [])
            take['diff_total'] = sum(x['diff_qty'] or 0 for x in take['items'])
            adjust = take
    for r in rows:
        r['diff_total'] = sum(x['diff_qty'] or 0 for x in imap.get(r['id'], []))
    return render_template('stocktakes.html', rows=rows, imap=imap, adjust=adjust,
                           warehouses=query('SELECT * FROM warehouses ORDER BY id'))


@app.route('/stocktakes/add', methods=['POST'])
@login_required
def stocktake_add():
    wid = int(request.form.get('warehouse_id') or 1)
    no = gen_no('ST', 'stocktakes')
    prods = query("SELECT * FROM products WHERE warehouse_id=? AND status='on' ORDER BY id", (wid,))
    if not prods:
        flash('该仓库暂无商品可盘点', 'warning')
        return redirect(url_for('stocktakes'))
    with transaction() as db:
        cur = db.execute('''INSERT INTO stocktakes(no,warehouse_id,status,created_at)
                            VALUES(?,?,'draft',?)''', (no, wid, now_str()))
        tid = cur.lastrowid
        for p in prods:
            db.execute('''INSERT INTO stocktake_items(take_id,product_id,book_qty,actual_qty,diff_qty)
                          VALUES(?,?,?,?,0)''', (tid, p['id'], p['stock'], p['stock']))
    flash(f'盘点单 {no} 已创建, 请录入实盘数量', 'success')
    return redirect(url_for('stocktakes'))


@app.route('/stocktakes/<int:tid>/adjust', methods=['POST'])
@login_required
def stocktake_adjust(tid):
    st = query_one('SELECT * FROM stocktakes WHERE id=?', (tid,))
    if not st:
        abort(404)
    if st['status'] == 'done':
        flash('该盘点单已完成', 'info')
        return redirect(url_for('stocktakes'))
    items = query('SELECT * FROM stocktake_items WHERE take_id=?', (tid,))
    changed = 0
    with transaction() as db:
        for it in items:
            raw = request.form.get(f'actual_{it["id"]}')
            try:
                actual = int(raw) if raw not in (None, '') else it['book_qty']
            except ValueError:
                actual = it['book_qty']
            book = _p(it['product_id'])['stock']   # 以当前账面为基准
            diff = actual - book
            db.execute('''UPDATE stocktake_items SET book_qty=?, actual_qty=?, diff_qty=?
                          WHERE id=?''', (book, actual, diff, it['id']))
            if diff != 0:
                changed += 1
                stock_adjust(it['product_id'], diff, st['no'],
                             f'盘点差异调整 · 盘点单 {st["no"]} (账面 {book} → 实盘 {actual})')
        db.execute("UPDATE stocktakes SET status='done', done_at=? WHERE id=?", (now_str(), tid))
    flash(f'盘点单 {st["no"]} 已确认, {changed} 个商品产生差异并自动记账', 'success')
    return redirect(url_for('stocktakes'))


# ==========================================================================
#  运单管理(宅急送)
# ==========================================================================
@app.route('/waybills')
@login_required
def waybills():
    status = request.args.get('status', '')
    sql = '''SELECT w.*, so.no AS so_no, so.total_amount, c.name AS customer_name
             FROM waybills w
             LEFT JOIN sale_orders so ON so.id=w.sale_order_id
             LEFT JOIN customers c ON c.id=so.customer_id'''
    args = []
    if status:
        sql += ' WHERE w.status=?'
        args.append(status)
    sql += ' ORDER BY w.id DESC'
    rows = query(sql, tuple(args))
    stat = {k: query_one('SELECT COUNT(*) c FROM waybills WHERE status=?', (k,))['c']
            for k in WB_STATUS}
    return render_template('waybills.html', rows=rows, status=status, stat=stat)


@app.route('/waybills/<int:wb_id>')
@login_required
def waybill_detail(wb_id):
    wb = query_one('''SELECT w.*, so.no AS so_no, so.total_amount, so.status AS so_status,
                             c.name AS customer_name, c.contact, c.phone AS customer_phone,
                             c.address AS customer_address
                      FROM waybills w
                      LEFT JOIN sale_orders so ON so.id=w.sale_order_id
                      LEFT JOIN customers c ON c.id=so.customer_id WHERE w.id=?''', (wb_id,))
    if not wb:
        abort(404)
    tracks = query('SELECT * FROM waybill_tracks WHERE waybill_id=? ORDER BY id DESC', (wb_id,))
    items = query('''SELECT si.*, p.name AS product_name, p.code AS product_code, p.unit
                     FROM sale_items si LEFT JOIN products p ON p.id=si.product_id
                     WHERE si.so_id=?''', (wb['sale_order_id'],))
    idx = WB_FLOW.index(wb['status']) if wb['status'] in WB_FLOW else -1
    next_status = WB_FLOW[idx + 1] if 0 <= idx < len(WB_FLOW) - 1 else None
    return render_template('waybill_detail.html', wb=wb, tracks=tracks, items=items,
                           next_status=next_status)


@app.route('/waybills/<int:wb_id>/advance', methods=['POST'])
@login_required
def waybill_advance(wb_id):
    wb = query_one('SELECT * FROM waybills WHERE id=?', (wb_id,))
    if not wb:
        abort(404)
    if wb['status'] == 'signed':
        flash('该运单已签收, 流程结束', 'info')
        return redirect(request.referrer or url_for('waybills'))
    if wb['status'] == 'exception':
        flash('异常运单不能继续推进, 请新建运单', 'warning')
        return redirect(request.referrer or url_for('waybills'))
    idx = WB_FLOW.index(wb['status'])
    if idx >= len(WB_FLOW) - 1:
        flash('运单已到终态', 'info')
        return redirect(request.referrer or url_for('waybills'))
    nxt = WB_FLOW[idx + 1]
    remarks = {
        'picked': '配送员已揽收, 货物进入宅急送分拣中心',
        'in_transit': '货物运输中, 已发往目的地城市',
        'delivering': '货物到达末端网点, 配送员派送中',
        'signed': '客户已签收, 交易完成',
    }
    with transaction() as db:
        db.execute('UPDATE waybills SET status=? WHERE id=?', (nxt, wb_id))
        db.execute('''INSERT INTO waybill_tracks(waybill_id,status,remark,created_at)
                      VALUES(?,?,?,?)''', (wb_id, nxt, remarks.get(nxt, ''), now_str()))
        if nxt == 'signed':
            db.execute('UPDATE waybills SET signed_at=? WHERE id=?', (now_str(), wb_id))
            db.execute("UPDATE sale_orders SET status='finished', finished_at=? WHERE id=?",
                       (now_str(), wb['sale_order_id']))
            so = query_one('SELECT * FROM sale_orders WHERE id=?', (wb['sale_order_id'],))
            if so and not query_one("""SELECT id FROM payments WHERE type='receive'
                                       AND ref_type='sale' AND ref_id=?""", (so['id'],)):
                db.execute('''INSERT INTO payments(type,ref_type,ref_id,amount,remark,created_at)
                              VALUES('receive','sale',?,?,?,?)''',
                           (so['id'], so['total_amount'], f'签收自动收款 · {so["no"]}', now_str()))
    flash(f'运单 {wb["no"]} 状态已推进: {WB_STATUS[nxt][0]}', 'success')
    return redirect(request.referrer or url_for('waybills'))


@app.route('/waybills/<int:wb_id>/exception', methods=['POST'])
@login_required
def waybill_exception(wb_id):
    wb = query_one('SELECT * FROM waybills WHERE id=?', (wb_id,))
    if not wb:
        abort(404)
    if wb['status'] in ('signed', 'exception'):
        flash('该运单已是终态, 不能标记异常', 'warning')
        return redirect(request.referrer or url_for('waybills'))
    reason = request.form.get('reason') or '拒收'
    note = (request.form.get('remark') or '').strip()
    rollback = reason in ('拒收', '退货')
    so = query_one('SELECT * FROM sale_orders WHERE id=?', (wb['sale_order_id'],))
    with transaction() as db:
        db.execute('''INSERT INTO waybill_tracks(waybill_id,status,remark,created_at)
                      VALUES(?,?,?,?)''',
                   (wb_id, 'exception', f'异常上报: {reason}。{note}', now_str()))
        db.execute("UPDATE waybills SET status='exception' WHERE id=?", (wb_id,))
        if rollback and so:
            db.execute("UPDATE sale_orders SET status='pending', shipped_at=NULL WHERE id=?",
                       (so['id'],))
    if rollback and so:
        items = query('SELECT * FROM sale_items WHERE so_id=?', (so['id'],))
        for it in items:
            stock_rollback(it['product_id'], it['qty'], wb['no'],
                           f'{reason}回滚出库 · 运单 {wb["no"]} / 销售单 {so["no"]}')
        flash(f'运单 {wb["no"]} 已标记【{reason}】, 出库已回滚, 销售单 {so["no"]} 退回待出库', 'warning')
    elif reason == '丢件' and so:
        with transaction() as db:
            db.execute('''INSERT INTO payments(type,ref_type,ref_id,amount,remark,created_at)
                          VALUES('pay','compensation',?,?,?,?)''',
                       (so['id'], so['total_amount'],
                        f'丢件赔付 · 运单 {wb["no"]}', now_str()))
        flash(f'运单 {wb["no"]} 已标记【丢件】, 已生成赔付支出记录', 'danger')
    else:
        flash(f'运单 {wb["no"]} 已标记【{reason}】, 库存不回滚', 'warning')
    return redirect(request.referrer or url_for('waybills'))


# ==========================================================================
#  财务报表
# ==========================================================================
@app.route('/finance')
@login_required
def finance():
    purchase_cost = query_one('''SELECT COALESCE(SUM(pi.qty*pi.price),0) v FROM purchase_items pi
                                 JOIN purchase_orders po ON po.id=pi.po_id
                                 WHERE po.status IN ('received','paid')''')['v']
    sale_revenue = query_one('''SELECT COALESCE(SUM(si.qty*si.price),0) v FROM sale_items si
                                JOIN sale_orders so ON so.id=si.so_id
                                WHERE so.status IN ('shipped','finished')''')['v']
    paid_out = query_one("SELECT COALESCE(SUM(amount),0) v FROM payments WHERE type='pay' AND ref_type='purchase'")['v']
    received = query_one("SELECT COALESCE(SUM(amount),0) v FROM payments WHERE type='receive'")['v']
    comp = query_one("SELECT COALESCE(SUM(amount),0) v FROM payments WHERE ref_type='compensation'")['v']
    receivable = query_one('''SELECT COALESCE(SUM(so.total_amount),0) v FROM sale_orders so
                              WHERE so.status IN ('shipped','finished')
                                AND NOT EXISTS (SELECT 1 FROM payments pm
                                    WHERE pm.type='receive' AND pm.ref_type='sale' AND pm.ref_id=so.id)''')['v']
    payable = query_one('''SELECT COALESCE(SUM(po.total_amount),0) v FROM purchase_orders po
                           WHERE po.status='received' ''')['v']
    gross = sale_revenue - purchase_cost
    margin = (gross / sale_revenue * 100) if sale_revenue else 0

    by_product = query('''SELECT p.id, p.code, p.name, p.unit,
                          COALESCE(sv.rev,0) rev, COALESCE(pc.cost,0) cost,
                          COALESCE(sv.qty,0) sale_qty
                          FROM products p
                          LEFT JOIN (SELECT si.product_id, SUM(si.qty*si.price) rev, SUM(si.qty) qty
                                     FROM sale_items si JOIN sale_orders so ON so.id=si.so_id
                                     WHERE so.status IN ('shipped','finished')
                                     GROUP BY si.product_id) sv ON sv.product_id=p.id
                          LEFT JOIN (SELECT pi.product_id, SUM(pi.qty*pi.price) cost
                                     FROM purchase_items pi JOIN purchase_orders po ON po.id=pi.po_id
                                     WHERE po.status IN ('received','paid')
                                     GROUP BY pi.product_id) pc ON pc.product_id=p.id
                          WHERE COALESCE(sv.rev,0)>0 OR COALESCE(pc.cost,0)>0
                          ORDER BY (COALESCE(sv.rev,0)-COALESCE(pc.cost,0)) DESC''')
    for r in by_product:
        r['gross'] = r['rev'] - r['cost']
        r['margin'] = (r['gross'] / r['rev'] * 100) if r['rev'] else 0

    by_customer = query('''SELECT c.id, c.name, COUNT(so.id) order_count,
                           COALESCE(SUM(so.total_amount),0) amount
                           FROM customers c LEFT JOIN sale_orders so ON so.customer_id=c.id
                             AND so.status IN ('shipped','finished')
                           GROUP BY c.id ORDER BY amount DESC''')
    by_supplier = query('''SELECT s.id, s.name, COUNT(po.id) order_count,
                           COALESCE(SUM(po.total_amount),0) amount
                           FROM suppliers s LEFT JOIN purchase_orders po ON po.supplier_id=s.id
                             AND po.status IN ('received','paid')
                           GROUP BY s.id ORDER BY amount DESC''')
    payments = query('''SELECT * FROM payments ORDER BY id DESC LIMIT 40''')
    return render_template('finance.html', purchase_cost=purchase_cost, sale_revenue=sale_revenue,
                           gross=gross, margin=margin, receivable=receivable, payable=payable,
                           paid_out=paid_out, received=received, comp=comp,
                           by_product=by_product, by_customer=by_customer,
                           by_supplier=by_supplier, payments=payments,
                           top_x=[r['name'] for r in by_product[:8]],
                           top_y=[round(r['gross'] / 100.0, 2) for r in by_product[:8]])


@app.errorhandler(403)
def forbidden(e):
    return render_template('error.html', code=403, msg='没有权限访问该页面'), 403


@app.errorhandler(404)
def not_found(e):
    return render_template('error.html', code=404, msg='页面不存在'), 404


with app.app_context():
    ensure_schema()

if __name__ == '__main__':
    print(f'[{APP_NAME}] http://127.0.0.1:{PORT}')
    app.run(host='127.0.0.1', port=PORT, debug=False, threaded=True)
