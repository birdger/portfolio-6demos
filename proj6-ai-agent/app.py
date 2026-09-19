# -*- coding: utf-8 -*-
"""
AI 商品推荐助手 (AI Agent · RAG) —— AI 产品经理作品集 Demo
五大能力: ① LLM 结构化富化 ② 纯 Python BM25 检索 ③ 对话推荐(强制引用商品) ④ 20用例×2模型量化评测
          ⑤ token 成本看板。LLM 走 siyu 中转, 不可用时离线兜底(诚实标注, 不伪造指标)。
约束: flask + stdlib; 金额整数分; 密钥只从 _shared/api.env 读取, 绝不落地/打印。
"""
import json, math, os, re, threading, time
from datetime import datetime
from functools import wraps
from urllib import request as urq
from urllib.error import URLError, HTTPError
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, g, abort, jsonify, Response)
from werkzeug.security import generate_password_hash, check_password_hash
import db as database
from db import query, query_one, execute, execute_many, transaction

# ================== 项目配置 ==================
APP_NAME = 'AI 商品推荐助手'
APP_SUBTITLE = 'AI Agent · RAG'
PORT = 8006
NAV = [
    ('chat',   '对话助手',   '/',      '💬'),
    ('products','商品库',    '/products', '📦'),
    ('enrich', '智能富化',   '/enrich', '✨'),
    ('eval',   '评测中心',   '/eval',  '📐'),
    ('cost',   '成本看板',   '/cost',  '💰'),
    ('logs',   '调用日志',   '/logs',  '📝'),
    ('settings','系统设置',  '/settings', '⚙️'),
    ('users',  '用户管理',   '/users', '👥'),
]
ROLE_LABELS = {'admin': '管理员', 'operator': '操作员', 'user': '普通用户'}
DEFAULT_SETTINGS = dict(base_url='', model_a='deepseek-v4-flash', model_b='glm-5.2-flash',
                        top_k=5, min_score=1.0, price_in_fen_per_1k=2, price_out_fen_per_1k=8,
                        corpus_limit=300)
# ==============================================

app = Flask(__name__)
app.secret_key = 'ai-agent-demo-dev-secret'
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
    execute('''CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asin TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        brand TEXT, price_fen INTEGER NOT NULL DEFAULT 0,
        main_category TEXT, categories TEXT, leaf_category TEXT,
        description TEXT, image TEXT, rating REAL, rating_count INTEGER,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    execute('''CREATE TABLE IF NOT EXISTS enrichments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL, model TEXT,
        summary TEXT, tags TEXT, audience TEXT, scenes TEXT,
        mode TEXT NOT NULL DEFAULT 'llm',
        prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
        cost_fen INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    execute('''CREATE TABLE IF NOT EXISTS eval_cases(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question TEXT NOT NULL, expected_asins TEXT NOT NULL DEFAULT '',
        category TEXT, kind TEXT NOT NULL DEFAULT 'in_domain',
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    execute('''CREATE TABLE IF NOT EXISTS eval_batches(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, model_a TEXT NOT NULL, model_b TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running',
        summary TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')), finished_at TEXT
    )''')
    execute('''CREATE TABLE IF NOT EXISTS eval_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER NOT NULL, case_id INTEGER NOT NULL, model TEXT NOT NULL,
        answer TEXT, retrieved TEXT, hit INTEGER NOT NULL DEFAULT 0,
        citation_ok INTEGER NOT NULL DEFAULT 0, refused INTEGER NOT NULL DEFAULT 0,
        latency_ms INTEGER NOT NULL DEFAULT 0,
        prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
        cost_fen INTEGER NOT NULL DEFAULT 0, mode TEXT NOT NULL DEFAULT 'offline',
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    execute('''CREATE TABLE IF NOT EXISTS chat_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, question TEXT, answer TEXT, retrieved TEXT,
        model TEXT, prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
        cost_fen INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0,
        mode TEXT NOT NULL DEFAULT 'offline',
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    execute('''CREATE TABLE IF NOT EXISTS settings(
        id INTEGER PRIMARY KEY CHECK (id=1),
        base_url TEXT, model_a TEXT, model_b TEXT,
        top_k INTEGER NOT NULL DEFAULT 5, min_score REAL NOT NULL DEFAULT 1.0,
        price_in_fen_per_1k INTEGER NOT NULL DEFAULT 2, price_out_fen_per_1k INTEGER NOT NULL DEFAULT 8,
        corpus_limit INTEGER NOT NULL DEFAULT 300
    )''')
    execute('CREATE INDEX IF NOT EXISTS idx_products_leaf ON products(leaf_category)')
    execute('CREATE INDEX IF NOT EXISTS idx_eruns_batch ON eval_runs(batch_id, model)')


def get_settings():
    s = query_one('SELECT * FROM settings WHERE id=1')
    if not s:
        return dict(DEFAULT_SETTINGS)
    return s


# ---------------- 凭据加载(密钥绝不打印/落库) ----------------
_ENV_CACHE = None


def _load_env():
    """解析 _shared/api.env, 返回 {'OPENAI_BASE_URL':..,'OPENAI_API_KEY':..,'OPENAI_MODEL':..}; 失败返回空 dict。"""
    global _ENV_CACHE
    if _ENV_CACHE is not None:
        return _ENV_CACHE
    env = {}
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '_shared', 'api.env')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    env[k.strip()] = v.strip()
    except OSError:
        pass
    _ENV_CACHE = env
    return env


def llm_available():
    env = _load_env()
    return bool(env.get('OPENAI_API_KEY') and env.get('OPENAI_BASE_URL'))


def llm_chat(messages, model=None, max_tokens=600):
    """调用中转站 chat/completions。返回 (text, pt, ct, mode); 任何异常 -> (None,0,0,'offline')。"""
    env = _load_env()
    base = env.get('OPENAI_BASE_URL', '').rstrip('/')
    key = env.get('OPENAI_API_KEY', '')
    model = model or env.get('OPENAI_MODEL') or 'deepseek-v4-flash'
    if not (base and key):
        return None, 0, 0, 'offline'
    payload = {'model': model, 'messages': messages, 'max_tokens': max_tokens, 'temperature': 0.4}
    body = json.dumps(payload).encode('utf-8')
    req = urq.Request(base + '/chat/completions', data=body, method='POST', headers={
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + key,
    })
    t0 = time.time()
    try:
        with urq.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except (URLError, HTTPError, TimeoutError, json.JSONDecodeError, OSError) as e:
        print(f'[llm] call failed ({type(e).__name__}), fallback offline')  # 不含 key
        return None, 0, 0, 'offline'
    latency = int((time.time() - t0) * 1000)
    try:
        text = data['choices'][0]['message']['content']
        pt = data.get('usage', {}).get('prompt_tokens', 0)
        ct = data.get('usage', {}).get('completion_tokens', 0)
    except (KeyError, IndexError, TypeError):
        return None, 0, 0, 'offline'
    return text, pt, ct, 'llm'


def cost_fen(pt, ct, s=None):
    s = s or get_settings()
    return round((pt / 1000.0) * (s['price_in_fen_per_1k'] or 0) + (ct / 1000.0) * (s['price_out_fen_per_1k'] or 0))


# ---------------- 纯 Python BM25 检索 ----------------
_CJK = re.compile(r'[\u4e00-\u9fff]')


def tokenize(text):
    if not text:
        return []
    toks = []
    for part in re.split(r'[^\w\u4e00-\u9fff]+', text.lower()):
        if not part:
            continue
        if _CJK.search(part):
            # 中文: 2-gram(单字时退回单字)
            if len(part) == 1:
                toks.append(part)
            else:
                toks.extend(part[i:i + 2] for i in range(len(part) - 1))
        else:
            toks.append(part)
    return toks


_INDEX = None


def _build_index(limit=None):
    """惰性构建 BM25 索引(商品子集), 缓存到模块级。"""
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    s = get_settings()
    n = limit or s['corpus_limit'] or 300
    rows = query('SELECT * FROM products ORDER BY id LIMIT ?', (n,))
    docs, dl, df = [], {}, {}
    for r in rows:
        text = ' '.join([r['title'] or '', r['brand'] or '', r['leaf_category'] or '',
                         r['categories'] or '', (r['description'] or '')[:400]])
        toks = tokenize(text)
        docs.append({'row': r, 'toks': toks})
        dl[len(docs) - 1] = len(toks)
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    N = len(docs)
    avgdl = (sum(dl.values()) / N) if N else 0.0
    k1, b = 1.5, 0.75
    idf = {t: math.log(1 + (N - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(q, k=5, min_score=1.0):
        qtoks = tokenize(q)
        if not qtoks or not docs:
            return []
        scored = []
        for i, d in enumerate(docs):
            if not d['toks']:
                continue
            tf = {}
            for t in d['toks']:
                tf[t] = tf.get(t, 0) + 1
            score = 0.0
            for t in qtoks:
                f = tf.get(t, 0)
                if f:
                    score += idf.get(t, 0) * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl[i] / avgdl))
            if score >= min_score:
                scored.append((score, d['row']))
        scored.sort(key=lambda x: -x[0])
        return [(row, round(score, 3)) for score, row in scored[:k]]

    _INDEX = (search, N)
    return _INDEX


def bm25_search(q, k=None, min_score=None):
    s = get_settings()
    search, N = _build_index()
    return search(q, k or s['top_k'] or 5, min_score if min_score is not None else s['min_score'])


def refuse_reply(q):
    return (f'【离线拒答】抱歉，我检索不到与「{q}」相关度足够的商品'
            f'（检索得分低于系统设定的阈值）。为避免幻觉，我不能编造推荐。'
            f'请换个更具体的商品描述，例如目标品类、肤质/场景、价格范围。')


# ---------------- 认证与权限(脚手架同款) ----------------
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
        'llm_available': llm_available(),
    }


# ---------------- 对话助手 ----------------
@app.route('/')
@login_required
def chat():
    history = query('SELECT * FROM chat_logs WHERE user_id=? ORDER BY id DESC LIMIT 20', (g.user['id'],))
    return render_template('chat.html', history=history)


@app.route('/chat', methods=['POST'])
@login_required
def chat_post():
    q = (request.form.get('q') or '').strip()
    if not q:
        return jsonify(ok=False, msg='请输入问题')
    s = get_settings()
    t0 = time.time()
    hits = bm25_search(q)
    top_score = hits[0][1] if hits else 0.0
    mode = 'llm' if llm_available() else 'offline'
    answer, pt, ct = None, 0, 0
    retrieved = json.dumps([{'asin': r['asin'], 'title': r['title']} for r, _ in hits], ensure_ascii=False)

    if not hits or top_score < s['min_score']:
        answer = refuse_reply(q)
        mode = 'offline' if mode == 'llm' else 'offline'
        hits = []
    elif mode == 'llm':
        ctx = '\n'.join(
            f"- {r['title']} | 品牌:{r['brand'] or '未知'} | 价格:{(r['price_fen'] or 0)/100:.2f}元"
            f" | 主题:{r['leaf_category'] or r['main_category'] or '通用'} | ASIN:{r['asin']}"
            f" | 描述:{(r['description'] or '')[:220]}"
            for r, _ in hits)
        messages = [
            {'role': 'system', 'content': (
                '你是电商推荐助手。只推荐上面给定商品列表里的商品，必须在回答中为每个推荐商品标注其 ASIN '
                '（格式：商品名（ASIN:XXXXXXXXXX））。若列表中没有匹配用户需求的商品，直接说明找不到，'
                '不得编造列表外商品。回答用简体中文，70 字以内，先给结论再列推荐。')},
            {'role': 'user', 'content': f'用户问题：{q}\n\n候选商品：\n{ctx}'},
        ]
        answer, pt, ct, mode = llm_chat(messages, model=s['model_a'])
        if mode == 'offline':
            answer = '【离线兜底】模型服务暂不可用，以下为检索结果，未经大模型生成：\n' + \
                     '\n'.join(f"- {r['title']}（ASIN:{r['asin']}）" for r, _ in hits[:s['top_k']])
    else:
        answer = '【离线兜底】以下为检索结果，未经大模型生成：\n' + \
                 '\n'.join(f"- {r['title']}（ASIN:{r['asin']}）" for r, _ in hits)

    latency = int((time.time() - t0) * 1000)
    cf = cost_fen(pt, ct, s)
    execute('''INSERT INTO chat_logs(user_id,question,answer,retrieved,model,prompt_tokens,completion_tokens,
              cost_fen,latency_ms,mode) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (g.user['id'], q, answer, retrieved, s['model_a'], pt, ct, cf, latency, mode))
    items = [{'asin': r['asin'], 'title': r['title'], 'price_fen': r['price_fen'],
              'leaf': r['leaf_category'] or r['main_category'] or '', 'score': sc, 'image': r['image'] or ''}
             for r, sc in hits]
    return jsonify(ok=True, answer=answer, mode=mode, items=items,
                   latency_ms=latency, tokens=pt + ct, cost_fen=cf, hit_count=len(hits))


@app.route('/logs')
@login_required
def logs():
    page = max(1, request.args.get('page', 1, type=int))
    per = 30
    total = query_one('SELECT COUNT(*) c FROM chat_logs')['c']
    pages = max(1, (total + per - 1) // per)
    rows = query('''SELECT l.*, u.display_name FROM chat_logs l
                    LEFT JOIN users u ON u.id=l.user_id
                    ORDER BY l.id DESC LIMIT ? OFFSET ?''', (per, (page - 1) * per))
    return render_template('logs.html', rows=rows, page=page, pages=pages, total=total)


# ---------------- 商品库 ----------------
@app.route('/products')
@login_required
def products():
    q = request.args.get('q', '').strip()
    leaf = request.args.get('leaf', '').strip()
    page = max(1, request.args.get('page', 1, type=int))
    per = 20
    where, args = ['1=1'], []
    if q:
        where.append('(title LIKE ? OR brand LIKE ? OR description LIKE ?)')
        args += [f'%{q}%'] * 3
    if leaf:
        where.append('leaf_category=?')
        args.append(leaf)
    wsql = ' AND '.join(where)
    total = query_one(f'SELECT COUNT(*) c FROM products WHERE {wsql}', tuple(args))['c']
    pages = max(1, (total + per - 1) // per)
    page = min(page, pages)
    rows = query(f'''SELECT p.*, (SELECT COUNT(*) FROM enrichments e WHERE e.product_id=p.id) enriched
                     FROM products p WHERE {wsql}
                     ORDER BY p.id LIMIT ? OFFSET ?''', tuple(args + [per, (page - 1) * per]))
    leaves = [r['leaf_category'] for r in query(
        "SELECT DISTINCT leaf_category FROM products WHERE leaf_category<>'' ORDER BY leaf_category")]
    total_all = query_one('SELECT COUNT(*) c FROM products')['c']
    return render_template('products.html', rows=rows, q=q, leaf=leaf, leaves=leaves,
                           page=page, pages=pages, total=total, total_all=total_all)


@app.route('/products/import', methods=['POST'])
@role_required('admin')
def products_import():
    limit = max(1, min(3000, request.form.get('limit', type=int) or 300))
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        '_shared', 'corpus', 'products.jsonl')
    if not os.path.exists(path):
        flash('语料文件不存在', 'danger')
        return redirect(url_for('products'))
    n = 0
    with open(path, 'r', encoding='utf-8') as f:
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
    global _INDEX
    _INDEX = None  # 重新建索引
    flash(f'语料导入完成: 处理 {n} 行', 'success')
    return redirect(url_for('products'))


@app.route('/product/<int:pid>')
@login_required
def product_detail(pid):
    p = query_one('SELECT * FROM products WHERE id=?', (pid,))
    if not p:
        abort(404)
    en = query('SELECT * FROM enrichments WHERE product_id=? ORDER BY id DESC LIMIT 5', (pid,))
    return render_template('product_detail.html', p=p, en=en)


# ---------------- 智能富化 ----------------
_RULE_TAGS = {
    '护肤': '保湿 修护 温和', '彩妆': '显色 持久 易上妆', '香氛': '留香 层次感',
    '洗护': '温和清洁 顺滑', '口腔': '清洁 清新', '工具': '便携 实用',
}


def _rule_enrich(p):
    leaf = (p['leaf_category'] or p['main_category'] or '').split('-')
    topic = leaf[0] if leaf else '美妆'
    tags = ' '.join([_RULE_TAGS.get(topic, '日常必备'), p['brand'] or p['main_category'] or '']).strip()
    return f"「{p['title']}」属于{topic}类商品，适合日常使用。", tags, '大众消费者', '日常通勤 / 居家 / 送礼'


@app.route('/enrich')
@login_required
def enrich():
    rows = query('''SELECT p.*, (SELECT COUNT(*) FROM enrichments e WHERE e.product_id=p.id) enriched
                    FROM products p ORDER BY id LIMIT 50''')
    stats = {
        'total': query_one('SELECT COUNT(*) c FROM products')['c'],
        'done': query_one('SELECT COUNT(DISTINCT product_id) c FROM enrichments')['c'],
        'llm': query_one("SELECT COUNT(*) c FROM enrichments WHERE mode='llm'")['c'],
        'rule': query_one("SELECT COUNT(*) c FROM enrichments WHERE mode='rule'")['c'],
        'cost': query_one('SELECT COALESCE(SUM(cost_fen),0) c FROM enrichments')['c'],
    }
    return render_template('enrich.html', rows=rows, stats=stats)


@app.route('/enrich/run', methods=['POST'])
@role_required('admin')
def enrich_run():
    n = max(1, min(20, request.form.get('n', type=int) or 5))
    s = get_settings()
    todo = query('''SELECT p.* FROM products p
                    WHERE NOT EXISTS (SELECT 1 FROM enrichments e WHERE e.product_id=p.id)
                    ORDER BY p.id LIMIT ?''', (n,))
    if not todo:
        flash('没有待富化的商品', 'info')
        return redirect(url_for('enrich'))
    done_llm = done_rule = 0
    for p in todo:
        if llm_available():
            prompt = '你是商品运营专家。基于商品信息生成:1)一句话卖点;2)3-5个标签;3)目标人群;4)适用场景。用 JSON 输出, 键: summary/tags/audience/scenes。'
            info = f"标题:{p['title']}\n品牌:{p['brand']}\n主题:{p['leaf_category'] or p['main_category']}\n描述:{(p['description'] or '')[:500]}"
            text, pt, ct, mode = llm_chat(
                [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': info}],
                model=s['model_a'], max_tokens=300)
            if mode == 'llm' and text:
                try:
                    d = json.loads(text[text.find('{'): text.rfind('}') + 1])
                    summary = d.get('summary', text[:120])
                    tags = d.get('tags', '')
                    if isinstance(tags, list):
                        tags = ' '.join(map(str, tags))
                    audience = d.get('audience', '大众消费者')
                    scenes = d.get('scenes', '日常使用')
                    mode = 'llm'
                except (ValueError, json.JSONDecodeError):
                    text = None
            if not text or mode == 'offline':
                summary, tags, audience, scenes = _rule_enrich(p)
                mode = 'rule'
            execute('''INSERT INTO enrichments(product_id,model,summary,tags,audience,scenes,mode,
                        prompt_tokens,completion_tokens,cost_fen) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (p['id'], s['model_a'], summary, tags, audience, scenes, mode, pt, ct, cost_fen(pt, ct, s)))
            if mode == 'llm':
                done_llm += 1
            else:
                done_rule += 1
        else:
            summary, tags, audience, scenes = _rule_enrich(p)
            execute('''INSERT INTO enrichments(product_id,model,summary,tags,audience,scenes,mode,
                        prompt_tokens,completion_tokens,cost_fen) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (p['id'], s['model_a'], summary, tags, audience, scenes, 'rule', 0, 0, 0))
            done_rule += 1
    flash(f'富化完成: LLM {done_llm} 条 / 规则兜底 {done_rule} 条', 'success')
    return redirect(url_for('enrich'))


@app.route('/enrich/<int:enid>/edit', methods=['POST'])
@role_required('admin')
def enrich_edit(enid):
    en = query_one('SELECT * FROM enrichments WHERE id=?', (enid,))
    if not en:
        abort(404)
    execute('''UPDATE enrichments SET summary=?, tags=?, audience=?, scenes=?, mode='rule' WHERE id=?''',
            ((request.form.get('summary') or '').strip()[:500],
             (request.form.get('tags') or '').strip()[:200],
             (request.form.get('audience') or '').strip()[:60],
             (request.form.get('scenes') or '').strip()[:120], enid))
    flash('已保存人工修正', 'success')
    return redirect(url_for('product_detail', pid=en['product_id']))


# ---------------- 评测中心 ----------------
_ASIN_RE = re.compile(r'\b[A-Z0-9]{8,12}\b')


def _answer_contains_refusal(text):
    if not text:
        return True
    marks = ['找不到', '无法推荐', '不相关', '抱歉', '超出', '没有候选', '无法回答', '不能编造',
             '不在候选', '拒绝', '未能', 'offline', '离线']
    return any(m in text for m in marks)


def run_eval_batch(batch_id):
    """后台线程: 逐用例 × 逐模型 串行, 结果即时落库。"""
    b = query_one('SELECT * FROM eval_batches WHERE id=?', (batch_id,))
    if not b:
        return
    s = get_settings()
    cases = query('SELECT * FROM eval_cases ORDER BY id')
    models = [b['model_a'], b['model_b']]
    try:
        for case in cases:
            expected = set(x for x in re.split(r'[\s,;，；]+', case['expected_asins'] or '') if x)
            hits = bm25_search(case['question'])
            hit_asin = {r['asin'] for r, _ in hits}
            hit = 1 if (expected & hit_asin) else 0
            retrieved = json.dumps([r['asin'] for r, _ in hits], ensure_ascii=False)
            for model in models:
                t0 = time.time()
                answer, pt, ct, mode = None, 0, 0, 'offline'
                top_score = hits[0][1] if hits else 0.0
                if not hits or top_score < s['min_score']:
                    answer = refuse_reply(case['question'])
                elif llm_available():
                    ctx = '\n'.join(
                        f"- {r['title']} | 品牌:{r['brand'] or '未知'} | 价格:{(r['price_fen'] or 0)/100:.2f}元"
                        f" | 主题:{r['leaf_category'] or r['main_category'] or '通用'} | ASIN:{r['asin']}"
                        f" | 描述:{(r['description'] or '')[:200]}" for r, _ in hits)
                    answer, pt, ct, mode = llm_chat([
                        {'role': 'system', 'content': (
                            '你是电商推荐助手。优先从候选商品中推荐，并标注每个推荐商品的 ASIN'
                            '（格式：商品名（ASIN:XXXXXXXXXX））。若候选与问题无关，直接说明无法推荐。'
                            '用简体中文，90 字以内。')},
                        {'role': 'user', 'content': f'用户问题：{case["question"]}\n\n候选商品：\n{ctx}'},
                    ], model=model, max_tokens=300)
                    if mode != 'llm':
                        answer = '【离线兜底】' + '、'.join(r['title'] for r, _ in hits[:3])
                else:
                    answer = '【离线兜底】' + '、'.join(r['title'] for r, _ in hits[:3])
                latency = int((time.time() - t0) * 1000)
                cited = set(_ASIN_RE.findall(answer or '')) & set(r['asin'] for r, _ in hits)
                citation_ok = 1 if (not cited and False) else (1 if _ASIN_RE.search(answer or '') and cited else 0)
                if case['kind'] == 'out_domain':
                    refused = 1 if _answer_contains_refusal(answer or '') else 0
                else:
                    refused = 0
                execute('''INSERT INTO eval_runs(batch_id,case_id,model,answer,retrieved,hit,citation_ok,
                            refused,latency_ms,prompt_tokens,completion_tokens,cost_fen,mode)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (batch_id, case['id'], model, answer or '', retrieved, hit, citation_ok,
                         refused, latency, pt, ct, cost_fen(pt, ct, s), mode))
        status = 'done'
        summary = eval_summary(batch_id)
    except Exception as e:
        status = 'failed'
        summary = f'评测中断: {type(e).__name__}'
    execute("UPDATE eval_batches SET status=?, summary=?, finished_at=? WHERE id=?",
            (status, json.dumps(summary, ensure_ascii=False)[:5000] if isinstance(summary, dict) else str(summary),
             datetime.now().strftime('%Y-%m-%d %H:%M:%S'), batch_id))


def eval_summary(batch_id):
    """按模型汇总: 命中率/引用准确率/拒答正确率/均延迟/token/成本。"""
    s = get_settings()
    out = {}
    for model in (s['model_a'], s['model_b']):
        rows = query('SELECT * FROM eval_runs WHERE batch_id=? AND model=?', (batch_id, model))
        if not rows:
            out[model] = {'n': 0}
            continue
        n = len(rows)
        in_case_ids = {r['case_id'] for r in query(
            "SELECT id FROM eval_cases WHERE kind='in_domain'")}
        in_d = [r for r in rows if r['case_id'] in in_case_ids]
        out_d = [r for r in rows if r['case_id'] not in in_case_ids]
        hit = sum(r['hit'] for r in in_d) / len(in_d) if in_d else None
        cite = sum(r['citation_ok'] for r in rows) / n
        refused = sum(r['refused'] for r in out_d) / len(out_d) if out_d else None
        out[model] = {
            'n': n,
            'hit': hit, 'cite': cite, 'refused': refused,
            'latency': sum(r['latency_ms'] for r in rows) / n,
            'tokens': sum(r['prompt_tokens'] + r['completion_tokens'] for r in rows),
            'cost': sum(r['cost_fen'] for r in rows),
            'mode_offline': sum(1 for r in rows if r['mode'] == 'offline') / n,
        }
    return out


@app.route('/eval')
@login_required
def eval_home():
    cases = query('SELECT * FROM eval_cases ORDER BY id')
    batches = query('SELECT * FROM eval_batches ORDER BY id DESC LIMIT 10')
    stats = {
        'cases': len(cases),
        'in_domain': sum(1 for c in cases if c['kind'] == 'in_domain'),
        'out_domain': sum(1 for c in cases if c['kind'] == 'out_domain'),
        'batches': query_one('SELECT COUNT(*) c FROM eval_batches')['c'],
    }
    return render_template('eval.html', cases=cases, batches=batches, stats=stats)


@app.route('/eval/run', methods=['POST'])
@login_required
def eval_run():
    s = get_settings()
    if not query_one('SELECT id FROM eval_cases'):
        flash('请先导入评测用例(运行 seed)', 'warning')
        return redirect(url_for('eval_home'))
    bid = execute('''INSERT INTO eval_batches(name,model_a,model_b,status,created_at)
                     VALUES(?,?,?,?,?)''',
                  (f'评测批次 {datetime.now().strftime("%m-%d %H:%M")}', s['model_a'], s['model_b'],
                   'running', datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    threading.Thread(target=run_eval_batch, args=(bid,), daemon=True).start()
    flash(f'评测已启动(20 用例 × 2 模型, 后台串行执行, 页面自动刷新)', 'success')
    return redirect(url_for('eval_batch', bid=bid))


@app.route('/eval/batch/<int:bid>/status')
@login_required
def eval_batch_status(bid):
    b = query_one('SELECT * FROM eval_batches WHERE id=?', (bid,))
    if not b:
        return jsonify(ok=False)
    total = query_one('SELECT COUNT(*) c FROM eval_cases')['c']
    done = query_one('SELECT COUNT(DISTINCT case_id) c FROM eval_runs WHERE batch_id=?', (bid,))['c']
    return jsonify(ok=True, status=b['status'], done=done, total=total, pct=int(done * 100 / total) if total else 0)


@app.route('/eval/batch/<int:bid>')
@login_required
def eval_batch(bid):
    b = query_one('SELECT * FROM eval_batches WHERE id=?', (bid,))
    if not b:
        abort(404)
    runs = query('SELECT * FROM eval_runs WHERE batch_id=? ORDER BY case_id, model', (bid,))
    cases = {c['id']: c for c in query('SELECT * FROM eval_cases ORDER BY id')}
    return render_template('eval_batch.html', b=b, runs=runs, cases=cases)


@app.route('/eval/report/<int:bid>')
@login_required
def eval_report(bid):
    b = query_one('SELECT * FROM eval_batches WHERE id=?', (bid,))
    if not b:
        abort(404)
    summary = eval_summary(bid)
    runs = query('SELECT * FROM eval_runs WHERE batch_id=? ORDER BY case_id, model', (bid,))
    cases = {c['id']: c for c in query('SELECT * FROM eval_cases ORDER BY id')}
    offline = any(r['mode'] == 'offline' for r in runs)
    return render_template('eval_report.html', b=b, summary=summary, runs=runs, cases=cases, offline=offline)


@app.route('/eval/report/<int:bid>.md')
@login_required
def eval_report_md(bid):
    b = query_one('SELECT * FROM eval_batches WHERE id=?', (bid,))
    if not b:
        abort(404)
    summary = eval_summary(bid)
    runs = query('SELECT * FROM eval_runs WHERE batch_id=? ORDER BY case_id, model', (bid,))
    cases = {c['id']: c for c in query('SELECT * FROM eval_cases ORDER BY id')}
    offline = any(r['mode'] == 'offline' for r in runs)
    lines = [f'# AI 推荐助手评测报告 · {b["name"]}', '',
             f'> 批次状态: {b["status"]}  |  完成时间: {b["finished_at"] or "-"}', '']
    if offline:
        lines += ['> ⚠️ **本次评测运行于离线兜底模式，指标不代表真实模型能力**', '']
    lines += ['## 指标总览', '', '| 模型 | 用例数 | Hit@K | 引用准确率 | 拒答正确率 | 平均延迟(ms) | 总token | 总成本(分) |',
              '|---|---|---|---|---|---|---|---|']
    for m, v in summary.items():
        if v.get('n', 0) == 0:
            continue
        lines.append(f"| {m} | {v['n']} | {('%.1f%%' % (v['hit']*100)) if v.get('hit') is not None else '-'} | "
                     f"{'%.1f%%' % (v['cite']*100)} | {('%.1f%%' % (v['refused']*100)) if v.get('refused') is not None else '-'} | "
                     f"{v['latency']:.0f} | {v['tokens']} | {v['cost']} |")
    lines += ['', '## 逐条用例', '']
    case_ids = sorted(cases.keys())
    for cid in case_ids:
        c = cases[cid]
        kind_label = '[域内]' if c['kind'] == 'in_domain' else '[域外]'
        lines.append(f'### 用例#{c["id"]} {kind_label} {c["question"]}')
        if c['expected_asins']:
            lines.append(f'- 期望 ASIN: {c["expected_asins"]}')
        lines.append('')
        for r in runs:
            if r['case_id'] != cid:
                continue
            lines.append(f'**{r["model"]}** [{r["mode"]}] hit={r["hit"]} cite={r["citation_ok"]} refused={r["refused"]} '
                         f'延迟{r["latency_ms"]}ms 成本{r["cost_fen"]}分')
            lines.append(f'> {r["answer"][:400]}')
            lines.append('')
    text = '\n'.join(lines)
    return Response(text, mimetype='text/markdown; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename=eval_report_{bid}.md'})


# ---------------- 成本看板 ----------------
@app.route('/cost')
@login_required
def cost():
    s = get_settings()
    stats = {
        'chat_cnt': query_one('SELECT COUNT(*) c FROM chat_logs')['c'],
        'run_cnt': query_one('SELECT COUNT(*) c FROM eval_runs')['c'],
        'enrich_cnt': query_one('SELECT COUNT(*) c FROM enrichments')['c'],
        'chat_tok': query_one('SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) c FROM chat_logs')['c'],
        'run_tok': query_one('SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) c FROM eval_runs')['c'],
        'enrich_tok': query_one('SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) c FROM enrichments')['c'],
        'chat_cost': query_one('SELECT COALESCE(SUM(cost_fen),0) c FROM chat_logs')['c'],
        'run_cost': query_one('SELECT COALESCE(SUM(cost_fen),0) c FROM eval_runs')['c'],
        'enrich_cost': query_one('SELECT COALESCE(SUM(cost_fen),0) c FROM enrichments')['c'],
        'offline_chat': query_one("SELECT COUNT(*) c FROM chat_logs WHERE mode='offline'")['c'],
    }
    by_model = query('''SELECT model, COUNT(*) cnt, SUM(prompt_tokens+completion_tokens) tok,
                        SUM(cost_fen) cost FROM chat_logs GROUP BY model ORDER BY cost DESC''')
    daily = query('''SELECT substr(created_at,1,10) d, COUNT(*) cnt, SUM(cost_fen) cost
                     FROM chat_logs GROUP BY d ORDER BY d DESC LIMIT 14''')
    daily.reverse()
    return render_template('cost.html', stats=stats, by_model=by_model, daily=daily, unit=s)


# ---------------- 设置 ----------------
@app.route('/settings', methods=['GET', 'POST'])
@role_required('admin')
def settings():
    if request.method == 'POST':
        s = get_settings()
        top_k = max(1, min(20, request.form.get('top_k', type=int) or 5))
        try:
            min_score = float(request.form.get('min_score') or 1.0)
        except ValueError:
            min_score = 1.0
        execute('''INSERT INTO settings(id,base_url,model_a,model_b,top_k,min_score,
                   price_in_fen_per_1k,price_out_fen_per_1k,corpus_limit)
                   VALUES(1,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET base_url=excluded.base_url, model_a=excluded.model_a,
                   model_b=excluded.model_b, top_k=excluded.top_k, min_score=excluded.min_score,
                   price_in_fen_per_1k=excluded.price_in_fen_per_1k,
                   price_out_fen_per_1k=excluded.price_out_fen_per_1k,
                   corpus_limit=excluded.corpus_limit''',
                (request.form.get('base_url') or s['base_url'], request.form.get('model_a') or s['model_a'],
                 request.form.get('model_b') or s['model_b'], top_k, min_score,
                 request.form.get('price_in', type=int) or 2, request.form.get('price_out', type=int) or 8,
                 max(50, min(3000, request.form.get('corpus_limit', type=int) or 300))))
        global _INDEX
        _INDEX = None
        flash('设置已保存(检索索引将重建)', 'success')
        return redirect(url_for('settings'))
    s = get_settings()
    env = _load_env()
    return render_template('settings.html', s=s, env_has_key=bool(env.get('OPENAI_API_KEY')),
                           env_model=env.get('OPENAI_MODEL', ''))


# ---------------- 登录/用户管理(脚手架搬运) ----------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = query_one('SELECT * FROM users WHERE username=?',
                      (request.form.get('username', '').strip(),))
        if u and check_password_hash(u['password_hash'], request.form.get('password', '')):
            session['uid'] = u['id']
            flash(f'欢迎回来, {u["display_name"]}', 'success')
            return redirect(request.args.get('next') or url_for('chat'))
        flash('用户名或密码错误', 'danger')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('已退出登录', 'info')
    return redirect(url_for('login'))


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
            return redirect(url_for('chat'))
    return render_template('profile.html')


@app.route('/users')
@role_required('admin')
def user_list():
    return render_template('users.html', users=query('SELECT * FROM users ORDER BY id'))


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