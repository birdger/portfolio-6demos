# -*- coding: utf-8 -*-
"""
统一脚手架应用: 认证 + RBAC(admin/operator/user) + 用户管理 + 仪表盘。
扩展方法:
  1) 修改 APP_NAME / APP_SUBTITLE / PORT / NAV(侧边栏)
  2) 新增业务表: 在 ensure_schema() 里加 CREATE TABLE IF NOT EXISTS
  3) 新增路由: 在文件下方按 @app.route 继续添加, 用 @login_required / @role_required('admin') 保护
  4) 演示数据: 编辑 seed.py 的 seed_demo(), 运行 python seed.py
  5) 模板: 放 templates/, 继承 base.html, 覆盖 {% block content %}
货币一律用「分」整数存储; 时间用 'YYYY-MM-DD HH:MM:SS' 字符串。
"""
import os
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, g, abort)
from werkzeug.security import generate_password_hash, check_password_hash
import db as database
from db import query, query_one, execute, transaction

# ================== 项目配置(每个项目改这里) ==================
APP_NAME = '统一管理系统'
APP_SUBTITLE = 'Scaffold'
PORT = 8001
NAV = [
    ('dashboard', '仪表盘', '/', '📊'),
    ('users',     '用户管理', '/users', '👥'),
]
ROLE_LABELS = {'admin': '管理员', 'operator': '操作员', 'user': '普通用户'}
# ==============================================================

app = Flask(__name__)
app.secret_key = 'scaffold-dev-secret'
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
    # 项目扩展: 在此追加 CREATE TABLE IF NOT EXISTS ...

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

@app.route('/')
@login_required
def index():
    stats = {
        '用户数': query_one('SELECT COUNT(*) c FROM users')['c'],
        '角色数': len(ROLE_LABELS),
        '系统状态': '运行中',
    }
    return render_template('index.html', stats=stats)

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
