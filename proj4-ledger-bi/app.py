# -*- coding: utf-8 -*-
"""
项目4: 记账 + 数据看板 (Ledger + BI)
=====================================
面向「数据/策略产品经理」作品集: 指标口径定义 + SQL 取数 + 可视化 + 留存/漏斗分析。

模块:
  记一笔 / 账单明细 / 分类管理 / 预算设置 / 数据看板 / 分析报告 /
  指标口径 / SQL工作台 / 用户活跃
约定:
  - 金额一律整数「分」存库与计算, 展示时 ÷100 保留 2 位
  - 时间统一 datetime('now','localtime') 字符串 'YYYY-MM-DD HH:MM:SS'
  - 仅依赖 flask + 标准库; 前端资源全部本地 static/
"""
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, g, abort)
from werkzeug.security import generate_password_hash, check_password_hash
import db as database
from db import query, query_one, execute, transaction

# ================== 项目配置 ==================
APP_NAME = '记账与数据看板'
APP_SUBTITLE = 'Ledger+BI'
PORT = 8004
NAV = [
    ('record',     '记一笔',    '/record',     '✏️'),
    ('ledger',     '账单明细',  '/ledger',     '📒'),
    ('categories', '分类管理',  '/categories', '🗂️'),
    ('budgets',    '预算设置',  '/budgets',    '🎯'),
    ('dashboard',  '数据看板',  '/dashboard',  '📊'),
    ('report',     '分析报告',  '/report',     '🧾'),
    ('metrics',    '指标口径',  '/metrics',    '📐'),
    ('sql_lab',    'SQL工作台', '/sql-lab',    '🧮'),
    ('activity',   '用户活跃',  '/activity',   '🔥'),
]
ROLE_LABELS = {'admin': '管理员', 'operator': '操作员', 'user': '普通用户'}
ACCOUNT_TYPES = ['现金', '银行卡', '支付宝', '微信']
# =============================================

app = Flask(__name__)
app.secret_key = 'ledger-bi-dev-secret'
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
    # 分类字典: 一级(parent_id IS NULL) / 二级
    execute('''CREATE TABLE IF NOT EXISTS categories(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        parent_id INTEGER REFERENCES categories(id),
        name TEXT NOT NULL,
        type TEXT NOT NULL,              -- income / expense
        sort INTEGER NOT NULL DEFAULT 0
    )''')
    # 账户(现金/银行卡/支付宝/微信), balance 为初始余额(分)
    execute('''CREATE TABLE IF NOT EXISTS accounts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        balance INTEGER NOT NULL DEFAULT 0
    )''')
    # 账目流水
    execute('''CREATE TABLE IF NOT EXISTS transactions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        account_id INTEGER REFERENCES accounts(id),
        category_id INTEGER REFERENCES categories(id),
        type TEXT NOT NULL,              -- income / expense
        amount INTEGER NOT NULL,         -- 分, 恒 > 0
        note TEXT,
        trans_date TEXT NOT NULL,        -- YYYY-MM-DD
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    execute('CREATE INDEX IF NOT EXISTS idx_tx_user_date ON transactions(user_id, trans_date)')
    execute('CREATE INDEX IF NOT EXISTS idx_tx_cat ON transactions(category_id)')
    # 月度预算(分)
    execute('''CREATE TABLE IF NOT EXISTS budgets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        month TEXT NOT NULL,             -- YYYY-MM
        amount INTEGER NOT NULL,
        UNIQUE(user_id, month)
    )''')
    # 模拟埋点行为表(匿名标识, 不建外键: 行为表独立于业务账号)
    execute('''CREATE TABLE IF NOT EXISTS user_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        event_type TEXT NOT NULL,        -- login / record / streak
        event_date TEXT NOT NULL,        -- YYYY-MM-DD
        UNIQUE(user_id, event_type, event_date)
    )''')
    execute('CREATE INDEX IF NOT EXISTS idx_ev_date ON user_events(event_date)')
    # 内置示例查询模板
    execute('''CREATE TABLE IF NOT EXISTS sql_queries(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT,
        sql_text TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')


# ---------------- 指标口径字典(单一事实来源) ----------------
# 看板/报告页每个数字旁的「口径」按钮都指到 /metrics#m-<key>
METRICS = [
    dict(key='month_income', page='看板·本月收支', name='本月收入',
         formula='Σ amount WHERE type=income',
         caliber="自然月(本地时区)，trans_date ≥ 本月 1 日且 < 下月 1 日；统计范围=当前登录用户，管理员可用 ?scope=all 看全局匿名汇总",
         sql="SELECT SUM(amount) FROM transactions WHERE type='income' AND trans_date >= date('now','localtime','start of month') AND user_id = :uid",
         pitfall='只统计已记账流水，不含未入账的应收；金额单位「分」，展示时 ÷100'),
    dict(key='month_expense', page='看板·本月收支', name='本月支出',
         formula='Σ amount WHERE type=expense',
         caliber='自然月，口径同「本月收入」，仅 type=expense',
         sql="SELECT SUM(amount) FROM transactions WHERE type='expense' AND trans_date >= date('now','localtime','start of month') AND user_id = :uid",
         pitfall='退款/报销若走收入分类则不会冲减支出，本 Demo 不做净额处理'),
    dict(key='month_balance', page='看板·本月收支', name='本月结余',
         formula='本月收入合计 − 本月支出合计',
         caliber='自然月，本地时区，先各自求和再相减(不是逐笔相加)',
         sql="SELECT SUM(CASE WHEN type='income' THEN amount ELSE -amount END) FROM transactions WHERE trans_date >= date('now','localtime','start of month') AND user_id = :uid",
         pitfall='结余可正可负；结余 ≠ 账户余额(账户余额含期初余额)'),
    dict(key='expense_mom', page='看板·本月收支', name='支出环比(MoM)',
         formula='(本月支出 − 上月支出) ÷ 上月支出 × 100%',
         caliber='相邻两个自然月；上月支出为 0 时环比无意义，展示为「—」而非 0%',
         sql="SELECT (this_m - last_m) / last_m FROM (SELECT SUM(CASE WHEN strftime('%Y-%m',trans_date)=strftime('%Y-%m',date('now','localtime')) THEN amount ELSE 0 END) this_m, SUM(CASE WHEN strftime('%Y-%m',trans_date)=strftime('%Y-%m',date('now','localtime','-1 month')) THEN amount ELSE 0 END) last_m FROM transactions WHERE type='expense' AND user_id = :uid)",
         pitfall='分母为 0 不能除；环比看「变化率」，同比看「同期」，本 Demo 只做环比'),
    dict(key='budget_rate', page='看板·预算', name='预算执行率',
         formula='本月支出 ÷ 本月预算 × 100%',
         caliber='预算按 (用户, 月份) 唯一；本月无预算记录时展示「未设置预算」，不按 0 处理',
         sql="SELECT spent*1.0/budget FROM budgets b JOIN (SELECT SUM(amount) spent FROM transactions WHERE type='expense' AND strftime('%Y-%m',trans_date)=:month) t WHERE b.month=:month",
         pitfall='执行率 > 100% 即超额，看板标红；执行率与「剩余额度」是两个口径'),
    dict(key='saving_rate', page='报告·结论', name='结余率(储蓄率)',
         formula='本月结余 ÷ 本月收入 × 100%',
         caliber='自然月；收入为 0 时无定义，展示为「—」',
         sql="SELECT (inc-exp)*1.0/inc FROM (SELECT SUM(CASE WHEN type='income' THEN amount ELSE 0 END) inc, SUM(CASE WHEN type='expense' THEN amount ELSE 0 END) exp FROM transactions WHERE trans_date >= date('now','localtime','start of month') AND user_id = :uid)",
         pitfall='结余率高不等于财务状况好，需结合收入绝对额看'),
    dict(key='avg_ticket', page='报告·结论', name='笔均支出(客单价类比)',
         formula='本月支出合计 ÷ 本月支出笔数',
         caliber='分母为「支出笔数」而非「记账天数」；笔数为 0 时展示「—」',
         sql="SELECT SUM(amount)*1.0/COUNT(*) FROM transactions WHERE type='expense' AND trans_date >= date('now','localtime','start of month') AND user_id = :uid",
         pitfall='大额低频支出会显著抬高笔均，需与「日均支出」一起看'),
    dict(key='daily_avg', page='报告·结论', name='日均支出',
         formula='本月支出合计 ÷ 本月已过天数',
         caliber='分母用「本月 1 日到今天」的已过天数，不用自然月总天数，避免月中被低估',
         sql="SELECT SUM(amount)*1.0/CAST(strftime('%d', date('now','localtime')) AS INTEGER) FROM transactions WHERE type='expense' AND trans_date >= date('now','localtime','start of month') AND user_id = :uid",
         pitfall='分母口径选错会横向不可比(月头偏低/月中偏高)'),
    dict(key='top_category_share', page='报告·结论', name='Top分类占比',
         formula='某分类支出 ÷ 本月支出合计 × 100%',
         caliber='按一级分类聚合(二级分类向上归并到父级)；占比按金额，不按笔数',
         sql="SELECT top_name, amt*1.0/SUM(amt) OVER () FROM (SELECT COALESCE(p.name,c.name) top_name, SUM(t.amount) amt FROM transactions t JOIN categories c ON c.id=t.category_id LEFT JOIN categories p ON p.id=c.parent_id WHERE t.type='expense' AND t.trans_date >= date('now','localtime','start of month') AND t.user_id=:uid GROUP BY top_name)",
         pitfall='未分类账目会落在 NULL 分组，本 Demo 记账时强制选择分类'),
    dict(key='pareto3', page='报告·结论', name='Top3分类累计占比(帕累托)',
         formula='金额降序前 3 个一级分类支出之和 ÷ 本月支出合计',
         caliber='窗口函数 SUM() OVER (ORDER BY amt DESC) 求累计，再取第 3 行',
         sql="SELECT cum*1.0/total FROM (SELECT SUM(amt) OVER (ORDER BY amt DESC) cum, SUM(amt) OVER () total, ROW_NUMBER() OVER (ORDER BY amt DESC) rn FROM (SELECT SUM(amount) amt FROM transactions t WHERE t.type='expense' AND t.trans_date >= date('now','localtime','start of month') AND t.user_id=:uid GROUP BY COALESCE((SELECT parent_id FROM categories WHERE id=t.category_id), t.category_id))) WHERE rn=3",
         pitfall='分类数 < 3 时该指标无定义'),
    dict(key='d1', page='用户活跃', name='D1 留存',
         formula='某日活跃用户中，次日仍有事件的用户数 ÷ 该日活跃用户数',
         caliber='「活跃」= 行为表当日存在任意事件(login/record/streak)；分母为当日 DAU，分子为次日 DAU 的交集；汇总口径 = Σ分子 ÷ Σ分母，最后一天无次日数据不参与',
         sql="SELECT COUNT(DISTINCT n.user_id)*1.0/COUNT(DISTINCT e.user_id) FROM user_events e LEFT JOIN user_events n ON n.user_id=e.user_id AND n.event_date=date(e.event_date,'+1 day') WHERE e.event_date < date('now','localtime')",
         pitfall='不能把「次日留存人数」直接除以「总用户数」；跨月要按活跃日切片'),
    dict(key='d7', page='用户活跃', name='D7 留存',
         formula='某日活跃用户中，第 7 日后仍有事件的用户数 ÷ 该日活跃用户数',
         caliber='口径同 D1，偏移 +7 天；最后 7 天无完整观察窗口，不参与汇总',
         sql="SELECT COUNT(DISTINCT n.user_id)*1.0/COUNT(DISTINCT e.user_id) FROM user_events e LEFT JOIN user_events n ON n.user_id=e.user_id AND n.event_date=date(e.event_date,'+7 day') WHERE e.event_date <= date('now','localtime','-7 day')",
         pitfall='观察窗口不足会把留存算低，必须截断尾部数据'),
    dict(key='funnel', page='用户活跃', name='记账转化漏斗',
         formula='登录用户数 → 产生记账事件用户数 → 连续记账 3 天用户数 → 连续记账 7 天用户数',
         caliber='按「去重用户数」逐级统计，各级为上一级的子集；连续 N 天用窗口函数 ROW_NUMBER 差值法判定(日期 − 序号 = 常量即连续)',
         sql="WITH d AS (SELECT DISTINCT user_id,event_date FROM user_events WHERE event_type='record'), g AS (SELECT user_id,event_date, julianday(event_date)-ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY event_date) grp FROM d) SELECT COUNT(DISTINCT user_id) FROM (SELECT user_id FROM g GROUP BY user_id,grp HAVING COUNT(*)>=3)",
         pitfall='「连续」必须真判连续，不能用「有 3 天记录」近似；漏斗各级必须同口径(都是用户数或都是次数)'),
    dict(key='dau', page='用户活跃', name='DAU(日活)',
         formula='当日有任意事件的去重用户数',
         caliber='按 event_date 聚合，含 login/record/streak 全部事件类型',
         sql="SELECT event_date, COUNT(DISTINCT user_id) FROM user_events GROUP BY event_date ORDER BY event_date",
         pitfall='事件表未去重会把同一天多次登录算成多人'),
    dict(key='record_per_day', page='用户活跃', name='日均记账笔数',
         formula='记账事件总数 ÷ 有记账行为的天数',
         caliber='分母为「发生记账的日期数」，不是自然日天数',
         sql="SELECT COUNT(*)*1.0/COUNT(DISTINCT event_date) FROM user_events WHERE event_type='record'",
         pitfall='分母用自然日会因周末低活跃被摊薄'),
]
METRIC_MAP = {m['key']: m for m in METRICS}


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
        'current_user': getattr(g, 'user', None),
        'request_path': request.path,
        'METRIC_MAP': METRIC_MAP,
    }


@app.template_filter('money')
def f_money(cents):
    try:
        return f'{(int(cents or 0)) / 100:,.2f}'
    except (TypeError, ValueError):
        return '0.00'


@app.template_filter('pct')
def f_pct(v, digits=1):
    if v is None:
        return '—'
    try:
        return f'{float(v):.{digits}f}%'
    except (TypeError, ValueError):
        return '—'


# ---------------- 取数辅助 ----------------
def is_global():
    """管理员可切换「全局匿名汇总」视角。"""
    return getattr(g, 'user', None) is not None and g.user['role'] == 'admin' \
        and request.args.get('scope') == 'all'


def scope_sql(alias=''):
    """返回 (WHERE 片段, 参数)。非全局时按 user_id 隔离; 片段始终放在 WHERE 末尾。"""
    pre = f'{alias}.' if alias else ''
    if is_global():
        return '1=1', []
    return f'{pre}user_id=?', [g.user['id']]


def y(cents):
    return round((cents or 0) / 100.0, 2)


def ratio(a, b):
    """a/b*100, 分母为 0 返回 None(页面显示「—」)。"""
    if not b:
        return None
    return round(a * 100.0 / b, 1)


def month_slots(n=12):
    """返回最近 n 个月 'YYYY-MM'(含本月), 用于补齐无数据的月份。"""
    t = date.today()
    yy, mm, out = t.year, t.month, []
    for i in range(n - 1, -1, -1):
        y2, m2 = yy, mm - i
        while m2 <= 0:
            m2 += 12
            y2 -= 1
        out.append(f'{y2:04d}-{m2:02d}')
    return out


def now_month():
    return query_one("SELECT strftime('%Y-%m', date('now','localtime')) m")['m']


def today_str():
    return query_one("SELECT date('now','localtime') d")['d']


def category_tree():
    """返回一级分类列表, 每项带 children(二级)。"""
    rows = query('SELECT * FROM categories ORDER BY type, sort, id')
    tops = [dict(r) for r in rows if not r['parent_id']]
    kids = {}
    for r in rows:
        if r['parent_id']:
            kids.setdefault(r['parent_id'], []).append(dict(r))
    for t in tops:
        t['children'] = kids.get(t['id'], [])
    return tops


def parse_amount(raw):
    s = (raw or '').strip().replace(',', '').replace('¥', '')
    if not s:
        return None, '请输入金额'
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None, '金额格式不正确, 例如 12.50'
    if d <= 0:
        return None, '金额必须大于 0'
    if d > Decimal('1000000'):
        return None, '单笔金额不能超过 1,000,000 元'
    return int((d * 100).quantize(Decimal('1'))), None


def parse_date(raw, allow_empty=False):
    s = (raw or '').strip()
    if not s and allow_empty:
        return None, None
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', s):
        return None, '日期格式应为 YYYY-MM-DD'
    if s > today_str():
        return None, '日期不能晚于今天'
    return s, None


# ---------------- 认证路由 ----------------
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


@app.route('/')
@login_required
def index():
    return redirect(url_for('dashboard'))


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


# ---------------- 记一笔 ----------------
@app.route('/record', methods=['GET', 'POST'])
@login_required
def record():
    tree = category_tree()
    accounts = query('SELECT * FROM accounts ORDER BY id')
    if request.method == 'POST':
        ttype = request.form.get('type', 'expense')
        if ttype not in ('income', 'expense'):
            flash('类型不合法', 'danger')
            return redirect(url_for('record'))
        amount, err = parse_amount(request.form.get('amount'))
        tdate, derr = parse_date(request.form.get('trans_date'))
        cat = query_one('SELECT * FROM categories WHERE id=?',
                        (request.form.get('category_id') or 0,))
        acct = query_one('SELECT * FROM accounts WHERE id=?',
                         (request.form.get('account_id') or 0,))
        err = err or derr
        if not err and not cat:
            err = '请选择分类'
        elif not err and cat and cat['type'] != ttype:
            err = '分类与收支类型不匹配'
        if not err and not acct:
            err = '请选择账户'
        if err:
            flash(err, 'danger')
            return redirect(url_for('record'))
        execute('''INSERT INTO transactions(user_id,account_id,category_id,type,amount,note,trans_date)
                   VALUES(?,?,?,?,?,?,?)''',
                (g.user['id'], acct['id'], cat['id'], ttype, amount,
                 (request.form.get('note') or '').strip()[:80], tdate))
        flash(f'已记录 {cat["name"]} {"支出" if ttype == "expense" else "收入"} ¥{amount / 100:,.2f}', 'success')
        return redirect(url_for('ledger'))
    # 常用分类(按近 90 天记账频次, 用于快捷按钮)
    sw, sp = scope_sql('t')
    quick = query(f'''SELECT c.id, c.name, c.type, COUNT(*) cnt
                      FROM transactions t JOIN categories c ON c.id=t.category_id
                      WHERE t.trans_date >= date('now','localtime','-90 day') AND {sw}
                      GROUP BY c.id ORDER BY cnt DESC, c.id LIMIT 8''', sp)
    return render_template('record.html', tree=tree, accounts=accounts,
                           today=today_str(), quick=quick,
                           preset_type=request.args.get('type', 'expense'),
                           preset_cat=request.args.get('category_id', ''))


@app.route('/ledger')
@login_required
def ledger():
    f_type = request.args.get('type', '')
    f_cat = request.args.get('category_id', '')
    f_acct = request.args.get('account_id', '')
    f_from = request.args.get('date_from', '')
    f_to = request.args.get('date_to', '')
    f_kw = (request.args.get('kw') or '').strip()
    page = max(int(request.args.get('page', 1) or 1), 1)
    size = 20

    where, args = ['1=1'], []
    sw, sp = scope_sql('t')
    where.append(sw)
    args += sp
    if f_type in ('income', 'expense'):
        where.append('t.type=?')
        args.append(f_type)
    if f_cat.isdigit():
        # 选一级分类时连带其二级
        where.append('(t.category_id=? OR c.parent_id=?)')
        args += [int(f_cat), int(f_cat)]
    if f_acct.isdigit():
        where.append('t.account_id=?')
        args.append(int(f_acct))
    if re.match(r'^\d{4}-\d{2}-\d{2}$', f_from):
        where.append('t.trans_date>=?')
        args.append(f_from)
    if re.match(r'^\d{4}-\d{2}-\d{2}$', f_to):
        where.append('t.trans_date<=?')
        args.append(f_to)
    if f_kw:
        where.append('(t.note LIKE ? OR c.name LIKE ?)')
        args += [f'%{f_kw}%', f'%{f_kw}%']
    w = ' AND '.join(where)

    base = f'''FROM transactions t
               LEFT JOIN categories c ON c.id=t.category_id
               LEFT JOIN categories p ON p.id=c.parent_id
               LEFT JOIN accounts a ON a.id=t.account_id
               WHERE {w}'''
    agg = query_one(f'''SELECT COUNT(*) cnt,
                        COALESCE(SUM(CASE WHEN t.type='income' THEN t.amount ELSE 0 END),0) income,
                        COALESCE(SUM(CASE WHEN t.type='expense' THEN t.amount ELSE 0 END),0) expense
                        {base}''', args)
    total = agg['cnt']
    rows = query(f'''SELECT t.*, c.name cat_name, p.name parent_name,
                            a.name acct_name, a.type acct_type
                     {base} ORDER BY t.trans_date DESC, t.id DESC LIMIT ? OFFSET ?''',
                 args + [size, (page - 1) * size])
    pages = max((total + size - 1) // size, 1)
    return render_template('ledger.html', rows=rows, agg=agg, page=page, pages=pages,
                           total=total, tree=category_tree(),
                           accounts=query('SELECT * FROM accounts ORDER BY id'),
                           f=dict(type=f_type, category_id=f_cat, account_id=f_acct,
                                  date_from=f_from, date_to=f_to, kw=f_kw))


@app.route('/ledger/<int:tid>/delete', methods=['POST'])
@login_required
def ledger_delete(tid):
    row = query_one('SELECT * FROM transactions WHERE id=?', (tid,))
    if not row:
        flash('账目不存在', 'warning')
    elif row['user_id'] != g.user['id'] and g.user['role'] != 'admin':
        abort(403)
    else:
        execute('DELETE FROM transactions WHERE id=?', (tid,))
        flash('该笔账目已删除', 'success')
    return redirect(request.referrer or url_for('ledger'))


# ---------------- 分类管理 ----------------
@app.route('/categories', methods=['GET', 'POST'])
@login_required
def categories():
    if request.method == 'POST':
        if g.user['role'] != 'admin':
            abort(403)
        name = (request.form.get('name') or '').strip()[:20]
        ctype = request.form.get('type')
        parent = request.form.get('parent_id') or ''
        if not name or ctype not in ('income', 'expense'):
            flash('请填写分类名称并选择收支类型', 'warning')
        elif query_one('SELECT id FROM categories WHERE name=? AND type=? AND IFNULL(parent_id,0)=?',
                       (name, ctype, int(parent) if parent.isdigit() else 0)):
            flash('同级下已存在同名分类', 'danger')
        else:
            execute('INSERT INTO categories(parent_id,name,type,sort) VALUES(?,?,?,?)',
                    (int(parent) if parent.isdigit() else None, name, ctype,
                     int(request.form.get('sort') or 0)))
            flash('分类已新增', 'success')
        return redirect(url_for('categories'))
    tree = category_tree()
    used = {r['category_id']: r['cnt']
            for r in query('SELECT category_id, COUNT(*) cnt FROM transactions GROUP BY category_id')}
    return render_template('categories.html', tree=tree, used=used, ACCOUNT_TYPES=ACCOUNT_TYPES)


@app.route('/categories/<int:cid>/delete', methods=['POST'])
@login_required
def category_delete(cid):
    if g.user['role'] != 'admin':
        abort(403)
    if query_one('SELECT id FROM categories WHERE parent_id=? LIMIT 1', (cid,)):
        flash('该分类下还有二级分类, 请先删除子分类', 'danger')
    elif query_one('SELECT id FROM transactions WHERE category_id=? LIMIT 1', (cid,)):
        flash('该分类已被账目引用, 不能删除', 'danger')
    else:
        execute('DELETE FROM categories WHERE id=?', (cid,))
        flash('分类已删除', 'success')
    return redirect(url_for('categories'))


# ---------------- 预算设置 ----------------
@app.route('/budgets', methods=['GET', 'POST'])
@login_required
def budgets():
    if request.method == 'POST':
        month = (request.form.get('month') or '').strip()
        amount, err = parse_amount(request.form.get('amount'))
        if not re.match(r'^\d{4}-\d{2}$', month):
            err = '请选择预算月份'
        if err:
            flash(err, 'danger')
        else:
            execute('''INSERT INTO budgets(user_id,month,amount) VALUES(?,?,?)
                       ON CONFLICT(user_id,month) DO UPDATE SET amount=excluded.amount''',
                    (g.user['id'], month, amount))
            flash(f'{month} 预算已保存', 'success')
        return redirect(url_for('budgets'))

    rows = query('''SELECT b.id, b.month, b.amount,
                      COALESCE(x.spent,0) spent, COALESCE(x.cnt,0) cnt
                    FROM budgets b
                    LEFT JOIN (SELECT strftime('%Y-%m',trans_date) m, SUM(amount) spent, COUNT(*) cnt
                               FROM transactions WHERE type='expense' AND user_id=?
                               GROUP BY m) x ON x.m=b.month
                    WHERE b.user_id=? ORDER BY b.month DESC''', (g.user['id'], g.user['id']))
    for r in rows:
        r['rate'] = ratio(r['spent'], r['amount'])
        r['left'] = r['amount'] - r['spent']
    cur = query_one('SELECT * FROM budgets WHERE user_id=? AND month=?',
                    (g.user['id'], now_month()))
    if cur:
        mrow = next((r for r in rows if r['month'] == cur['month']), None)
        cur['spent'] = mrow['spent'] if mrow else 0
        cur['left'] = cur['amount'] - cur['spent']
    # 近 6 月支出参考(设预算时看历史)
    sw, sp = scope_sql('t')
    hist = query(f'''SELECT strftime('%Y-%m', trans_date) m, SUM(amount) amt
                     FROM transactions t WHERE t.type='expense' AND {sw}
                     GROUP BY m ORDER BY m DESC LIMIT 6''', sp)
    return render_template('budgets.html', rows=rows, cur=cur, hist=hist,
                           months=month_slots(6)[::-1], now_month=now_month())


@app.route('/budgets/<int:bid>/delete', methods=['POST'])
@login_required
def budget_delete(bid):
    row = query_one('SELECT * FROM budgets WHERE id=?', (bid,))
    if row and (row['user_id'] == g.user['id'] or g.user['role'] == 'admin'):
        execute('DELETE FROM budgets WHERE id=?', (bid,))
        flash('预算已删除', 'success')
    else:
        flash('无权删除该预算', 'danger')
    return redirect(url_for('budgets'))


# ---------------- 数据看板 ----------------
def collect_dashboard():
    sw, sp = scope_sql('t')
    kpi = query_one(f'''SELECT
        COALESCE(SUM(CASE WHEN t.type='income' AND t.trans_date >= date('now','localtime','start of month') THEN t.amount ELSE 0 END),0) m_income,
        COALESCE(SUM(CASE WHEN t.type='expense' AND t.trans_date >= date('now','localtime','start of month') THEN t.amount ELSE 0 END),0) m_expense,
        COALESCE(SUM(CASE WHEN t.type='income' AND strftime('%Y-%m',t.trans_date)=strftime('%Y-%m',date('now','localtime','-1 month')) THEN t.amount ELSE 0 END),0) l_income,
        COALESCE(SUM(CASE WHEN t.type='expense' AND strftime('%Y-%m',t.trans_date)=strftime('%Y-%m',date('now','localtime','-1 month')) THEN t.amount ELSE 0 END),0) l_expense,
        COUNT(*) all_cnt,
        COUNT(DISTINCT t.trans_date) days
        FROM transactions t WHERE {sw}''', sp)

    # 近 12 个月收支趋势(补齐空月)
    trend_raw = query(f'''SELECT strftime('%Y-%m', t.trans_date) m,
                            SUM(CASE WHEN t.type='income' THEN t.amount ELSE 0 END) income,
                            SUM(CASE WHEN t.type='expense' THEN t.amount ELSE 0 END) expense,
                            COUNT(*) cnt
                          FROM transactions t
                          WHERE t.trans_date >= date('now','localtime','start of month','-11 months') AND {sw}
                          GROUP BY m ORDER BY m''', sp)
    tmap = {r['m']: r for r in trend_raw}
    trend = []
    for m in month_slots(12):
        r = tmap.get(m, {})
        inc, exp = r.get('income') or 0, r.get('expense') or 0
        trend.append({'month': m, 'income': y(inc), 'expense': y(exp),
                      'balance': y(inc - exp), 'cnt': r.get('cnt') or 0})
    cmp6 = trend[-6:]

    # 本月一级分类占比(支出)
    share = query(f'''SELECT COALESCE(p.name, c.name) top_name, SUM(t.amount) amt, COUNT(*) cnt
                      FROM transactions t
                      JOIN categories c ON c.id=t.category_id
                      LEFT JOIN categories p ON p.id=c.parent_id
                      WHERE t.type='expense' AND t.trans_date >= date('now','localtime','start of month') AND {sw}
                      GROUP BY top_name ORDER BY amt DESC''', sp)
    share_total = sum(r['amt'] for r in share)
    for r in share:
        r['share'] = ratio(r['amt'], share_total)

    # 账户分布(当前余额 = 期初 + 收入 − 支出)
    aw, ap = scope_sql('t')
    accounts = query(f'''SELECT a.id, a.name, a.type, a.balance,
                            a.balance + COALESCE(SUM(CASE WHEN t.type='income' THEN t.amount ELSE -t.amount END),0) cur_balance,
                            COALESCE(SUM(CASE WHEN t.type='expense' THEN t.amount ELSE 0 END),0) expense,
                            COUNT(t.id) cnt
                         FROM accounts a
                         LEFT JOIN transactions t ON t.account_id=a.id AND {aw}
                         GROUP BY a.id ORDER BY cur_balance DESC''', ap)
    for r in accounts:
        r['cur_yuan'] = y(r['cur_balance'])

    # 预算执行(本月 + 近 6 月)
    budget_rows = query('''SELECT b.month, b.amount budget, COALESCE(x.spent,0) spent
                           FROM budgets b
                           LEFT JOIN (SELECT strftime('%Y-%m',trans_date) m, SUM(amount) spent
                                      FROM transactions WHERE type='expense' AND user_id=?
                                      GROUP BY m) x ON x.m=b.month
                           WHERE b.user_id=? ORDER BY b.month DESC LIMIT 6''',
                        (g.user['id'], g.user['id']))
    for r in budget_rows:
        r['rate'] = ratio(r['spent'], r['budget'])
        r['left'] = r['budget'] - r['spent']
    cur_budget = next((r for r in budget_rows if r['month'] == now_month()), None)

    kpi['balance'] = kpi['m_income'] - kpi['m_expense']
    kpi['mom_expense'] = ratio(kpi['m_expense'] - kpi['l_expense'], kpi['l_expense'])
    kpi['mom_income'] = ratio(kpi['m_income'] - kpi['l_income'], kpi['l_income'])
    kpi['budget_rate'] = ratio(kpi['m_expense'], cur_budget['budget']) if cur_budget else None
    return dict(kpi=kpi, trend=trend, cmp6=cmp6, share=share, share_total=share_total,
                accounts=accounts, budgets=budget_rows, cur_budget=cur_budget)


@app.route('/dashboard')
@login_required
def dashboard():
    d = collect_dashboard()
    return render_template('dashboard.html', **d, scope=('all' if is_global() else 'me'))


# ---------------- 分析报告(一个结论一个图) ----------------
@app.route('/report')
@login_required
def report():
    sw, sp = scope_sql('t')
    cur_m = now_month()
    kpi = query_one(f'''SELECT
        COALESCE(SUM(CASE WHEN t.type='income' THEN t.amount ELSE 0 END),0) inc,
        COALESCE(SUM(CASE WHEN t.type='expense' THEN t.amount ELSE 0 END),0) exp,
        COUNT(CASE WHEN t.type='expense' THEN 1 END) cnt
        FROM transactions t
        WHERE t.trans_date >= date('now','localtime','start of month') AND {sw}''', sp)
    last = query_one(f'''SELECT
        COALESCE(SUM(CASE WHEN t.type='income' THEN t.amount ELSE 0 END),0) inc,
        COALESCE(SUM(CASE WHEN t.type='expense' THEN t.amount ELSE 0 END),0) exp
        FROM transactions t
        WHERE strftime('%Y-%m', t.trans_date)=strftime('%Y-%m', date('now','localtime','-1 month')) AND {sw}''', sp)

    # 分类(一级)本月 vs 上月, 用于结构变化结论
    cat_cmp = query(f'''SELECT COALESCE(p.name,c.name) name,
                          SUM(CASE WHEN strftime('%Y-%m',t.trans_date)=strftime('%Y-%m',date('now','localtime')) THEN t.amount ELSE 0 END) cur,
                          SUM(CASE WHEN strftime('%Y-%m',t.trans_date)=strftime('%Y-%m',date('now','localtime','-1 month')) THEN t.amount ELSE 0 END) last,
                          SUM(t.amount) total
                        FROM transactions t
                        JOIN categories c ON c.id=t.category_id
                        LEFT JOIN categories p ON p.id=c.parent_id
                        WHERE t.type='expense'
                          AND t.trans_date >= date('now','localtime','start of month','-1 month') AND {sw}
                        GROUP BY name HAVING total>0 ORDER BY cur DESC''', sp)
    cat_total = sum(r['cur'] for r in cat_cmp)
    for r in cat_cmp:
        r['share'] = ratio(r['cur'], cat_total)
        r['delta'] = r['cur'] - r['last']
    pareto = query(f'''SELECT name, cum, total FROM (
                          SELECT name, SUM(amt) OVER (ORDER BY amt DESC) cum,
                                 SUM(amt) OVER () total,
                                 ROW_NUMBER() OVER (ORDER BY amt DESC) rn
                          FROM (SELECT COALESCE(p.name,c.name) name, SUM(t.amount) amt
                                FROM transactions t JOIN categories c ON c.id=t.category_id
                                LEFT JOIN categories p ON p.id=c.parent_id
                                WHERE t.type='expense' AND t.trans_date >= date('now','localtime','start of month') AND {sw}
                                GROUP BY name)) WHERE rn=3''', sp)
    pareto3 = ratio(pareto[0]['cum'], pareto[0]['total']) if pareto else None

    # 最大单笔支出
    biggest = query_one(f'''SELECT t.amount, t.trans_date, t.note, c.name cat_name, a.name acct_name
                            FROM transactions t
                            LEFT JOIN categories c ON c.id=t.category_id
                            LEFT JOIN accounts a ON a.id=t.account_id
                            WHERE t.type='expense' AND t.trans_date >= date('now','localtime','start of month') AND {sw}
                            ORDER BY t.amount DESC LIMIT 1''', sp)

    # 按星期分布
    weekday = query(f'''SELECT CAST(strftime('%w', t.trans_date) AS INTEGER) w, SUM(t.amount) amt, COUNT(*) cnt
                        FROM transactions t
                        WHERE t.type='expense' AND t.trans_date >= date('now','localtime','start of month') AND {sw}
                        GROUP BY w ORDER BY w''', sp)
    wk_names = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
    wk = {r['w']: r for r in weekday}
    weekday_series = [{'name': wk_names[i], 'amt': y(wk.get(i, {}).get('amt', 0)),
                       'cnt': wk.get(i, {}).get('cnt', 0)} for i in range(7)]
    top_wk = max(weekday_series, key=lambda x: x['amt']) if weekday else None

    # 本月每日支出(日历热力图)
    daily = query(f'''SELECT t.trans_date d, SUM(t.amount) amt
                      FROM transactions t
                      WHERE t.type='expense' AND t.trans_date >= date('now','localtime','start of month') AND {sw}
                      GROUP BY d ORDER BY d''', sp)
    daily_series = [[r['d'], y(r['amt'])] for r in daily]

    # 近 6 月结余趋势
    bal = query(f'''SELECT strftime('%Y-%m',t.trans_date) m,
                      SUM(CASE WHEN t.type='income' THEN t.amount ELSE 0 END) inc,
                      SUM(CASE WHEN t.type='expense' THEN t.amount ELSE 0 END) exp
                    FROM transactions t
                    WHERE t.trans_date >= date('now','localtime','start of month','-5 months') AND {sw}
                    GROUP BY m''', sp)
    bmap = {r['m']: r for r in bal}
    bal_series = []
    for m in month_slots(6):
        r = bmap.get(m, {})
        bal_series.append({'month': m, 'balance': y((r.get('inc') or 0) - (r.get('exp') or 0)),
                           'income': y(r.get('inc') or 0), 'expense': y(r.get('exp') or 0)})

    # 预算
    bud = query_one('''SELECT * FROM budgets WHERE user_id=? AND month=?''', (g.user['id'], cur_m))
    bud_rate = ratio(kpi['exp'], bud['amount']) if bud else None
    top_cat = cat_cmp[0] if cat_cmp else None
    mom = ratio(kpi['exp'] - last['exp'], last['exp'])
    days_passed = int(today_str()[8:10])
    saving = ratio(kpi['inc'] - kpi['exp'], kpi['inc'])

    conclusions = []
    bal_vals = [r['balance'] for r in bal_series] or [0.0]
    this_bal = bal_vals[-1]
    hi, lo = max(bal_vals), min(bal_vals)
    rank = len([v for v in bal_vals if v > this_bal]) + 1
    conclusions.append(dict(
        id='c1', title='本月收支总览', chart='balanceTrend',
        text=(f'本月收入 ¥{y(kpi["inc"]):,.2f}、支出 ¥{y(kpi["exp"]):,.2f}，结余 '
              f'¥{y(kpi["inc"] - kpi["exp"]):,.2f}，结余率 {saving if saving is not None else "—"}%；'
              f'该结余在近 {len(bal_vals)} 个月中排第 {rank} 位，区间 [{lo:,.2f}, {hi:,.2f}]。')
        if kpi['inc'] else '本月暂无收入记录，先记一笔收入吧。',
        hint='结论取自「近 6 月结余趋势」'))
    conclusions.append(dict(
        id='c2', title='支出结构', chart='catBar',
        text=(f'本月支出集中在「{top_cat["name"]}」，占 {top_cat["share"]}%'
              f'（¥{y(top_cat["cur"]):,.2f}）；Top3 分类累计占比 {pareto3 if pareto3 is not None else "—"}%，'
              f'符合「少数分类贡献多数支出」的帕累托特征。') if top_cat else '本月暂无支出记录。',
        hint='结论取自「分类支出与累计占比」'))
    mover = None
    if cat_cmp:
        mover = max(cat_cmp, key=lambda r: abs(r['delta']))
    conclusions.append(dict(
        id='c3', title='环比变化', chart='catCmp',
        text=(f'本月支出较上月{"上升" if (mom or 0) >= 0 else "下降"} {abs(mom):.1f}%'
              f'（上月 ¥{y(last["exp"]):,.2f} → 本月 ¥{y(kpi["exp"]):,.2f}）；'
              f'变化最大的分类是「{mover["name"]}」'
              f'（{"+" if mover["delta"] >= 0 else "−"}¥{abs(y(mover["delta"])):,.2f}）。')
        if mover and mom is not None else '缺少上月数据，无法计算环比。',
        hint='结论取自「本月 vs 上月分类对比」'))
    conclusions.append(dict(
        id='c4', title='预算执行', chart='budgetBar',
        text=(f'本月预算 ¥{y(bud["amount"]):,.2f}，已支出 ¥{y(kpi["exp"]):,.2f}，执行率 {bud_rate}%，'
              f'{"已超额 ¥" + format(y(kpi["exp"] - bud["amount"]), ",.2f") if (bud_rate or 0) > 100 else "剩余额度 ¥" + format(y(bud["amount"] - kpi["exp"]), ",.2f")}。'
              f'按当前节奏，日均支出 ¥{y(kpi["exp"] / days_passed if days_passed else 0):,.2f}。')
        if bud else '本月尚未设置预算，可到「预算设置」页填写。',
        hint='结论取自「预算执行」'))
    conclusions.append(dict(
        id='c5', title='消费节奏', chart='weekdayBar',
        text=(f'本月支出最多的一天是 {biggest["trans_date"]}（¥{y(biggest["amount"]):,.2f}，'
              f'{biggest["cat_name"]}）；按星期看，{top_wk["name"]} 支出最高'
              f'（¥{top_wk["amt"]:,.2f}），笔均支出 ¥{y(kpi["exp"] / kpi["cnt"] if kpi["cnt"] else 0):,.2f}。')
        if biggest and top_wk else '本月暂无足够数据。',
        hint='结论取自「按星期支出分布」'))

    return render_template('report.html', kpi=kpi, last=last, cat_cmp=cat_cmp, pareto3=pareto3,
                           biggest=biggest, weekday_series=weekday_series, daily_series=daily_series,
                           bal_series=bal_series, bud=bud, bud_rate=bud_rate, mom=mom,
                           cur_m=cur_m, conclusions=conclusions,
                           scope=('all' if is_global() else 'me'))


# ---------------- 指标口径文档 ----------------
@app.route('/metrics')
@login_required
def metrics():
    groups = {}
    for m in METRICS:
        groups.setdefault(m['page'], []).append(m)
    return render_template('metrics.html', groups=groups, total=len(METRICS),
                           metric_map=METRIC_MAP)


# ---------------- SQL 工作台 ----------------
FORBIDDEN = ('insert', 'update', 'delete', 'drop', 'alter', 'attach', 'detach',
             'pragma', 'create', 'replace', 'vacuum', 'reindex', 'grant')


def run_select(sql, max_rows=200):
    """只允许单条 SELECT: 校验 + 执行 + 计时。返回 (cols, rows, elapsed_ms)。"""
    s = (sql or '').strip().rstrip(';').strip()
    if not s:
        raise ValueError('SQL 不能为空')
    low = s.lower()
    if not (low.startswith('select') or low.startswith('with')):
        raise ValueError('只允许执行 SELECT / WITH 查询')
    if ';' in s:
        raise ValueError('只允许执行单条语句(不能含分号)')
    for kw in FORBIDDEN:
        if re.search(r'(?<![a-z_])' + kw + r'(?![a-z_])', low):
            raise ValueError(f'查询中不允许出现关键字: {kw.upper()}')
    import time as _t
    t0 = _t.perf_counter()
    rows = query(s)
    elapsed = (_t.perf_counter() - t0) * 1000
    cols = list(rows[0].keys()) if rows else []
    return cols, rows[:max_rows], elapsed


def render_sql(text, month, uid):
    return (text.replace('{{month}}', month or now_month())
                .replace('{{uid}}', str(uid)))


@app.route('/sql-lab', methods=['GET', 'POST'])
@login_required
def sql_lab():
    templates = query('SELECT * FROM sql_queries ORDER BY id')
    qid = request.values.get('qid', '') or (str(templates[0]['id']) if templates else '')
    month = request.values.get('month', '') or now_month()
    uid = request.values.get('uid', '') or str(g.user['id'])
    if not re.match(r'^\d{4}-\d{2}$', month):
        month = now_month()
    if not str(uid).isdigit():
        uid = str(g.user['id'])
    custom = (request.form.get('custom_sql') or '').strip() if request.method == 'POST' else ''
    sql_text, title, desc, cols, rows, err, elapsed = '', '', '', [], [], None, None

    if custom:
        sql_text, title, desc = custom, '自定义查询', '手动编辑后执行, 仅允许 SELECT'
    elif qid.isdigit():
        q = query_one('SELECT * FROM sql_queries WHERE id=?', (int(qid),))
        if q:
            sql_text = render_sql(q['sql_text'], month, uid)
            title, desc = q['title'], q['description']
    if sql_text:
        try:
            cols, rows, elapsed = run_select(sql_text)
        except Exception as e:  # noqa: BLE001 - 把真实错误展示给用户
            err = str(e)
    return render_template('sql_lab.html', templates=templates, qid=qid, month=month, uid=uid,
                           sql_text=sql_text, title=title, desc=desc, cols=cols, rows=rows,
                           err=err, elapsed=elapsed, now_month=now_month())


# ---------------- 用户活跃: 留存 / 漏斗 ----------------
@app.route('/activity')
@login_required
def activity():
    today = today_str()
    # 按日留存(D1 / D7)
    daily = query('''SELECT e.event_date d,
                       COUNT(DISTINCT e.user_id) dau,
                       COUNT(DISTINCT n1.user_id) d1,
                       COUNT(DISTINCT n7.user_id) d7
                     FROM user_events e
                     LEFT JOIN user_events n1 ON n1.user_id=e.user_id AND n1.event_date=date(e.event_date,'+1 day')
                     LEFT JOIN user_events n7 ON n7.user_id=e.user_id AND n7.event_date=date(e.event_date,'+7 day')
                     GROUP BY d ORDER BY d''')
    # 汇总口径: 只统计观察窗口完整的日期
    cutoff7 = query_one("SELECT date('now','localtime','-7 day') d")['d']
    full1 = [r for r in daily if r['d'] < today]
    full7 = [r for r in daily if r['d'] <= cutoff7]
    d1_rate = ratio(sum(r['d1'] for r in full1), sum(r['dau'] for r in full1))
    d7_rate = ratio(sum(r['d7'] for r in full7), sum(r['dau'] for r in full7))
    ret_series = [{'d': r['d'], 'dau': r['dau'],
                   'd1': ratio(r['d1'], r['dau']),
                   'd7': ratio(r['d7'], r['dau']) if r['d'] <= cutoff7 else None}
                  for r in daily]

    # 漏斗: 登录 → 记账 → 连续 3 天 → 连续 7 天 (窗口函数判连续)
    STREAK = '''WITH d AS (
                    SELECT DISTINCT user_id, event_date FROM user_events WHERE event_type='record'
                ), g AS (
                    SELECT user_id, event_date,
                           julianday(event_date) - ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY event_date) grp
                    FROM d
                )
                SELECT COUNT(DISTINCT user_id) c FROM (
                    SELECT user_id FROM g GROUP BY user_id, grp HAVING COUNT(*) >= {n}
                )'''
    f1 = query_one("SELECT COUNT(DISTINCT user_id) c FROM user_events WHERE event_type='login'")['c']
    f2 = query_one("SELECT COUNT(DISTINCT user_id) c FROM user_events WHERE event_type='record'")['c']
    f3 = query_one(STREAK.format(n=3))['c']
    f4 = query_one(STREAK.format(n=7))['c']
    funnel = [{'name': '打开App(登录)', 'value': f1}, {'name': '记一笔', 'value': f2},
              {'name': '连续记账3天', 'value': f3}, {'name': '连续记账7天', 'value': f4}]
    for i, s in enumerate(funnel):
        s['conv'] = 100.0 if i == 0 else ratio(s['value'], funnel[0]['value'])
        s['step'] = 100.0 if i == 0 else ratio(s['value'], funnel[i - 1]['value'])

    # 同期群热力图(按首次活跃日, 观察 D0~D7)
    cohort_rows = query('''WITH first_seen AS (
                              SELECT user_id, MIN(event_date) fs FROM user_events GROUP BY user_id
                           )
                           SELECT f.fs cohort, COUNT(*) size FROM first_seen f GROUP BY f.fs ORDER BY f.fs''')
    heat = query('''WITH first_seen AS (
                        SELECT user_id, MIN(event_date) fs FROM user_events GROUP BY user_id
                    )
                    SELECT f.fs cohort,
                           CAST(julianday(e.event_date)-julianday(f.fs) AS INTEGER) dn,
                           COUNT(DISTINCT e.user_id) users
                    FROM user_events e JOIN first_seen f ON f.user_id=e.user_id
                    WHERE julianday(e.event_date)-julianday(f.fs) BETWEEN 0 AND 7
                    GROUP BY cohort, dn''')
    idx = {r['cohort']: i for i, r in enumerate(cohort_rows)}
    size = {r['cohort']: r['size'] for r in cohort_rows}
    heat_data = [[r['dn'], idx[r['cohort']], ratio(r['users'], size[r['cohort']])] for r in heat]
    cohort_labels = [r['cohort'][5:] for r in cohort_rows]
    cohort_sizes = [r['size'] for r in cohort_rows]

    # 事件类型分布 + 日均记账笔数
    ev_types = query('''SELECT event_type, COUNT(*) cnt, COUNT(DISTINCT user_id) users
                        FROM user_events GROUP BY event_type ORDER BY cnt DESC''')
    per_day = query_one('''SELECT COUNT(*)*1.0/COUNT(DISTINCT event_date) v
                           FROM user_events WHERE event_type='record' ''')['v']
    users_total = query_one('SELECT COUNT(DISTINCT user_id) c FROM user_events')['c']
    days_total = query_one('SELECT COUNT(DISTINCT event_date) c FROM user_events')['c']
    dau_avg = query_one('SELECT AVG(dau) v FROM (SELECT event_date, COUNT(DISTINCT user_id) dau FROM user_events GROUP BY event_date)')['v']

    return render_template('activity.html', daily=daily, ret_series=ret_series,
                           d1_rate=d1_rate, d7_rate=d7_rate, funnel=funnel,
                           heat_data=heat_data, cohort_labels=cohort_labels,
                           cohort_sizes=cohort_sizes, ev_types=ev_types,
                           per_day=per_day, users_total=users_total,
                           days_total=days_total, dau_avg=dau_avg, today=today)


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
