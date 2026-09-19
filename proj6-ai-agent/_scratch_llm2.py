# -*- coding: utf-8 -*-
"""临时实验 4：响应结构 + 可用模型列表"""
import json, urllib.request, urllib.error
exec(open('_scratch_llm.py', encoding='utf-8').read().split('cfg = load_env()')[0])
cfg = load_env()
base = cfg['OPENAI_BASE_URL'].rstrip('/')
key = cfg['OPENAI_API_KEY']

req = urllib.request.Request(base + '/models')
req.add_header('Authorization', 'Bearer ' + key)
try:
    with urllib.request.urlopen(req, timeout=45) as r:
        d = json.loads(r.read().decode('utf-8'))
    ids = sorted(m.get('id', '') for m in d.get('data', []))
    print('models count', len(ids))
    print([i for i in ids if 'deepseek' in i or 'glm' in i or 'qwen' in i or 'flash' in i][:60])
except Exception as e:
    print('models err', e)

body = json.dumps({'model': cfg['OPENAI_MODEL'],
                   'messages': [{'role': 'user', 'content': '请只输出 JSON：{"summary":"卖点","tags":["a"],"audience":"人群","scenes":["场景"]}'}],
                   'max_tokens': 400, 'temperature': 0.2}).encode()
req = urllib.request.Request(base + '/chat/completions', data=body, method='POST')
req.add_header('Content-Type', 'application/json')
req.add_header('Authorization', 'Bearer ' + key)
with urllib.request.urlopen(req, timeout=60) as r:
    d = json.loads(r.read().decode('utf-8'))
print('top keys', list(d.keys()))
ch = d['choices'][0]
print('choice keys', list(ch.keys()))
print('msg keys', list(ch['message'].keys()))
for k, v in ch['message'].items():
    print('--', k, repr(v)[:300])
print('usage', d.get('usage'))
