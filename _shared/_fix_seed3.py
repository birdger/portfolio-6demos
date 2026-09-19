# -*- coding: utf-8 -*-
import py_compile

path = r'G:\code\hermes_project\proj3-inventory-logistics\seed.py'
src = open(path, encoding='utf-8').read()
old = """                seq = ['created', 'picked', 'in_transit', 'delivering', 'signed']
                                if wb_status == 'exception':
                                    seq = ['created', 'picked', 'in_transit', 'delivering', 'exception']
                                idx = seq.index(wb_status)
                                for j, st in enumerate(seq[:idx + 1]):
                    remark = '' if j < idx else ('已签收' if st == 'signed' else ('异常：破损拒收' if st == 'delivering' and wb_status == 'exception' else ''))
                    rk = st if (st == 'delivering' and wb_status == 'exception') else remark
                    db.execute('''INSERT INTO waybill_tracks(waybill_id,status,remark,created_at)
                                  VALUES(?,?,?,?)''', (wbid, st, rk, now_str(datetime.now() - timedelta(days=days_ago, hours=idx - j))))"""
new = """                seq = ['created', 'picked', 'in_transit', 'delivering', 'signed']
                if wb_status == 'exception':
                    seq = ['created', 'picked', 'in_transit', 'delivering', 'exception']
                idx = seq.index(wb_status)
                for j, st in enumerate(seq[:idx + 1]):
                    remark = '' if j < idx else ('已签收' if st == 'signed' else ('异常：破损拒收' if st == 'delivering' and wb_status == 'exception' else ''))
                    rk = st if (st == 'delivering' and wb_status == 'exception') else remark
                    db.execute('''INSERT INTO waybill_tracks(waybill_id,status,remark,created_at)
                                  VALUES(?,?,?,?)''', (wbid, st, rk, now_str(datetime.now() - timedelta(days=days_ago, hours=idx - j))))"""
assert old in src, 'block not found'
open(path, 'w', encoding='utf-8').write(src.replace(old, new))
py_compile.compile(path, doraise=True)
print('OK')