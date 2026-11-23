# AGENTS.md — Foxcore 销售跟踪系统

> 给 AI 编码代理的项目约定。**每次开工先读本文件；本文件与用户口头要求冲突时，先问用户。**

## 1. 项目一句话

为 Foxcore Retail（音乐节/展销会临时摊位零售商）实现的销售跟踪系统：销售顾问现场登记销售，系统按规则实时计提佣金，合伙人核算结算并查看多维报表。设计依据见 `docs/需求基线.md`（源自 48 页课程设计文档）。

## 2. 技术栈（不得擅自更换）

- Python 3.13 + Flask 3.x（同步，不用 async）
- Jinja2 模板 + 原生 HTML/CSS/JS
- SQLite3（Python 标准库 `sqlite3`，**不用 ORM**，直接写 SQL）
- Bootstrap 5 与 ECharts 使用 `static/vendor/` 下的**本地文件**（演示可能断网，禁止引用 CDN）
- 测试：pytest
- 不使用：Django、FastAPI、SQLAlchemy、React/Vue、Redis、Celery、Docker

## 3. 运行与命令

```powershell
# 首次
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

# 初始化数据库（建表 + 种子数据；--reset 先删库）
python init_db.py --reset

# 启动
python run.py          # http://127.0.0.1:5000

# 测试
python -m pytest -q
```

## 4. 目录结构（新增文件必须放在对应位置）

```
app/
  __init__.py          # create_app()
  db.py                # 连接、row_factory、事务辅助
  auth.py              # 登录、session、角色装饰器
  routes/
    auth_routes.py
    dashboard.py
    sales.py
    settlements.py
    reports.py
    rules.py
    master.py
    imports.py
  services/
    sales_service.py
    commission_service.py
    report_service.py
    import_service.py
  templates/
    base.html
    login.html
    dashboard.html
    sales_new.html
    sales_list.html
    settlements.html
    settlement_detail.html
    reports.html
    rules.html
    master.html
    import.html
  static/
    css/app.css
    js/app.js
    vendor/            # bootstrap.min.css, echarts.min.js
data/foxcore.db        # 不提交、不删除（演示数据）
db/schema.sql          # 建表语句（唯一事实来源）
scripts/seed.py        # 种子数据
tests/                 # pytest
run.py
init_db.py
requirements.txt
README.md
```

## 5. 数据与金额约定（违反会导致对账失败）

- **金额一律用“分”存 INTEGER**，字段名以 `_cents` 结尾；展示时 `分 / 100`，保留两位小数。
- 计算用 `decimal.Decimal`，禁止用 `float` 做金额运算，禁止裸 `round()`。
- 时间：`YYYY-MM-DD HH:MM:SS`；日期：`YYYY-MM-DD`；统一按本地时区（Asia/Shanghai）。
- 状态值用中文枚举，严格按 `docs/需求基线.md` 第 7 节，不得自造新值。
- 主键：`INTEGER PRIMARY KEY AUTOINCREMENT`；外键开启 `PRAGMA foreign_keys = ON`。
- 删除：业务数据一律“作废/停用”，不物理删除。

## 6. 核心业务规则（必须实现，且写测试）

1. **最低售价**：`SALE_ITEM.final_unit_price >= PRODUCT.min_price`，否则拒绝整单（MVP 不做合伙人审批流，提示“需合伙人批准”即可）。
2. **数量**：正整数；`line_amount = quantity * final_unit_price`。
3. **可售条件**：产品 `在售`、班次 `进行中`、摊位属于该班次、销售顾问在职。
4. **幂等**：`SALE.client_txn_id` 全局唯一；插入冲突时查询原单并返回原 `sale_id`，不重复写明细/佣金。
5. **计佣**：按 `SALE_ITEM` 逐行匹配规则；一条明细只命中一条规则。
6. **规则匹配顺序**：状态=`有效` → 生效期覆盖销售日期 → 维度匹配（产品关联表 / 顾问关联表 / 活动类型）→ 优先级降序 → 版本号降序 → 取第一条。命中结果写入 `COMMISSION_ENTRY.rule_id`。
7. **缺规则**：销售照常保存，`SALE.status='待计佣'`，不生成 `COMMISSION_ENTRY`，在核算异常清单中列出。
8. **作废**：`SALE.status='作废'`，记录 `void_reason/void_by/void_at`；报表按净额统计；已计佣金在下一期冲减（P1）。
9. **结算快照**：生成草稿时回填 `COMMISSION_ENTRY.settlement_id`；确认后 `COMMISSION_SETTLEMENT.status='已确认'`、`COMMISSION_ENTRY.status='已结算'`，金额冻结；此后修改规则不得改变已确认金额。
10. **报表口径**：净销售额 = 有效明细 `line_amount` 之和；平均成交价 = 净销售额 / 数量；佣金只统计有效明细。

## 7. 页面与路由

| 路由 | 方法 | 角色 | 说明 |
| --- | --- | --- | --- |
| `/login` `/logout` | GET/POST | 全部 | 账号密码登录 |
| `/` | GET | 合伙人 | 工作台 |
| `/sales/new` | GET/POST | 销售顾问 | 登记销售 |
| `/sales` | GET | 全部 | 流水（顾问只看自己） |
| `/sales/<id>/void` | POST | 合伙人 | 作废 |
| `/settlements` | GET/POST | 合伙人 | 生成核算 |
| `/settlements/<id>` | GET | 合伙人 | 明细 + 确认 |
| `/settlements/<id>/confirm` | POST | 合伙人 | 确认快照 |
| `/reports` | GET | 合伙人 | 多维报表 |
| `/reports/export` | GET | 合伙人 | CSV（utf-8-sig） |
| `/rules` | GET/POST | 合伙人 | 规则版本 |
| `/master` | GET/POST | 合伙人 | 产品/活动/摊位/顾问/班次 |
| `/import` | GET/POST | 合伙人 | CSV 导入 |

## 8. 代码规范

- 路由层只做：取参数、鉴权、调 service、渲染/重定向；**业务逻辑全部放 service**。
- 每个 service 函数：类型注解 + docstring + 一条 pytest。
- 数据库访问统一走 `app/db.py` 的 `get_db()`，用参数化 SQL（`?`），禁止字符串拼接。
- 写操作必须 POST，成功后 `redirect` + `flash`，失败返回原页面并保留输入。
- 模板统一继承 `base.html`；金额显示用 Jinja 过滤器 `yuan`。
- 错误页：404/500 各自一个模板；不允许把 traceback 直接显示给用户。

## 9. 禁止事项

- 不删除 `data/foxcore.db`；不执行 `DROP TABLE`（除非 `init_db.py --reset` 显式调用）。
- 不修改 `db/schema.sql` 而不同步更新 `init_db.py` 与种子数据。
- 不引入需要编译的依赖（如 `mysqlclient`、`psycopg2`）。
- 不一次性改超过 5 个文件；不做与当前任务无关的重构。
- 不在模板里写业务计算；不在路由里写 SQL。
- 不生成真实个人信息；种子数据用“顾问01”这类虚构姓名。

## 10. 完成定义（DoD）

一个任务只有同时满足以下 5 条才算完成：

1. `python run.py` 能启动，页面能打开；
2. 操作后数据真的落库（刷新仍在）；
3. 至少覆盖 1 条边界（错误输入有提示、不 500）；
4. `python -m pytest -q` 通过；
5. 在 `README.md` 的“进度”里加一行说明。

## 11. 验收用例（每次回归都跑）

| # | 场景 | 期望 |
| --- | --- | --- |
| 1 | 单价低于最低售价 | 拒绝，`SALE` 不落库 |
| 2 | 同一 `client_txn_id` 提交两次 | 只 1 条 `SALE`，返回原单号 |
| 3 | 停用产品登记 | 拒绝，提示产品停用 |
| 4 | 无匹配规则 | 销售保存，状态 `待计佣`，无佣金记录 |
| 5 | 确认结算后改规则并重算 | 已确认金额不变 |
| 6 | 作废销售后看报表 | 净额减少，佣金不计 |
| 7 | 报表合计 vs 明细合计 | 分毫不差 |

## 12. 沟通约定

- 动工前先给“计划 + 要改的文件清单”，等确认。
- 报错时贴完整日志与复现步骤，先给原因再给最小修复。
- 不确定业务规则时，**停下来问**，不要猜。

## 13. 部署约定（第 6 天用）

- 生产启动：`waitress-serve --listen=127.0.0.1:8000 --threads=4 run:app`。
- **SQLite 只允许单进程多线程**，禁止 gunicorn 多 worker、禁止多实例共享同一个 db 文件。
- `run.py` 必须同时支持两种方式：`python run.py`（开发）和 `waitress-serve ... run:app`（生产）。
- 生产环境 `debug=False`；`SECRET_KEY` 从环境变量读，缺省时启动直接报错，不许用硬编码默认值。
- 部署后默认密码必须更换；演示前用 `python init_db.py --reset` 重置数据。
- 代码里不写死 `C:\...` 路径，统一用 `Path(__file__).resolve().parent` 推导。
- `data/` 目录必须对运行用户可写；提供备份命令或脚本。
