# SPEC: proj6-ai-agent AI 商品推荐助手 (端口 8006)

## 环境
- 目录 `G:\code\hermes_project\proj6-ai-agent`; venv `G:/code/hermes_project/.venv/Scripts/python.exe`
- 先读 `app.py`(认证/RBAC/用户管理已实现) / `db.py`(query/query_one/execute/transaction/execute_many) / `seed.py`(seed_demo)
- **只允许 flask + 标准库**（urllib.request / json / threading / re / math / collections；不得用 requests/numpy/openai）
- 金额一律整数**分**；时间用 `datetime('now','localtime')` 字符串
- 静态资源已本地化：`static/css/bootstrap.min.css`、`static/js/bootstrap.bundle.min.js`、`static/js/echarts.min.js`；模板用 `url_for('static', ...)`，**禁止任何 CDN**
- **语料文件（已由主控准备好，直接用）**：`G:\code\hermes_project\_shared\corpus\products.jsonl`（3000 条美妆商品，每行一个 JSON，字段见下）
  ```
  asin, title, brand, price_fen(整数分,可能为0), main_category, categories,
  leaf_category(中文主题,如"护肤-面霜乳液"/"香氛"/"彩妆-唇部"), description(≤2000字), image(URL), rating, rating_count
  ```
  - **逐行流式读取**（`for line in open(...)`），不得整体 `read()`
  - 图片是境外 URL，页面上可能加载不出来 → 卡片用 `<img onerror>` 兜底显示占位，别让破图破坏观感
- 凭据文件：`G:\code\hermes_project\_shared\api.env`，格式三行 `OPENAI_BASE_URL=` / `OPENAI_API_KEY=` / `OPENAI_MODEL=`
  - **密钥严禁写进代码、模板、日志或打印**；运行时用 `_load_env()` 解析该文件读取
  - 若文件不存在，走离线兜底模式，页面顶部显示黄色提示条

## 改 app.py
1. `APP_NAME='AI 商品推荐助手'` `APP_SUBTITLE='AI Agent · RAG'` `PORT=8006`
2. `NAV` = 对话助手(`/`)/ 商品库(`/products`)/ 智能富化(`/enrich`)/ 评测中心(`/eval`)/ 成本看板(`/cost`)/ 调用日志(`/logs`) + 保留 用户管理(`/users`)
3. `ensure_schema()` 建表：
   - `products(id, asin UNIQUE, title, brand, price_fen, main_category, categories, description, image, rating, rating_count, created_at)`
   - `enrichments(id, product_id, model, summary, tags, audience, scenes, mode[llm/rule], prompt_tokens, completion_tokens, cost_fen, created_at)`
   - `eval_cases(id, question, expected_asins, category, kind[in_domain/out_domain], created_at)`
   - `eval_batches(id, name, model_a, model_b, status[running/done/failed], summary, created_at, finished_at)`
   - `eval_runs(id, batch_id, case_id, model, answer, retrieved, hit, citation_ok, refused, latency_ms, prompt_tokens, completion_tokens, cost_fen, mode, created_at)`
   - `chat_logs(id, user_id, question, answer, retrieved, model, prompt_tokens, completion_tokens, cost_fen, latency_ms, mode, created_at)`
   - `settings(id, base_url, model_a, model_b, top_k, min_score, price_in_fen_per_1k, price_out_fen_per_1k, corpus_limit)`
4. 模块（写在 app.py 内，分区块注释）：
   - **`_load_env()`** 解析 `_shared/api.env`；`llm_available()` 判断
   - **`llm_chat(messages, model, max_tokens)`** 用 `urllib.request` POST `{base_url}/chat/completions`，Bearer 认证，`timeout=45`，返回 `(text, prompt_tokens, completion_tokens, mode)`；任何异常 → 返回 `(None, 0, 0, 'offline')` 并写 `llm_errors` 日志；**禁止把 key 打进日志**
   - **BM25 检索**（纯 Python，约 60 行）：对 `products.title + categories + description` 分词（英文按 `\W+` 切分 + 小写；中文按 2-gram 切分），建 `df`/`idf`/`doc_len`/`avgdl`，`k1=1.5, b=0.75`；`search(query, k)` 返回 `[(product_dict, score)]` 按分降序。索引首次使用时构建并缓存到模块级变量
   - **`refuse()`** 当 `top1_score < settings.min_score` → 拒答话术
   - **后台线程**：`threading.Thread(daemon=True)` 跑评测批次，逐用例串行调用（避免限流），每条结果即时落库，前端轮询 `/eval/batch/<id>/status`(JSON) 自动刷新
5. 路由：
   - `GET /` 对话助手页（历史对话 + 输入框 + 商品卡片渲染）
   - `POST /chat` 处理提问：检索 → 拼 prompt → LLM → 落 `chat_logs` → 返回 JSON `{answer, mode, items:[{asin,title,price_fen,score,image}], latency_ms, tokens, cost_fen}`（前端 fetch 渲染，不整页刷新）
   - `GET /products` 列表（关键词搜索 + 类目筛选 + 分页，每页 20）
   - `GET /product/<id>` 详情（原始信息 + 富化结果 + 单条富化按钮）
   - `POST /products/import` 导入语料（表单 `limit`，默认 300，上限 3000）→ 流式逐行读取，`INSERT OR IGNORE`，显示导入条数
   - `GET/POST /enrich` 富化页：列出未富化商品，`POST /enrich/run` 批量富化前 N 条（串行，带进度），`POST /enrich/<id>/edit` 人工修正
   - `GET /eval` 评测页（用例表 + 批次表 + 发起按钮）
   - `POST /eval/run` 创建批次并起线程；`GET /eval/batch/<id>/status` JSON；`GET /eval/batch/<id>` 结果页（表格 + 双模型对比柱状图 echarts）
   - `GET /eval/report/<id>` 评测报告（指标卡 + 逐条用例原始回答 + 结论段落）；`GET /eval/report/<id>.md` 下载 Markdown
   - `GET /cost` 成本看板（卡片 + 按模型/按功能拆分表 + echarts 柱状图）
   - `GET /logs` 调用日志（分页）
   - `GET/POST /settings` 设置页（base_url / model_a / model_b / top_k / min_score / 单价 / corpus_limit）
6. 成本计算：`cost_fen = round((pt/1000)*price_in_fen_per_1k + (ct/1000)*price_out_fen_per_1k)`（四舍五入取整，允许 0）
7. 指标口径（写进页面说明，不许含糊）：
   - `Hit@K` = 该用例 `expected_asins` 中**至少一个**出现在检索 Top-K 里 → 记 1，否则 0
   - `引用准确率` = 回答中出现的 ASIN 全部 ∈ 检索结果 → 记 1；回答中无任何 ASIN 记 0（并要求 prompt 里强制模型引用）
   - `拒答正确率` = `out_domain` 用例中 `refused=1` 的比例
   - 平均延迟/token/成本 = 该模型全部用例的算术平均

## 离线兜底（必须有，且必须诚实标注）
- LLM 不可用时：问答返回"【离线兜底】以下为检索结果，未经大模型生成：…"+ 商品列表；`mode='offline'`
- 评测在离线模式下**照常跑**，但报告顶部必须红字写明"本次评测运行于离线兜底模式，指标不代表真实模型能力"
- 富化降级为规则模板（标题截断 + 类目当标签），`mode='rule'`

## 种子(seed_demo)
- 从 `_shared/corpus/products.jsonl` 导入 **300 条**商品到 `products`（逐行流式，`INSERT OR IGNORE`）
- **20 条评测用例**，期望值必须是**独立于检索索引的客观标签**（不许用手编 ASIN，也不许用 BM25 自己的结果当答案，否则是循环论证）：
  - 17 条 in_domain：挑 17 个商品数 ≥ 15 的 `leaf_category`（如"护肤-面霜乳液""香氛""彩妆-唇部"…），
    问题按类目 + 真实商品标题里的品类词构造（例："我想找适合干皮的保湿面霜，有什么推荐？"），
    `expected_asins` = **该 `leaf_category` 下 `rating_count` 最高的 Top-5 的 ASIN**（客观标签，与检索无关）
  - 3 条 out_domain：美妆语料外问题（如"推荐一款高性能游戏笔记本电脑""帮我找一只猫粮"），`expected_asins=''`
- `settings` 一行：model_a=deepseek-v4-flash、model_b=glm-5.2-flash（model_a 取 api.env 的 OPENAI_MODEL）、top_k=5、min_score=1.0、单价按中转站估算（输入 2 分/1k、输出 8 分/1k）、corpus_limit=300
- seed 要打印落地行数（`products=300, eval_cases=20`）

## 自测（必须通过再交付，命令逐条贴进报告）
```bash
cd /g/code/hermes_project/proj6-ai-agent
rm -f data.db && G:/code/hermes_project/.venv/Scripts/python.exe seed.py
(G:/code/hermes_project/.venv/Scripts/python.exe app.py > _p6.log 2>&1 &)
sleep 3
curl -s -o /dev/null -w "login:%{http_code}\n" http://127.0.0.1:8006/login
curl -s -c _ck -d "username=admin&password=admin123" -o /dev/null -w "post:%{http_code}\n" http://127.0.0.1:8006/login
curl -s -b _ck http://127.0.0.1:8006/products | grep -c "asin"          # ≥1
curl -s -b _ck -H "Content-Type: application/json" -d '{"q":"保湿面霜"}' http://127.0.0.1:8006/chat   # 返回 JSON
curl -s -b _ck http://127.0.0.1:8006/eval | grep -c "评测"               # ≥1
curl -s -b _ck -X POST http://127.0.0.1:8006/eval/run -o /dev/null -w "run:%{http_code}\n"   # 302/200
curl -s -b _ck http://127.0.0.1:8006/cost | grep -c "成本"               # ≥1
```
- **评测必须真的跑起来**：等 60–180s 后查 `eval_runs` 行数（`sqlite3 data.db "select count(*) from eval_runs"` 或用 python 一行脚本），**行数 > 0 才算通过**，把真实行数与报告页关键指标贴进交付报告
- 结束前 kill 自己的 8006 进程：`PID=$(netstat -ano | grep ":8006" | grep LISTEN | awk '{print $5}' | head -1); taskkill /F /PID $PID`
  （注意本机是 git-bash，**taskkill 用单斜杠 `/F /PID`**，`//F` 会报"无效参数"）
- 交付报告：完成模块清单 + 每条 curl 实际输出 + 评测真实行数/指标 + 启动命令 + 已知问题
