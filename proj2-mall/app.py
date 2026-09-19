# -*- coding: utf-8 -*-
"""
网上商城 (Mall) —— 交易/电商 PM 作品集 Demo
核心能力: 商品SPU/库存三态、购物车、下单冻结库存、模拟支付(幂等)、超时自动关单、优惠券(满减/折扣互斥)、
退款审核、抢购并发演示(证明不超卖)。
约束: 只使用 flask + stdlib; 金额一律「分」整数; 时间 'YYYY-MM-DD HH:MM:SS' localtime。
"""
import os, random, re, threading, time
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, g, abort, jsonify)
from werkzeug.security import generate_password_hash, check_password_hash
import db as database
from db import query, query_one, execute, execute_many, transaction

# ================== 项目配置 ==================
APP_NAME = '网上商城'
APP_SUBTITLE = 'Mall'
PORT = 8002
NAV = [
    ('home',    '商城首页',   '/',             '🏪'),
    ('products','商品管理',   '/products',     '📦'),
    ('categories','分类管理', '/categories',   '🗂️'),
    ('orders',  '订单管理',   '/orders',       '🧾'),
    ('coupons', '营销中心',   '/coupons',      '🎟️'),
    ('refunds', '售后管理',   '/refunds',      '🔁'),
    ('stats',   '数据总览',   '/stats',        '📊'),
    ('race',    '抢购演示',   '/demo/race',    '⚡'),
    ('my',      '我的订单',   '/my/orders',    '🛍️'),
]
NAV_ROLES = {
    'home': ('admin', 'operator', 'user'),
    'products': ('admin', 'operator'),
    'categories': ('admin',),
    'orders': ('admin', 'operator'),
    'coupons': ('admin', 'operator'),
    'refunds': ('admin', 'operator'),
    'stats': ('admin', 'operator'),
    'race': ('admin', 'operator', 'user'),
    'my': ('admin', 'operator', 'user'),
}
ROLE_LABELS = {'admin': '管理员', 'operator': '运营', 'user': '会员'}

ORDER_STATUS = {
    'pending': ('待付款', 'text-bg-warning'),
    'paid': ('待发货', 'text-bg-primary'),
    'shipped': ('已发货', 'text-bg-info'),
    'finished': ('已完成', 'text-bg-success'),
    'canceled': ('已取消', 'text-bg-secondary'),
    'refunded': ('已退款', 'text-bg-danger'),
}
REFUND_STATUS = {'pending': ('审核中', 'text-bg-warning'), 'approved': ('已同意', 'text-bg-info'),
                 'rejected': ('已拒绝', 'text-bg-secondary'), 'done': ('退款完成', 'text-bg-success')}
PAY_TIMEOUT_MIN = 30   # 超时关单: 待付款 30 分钟
# ==============================================

app = Flask(__name__)
app.secret_key = 'mall-demo-dev-secret'
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
    execute('''CREATE TABLE IF NOT EXISTS categories(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL, sort INTEGER NOT NULL DEFAULT 0
    )''')
    execute('''CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category_id INTEGER,
        title TEXT NOT NULL,
        subtitle TEXT,
        main_image TEXT,
        color TEXT, size TEXT,
        price INTEGER NOT NULL DEFAULT 0,      -- 分
        original_price INTEGER NOT NULL DEFAULT 0,
        stock INTEGER NOT NULL DEFAULT 0,
        frozen_stock INTEGER NOT NULL DEFAULT 0,
        sales INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'on',     -- on/off
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    execute('''CREATE TABLE IF NOT EXISTS carts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, product_id INTEGER NOT NULL, qty INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    execute('''CREATE TABLE IF NOT EXISTS coupons(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        type TEXT NOT NULL,                    -- full_reduce / discount
        threshold INTEGER NOT NULL DEFAULT 0,  -- 门槛(分), 0=无门槛
        value INTEGER NOT NULL,                -- 满减:减多少(分); 折扣:88=8.8折
        stock INTEGER NOT NULL DEFAULT 0,
        total INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'on',
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    execute('''CREATE TABLE IF NOT EXISTS user_coupons(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, coupon_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'unused',
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    execute('''CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_no TEXT UNIQUE NOT NULL,
        user_id INTEGER NOT NULL,
        total_amount INTEGER NOT NULL DEFAULT 0,      -- 商品原价合计
        discount_amount INTEGER NOT NULL DEFAULT 0,   -- 优惠金额
        pay_amount INTEGER NOT NULL DEFAULT 0,        -- 实付
        coupon_id INTEGER,
        address TEXT,
        receiver TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        pay_no TEXT UNIQUE,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        paid_at TEXT, shipped_at TEXT, finished_at TEXT, canceled_at TEXT
    )''')
    execute('''CREATE TABLE IF NOT EXISTS order_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
        title TEXT NOT NULL, spec TEXT, price INTEGER NOT NULL, qty INTEGER NOT NULL
    )''')
    execute('''CREATE TABLE IF NOT EXISTS refunds(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        reason TEXT, status TEXT NOT NULL DEFAULT 'pending',
        amount INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    execute('''CREATE TABLE IF NOT EXISTS comments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL, user_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
        rating INTEGER NOT NULL DEFAULT 5, content TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    execute('''CREATE TABLE IF NOT EXISTS pay_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pay_no TEXT UNIQUE NOT NULL, order_id INTEGER NOT NULL,
        amount INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    execute('CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)')
    execute('CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)')


# ---------------- 认证与权限(同脚手架) ----------------
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
    u = getattr(g, 'user', None)
    role = u['role'] if u else None
    nav = [n for n in NAV if role is None or role in NAV_ROLES.get(n[0], ())] if u else []
    cart_cnt = 0
    if u:
        r = query_one('SELECT COALESCE(SUM(qty),0) c FROM carts WHERE user_id=?', (u['id'],))
        cart_cnt = r['c'] if r else 0
    return {
        'APP_NAME': APP_NAME, 'APP_SUBTITLE': APP_SUBTITLE,
        'NAV': nav, 'ROLE_LABELS': ROLE_LABELS,
        'current_user': u, 'request_path': request.path,
        'ORDER_STATUS': ORDER_STATUS, 'REFUND_STATUS': REFUND_STATUS,
        'cart_cnt': cart_cnt,
    }


def now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def gen_order_no():
    return 'SN' + datetime.now().strftime('%Y%m%d%H%M%S') + str(random.randint(1000, 9999))


def gen_pay_no():
    return 'PAY' + datetime.now().strftime('%Y%m%d%H%M%S') + str(random.randint(100, 999))


def yuan(x):
    """元字符串 -> 分整数(四舍五入)"""
    try:
        return int(round(float(x) * 100))
    except (TypeError, ValueError):
        return 0


# ---------------- 首页 / 商品 ----------------
@app.route('/')
def home():
    q = request.args.get('q', '').strip()
    cid = request.args.get('cid', type=int) or 0
    where, args = ["status='on'"], []
    if q:
        where.append('(title LIKE ? OR subtitle LIKE ?)')
        args += [f'%{q}%', f'%{q}%']
    if cid:
        where.append('category_id=?')
        args.append(cid)
    products = query(f"SELECT * FROM products WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 30", tuple(args))
    cats = query('SELECT * FROM categories ORDER BY sort, id')
    hot = query("SELECT * FROM products WHERE status='on' ORDER BY sales DESC LIMIT 6")
    return render_template('home.html', products=products, cats=cats, hot=hot, q=q, cid=cid)


@app.route('/product/<int:pid>')
def product_detail(pid):
    p = query_one('SELECT * FROM products WHERE id=?', (pid,))
    if not p:
        abort(404)
    cmts = query('''SELECT c.*, u.display_name FROM comments c
                    LEFT JOIN users u ON u.id=c.user_id
                    WHERE c.product_id=? ORDER BY c.id DESC LIMIT 20''', (pid,))
    return render_template('product.html', p=p, cmts=cmts, can_buy=p['stock'] - p['frozen_stock'] > 0)


@app.route('/products')
@role_required('admin', 'operator')
def product_list():
    q = request.args.get('q', '').strip()
    cid = request.args.get('cid', type=int) or 0
    where, args = [], []
    if q:
        where.append('(title LIKE ? OR subtitle LIKE ?)')
        args += [f'%{q}%', f'%{q}%']
    if cid:
        where.append('category_id=?')
        args.append(cid)
    wsql = ('WHERE ' + ' AND '.join(where)) if where else ''
    rows = query(f'SELECT p.*, c.name category_name FROM products p LEFT JOIN categories c ON c.id=p.category_id {wsql} ORDER BY p.id DESC LIMIT 100', tuple(args))
    cats = query('SELECT * FROM categories ORDER BY sort, id')
    return render_template('product_list.html', rows=rows, cats=cats, cid=cid, q=q)


@app.route('/products/add', methods=['POST'])
@role_required('admin', 'operator')
def product_add():
    title = (request.form.get('title') or '').strip()
    if not title:
        flash('商品标题必填', 'warning')
        return redirect(url_for('product_list'))
    execute('''INSERT INTO products(category_id,title,subtitle,main_image,color,size,price,original_price,stock,status)
               VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (request.form.get('category_id', type=int) or None, title,
             (request.form.get('subtitle') or '').strip(), request.form.get('main_image') or '',
             request.form.get('color') or '', request.form.get('size') or '',
             yuan(request.form.get('price')), yuan(request.form.get('original_price') or 0),
             max(0, request.form.get('stock', type=int) or 0),
             'on' if request.form.get('status') == 'on' else 'on'))
    flash(f'商品「{title}」已上架', 'success')
    return redirect(url_for('product_list'))


@app.route('/products/<int:pid>/edit', methods=['POST'])
@role_required('admin', 'operator')
def product_edit(pid):
    p = query_one('SELECT * FROM products WHERE id=?', (pid,))
    if not p:
        abort(404)
    stock = max(0, request.form.get('stock', type=int) or 0)
    if stock < p['stock']:
        flash('库存不能低于当前账面库存(先处理订单)', 'warning')
        return redirect(url_for('product_list'))
    execute('''UPDATE products SET category_id=?, title=?, subtitle=?, main_image=?, color=?, size=?,
               price=?, original_price=?, stock=?, status=? WHERE id=?''',
            (request.form.get('category_id', type=int) or None, (request.form.get('title') or p['title']).strip(),
             request.form.get('subtitle') or '', request.form.get('main_image') or '',
             request.form.get('color') or '', request.form.get('size') or '',
             yuan(request.form.get('price')), yuan(request.form.get('original_price') or 0),
             stock, request.form.get('status', 'on'), pid))
    flash('已保存', 'success')
    return redirect(url_for('product_list'))


@app.route('/products/<int:pid>/toggle')
@role_required('admin', 'operator')
def product_toggle(pid):
    p = query_one('SELECT * FROM products WHERE id=?', (pid,))
    if p:
        execute('UPDATE products SET status=? WHERE id=?', ('off' if p['status'] == 'on' else 'on', pid))
    return redirect(url_for('product_list'))


@app.route('/products/<int:pid>/delete', methods=['POST'])
@role_required('admin')
def product_delete(pid):
    execute('DELETE FROM products WHERE id=?', (pid,))
    flash('已删除', 'success')
    return redirect(url_for('product_list'))


@app.route('/categories', methods=['GET', 'POST'])
@role_required('admin')
def categories():
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        if not name:
            flash('分类名必填', 'warning')
        elif query_one('SELECT id FROM categories WHERE name=?', (name,)):
            flash('分类已存在', 'danger')
        else:
            execute('INSERT INTO categories(name,sort) VALUES(?,?)', (name, request.form.get('sort', type=int) or 0))
            flash('分类已添加', 'success')
        return redirect(url_for('categories'))
    rows = query('''SELECT c.*, (SELECT COUNT(*) FROM products p WHERE p.category_id=c.id) product_cnt
                    FROM categories c ORDER BY c.sort, c.id''')
    return render_template('categories.html', rows=rows)


@app.route('/categories/<int:cid>/delete', methods=['POST'])
@role_required('admin')
def category_delete(cid):
    execute('DELETE FROM categories WHERE id=?', (cid,))
    flash('已删除', 'success')
    return redirect(url_for('categories'))


# ---------------- 购物车 ----------------
@app.route('/cart/add', methods=['POST'])
@login_required
def cart_add():
    pid = request.form.get('product_id', type=int) or 0
    qty = max(1, request.form.get('qty', type=int) or 1)
    p = query_one('SELECT * FROM products WHERE id=? AND status=?', (pid, 'on'))
    if not p:
        flash('商品不存在或已下架', 'danger')
        return redirect(request.referrer or url_for('home'))
    if p['stock'] - p['frozen_stock'] < qty:
        flash(f'「{p["title"]}」库存不足', 'danger')
        return redirect(request.referrer or url_for('home'))
    cur = query_one('SELECT id, qty FROM carts WHERE user_id=? AND product_id=?', (g.user['id'], pid))
    if cur:
        execute('UPDATE carts SET qty=? WHERE id=?', (cur['qty'] + qty, cur['id']))
    else:
        execute('INSERT INTO carts(user_id,product_id,qty) VALUES(?,?,?)', (g.user['id'], pid, qty))
    flash('已加入购物车', 'success')
    return redirect(url_for('cart'))


@app.route('/cart')
@login_required
def cart():
    rows = query('''SELECT c.*, p.title, p.price, p.original_price, p.main_image,
                           p.color, p.size, (p.stock - p.frozen_stock) avail
                    FROM carts c JOIN products p ON p.id=c.product_id
                    WHERE c.user_id=? ORDER BY c.id''', (g.user['id'],))
    total = sum(r['price'] * r['qty'] for r in rows)
    coupons = query('''SELECT u.*, c.title, c.type, c.threshold, c.value
                        FROM user_coupons u JOIN coupons c ON c.id=u.coupon_id
                        WHERE u.user_id=? AND u.status='unused' AND c.status='on'
                          AND (c.threshold=0 OR c.threshold<=?) ORDER BY c.id''', (g.user['id'], total))
    return render_template('cart.html', rows=rows, total=total, coupons=coupons)


@app.route('/cart/update', methods=['POST'])
@login_required
def cart_update():
    cid = request.form.get('cart_id', type=int) or 0
    qty = max(0, request.form.get('qty', type=int) or 0)
    if qty <= 0:
        execute('DELETE FROM carts WHERE id=? AND user_id=?', (cid, g.user['id']))
    else:
        execute('UPDATE carts SET qty=? WHERE id=? AND user_id=?', (qty, cid, g.user['id']))
    return redirect(url_for('cart'))


@app.route('/cart/checkout', methods=['POST'])
@login_required
def checkout():
    """下单: 事务内 校验->冻结库存->生成订单->生成支付单(幂等 pay_no 唯一)。"""
    coupon_id = request.form.get('coupon_id', type=int) or 0
    items = query('''SELECT c.*, p.title, p.price, p.original_price, p.stock, p.frozen_stock, p.color, p.size
                     FROM carts c JOIN products p ON p.id=c.product_id WHERE c.user_id=?''', (g.user['id'],))
    if not items:
        flash('购物车是空的', 'warning')
        return redirect(url_for('cart'))
    total = sum(r['price'] * r['qty'] for r in items)
    disc = 0
    uc = None
    if coupon_id:
        uc = query_one('''SELECT u.*, c.title ctitle, c.type ctype, c.threshold, c.value cvalue
                          FROM user_coupons u JOIN coupons c ON c.id=u.coupon_id
                          WHERE u.id=? AND u.user_id=? AND u.status='unused' AND c.status='on' ''',
                       (coupon_id, g.user['id']))
        if not uc:
            flash('优惠券无效或已使用', 'danger')
            return redirect(url_for('cart'))
        if uc['threshold'] and total < uc['threshold']:
            flash(f'未达优惠门槛(满 {uc["threshold"]/100:.0f} 元)', 'warning')
            return redirect(url_for('cart'))
        disc = min(uc['cvalue'], total) if uc['ctype'] == 'full_reduce' else total - total * uc['cvalue'] // 100
    pay = max(0, total - disc)

    order_no = gen_order_no()
    pay_no = gen_pay_no()
    try:
        with transaction() as db:
            # 校验并扣减/冻结每个商品(条件更新防超卖)
            for it in items:
                cur = db.execute('''UPDATE products SET stock=stock-?, frozen_stock=frozen_stock+?, sales=sales+?
                                    WHERE id=? AND stock>=?''', (it['qty'], it['qty'], 0, it['product_id'], it['qty']))
                if cur.rowcount == 0:
                    raise ValueError(f'「{it["title"]}」库存不足')
            oid = db.execute('''INSERT INTO orders(order_no,user_id,total_amount,discount_amount,pay_amount,
                                coupon_id,address,receiver,status,pay_no,created_at)
                                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                             (order_no, g.user['id'], total, disc, pay, coupon_id or None,
                              request.form.get('address') or '', request.form.get('receiver') or '',
                              'pending', pay_no, now_str())).lastrowid
            for it in items:
                spec = ' / '.join(x for x in (it['color'], it['size']) if x)
                db.execute('''INSERT INTO order_items(order_id,product_id,title,spec,price,qty)
                              VALUES(?,?,?,?,?,?)''', (oid, it['product_id'], it['title'], spec, it['price'], it['qty']))
            if uc:
                db.execute("UPDATE user_coupons SET status='used' WHERE id=?", (uc['id'],))
            db.execute('DELETE FROM carts WHERE user_id=?', (g.user['id'],))
            db.execute('''INSERT INTO pay_logs(pay_no,order_id,amount,status,created_at)
                          VALUES(?,?,?,?,?)''', (pay_no, oid, pay, 'pending', now_str()))
    except ValueError as e:
        flash(str(e), 'danger')
        return redirect(url_for('cart'))
    return redirect(url_for('order_detail', oid=oid))


# ---------------- 订单 ----------------
@app.route('/orders')
@role_required('admin', 'operator')
def orders():
    st = request.args.get('status', '')
    kw = request.args.get('kw', '').strip()
    where, args = [], []
    if st:
        where.append('o.status=?')
        args.append(st)
    if kw:
        where.append('(o.order_no LIKE ? OR u.username LIKE ?)')
        args += [f'%{kw}%', f'%{kw}%']
    wsql = ('WHERE ' + ' AND '.join(where)) if where else ''
    rows = query(f'''SELECT o.*, u.username FROM orders o LEFT JOIN users u ON u.id=o.user_id
                     {wsql} ORDER BY o.id DESC LIMIT 200''', tuple(args))
    stat = {k: query_one('SELECT COUNT(*) c FROM orders WHERE status=?', (k,))['c'] for k in ORDER_STATUS}
    return render_template('orders.html', rows=rows, st=st, kw=kw, stat=stat)


@app.route('/order/<int:oid>')
@login_required
def order_detail(oid):
    o = query_one('SELECT * FROM orders WHERE id=?', (oid,))
    if not o or (g.user['role'] == 'user' and o['user_id'] != g.user['id']):
        abort(404)
    items = query('SELECT * FROM order_items WHERE order_id=?', (oid,))
    pay = query_one('SELECT * FROM pay_logs WHERE order_id=?', (oid,))
    return render_template('order_detail.html', o=o, items=items, pay=pay)


@app.route('/order/<int:oid>/pay', methods=['POST'])
@login_required
def order_pay(oid):
    o = query_one('SELECT * FROM orders WHERE id=?', (oid,))
    if not o or (g.user['role'] == 'user' and o['user_id'] != g.user['id']):
        abort(404)
    if o['status'] == 'paid':
        flash('该订单已支付(幂等拦截), 无需重复付款', 'info')
        return redirect(url_for('order_detail', oid=oid))
    if o['status'] != 'pending':
        flash('当前状态不可支付', 'warning')
        return redirect(url_for('order_detail', oid=oid))
    # 幂等: 同一 pay_no 只能成功一次
    with transaction() as db:
        pl = db.execute('SELECT * FROM pay_logs WHERE pay_no=?', (o['pay_no'],)).fetchone()
        if pl and pl['status'] == 'paid':
            flash('支付已完成', 'success')
        else:
            db.execute("UPDATE pay_logs SET status='paid' WHERE pay_no=?", (o['pay_no'],))
            db.execute('''UPDATE orders SET status='paid', paid_at=? WHERE id=?''', (now_str(), oid))
            for it in query('SELECT * FROM order_items WHERE order_id=?', (oid,)):
                db.execute('UPDATE products SET frozen_stock=frozen_stock-? WHERE id=?', (it['qty'], it['product_id']))
        flash('支付成功 (沙箱模拟, 已按 pay_no 幂等)', 'success')
    return redirect(url_for('order_detail', oid=oid))


@app.route('/order/<int:oid>/ship', methods=['POST'])
@role_required('admin', 'operator')
def order_ship(oid):
    o = query_one('SELECT * FROM orders WHERE id=?', (oid,))
    if o and o['status'] == 'paid':
        execute("UPDATE orders SET status='shipped', shipped_at=? WHERE id=?", (now_str(), oid))
        flash('已发货', 'success')
    else:
        flash('仅「待发货」订单可发货', 'warning')
    return redirect(url_for('order_detail', oid=oid))


@app.route('/order/<int:oid>/finish', methods=['POST'])
@login_required
def order_finish(oid):
    o = query_one('SELECT * FROM orders WHERE id=?', (oid,))
    if not o or (g.user['role'] == 'user' and o['user_id'] != g.user['id']):
        abort(404)
    if o and o['status'] == 'shipped':
        execute("UPDATE orders SET status='finished', finished_at=? WHERE id=?", (now_str(), oid))
        flash('订单已确认收货, 欢迎评价', 'success')
    else:
        flash('仅「已发货」订单可确认', 'warning')
    return redirect(url_for('order_detail', oid=oid))


@app.route('/order/<int:oid>/cancel', methods=['POST'])
@login_required
def order_cancel(oid):
    o = query_one('SELECT * FROM orders WHERE id=?', (oid,))
    if not o or (g.user['role'] == 'user' and o['user_id'] != g.user['id']):
        abort(404)
    if o and o['status'] == 'pending':
        with transaction() as db:
            db.execute("UPDATE orders SET status='canceled', canceled_at=? WHERE id=?", (now_str(), oid))
            for it in query('SELECT * FROM order_items WHERE order_id=?', (oid,)):
                db.execute('UPDATE products SET stock=stock+?, frozen_stock=frozen_stock-? WHERE id=?',
                           (it['qty'], it['qty'], it['product_id']))
            if o['coupon_id']:
                db.execute("UPDATE user_coupons SET status='unused' WHERE user_id=? AND coupon_id=? AND status='used'",
                           (o['user_id'], o['coupon_id']))
        flash('订单已取消, 库存已回滚', 'success')
    else:
        flash('当前状态不可取消', 'warning')
    return redirect(url_for('order_detail', oid=oid))


@app.route('/my/orders')
@login_required
def my_orders():
    rows = query('''SELECT o.*, (SELECT COUNT(*) FROM order_items i WHERE i.order_id=o.id) item_cnt
                    FROM orders o WHERE o.user_id=? ORDER BY o.id DESC LIMIT 50''', (g.user['id'],))
    return render_template('my_orders.html', rows=rows)


# ---------------- 售后 ----------------
@app.route('/order/<int:oid>/refund', methods=['POST'])
@login_required
def order_refund(oid):
    o = query_one('SELECT * FROM orders WHERE id=?', (oid,))
    if not o or (g.user['role'] == 'user' and o['user_id'] != g.user['id']):
        abort(404)
    if o['status'] not in ('paid', 'shipped'):
        flash('该状态不可申请退款', 'warning')
        return redirect(url_for('order_detail', oid=oid))
    if query_one('SELECT id FROM refunds WHERE order_id=? AND status IN (?)', (oid, 'pending')):
        flash('已有待审核的退款申请', 'warning')
        return redirect(url_for('order_detail', oid=oid))
    execute('''INSERT INTO refunds(order_id,user_id,reason,status,amount,created_at)
               VALUES(?,?,?,?,?,?)''',
            (oid, o['user_id'], (request.form.get('reason') or '不想要了').strip()[:200],
             'pending', o['pay_amount'], now_str()))
    flash('退款申请已提交', 'success')
    return redirect(url_for('order_detail', oid=oid))


@app.route('/refunds')
@role_required('admin', 'operator')
def refunds():
    st = request.args.get('status', '')
    where, args = ['1=1'], []
    if st:
        where.append('r.status=?')
        args.append(st)
    rows = query(f'''SELECT r.*, o.order_no, o.user_id, u.username
                     FROM refunds r JOIN orders o ON o.id=r.order_id
                     LEFT JOIN users u ON u.id=o.user_id
                     WHERE {' AND '.join(where)} ORDER BY r.id DESC LIMIT 200''', tuple(args))
    stat = {k: query_one('SELECT COUNT(*) c FROM refunds WHERE status=?', (k,))['c'] for k in REFUND_STATUS}
    return render_template('refunds.html', rows=rows, st=st, stat=stat)


@app.route('/refunds/<int:rid>/review', methods=['POST'])
@role_required('admin', 'operator')
def refund_review(rid):
    act = request.form.get('action')
    r = query_one('SELECT * FROM refunds WHERE id=?', (rid,))
    if not r:
        abort(404)
    if act == 'approve' and r['status'] == 'pending':
        with transaction() as db:
            db.execute("UPDATE refunds SET status='approved' WHERE id=?", (rid,))
            db.execute("UPDATE orders SET status='refunded' WHERE id=?", (r['order_id'],))
            for it in query('SELECT * FROM order_items WHERE order_id=?', (r['order_id'],)):
                db.execute('UPDATE products SET stock=stock+? WHERE id=?', (it['qty'], it['product_id']))
        flash('退款已同意, 库存已回补', 'success')
    elif act == 'reject' and r['status'] == 'pending':
        execute("UPDATE refunds SET status='rejected' WHERE id=?", (rid,))
        flash('已拒绝退款', 'info')
    else:
        flash('状态已变化, 请刷新', 'warning')
    return redirect(url_for('refunds'))


@app.route('/comment', methods=['POST'])
@login_required
def comment():
    oid = request.form.get('order_id', type=int) or 0
    o = query_one('SELECT * FROM orders WHERE id=?', (oid,))
    if not o or o['user_id'] != g.user['id']:
        abort(404)
    pid = request.form.get('product_id', type=int) or 0
    rating = max(1, min(5, request.form.get('rating', type=int) or 5))
    if query_one('SELECT id FROM comments WHERE order_id=? AND product_id=?', (oid, pid)):
        flash('已评价过该商品', 'warning')
        return redirect(url_for('order_detail', oid=oid))
    execute('''INSERT INTO comments(order_id,user_id,product_id,rating,content,created_at)
               VALUES(?,?,?,?,?,?)''', (oid, g.user['id'], pid, rating,
                                        (request.form.get('content') or '').strip()[:500], now_str()))
    flash('评价成功', 'success')
    return redirect(url_for('order_detail', oid=oid))


# ---------------- 营销(优惠券) ----------------
@app.route('/coupons')
@login_required
def coupons():
    rows = query('SELECT * FROM coupons WHERE status=? ORDER BY id', ('on',))
    claimed = {r['coupon_id'] for r in query('SELECT coupon_id FROM user_coupons WHERE user_id=?', (g.user['id'],))}
    # 管理端: 全部券(含已停用) + 领取统计
    mgmt = query('''SELECT c.*, (SELECT COUNT(*) FROM user_coupons u WHERE u.coupon_id=c.id) claimed_cnt
                    FROM coupons c ORDER BY c.id DESC LIMIT 50''') if g.user['role'] in ('admin', 'operator') else []
    return render_template('coupons.html', rows=rows, claimed=claimed, mgmt=mgmt)


@app.route('/coupon/add', methods=['POST'])
@role_required('admin', 'operator')
def coupon_add():
    t = request.form.get('title') or '优惠券'
    typ = request.form.get('type')
    if typ not in ('full_reduce', 'discount'):
        flash('券类型不合法', 'danger')
        return redirect(url_for('coupons'))
    execute('''INSERT INTO coupons(title,type,threshold,value,stock,total,status)
               VALUES(?,?,?,?,?,?,?)''',
            (t, typ, yuan(request.form.get('threshold') or 0),
             yuan(request.form.get('value') or 0) if typ == 'full_reduce' else int(request.form.get('value') or 90),
             request.form.get('stock', type=int) or 0, request.form.get('stock', type=int) or 0, 'on'))
    flash('优惠券已创建', 'success')
    return redirect(url_for('coupons'))


@app.route('/coupon/claim', methods=['POST'])
@login_required
def coupon_claim():
    cid = request.form.get('coupon_id', type=int) or 0
    c = query_one("SELECT * FROM coupons WHERE id=? AND status='on'", (cid,))
    if not c:
        flash('优惠券不存在', 'warning')
    elif query_one('SELECT id FROM user_coupons WHERE user_id=? AND coupon_id=?', (g.user['id'], cid)):
        flash('已领取过该券', 'info')
    elif c['stock'] <= 0:
        flash('已被领完', 'danger')
    else:
        with transaction() as db:
            cur = db.execute("UPDATE coupons SET stock=stock-1 WHERE id=? AND stock>0", (cid,))
            if cur.rowcount:
                db.execute('INSERT INTO user_coupons(user_id,coupon_id) VALUES(?,?)', (g.user['id'], cid))
                flash(f'领取成功: {c["title"]}', 'success')
            else:
                flash('手慢了, 已被领完', 'danger')
    return redirect(url_for('coupons'))


# ---------------- 数据总览 ----------------
@app.route('/stats')
@role_required('admin', 'operator')
def stats():
    m = {}
    m['gmv'] = query_one("SELECT COALESCE(SUM(pay_amount),0) v FROM orders WHERE status IN ('paid','shipped','finished')")['v']
    m['order_cnt'] = query_one("SELECT COUNT(*) c FROM orders WHERE status NOT IN ('canceled','refunded')")['c']
    m['paid_cnt'] = query_one("SELECT COUNT(*) c FROM orders WHERE status='paid'")['c']
    m['pending_cnt'] = query_one("SELECT COUNT(*) c FROM orders WHERE status='pending'")['c']
    m['refund_cnt'] = query_one("SELECT COUNT(*) c FROM refunds WHERE status='pending'")['c']
    m['user_cnt'] = query_one("SELECT COUNT(*) c FROM users WHERE role='user'")['c']
    m['coupon_cnt'] = query_one("SELECT COUNT(*) c FROM user_coupons")['c']
    # 近 7 日订单/支付趋势
    trend = query('''SELECT date(created_at) d, COUNT(*) cnt,
                     COALESCE(SUM(CASE WHEN status IN ('paid','shipped','finished') THEN pay_amount END),0) gmv
                     FROM orders WHERE created_at >= datetime('now','localtime','-6 day')
                     GROUP BY d ORDER BY d''')
    tmap = {r['d']: r for r in trend}
    days, o_cnt, gmv = [], [], []
    for i in range(6, -1, -1):
        d = (datetime.now() - timedelta(days=i)).strftime('%m-%d')
        r = tmap.get((datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d'), {})
        days.append(d)
        o_cnt.append(r.get('cnt') or 0)
        gmv.append(round((r.get('gmv') or 0) / 100, 2))
    hot = query('SELECT title, sales, price FROM products ORDER BY sales DESC LIMIT 10')
    cat_sales = query('''SELECT c.name, COALESCE(SUM(oi.qty),0) q
                         FROM products p LEFT JOIN categories c ON c.id=p.category_id
                         LEFT JOIN order_items oi ON oi.product_id=p.id
                         GROUP BY p.id, c.name ORDER BY q DESC''')
    cat_agg = {}
    for r in cat_sales:
        cat_agg[r['name'] or '未分类'] = cat_agg.get(r['name'] or '未分类', 0) + r['q']
    return render_template('stats.html', m=m, days=days, o_cnt=o_cnt, gmv=gmv,
                           hot=hot, cat_dist=[{'name': k, 'c': v} for k, v in sorted(cat_agg.items(), key=lambda x: -x[1])])


# ---------------- 抢购并发演示 ----------------
@app.route('/demo/race')
@login_required
def demo_race():
    ps = query("SELECT * FROM products WHERE status='on' AND stock-frozen_stock>0 AND stock<=5 ORDER BY stock LIMIT 6")
    if not ps:
        ps = query("SELECT * FROM products WHERE status='on' ORDER BY stock LIMIT 6")
    return render_template('race.html', products=ps)


@app.route('/demo/quick-buy', methods=['POST'])
@login_required
def demo_quick_buy():
    """抢购: 条件更新原子扣减, 返回成功/失败。"""
    pid = request.form.get('product_id', type=int) or 0
    qty = request.form.get('qty', type=int) or 1
    p = query_one('SELECT * FROM products WHERE id=? AND status=?', (pid, 'on'))
    if not p:
        return jsonify(ok=False, msg='商品不存在')
    try:
        with transaction() as db:
            cur = db.execute('''UPDATE products SET stock=stock-?, frozen_stock=frozen_stock+?, sales=sales+?
                                WHERE id=? AND stock>=?''', (qty, qty, qty, pid, qty))
            if cur.rowcount == 0:
                return jsonify(ok=False, msg='手慢了, 库存不足')
            oid = db.execute('''INSERT INTO orders(order_no,user_id,total_amount,discount_amount,pay_amount,
                                status,pay_no,created_at) VALUES(?,?,?,?,?,?,?,?)''',
                             (gen_order_no(), g.user['id'], p['price'] * qty, 0, p['price'] * qty,
                              'paid', gen_pay_no(), now_str())).lastrowid
            db.execute('''INSERT INTO order_items(order_id,product_id,title,spec,price,qty)
                          VALUES(?,?,?,?,?,?)''', (oid, pid, p['title'], '', p['price'], qty))
            db.execute('UPDATE products SET frozen_stock=frozen_stock-? WHERE id=?', (qty, pid))
        return jsonify(ok=True, msg='抢购成功', oid=oid)
    except Exception:
        return jsonify(ok=False, msg='系统繁忙, 请重试')


# ---------------- 登录/用户管理(脚手架搬运) ----------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = query_one('SELECT * FROM users WHERE username=?',
                      (request.form.get('username', '').strip(),))
        if u and check_password_hash(u['password_hash'], request.form.get('password', '')):
            session['uid'] = u['id']
            flash(f'欢迎回来, {u["display_name"]}', 'success')
            return redirect(request.args.get('next') or url_for('home'))
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
            return redirect(url_for('home'))
    return render_template('profile.html')


@app.route('/users')
@role_required('admin')
def user_list():
    return render_template('users.html', users=query('SELECT * FROM users ORDER BY id'))


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


# ---------------- 超时自动关单 ----------------
def _close_timeout_orders():
    """后台线程: 每 10s 扫描待付款超时订单, 取消+回滚库存+退券。"""
    while True:
        try:
            cutoff = (datetime.now() - timedelta(minutes=PAY_TIMEOUT_MIN)).strftime('%Y-%m-%d %H:%M:%S')
            expired = query("SELECT * FROM orders WHERE status='pending' AND created_at < ?", (cutoff,))
            for o in expired:
                with transaction() as db:
                    db.execute("UPDATE orders SET status='canceled', canceled_at=? WHERE id=? AND status='pending'",
                               (now_str(), o['id']))
                    for it in query('SELECT * FROM order_items WHERE order_id=?', (o['id'],)):
                        db.execute('UPDATE products SET stock=stock+?, frozen_stock=frozen_stock-? WHERE id=?',
                                   (it['qty'], it['qty'], it['product_id']))
                    if o['coupon_id']:
                        db.execute("UPDATE user_coupons SET status='unused' WHERE user_id=? AND coupon_id=? AND status='used'",
                                   (o['user_id'], o['coupon_id']))
        except Exception:
            pass
        time.sleep(10)


@app.errorhandler(403)
def forbidden(e):
    return render_template('error.html', code=403, msg='没有权限访问该页面'), 403


@app.errorhandler(404)
def not_found(e):
    return render_template('error.html', code=404, msg='页面不存在'), 404


with app.app_context():
    ensure_schema()

if __name__ == '__main__':
    threading.Thread(target=_close_timeout_orders, daemon=True).start()
    print(f'[{APP_NAME}] http://127.0.0.1:{PORT}')
    app.run(host='127.0.0.1', port=PORT, debug=False, threaded=True)