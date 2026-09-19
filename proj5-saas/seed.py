# -*- coding: utf-8 -*-
"""种子数据：python seed.py  （幂等，可重复执行）

演示用多租户数据：
  平台侧：3 个套餐（免费版 / 专业版 / 企业版）、3 个租户（2 启用 1 停用）
  租户侧：朝阳区图书馆（专业版，数据多）、知行学院图书馆（免费版）、城东社区图书室（已停用）

安全约定：脚本只在终端打印「账号名 + 掩码后的 API Key」，绝不输出明文 Key。
"""
import os
import sys
import secrets

os.environ.setdefault('WERKZEUG_RUN_MAIN', '1')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from werkzeug.security import generate_password_hash  # noqa: E402

import app as _app  # noqa: E402  导入即建表（ensure_schema）
from db import query, query_one, execute, execute_many  # noqa: E402

# ---------------- 套餐 ----------------
PLANS = [
    # name,        user, book,  api_month, rate/min, price_month(分), sort
    ('免费版', 10, 100, 1000, 60, 0, 1),
    ('专业版', 50, 1000, 10000, 300, 29900, 2),
    ('企业版', 1000, 100000, 1000000, 3000, 199900, 3),
]

# ---------------- 租户 ----------------
TENANTS = [
    # name,             contact, phone,           plan_name, status,     expire
    ('朝阳区图书馆', '张馆长', '010-8888-1001', '专业版', 'active', '2027-06-30'),
    ('知行学院图书馆', '李老师', '0571-6666-2002', '免费版', 'active', '2027-01-31'),
    ('城东社区图书室', '王主任', '021-5555-3003', '免费版', 'disabled', '2026-12-31'),
]

# ---------------- 用户 ----------------
USERS = [
    # username, password,     display_name,        role,             tenant_name
    ('padmin', 'padmin123', '平台运营', 'platform_admin', None),
    ('tenanta', 'tenanta123', '张馆长（朝阳）', 'tenant_admin', '朝阳区图书馆'),
    ('auser', 'auser123', '小艾（朝阳读者）', 'tenant_user', '朝阳区图书馆'),
    ('tenantb', 'tenantb123', '李老师（知行）', 'tenant_admin', '知行学院图书馆'),
    ('tdis', 'tdis123', '王主任（城东）', 'tenant_admin', '城东社区图书室'),
]

# ---------------- 图书（书名带租户标记，便于肉眼与 grep 验证隔离） ----------------
BOOKS = {
    '朝阳区图书馆': [
        ('朝阳馆藏·城市记忆', '朝阳区文化馆', '地方文献', 4),
        ('朝阳馆藏·北京胡同考', '李文博', '地方文献', 3),
        ('朝阳馆藏·社区图书馆实务', '张明远', '管理实务', 5),
        ('朝阳馆藏·图书馆学导论', '吴慰慈', '图书馆学', 6),
        ('朝阳馆藏·信息组织', '马费成', '图书馆学', 4),
        ('朝阳馆藏·红楼梦', '曹雪芹', '文学', 8),
        ('朝阳馆藏·三体', '刘慈欣', '文学', 7),
        ('朝阳馆藏·平凡的世界', '路遥', '文学', 5),
        ('朝阳馆藏·人类简史', '尤瓦尔·赫拉利', '历史', 4),
        ('朝阳馆藏·万历十五年', '黄仁宇', '历史', 3),
        ('朝阳馆藏·中国通史', '吕思勉', '历史', 4),
        ('朝阳馆藏·Python 编程从入门到实践', 'Eric Matthes', '计算机', 6),
        ('朝阳馆藏·深入理解计算机系统', 'Randal Bryant', '计算机', 3),
        ('朝阳馆藏·算法导论', 'Cormen', '计算机', 4),
        ('朝阳馆藏·数据结构与算法分析', 'Weiss', '计算机', 3),
        ('朝阳馆藏·儿童绘本精选', '编辑部', '少儿', 10),
        ('朝阳馆藏·十万个为什么', '少儿社', '少儿', 8),
        ('朝阳馆藏·经济学的思维方式', 'Paul Heyne', '经济', 3),
        ('朝阳馆藏·国富论', '亚当·斯密', '经济', 2),
        ('朝阳馆藏·心理学与生活', 'Richard Gerrig', '心理', 4),
    ],
    '知行学院图书馆': [
        ('知行馆藏·高等数学（上册）', '同济大学', '教材', 12),
        ('知行馆藏·线性代数', '同济大学', '教材', 10),
        ('知行馆藏·大学物理', '程守洙', '教材', 8),
        ('知行馆藏·计算机网络', '谢希仁', '教材', 9),
        ('知行馆藏·操作系统概念', 'Silberschatz', '教材', 6),
        ('知行馆藏·数据库系统概论', '王珊', '教材', 7),
        ('知行馆藏·编译原理', 'Alfred Aho', '教材', 4),
        ('知行馆藏·概率论与数理统计', '盛骤', '教材', 9),
    ],
    '城东社区图书室': [
        ('城东馆藏·家常菜谱', '美食编辑部', '生活', 5),
        ('城东馆藏·养生百科', '健康之家', '生活', 4),
        ('城东馆藏·少儿百科', '少儿社', '少儿', 6),
        ('城东馆藏·故事会合订本', '故事会', '文学', 3),
        ('城东馆藏·园艺入门', '绿手指', '生活', 3),
        ('城东馆藏·太极拳图解', '体育社', '体育', 2),
    ],
}

READERS = ['刘一鸣', '陈思远', '赵晓彤', '孙浩然', '周雨欣', '吴嘉琪', '郑一凡', '王梓涵']

AUDIT_ACTIONS = [
    ('创建租户', '朝阳区图书馆（专业版）'),
    ('创建租户', '知行学院图书馆（免费版）'),
    ('创建租户', '城东社区图书室（免费版）'),
    ('停用租户', '城东社区图书室 —— 合同到期未续费'),
    ('登录平台后台', '平台运营'),
    ('调整套餐', '朝阳区图书馆：免费版 → 专业版'),
    ('生成 API Key', '租户 1（掩码 lsk_****）'),
    ('导出审计日志', '近 30 天全平台操作记录'),
    ('登录租户后台', '张馆长（朝阳）'),
    ('查看 API Key', '租户 1 管理员查看了完整 API Key'),
]


def _api_key():
    return 'lsk_' + secrets.token_hex(16)


def _clear():
    for t in ('rate_buckets', 'api_logs', 'api_keys', 'borrows', 'books', 'users', 'tenants', 'plans'):
        execute('DELETE FROM %s' % t)
    execute("DELETE FROM sqlite_sequence WHERE name IN "
            "('rate_buckets','api_logs','api_keys','borrows','books','users','tenants','plans')")


def seed_demo():
    """幂等：已存在租户数据则跳过（--reset 可强制重建）。"""
    reset = '--reset' in sys.argv
    if reset:
        _clear()
    if query_one('SELECT id FROM tenants LIMIT 1'):
        print('[seed] 已存在租户数据，跳过（如需重建：python seed.py --reset）')
        return

    plan_id = {}
    for name, uq, bq, aq, rate, price, sort in PLANS:
        pid = execute('''INSERT INTO plans(name,user_quota,book_quota,api_monthly_quota,
                                          api_rate_per_min,price_month,sort)
                         VALUES(?,?,?,?,?,?,?)''', (name, uq, bq, aq, rate, price, sort))
        plan_id[name] = pid

    tenant_id = {}
    for name, contact, phone, plan, status, expire in TENANTS:
        tid = execute('''INSERT INTO tenants(name,contact,phone,plan_id,status,expire_date)
                         VALUES(?,?,?,?,?,?)''', (name, contact, phone, plan_id[plan], status, expire))
        tenant_id[name] = tid
        execute('INSERT INTO api_keys(tenant_id,api_key,status) VALUES(?,?,?)',
                (tid, _api_key(), 'active'))

    for username, pwd, display, role, tname in USERS:
        execute('''INSERT INTO users(username,password_hash,display_name,role,tenant_id)
                   VALUES(?,?,?,?,?)''',
                (username, generate_password_hash(pwd), display, role,
                 tenant_id[tname] if tname else None))

    book_ids = {}
    for tname, rows in BOOKS.items():
        tid = tenant_id[tname]
        book_ids[tname] = []
        for title, author, category, stock in rows:
            bid = execute('''INSERT INTO books(tenant_id,title,author,category,stock,status)
                             VALUES(?,?,?,?,?,'available')''', (tid, title, author, category, stock))
            book_ids[tname].append(bid)

    # 借阅记录
    import itertools
    seq = itertools.cycle(READERS)
    for tname, bids in book_ids.items():
        tid = tenant_id[tname]
        n = {'朝阳区图书馆': 12, '知行学院图书馆': 5, '城东社区图书室': 3}[tname]
        for i in range(n):
            bid = bids[i % len(bids)]
            returned = (i % 3 == 0)
            execute('''INSERT INTO borrows(tenant_id,book_id,reader_name,borrow_date,return_date,status)
                       VALUES(?,?,?,datetime('now','localtime',?),
                              CASE WHEN ?=1 THEN datetime('now','localtime') ELSE NULL END,
                              CASE WHEN ?=1 THEN 'returned' ELSE 'borrowed' END)''',
                    (tid, bid, next(seq), '-%d days' % (i + 1), 1 if returned else 0,
                     1 if returned else 0))

    # API 调用日志（近 7 天，供图表使用）
    for tname, count in (('朝阳区图书馆', 10), ('知行学院图书馆', 6), ('城东社区图书室', 2)):
        tid = tenant_id[tname]
        key = query_one('SELECT api_key FROM api_keys WHERE tenant_id=?', (tid,))['api_key']
        masked = key[:8] + '****'
        for i in range(count):
            code = 200 if i % 5 != 0 else 429
            path = '/api/v1/books' if i % 2 == 0 else '/api/v1/borrows'
            execute('''INSERT INTO api_logs(tenant_id,api_key,path,status_code,created_at)
                       VALUES(?,?,?,?,datetime('now','localtime',?))''',
                    (tid, masked, path, code, '-%d days' % (i % 7)))

    for action, detail in AUDIT_ACTIONS:
        execute('''INSERT INTO audit_logs(user_id,username,action,detail,created_at)
                   VALUES(?,?,?,?,datetime('now','localtime'))''', (1, 'padmin', action, detail))

    print('[seed] 完成：套餐 %d / 租户 %d / 用户 %d / 图书 %d / 借阅 %d / API 日志 %d / 审计 %d'
          % (len(PLANS), len(TENANTS), len(USERS), sum(len(v) for v in BOOKS.values()),
             sum({'朝阳区图书馆': 12, '知行学院图书馆': 5, '城东社区图书室': 3}.values()),
             sum((10, 6, 2)), len(AUDIT_ACTIONS)))
    for tname, tid in tenant_id.items():
        k = query_one('SELECT api_key FROM api_keys WHERE tenant_id=?', (tid,))['api_key']
        print('       租户 #%d %-16s API Key: %s' % (tid, tname, k[:8] + '****（已掩码）'))
    print('[seed] 演示账号：padmin/padmin123(平台) · tenanta/tenanta123 · auser/auser123 '
          '· tenantb/tenantb123 · tdis/tdis123(已停用租户)')


if __name__ == '__main__':
    seed_demo()
    print('seed done')
