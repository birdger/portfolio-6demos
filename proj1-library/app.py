# -*- coding: utf-8 -*-
"""
图书借阅管理系统 (Library) —— 后台/功能型 PM 作品集 Demo
统一脚手架扩展: 认证 + RBAC(admin/operator/user) + 借阅业务状态机 + 罚款 + 报表。

业务约束:
  * 只使用 flask + 标准库
  * 金额一律以「分」为整数存储与计算, 展示时除 100
  * 时间统一 'YYYY-MM-DD HH:MM:SS' 字符串, 由 datetime('now','localtime') 生成
"""
import csv
import io
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, g, abort, Response)
from werkzeug.security import generate_password_hash, check_password_hash
import db as database
from db import query, query_one, execute, transaction

# ================== 项目配置 ==================
APP_NAME = '图书借阅管理系统'
APP_SUBTITLE = 'Library'
PORT = 8001

NAV = [
    ('dashboard',    '仪表盘',   '/',             '📊'),
    ('books',        '图书管理', '/books',        '📚'),
    ('readers',      '读者管理', '/readers',      '🧑',),
    ('borrows',      '借阅管理', '/borrows',      '🔁'),
    ('reservations', '预约管理', '/reservations', '🔖'),
    ('fines',        '罚款管理', '/fines',        '💰'),
    ('logs',         '操作日志', '/logs',         '📝'),
    ('stats',        '统计报表', '/stats',        '📈'),
]

# 权限矩阵: 模块 -> 可见角色
NAV_ROLES = {
    'dashboard':    ('admin', 'operator', 'user'),
    'books':        ('admin', 'operator', 'user'),
    'readers':      ('admin', 'operator'),
    'borrows':      ('admin', 'operator'),
    'reservations': ('admin', 'operator'),
    'fines':        ('admin', 'operator'),
    'logs':         ('admin',),
    'stats':        ('admin', 'operator', 'user'),
}
ROLE_LABELS = {'admin': '管理员', 'operator': '操作员', 'user': '读者'}

# 业务常量
BORROW_DAYS = 30            # 借期 30 天
RENEW_DAYS = 30             # 续借延长 30 天
MAX_RENEW = 1               # 每单最多续借 1 次
FINE_PER_DAY = 50           # 逾期罚款 0.50 元/天(以分计)
DEFAULT_LIMIT = 5           # 默认借阅上限

BOOK_STATUS = {'on_shelf': '在架', 'off_shelf': '已下架', 'lost': '挂失'}
BOOK_STATUS_CLS = {'on_shelf': 'success', 'off_shelf': 'secondary', 'lost': 'dark'}
BORROW_STATUS = {'borrowed': '借出中', 'returned': '已归还', 'overdue': '逾期未还', 'lost': '挂失赔偿'}
BORROW_STATUS_CLS = {'borrowed': 'primary', 'returned': 'success', 'overdue': 'danger', 'lost': 'dark'}
RESV_STATUS = {'waiting': '排队中', 'ready': '可取书', 'done': '已完成', 'cancelled': '已取消'}
RESV_STATUS_CLS = {'waiting': 'warning', 'ready': 'info', 'done': 'success', 'cancelled': 'secondary'}
READER_STATUS = {'normal': '正常', 'suspended': '停借'}
READER_STATUS_CLS = {'normal': 'success', 'suspended': 'danger'}
# ==============================================

app = Flask(__name__)
app.secret_key = 'library-demo-secret-key'
database.init_app(app)


# ---------------- 通用工具 ----------------
def money(cents):
    """分 -> 元字符串"""
    return f'{(cents or 0) / 100:.2f}'


app.jinja_env.filters['money'] = money


def today():
    return date.today()


def dstr(d):
    return d.strftime('%Y-%m-%d')


def overdue_days(due_date_str):
    """相对今天已逾期天数(未逾期返回 0)"""
    try:
        d = datetime.strptime(str(due_date_str)[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return 0
    return max(0, (date.today() - d).days)


def log_action(action, detail=''):
    """关键操作留痕"""
    try:
        execute('INSERT INTO logs(user_id,action,detail) VALUES(?,?,?)',
                (session.get('uid'), action, detail))
    except Exception:
        pass


def current_reader():
    """当前登录账号对应的读者档案(按 user_id 关联, 退化按学工号匹配)"""
    uid = session.get('uid')
    if not uid:
        return None
    r = query_one('SELECT * FROM readers WHERE user_id=?', (uid,))
    if not r:
        u = query_one('SELECT username FROM users WHERE id=?', (uid,))
        if u:
            r = query_one('SELECT * FROM readers WHERE card_no=?', (u['username'],))
    return r


def refresh_overdue():
    """把到期未还的借阅单刷新为逾期状态并结算罚金(幂等)"""
    rows = query("""SELECT id, due_date, status FROM borrows
                    WHERE status IN ('borrowed','overdue') AND return_date IS NULL""")
    for r in rows:
        days = overdue_days(r['due_date'])
        if days > 0:
            execute("UPDATE borrows SET status='overdue', fine=? WHERE id=?",
                    (days * FINE_PER_DAY, r['id']))
        elif r['status'] == 'overdue':
            execute("UPDATE borrows SET status='borrowed', fine=0 WHERE id=?", (r['id'],))


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
    execute('''CREATE TABLE IF NOT EXISTS books(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        isbn TEXT,
        author TEXT,
        category TEXT,
        publisher TEXT,
        price INTEGER NOT NULL DEFAULT 0,
        stock INTEGER NOT NULL DEFAULT 0,
        total INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'on_shelf',
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    execute('''CREATE TABLE IF NOT EXISTS readers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        card_no TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        phone TEXT,
        status TEXT NOT NULL DEFAULT 'normal',
        borrow_limit INTEGER NOT NULL DEFAULT 5,
        user_id INTEGER,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    execute('''CREATE TABLE IF NOT EXISTS borrows(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id INTEGER NOT NULL,
        reader_id INTEGER NOT NULL,
        borrow_date TEXT NOT NULL,
        due_date TEXT NOT NULL,
        return_date TEXT,
        status TEXT NOT NULL DEFAULT 'borrowed',
        fine INTEGER NOT NULL DEFAULT 0,
        fine_paid INTEGER NOT NULL DEFAULT 0,
        renew_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    execute('''CREATE TABLE IF NOT EXISTS reservations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id INTEGER NOT NULL,
        reader_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'waiting',
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    execute('''CREATE TABLE IF NOT EXISTS logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action TEXT NOT NULL,
        detail TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    execute('CREATE INDEX IF NOT EXISTS idx_borrows_reader ON borrows(reader_id)')
    execute('CREATE INDEX IF NOT EXISTS idx_borrows_status ON borrows(status)')


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
    u = getattr(g, 'user', None)
    role = u['role'] if u else None
    nav = [n for n in NAV if role is None or role in NAV_ROLES.get(n[0], ())]
    return {
        'APP_NAME': APP_NAME, 'APP_SUBTITLE': APP_SUBTITLE,
        'NAV': nav, 'ROLE_LABELS': ROLE_LABELS,
        'current_user': u,
        'request_path': request.path,
        'is_admin': role == 'admin',
        'can_manage': role in ('admin', 'operator'),
        'BOOK_STATUS': BOOK_STATUS, 'BOOK_STATUS_CLS': BOOK_STATUS_CLS,
        'BORROW_STATUS': BORROW_STATUS, 'BORROW_STATUS_CLS': BORROW_STATUS_CLS,
        'RESV_STATUS': RESV_STATUS, 'RESV_STATUS_CLS': RESV_STATUS_CLS,
        'READER_STATUS': READER_STATUS, 'READER_STATUS_CLS': READER_STATUS_CLS,
        'FINE_PER_DAY': FINE_PER_DAY, 'MAX_RENEW': MAX_RENEW,
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


# ================== 仪表盘 ==================
def dashboard_metrics():
    refresh_overdue()
    m = {}
    m['borrow_total'] = query_one('SELECT COUNT(*) c FROM borrows')['c']
    m['book_total'] = query_one('SELECT COUNT(*) c FROM books')['c']
    m['reader_total'] = query_one('SELECT COUNT(*) c FROM readers')['c']
    m['copy_total'] = query_one('SELECT COALESCE(SUM(total),0) c FROM books')['c']
    m['copy_stock'] = query_one('SELECT COALESCE(SUM(stock),0) c FROM books')['c']
    m['on_shelf_rate'] = round(m['copy_stock'] * 100.0 / m['copy_total'], 1) if m['copy_total'] else 0.0
    m['out_total'] = query_one("SELECT COUNT(*) c FROM borrows WHERE return_date IS NULL AND status IN ('borrowed','overdue')")['c']
    m['overdue_total'] = query_one("SELECT COUNT(*) c FROM borrows WHERE status='overdue'")['c']
    m['overdue_rate'] = round(m['overdue_total'] * 100.0 / m['out_total'], 1) if m['out_total'] else 0.0
    m['active_readers'] = query_one("""SELECT COUNT(DISTINCT reader_id) c FROM borrows
                                       WHERE borrow_date >= date('now','localtime','-30 day')""")['c']
    m['today_borrow'] = query_one("SELECT COUNT(*) c FROM borrows WHERE date(borrow_date)=date('now','localtime')")['c']
    m['today_return'] = query_one("SELECT COUNT(*) c FROM borrows WHERE date(return_date)=date('now','localtime')")['c']
    m['fine_unpaid'] = query_one('SELECT COALESCE(SUM(fine),0) c FROM borrows WHERE fine_paid=0')['c']
    m['fine_paid'] = query_one('SELECT COALESCE(SUM(fine),0) c FROM borrows WHERE fine_paid=1')['c']
    m['waiting_resv'] = query_one("SELECT COUNT(*) c FROM reservations WHERE status='waiting'")['c']
    m['suspended'] = query_one("SELECT COUNT(*) c FROM readers WHERE status='suspended'")['c']
    return m


@app.route('/')
@login_required
def index():
    m = dashboard_metrics()
    recent = query("""SELECT l.*, u.display_name FROM logs l
                      LEFT JOIN users u ON u.id=l.user_id
                      ORDER BY l.id DESC LIMIT 6""")
    hot = query("""SELECT b.title, b.author, COUNT(bo.id) c FROM borrows bo
                   JOIN books b ON b.id=bo.book_id GROUP BY b.id
                   ORDER BY c DESC LIMIT 5""")
    overdue_list = query("""SELECT bo.*, b.title, r.name rname, r.card_no, r.phone
                            FROM borrows bo JOIN books b ON b.id=bo.book_id
                            JOIN readers r ON r.id=bo.reader_id
                            WHERE bo.status='overdue' ORDER BY bo.due_date LIMIT 5""")
    return render_template('index.html', m=m, recent=recent, hot=hot, overdue_list=overdue_list)


# ================== 图书管理 ==================
CATEGORIES = ['文学', '计算机', '经管', '历史', '科技', '教育', '艺术']


@app.route('/books', methods=['GET', 'POST'])
@login_required
def book_list():
    if request.method == 'POST':
        if g.user['role'] != 'admin':
            abort(403)
        title = request.form.get('title', '').strip()
        if not title:
            flash('书名不能为空', 'warning')
            return redirect(url_for('book_list'))
        try:
            price = int(request.form.get('price') or 0)
            stock = int(request.form.get('stock') or 0)
            total = int(request.form.get('total') or stock)
        except ValueError:
            flash('单价/库存/总量必须为数字', 'danger')
            return redirect(url_for('book_list'))
        if stock < 0 or total < 0 or stock > total:
            flash('库存必须 ≥0 且不大于总量', 'danger')
            return redirect(url_for('book_list'))
        isbn = request.form.get('isbn', '').strip()
        if isbn and query_one('SELECT id FROM books WHERE isbn=?', (isbn,)):
            flash(f'ISBN {isbn} 已存在, 不能重复入库', 'danger')
            return redirect(url_for('book_list'))
        bid = execute("""INSERT INTO books(title,isbn,author,category,publisher,price,stock,total,status)
                         VALUES(?,?,?,?,?,?,?,?,'on_shelf')""",
                      (title, isbn, request.form.get('author', '').strip(),
                       request.form.get('category', '').strip(),
                       request.form.get('publisher', '').strip(), price, stock, total))
        log_action('新增图书', f'《{title}》入库 {total} 册, 单价 {money(price)} 元')
        flash(f'《{title}》已入库 (ID {bid})', 'success')
        return redirect(url_for('book_list'))

    q = request.args.get('q', '').strip()
    cat = request.args.get('category', '').strip()
    st = request.args.get('status', '').strip()
    where, args = [], []
    if q:
        where.append('(title LIKE ? OR author LIKE ? OR isbn LIKE ? OR publisher LIKE ?)')
        args += [f'%{q}%'] * 4
    if cat:
        where.append('category=?')
        args.append(cat)
    if st:
        where.append('status=?')
        args.append(st)
    wsql = ('WHERE ' + ' AND '.join(where)) if where else ''
    total = query_one(f'SELECT COUNT(*) c FROM books {wsql}', tuple(args))['c']
    per = 12
    pages = max(1, (total + per - 1) // per)
    page = min(max(1, request.args.get('page', 1, type=int)), pages)
    books = query(f'SELECT * FROM books {wsql} ORDER BY id DESC LIMIT {per} OFFSET {(page - 1) * per}',
                  tuple(args))
    cats = [r['category'] for r in query(
        "SELECT DISTINCT category FROM books WHERE category IS NOT NULL AND category<>'' ORDER BY category")]
    edit_id = request.args.get('edit', type=int)
    edit_book = query_one('SELECT * FROM books WHERE id=?', (edit_id,)) if edit_id else None
    return render_template('books.html', books=books, cats=cats, q=q, cat=cat, st=st,
                           page=page, pages=pages, total=total, edit_book=edit_book,
                           import_report=session.pop('import_report', None))


@app.route('/books/<int:bid>/edit', methods=['GET', 'POST'])
@login_required
def book_edit(bid):
    if g.user['role'] != 'admin':
        abort(403)
    b = query_one('SELECT * FROM books WHERE id=?', (bid,))
    if not b:
        abort(404)
    if request.method == 'POST':
        try:
            price = int(request.form.get('price') or 0)
            stock = int(request.form.get('stock') or 0)
            total = int(request.form.get('total') or 0)
        except ValueError:
            flash('单价/库存/总量必须为数字', 'danger')
            return redirect(url_for('book_list'))
        if stock < 0 or total < 0 or stock > total:
            flash('库存必须 ≥0 且不大于总量', 'danger')
            return redirect(url_for('book_list'))
        isbn = request.form.get('isbn', '').strip()
        dup = query_one('SELECT id, title FROM books WHERE isbn=? AND id<>?', (isbn, bid))
        if isbn and dup:
            flash(f'ISBN {isbn} 已被《{dup["title"]}》占用', 'danger')
            return redirect(url_for('book_list'))
        execute("""UPDATE books SET title=?,isbn=?,author=?,category=?,publisher=?,
                   price=?,stock=?,total=?,status=? WHERE id=?""",
                (request.form.get('title', '').strip(), isbn,
                 request.form.get('author', '').strip(),
                 request.form.get('category', '').strip(),
                 request.form.get('publisher', '').strip(), price, stock, total,
                 request.form.get('status', b['status']), bid))
        log_action('编辑图书', f'《{request.form.get("title","").strip()}》资料已更新')
        flash('图书信息已保存', 'success')
        return redirect(url_for('book_list'))
    return redirect(url_for('book_list', edit=bid))


@app.route('/books/<int:bid>/delete', methods=['POST'])
@role_required('admin')
def book_delete(bid):
    b = query_one('SELECT * FROM books WHERE id=?', (bid,))
    if not b:
        abort(404)
    out = query_one("SELECT COUNT(*) c FROM borrows WHERE book_id=? AND return_date IS NULL", (bid,))['c']
    if out:
        flash(f'《{b["title"]}》尚有 {out} 册在借, 请先办理归还再删除', 'danger')
        return redirect(url_for('book_list'))
    execute('DELETE FROM books WHERE id=?', (bid,))
    execute('DELETE FROM reservations WHERE book_id=?', (bid,))
    log_action('删除图书', f'《{b["title"]}》已从馆藏移除')
    flash(f'《{b["title"]}》已删除', 'success')
    return redirect(url_for('book_list'))


@app.route('/books/export')
@login_required
def book_export():
    rows = query('SELECT * FROM books ORDER BY id')
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['书名', 'ISBN', '作者', '分类', '出版社', '单价(元)', '库存', '总量', '状态'])
    for b in rows:
        w.writerow([b['title'], b['isbn'], b['author'], b['category'], b['publisher'],
                    money(b['price']), b['stock'], b['total'], BOOK_STATUS.get(b['status'], b['status'])])
    log_action('导出图书', f'导出 {len(rows)} 条馆藏记录 (CSV)')
    payload = buf.getvalue().encode('utf-8-sig')
    fname = 'books_%s.csv' % datetime.now().strftime('%Y%m%d_%H%M')
    return Response(payload, mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename={fname}'})


def _parse_int(v, default=None):
    v = (v or '').strip()
    if v == '':
        return default
    return int(float(v))


@app.route('/books/import', methods=['POST'])
@role_required('admin')
def book_import():
    f = request.files.get('file')
    if not f or not f.filename:
        flash('请选择要导入的 CSV 文件', 'warning')
        return redirect(url_for('book_list'))
    raw = f.read()
    text = None
    for enc in ('utf-8-sig', 'utf-8', 'gbk'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        flash('文件编码无法识别, 请另存为 UTF-8 或 GBK 的 CSV', 'danger')
        return redirect(url_for('book_list'))

    ok, errors = 0, []
    rows = list(csv.reader(io.StringIO(text)))
    if rows and any(c.strip() in ('书名', 'title') for c in rows[0]):
        rows = rows[1:]
    for idx, row in enumerate(rows, start=1):
        line_no = idx + 1
        if not row or not any((c or '').strip() for c in row):
            continue
        row = (row + [''] * 9)[:9]
        title = row[0].strip()
        if not title:
            errors.append({'line': line_no, 'msg': '书名为空, 已跳过'})
            continue
        try:
            price_yuan = float((row[5] or '0').strip() or 0)
            stock = _parse_int(row[6], 0)
            total = _parse_int(row[7], stock)
        except ValueError:
            errors.append({'line': line_no, 'msg': f'「{title}」单价/库存/总量不是合法数字'})
            continue
        if stock is None or stock < 0:
            errors.append({'line': line_no, 'msg': f'「{title}」库存不能为负数'})
            continue
        if total is None or total < stock:
            errors.append({'line': line_no, 'msg': f'「{title}」总量({total})小于库存({stock})'})
            continue
        isbn = row[1].strip()
        if isbn and query_one('SELECT id FROM books WHERE isbn=?', (isbn,)):
            errors.append({'line': line_no, 'msg': f'「{title}」ISBN {isbn} 已存在, 已跳过'})
            continue
        execute("""INSERT INTO books(title,isbn,author,category,publisher,price,stock,total,status)
                   VALUES(?,?,?,?,?,?,?,?,'on_shelf')""",
                (title, isbn, row[2].strip(), row[3].strip(), row[4].strip(),
                 int(round(price_yuan * 100)), stock, total))
        ok += 1
    session['import_report'] = {'ok': ok, 'fail': len(errors), 'errors': errors[:50]}
    log_action('导入图书', f'CSV 批量导入: 成功 {ok} 条, 失败 {len(errors)} 条')
    if errors:
        flash(f'导入完成: 成功 {ok} 条, 失败 {len(errors)} 条, 详见下方错误报告', 'warning')
    else:
        flash(f'导入完成: 全部 {ok} 条记录成功入库', 'success')
    return redirect(url_for('book_list'))


# ================== 读者管理 ==================
@app.route('/readers', methods=['GET', 'POST'])
@role_required('admin', 'operator')
def reader_list():
    if request.method == 'POST':
        if g.user['role'] != 'admin':
            abort(403)
        card_no = request.form.get('card_no', '').strip()
        name = request.form.get('name', '').strip()
        if not card_no or not name:
            flash('学工号与姓名必填', 'warning')
            return redirect(url_for('reader_list'))
        if query_one('SELECT id FROM readers WHERE card_no=?', (card_no,)):
            flash(f'学工号 {card_no} 已存在', 'danger')
            return redirect(url_for('reader_list'))
        try:
            limit = int(request.form.get('borrow_limit') or DEFAULT_LIMIT)
        except ValueError:
            limit = DEFAULT_LIMIT
        execute("""INSERT INTO readers(card_no,name,phone,status,borrow_limit)
                   VALUES(?,?,?,?,?)""",
                (card_no, name, request.form.get('phone', '').strip(),
                 request.form.get('status', 'normal'), limit))
        log_action('新增读者', f'读者 {name}({card_no}) 建档, 借阅上限 {limit} 册')
        flash(f'读者 {name} 已建档', 'success')
        return redirect(url_for('reader_list'))

    q = request.args.get('q', '').strip()
    st = request.args.get('status', '').strip()
    where, args = [], []
    if q:
        where.append('(name LIKE ? OR card_no LIKE ? OR phone LIKE ?)')
        args += [f'%{q}%'] * 3
    if st:
        where.append('status=?')
        args.append(st)
    wsql = ('WHERE ' + ' AND '.join(where)) if where else ''
    readers = query(f"""SELECT r.*,
        (SELECT COUNT(*) FROM borrows b WHERE b.reader_id=r.id AND b.return_date IS NULL) out_cnt,
        (SELECT COUNT(*) FROM borrows b WHERE b.reader_id=r.id) total_cnt
        FROM readers r {wsql} ORDER BY r.id""", tuple(args))
    edit_id = request.args.get('edit', type=int)
    edit_reader = query_one('SELECT * FROM readers WHERE id=?', (edit_id,)) if edit_id else None
    return render_template('readers.html', readers=readers, q=q, st=st, edit_reader=edit_reader)


@app.route('/readers/<int:rid>/edit', methods=['GET', 'POST'])
@role_required('admin', 'operator')
def reader_edit(rid):
    r = query_one('SELECT * FROM readers WHERE id=?', (rid,))
    if not r:
        abort(404)
    if request.method == 'GET':
        return redirect(url_for('reader_list', edit=rid))
    if g.user['role'] != 'admin':
        abort(403)
    try:
        limit = int(request.form.get('borrow_limit') or DEFAULT_LIMIT)
    except ValueError:
        limit = DEFAULT_LIMIT
    execute('UPDATE readers SET name=?,phone=?,status=?,borrow_limit=? WHERE id=?',
            (request.form.get('name', '').strip() or r['name'],
             request.form.get('phone', '').strip(),
             request.form.get('status', 'normal'), limit, rid))
    log_action('编辑读者', f'读者 {request.form.get("name", "").strip()} 档案已更新')
    flash('读者档案已保存', 'success')
    return redirect(url_for('reader_list'))


@app.route('/readers/<int:rid>/delete', methods=['POST'])
@role_required('admin')
def reader_delete(rid):
    r = query_one('SELECT * FROM readers WHERE id=?', (rid,))
    if not r:
        abort(404)
    out = query_one('SELECT COUNT(*) c FROM borrows WHERE reader_id=? AND return_date IS NULL', (rid,))['c']
    if out:
        flash(f'读者 {r["name"]} 尚有 {out} 册未归还, 不能注销', 'danger')
        return redirect(url_for('reader_list'))
    execute('DELETE FROM readers WHERE id=?', (rid,))
    execute('DELETE FROM reservations WHERE reader_id=?', (rid,))
    log_action('注销读者', f'读者 {r["name"]}({r["card_no"]}) 已注销')
    flash(f'读者 {r["name"]} 已注销', 'success')
    return redirect(url_for('reader_list'))


# ================== 借阅管理 ==================
@app.route('/borrows', methods=['GET', 'POST'])
@role_required('admin', 'operator')
def borrow_list():
    if request.method == 'POST':
        book_id = request.form.get('book_id', type=int) or 0
        reader_id = request.form.get('reader_id', type=int) or 0
        book = query_one('SELECT * FROM books WHERE id=?', (book_id,))
        reader = query_one('SELECT * FROM readers WHERE id=?', (reader_id,))
        if not book or not reader:
            flash('请选择要借出的图书与读者', 'warning')
            return redirect(url_for('borrow_list'))
        if reader['status'] != 'normal':
            flash(f'读者「{reader["name"]}」当前为停借状态, 不能办理借书', 'danger')
        elif book['status'] == 'off_shelf':
            flash(f'《{book["title"]}》已下架, 不能借出', 'danger')
        elif book['status'] == 'lost':
            flash(f'《{book["title"]}》状态为挂失, 不能借出', 'danger')
        elif book['stock'] <= 0:
            flash(f'《{book["title"]}》可借库存为 0, 借出失败; 可为其办理预约', 'danger')
        else:
            cnt = query_one("""SELECT COUNT(*) c FROM borrows
                               WHERE reader_id=? AND status IN ('borrowed','overdue')""",
                            (reader_id,))['c']
            if cnt >= reader['borrow_limit']:
                flash(f'读者「{reader["name"]}」在借 {cnt} 册, 已达借阅上限 {reader["borrow_limit"]} 册', 'danger')
            else:
                bd = today()
                dd = bd + timedelta(days=BORROW_DAYS)
                with transaction() as conn:
                    conn.execute("""INSERT INTO borrows(book_id,reader_id,borrow_date,due_date,status)
                                    VALUES(?,?,?,?,'borrowed')""",
                                 (book_id, reader_id, dstr(bd), dstr(dd)))
                    conn.execute('UPDATE books SET stock=stock-1 WHERE id=?', (book_id,))
                log_action('借书', f'《{book["title"]}》借给 {reader["name"]}({reader["card_no"]}), 应还 {dstr(dd)}')
                flash(f'借书成功: 《{book["title"]}》→ {reader["name"]}, 应还日期 {dstr(dd)}', 'success')
        return redirect(url_for('borrow_list'))

    refresh_overdue()
    q = request.args.get('q', '').strip()
    st = request.args.get('status', '').strip()
    where, args = [], []
    if q:
        where.append('(b.title LIKE ? OR r.name LIKE ? OR r.card_no LIKE ?)')
        args += [f'%{q}%'] * 3
    if st:
        where.append('bo.status=?')
        args.append(st)
    wsql = ('WHERE ' + ' AND '.join(where)) if where else ''
    total = query_one(f"""SELECT COUNT(*) c FROM borrows bo
                          JOIN books b ON b.id=bo.book_id
                          JOIN readers r ON r.id=bo.reader_id {wsql}""", tuple(args))['c']
    per = 15
    pages = max(1, (total + per - 1) // per)
    page = min(max(1, request.args.get('page', 1, type=int)), pages)
    rows = query(f"""SELECT bo.*, b.title, b.isbn, r.name rname, r.card_no, r.phone
                     FROM borrows bo JOIN books b ON b.id=bo.book_id
                     JOIN readers r ON r.id=bo.reader_id
                     {wsql} ORDER BY bo.id DESC LIMIT {per} OFFSET {(page - 1) * per}""",
                 tuple(args))
    stat = {
        'out': query_one("SELECT COUNT(*) c FROM borrows WHERE return_date IS NULL")['c'],
        'overdue': query_one("SELECT COUNT(*) c FROM borrows WHERE status='overdue'")['c'],
        'today_b': query_one("SELECT COUNT(*) c FROM borrows WHERE date(borrow_date)=date('now','localtime')")['c'],
        'today_r': query_one("SELECT COUNT(*) c FROM borrows WHERE date(return_date)=date('now','localtime')")['c'],
    }
    readers = query("SELECT * FROM readers WHERE status='normal' ORDER BY card_no")
    books = query("SELECT * FROM books WHERE stock>0 AND status='on_shelf' ORDER BY title")
    return render_template('borrows.html', rows=rows, q=q, st=st, page=page, pages=pages,
                           total=total, stat=stat, readers=readers, books=books)


@app.route('/borrows/<int:bid>/return', methods=['POST'])
@role_required('admin', 'operator')
def borrow_return(bid):
    b = query_one('SELECT * FROM borrows WHERE id=?', (bid,))
    if not b:
        abort(404)
    book = query_one('SELECT * FROM books WHERE id=?', (b['book_id'],))
    reader = query_one('SELECT * FROM readers WHERE id=?', (b['reader_id'],))
    title = book['title'] if book else f'#{b["book_id"]}'
    if b['status'] == 'returned':
        flash('该借阅单已归还, 请勿重复还书', 'warning')
    elif b['status'] == 'lost':
        flash('该借阅单已挂失赔偿, 无法办理归还', 'warning')
    else:
        days = overdue_days(b['due_date'])
        fine = days * FINE_PER_DAY
        with transaction() as conn:
            conn.execute("""UPDATE borrows SET status='returned',
                            return_date=datetime('now','localtime'), fine=? WHERE id=?""", (fine, bid))
            conn.execute('UPDATE books SET stock=stock+1 WHERE id=?', (b['book_id'],))
        log_action('还书', f'《{title}》已归还' + (f', 逾期 {days} 天, 罚款 {money(fine)} 元' if days else ', 按期归还'))
        resv = query_one("""SELECT rv.id, rd.name rname FROM reservations rv
                            JOIN readers rd ON rd.id=rv.reader_id
                            WHERE rv.book_id=? AND rv.status='waiting' ORDER BY rv.id LIMIT 1""",
                         (b['book_id'],))
        tip = f', 逾期 {days} 天, 应缴罚款 {money(fine)} 元' if days else ', 按期归还无罚款'
        if resv:
            execute("UPDATE reservations SET status='ready' WHERE id=?", (resv['id'],))
            log_action('预约到书', f'《{title}》到书, 已通知预约读者 {resv["rname"]}')
            flash(f'还书成功: 《{title}》{tip}; 已通知预约读者 {resv["rname"]} 前来取书', 'success')
        else:
            flash(f'还书成功: 《{title}》{tip}', 'success')
    return redirect(request.form.get('back') or url_for('borrow_list'))


@app.route('/borrows/<int:bid>/renew', methods=['POST'])
@login_required
def borrow_renew(bid):
    b = query_one('SELECT * FROM borrows WHERE id=?', (bid,))
    if not b:
        abort(404)
    if g.user['role'] == 'user':
        r = current_reader()
        if not r or r['id'] != b['reader_id']:
            abort(403)
    book = query_one('SELECT * FROM books WHERE id=?', (b['book_id'],))
    title = book['title'] if book else f'#{b["book_id"]}'
    back = request.form.get('back') or (url_for('my') if g.user['role'] == 'user' else url_for('borrow_list'))
    if b['status'] == 'returned':
        flash('该借阅单已归还, 不能续借', 'warning')
    elif b['status'] == 'lost':
        flash('该借阅单已挂失, 不能续借', 'warning')
    elif b['status'] == 'overdue':
        flash(f'《{title}》已逾期 {overdue_days(b["due_date"])} 天, 请先归还并结清罚款', 'danger')
    elif b['renew_count'] >= MAX_RENEW:
        flash(f'《{title}》已续借过 {b["renew_count"]} 次, 每张借阅单仅可续借 {MAX_RENEW} 次', 'warning')
    else:
        try:
            cur_due = datetime.strptime(str(b['due_date'])[:10], '%Y-%m-%d').date()
        except ValueError:
            cur_due = today()
        new_due = cur_due + timedelta(days=RENEW_DAYS)
        execute('UPDATE borrows SET due_date=?, renew_count=renew_count+1 WHERE id=?',
                (dstr(new_due), bid))
        log_action('续借', f'《{title}》续借 {RENEW_DAYS} 天, 应还日期延长至 {dstr(new_due)}')
        flash(f'续借成功: 《{title}》应还日期延长至 {dstr(new_due)}', 'success')
    return redirect(back)


@app.route('/borrows/<int:bid>/lost', methods=['POST'])
@role_required('admin', 'operator')
def borrow_lost(bid):
    b = query_one('SELECT * FROM borrows WHERE id=?', (bid,))
    if not b:
        abort(404)
    book = query_one('SELECT * FROM books WHERE id=?', (b['book_id'],))
    title = book['title'] if book else f'#{b["book_id"]}'
    if b['status'] == 'returned':
        flash('该借阅单已归还, 不能挂失', 'warning')
    elif b['status'] == 'lost':
        flash('该借阅单已挂失赔偿, 请勿重复操作', 'warning')
    else:
        comp = book['price'] if book else 0
        with transaction() as conn:
            conn.execute("UPDATE borrows SET status='lost', fine=?, return_date=NULL WHERE id=?",
                         (comp, bid))
            conn.execute('UPDATE books SET total=MAX(total-1,0) WHERE id=?', (b['book_id'],))
            conn.execute("UPDATE books SET status='lost' WHERE id=? AND total<=0", (b['book_id'],))
        log_action('挂失赔偿', f'《{title}》挂失, 按书价赔偿 {money(comp)} 元')
        flash(f'已登记挂失: 《{title}》按书价赔偿 {money(comp)} 元, 请读者到前台缴纳', 'warning')
    return redirect(url_for('borrow_list'))


# ================== 预约管理 ==================
@app.route('/reservations')
@role_required('admin', 'operator')
def reservation_list():
    st = request.args.get('status', '').strip()
    where, args = [], []
    if st:
        where.append('rv.status=?')
        args.append(st)
    wsql = ('WHERE ' + ' AND '.join(where)) if where else ''
    rows = query(f"""SELECT rv.*, b.title, b.stock, b.isbn, r.name rname, r.card_no, r.phone
                     FROM reservations rv JOIN books b ON b.id=rv.book_id
                     JOIN readers r ON r.id=rv.reader_id {wsql}
                     ORDER BY CASE rv.status WHEN 'ready' THEN 0 WHEN 'waiting' THEN 1 ELSE 2 END,
                              rv.id DESC""", tuple(args))
    stat = {
        'waiting': query_one("SELECT COUNT(*) c FROM reservations WHERE status='waiting'")['c'],
        'ready': query_one("SELECT COUNT(*) c FROM reservations WHERE status='ready'")['c'],
        'done': query_one("SELECT COUNT(*) c FROM reservations WHERE status='done'")['c'],
    }
    return render_template('reservations.html', rows=rows, st=st, stat=stat,
                           readers=query("SELECT * FROM readers WHERE status='normal' ORDER BY card_no"),
                           books=query("SELECT * FROM books ORDER BY title"))


@app.route('/reserve', methods=['POST'])
@login_required
def reserve():
    book_id = request.form.get('book_id', type=int) or 0
    book = query_one('SELECT * FROM books WHERE id=?', (book_id,))
    back = request.form.get('back') or url_for('my')
    if g.user['role'] == 'user':
        r = current_reader()
    else:
        r = query_one('SELECT * FROM readers WHERE id=?', (request.form.get('reader_id', type=int) or 0,))
    if not book:
        flash('图书不存在', 'warning')
    elif not r:
        flash('未找到对应读者档案, 无法预约', 'warning')
    elif r['status'] != 'normal':
        flash(f'读者「{r["name"]}」为停借状态, 不能预约', 'danger')
    elif book['stock'] > 0:
        flash(f'《{book["title"]}》当前有可借库存, 可直接办理借书', 'info')
    elif query_one("""SELECT id FROM reservations WHERE book_id=? AND reader_id=?
                      AND status IN ('waiting','ready')""", (book_id, r['id'])):
        flash(f'已预约过《{book["title"]}》, 请勿重复预约', 'warning')
    else:
        execute("INSERT INTO reservations(book_id,reader_id,status) VALUES(?,?,'waiting')",
                (book_id, r['id']))
        pos = query_one("""SELECT COUNT(*) c FROM reservations WHERE book_id=? AND status='waiting'""",
                        (book_id,))['c']
        log_action('预约图书', f'{r["name"]} 预约《{book["title"]}》, 当前排队第 {pos} 位')
        flash(f'预约成功: 《{book["title"]}》, 当前排队第 {pos} 位, 到书后将优先通知', 'success')
    return redirect(back)


@app.route('/reservations/<int:rid>/cancel', methods=['POST'])
@login_required
def reservation_cancel(rid):
    rv = query_one('SELECT * FROM reservations WHERE id=?', (rid,))
    if not rv:
        abort(404)
    if g.user['role'] == 'user':
        r = current_reader()
        if not r or r['id'] != rv['reader_id']:
            abort(403)
    if rv['status'] in ('done',):
        flash('该预约已完成, 不能取消', 'warning')
    else:
        execute("UPDATE reservations SET status='cancelled' WHERE id=?", (rid,))
        flash('预约已取消', 'success')
    return redirect(request.form.get('back') or url_for('reservation_list'))


@app.route('/reservations/<int:rid>/fulfill', methods=['POST'])
@role_required('admin', 'operator')
def reservation_fulfill(rid):
    rv = query_one('SELECT * FROM reservations WHERE id=?', (rid,))
    if not rv:
        abort(404)
    book = query_one('SELECT * FROM books WHERE id=?', (rv['book_id'],))
    reader = query_one('SELECT * FROM readers WHERE id=?', (rv['reader_id'],))
    if rv['status'] == 'done':
        flash('该预约已办理完成', 'warning')
    elif not book or book['stock'] <= 0:
        flash('该书当前无在架库存, 无法办理借出', 'danger')
    elif not reader or reader['status'] != 'normal':
        flash('读者状态异常, 无法办理借出', 'danger')
    else:
        cnt = query_one("""SELECT COUNT(*) c FROM borrows WHERE reader_id=?
                           AND status IN ('borrowed','overdue')""", (reader['id'],))['c']
        if cnt >= reader['borrow_limit']:
            flash(f'读者「{reader["name"]}」已达借阅上限 {reader["borrow_limit"]} 册', 'danger')
        else:
            bd = today()
            dd = bd + timedelta(days=BORROW_DAYS)
            with transaction() as conn:
                conn.execute("""INSERT INTO borrows(book_id,reader_id,borrow_date,due_date,status)
                                VALUES(?,?,?,?,'borrowed')""",
                             (book['id'], reader['id'], dstr(bd), dstr(dd)))
                conn.execute('UPDATE books SET stock=stock-1 WHERE id=?', (book['id'],))
                conn.execute("UPDATE reservations SET status='done' WHERE id=?", (rid,))
            log_action('预约取书', f'{reader["name"]} 凭预约借出《{book["title"]}》, 应还 {dstr(dd)}')
            flash(f'预约取书成功: 《{book["title"]}》→ {reader["name"]}, 应还 {dstr(dd)}', 'success')
    return redirect(url_for('reservation_list'))


# ================== 读者视角 ==================
@app.route('/my')
@login_required
def my():
    refresh_overdue()
    readers = None
    if g.user['role'] == 'user':
        r = current_reader()
    else:
        rid = request.args.get('reader_id', type=int)
        readers = query('SELECT * FROM readers ORDER BY card_no')
        r = query_one('SELECT * FROM readers WHERE id=?', (rid,)) if rid else None
        if r is None and readers:
            r = None
    if not r:
        return render_template('my.html', reader=None, borrows=[], resv=[], readers=readers,
                               out_cnt=0, overdue_cnt=0, fine_unpaid=0)
    rows = query("""SELECT bo.*, b.title, b.author, b.isbn, b.category FROM borrows bo
                    JOIN books b ON b.id=bo.book_id WHERE bo.reader_id=?
                    ORDER BY bo.return_date IS NOT NULL, bo.id DESC""", (r['id'],))
    resv = query("""SELECT rv.*, b.title, b.author FROM reservations rv
                    JOIN books b ON b.id=rv.book_id WHERE rv.reader_id=?
                    ORDER BY rv.id DESC""", (r['id'],))
    out_cnt = query_one("""SELECT COUNT(*) c FROM borrows WHERE reader_id=?
                           AND return_date IS NULL AND status IN ('borrowed','overdue')""", (r['id'],))['c']
    overdue_cnt = query_one("SELECT COUNT(*) c FROM borrows WHERE reader_id=? AND status='overdue'",
                            (r['id'],))['c']
    fine_unpaid = query_one("""SELECT COALESCE(SUM(fine),0) c FROM borrows
                               WHERE reader_id=? AND fine_paid=0""", (r['id'],))['c']
    return render_template('my.html', reader=r, borrows=rows, resv=resv, readers=readers,
                           out_cnt=out_cnt, overdue_cnt=overdue_cnt, fine_unpaid=fine_unpaid)


# ================== 罚款管理 ==================
@app.route('/fines')
@role_required('admin', 'operator')
def fines():
    refresh_overdue()
    unpaid = query("""SELECT bo.*, b.title, r.name rname, r.card_no, r.phone
                      FROM borrows bo JOIN books b ON b.id=bo.book_id
                      JOIN readers r ON r.id=bo.reader_id
                      WHERE bo.fine>0 AND bo.fine_paid=0 ORDER BY bo.fine DESC, bo.id DESC""")
    paid = query("""SELECT bo.*, b.title, r.name rname, r.card_no FROM borrows bo
                    JOIN books b ON b.id=bo.book_id JOIN readers r ON r.id=bo.reader_id
                    WHERE bo.fine_paid=1 ORDER BY bo.id DESC LIMIT 50""")
    overdue_list = query("""SELECT bo.*, b.title, r.name rname, r.card_no, r.phone
                            FROM borrows bo JOIN books b ON b.id=bo.book_id
                            JOIN readers r ON r.id=bo.reader_id
                            WHERE bo.status='overdue' ORDER BY bo.due_date""")
    stat = {
        'unpaid_sum': sum(x['fine'] for x in unpaid),
        'unpaid_cnt': len(unpaid),
        'paid_sum': query_one('SELECT COALESCE(SUM(fine),0) c FROM borrows WHERE fine_paid=1')['c'],
        'overdue_cnt': len(overdue_list),
    }
    return render_template('fines.html', unpaid=unpaid, paid=paid,
                           overdue_list=overdue_list, stat=stat)


@app.route('/fines/<int:bid>/pay', methods=['POST'])
@role_required('admin', 'operator')
def fine_pay(bid):
    b = query_one('SELECT * FROM borrows WHERE id=?', (bid,))
    if not b:
        abort(404)
    if b['fine'] <= 0:
        flash('该借阅单没有待缴罚款', 'warning')
    elif b['fine_paid']:
        flash('该罚款已结清, 请勿重复收款', 'warning')
    else:
        execute('UPDATE borrows SET fine_paid=1 WHERE id=?', (bid,))
        log_action('罚款结算', f'借阅单 #{bid} 罚款 {money(b["fine"])} 元已缴清')
        flash(f'收款成功: 罚款 {money(b["fine"])} 元已结清', 'success')
    return redirect(url_for('fines'))


# ================== 操作日志 ==================
@app.route('/logs')
@role_required('admin')
def logs():
    q = request.args.get('q', '').strip()
    act = request.args.get('action', '').strip()
    where, args = [], []
    if q:
        where.append('(l.detail LIKE ? OR u.display_name LIKE ?)')
        args += [f'%{q}%'] * 2
    if act:
        where.append('l.action=?')
        args.append(act)
    wsql = ('WHERE ' + ' AND '.join(where)) if where else ''
    total = query_one(f"""SELECT COUNT(*) c FROM logs l LEFT JOIN users u ON u.id=l.user_id {wsql}""",
                      tuple(args))['c']
    per = 30
    pages = max(1, (total + per - 1) // per)
    page = min(max(1, request.args.get('page', 1, type=int)), pages)
    rows = query(f"""SELECT l.*, u.display_name, u.role FROM logs l
                     LEFT JOIN users u ON u.id=l.user_id {wsql}
                     ORDER BY l.id DESC LIMIT {per} OFFSET {(page - 1) * per}""", tuple(args))
    actions = [r['action'] for r in query('SELECT DISTINCT action FROM logs ORDER BY action')]
    return render_template('logs.html', rows=rows, q=q, act=act, page=page, pages=pages,
                           total=total, actions=actions)


# ================== 统计报表 ==================
@app.route('/stats')
@login_required
def stats():
    m = dashboard_metrics()
    # 近 6 个月借阅趋势
    raw = query("""SELECT substr(borrow_date,1,7) ym, COUNT(*) c FROM borrows
                   GROUP BY ym ORDER BY ym""")
    trend_map = {r['ym']: r['c'] for r in raw}
    months, series = [], []
    cur = date.today().replace(day=1)
    for i in range(5, -1, -1):
        y = cur.year
        mo = cur.month - i
        while mo <= 0:
            mo += 12
            y -= 1
        key = f'{y:04d}-{mo:02d}'
        months.append(f'{mo}月')
        series.append(trend_map.get(key, 0))
    # 借阅分类占比
    cat_rows = query("""SELECT COALESCE(NULLIF(b.category,''),'未分类') cat, COUNT(*) c
                        FROM borrows bo JOIN books b ON b.id=bo.book_id
                        GROUP BY cat ORDER BY c DESC""")
    cats = [{'name': r['cat'], 'value': r['c']} for r in cat_rows]
    # 借阅排行
    rank = query("""SELECT b.title, b.author, b.category, COUNT(bo.id) c
                    FROM borrows bo JOIN books b ON b.id=bo.book_id
                    GROUP BY b.id ORDER BY c DESC, b.id LIMIT 10""")
    # 读者活跃榜
    top_readers = query("""SELECT r.name, r.card_no, COUNT(bo.id) c,
                           COALESCE(SUM(bo.fine),0) fine
                           FROM borrows bo JOIN readers r ON r.id=bo.reader_id
                           GROUP BY r.id ORDER BY c DESC LIMIT 8""")
    # 分类馆藏
    stock_rows = query("""SELECT COALESCE(NULLIF(category,''),'未分类') cat,
                          COUNT(*) kinds, COALESCE(SUM(total),0) copies,
                          COALESCE(SUM(stock),0) avail
                          FROM books GROUP BY cat ORDER BY copies DESC""")
    return render_template('stats.html', m=m, months=months, series=series, cats=cats,
                           rank=rank, top_readers=top_readers, stock_rows=stock_rows)


# ---------------- 错误页 ----------------
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
