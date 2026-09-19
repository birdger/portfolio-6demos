# SPEC: proj3-inventory-logistics 进销存+宅急送 (端口 8003)

## 环境(同脚手架)
- 目录 G:\code\hermes_project\proj3-inventory-logistics; venv G:/code/hermes_project/.venv/Scripts/python.exe
- flask+stdlib; 金额整数分; 时间 localtime; 本地静态资源

## 改 app.py
1. APP_NAME='进销存+宅急送物流' APP_SUBTITLE='WMS+TMS' PORT=8003
2. NAV: 仪表盘/商品管理/供应商/客户/仓库/采购管理/销售管理/库存台账/库存流水/盘点管理/运单管理/财务报表
3. 建表:
   - products(id,code,name,category,unit,price(成本价),sale_price,stock,available_stock,frozen_stock,safety_stock,status)
   - suppliers(id,name,contact,phone,status) ; customers(id,name,contact,phone,status)
   - warehouses(id,name,address)
   - purchase_orders(id,no,supplier_id,total_amount,status[pending/received/paid],created_at)
   - purchase_items(id,po_id,product_id,qty,price)
   - sale_orders(id,no,customer_id,total_amount,status[pending/shipped/finished],created_at)
   - sale_items(id,so_id,product_id,qty,price)
   - stock_ledger(id,product_id,warehouse_id,change_qty,before_qty,after_qty,type[in/out/freeze/unfreeze/adjust],ref_no,remark,created_at,user_id)  # 核心: 信息流留痕
   - stocktakes(id,no,warehouse_id,status[draft/done],created_at) ; stocktake_items(id,take_id,product_id,book_qty,actual_qty,diff_qty)
   - waybills(id,no,sale_order_id,carrier,status[created/picked/in_transit/delivering/signed/exception],created_at,signed_at)
   - waybill_tracks(id,waybill_id,status,remark,created_at)
   - payments(id,type[pay/receive],ref_type,ref_id,amount,created_at)
4. 路由:
   - CRUD: /products /suppliers /customers /warehouses (列表+新增+编辑+删除)
   - 采购: GET/POST /purchases, POST /purchases/<id>/receive(入库: 商品库存+=qty, 写流水), POST /purchases/<id>/pay
   - 销售: GET/POST /sales, POST /sales/<id>/ship(冻结→出库: 库存-, 写流水, 生成运单)
   - 库存: GET /stock (台账+缺货预警标红), GET /stock/ledger (流水, 可按商品过滤), GET/POST /stocktakes, POST /stocktakes/<id>/adjust(按 diff 调整库存+流水)
   - 运单: GET /waybills, POST /waybills/<id>/advance(按序推进状态+写轨迹), POST /waybills/<id>/exception(拒收→回滚出库: 库存+流水)
   - 财务: GET /finance (采购成本/销售收入/毛利, 按商品/客户/供应商)
   - 仪表盘: 库存总量/缺货数/在途运单/本月销售额 + echarts(近6月销售趋势, 库存Top10)
5. 关键实现:
   - 出库原子: 检查 available>=qty, UPDATE products SET stock=stock-?,frozen_stock=frozen_stock-? WHERE id=? AND stock>=? AND frozen_stock>=?
   - 每次库存变动写 stock_ledger(before/after)
   - 运单状态机: created→picked→in_transit→delivering→signed; exception 从任何非终态进入
   - 拒收: sale 单状态回 pending, 商品库存+ 写流水, 运单 status=exception

## 种子(seed_demo)
- 商品 ≥10(2个低于安全库存触发预警, 1个可用为0), 仓库2个, 供应商3, 客户4
- 采购单 2 笔(1笔已入库已付、1笔待收货), 销售单 ≥5 笔覆盖各状态(含已签收2笔、在途1笔、拒收1笔)
- 库存流水 ≥20 条(对应上述单据, before/after 数值自洽)
- 运单 3 张+轨迹若干; 收款/付款记录 ≥6
- 历史月份销售数据让近6月趋势图有起伏

## 自测(必须通过)
```
rm -f data.db && G:/code/hermes_project/.venv/Scripts/python.exe seed.py
(G:/code/hermes_project/.venv/Scripts/python.exe app.py >/tmp/p3.log 2>&1 &)
curl -s -c ck -d "username=admin&password=admin123" -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8003/login   # 302
curl -s -b ck http://127.0.0.1:8003/stock -o /dev/null -w "%{http_code}\n"    # 200
curl -s -b ck http://127.0.0.1:8003/stock/ledger | grep -c "流水\|ledger"      # ≥1
# 走一遍: 建销售单→出库→运单推进到签收, 每步 302/200; 拒收一笔验证库存回滚
```
测试后 kill 8003。交付报告: 模块+curl 输出+启动命令+已知问题。
