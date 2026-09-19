# -*- coding: utf-8 -*-
"""临时实验 3：中转站连通性与模型可用性（不打印任何密钥）"""
import json, time, urllib.request, urllib.error

ENV = r'G:\code\hermes_project\_shared\api.env'

def load_env():
    cfg = {}
    with open(ENV, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg

cfg = load_env()
base = cfg['OPENAI_BASE_URL'].rstrip('/')
key = cfg['OPENAI_API_KEY']
print('base_url =', base)
print('model    =', cfg.get('OPENAI_MODEL'))
print('key len  =', len(key))

def call(model, prompt, max_tokens=64, timeout=45):
    body = json.dumps({'model': model, 'messages': [{'role': 'user', 'content': prompt}],
                       'max_tokens': max_tokens, 'temperature': 0.2}).encode('utf-8')
    req = urllib.request.Request(base + '/chat/completions', data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    req.add_header('Authorization', 'Bearer ' + key)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode('utf-8'))
        dt = int((time.time() - t0) * 1000)
        txt = data['choices'][0]['message']['content']
        u = data.get('usage') or {}
        return ('OK', dt, u.get('prompt_tokens'), u.get('completion_tokens'), txt[:120])
    except urllib.error.HTTPError as e:
        return ('HTTP%d' % e.code, int((time.time() - t0) * 1000), None, None, e.read()[:200].decode('utf-8', 'replace'))
    except Exception as e:
        return (type(e).__name__, int((time.time() - t0) * 1000), None, None, str(e)[:200])

for m in [cfg.get('OPENAI_MODEL'), 'glm-5.2-flash', 'deepseek-v4-flash']:
    print('---', m, call(m, '只回复两个字：可用'))

# 结构化 JSON 输出能力（富化用）
print(call(cfg.get('OPENAI_MODEL'),
           '请只输出 JSON：{"summary":"一句话卖点","tags":["a"],"audience":"人群","scenes":["场景"]}',
           max_tokens=200))
