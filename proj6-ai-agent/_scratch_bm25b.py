# -*- coding: utf-8 -*-
"""临时实验 2：自然问句 + 类目标题品类词 对 Hit@5 的影响"""
import json, math, re, collections
exec(open('_scratch_bm25.py', encoding='utf-8').read().split("# ---- 每个类目的期望集")[0])

expect = {}
for c in CATS:
    rows = [r for r in sample if r['leaf_category'] == c]
    rows.sort(key=lambda r: (-(r.get('rating_count') or 0), r['asin']))
    expect[c] = [r['asin'] for r in rows[:5]]

ZH = {
    '香氛': '香水', '彩妆-眼部': '眼影睫毛膏', '护发-工具': '吹风机卷发棒',
    '护肤-面霜乳液': '保湿面霜', '彩妆-面部': '粉底液', '护发-洗护': '洗发水护发素',
    '彩妆-美甲': '美甲指甲油', '护肤-清洁': '洁面洗面奶', '美妆套装': '美妆套装',
    '彩妆-唇部': '口红唇膏', '护肤-精华': '精华液', '护发-造型': '发胶造型',
    '工具-收纳': '化妆收纳包', '剃须脱毛': '剃须刀脱毛器', '身体护理': '身体乳',
    '口腔护理': '牙膏牙刷', '工具-美容仪器': '美容仪',
}

stop = set('for and the with of a to in on by x oz ml new set pack pcs'.split())

def top_en(c):
    wc = collections.Counter()
    for r in sample:
        if r['leaf_category'] == c:
            for w in EN.findall((r.get('title') or '').lower()):
                if w not in stop and len(w) > 2 and not w.isdigit():
                    wc[w] += 1
    return wc.most_common(1)[0][0] if wc else ''

def run(name, qf, k=5):
    hits, det = 0, []
    for c in CATS:
        res = search(qf(c), k)
        got = [r['asin'] for r, s in res]
        ok = any(a in got for a in expect[c])
        hits += ok
        det.append((c, ok, round(res[0][1], 2) if res else 0))
    print('===', name, 'Hit@5 =', hits, '/17')
    for x in det:
        print('   ', x)
    return hits

run('D 自然问句(中文品类词+类目名)', lambda c: f'我想找{ZH[c]}，属于{c}类的商品，有什么推荐？')
run('E 自然问句+类目最高频英文品类词', lambda c: f'我想找{ZH[c]}（{c}类，{top_en(c)}），有什么推荐？')
print()
for c in CATS:
    print(c, '->', top_en(c))
