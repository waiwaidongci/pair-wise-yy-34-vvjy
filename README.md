# 工伤事故调查与纠正措施

记录工伤经过、伤害、现场和证人，维护调查、纠正措施、验证与关闭流程。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8311
```

默认端口为`8311`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：reporter, investigator, safety_manager, foreman, viewer。严重度越高、伤害指数越大或未关闭措施越多，优先级越高；严重事故必须在4小时内启动调查。

## 伤者台账

事故内嵌伤者台账，防止事故在伤者未结案前提前关档。

- 登记伤者：姓名、编号、岗位、伤部、是否伤情严重、首诊日、预计返岗日。
- 复诊记录：就诊日、活动能力（restricted/partial/full）、岗位限制、医生意见、下次复诊日。
- 待返岗判定（满足任一即保持待返岗）：伤情严重、无复诊、复诊到期未完成、最近复诊活动能力不是full。
- 返岗许可必须由安全员（safety_manager）和班组长（foreman）分别确认，两份确认不能是同一人。
- 最终结论为“返岗(returned)”或“长期限制(long_term_restriction)”；伤情严重时只能下长期限制结论。事故进入verification前，所有伤者必须有最终结论。
- 已关档事故再修改伤情资料或补充复诊，原许可与结论作废，事故自动回到corrective_action（待处理）。
- 台账按待复诊、待许可、可返岗分组，支持按岗位和事故状态筛选。

接口：

- `POST /api/items/{id}/workers`（investigator, safety_manager）
- `GET /api/items/{id}/workers`
- `PATCH /api/workers/{id}`（investigator, safety_manager）
- `POST /api/workers/{id}/followups`（investigator, safety_manager）
- `POST /api/workers/{id}/confirmations`，body含`slot=safety|foreman`，分别需safety_manager、foreman
- `POST /api/workers/{id}/conclusion`（investigator, safety_manager）
- `GET /api/workers/{id}`
- `GET /api/workers?position=&item_status=`，返回三个分组

## 测试

```bash
python3 -m unittest discover -s tests -v
```
