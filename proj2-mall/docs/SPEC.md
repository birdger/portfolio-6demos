# SPEC: proj2-mall 网上商城 (端口 8002)

## 环境(同项目1脚手架)
- 目录 G:\code\hermes_project\proj2-mall; venv: G:/code/hermes_project/.venv/Scripts/python.exe
- 只允许 flask+stdlib; 金额整数分; 时间 localtime 字符串; 静态资源本地(static/ 已有 bootstrap+echarts)
- 先读 app.py / db.py / seed.py 再扩展

## 改 app.py
1. APP_NAME='网上商城' APP_SUBTITLE='Mall' PORT=8002
2. NAV: 首页/商品管理/分类管理/订单管理/营销中心/售后管理/数据总览/我的订单(用户)
3. 建表:
   - categories(id,name,sort)
   - products(id,category_id,title,subtitle,main_image(用占位色块或 picsum 本地无网→用纯色div),price,original_price,stock,frozen_stock,sales,status[on/off],created_at)
   - skus(id,product_id,spec_name,price,stock)  # 可简化为 products 单规格+color/size字段, 但保留 sku 表体现设计
   - carts(id,user_id,product_id,sku_id,qty,checked,created_at)
   - coupons(id,title,type[full_reduce/discount],threshold,value,stock,total,status)
   - user_coupons(id,user_id,coupon_id,status[unused/used],created_at)
   - orders(id,order_no,user_id,total_amount,discount_amount,pay_amount,coupon_id,status,address,pay_no,created_at,paid_at,shipped_at,finished_at,canceled_at)
   - order_items(id,order_id,product_id,sku_id,title,spec,price,qty)
   - refunds(id,order_id,user_id,reason,status[pending/approved/rejected/done],amount,created_at)
   - comments(id,order_id,user_id,product_id,rating,content,created_at)
   - pay_logs(id,pay_no,order_id,amount,status,created_at)  # 幂等依据: pay_no 唯一
4. 路由:
   - GET / 商品列表(搜索/分类/上下架过滤)
   - GET /product/<id> 详情
   - POST /cart/add, GET /cart, POST /cart/update, POST /cart/checkout(→创建订单+扣库存+生成 pay_no)
   - GET /order/<id> 详情(状态时间线), POST /order/<id>/pay(模拟支付: 幂等), POST /order/<id>/ship, POST /order/<id>/finish, POST /order/<id>/cancel
   - POST /order/<id>/refund, GET/POST /refunds 审核
   - GET /coupons, POST /coupon/claim(领券), 下单时选券
   - POST /comment
   - GET /stats 数据总览(卡片+漏斗+热销榜+echarts)
   - GET /demo/race 并发抢购演示页(JS fetch 并发 N 请求同一商品, 展示成功数=库存数, 证明不超卖)
   - 用户路由 GET /my/orders
5. 关键实现:
   - 扣库存: `UPDATE products SET stock=stock-?, frozen_stock=frozen_stock+? WHERE id=? AND stock>=?` 影响行数=0→失败
   - 超时关单: 后台线程每秒扫描 status='pending' 且 created_at 超 30 分钟 → 取消+回滚库存(冻结→库存)
   - 支付幂等: 按 pay_no 查 pay_logs, 已成功直接返回成功
   - 优惠券: 下单时若用券 → user_coupons 置 used; 满减与折扣互斥(一个订单一张券)

## 种子(seed_demo)
- 分类5个; 商品 ≥12(各分类, 1个下架, 1个库存仅3本用于抢购演示); 会员用户(在seed_users已有demo/ops/admin基础上新增 buyer/buyer123 普通用户)
- 优惠券 3 张(满100减20/满200减50/9折), 会员各领1张
- 历史订单 ≥6 条覆盖所有状态(已付款1条待发货、已完成2条含评价、退款中1条、超时取消1条), 用不同时间
- 评论 5 条; 支付日志配套

## 自测(必须通过)
```
rm -f data.db && G:/code/hermes_project/.venv/Scripts/python.exe seed.py
(G:/code/hermes_project/.venv/Scripts/python.exe app.py >/tmp/p2.log 2>&1 &)
curl -s http://127.0.0.1:8002/ -o /dev/null -w "%{http_code}\n"            # 200(游客可看)
# 用 buyer 登录走完整链路: 加购→下单→支付→(admin发货)→确认→评价, 每步记录302/200
# 抢购演示页: curl -s http://127.0.0.1:8002/demo/race -o /dev/null -w "%{http_code}\n" # 200
```
测试后 kill 8002 进程。交付报告: 模块清单+curl 输出+启动命令+已知问题。
