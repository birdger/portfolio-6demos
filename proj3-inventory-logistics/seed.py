# -*- coding: utf-8 -*-
"""proj7-inventory-logistics 种子数据: python seed.py (幂等, 可重复执行)。"""
import os, random
os.environ.setdefault('WERKZEUG_RUN_MAIN', '1')
from datetime import datetime, timedelta
import app as _app  # noqa: F401 导入即建表
from werkzeug.security import generate_password_hash
from db import query, query_one, execute, execute_many, transaction

random.seed(42)


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


def gen_no(prefix, table, col='no'):
    n = query_one(f'SELECT COUNT(*) c FROM {table}')['c'] + 1
    return f'{prefix}{n:04d}'


def now_str(dt=None):
    return (dt or datetime.now()).strftime('%Y-%m-%d %H:%M:%S')


def money(yuan):
    return int(round(float(yuan) * 100))


def seed_demo():
    # ---- 仓库 ----
    if not query_one('SELECT id FROM warehouses'):
        execute_many('INSERT INTO warehouses(name,address) VALUES(?,?)', [
            ('华东一号仓', '上海市青浦区华新镇 1 号'),
            ('华南二号仓', '广州市白云区太和镇 88 号'),
        ])
    wid_default = 1
    if not query_one('SELECT id FROM products'):
        products = [
            ('P1001', '氨基酸洗面奶', '洗护', '支', 18.5, 39.9, 120, 3),
            ('P1002', '烟酰胺身体乳', '洗护', '瓶', 25.0, 59.9, 80, 5),
            ('P1003', '玻尿酸精华液', '护肤', '瓶', 42.0, 129.0, 60, 5),
            ('P1004', '胶原蛋白面膜', '护肤', '盒', 30.0, 89.0, 100, 8),
            ('P1005', '润唇膏（淡香）', '彩妆', '支', 6.5, 25.0, 200, 10),
            ('P1006', '防水眼线笔', '彩妆', '支', 8.0, 35.0, 150, 10),
            ('P1007', '樱花柔肤水', '护肤', '瓶', 22.0, 79.0, 90, 6),
            ('P1008', '修护洗发水', '洗护', '瓶', 28.0, 69.0, 110, 5),
        ]
        for code, name, cat, unit, cost, sale, stock, safety in products:
            execute('''INSERT INTO products(code,name,category,unit,price,sale_price,stock,available_stock,frozen_stock,safety_stock,warehouse_id,status)
                       VALUES(?,?,?,?,?,?,?,?,0,?,1,'on')''',
                    (code, name, cat, unit, money(cost), money(sale), stock, stock, safety))
            execute('''INSERT INTO stock_ledger(product_id,warehouse_id,change_qty,before_qty,after_qty,type,ref_no,remark,user_id)
                       VALUES(?,?,?,0,?,'in',?,'期初建账',1)''', (query_one("SELECT id FROM products WHERE code=?", (code,))['id'], 1, stock, stock, f'INIT-{code}'))
    if not query_one('SELECT id FROM suppliers'):
        execute_many('INSERT INTO suppliers(name,contact,phone,address,status) VALUES(?,?,?,?,?)', [
            ('上海悦妆供应链', '王经理', '13800001001', '上海市嘉定区', 'on'),
            ('广州品汇日化', '李主管', '13800001002', '广州市白云区', 'on'),
        ])
    if not query_one('SELECT id FROM customers'):
        execute_many('INSERT INTO customers(name,contact,phone,address,status) VALUES(?,?,?,?,?)', [
            ('北京悦己美妆店', '刘老板', '13800002001', '北京市朝阳区', 'on'),
            ('线上旗舰店仓', '张主管', '13800002002', '北京市大兴区', 'on'),
        ])

    # 采购单
    if not query_one('SELECT id FROM purchase_orders'):
        pids = {r['code']: r['id'] for r in query('SELECT id,code FROM products')}
        sup = query_one("SELECT id FROM suppliers WHERE name='上海悦妆供应链'")['id']
        t0 = datetime.now() - timedelta(days=12)
        t1 = datetime.now() - timedelta(days=5)
        for i, ((c1, q1, pr1), (c2, q2, pr2)) in enumerate([
            (('P1001', 100, 18.5), ('P1002', 80, 25.0)),
            (('P1003', 60, 42.0), ('P1004', 100, 30.0)),
            (('P1005', 150, 6.5), ('P1006', 120, 8.0)),
        ]):
            no = f'PO{i+1:04d}'
            total = money(q1 * pr1 + q2 * pr2)
            ct = now_str(t0 + timedelta(days=i * 3))
            with transaction() as db:
                cur = db.execute('''INSERT INTO purchase_orders(no,supplier_id,total_amount,status,created_at,received_at,paid_at)
                                    VALUES(?,?,?,'received',?,?,?)''',
                                 (no, sup, total, ct, ct, now_str(t0 + timedelta(days=i * 3 + 1))))
                poid = cur.lastrowid
                for code, q, pr in ((c1, q1, pr1), (c2, q2, pr2)):
                    db.execute('INSERT INTO purchase_items(po_id,product_id,qty,price) VALUES(?,?,?,?)',
                               (poid, pids[code], q, money(pr)))
                    p = query_one('SELECT * FROM products WHERE id=?', (pids[code],))
                    db.execute('UPDATE products SET stock=stock+?, available_stock=available_stock+? WHERE id=?',
                               (q, q, pids[code]))
                    db.execute('''INSERT INTO stock_ledger(product_id,warehouse_id,change_qty,before_qty,after_qty,type,ref_no,remark,user_id)
                                  VALUES(?,?,?,?,?,'in',?,?,1)''',
                               (pids[code], wid_default, q, p['stock'], p['stock'] + q, no, f'采购入库 · {no}'))
                db.execute('''INSERT INTO payments(type,ref_type,ref_id,amount,remark,created_at)
                              VALUES('pay','purchase',?,?,?,?)''', (poid, total, f'采购货款 · {no}', ct))

    # 销售单 + 运单（含一笔已签收、一笔在途、一笔异常）
    if not query_one('SELECT id FROM sale_orders'):
        pids = {r['code']: r['id'] for r in query('SELECT id,code FROM products')}
        cust = query_one("SELECT id FROM customers WHERE name='北京悦己美妆店'")['id']
        flows = [
            (('P1001', 20, 39.9), ('P1002', 10, 59.9), 'signed', 8),
            (('P1003', 6, 129.0), ('P1004', 10, 89.0), 'delivering', 3),
            (('P1005', 30, 25.0), ('P1006', 20, 35.0), 'in_transit', 2),
            (('P1007', 8, 79.0), ('P1008', 12, 69.0), 'exception', 5),
        ]
        for i, (it1, it2, wb_status, days_ago) in enumerate(flows):
            no = f'SO{i+1:04d}'
            total = money(it1[1] * it1[2] + it2[1] * it2[2])
            so_ct = now_str(datetime.now() - timedelta(days=days_ago, hours=2))
            ship_ct = now_str(datetime.now() - timedelta(days=days_ago))
            with transaction() as db:
                cur = db.execute('''INSERT INTO sale_orders(no,customer_id,total_amount,status,created_at,shipped_at,finished_at)
                                    VALUES(?,?,?,'shipped',?,?,?)''',
                                 (no, cust, total, so_ct, ship_ct,
                                  ship_ct if wb_status == 'signed' else None))
                soid = cur.lastrowid
                for code, q, pr in (it1, it2):
                    pid = pids[code]
                    db.execute('INSERT INTO sale_items(so_id,product_id,qty,price) VALUES(?,?,?,?)',
                               (soid, pid, q, money(pr)))
                    p = query_one('SELECT * FROM products WHERE id=?', (pid,))
                    db.execute('UPDATE products SET stock=stock-?, available_stock=available_stock-? WHERE id=?',
                               (q, q, pid))
                    db.execute('''INSERT INTO stock_ledger(product_id,warehouse_id,change_qty,before_qty,after_qty,type,ref_no,remark,user_id)
                                  VALUES(?,?,?,?,?,'out',?,?,1)''',
                               (pid, wid_default, -q, p['stock'], p['stock'] - q, no, f'发货扣减 · {no}'))
                if wb_status != 'exception':
                    db.execute('''INSERT INTO payments(type,ref_type,ref_id,amount,remark,created_at)
                                  VALUES('receive','sale',?,?,?,?)''', (soid, total, f'销售回款 · {no}', ship_ct))
                wno = f'WB{i+1:04d}'
                cur2 = db.execute('''INSERT INTO waybills(no,sale_order_id,carrier,receiver,phone,address,status,created_at,signed_at)
                                     VALUES(?,?,?,?,?,?,?,?,?)''',
                                  (wno, soid, '宅急送', '刘老板', '13800002001', '北京市朝阳区望京 SOHO',
                                   wb_status, ship_ct, ship_ct if wb_status == 'signed' else None))
                wbid = cur2.lastrowid
                seq = ['created', 'picked', 'in_transit', 'delivering', 'signed']
                if wb_status == 'exception':
                    seq = ['created', 'picked', 'in_transit', 'delivering', 'exception']
                idx = seq.index(wb_status)
                for j, st in enumerate(seq[:idx + 1]):
                    remark = '' if j < idx else ('已签收' if st == 'signed' else ('异常：破损拒收' if st == 'delivering' and wb_status == 'exception' else ''))
                    rk = st if (st == 'delivering' and wb_status == 'exception') else remark
                    db.execute('''INSERT INTO waybill_tracks(waybill_id,status,remark,created_at)
                                  VALUES(?,?,?,?)''', (wbid, st, rk, now_str(datetime.now() - timedelta(days=days_ago, hours=idx - j))))

    # 一笔完成状态销售（测试完成链路）
    if not query_one("SELECT id FROM sale_orders WHERE status='finished'"):
        pids = {r['code']: r['id'] for r in query('SELECT id,code FROM products')}
        cust = query_one("SELECT id FROM customers WHERE name='线上旗舰店仓'")['id']
        no = 'SO9999'
        total = money(10 * 39.9)
        ct = now_str(datetime.now() - timedelta(days=20))
        with transaction() as db:
            cur = db.execute('''INSERT INTO sale_orders(no,customer_id,total_amount,status,created_at,shipped_at,finished_at)
                                VALUES(?,?,?,'finished',?,?,?)''', (no, cust, total, ct, ct, ct))
            soid = cur.lastrowid
            db.execute('INSERT INTO sale_items(so_id,product_id,qty,price) VALUES(?,?,?,?)',
                       (soid, pids['P1001'], 10, money(39.9)))
            p = query_one('SELECT * FROM products WHERE id=?', (pids['P1001'],))
            db.execute('UPDATE products SET stock=stock-10, available_stock=available_stock-10 WHERE id=?', (pids['P1001'],))
            db.execute('''INSERT INTO stock_ledger(product_id,warehouse_id,change_qty,before_qty,after_qty,type,ref_no,remark,user_id)
                          VALUES(?,?,?,?,?,'out',?,?,1)''',
                       (pids['P1001'], wid_default, -10, p['stock'], p['stock'] - 10, no, '发货扣减 · ' + no))
            db.execute('''INSERT INTO payments(type,ref_type,ref_id,amount,remark,created_at)
                          VALUES('receive','sale',?,?,?,?)''', (soid, total, '销售回款 · ' + no, ct))
        wb2 = query_one("SELECT id FROM waybills WHERE sale_order_id=?", (soid,))
        if not wb2:
            cur2 = execute('''INSERT INTO waybills(no,sale_order_id,carrier,receiver,phone,address,status,created_at,signed_at)
                              VALUES(?,?,?,?,?,?,'signed',?,?)''',
                           (f'WB{query_one("SELECT COUNT(*) c FROM waybills")["c"]+1:04d}', soid, '宅急送',
                            '张主管', '13800002002', '北京市大兴区', now_str(datetime.now() - timedelta(days=19)),
                            now_str(datetime.now() - timedelta(days=18))))
            wbid = cur2
            for j, st in enumerate(['created', 'picked', 'in_transit', 'delivering', 'signed']):
                execute('''INSERT INTO waybill_tracks(waybill_id,status,remark,created_at)
                           VALUES(?,?,?,?)''', (wbid, st, '已签收' if st == 'signed' else '',
                                                now_str(datetime.now() - timedelta(days=19, hours=4 - j))))

    print('seed done')


if __name__ == '__main__':
    seed_users()
    seed_demo()