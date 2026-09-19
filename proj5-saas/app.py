# -*- coding: utf-8 -*-
"""
图书馆云 SaaS 平台 —— 多租户（Multi-Tenant）SaaS 演示系统。

核心设计
  1) 行级租户隔离：所有业务表带 tenant_id，业务查询一律走 q_tenant()/ex_tenant()
     helper，helper 强制把当前 session 的 tenant_id 注入 SQL 的第一个参数位；
     查不到就 404（而不是 403），避免泄露「该资源是否存在」。
  2) 套餐与配额：用户数 / 图书数 / API 月调用量，超限拦截（页面 flash + API 429）。
  3) API Key 鉴权 + 令牌桶（滑动窗口）限流，超限返回 429 并带 Retry-After。
  4) 审计日志：登录、建租户、改配额、API 调用等关键动作全记录。
  5) 租户停用：登录与 API 双向拦截。

仅依赖 flask + Python 标准库；前端资源全部本地（static/）。
金额一律用整数「分」；时间用 'YYYY-MM-DD HH:MM:SS'（localtime）字符串。
"""
import secrets
from datetime import datetime
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, g, abort, jsonify)
from werkzeug.security import generate_password_hash, check_password_hash

import db as database
from db import query, query_one, execute, execute_many, transaction

# ================== 项目配置 ==================
APP_NAME = '图书馆云 SaaS 平台'
APP_SUBTITLE = 'Multi-Tenant SaaS'
PORT = 8005

PLATFORM_NAV = [
    ('platform',  '平台概览', '/platform', '📊'),
    ('tenants',   '租户管理', '/tenants',  '🏢'),
    ('plans',     '套餐管理', '/plans',    '💳'),
    ('audit',     '审计日志', '/audit',    '📜'),
    ('isolation', '隔离方案', '/isolation', '🧩'),
]
TENANT_NAV = [
    ('tenant_dash',  '租户工作台', '/',            '🏠'),
    ('books',        '图书管理',   '/books',       '📚'),
    ('borrows',      '借阅管理',   '/borrows',     '🔄'),
    ('api_mgmt',     'API 管理',   '/api-mgmt',    '🔑'),
    ('quota',        '配额用量',   '/quota',       '📈'),
    ('tenant_users', '本租户用户', '/tenant-users', '👥'),
]
TENANT_NAV_MEMBER = ['tenant_dash', 'books', 'borrows', 'quota']

NAV = PLATFORM_NAV  # 兼容脚手架默认值；实际按角色动态注入
ROLE_LABELS = {
    'platform_admin': '平台管理员',
    'tenant_admin': '租户管理员',
    'tenant_user': '租户成员',
}
TENANT_STATUS_LABELS = {'active': '正常', 'disabled': '已停用'}
TENANT_STATUS_BADGE = {'active': 'text-bg-success', 'disabled': 'text-bg-danger'}
BORROW_STATUS_LABELS = {'borrowed': '借阅中', 'returned': '已归还', 'overdue': '逾期'}
# =============================================

app = Flask(__name__)
app.secret_key = 'saas-demo-dev-secret-key'
database.init_app(app)
try:
    app.json.ensure_ascii = False          # Flask >= 2.3
except Exception:
    app.config['JSON_AS_ASCII'] = False    # 老版本兜底


# ==================== 数据模型 ====================
def ensure_schema():
    execute('''CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        display_name TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user',
        tenant_id INTEGER DEFAULT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    cols = [r['name'] for r in query('PRAGMA table_info(users)')]
    if 'tenant_id' not in cols:                      # 老库平滑升级
        execute('ALTER TABLE users ADD COLUMN tenant_id INTEGER DEFAULT NULL')

    execute('''CREATE TABLE IF NOT EXISTS plans(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        user_quota INTEGER NOT NULL DEFAULT 10,
        book_quota INTEGER NOT NULL DEFAULT 100,
        api_monthly_quota INTEGER NOT NULL DEFAULT 1000,
        api_rate_per_min INTEGER NOT NULL DEFAULT 60,
        price_month INTEGER NOT NULL DEFAULT 0,
        sort INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')

    execute('''CREATE TABLE IF NOT EXISTS tenants(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        contact TEXT,
        phone TEXT,
        plan_id INTEGER,
        status TEXT NOT NULL DEFAULT 'active',
        expire_date TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')

    # ↓↓↓ 业务表全部带 tenant_id —— 行级隔离的地基 ↓↓↓
    execute('''CREATE TABLE IF NOT EXISTS books(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        author TEXT,
        category TEXT,
        stock INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'available',
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')

    execute('''CREATE TABLE IF NOT EXISTS borrows(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL,
        book_id INTEGER NOT NULL,
        reader_name TEXT NOT NULL,
        borrow_date TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        return_date TEXT,
        status TEXT NOT NULL DEFAULT 'borrowed'
    )''')

    execute('''CREATE TABLE IF NOT EXISTS api_keys(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL,
        api_key TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')

    execute('''CREATE TABLE IF NOT EXISTS api_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER,
        api_key TEXT,
        path TEXT,
        status_code INTEGER,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')

    execute('''CREATE TABLE IF NOT EXISTS audit_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        action TEXT,
        detail TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')

    execute('''CREATE TABLE IF NOT EXISTS rate_buckets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL,
        bucket_key TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        window_start TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        UNIQUE(tenant_id, bucket_key)
    )''')

    for stmt in (
        'CREATE INDEX IF NOT EXISTS idx_books_tenant   ON books(tenant_id)',
        'CREATE INDEX IF NOT EXISTS idx_borrows_tenant ON borrows(tenant_id)',
        'CREATE INDEX IF NOT EXISTS idx_users_tenant   ON users(tenant_id)',
        'CREATE INDEX IF NOT EXISTS idx_apilog_tenant  ON api_logs(tenant_id, created_at)',
        'CREATE INDEX IF NOT EXISTS idx_audit_time     ON audit_logs(created_at)',
    ):
        execute(stmt)


with app.app_context():
    ensure_schema()


# ==================== 通用工具 ====================
def now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def month_start():
    return datetime.now().strftime('%Y-%m-01 00:00:00')


@app.template_filter('yuan')
def yuan(cents):
    try:
        return '%.2f' % (int(cents) / 100.0)
    except (TypeError, ValueError):
        return '0.00'


def pct(used, quota):
    if not quota or quota <= 0:
        return 0
    return min(100, int(round(used * 100.0 / quota)))


def bar_class(p):
    if p >= 100:
        return 'bg-danger'
    if p >= 80:
        return 'bg-warning'
    return 'bg-success'


def mask_key(k):
    """对外展示/落库一律用掩码，避免凭据明文外泄。"""
    if not k:
        return ''
    return (k[:8] + '****') if len(k) > 8 else '****'


def gen_api_key():
    return 'lsk_' + secrets.token_hex(16)


def audit(action, detail=''):
    try:
        execute('INSERT INTO audit_logs(user_id,username,action,detail) VALUES(?,?,?,?)',
                (session.get('uid'), session.get('uname') or 'anonymous', action, detail))
    except Exception:
        pass


# ==================== 租户隔离核心 helper ====================
def current_tenant():
    """当前登录租户（含套餐配额），平台管理员返回 None。"""
    tid = session.get('tenant_id')
    if not tid:
        return None
    return query_one('''SELECT t.*, p.name AS plan_name, p.user_quota, p.book_quota,
                               p.api_monthly_quota, p.api_rate_per_min, p.price_month
                        FROM tenants t LEFT JOIN plans p ON p.id = t.plan_id
                        WHERE t.id = ?''', (tid,))


def require_tenant_id():
    """取当前租户 id；非租户会话直接 404（不暴露「这是权限问题」）。"""
    tid = session.get('tenant_id')
    if not tid:
        abort(404)
    return tid


def q_tenant(sql, args=(), one=False):
    """租户强制过滤查询。

    约定：SQL 中用 {T} 占位当前租户 id，且必须出现在参数序列第一位，例如
        q_tenant('SELECT * FROM books WHERE tenant_id={T} AND title LIKE ?', ('%x%',))
    helper 会把 {T} 换成 ? 并把 session['tenant_id'] 作为第一个参数注入 ——
    这样「漏写 tenant_id 过滤」在语法上就写不出来。
    """
    tid = require_tenant_id()
    sql = sql.replace('{T}', '?')
    a = (tid,) + tuple(args or ())
    return query_one(sql, a) if one else query(sql, a)


def ex_tenant(sql, args=()):
    """租户强制过滤写操作，同样使用 {T} 占位（用于 INSERT/UPDATE/DELETE）。"""
    tid = require_tenant_id()
    return execute(sql.replace('{T}', '?'), (tid,) + tuple(args or ()))


# ==================== 配额与用量 ====================
def tenant_usage(tid):
    return {
        'users': query_one('SELECT COUNT(*) c FROM users WHERE tenant_id=?', (tid,))['c'],
        'books': query_one('SELECT COUNT(*) c FROM books WHERE tenant_id=?', (tid,))['c'],
        'api_calls': query_one('SELECT COUNT(*) c FROM api_logs WHERE tenant_id=? AND created_at>=?',
                               (tid, month_start()))['c'],
    }


def quota_state(tenant):
    """返回 (用量 dict, 进度 dict, 是否超限 dict)。"""
    tid = tenant['id']
    u = tenant_usage(tid)
    q = {'users': tenant['user_quota'] or 0,
         'books': tenant['book_quota'] or 0,
         'api_calls': tenant['api_monthly_quota'] or 0}
    p = {k: pct(u[k], q[k]) for k in q}
    over = {k: (q[k] > 0 and u[k] >= q[k]) for k in q}
    return u, p, over


def check_quota(tid, kind):
    """配额校验：返回 (True, None) 或 (False, 提示文案)。"""
    t = query_one('''SELECT t.id, p.user_quota, p.book_quota, p.api_monthly_quota, p.name AS plan_name
                     FROM tenants t LEFT JOIN plans p ON p.id=t.plan_id WHERE t.id=?''', (tid,))
    if not t:
        return False, '租户不存在'
    col = {'users': 'user_quota', 'books': 'book_quota', 'api': 'api_monthly_quota'}[kind]
    quota = t[col] or 0
    if quota <= 0:
        return True, None
    used = tenant_usage(tid)[kind if kind != 'api' else 'api_calls']
    if used >= quota:
        label = {'users': '用户数', 'books': '图书数', 'api': 'API 月调用量'}[kind]
        return False, ('%s 配额已用尽（%d/%d），当前套餐「%s」，请升级套餐后再试'
                       % (label, used, quota, t['plan_name'] or '未配置'))
    return True, None


# ==================== 认证与权限 ====================
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'uid' not in session:
            flash('请先登录', 'warning')
            return redirect(url_for('login', next=request.path))
        g.user = query_one('SELECT * FROM users WHERE id=?', (session['uid'],))
        if not g.user:
            session.clear()
            flash('账号已失效，请重新登录', 'warning')
            return redirect(url_for('login'))
        # 租户停用 → 会话立即失效（边界场景 4）
        if g.user['role'] != 'platform_admin':
            t = current_tenant()
            if not t or t['status'] != 'active':
                session.clear()
                flash('所属租户已被停用，请联系平台管理员', 'danger')
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


def platform_required(f):
    return role_required('platform_admin')(f)


def tenant_required(f):
    """租户业务页：平台管理员一律 403（只读越权控制，边界场景 5）。"""
    return role_required('tenant_admin', 'tenant_user')(f)


def tenant_admin_required(f):
    return role_required('tenant_admin')(f)


@app.context_processor
def inject_globals():
    user = getattr(g, 'user', None)
    nav = []
    tenant = None
    if user:
        if user['role'] == 'platform_admin':
            nav = PLATFORM_NAV
        else:
            tenant = current_tenant()
            nav = [n for n in TENANT_NAV
                   if user['role'] == 'tenant_admin' or n[0] in TENANT_NAV_MEMBER]
    return {
        'APP_NAME': APP_NAME, 'APP_SUBTITLE': APP_SUBTITLE,
        'NAV': nav, 'ROLE_LABELS': ROLE_LABELS,
        'current_user': user, 'current_tenant': tenant,
        'request_path': request.path,
        'TENANT_STATUS_LABELS': TENANT_STATUS_LABELS,
        'TENANT_STATUS_BADGE': TENANT_STATUS_BADGE,
        'BORROW_STATUS_LABELS': BORROW_STATUS_LABELS,
        'now_str': now_str(),
    }


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        tid_raw = (request.form.get('tenant_id') or '0').strip()
        u = query_one('SELECT * FROM users WHERE username=?', (username,))
        if not (u and check_password_hash(u['password_hash'], password)):
            flash('用户名或密码错误', 'danger')
            return render_template('login.html', tenants=active_tenants())

        if u['role'] == 'platform_admin':
            session.clear()
            session['uid'] = u['id']
            session['uname'] = u['username']
            session['tenant_id'] = None
            session['role'] = u['role']
            audit('登录平台后台', '平台管理员登录')
            flash('欢迎回来，%s' % u['display_name'], 'success')
            return redirect(url_for('platform_home'))

        # ---- 租户侧登录：校验租户选择 / 状态 / 归属 ----
        try:
            tid = int(tid_raw)
        except ValueError:
            tid = 0
        if not tid:
            flash('请选择所属租户', 'warning')
            return render_template('login.html', tenants=active_tenants())
        t = query_one('SELECT * FROM tenants WHERE id=?', (tid,))
        if not t:
            flash('所选租户不存在', 'danger')
        elif t['status'] != 'active':
            audit('登录被拦截', '租户「%s」已停用，账号 %s 登录被拒' % (t['name'], username))
            flash('租户「%s」已停用，无法登录' % t['name'], 'danger')
        elif u['tenant_id'] != tid:
            flash('该账号不属于所选租户', 'danger')
        else:
            session.clear()
            session['uid'] = u['id']
            session['uname'] = u['username']
            session['tenant_id'] = tid
            session['role'] = u['role']
            audit('登录租户', '进入租户「%s」' % t['name'])
            flash('欢迎回来，%s' % u['display_name'], 'success')
            return redirect(request.args.get('next') or url_for('index'))
    return render_template('login.html', tenants=active_tenants())


def active_tenants():
    return query('''SELECT t.id, t.name, t.status, p.name AS plan_name
                    FROM tenants t LEFT JOIN plans p ON p.id=t.plan_id
                    ORDER BY t.id''')


@app.route('/logout')
def logout():
    if session.get('uid'):
        audit('退出登录', '')
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
            audit('修改密码', '用户 %s 修改了登录密码' % g.user['username'])
            flash('密码已修改', 'success')
            return redirect(url_for('index'))
    return render_template('profile.html')


@app.route('/')
@login_required
def index():
    if g.user['role'] == 'platform_admin':
        return redirect(url_for('platform_home'))
    return redirect(url_for('tenant_dash'))


# ==================== 平台后台 ====================
@app.route('/platform')
@platform_required
def platform_home():
    tids = [r['id'] for r in query('SELECT id FROM tenants')]
    total_tenants = len(tids)
    active_tenants_n = query_one("SELECT COUNT(*) c FROM tenants WHERE status='active'")['c']
    disabled_n = total_tenants - active_tenants_n
    new_this_month = query_one("SELECT COUNT(*) c FROM tenants WHERE created_at>=?", (month_start(),))['c']
    api_total = query_one('SELECT COUNT(*) c FROM api_logs')['c']
    api_month = query_one('SELECT COUNT(*) c FROM api_logs WHERE created_at>=?', (month_start(),))['c']
    over_quota = 0
    rows = []
    for r in query('''SELECT t.id, t.name, t.status, t.expire_date, t.created_at,
                             p.name AS plan_name, p.user_quota, p.book_quota,
                             p.api_monthly_quota, p.api_rate_per_min, p.price_month
                      FROM tenants t LEFT JOIN plans p ON p.id=t.plan_id ORDER BY t.id'''):
        u = tenant_usage(r['id'])
        pu = pct(u['users'], r['user_quota'])
        pb = pct(u['books'], r['book_quota'])
        pa = pct(u['api_calls'], r['api_monthly_quota'])
        hit = max(pu, pb, pa) >= 100
        if hit:
            over_quota += 1
        rows.append({**r, 'usage': u, 'p_users': pu, 'p_books': pb, 'p_api': pa, 'over': hit})

    # 图表数据
    plan_dist = query('''SELECT COALESCE(p.name,'未配置') name, COUNT(t.id) c
                         FROM tenants t LEFT JOIN plans p ON p.id=t.plan_id
                         GROUP BY p.id ORDER BY p.sort''')
    api_trend = []
    for i in range(6, -1, -1):
        day = datetime.now().date()
        from datetime import timedelta
        d = (day - timedelta(days=i)).strftime('%Y-%m-%d')
        c = query_one('SELECT COUNT(*) c FROM api_logs WHERE substr(created_at,1,10)=?', (d,))['c']
        api_trend.append({'day': d[5:], 'count': c})
    action_dist = query('''SELECT action, COUNT(*) c FROM audit_logs
                           GROUP BY action ORDER BY c DESC LIMIT 6''')

    stats = {
        '租户总数': total_tenants,
        '正常租户': active_tenants_n,
        '已停用': disabled_n,
        '本月新增': new_this_month,
        'API 调用总量': api_total,
        '本月 API 调用': api_month,
        '配额超限租户': over_quota,
    }
    return render_template('platform.html', stats=stats, rows=rows,
                           plan_dist=plan_dist, api_trend=api_trend,
                           action_dist=action_dist)


@app.route('/tenants')
@platform_required
def tenants():
    plans = query('SELECT * FROM plans ORDER BY sort, id')
    rows = []
    for r in query('''SELECT t.*, p.name AS plan_name, p.user_quota, p.book_quota,
                             p.api_monthly_quota, p.api_rate_per_min, p.price_month
                      FROM tenants t LEFT JOIN plans p ON p.id=t.plan_id ORDER BY t.id'''):
        u = tenant_usage(r['id'])
        key = query_one('SELECT api_key FROM api_keys WHERE tenant_id=? ORDER BY id LIMIT 1', (r['id'],))
        rows.append({**r, 'usage': u, 'p_users': pct(u['users'], r['user_quota']),
                     'p_books': pct(u['books'], r['book_quota']),
                     'p_api': pct(u['api_calls'], r['api_monthly_quota']),
                     'key_mask': mask_key(key['api_key']) if key else '未生成'})
    return render_template('tenants.html', rows=rows, plans=plans)


@app.route('/tenants/add', methods=['POST'])
@platform_required
def tenant_add():
    name = request.form.get('name', '').strip()
    if not name:
        flash('租户名称必填', 'warning')
        return redirect(url_for('tenants'))
    if query_one('SELECT id FROM tenants WHERE name=?', (name,)):
        flash('同名租户已存在', 'danger')
        return redirect(url_for('tenants'))
    plan_id = request.form.get('plan_id') or None
    expire = request.form.get('expire_date') or None
    with transaction() as conn:
        cur = conn.execute('''INSERT INTO tenants(name,contact,phone,plan_id,status,expire_date)
                              VALUES(?,?,?,?, 'active', ?)''',
                           (name, request.form.get('contact', '').strip(),
                            request.form.get('phone', '').strip(), plan_id, expire))
        tid = cur.lastrowid
        conn.execute('INSERT INTO api_keys(tenant_id,api_key,status) VALUES(?,?, \'active\')',
                     (tid, gen_api_key()))
    # 可选：同时开通租户管理员
    adm_u = request.form.get('admin_username', '').strip()
    adm_p = request.form.get('admin_password', '').strip()
    extra = ''
    if adm_u and adm_p:
        if query_one('SELECT id FROM users WHERE username=?', (adm_u,)):
            extra = '；管理员账号 %s 已存在，未创建' % adm_u
        else:
            execute('''INSERT INTO users(username,password_hash,display_name,role,tenant_id)
                       VALUES(?,?,?, 'tenant_admin', ?)''',
                    (adm_u, generate_password_hash(adm_p),
                     request.form.get('admin_name', '').strip() or (name + '管理员'), tid))
            extra = '；已开通管理员 %s' % adm_u
    audit('创建租户', '新建租户「%s」并自动生成 API Key%s' % (name, extra))
    flash('租户「%s」已创建，API Key 自动生成%s' % (name, extra), 'success')
    return redirect(url_for('tenants'))


@app.route('/tenants/<int:tid>/edit', methods=['POST'])
@platform_required
def tenant_edit(tid):
    t = query_one('SELECT * FROM tenants WHERE id=?', (tid,))
    if not t:
        abort(404)
    old_plan = t['plan_id']
    new_plan = request.form.get('plan_id') or None
    execute('''UPDATE tenants SET name=?, contact=?, phone=?, plan_id=?, expire_date=? WHERE id=?''',
            (request.form.get('name', '').strip() or t['name'],
             request.form.get('contact', '').strip(),
             request.form.get('phone', '').strip(),
             new_plan, request.form.get('expire_date') or None, tid))
    detail = '更新租户「%s」资料' % t['name']
    if str(old_plan) != str(new_plan):
        p = query_one('SELECT name FROM plans WHERE id=?', (new_plan,))
        detail += '，套餐由「%s」调整为「%s」' % (
            (query_one('SELECT name FROM plans WHERE id=?', (old_plan,)) or {}).get('name', '未配置'),
            (p or {}).get('name', '未配置'))
    audit('编辑租户', detail)
    flash('租户信息已保存', 'success')
    return redirect(url_for('tenants'))


@app.route('/tenants/<int:tid>/toggle', methods=['POST'])
@platform_required
def tenant_toggle(tid):
    t = query_one('SELECT * FROM tenants WHERE id=?', (tid,))
    if not t:
        abort(404)
    new_status = 'disabled' if t['status'] == 'active' else 'active'
    execute('UPDATE tenants SET status=? WHERE id=?', (new_status, tid))
    if new_status == 'disabled':
        execute('UPDATE api_keys SET status=\'disabled\' WHERE tenant_id=?', (tid,))
    else:
        execute('UPDATE api_keys SET status=\'active\' WHERE tenant_id=?', (tid,))
    audit('租户状态变更', '租户「%s」：%s → %s' % (
        t['name'], TENANT_STATUS_LABELS[t['status']], TENANT_STATUS_LABELS[new_status]))
    flash('租户「%s」已%s' % (t['name'], TENANT_STATUS_LABELS[new_status]),
          'warning' if new_status == 'disabled' else 'success')
    return redirect(url_for('tenants'))


@app.route('/tenants/<int:tid>/rotate-key', methods=['POST'])
@platform_required
def tenant_rotate_key(tid):
    t = query_one('SELECT * FROM tenants WHERE id=?', (tid,))
    if not t:
        abort(404)
    new_key = gen_api_key()
    execute('UPDATE api_keys SET api_key=?, status=? WHERE tenant_id=?',
            (new_key, 'active' if t['status'] == 'active' else 'disabled', tid))
    audit('轮换 API Key', '租户「%s」的 API Key 已重置（新 Key 掩码 %s）' % (t['name'], mask_key(new_key)))
    flash('租户「%s」的 API Key 已轮换' % t['name'], 'success')
    return redirect(url_for('tenants'))


@app.route('/plans')
@platform_required
def plans():
    rows = []
    for p in query('SELECT * FROM plans ORDER BY sort, id'):
        cnt = query_one('SELECT COUNT(*) c FROM tenants WHERE plan_id=?', (p['id'],))['c']
        rows.append({**p, 'tenant_count': cnt})
    return render_template('plans.html', rows=rows)


@app.route('/plans/add', methods=['POST'])
@platform_required
def plan_add():
    name = request.form.get('name', '').strip()
    if not name:
        flash('套餐名称必填', 'warning')
        return redirect(url_for('plans'))
    execute('''INSERT INTO plans(name,user_quota,book_quota,api_monthly_quota,api_rate_per_min,price_month,sort)
               VALUES(?,?,?,?,?,?,?)''',
            (name, int(request.form.get('user_quota') or 0), int(request.form.get('book_quota') or 0),
             int(request.form.get('api_monthly_quota') or 0), int(request.form.get('api_rate_per_min') or 60),
             int(round(float(request.form.get('price_yuan') or 0) * 100)), int(request.form.get('sort') or 99)))
    audit('新增套餐', '创建套餐「%s」' % name)
    flash('套餐「%s」已创建' % name, 'success')
    return redirect(url_for('plans'))


@app.route('/plans/<int:pid>/edit', methods=['POST'])
@platform_required
def plan_edit(pid):
    p = query_one('SELECT * FROM plans WHERE id=?', (pid,))
    if not p:
        abort(404)
    execute('''UPDATE plans SET name=?, user_quota=?, book_quota=?, api_monthly_quota=?,
                                 api_rate_per_min=?, price_month=?, sort=? WHERE id=?''',
            (request.form.get('name', '').strip() or p['name'],
             int(request.form.get('user_quota') or 0), int(request.form.get('book_quota') or 0),
             int(request.form.get('api_monthly_quota') or 0), int(request.form.get('api_rate_per_min') or 60),
             int(round(float(request.form.get('price_yuan') or 0) * 100)),
             int(request.form.get('sort') or p['sort']), pid))
    audit('修改套餐配额', '套餐「%s」：用户 %s/%s，图书 %s/%s，API 月 %s/%s 次，限流 %s 次/分'
          % (p['name'], request.form.get('user_quota'), p['user_quota'],
             request.form.get('book_quota'), p['book_quota'],
             request.form.get('api_monthly_quota'), p['api_monthly_quota'],
             request.form.get('api_rate_per_min')))
    flash('套餐「%s」配额已更新，对全部租户即时生效' % p['name'], 'success')
    return redirect(url_for('plans'))


@app.route('/audit')
@platform_required
def audit_list():
    kw = request.args.get('q', '').strip()
    try:
        limit = min(300, max(10, int(request.args.get('limit') or 80)))
    except ValueError:
        limit = 80
    if kw:
        rows = query('''SELECT * FROM audit_logs WHERE action LIKE ? OR detail LIKE ? OR username LIKE ?
                        ORDER BY id DESC LIMIT ?''', ('%' + kw + '%',) * 3 + (limit,))
    else:
        rows = query('SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?', (limit,))
    total = query_one('SELECT COUNT(*) c FROM audit_logs')['c']
    actions = query('SELECT action, COUNT(*) c FROM audit_logs GROUP BY action ORDER BY c DESC')
    return render_template('audit.html', rows=rows, kw=kw, limit=limit,
                           total=total, actions=actions)


# ==================== 租户工作台 ====================
@app.route('/dashboard')
@tenant_required
def tenant_dash():
    t = current_tenant()
    if not t:
        abort(404)
    u, p, over = quota_state(t)
    recent_borrows = q_tenant('''SELECT b.*, k.title FROM borrows b
                                 LEFT JOIN books k ON k.id=b.book_id AND k.tenant_id=b.tenant_id
                                 WHERE b.tenant_id={T} ORDER BY b.id DESC LIMIT 6''')
    recent_logs = q_tenant('''SELECT * FROM api_logs WHERE tenant_id={T} ORDER BY id DESC LIMIT 6''')
    cat_dist = q_tenant('''SELECT COALESCE(category,'未分类') name, COUNT(*) c FROM books
                           WHERE tenant_id={T} GROUP BY category ORDER BY c DESC''')
    stats = {'本租户图书': u['books'], '本租户用户': u['users'], '本月 API 调用': u['api_calls'],
             '在借记录': q_tenant("SELECT COUNT(*) c FROM borrows WHERE tenant_id={T} AND status='borrowed'", one=True)['c']}
    return render_template('tenant_dash.html', t=t, usage=u, pcts=p, over=over,
                           recent_borrows=recent_borrows, recent_logs=recent_logs,
                           cat_dist=cat_dist, stats=stats)


@app.route('/books')
@tenant_required
def books():
    t = current_tenant()
    kw = request.args.get('q', '').strip()
    cat = request.args.get('cat', '').strip()
    sql = 'SELECT * FROM books WHERE tenant_id={T}'
    args = []
    if kw:
        sql += ' AND (title LIKE ? OR author LIKE ?)'
        args += ['%' + kw + '%', '%' + kw + '%']
    if cat:
        sql += ' AND category=?'
        args.append(cat)
    sql += ' ORDER BY id DESC'
    rows = q_tenant(sql, args)
    cats = q_tenant('SELECT DISTINCT category FROM books WHERE tenant_id={T} AND category IS NOT NULL ORDER BY category')
    u, p, over = quota_state(t)
    return render_template('books.html', rows=rows, kw=kw, cat=cat,
                           cats=[c['category'] for c in cats], t=t, usage=u, over=over)


@app.route('/books/<int:bid>')
@tenant_required
def book_detail(bid):
    # 跨租户访问 → 查不到 → 404（不暴露资源是否存在）
    b = q_tenant('SELECT * FROM books WHERE id=? AND tenant_id={T}', (bid,), one=True)
    if not b:
        abort(404)
    hist = q_tenant('''SELECT * FROM borrows WHERE tenant_id={T} AND book_id=? ORDER BY id DESC LIMIT 20''', (bid,))
    return render_template('book_detail.html', b=b, hist=hist)


@app.route('/books/add', methods=['POST'])
@tenant_admin_required
def book_add():
    tid = require_tenant_id()
    ok, msg = check_quota(tid, 'books')
    if not ok:
        audit('配额拦截', '新增图书被拒：%s' % msg)
        flash(msg, 'danger')
        return redirect(url_for('books'))
    title = request.form.get('title', '').strip()
    if not title:
        flash('书名必填', 'warning')
        return redirect(url_for('books'))
    ex_tenant('''INSERT INTO books(tenant_id,title,author,category,stock,status)
                 VALUES({T},?,?,?,?,?)''',
              (title, request.form.get('author', '').strip(),
               request.form.get('category', '').strip() or '综合',
               int(request.form.get('stock') or 1), 'available'))
    audit('新增图书', '《%s》' % title)
    flash('图书《%s》已入库' % title, 'success')
    return redirect(url_for('books'))


@app.route('/books/<int:bid>/edit', methods=['POST'])
@tenant_admin_required
def book_edit(bid):
    b = q_tenant('SELECT * FROM books WHERE id=? AND tenant_id={T}', (bid,), one=True)
    if not b:
        abort(404)
    ex_tenant('''UPDATE books SET title=?, author=?, category=?, stock=?, status=?
                 WHERE id=? AND tenant_id={T}''',
              (request.form.get('title', '').strip() or b['title'],
               request.form.get('author', '').strip(),
               request.form.get('category', '').strip() or b['category'],
               int(request.form.get('stock') or b['stock']),
               request.form.get('status') or b['status'], bid))
    audit('编辑图书', '《%s》' % (request.form.get('title') or b['title']))
    flash('图书信息已保存', 'success')
    return redirect(url_for('books'))


@app.route('/books/<int:bid>/delete', methods=['POST'])
@tenant_admin_required
def book_delete(bid):
    b = q_tenant('SELECT * FROM books WHERE id=? AND tenant_id={T}', (bid,), one=True)
    if not b:
        abort(404)
    ex_tenant('DELETE FROM books WHERE id=? AND tenant_id={T}', (bid,))
    audit('删除图书', '《%s》' % b['title'])
    flash('图书《%s》已下架' % b['title'], 'success')
    return redirect(url_for('books'))


@app.route('/borrows')
@tenant_required
def borrows():
    t = current_tenant()
    status = request.args.get('status', '').strip()
    sql = '''SELECT b.*, k.title, k.author FROM borrows b
             LEFT JOIN books k ON k.id=b.book_id AND k.tenant_id=b.tenant_id
             WHERE b.tenant_id={T}'''
    args = []
    if status:
        sql += ' AND b.status=?'
        args.append(status)
    sql += ' ORDER BY b.id DESC LIMIT 200'
    rows = q_tenant(sql, args)
    books_list = q_tenant('SELECT id, title, stock FROM books WHERE tenant_id={T} ORDER BY title')
    stats = {
        '借阅中': q_tenant("SELECT COUNT(*) c FROM borrows WHERE tenant_id={T} AND status='borrowed'", one=True)['c'],
        '已归还': q_tenant("SELECT COUNT(*) c FROM borrows WHERE tenant_id={T} AND status='returned'", one=True)['c'],
        '图书种类': len(books_list),
    }
    return render_template('borrows.html', rows=rows, status=status, books_list=books_list,
                           stats=stats, t=t)


@app.route('/borrows/add', methods=['POST'])
@tenant_required
def borrow_add():
    reader = request.form.get('reader_name', '').strip()
    try:
        bid = int(request.form.get('book_id') or 0)
    except ValueError:
        bid = 0
    if not reader or not bid:
        flash('请填写读者姓名并选择图书', 'warning')
        return redirect(url_for('borrows'))
    b = q_tenant('SELECT * FROM books WHERE id=? AND tenant_id={T}', (bid,), one=True)
    if not b:
        abort(404)                      # 跨租户借书 → 404
    if b['stock'] <= 0:
        flash('《%s》暂无可借库存' % b['title'], 'warning')
        return redirect(url_for('borrows'))
    ex_tenant('''INSERT INTO borrows(tenant_id,book_id,reader_name,status) VALUES({T},?,?, 'borrowed')''',
              (bid, reader))
    ex_tenant('UPDATE books SET stock=stock-1 WHERE id=? AND tenant_id={T}', (bid,))
    audit('办理借阅', '读者 %s 借出《%s》' % (reader, b['title']))
    flash('已为 %s 办理《%s》借阅' % (reader, b['title']), 'success')
    return redirect(url_for('borrows'))


@app.route('/borrows/<int:rid>/return', methods=['POST'])
@tenant_required
def borrow_return(rid):
    r = q_tenant('SELECT * FROM borrows WHERE id=? AND tenant_id={T}', (rid,), one=True)
    if not r:
        abort(404)
    if r['status'] == 'returned':
        flash('该记录已归还', 'info')
        return redirect(url_for('borrows'))
    ex_tenant("""UPDATE borrows SET status='returned', return_date=datetime('now','localtime')
                 WHERE id=? AND tenant_id={T}""", (rid,))
    ex_tenant('UPDATE books SET stock=stock+1 WHERE id=? AND tenant_id={T}', (r['book_id'],))
    audit('办理归还', '读者 %s 归还图书（记录 #%d）' % (r['reader_name'], rid))
    flash('已办理归还', 'success')
    return redirect(url_for('borrows'))


@app.route('/tenant-users')
@tenant_admin_required
def tenant_users():
    t = current_tenant()
    rows = q_tenant('SELECT * FROM users WHERE tenant_id={T} ORDER BY id', one=False)
    u, p, over = quota_state(t)
    return render_template('tenant_users.html', rows=rows, t=t, usage=u, over=over)


@app.route('/tenant-users/add', methods=['POST'])
@tenant_admin_required
def tenant_user_add():
    tid = require_tenant_id()
    ok, msg = check_quota(tid, 'users')
    if not ok:
        audit('配额拦截', '新增用户被拒：%s' % msg)
        flash(msg, 'danger')
        return redirect(url_for('tenant_users'))
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()
    if not username or len(password) < 6:
        flash('用户名必填，密码至少 6 位', 'warning')
        return redirect(url_for('tenant_users'))
    if query_one('SELECT id FROM users WHERE username=?', (username,)):
        flash('用户名已被占用（用户名全平台唯一）', 'danger')
        return redirect(url_for('tenant_users'))
    role = request.form.get('role') or 'tenant_user'
    if role not in ('tenant_admin', 'tenant_user'):
        role = 'tenant_user'
    execute('''INSERT INTO users(username,password_hash,display_name,role,tenant_id)
               VALUES(?,?,?,?,?)''',
            (username, generate_password_hash(password),
             request.form.get('display_name', '').strip() or username, role, tid))
    audit('新增租户用户', '账号 %s（%s）' % (username, ROLE_LABELS.get(role, role)))
    flash('成员 %s 已开通' % username, 'success')
    return redirect(url_for('tenant_users'))


@app.route('/tenant-users/<int:uid>/edit', methods=['POST'])
@tenant_admin_required
def tenant_user_edit(uid):
    u = q_tenant('SELECT * FROM users WHERE id=? AND tenant_id={T}', (uid,), one=True)
    if not u:
        abort(404)
    role = request.form.get('role') or u['role']
    if role not in ('tenant_admin', 'tenant_user'):
        role = u['role']
    if uid == g.user['id'] and role != 'tenant_admin':
        flash('不能把自己降级为成员', 'danger')
        return redirect(url_for('tenant_users'))
    ex_tenant('UPDATE users SET display_name=?, role=? WHERE id=? AND tenant_id={T}',
              (request.form.get('display_name', '').strip() or u['display_name'], role, uid))
    audit('编辑租户用户', '账号 %s 资料/角色更新' % u['username'])
    flash('成员信息已保存', 'success')
    return redirect(url_for('tenant_users'))


@app.route('/tenant-users/<int:uid>/reset', methods=['POST'])
@tenant_admin_required
def tenant_user_reset(uid):
    u = q_tenant('SELECT * FROM users WHERE id=? AND tenant_id={T}', (uid,), one=True)
    if not u:
        abort(404)
    pwd = request.form.get('password', '').strip()
    if len(pwd) < 6:
        flash('新密码至少 6 位', 'warning')
        return redirect(url_for('tenant_users'))
    ex_tenant('UPDATE users SET password_hash=? WHERE id=? AND tenant_id={T}',
              (generate_password_hash(pwd), uid))
    audit('重置租户用户密码', '账号 %s' % u['username'])
    flash('已重置 %s 的密码' % u['username'], 'success')
    return redirect(url_for('tenant_users'))


@app.route('/tenant-users/<int:uid>/delete', methods=['POST'])
@tenant_admin_required
def tenant_user_delete(uid):
    u = q_tenant('SELECT * FROM users WHERE id=? AND tenant_id={T}', (uid,), one=True)
    if not u:
        abort(404)
    if uid == g.user['id']:
        flash('不能删除自己', 'danger')
        return redirect(url_for('tenant_users'))
    ex_tenant('DELETE FROM users WHERE id=? AND tenant_id={T}', (uid,))
    audit('删除租户用户', '账号 %s' % u['username'])
    flash('成员 %s 已移除' % u['username'], 'success')
    return redirect(url_for('tenant_users'))


# ==================== API 管理（租户侧） ====================
@app.route('/api-mgmt')
@tenant_admin_required
def api_mgmt():
    t = current_tenant()
    key = q_tenant('SELECT * FROM api_keys WHERE tenant_id={T} ORDER BY id LIMIT 1', one=True)
    logs = q_tenant('SELECT * FROM api_logs WHERE tenant_id={T} ORDER BY id DESC LIMIT 30')
    u, p, over = quota_state(t)
    ok_cnt = q_tenant('SELECT COUNT(*) c FROM api_logs WHERE tenant_id={T} AND status_code=200', one=True)['c']
    err_cnt = q_tenant('SELECT COUNT(*) c FROM api_logs WHERE tenant_id={T} AND status_code>=400', one=True)['c']
    bucket = q_tenant('SELECT * FROM rate_buckets WHERE tenant_id={T} AND bucket_key=?', ('tenant',), one=True)
    return render_template('api_mgmt.html', t=t, key=key, key_mask=mask_key(key['api_key']) if key else '未生成',
                           logs=logs, usage=u, pcts=p, over=over, ok_cnt=ok_cnt, err_cnt=err_cnt,
                           bucket=bucket)


@app.route('/api-mgmt/reveal', methods=['POST'])
@tenant_admin_required
def api_key_reveal():
    """按需显示完整 Key：不写进 HTML 源码，也不进日志。"""
    key = q_tenant('SELECT * FROM api_keys WHERE tenant_id={T} ORDER BY id LIMIT 1', one=True)
    if not key:
        return jsonify({'ok': False, 'message': '该租户尚未生成 API Key'}), 404
    audit('查看 API Key', '租户管理员查看了完整 API Key（掩码 %s）' % mask_key(key['api_key']))
    return jsonify({'ok': True, 'api_key': key['api_key']})


@app.route('/api-mgmt/rotate', methods=['POST'])
@tenant_admin_required
def api_key_rotate():
    tid = require_tenant_id()
    new_key = gen_api_key()
    execute('UPDATE api_keys SET api_key=? WHERE tenant_id=?', (new_key, tid))
    audit('轮换 API Key', '租户自行轮换 API Key（新掩码 %s）' % mask_key(new_key))
    flash('API Key 已轮换，旧 Key 立即失效', 'success')
    return redirect(url_for('api_mgmt'))


@app.route('/quota')
@tenant_required
def quota():
    t = current_tenant()
    u, p, over = quota_state(t)
    logs = q_tenant('SELECT * FROM api_logs WHERE tenant_id={T} ORDER BY id DESC LIMIT 15')
    plans = query('SELECT * FROM plans ORDER BY sort, id')
    daily = []
    from datetime import timedelta
    for i in range(6, -1, -1):
        d = (datetime.now().date() - timedelta(days=i)).strftime('%Y-%m-%d')
        c = q_tenant('SELECT COUNT(*) c FROM api_logs WHERE tenant_id={T} AND substr(created_at,1,10)=?',
                     (d,), one=True)['c']
        daily.append({'day': d[5:], 'count': c})
    return render_template('quota.html', t=t, usage=u, pcts=p, over=over, logs=logs,
                           plans=plans, daily=daily)


# ==================== 隔离方案文档页 ====================
@app.route('/isolation')
@login_required
def isolation():
    # 平台管理员看到全平台样本，租户只能看到自己的行数（同样走隔离）
    if g.user['role'] == 'platform_admin':
        sample = query('''SELECT t.name, t.status,
                                 (SELECT COUNT(*) FROM books b WHERE b.tenant_id=t.id) books,
                                 (SELECT COUNT(*) FROM borrows r WHERE r.tenant_id=t.id) borrows,
                                 (SELECT COUNT(*) FROM users u WHERE u.tenant_id=t.id) users
                          FROM tenants t ORDER BY t.id''')
        scope = '全平台（平台管理员视图）'
    else:
        t = current_tenant()
        sample = q_tenant('''SELECT t.name, t.status,
                                 (SELECT COUNT(*) FROM books b WHERE b.tenant_id=t.id) books,
                                 (SELECT COUNT(*) FROM borrows r WHERE r.tenant_id=t.id) borrows,
                                 (SELECT COUNT(*) FROM users u WHERE u.tenant_id=t.id) users
                             FROM tenants t WHERE t.id={T}''')
        scope = '仅本租户（%s）' % (t['name'] if t else '')
    return render_template('isolation.html', sample=sample, scope=scope)


@app.route('/api-playground')
@login_required
def api_playground():
    key_mask = ''
    if g.user['role'] != 'platform_admin':
        k = q_tenant('SELECT api_key FROM api_keys WHERE tenant_id={T} ORDER BY id LIMIT 1', one=True)
        key_mask = mask_key(k['api_key']) if k else '未生成'
    return render_template('api_playground.html', key_mask=key_mask)


# ==================== 开放 API（X-API-Key 鉴权 + 限流） ====================
def api_log(tid, key, code):
    execute('INSERT INTO api_logs(tenant_id,api_key,path,status_code) VALUES(?,?,?,?)',
            (tid, mask_key(key), request.path, code))


def api_error(tid, key, code, err, message, retry_after=None):
    if tid:
        api_log(tid, key, code)
    payload = {'ok': False, 'error': err, 'message': message, 'status': code}
    resp = jsonify(payload)
    resp.status_code = code
    if retry_after is not None:
        resp.headers['Retry-After'] = str(retry_after)
    return resp


def api_auth():
    """返回 (ctx, err_response)。ctx 为 None 时 err_response 可直接 return。"""
    key = (request.headers.get('X-API-Key') or request.args.get('api_key') or '').strip()
    if not key:
        return None, api_error(None, None, 401, 'missing_api_key',
                               '缺少 X-API-Key 请求头')
    row = query_one('''SELECT k.id AS key_id, k.tenant_id, k.api_key, k.status AS key_status,
                              t.name AS tenant_name, t.status AS tenant_status, t.expire_date,
                              p.name AS plan_name, p.user_quota, p.book_quota,
                              p.api_monthly_quota, p.api_rate_per_min
                       FROM api_keys k
                       JOIN tenants t ON t.id = k.tenant_id
                       LEFT JOIN plans p ON p.id = t.plan_id
                       WHERE k.api_key = ?''', (key,))
    if not row:
        return None, api_error(None, None, 401, 'invalid_api_key', 'API Key 无效')
    if row['key_status'] != 'active':
        return None, api_error(row['tenant_id'], key, 403, 'api_key_disabled', 'API Key 已被停用')
    if row['tenant_status'] != 'active':
        return None, api_error(row['tenant_id'], key, 403, 'tenant_disabled',
                               '租户「%s」已停用，API 拒绝服务' % row['tenant_name'])
    return {'key': key, **row}, None


def rate_check(tid, limit):
    """滑动窗口（60 秒）限流。返回 (allowed, retry_after, remaining)。"""
    limit = limit or 60
    bkey = 'tenant'
    row = query_one('SELECT * FROM rate_buckets WHERE tenant_id=? AND bucket_key=?', (tid, bkey))
    now = datetime.now()
    if not row:
        execute("""INSERT INTO rate_buckets(tenant_id,bucket_key,count,window_start)
                   VALUES(?,?,1,datetime('now','localtime'))""", (tid, bkey))
        return True, 0, limit - 1
    try:
        ws = datetime.strptime(row['window_start'], '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        ws = now
    elapsed = (now - ws).total_seconds()
    if elapsed >= 60:
        execute("""UPDATE rate_buckets SET count=1, window_start=datetime('now','localtime')
                   WHERE id=?""", (row['id'],))
        return True, 0, limit - 1
    if row['count'] >= limit:
        return False, max(1, int(round(60 - elapsed))), 0
    execute('UPDATE rate_buckets SET count=count+1 WHERE id=?', (row['id'],))
    return True, 0, max(0, limit - row['count'] - 1)


def api_guard(ctx):
    """配额 + 限流统一闸门；通过返回 None，否则返回错误响应。"""
    tid = ctx['tenant_id']
    monthly = ctx['api_monthly_quota'] or 0
    used = query_one('SELECT COUNT(*) c FROM api_logs WHERE tenant_id=? AND created_at>=?',
                     (tid, month_start()))['c']
    if monthly > 0 and used >= monthly:
        return api_error(tid, ctx['key'], 429, 'monthly_quota_exceeded',
                         '本月 API 调用量已达套餐上限（%d/%d），请升级套餐' % (used, monthly))
    allowed, retry_after, remaining = rate_check(tid, ctx['api_rate_per_min'])
    if not allowed:
        return api_error(tid, ctx['key'], 429, 'rate_limited',
                         '请求过于频繁，超过套餐限流（%d 次/分钟），请稍后重试' % (ctx['api_rate_per_min'] or 60),
                         retry_after=retry_after)
    ctx['remaining'] = remaining
    return None


def api_ok(ctx, data):
    api_log(ctx['tenant_id'], ctx['key'], 200)
    payload = {'ok': True, 'tenant': ctx['tenant_name'], 'plan': ctx['plan_name'],
               'rate_limit_per_min': ctx['api_rate_per_min'], 'data': data}
    resp = jsonify(payload)
    resp.headers['X-RateLimit-Limit'] = str(ctx['api_rate_per_min'] or 60)
    resp.headers['X-RateLimit-Remaining'] = str(ctx.get('remaining', 0))
    resp.headers['X-Tenant'] = ctx['tenant_name']
    return resp


@app.route('/api/v1/books')
def api_books():
    ctx, err = api_auth()
    if err:
        return err
    err = api_guard(ctx)
    if err:
        return err
    tid = ctx['tenant_id']
    kw = request.args.get('q', '').strip()
    sql = 'SELECT id,title,author,category,stock,status FROM books WHERE tenant_id=?'
    args = [tid]
    if kw:
        sql += ' AND (title LIKE ? OR author LIKE ?)'
        args += ['%' + kw + '%', '%' + kw + '%']
    sql += ' ORDER BY id LIMIT 100'
    rows = query(sql, tuple(args))
    return api_ok(ctx, {'count': len(rows), 'items': rows})


@app.route('/api/v1/borrows')
def api_borrows():
    ctx, err = api_auth()
    if err:
        return err
    err = api_guard(ctx)
    if err:
        return err
    tid = ctx['tenant_id']
    status = request.args.get('status', '').strip()
    sql = '''SELECT b.id,b.reader_name,b.borrow_date,b.return_date,b.status,k.title
             FROM borrows b LEFT JOIN books k ON k.id=b.book_id AND k.tenant_id=b.tenant_id
             WHERE b.tenant_id=?'''
    args = [tid]
    if status:
        sql += ' AND b.status=?'
        args.append(status)
    sql += ' ORDER BY b.id DESC LIMIT 100'
    rows = query(sql, tuple(args))
    return api_ok(ctx, {'count': len(rows), 'items': rows})


@app.route('/api/v1/usage')
def api_usage():
    ctx, err = api_auth()
    if err:
        return err
    err = api_guard(ctx)
    if err:
        return err
    tid = ctx['tenant_id']
    u = tenant_usage(tid)
    return api_ok(ctx, {
        'users': {'used': u['users'], 'quota': ctx['user_quota']},
        'books': {'used': u['books'], 'quota': ctx['book_quota']},
        'api_calls_month': {'used': u['api_calls'], 'quota': ctx['api_monthly_quota']},
        'rate_limit_per_min': ctx['api_rate_per_min'],
    })


# ==================== 错误页 ====================
@app.errorhandler(403)
def forbidden(e):
    return render_template('error.html', code=403, msg='没有权限访问该页面（平台管理员不可修改租户业务数据）'), 403


@app.errorhandler(404)
def not_found(e):
    return render_template('error.html', code=404, msg='资源不存在或不属于当前租户'), 404


if __name__ == '__main__':
    print('[%s] http://127.0.0.1:%d' % (APP_NAME, PORT))
    app.run(host='127.0.0.1', port=PORT, debug=False, threaded=True)
