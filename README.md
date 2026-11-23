# Foxcore 销售跟踪系统

面向音乐节、展销会等临时摊位零售场景的销售与佣金核算系统，提供 Flask Web 管理端和微信小程序现场登记端，覆盖销售登记、佣金实时计提、核算结算与多维报表。

## 功能概览

- Web 管理端：工作台、销售流水、佣金核算、销售报表、佣金规则、基础数据、历史导入
- 微信小程序：登录、工作台（班次/业绩）、登记销售、我的销售
- JSON API：Bearer token 认证，供小程序调用

## 技术栈

- Python 3.13 / Flask 3.1 / SQLite3（无 ORM）
- Jinja2 / 原生 WXML-WXSS / pytest
- 前端为原生 CSS（`app/static/css/app.css`），无第三方前端库；趋势图为纯 CSS 实现
- 金额以“分”为整数存储，计算使用 Decimal，舍入方式为 `ROUND_HALF_UP`
- 业务数据采用参数化 SQL，数据库启用外键约束

## 目录结构

```text
foxcore/
├── app/
│   ├── __init__.py          # Flask 应用工厂
│   ├── db.py                # SQLite 连接与事务辅助
│   ├── auth.py              # 登录、会话与角色校验
│   ├── routes/              # Web 页面与 JSON API 路由
│   ├── services/            # 销售、计佣、结算、报表、导入等业务逻辑
│   ├── templates/           # Jinja2 页面模板
│   └── static/              # CSS、JavaScript 与本地前端依赖
├── db/
│   └── schema.sql           # 数据库建表语句
├── scripts/
│   ├── seed.py              # 种子数据
│   └── backfill_commissions.py
├── tests/                   # pytest 测试集
├── miniapp/                 # 微信小程序
│   ├── app.js
│   ├── app.json
│   ├── app.wxss
│   ├── config.js            # 小程序后端地址配置
│   ├── pages/               # 登录、工作台、登记销售、我的销售
│   └── utils/               # API 请求与通用工具
├── data/
│   └── foxcore.db           # SQLite 数据库（含演示数据快照）
├── docs/
│   └── 需求基线.md
├── init_db.py
├── run.py
├── requirements.txt
└── README.md
```

## 本地运行

在 Windows PowerShell 中执行：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python init_db.py --reset
python run.py
```

浏览器访问：<http://127.0.0.1:5000>

说明：`init_db.py --reset` 会重建本地数据库并写入种子数据，仅在确认无需保留现有数据时使用；日常启动无需重复初始化。

## 演示账号

| 角色 | 账号 | 密码 |
| --- | --- | --- |
| 合伙人 | `admin` | `123456` |
| 销售顾问 | `sales01` | `123456` |

## 测试

```powershell
python -m pytest -q
```

项目 pytest 测试集共 124 条，全部通过。测试覆盖登录权限、销售登记、最低售价、幂等提交、佣金匹配、结算冻结、报表口径、CSV 导入和小程序 API 等关键流程。

## 部署

生产环境部署在 Ubuntu 24.04，使用 waitress 运行 Flask 应用，并通过 Nginx 反向代理提供服务。

公网访问：<http://39.96.75.0>

应用启动命令：

```bash
waitress-serve --listen=127.0.0.1:8000 --threads=4 run:app
```

生产环境需配置 `SECRET_KEY` 环境变量。SQLite 数据库按单进程多线程方式使用，避免多个应用实例同时写入同一个数据库文件。

## 微信小程序

- AppID：`wx0a07fbac98189ea3`
- 后端地址配置：编辑 `miniapp/config.js` 中的 `BASE_URL`
- 导入方式：在微信开发者工具中选择“导入项目”，项目目录选择仓库内的 `miniapp/`

当前 `miniapp/config.js` 配置：

```js
module.exports = {
  BASE_URL: 'http://39.96.75.0'
};
```

本地联调时可将 `BASE_URL` 改为 `http://127.0.0.1:5000`。真机调试时需改为手机可访问的后端地址，并按微信平台要求配置合法请求域名。小程序通过 `Authorization: Bearer <token>` 调用 JSON API。

## 文档

- [需求基线](docs/需求基线.md)
- [GitHub 仓库](https://github.com/Qian-qing19/foxcore-sales)

## 开发记录

- 2026-11-16：完成 SQLite 数据库层，建立 16 张表、幂等种子数据与数据库层 pytest。
- 2026-11-16：完成 D2 Flask 应用骨架、登录、角色菜单与空页面。
- 2026-11-17：D3 阶段 1 完成产品与销售顾问维护（新增、编辑、停用/启用、权限与校验测试）。
- 2026-11-17：D3 完成 /master 五个 Tab 基础数据维护（关联下拉、组合唯一、状态与时间格式校验）。
- 2026-11-17：D4 完成 /rules 佣金规则存储、版本列表、发布与停用（草稿流转、历史版本保留、新版本复制与自动递增校验）。
- 2026-11-17：D5-A 完成 /sales/new 销售登记（当前班次、在售产品、多行明细、严格金额校验、幂等提交与临时库测试）。
- 2026-11-18：D5-B 批次 1 完成佣金规则匹配与销售登记同事务计提（缺规则保持待计佣，重试不重复生成 entry）。
- 2026-11-18：D6-A 完成 /sales 销售流水（角色数据范围、组合筛选、20 条分页、详情佣金展示与作废冲减，11 项临时库测试）。
- 2026-11-18：D6-B 完成 /settlements 佣金核算（期间生成、草稿互斥、单号冲突顺延、17 位顾问汇总、540 条 entry 归属、事务确认冻结、异常清单与 13 项临时库测试）。
- 2026-11-18：D7-A 完成 /reports 多维报表与 CSV 导出（4 维度、Decimal 均价、作废/冲减排除、UTF-8 BOM、非法条件回退与 8 项核心临时库测试）。
- 2026-11-18：D7-B 完成合伙人工作台（四项核心指标、近 7 日纯 CSS 趋势、本月 TOP5 产品、最近 5 笔销售与数据截至时间，覆盖临时库、权限与空数据测试）。
- 2026-11-18：D8-A 完成 /import 历史销售 CSV 导入（模板下载、同交易合并、幂等跳过、逐行错误报告、2MB 限制、IMPORT_BATCH 单事务计数、整体回滚与 13 项临时库测试）。
- 2026-11-19：D9-A 完成微信小程序 JSON API（Bearer 12 小时 token、当前班次/产品/下单/流水/汇总、整数分金额、复用销售佣金事务与 8 项 API 测试）。
- 2026-11-23：重写 README，补充项目概览、技术栈、运行、部署、小程序和文档说明。