# 天文瞬变事件警报与后续观测

只使用Python标准库和SQLite的模块化服务，默认端口`8338`。支持全天巡天来源、候选事件去重、坐标与亮度测量合并、优先级计算、观测申请、望远镜排程、撤回、重分类、修正、观测队冲突、角色权限、审计、观测质量复核与补观测待办。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：状态机、优先级、测量合并和排程冲突约束。
- `src/repository.py`：SQLite持久化、乐观锁、幂等和审计查询。
- `src/service.py`：用例编排、权限校验和版本控制。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8338
```

## 核心对象

`source`为巡天来源，`candidate`为瞬变候选，`telescope`为望远镜，`observation`为后续观测申请，`reobservation`为质量门限触发的补观测待办。

## 质量复核与补观测

后续观测完成后不会直接归档，必须经过质量门限与值班长复核：

1. 操作员执行`complete`时必须提交四项质量指标，缺一不可：
   - 云量 `cloud_cover`（0–1，门限 ≤ 0.30）
   - 视宁度 `seeing_arcsec`（角秒，门限 ≤ 2.0）
   - 亮限 `limiting_magnitude`（星等，门限 ≥ 19.0，数值越大越深）
   - 跟踪偏差 `tracking_error_arcsec`（角秒，门限 ≤ 1.5）
2. 指标全部合格：观测进入 `review_pending`，原记录保留；值班长（supervisor）执行 `review {decision: approve}` 后才归档为 `archived`。
3. 任一项超限：观测进入 `needs_reobservation`，原记录与逐条超限原因一并保留，并自动生成 `reobservation` 待办（状态 `open`，携带原因、来源观测、候选、望远镜、观测队）。值班长也可在复核时 `reject`（须填原因）要求补观测。
4. 为补观测新建的观测申请携带 `reobservation_id`；排程后待办变为 `scheduled` 并记录关联申请。补观测再次失败时复用同一条待办（回到 `open`），不会重复堆积。
5. 同一候选已有 `archived` 合格结果时：
   - 新建普通观测或补观测申请返回冲突，排程也会被拒绝，不再占用望远镜与观测队时段；
   - 归档合格结果时，系统自动 `resolve` 被本次观测满足的待办，并 `cancel` 该候选其余待办；
   - 已有合格结果之后到达的失败完成不会再开新待办（仅记录审计）。
6. 待办可手动 `cancel`（须填原因）。

观测状态流转：`requested → scheduled → review_pending → archived`，或
`scheduled/review_pending → needs_reobservation →（补观测申请）→ … → archived`。

演示页面（`/`）可录入质量结果、查看补观测待办与归档状态、执行复核与取消。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `GET /api/audit`

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

轨道和观测计划使用简化的字符串时间窗比较，不包含真实天文历表、可见性预报、望远镜控制系统和观测数据存储。
