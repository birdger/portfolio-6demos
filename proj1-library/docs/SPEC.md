# SPEC: proj1-library 图书借阅管理系统 (端口 8001)

## 环境
- 项目目录: G:\code\hermes_project\proj1-library
- 运行: `cd proj1-library && G:/code/hermes_project/.venv/Scripts/python.exe app.py`
- 种子: 删除 data.db 后 `G:/code/hermes_project/.venv/Scripts/python.exe seed.py`
- 脚手架: 先读 app.py(认证/RBAC/用户管理已实现)、db.py(query/query_one/execute/transaction)、seed.py(seed_demo() 写业务种子)
- 静态资源已本地化: static/css/bootstrap.min.css, static/js/echarts.min.js —— 模板里直接用 url_for('static', ...), 禁止 CDN
- 只允许 flask + 标准库; 金额整数分; 时间 datetime('now','localtime') 字符串

## 改 app.py
1. APP_NAME='图书借阅管理系统' APP_SUBTITLE='Library' PORT=8001
2. NAV = 仪表盘/图书管理/读者管理/借阅管理/预约管理/罚款管理/操作日志/统计报表
3. ensure_schema() 建表:
   - books(id,title,isbn,author,category,publisher,price,stock,total,status,created_at)
   - readers(id,card_no,name,phone,status,borrow_limit,created_at)
   - borrows(id,book_id,reader_id,borrow_date,due_date,return_date,status[borrowed/returned/overdue/lost],fine,fine_paid,created_at)
   - reservations(id,book_id,reader_id,created_at,status)
   - logs(id,user_id,action,detail,created_at)
4. 路由(全部中文界面):
   - GET/POST /books (列表+搜索+新增), /books/<id>/edit, /books/<id>/delete, /books/import (CSV), /books/export
   - GET/POST /readers, /readers/<id>/edit, /readers/<id>/delete
   - POST /borrows (借书), /borrows/<id>/return (还书+算逾期罚款), /borrows/<id>/renew (续借), /borrows/<id>/lost
   - GET /my (读者视角: 我的借阅+续借+预约), POST /reserve
   - GET /fines (逾期/罚款清单, 标记已缴)
   - GET /logs
   - GET /stats (仪表盘: 卡片 + ECharts 趋势/占比)
5. 借阅上限 = readers.borrow_limit(默认5), 停借状态禁借; 还书算逾期天数×0.5元

## 种子数据(seed_demo)
- 图书 ≥12 本(文学/计算机/经管/历史 分类, 含 1 本库存0、1本被借出)
- 读者 ≥5 人(含 1 个停借)
- 借阅记录 ≥10 条, 时间跨近 3 个月(手动造不同状态: 已还/借出中/逾期未还/挂失), 逾期 1 条
- 操作日志 5 条

## 模板
- templates/books.html, readers.html, borrows.html, fines.html, logs.html, my.html, stats.html 均 extends base.html
- stats.html 用本地 echarts.min.js 画 折线(月度借阅) + 饼图(分类占比)

## 自测(必须通过再交付)
```
rm -f data.db && G:/code/hermes_project/.venv/Scripts/python.exe seed.py
(G:/code/hermes_project/.venv/Scripts/python.exe app.py >/tmp/p1.log 2>&1 &)
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8001/login          # 200
curl -s -c ck -d "username=admin&password=admin123" -o /dev/null http://127.0.0.1:8001/login   # 302
curl -s -b ck http://127.0.0.1:8001/books | grep -c "计算机"                   # ≥1
curl -s -b ck -d "title=测试书&isbn=T001&author=甲&category=测试&publisher=社&price=1000&stock=2&total=2" http://127.0.0.1:8001/books   # 新增→302
curl -s -b ck -d "book_id=1&reader_id=1" http://127.0.0.1:8001/borrows        # 借书→302
curl -s -b ck http://127.0.0.1:8001/stats | grep -c "echarts"                  # ≥1
```
测试完 kill 自己的服务进程(netstat 找 8001 PID, taskkill //F //PID)。
交付报告: 完成模块列表 + 每个 curl 的实际输出 + 启动命令 + 已知问题。
