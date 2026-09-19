# -*- coding: utf-8 -*-
"""临时实验 6：model_a(deepseek-v4-pro) 在 max_tokens 600/1200 下的表现"""
exec(open('_scratch_llm3.py', encoding='utf-8').read().split('for m in')[0])
for mt in (600, 1200):
    print('--- deepseek-v4-pro max_tokens=%d' % mt, call('deepseek-v4-pro', PROMPT, max_tokens=mt))
