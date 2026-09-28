# 天文瞬变事件警报与后续观测

只使用Python标准库和SQLite的模块化服务，默认端口`8338`。支持全天巡天来源、候选事件去重、坐标与亮度测量合并、优先级计算、观测申请、望远镜排程、撤回、重分类、修正、观测队冲突、质量复核、补观测、归档、角色权限和审计。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：状态机、优先级、测量合并、质量阈值和排程冲突约束。
- `src/repository.py`：SQLite持久化、乐观锁、幂等和审计查询。
- `src/service.py`：用例编排、权限校验、版本控制和补观测待办生成。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：质量录入、补观测待办和归档状态页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8338
```

## 核心对象

`source`为巡天来源，`candidate`为瞬变候选，`telescope`为望远镜，`observation`为后续观测申请。

## 质量复核与补观测

观测状态机：`requested → scheduled → completed → quality_passed → archived`；复核不合格时进入`quality_failed`（终态，原记录保留）。

观测完成后，操作员通过`submit_quality`提交云量(`cloud_cover_pct`)、视宁度(`seeing_arcsec`)、亮限(`limiting_magnitude`)与跟踪偏差(`tracking_error_arcsec`)。默认阈值：云量≤30%、视宁度≤2.5″、亮限≥18.0 mag、跟踪偏差≤1.0″，可用`RuleEngine(quality_limits={...})`调整。

- 全部合格：状态变为`quality_passed`，之后允许`archive`归档；未通过复核的观测不能归档。
- 任一项超限：状态变为`quality_failed`，原记录与超限原因保留，系统自动生成带原因的补观测待办（新的`requested`观测，含`reobservation_of`与`reobservation_reason`）。
- 同一候选已有合格归档结果时，新的观测申请被拒绝并提示已有可用结果；此时质量不合格也不再生成补观测待办（记录`reobservation_suppressed`），避免重复占用望远镜与观测队时段。

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
