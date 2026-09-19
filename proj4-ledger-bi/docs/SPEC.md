# SPEC: proj4-ledger-bi 记账+数据看板 (端口 8004)

## 环境(同脚手架)
- 目录 G:\code\hermes_project\proj4-ledger-bi; venv G:/code/hermes_project/.venv/Scripts/python.exe
- flask+stdlib; 金额整数分; 本地 echarts

## 改 app.py
1. APP_NAME='记账与数据看板' APP_SUBTITLE='Ledger+BI' PORT=8004
2. NAV: 记一笔/账单明细/分类管理/预算设置/数据看板/分析报告/指标口径/SQL工作台/用户活跃
3. 建表:
   - categories(id,parent_id,name,type[income/expense],sort)
   - accounts(id,name,type[现金/银行卡/支付宝/微信],balance)
   - transactions(id,user_id,account_id,category_id,type,amount,note,trans_date,created_at)
   - budgets(id,user_id,month(YYYY-MM),amount)
   - user_events(id,user_id,event_type[login/record/streak],event_date)  # 模拟行为表(种子造 30 天×N 用户)
   - sql_queries(id,title,description,sql_text,created_at)  # 内置示例
4. 路由:
   - GET/POST /record (记一笔), GET /ledger (明细+筛选: 类型/分类/日期范围/关键词), POST /ledger/<id>/delete
   - GET/POST /categories, GET/POST /budgets
   - GET /dashboard (看板: echarts 折线/饼图/柱状/进度条; 全部数据由 SQL 实时聚合)
   - GET /report (分析报告: 结余/最大支出/环比/预算执行)
   - GET /metrics (指标口径文档页)
   - GET /sql-lab, POST /sql-lab (执行内置 SQL(参数化替换日期), 返回表格结果; 只允许 SELECT)
   - GET /activity (留存 D1/D7 + 漏斗)
5. 关键实现:
   - 金额整数分, 展示 /100 保留2位
   - 看板 SQL 聚合: 月度收支(SUM+GROUP BY strftime('%Y-%m')), 分类占比, 环比用上月
   - 留存: 以 user_events 计算 D1/D7: 取某日活跃用户集合, 统计次日/7日后仍有事件的比例
   - 漏斗: 全用户中 login→record→连续3天 各级人数递减
   - SQL 工作台: 内置 6 条查询模板(月度支出Top分类/高频账户/日均记账笔数/预算执行/留存/漏斗), 页面输入参数(月份)执行
6. 模板: record.html, ledger.html, categories.html, budgets.html, dashboard.html, report.html, metrics.html, sql_lab.html, activity.html

## 种子(seed_demo)
- 分类: 收入(工资/兼职/理财/其他), 支出(餐饮/交通/购物/居住/娱乐/学习/医疗/其他)
- 账户 4 个; 预算 2 个月
- 交易记录: demo 用户近 6 个月 ≥80 条(有起伏, 本月>上月, 某分类占比突出), admin 少量
- user_events: 30 天内 15 个模拟用户 × 随机 15-25 天事件(可构造 1-2 个高活用户保证留存曲线合理)
- sql_queries: 6 条示例

## 自测(必须通过)
```
rm -f data.db && G:/code/hermes_project/.venv/Scripts/python.exe seed.py
(G:/code/hermes_project/.venv/Scripts/python.exe app.py >/tmp/p4.log 2>&1 &)
curl -s -c ck -d "username=demo&password=demo123" -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8004/login  # 302
curl -s -b ck http://127.0.0.1:8004/dashboard | grep -c "echarts"      # ≥1
curl -s -b ck http://127.0.0.1:8004/metrics | grep -c "口径"           # ≥1
curl -s -b ck -X POST -d "qid=1" http://127.0.0.1:8004/sql-lab | grep -c "SELECT\|table"  # ≥1
curl -s -b ck http://127.0.0.1:8004/activity | grep -c "留存\|D1"      # ≥1
```
测试后 kill 8004。交付报告: 模块+curl 输出+启动命令+已知问题。
