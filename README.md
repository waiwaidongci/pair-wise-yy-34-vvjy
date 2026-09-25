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

允许角色：reporter, investigator, safety_manager, team_leader, viewer。严重度越高、伤害指数越大或未关闭措施越多，优先级越高；严重事故必须在4小时内启动调查。

## 伤者台账

- `POST /api/items/{id}/workers`：登记伤者（姓名、编号、岗位、伤部、伤情、首诊日、预计返岗日），仅安全员。
- `GET /api/items/{id}/workers`：查看某事故的伤者台账。
- `GET /api/workers?position=&item_status=`：跨事故台账，按待复诊、待许可、可返岗、最终返岗、长期限制分组，可按岗位和事故状态筛选。
- `POST /api/items/{id}/workers/{wid}/followups`：登记复诊（活动能力、限制、医生意见）。
- `PUT /api/items/{id}/workers/{wid}`：修改伤情资料；`PUT /api/items/{id}/workers/{wid}/followups/{fid}`：修改复诊资料。修改后原许可作废、状态重算并回到待处理。
- `POST /api/items/{id}/workers/{wid}/confirm`：返岗许可确认，须安全员与班组长分别确认，同一伤者的两次确认不得为同一人。
- `POST /api/items/{id}/workers/{wid}/conclusion`：登记最终结论（`returned`最终返岗 / `restricted`长期限制）。

规则：伤情严重、复诊未到或活动能力不达标（`limited`）的伤者只能保持待返岗；最终返岗须先获得返岗许可；事故进入`verification`前，所有伤者必须有最终返岗或长期限制结论；已关档后修改伤情或复诊资料，原许可作废并回到待处理。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
