# -*- coding: utf-8 -*-
"""种子数据: python seed.py  (幂等, 可重复执行)"""
import os, sys
os.environ.setdefault('WERKZEUG_RUN_MAIN', '1')
import app as _app  # noqa: F401  导入即执行 ensure_schema() 建表
from werkzeug.security import generate_password_hash
from db import query_one, execute, table_exists, transaction

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
    """项目专属演示数据: 子项目覆盖本函数, 幂等(先查重再插入)。"""
    pass

if __name__ == '__main__':
    seed_users()
    seed_demo()
    print('seed done')
