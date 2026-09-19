# -*- coding: utf-8 -*-
"""临时实验脚本：验证 BM25 检索 + 评测用例构造是否可行（不属于交付物）"""
import json, math, re, collections

CORPUS = r'G:\code\hermes_project\_shared\corpus\products.jsonl'
CATS = ['香氛', '彩妆-眼部', '护发-工具', '护肤-面霜乳液', '彩妆-面部', '护发-洗护',
        '彩妆-美甲', '护肤-清洁', '美妆套装', '彩妆-唇部', '护肤-精华', '护发-造型',
        '工具-收纳', '剃须脱毛', '身体护理', '口腔护理', '工具-美容仪器']

EN = re.compile(r'[a-z0-9]+')
CJK = re.compile(r'[\u4e00-\u9fff]+')

def tokens(text):
    text = (text or '').lower()
    out = EN.findall(text)
    for seg in CJK.findall(text):
        if len(seg) == 1:
            out.append(seg)
        else:
            for i in range(len(seg) - 1):
                out.append(seg[i:i + 2])
    return out

# ---- 分层抽样：17 类目，每类取 rating_count 最高的 N 条，共 300 ----
by_cat = collections.defaultdict(list)
with open(CORPUS, encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        if d.get('leaf_category') in CATS:
            by_cat[d['leaf_category']].append(d)

per = {}
base = 300 // len(CATS)
extra = 300 - base * len(CATS)
for i, c in enumerate(CATS):
    per[c] = base + (1 if i < extra else 0)

sample = []
for c in CATS:
    rows = sorted(by_cat[c], key=lambda r: (-(r.get('rating_count') or 0), r['asin']))[:per[c]]
    sample += rows
print('sample size', len(sample), 'cats', len(per))

# ---- 建索引（doc = title + categories(=leaf_category) + description） ----
docs = []
for r in sample:
    txt = ' '.join([r.get('title') or '', r.get('leaf_category') or '', r.get('description') or ''])
    docs.append(tokens(txt))
N = len(docs)
df = collections.Counter()
for t in docs:
    for w in set(t):
        df[w] += 1
idf = {w: math.log(1 + (N - c + 0.5) / (c + 0.5)) for w, c in df.items()}
avgdl = sum(len(t) for t in docs) / N
tf_list = [collections.Counter(t) for t in docs]
k1, b = 1.5, 0.75

def search(q, k=5):
    qt = tokens(q)
    scores = []
    for i, tf in enumerate(tf_list):
        dl = len(docs[i])
        s = 0.0
        for w in qt:
            f = tf.get(w)
            if not f:
                continue
            s += idf.get(w, 0.0) * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / avgdl))
        if s > 0:
            scores.append((i, s))
    scores.sort(key=lambda x: -x[1])
    return [(sample[i], s) for i, s in scores[:k]]

# ---- 每个类目的期望集：抽样内 rating_count Top-5 ----
expect = {}
for c in CATS:
    rows = [r for r in sample if r['leaf_category'] == c]
    rows.sort(key=lambda r: (-(r.get('rating_count') or 0), r['asin']))
    expect[c] = [r['asin'] for r in rows[:5]]

ZH = {
    '香氛': '香水', '彩妆-眼部': '眼影 睫毛膏', '护发-工具': '吹风机 卷发棒',
    '护肤-面霜乳液': '保湿面霜', '彩妆-面部': '粉底液', '护发-洗护': '洗发水 护发素',
    '彩妆-美甲': '美甲', '护肤-清洁': '洁面 洗面奶', '美妆套装': '美妆套装',
    '彩妆-唇部': '口红 唇膏', '护肤-精华': '精华液', '护发-造型': '发胶 造型',
    '工具-收纳': '化妆收纳包', '剃须脱毛': '剃须刀 脱毛', '身体护理': '身体乳',
    '口腔护理': '牙膏 牙刷', '工具-美容仪器': '美容仪',
}

variants = {
    'A 纯中文类目词': lambda c: ZH[c],
    'B 中文+类目名': lambda c: ZH[c] + ' ' + c,
    'C 中文+类目名+英文title词': None,
}

# 每个类目 title 里最高频英文内容词
stop = set('for and the with of a to in on by x oz ml new set pack pcs'.split())
for c in CATS:
    wc = collections.Counter()
    for r in sample:
        if r['leaf_category'] == c:
            for w in EN.findall((r.get('title') or '').lower()):
                if w not in stop and len(w) > 2:
                    wc[w] += 1
    print(c, wc.most_common(6))

def hit_at_k(queries, k=5):
    hits = 0
    detail = []
    for c in CATS:
        res = search(queries(c), k)
        got = [r['asin'] for r, s in res]
        ok = any(a in got for a in expect[c])
        hits += ok
        detail.append((c, ok, round(res[0][1], 2) if res else 0, len(got)))
    return hits, detail

print('\n=== A ===')
h, d = hit_at_k(lambda c: ZH[c])
print('Hit@5', h, '/17')
for x in d: print(x)
print('\n=== B ===')
h, d = hit_at_k(lambda c: ZH[c] + ' ' + c)
print('Hit@5', h, '/17')
for x in d: print(x)
