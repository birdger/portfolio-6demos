# -*- coding: utf-8 -*-
"""SQLite 助手: 每请求一连接 + dict 行 + 事务。所有项目共用此模式。"""
import os, sqlite3
from contextlib import contextmanager
from flask import g

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'data.db')

def _connect():
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db

def get_db():
    """请求内复用 g 连接; 应用上下文外(seed 脚本)退化为独立连接。"""
    try:
        if 'db' in g:
            return g.db
        db = _connect()
        g.db = db
        return db
    except RuntimeError:
        return _connect()

def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_app(app):
    app.teardown_appcontext(close_db)

def query(sql, args=()):
    return [dict(r) for r in get_db().execute(sql, args).fetchall()]

def query_one(sql, args=()):
    r = get_db().execute(sql, args).fetchone()
    return dict(r) if r else None

def execute(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid

def execute_many(sql, seq):
    db = get_db()
    db.executemany(sql, seq)
    db.commit()

@contextmanager
def transaction():
    """多语句原子操作: with transaction() as db: db.execute(...)"""
    db = get_db()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise

def table_exists(name):
    r = query_one("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return r is not None
