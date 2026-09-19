# -*- coding: utf-8 -*-
"""种子数据: 图书借阅管理系统的演示数据集。

幂等: 每张表为空时才写入, 可重复执行。
运行: .venv/Scripts/python.exe seed.py
"""
import os
from datetime import date, timedelta

os.environ.setdefault('WERKZEUG_RUN_MAIN', '1')

import app as _app  # noqa: E402  (导入即建表)
from db import query, query_one, execute, transaction  # noqa: E402

FINE_PER_DAY = _app.FINE_PER_DAY
T = date.today()


def d(offset):
    """相对今天偏移若干天的日期字符串。"""
    return (T + timedelta(days=offset)).strftime('%Y-%m-%d')


# ---------------- 用户 ----------------
USERS = [
    ('admin', 'admin123', '系统管理员', 'admin'),
    ('ops', 'ops123', '借阅台操作员', 'operator'),
    ('demo', 'demo123', '演示读者', 'user'),
]


def seed_users():
    for username, pwd, name, role in USERS:
        if not query_one('SELECT id FROM users WHERE username=?', (username,)):
            execute('INSERT INTO users(username,password_hash,display_name,role) VALUES(?,?,?,?)',
                    (username, _app.generate_password_hash(pwd), name, role))


# ---------------- 图书 ----------------
# (书名, ISBN, 作者, 分类, 出版社, 单价分, 总量, 库存, 状态)
BOOKS = [
    ('活着', '9787506365437', '余华', '文学', '作家出版社', 2800, 5, 5, 'on_shelf'),
    ('三体(全集)', '9787536692930', '刘慈欣', '文学', '重庆出版社', 8800, 6, 4, 'on_shelf'),
    ('百年孤独', '9787544253994', '加西亚·马尔克斯', '文学', '南海出版公司', 3950, 4, 4, 'on_shelf'),
    ('平凡的世界', '9787530216781', '路遥', '文学', '北京十月文艺出版社', 10800, 5, 5, 'on_shelf'),
    ('深入理解计算机系统', '9787111544937', 'Randal E. Bryant', '计算机', '机械工业出版社', 13900, 4, 3, 'on_shelf'),
    ('算法导论', '9787111407010', 'Thomas H. Cormen', '计算机', '机械工业出版社', 12800, 3, 3, 'on_shelf'),
    ('Python编程:从入门到实践', '9787115546081', 'Eric Matthes', '计算机', '人民邮电出版社', 8900, 2, 0, 'on_shelf'),
    ('重构:改善既有代码的设计', '9787115508645', 'Martin Fowler', '计算机', '人民邮电出版社', 11800, 3, 2, 'on_shelf'),
    ('代码整洁之道', '9787115216878', 'Robert C. Martin', '计算机', '人民邮电出版社', 5900, 4, 4, 'on_shelf'),
    ('穷查理宝典', '9787508643823', '查理·芒格', '经管', '中信出版社', 8800, 3, 3, 'on_shelf'),
    ('影响力', '9787559615015', '罗伯特·西奥迪尼', '经管', '北京联合出版公司', 6800, 4, 4, 'on_shelf'),
    ('卓有成效的管理者', '9787111263654', '彼得·德鲁克', '经管', '机械工业出版社', 4500, 3, 3, 'on_shelf'),
    ('万历十五年', '9787101054491', '黄仁宇', '历史', '中华书局', 3200, 4, 3, 'on_shelf'),
    ('人类简史', '9787508647357', '尤瓦尔·赫拉利', '历史', '中信出版社', 6800, 5, 5, 'on_shelf'),
    ('时间简史', '9787535732309', '史蒂芬·霍金', '科普', '湖南科学技术出版社', 4500, 2, 2, 'on_shelf'),
    ('数学分析新讲(第一册)', '9787301016671', '张筑生', '教材', '北京大学出版社', 5600, 2, 2, 'on_shelf'),
    ('操作系统概念(第7版)', '9787111268641', 'Abraham Silberschatz', '计算机', '机械工业出版社', 7900, 1, 1, 'off_shelf'),
]

# ---------------- 读者 ----------------
# (借书证号, 姓名, 手机号, 状态, 可借上限)
READERS = [
    ('R2024001', '张伟', '13800000001', 'normal', 5),
    ('R2024002', '李娜', '13800000002', 'normal', 5),
    ('R2024003', '王强', '13800000003', 'normal', 3),
    ('R2024004', '刘敏', '13800000004', 'suspended', 5),
    ('R2024005', '陈晨', '13800000005', 'normal', 5),
    ('R2024006', '赵磊', '13800000006', 'normal', 5),
]

# ---------------- 借阅单 ----------------
# (book_id, reader_id, 借出偏移, 应还偏移, 归还偏移, 状态, 罚金分, 已缴, 续借次数)
# 库存一致性: stock == total - count(status in ('borrowed','overdue'))
BORROWS = [
    # —— 已归还 8 条 (跨近三个月) ——
    (2, 1, -70, -40, -45, 'returned', 0, 0, 0),
    (5, 2, -60, -30, -33, 'returned', 0, 0, 0),
    (13, 3, -55, -25, -26, 'returned', 0, 0, 0),
    (7, 1, -50, -20, -18, 'returned', 0, 0, 0),
    (8, 4, -45, -15, -10, 'returned', 5 * FINE_PER_DAY, 1, 0),
    (11, 5, -40, -10, -12, 'returned', 0, 0, 0),
    (3, 2, -35, -5, -6, 'returned', 0, 0, 0),
    (15, 6, -30, 0, -2, 'returned', 0, 0, 0),
    # —— 借出中 5 条 ——
    (2, 5, -28, 2, None, 'borrowed', 0, 0, 1),
    (7, 3, -25, 5, None, 'borrowed', 0, 0, 0),
    (5, 6, -20, 10, None, 'borrowed', 0, 0, 0),
    (8, 1, -18, 12, None, 'borrowed', 0, 0, 0),
    (13, 2, -15, 15, None, 'borrowed', 0, 0, 0),
    # —— 逾期 2 条 (未还, 罚金未缴) ——
    (2, 4, -45, -15, None, 'overdue', 15 * FINE_PER_DAY, 0, 0),
    (7, 6, -40, -8, None, 'overdue', 8 * FINE_PER_DAY, 0, 0),
    # —— 挂失 1 条 (按书价赔偿) ——
    (15, 5, -60, -30, None, 'lost', 4500, 0, 0),
]

# ---------------- 预约 ----------------
# (book_id, reader_id, 创建偏移, 状态)
RESERVATIONS = [
    (7, 1, -3, 'ready'),
    (7, 2, -2, 'waiting'),
    (15, 3, -35, 'done'),
]

# ---------------- 操作日志 ----------------
# (user_id, 操作, 详情, 偏移天数)
LOGS = [
    (1, '登录', '管理员 admin 登录系统', -6),
    (2, '新增图书', '新增《数学分析新讲(第一册)》 2 册', -5),
    (2, '借书', '《深入理解计算机系统》借给 赵磊(R2024006), 应还 %s' % d(10), -4),
    (2, '还书', '《影响力》已归还, 无逾期', -4),
    (1, '导入图书', 'CSV 批量导入: 成功 3 条, 失败 0 条', -3),
    (2, '预约', '张伟(R2024001) 预约《Python编程:从入门到实践》', -3),
    (2, '续借', '《三体(全集)》续借成功, 应还日期延长 30 天', -2),
    (1, '新增读者', '新增读者 赵磊(R2024006)', -2),
    (2, '借书', '《万历十五年》借给 李娜(R2024002), 应还 %s' % d(15), -1),
    (1, '标记罚款', '《重构:改善既有代码的设计》逾期罚款 2.50 元已缴纳', -1),
]


def _table_empty(name):
    return query_one('SELECT COUNT(*) c FROM %s' % name)['c'] == 0


def seed_books():
    if not _table_empty('books'):
        return
    with transaction() as conn:
        for b in BOOKS:
            conn.execute('INSERT INTO books(title,isbn,author,category,publisher,price,total,stock,status)'
                         ' VALUES(?,?,?,?,?,?,?,?,?)', b)


def seed_readers():
    if not _table_empty('readers'):
        return
    demo = query_one("SELECT id FROM users WHERE username='demo'")
    with transaction() as conn:
        for i, r in enumerate(READERS):
            uid = demo['id'] if (i == 0 and demo) else None
            conn.execute('INSERT INTO readers(card_no,name,phone,status,borrow_limit,user_id)'
                         ' VALUES(?,?,?,?,?,?)', (r[0], r[1], r[2], r[3], r[4], uid))


def seed_borrows():
    if not _table_empty('borrows'):
        return
    with transaction() as conn:
        for book_id, reader_id, bo, du, re, st, fine, paid, rc in BORROWS:
            conn.execute(
                'INSERT INTO borrows(book_id,reader_id,borrow_date,due_date,return_date,'
                'status,fine,fine_paid,renew_count) VALUES(?,?,?,?,?,?,?,?,?)',
                (book_id, reader_id, d(bo), d(du), d(re) if re is not None else None,
                 st, fine, paid, rc))


def seed_reservations():
    if not _table_empty('reservations'):
        return
    with transaction() as conn:
        for book_id, reader_id, off, st in RESERVATIONS:
            conn.execute('INSERT INTO reservations(book_id,reader_id,status,created_at)'
                         ' VALUES(?,?,?,?)', (book_id, reader_id, st, d(off) + ' 10:00:00'))


def seed_logs():
    if not _table_empty('logs'):
        return
    with transaction() as conn:
        for uid, action, detail, off in LOGS:
            conn.execute('INSERT INTO logs(user_id,action,detail,created_at) VALUES(?,?,?,?)',
                         (uid, action, detail, d(off) + ' 09:30:00'))


def seed_demo():
    seed_books()
    seed_readers()
    seed_borrows()
    seed_reservations()
    seed_logs()


if __name__ == '__main__':
    with _app.app.app_context():
        seed_users()
        seed_demo()
        for t in ('users', 'books', 'readers', 'borrows', 'reservations', 'logs'):
            n = query_one('SELECT COUNT(*) c FROM %s' % t)['c']
            print('  %-13s %3d 行' % (t, n))
    print('seed done')
