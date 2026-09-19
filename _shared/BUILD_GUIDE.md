# 施工须知（6 个 Flask 项目共用，务必先读完）

## 0. 你是谁、在干什么
你在为一个 **产品经理作品集** 造 6 个「能点开、有真实效果」的可运行系统 Demo。
主控已把统一脚手架铺好，你只负责**你被分配的那一个项目**。你的产出会被主控独立验收，
**自报"完成了"不算数——必须有真实命令输出为证**。

## 1. 目录与运行环境（Windows + git-bash）
- 项目根：`G:\code\hermes_project\<你的项目目录>`
- Python：`G:/code/hermes_project/.venv/Scripts/python.exe`（Python 3.12，已装 flask）
- 工作目录用 git-bash 风格：`/g/code/hermes_project/<你的项目目录>`
- 端口见你的 SPEC

## 2. 已有脚手架（**先读懂再改**）
```
app.py        # 已实现：Flask 应用工厂、登录/登出、RBAC(admin/operator/user)、
              #        用户管理 CRUD、仪表盘、错误页；含扩展点 APP_NAME/APP_SUBTITLE/PORT/NAV
              #        和 ensure_schema() 里的建表区
db.py         # query(sql,args)->list[dict] / query_one / execute / execute_many / transaction()
              #        每请求一连接、sqlite3.Row、PRAGMA foreign_keys=ON
seed.py       # seed_demo()：幂等种子数据，首部 import app 以触发 ensure_schema() 建表
templates/    # base.html(布局+导航+flash) login.html index.html users.html profile.html error.html
static/css/   # bootstrap.min.css, app.css
static/js/    # bootstrap.bundle.min.js, echarts.min.js
docs/         # PRD.md + SPEC.md ← **你的施工图，先完整读一遍**
```

## 3. 硬约束（违反即返工）
1. **只允许 `flask` + Python 标准库**。禁止 requests / numpy / pandas / openai / redis / sqlalchemy / celery 等一切第三方包。需要 HTTP 就用 `urllib.request`。
2. **所有前端资源必须本地**：只能 `url_for('static', filename=...)` 引用 `static/` 下的文件。**禁止任何 CDN / 外链 JS CSS**（本机访问不到 jsdelivr 等境外 CDN）。ECharts 已本地化在 `static/js/echarts.min.js`。
3. **金额一律用整数「分」**存库与计算，展示时再除 100。
4. **时间统一** `datetime('now','localtime')` 字符串（`YYYY-MM-DD HH:MM:SS`）。
5. **中文界面**，文案像真产品，不要出现"示例""test123"这类占位感强的词。
6. 页面必须**好看**：用 Bootstrap 5 的卡片/表格/徽章/按钮，状态用彩色徽章，别搞成裸 HTML 表格。ECharts 用于需要图的地方。
7. 不要修改 `_scaffold/`、不要动别的项目目录、不要动 `_shared/`（除你 SPEC 明确允许的）。
8. **不要打印/写入任何 API Key、密码、token**。凭据从 `_shared/api.env` 运行时读取。

## 4. 你必须自己做的自测（把真实输出贴进报告）
```bash
cd /g/code/hermes_project/<项目目录>
rm -f data.db && G:/code/hermes_project/.venv/Scripts/python.exe seed.py    # 必须无异常
(G:/code/hermes_project/.venv/Scripts/python.exe app.py > _svc.log 2>&1 &)   # 后台起服务
sleep 3
curl -s -o /dev/null -w "login:%{http_code}\n" http://127.0.0.1:<端口>/login
curl -s -c _ck -d "username=admin&password=admin123" -o /dev/null -w "post:%{http_code}\n" http://127.0.0.1:<端口>/login
# ……按你的 SPEC 逐页 curl，断言 grep -c 命中数 ≥1
```
- **登录默认账号**：`admin` / `admin123`（与 seed.py 一致；如 seed.py 里不是这个就按 seed.py 的来）
- 结束前**必须杀掉自己的服务进程**，别占端口：
  ```bash
  PID=$(netstat -ano | grep ":<端口>" | grep LISTEN | awk '{print $5}' | head -1); taskkill /F /PID $PID
  ```
  ⚠️ 本机是 git-bash 且 MSYS 路径转换已禁用：**`taskkill` 必须用单斜杠 `/F /PID`**，写 `//F` 会报"无效参数/选项"。
- curl 的 cookie jar 写到**项目目录内**（如 `_ck`），写 `/tmp` 会 exit 23。

## 5. 交付报告格式（你的最终回复就用这个结构）
```
## 项目 <N> <名称> 交付报告
### 完成的模块
- ...
### 自测证据（真实输出，逐条）
- `curl ...` → `login:200`
- ...
### 数据库落地证据
- 表名 + 行数（如 `books=60, borrows=25`）
### 启动命令
### 已知问题 / 未做的部分（诚实写，别美化）
```
**不要**写"全部完成、完美运行"这种没有证据的话。有做不到的就写做不到。

## 6. 常见坑（前人踩过）
- 脚手架 `static/css/` 已含 `bootstrap.min.css`，别再去下
- 模板里 `{{ }}` 里的字典取值用 `x['k']` 或 `x.k`（sqlite3.Row 转 dict 后是 dict）
- `db.py` 的 `get_db()` 在**应用上下文外**（seed 脚本）会自动降级直连，别改成用 `g`
- 后台线程里操作数据库要**自己开连接**（用 db.py 的函数即可，它每次新开连接），不要跨线程复用连接
- 长轮询/进度页：前端 `fetch` + `setTimeout` 轮询 JSON 接口，别用整页 meta refresh（观感差）
