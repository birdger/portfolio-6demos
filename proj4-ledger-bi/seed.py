# -*- coding: utf-8 -*-
"""proj4-ledger-bi 种子数据: python seed.py (幂等)。"""
import os, random
os.environ.setdefault('WERKZEUG_RUN_MAIN', '1')
from datetime import date, datetime, timedelta
import app as _app  # noqa: F401 导入即建表
from werkzeug.security import generate_password_hash
from db import query, query_one, execute, execute_many

random.seed(2026)


def seed_users():
    base = [
        ('admin', 'admin123', '系统管理员', 'admin'),
        ('ops',   'ops123',   '业务操作员', 'operator'),
        ('demo',  'demo123',  '演示用户',   'user'),
    ]
    for u, p, n, r in base:
        if not query_one('SELECT id FROM users WHERE username=?', (u,)):
            execute('INSERT INTO users(username,password_hash,display_name,role) VALUES(?,?,?,?)',
                    (u, generate_password_hash(p), n, r))


def seed_demo():
    # ---------- 分类字典 ----------
    if not query_one('SELECT id FROM categories'):
        cats = [
            # (name, type, parent_name or None)
            ('工资薪金', 'income', None), ('兼职外快', 'income', None), ('理财收益', 'income', None), ('红包礼金', 'income', None),
            ('餐饮美食', 'expense', None), ('交通出行', 'expense', None), ('购物消费', 'expense', None),
            ('居住水电', 'expense', None), ('娱乐休闲', 'expense', None), ('医疗健康', 'expense', None),
            ('学习教育', 'expense', None), ('日用杂项', 'expense', None),
            ('早午晚餐', 'expense', '餐饮美食'), ('外卖夜宵', 'expense', '餐饮美食'), ('零食饮料', 'expense', '餐饮美食'),
            ('公交地铁', 'expense', '交通出行'), ('打车加油', 'expense', '交通出行'),
            ('服饰鞋包', 'expense', '购物消费'), ('数码家电', 'expense', '购物消费'), ('美妆个护', 'expense', '购物消费'),
            ('房租', 'expense', '居住水电'), ('水电气', 'expense', '居住水电'), ('话费网费', 'expense', '居住水电'),
            ('电影演出', 'expense', '娱乐休闲'), ('运动健身', 'expense', '娱乐休闲'),
            ('药品门诊', 'expense', '医疗健康'), ('健身课', 'expense', '学习教育'), ('书籍课程', 'expense', '学习教育'),
        ]
        id_map = {}
        for name, typ, parent in cats:
            pid = None
            if parent:
                pid = id_map[parent]
            cur = execute('INSERT INTO categories(parent_id,name,type,sort) VALUES(?,?,?,?)',
                          (pid, name, typ, 0))
            id_map[name] = cur
    # ---------- 账户 ----------
    if not query_one('SELECT id FROM accounts'):
        execute_many('INSERT INTO accounts(name,type,balance) VALUES(?,?,?)', [
            ('现金钱包', '现金', 80000),
            ('工资卡', '银行卡', 1865000),
            ('支付宝', '支付宝', 42300),
            ('微信零钱', '微信', 12600),
        ])
    # ---------- 账目流水 ----------
    if not query_one('SELECT id FROM transactions'):
        c = {r['name']: r['id'] for r in query('SELECT id,name FROM categories')}
        acc = {r['name']: r['id'] for r in query('SELECT id,name FROM accounts')}
        users = [r['id'] for r in query('SELECT id,username FROM users ORDER BY id')]
        today = date.today()
        rows = []
        # 每人近 5 个月: 月初发工资(admin), 每天 1-3 笔支出
        for uid, salary, base_income in [(users[0], 1250000, 150000), (users[1], 900000, 80000), (users[2], None, 0)]:
            for m_off in range(5, -1, -1):
                ym = today - timedelta(days=1)
                month_start = (ym.replace(day=1) - timedelta(days=int(m_off * 31))).replace(day=1)
                if month_start > today:
                    continue
                next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
                days = (next_month - month_start).days
                if salary:
                    rows.append((uid, acc['工资卡'], c['工资薪金'], 'income', salary, f'{month_start.month}月工资', month_start.isoformat()))
                    rows.append((uid, acc['支付宝'], c['理财收益'], 'income', 38000 + m_off * 1200, '低波理财收益', (month_start + timedelta(days=12)).isoformat()))
                    rows.append((uid, acc['微信零钱'], c['兼职外快'], 'income', base_income, '周末兼职', (month_start + timedelta(days=17)).isoformat()))
                n_days = min(days, max(1, (today - month_start).days + 1))
                for d in range(n_days):
                    if random.random() < 0.12:
                        continue
                    dt = month_start + timedelta(days=d)
                    picks = random.choices(
                        [(c['早午晚餐'], 1500, 4500), (c['外卖夜宵'], 1800, 6000), (c['零食饮料'], 500, 2500),
                         (c['公交地铁'], 400, 1600), (c['打车加油'], 2500, 9000), (c['服饰鞋包'], 8000, 60000),
                         (c['数码家电'], 20000, 150000), (c['美妆个护'], 6000, 40000), (c['电影演出'], 4000, 16000),
                         (c['运动健身'], 2000, 8000), (c['药品门诊'], 3000, 80000), (c['书籍课程'], 3000, 30000),
                         (c['日用杂项'], 1000, 8000), (c['房租'], 240000, 240000), (c['水电气'], 8000, 30000),
                         (c['话费网费'], 10000, 20000)],
                        k=1)[0]
                    cat_id, lo, hi = picks
                    if cat_id == c['房租'] and d != 1:
                        continue
                    if cat_id in (c['水电气'], c['话费网费']) and d not in (1, 15):
                        continue
                    amt = random.randint(lo, hi)
                    note = random.choice(['', '', '日常', '超市采购', '打车', '聚餐', '网购', '续费'])
                    rows.append((uid, random.choice(list(acc.values())), cat_id, 'expense', amt, note, dt.isoformat()))
        execute_many('''INSERT INTO transactions(user_id,account_id,category_id,type,amount,note,trans_date)
                        VALUES(?,?,?,?,?,?,?)''', rows)
    # ---------- 预算 ----------
    if not query_one('SELECT id FROM budgets'):
        uid = query_one("SELECT id FROM users WHERE username='admin'")['id']
        for m_off, amt in [(0, 550000), (1, 520000), (2, 580000)]:
            month = (today.replace(day=1) - timedelta(days=int(m_off * 31))).strftime('%Y-%m')
            execute('INSERT OR IGNORE INTO budgets(user_id,month,amount) VALUES(?,?,?)',
                    (uid, month, amt))
    # ---------- 模拟埋点(匿名行为用户 1001-1020) ----------
    if not query_one('SELECT id FROM user_events'):
        ev_rows = []
        for uid in range(1001, 1021):
            active_days = sorted(set(random.sample(range(30), random.randint(9, 22))))
            start = today - timedelta(days=29)
            for d in active_days:
                dt = (start + timedelta(days=d)).isoformat()
                ev_rows.append((uid, 'login', dt))
                ev_rows.append((uid, 'record', dt))
                if d in active_days and (d + 1) not in active_days:
                    ev_rows.append((uid, 'streak', dt))
        execute_many('''INSERT INTO user_events(user_id,event_type,event_date)
                        VALUES(?,?,?)''', ev_rows)
    # ---------- SQL 工作台内置模板 ----------
    if not query_one('SELECT id FROM sql_queries'):
        execute_many('''INSERT INTO sql_queries(title,description,sql_text) VALUES(?,?,?)''', [
            ('月度收支总览', '按月份聚合收入/支出/结余（支持 :month 筛选）',
             "SELECT strftime('%Y-%m', trans_date) AS 月份,\n       SUM(CASE WHEN type='income' THEN amount ELSE 0 END)/100.0 AS 收入_元,\n       SUM(CASE WHEN type='expense' THEN amount ELSE 0 END)/100.0 AS 支出_元,\n       SUM(CASE WHEN type='income' THEN amount ELSE -amount END)/100.0 AS 结余_元\nFROM transactions\nWHERE (:month = '' OR strftime('%Y-%m', trans_date) = :month)\n  AND (:uid = 0 OR user_id = :uid)\nGROUP BY 月份 ORDER BY 月份 DESC;"),
            ('一级分类支出排行', '本月按一级分类汇总支出金额与笔数',
             "SELECT COALESCE(p.name, c.name) AS 一级分类, COUNT(*) AS 笔数,\n       SUM(t.amount)/100.0 AS 支出_元\nFROM transactions t\nJOIN categories c ON c.id = t.category_id\nLEFT JOIN categories p ON p.id = c.parent_id\nWHERE t.type='expense' AND strftime('%Y-%m', t.trans_date) = :month\nGROUP BY 一级分类 ORDER BY 支出_元 DESC;"),
            ('用户留存 D1/D7（行为表）', '以首次活跃日为同期群，计算次日与第 7 天留存率',
             "WITH fs AS (SELECT user_id, MIN(event_date) AS first_day FROM user_events GROUP BY user_id),\n     act AS (SELECT DISTINCT user_id, event_date FROM user_events)\nSELECT fs.first_day AS 首活日, COUNT(DISTINCT fs.user_id) AS 群人数,\n       COUNT(DISTINCT CASE WHEN f1.event_date IS NOT NULL THEN fs.user_id END) AS D1留存,\n       COUNT(DISTINCT CASE WHEN f7.event_date IS NOT NULL THEN fs.user_id END) AS D7留存\nFROM fs\nLEFT JOIN act f1 ON f1.user_id=fs.user_id AND julianday(f1.event_date)-julianday(fs.first_day)=1\nLEFT JOIN act f7 ON f7.user_id=fs.user_id AND julianday(f7.event_date)-julianday(fs.first_day)=7\nGROUP BY fs.first_day ORDER BY fs.first_day DESC;"),
            ('连续记账最长记录(窗口函数)', '每个用户连续记账的最长天数',
             "WITH d AS (SELECT DISTINCT user_id, event_date FROM user_events WHERE event_type='record'),\n     g AS (SELECT user_id, event_date,\n                  julianday(event_date) - ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY event_date) AS grp\n           FROM d)\nSELECT user_id, COUNT(*) AS 连续天数\nFROM g GROUP BY user_id, grp\nORDER BY 连续天数 DESC LIMIT 10;"),
        ])
    print('seed done')


if __name__ == '__main__':
    seed_users()
    seed_demo()