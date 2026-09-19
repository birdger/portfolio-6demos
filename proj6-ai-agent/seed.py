# -*- coding: utf-8 -*-
"""proj6-ai-agent 种子数据: python seed.py (幂等)。"""
import os, json
os.environ.setdefault('WERKZEUG_RUN_MAIN', '1')
import app as _app  # noqa: F401 导入即建表(含 ensure_schema)
from werkzeug.security import generate_password_hash
from db import query, query_one, execute
from app import _load_env

CORPUS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      '_shared', 'corpus', 'products.jsonl')


def seed_users():
    base = [
        ('admin', 'admin123', '系统管理员', 'admin'),
        ('ops',   'ops123',   '运营分析',   'operator'),
        ('demo',  'demo123',  '演示用户',   'user'),
    ]
    for u, p, n, r in base:
        if not query_one('SELECT id FROM users WHERE username=?', (u,)):
            execute('INSERT INTO users(username,password_hash,display_name,role) VALUES(?,?,?,?)',
                    (u, generate_password_hash(p), n, r))


def import_products(limit=300):
    n = 0
    if not os.path.exists(CORPUS):
        raise SystemExit('corpus not found: ' + CORPUS)
    with open(CORPUS, 'r', encoding='utf-8') as f:
        for line in f:
            if n >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            execute('''INSERT OR IGNORE INTO products(asin,title,brand,price_fen,main_category,categories,
                       leaf_category,description,image,rating,rating_count)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                    (str(d.get('asin') or '')[:32], (d.get('title') or '')[:300], (d.get('brand') or '')[:60],
                     int(d.get('price_fen') or 0), (d.get('main_category') or '')[:60],
                     json.dumps(d.get('categories') or [], ensure_ascii=False)[:300],
                     (d.get('leaf_category') or '')[:60], (d.get('description') or '')[:2000],
                     (d.get('image') or '')[:500], float(d.get('rating') or 0), int(d.get('rating_count') or 0)))
            n += 1
    return n


def build_eval_cases():
    """17 个域内用例: 期望值 = leaf_category 下 rating_count Top-5 ASIN(独立于检索索引的客观标签)。
    3 个域外用例: 期望为空。类目不足 17 个时依次放宽阈值, 尽量凑满 17。"""
    made = 0
    for thr in (15, 12, 10, 8):
        if made >= 14:
            break
        cats = query('''SELECT leaf_category, COUNT(*) c FROM products
                        WHERE leaf_category<>'' GROUP BY leaf_category HAVING c>=?
                        ORDER BY c DESC LIMIT 17''', (thr,))
        need = max(0, 14 - made)
        for cat in cats[:need]:
            exists = query_one('SELECT id FROM eval_cases WHERE category=?', (cat['leaf_category'],))
            if exists:
                continue
            top = query('''SELECT asin FROM products WHERE leaf_category=?
                           ORDER BY rating_count DESC, rating DESC LIMIT 5''', (cat['leaf_category'],))
            if not top:
                continue
            main, _, sub = cat['leaf_category'].partition('-')
            question = f"我想找{main}类的好东西，{'尤其是' + sub if sub else '性价比高的'}，有什么推荐？"
            expected = ' '.join(r['asin'] for r in top)
            execute('INSERT INTO eval_cases(question,expected_asins,category,kind) VALUES(?,?,?,?)',
                    (question, expected, cat['leaf_category'], 'in_domain'))
            made += 1
    out_domain = [
        ('推荐一款高性能游戏笔记本电脑, 预算一万以内, 打 3A 大作。', ''),
        ('帮我找一款适合布偶猫的猫粮, 需要幼猫配方。', ''),
        ('推荐一辆适合城市通勤的电动自行车。', ''),
    ]
    for q, exp in out_domain:
        if not query_one('SELECT id FROM eval_cases WHERE question=?', (q,)):
            execute('INSERT INTO eval_cases(question,expected_asins,category,kind) VALUES(?,?,?,?)',
                    (q, exp, 'out_domain', 'out_domain'))
            made += 1
    return made


def seed_demo():
    # settings 一行
    if not query_one('SELECT id FROM settings'):
        env = _load_env()
        model_a = env.get('OPENAI_MODEL') or 'deepseek-v4-flash'
        execute('''INSERT INTO settings(id,base_url,model_a,model_b,top_k,min_score,
                   price_in_fen_per_1k,price_out_fen_per_1k,corpus_limit)
                   VALUES(1,?,?,?,?,?,?,?,?)''',
                (env.get('OPENAI_BASE_URL', ''), model_a, 'glm-5.2-flash', 5, 1.0, 2, 8, 1500))
    n = import_products(1500)
    m = build_eval_cases()
    pc = query_one('SELECT COUNT(*) c FROM products')['c']
    cc = query_one('SELECT COUNT(*) c FROM eval_cases')['c']
    print(f'seed done: products={pc} (imported {n}), eval_cases={cc} ({m} new)')


if __name__ == '__main__':
    seed_users()
    seed_demo()