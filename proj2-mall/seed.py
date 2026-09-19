# -*- coding: utf-8 -*-
"""网上商城 种子数据: python seed.py (幂等)。"""
import os, random
os.environ.setdefault('WERKZEUG_RUN_MAIN', '1')
from datetime import datetime, timedelta
import app as _app  # noqa: F401 导入即建表
from werkzeug.security import generate_password_hash
from db import query, query_one, execute, execute_many

random.seed(7)


def seed_users():
    base = [
        ('admin', 'admin123', '系统管理员', 'admin'),
        ('ops',   'ops123',   '运营小李',   'operator'),
        ('demo',  'demo123',  '演示用户',   'user'),
        ('buyer', 'buyer123', '剁手会员小张', 'user'),
    ]
    for u, p, n, r in base:
        if not query_one('SELECT id FROM users WHERE username=?', (u,)):
            execute('INSERT INTO users(username,password_hash,display_name,role) VALUES(?,?,?,?)',
                    (u, generate_password_hash(p), n, r))


def seed_demo():
    # ---- 分类 ----
    if not query_one('SELECT id FROM categories'):
        execute_many('INSERT INTO categories(name,sort) VALUES(?,?)', [
            ('美妆护肤', 1), ('个人护理', 2), ('数码配件', 3), ('家居日用', 4), ('食品饮料', 5),
        ])
    cat = {r['name']: r['id'] for r in query('SELECT id,name FROM categories')}

    # ---- 商品 (13 个, 含 1 个下架 + 1 个库存 3 用于抢购) ----
    if not query_one('SELECT id FROM products'):
        products = [
            ('小气泡氨基酸洁面乳 120g', '温和不紧绷, 敏感肌可用', cat['美妆护肤'], '洁面乳', '白色',
             4900, 6900, 200, 'on'),
            ('玻尿酸补水面膜 10 片装', '深层补水, 妆前急救', cat['美妆护肤'], '面膜', '透明',
             6900, 9900, 150, 'on'),
            ('烟酰胺身体乳 400ml', '秋冬保湿, 提亮肤色', cat['个人护理'], '身体乳', '白色',
             5900, 7900, 120, 'on'),
            ('电动牙刷 声波款', 'IPX7 防水, 续航 30 天', cat['个人护理'], '电动牙刷', '蓝色',
             12900, 19900, 80, 'on'),
            ('智能手环 心率监测版', '1.47 寸彩屏, 20 天续航', cat['数码配件'], '手环', '曜石黑',
             15900, 22900, 60, 'on'),
            ('运动蓝牙耳机 半入耳式', '低延迟游戏模式, 双设备连接', cat['数码配件'], '耳机', '白色',
             9900, 14900, 100, 'on'),
            ('加厚不锈钢保温杯 500ml', '12 小时保温, 车载适用', cat['家居日用'], '保温杯', '磨砂黑',
             7900, 9900, 90, 'on'),
            ('香薰加湿器 大容量 2L', '静音夜灯, 缺水断电保护', cat['家居日用'], '加湿器', '白色',
             6900, 8900, 70, 'on'),
            ('每日坚果混合装 30 包', '7 种坚果科学配比', cat['食品饮料'], '坚果', '-',
             9900, 12900, 200, 'on'),
            ('冷萃黑咖啡 10 袋', '0 糖 0 脂, 配料表只有咖啡豆', cat['食品饮料'], '咖啡', '-',
             4900, 5900, 150, 'on'),
            ('眼线笔 极细防水款', '顺滑易画, 持久不晕染', cat['美妆护肤'], '眼线笔', '黑色',
             2900, 4500, 40, 'on'),
            ('待上新神秘商品', '敬请期待', cat['数码配件'], '神秘', '-',
             9900, 19900, 0, 'off'),
            ('惊爆秒杀-小样福袋', '手快有手慢无(库存仅 3, 用于抢购演示)', cat['美妆护肤'], '福袋', '随机',
             900, 1900, 3, 'on'),
        ]
        for title, sub, cid, spec, color, price, oprice, stock, st in products:
            execute('''INSERT INTO products(category_id,title,subtitle,main_image,color,size,price,original_price,stock,status)
                       VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (cid, title, sub, '', color, spec, price, oprice, stock, st))

    # ---- 优惠券 3 张 + 每人领 1 张 ----
    if not query_one('SELECT id FROM coupons'):
        execute_many('INSERT INTO coupons(title,type,threshold,value,stock,total,status) VALUES(?,?,?,?,?,?,?)', [
            ('新人满 100 减 20', 'full_reduce', 10000, 2000, 500, 500, 'on'),
            ('满 200 减 50', 'full_reduce', 20000, 5000, 300, 300, 'on'),
            ('全场 9 折券', 'discount', 0, 90, 200, 200, 'on'),
        ])
        for uid in [r['id'] for r in query('SELECT id FROM users WHERE role=?', ('user',))]:
            for cid2 in range(1, 4):
                execute('INSERT INTO user_coupons(user_id,coupon_id) VALUES(?,?)', (uid, cid2))

    # ---- 历史订单覆盖全部状态 ----
    if not query_one('SELECT id FROM orders'):
        buyer = query_one("SELECT id FROM users WHERE username='buyer'")['id']
        pid_map = {r['title']: r for r in query('SELECT * FROM products')}
        now = datetime.now()

        def make_order(days, status, ptitle, qty, coupon=None, paid=True, receiver='小张', address='北京市海淀区中关村大街 1 号'):
            p = pid_map[ptitle]
            pay_amount = p['price'] * qty
            disc = 0
            if coupon:
                disc = min(coupon['value'], pay_amount) if coupon['type'] == 'full_reduce' else pay_amount - pay_amount * coupon['value'] // 100
            pay = max(0, pay_amount - disc)
            ct = now - timedelta(days=days)
            paid_at = ct + timedelta(minutes=5) if paid else None
            stts = status
            pay_no = 'PAY' + ct.strftime('%Y%m%d%H%M') + str(random.randint(100, 999))
            execute('''INSERT INTO orders(order_no,user_id,total_amount,discount_amount,pay_amount,coupon_id,
                        address,receiver,status,pay_no,created_at,paid_at,shipped_at,finished_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (f'SN{ct.strftime("%Y%m%d%H%M%S")}', buyer, pay_amount, disc, pay,
                     coupon['id'] if coupon else None, address, receiver,
                     stts, pay_no, ct.strftime('%Y-%m-%d %H:%M:%S'),
                     paid_at.strftime('%Y-%m-%d %H:%M:%S') if paid_at else None,
                     (paid_at + timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S') if status == 'shipped' else None,
                     (paid_at + timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S') if status == 'finished' else None))
            oid = query_one('SELECT id FROM orders WHERE order_no=?', (f'SN{ct.strftime("%Y%m%d%H%M%S")}',))['id']
            execute('''INSERT INTO order_items(order_id,product_id,title,spec,price,qty)
                       VALUES(?,?,?,?,?,?)''', (oid, p['id'], p['title'], p['size'], p['price'], qty))
            if paid:
                execute('''INSERT INTO pay_logs(pay_no,order_id,amount,status,created_at)
                           VALUES(?,?,?,?,?)''', (pay_no, oid, pay, 'paid', ct.strftime('%Y-%m-%d %H:%M:%S')))
            return oid

        c100 = query_one("SELECT * FROM coupons WHERE title='新人满 100 减 20'")
        c200 = query_one("SELECT * FROM coupons WHERE title='满 200 减 50'")
        c90 = query_one("SELECT * FROM coupons WHERE title='全场 9 折券'")
        # 已完成 2 条(含评价)
        o1 = make_order(8, 'finished', '玻尿酸补水面膜 10 片装', 2, c100)
        o2 = make_order(5, 'finished', '智能手环 心率监测版', 1, c90)
        # 已发货 1 条
        make_order(1, 'shipped', '运动蓝牙耳机 半入耳式', 1, None)
        # 待付款 1 条(未支付, 供延迟关单演示)
        make_order(0, 'pending', '电动牙刷 声波款', 1, None, paid=False)
        # 退款中 1 条
        o5 = make_order(2, 'paid', '香薰加湿器 大容量 2L', 1, None)
        pay_amt = query_one('SELECT pay_amount v FROM orders WHERE id=?', (o5,))['v']
        execute('INSERT INTO refunds(order_id,user_id,reason,status,amount,created_at) VALUES(?,?,?,?,?,?)',
                (o5, buyer, '收到货有磕碰, 申请退款', 'pending', pay_amt,
                 (now - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')))
        # 超时取消 1 条(已回滚库存)
        execute('''INSERT INTO orders(order_no,user_id,total_amount,discount_amount,pay_amount,
                    address,receiver,status,pay_no,created_at,canceled_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                ('SN0000000000000', buyer, 15900, 0, 15900, '北京市朝阳区', '小张',
                 'canceled', 'PAY0000000000000', (now - timedelta(days=3, hours=2)).strftime('%Y-%m-%d %H:%M:%S'),
                 (now - timedelta(days=2, hours=31)).strftime('%Y-%m-%d %H:%M:%S')))

        # 评论 5 条
        execute_many('''INSERT INTO comments(order_id,user_id,product_id,rating,content,created_at)
                        VALUES(?,?,?,?,?,?)''', [
            (o1, buyer, pid_map['玻尿酸补水面膜 10 片装']['id'], 5, '膜布很服帖, 补水效果明显', now.strftime('%Y-%m-%d %H:%M:%S')),
            (o1, buyer, pid_map['玻尿酸补水面膜 10 片装']['id'], 5, '活动价划算, 回购', ''),
            (o2, buyer, pid_map['智能手环 心率监测版']['id'], 4, '续航不错, 表盘好看', ''),
            (o2, buyer, pid_map['智能手环 心率监测版']['id'], 5, '心率挺准的', ''),
            (o2, buyer, pid_map['运动蓝牙耳机 半入耳式']['id'], 4, '音质可以, 延迟低', ''),
        ])

    print('seed done')


if __name__ == '__main__':
    seed_users()
    seed_demo()