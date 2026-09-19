# -*- coding: utf-8 -*-
"""临时实验 5：model_b 候选可用性与真实延迟（贴近评测 prompt 规模）"""
import json, time, urllib.request, urllib.error
exec(open('_scratch_llm.py', encoding='utf-8').read().split('cfg = load_env()')[0])
cfg = load_env()
base = cfg['OPENAI_BASE_URL'].rstrip('/')
key = cfg['OPENAI_API_KEY']

PROMPT = ('你是电商导购助手。以下是检索到的商品（只允许引用这些商品）：\n'
          + '\n'.join(f'[{i+1}] ASIN: B0{i}AAAA | 标题: Moisturizing Face Cream for Dry Skin {i} | 价格: ¥{(i+1)*9.9:.2f} | 卖点: 保湿滋润'
                      for i in range(5))
          + '\n用户问题：我想找保湿面霜，有什么推荐？\n请用中文回答，必须引用 ASIN，控制在 200 字内。')

def call(model, prompt, max_tokens=500, timeout=45):
    body = json.dumps({'model': model, 'messages': [{'role': 'user', 'content': prompt}],
                       'max_tokens': max_tokens, 'temperature': 0.3}).encode('utf-8')
    req = urllib.request.Request(base + '/chat/completions', data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    req.add_header('Authorization', 'Bearer ' + key)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode('utf-8'))
        dt = int((time.time() - t0) * 1000)
        m = d['choices'][0]['message']
        u = d.get('usage') or {}
        return ('OK', dt, u.get('prompt_tokens'), u.get('completion_tokens'), (m.get('content') or '')[:100])
    except urllib.error.HTTPError as e:
        return ('HTTP%d' % e.code, int((time.time() - t0) * 1000), None, None, e.read()[:160].decode('utf-8', 'replace'))
    except Exception as e:
        return (type(e).__name__, int((time.time() - t0) * 1000), None, None, str(e)[:160])

for m in ['glm-5.2', 'glm-5.3-flash', 'deepseek-v4-flash']:
    print('---', m, call(m, PROMPT))
