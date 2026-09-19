# SPEC: proj5-saas 多租户SaaS (端口 8005)

## 环境(同脚手架)
- 目录 G:\code\hermes_project\proj5-saas; venv G:/code/hermes_project/.venv/Scripts/python.exe
- flask+stdlib; 本地静态资源; 金额分; localtime

## 改 app.py
1. APP_NAME='图书馆云 SaaS 平台' APP_SUBTITLE='Multi-Tenant SaaS' PORT=8005
2. 角色: platform_admin(超级管理员) / tenant_admin / tenant_user
   - 脚手架用户表已有 role 字段, 扩展 role 枚举, ROLE_LABELS 加两项
3. NAV(按角色渲染, 在模板里用 current_user.role 判断):
   - 平台: 平台概览/租户管理/套餐管理/审计日志/隔离方案
   - 租户: 租户工作台/图书管理/借阅管理/API管理/配额用量/本租户用户
4. 建表:
   - tenants(id,name,contact,phone,plan_id,status[active/disabled],expire_date,created_at)
   - plans(id,name,user_quota,book_quota,api_monthly_quota,api_rate_per_min,price_month(分),sort)
   - users 表加列 tenant_id(ALTER 或重建: 直接改脚手架建表语句, 加 tenant_id INTEGER DEFAULT NULL)
   - books(id,tenant_id,title,author,category,stock,status,created_at)  # 业务数据带 tenant_id
   - borrows(id,tenant_id,book_id,reader_name,borrow_date,return_date,status)
   - api_keys(id,tenant_id,api_key,status,created_at)  # 一个租户一个 key
   - api_logs(id,tenant_id,api_key,path,status_code,created_at)
   - audit_logs(id,user_id,username,action,detail,created_at)
   - rate_buckets(id,tenant_id,bucket_key,count,window_start)  # 简单限流计数
5. 路由:
   - 平台: GET /platform(概览: 租户数/用量/图表), GET/POST /tenants, GET/POST /plans, GET /audit
   - 租户登录: /login 页加"选择租户"下拉(tenant_id 进 session);登录后 session['tenant_id']
   - 租户: GET / (工作台: 用量进度条/最近借阅), GET/POST /books(过滤 tenant_id), GET /borrows, POST /borrows (借/还)
   - 用户: GET /tenant-users (租户管理员管理本租户用户, 超配额拦截)
   - API: GET /api/v1/books (X-API-Key 认证→查租户→限流→返回 JSON), /api/v1/borrows; 429 JSON 返回
   - GET /isolation (隔离方案文档页)
   - GET /api-playground (API 试玩页: 填 key 调接口看 JSON)
6. 关键实现:
   - 所有业务查询 SQL 必须 WHERE tenant_id=session['tenant_id'](写一个 helper: q_tenant(sql,args) 自动注入)
   - 创建用户/图书前检查配额(COUNT(*)<quota), 超限 flash 拦截
   - API 限流: 滑动窗口计数(内存 dict 或 rate_buckets 表), 超过 rate_per_min → 429
   - 停用租户: 登录检查 tenant.status;API 检查
   - 平台管理员 session['role']=='platform_admin' 时不做租户过滤(只读视图)
7. 模板: platform.html, tenants.html, plans.html, audit.html, tenant_dash.html, books.html, borrows.html, tenant_users.html, api_mgmt.html, isolation.html, api_playground.html(改造 login.html 加租户选择)

## 种子(seed_demo)
- 套餐 3 个: 免费(10/100/1000/60次每分/0元), 专业(50/1000/10000/300/299元月), 企业(1000/100000/1000000/3000/1999元月)
- 租户 3 个: 朝阳区图书馆(专业版,active), 知行学院图书馆(免费版,active), 停用示例(disabled)
- 用户: platform_admin/padmin123; 朝阳: tenanta/tenanta123(admin), 成员 auser/auser123; 知行: tenantb/tenantb123(admin)
- 图书: 朝阳 20 本(含书名带"朝阳"标记), 知行 8 本(带"知行"标记)→ 隔离演示一目了然
- 借阅记录若干; API key 每租户 1 个(随机串); api_logs 10 条; audit_logs 10 条

## 自测(必须通过)
```
rm -f data.db && G:/code/hermes_project/.venv/Scripts/python.exe seed.py
(G:/code/hermes_project/.venv/Scripts/python.exe app.py >/tmp/p5.log 2>&1 &)
# 平台登录
curl -s -c ck -d "username=padmin&password=padmin123&tenant_id=0" -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8005/login  # 302
# 租户A登录
curl -s -c cka -d "username=tenanta&password=tenanta123&tenant_id=1" -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8005/login
curl -s -b cka http://127.0.0.1:8005/books | grep -c "朝阳"     # ≥1
curl -s -b cka http://127.0.0.1:8005/books | grep -c "知行"     # =0 (隔离!)
# API
KEY=$(sqlite3 data.db "select api_key from api_keys where tenant_id=1" 2>/dev/null || G:/code/hermes_project/.venv/Scripts/python.exe -c "import sqlite3;print(sqlite3.connect('data.db').execute('select api_key from api_keys where tenant_id=1').fetchone()[0])")
curl -s -H "X-API-Key: $KEY" http://127.0.0.1:8005/api/v1/books | head -c 200
```
测试后 kill 8005。交付报告: 模块+curl 输出+启动命令+已知问题。
