# 美国商业财产险灾害预警：架构与决策设计

研究日期：2026-09-14。本文是设计建议，没有部署、创建或调用任何 AWS 资源。轮询频率、保留期限、业务阈值与验收目标均为建议初值，需要结合公司承保授权、产品条款、实际数据质量和运行负载确定。

## 1. 推荐结论

第一阶段建立一套由后端统一采集、保存与空间匹配的 hazard alert service，前端展示某个建筑或保单地点涉及的官方预警及其时间、来源和精度。可以暂时不改变原有报价与出单逻辑，但不建议浏览器直接调用政府 API 并独立判断风险。

第二阶段在同一数据服务之上建立独立、可版本化的 moratorium policy engine。产品 feature flag 只负责启用 `off / shadow / enforce` 模式；具体哪些产品、哪些地点、哪些交易被限制，由有生效时间和审批记录的规则决定。bind、issue、增加地点和提高限额等实际写入动作必须由服务端最后检查。

实时事件能辅助 appetite 与人工核保；它不会自动给出建筑的长期损失成本，更不能把 NWS severity 或 FEMA 相对风险分数直接转换成保费倍数。长期 rating 应另接历史灾害、建筑特征、保障范围、损失经验和经过审批的 rating model。FEMA NRI 的 Expected Annual Loss 是基于暴露、年化频率和历史损失比构建的长期指标，适合作为研究层的背景数据。[FEMA NRI 技术文档](https://www.fema.gov/sites/default/files/documents/fema_national-risk-index_technical-documentation.pdf)

## 2. Source priority 与事件含义

所有来源都保留原始信息；优先级用于决定某一种事实依赖哪个来源，不能用“一个等级更高的来源”把其他来源整条覆盖。

- 气象 watch / warning / advisory：NWS CAP 是操作预警主线。保留其 `event`、`severity`、`urgency`、`certainty`、`messageType`、`status`、`references`。NWS API 免费、要求应用 User-Agent，并只提供近七日 alerts，所以平台必须自行保留历史。[NWS API](https://www.weather.gov/documentation/services-web-api)
- 热带气旋：NWS 提供地方警报，NHC/CPHC 提供 storm identifier、预报轨迹与风/风暴潮产品。把同一 storm 下的风、降雨洪水、风暴潮保持为独立 peril。NHC forecast cone 表示中心路径的不确定性，不能作为完整灾害影响区或“一出圈即安全”的规则。[NHC cone 定义](https://www.nhc.noaa.gov/aboutcone.shtml)
- 野火：官方 incident/perimeter 数据提供已知火情与制图边界；NWS Red Flag / Fire Weather Watch 提供有利于火势发展的天气条件。两者不可互相替代：Red Flag 不是已发生野火；perimeter 也不保证覆盖飞火、烟尘或全部受威胁建筑。
- 地震：USGS 事件记录提供已观测地震；震中 Point 是位置，不是受损范围。早期仅做信息提示；若要据此限制承保，需采用 ShakeMap 等地面震动范围/强度证据，或由承保人明确审批并标记为公司推导的缓冲规则。
- 水灾：NWS flood/flash flood/coastal flood warnings 是预警主线。NWPS/USGS 河道站点用于观测佐证；水位超过阈值只代表站点状态，不能自动当成周边所有建筑的淹水范围。FEMA flood zone 是长期地图，不代表当前洪水。
- FEMA disaster declarations：行政声明、地理指定范围、灾后上下文与审计证据。声明有事后性，不能成为实时灾害启动或解除的唯一条件。应使用当前 v2 数据集；旧版页面说明的数据角色同样是联邦灾害声明，不是实时观测。[FEMA 数据集说明](https://www.fema.gov/zh-hans/about/openfema/disaster-declarations-summaries)、[v2 目录](https://www.fema.gov/openfema-data-page/disaster-declarations-summaries-v2)

首批应重点覆盖 tropical cyclone/wind、tornado、severe thunderstorm wind/hail、flash/river/coastal flood、wildfire、winter storm/ice/heavy snow、extreme cold/freeze、earthquake。海啸与火山适合有 Alaska/Hawaii/西海岸暴露的产品；高温、干旱、沙尘、一般降雪、烟雾按建筑用途与保障条件决定是否启用。Marine warnings 默认不用于普通陆上 commercial property；不要把 NWS 所有公共安全事件不加区分地塞入商业财产险告警。

UI 应使用“官方预警覆盖”“观测事件附近”“灾害行政声明”这些可验证措辞。即使是 Warning，也不能一律显示为“该建筑正在遭灾”；CAP `certainty=Observed` 也不等于每栋建筑都有损害。

## 3. AWS 结构：先把数据层独立出来

建议的数据流：

```text
EventBridge Scheduler（每个数据源独立 schedule）
    → SQS ingestion queue（失败进入 DLQ）
    → Fetch / normalize worker（普通 Lambda 为默认候选）
        → S3：压缩原始响应 + HTTP / checksum / run manifest
        → RDS PostgreSQL + PostGIS：事件版本、当前视图、地理边界
        → transactional outbox：成功发布后的变化消息
    → spatial match / eligibility service
        → 既有平台 API → underwriter UI
        → 第二阶段：bind / issue 服务端 gate
CloudWatch：延迟、失败、数据陈旧、几何失败、队列积压
```

一个数据源一个运行锁或有期限的 lease，防止上轮未结束又并发覆盖。SQS 标准投递及 Lambda event source mapping 存在重复处理，必须用 `(source, source_id, source_revision_or_payload_hash)` 唯一键实现幂等；写数据库和 outbox 在同一事务中提交。客户端通知只消费 outbox 中已提交的变化，不能从下载了一半的文件发布。[AWS SQS 与 Lambda](https://docs.aws.amazon.com/lambda/latest/dg/with-sqs.html)

普通 Lambda 的最长执行时间是 900 秒。实测下载/解压/几何处理超过该范围时，拆分任务或使用 ECS Fargate task；不要在函数内长时间等待下一次轮询。[AWS Lambda timeout](https://docs.aws.amazon.com/lambda/latest/dg/configuration-timeout.html)

RDS PostgreSQL 支持 PostGIS，适合点与多边形、周边距离及版本化数据的查询。若现有产品不是 PostgreSQL，也可建立隔离的 hazard 数据库，由 API 交互；不需要为此更换核心保单数据库。[AWS RDS extensions](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Appendix.PostgreSQL.CommonDBATasks.Extensions.html)

MVP 可以先采用 Scheduler → Lambda，不强制第一天引入所有队列与事件组件；只要保留相同任务边界、幂等键、运行记录和原子发布机制。普通 Lambda 的公网抓取与私有 RDS 网络连接、出口流量、连接池/并发和运维成本，需要在部署设计时单独核算。当前没有足够流量假设支持精确 AWS 月费报价。

## 4. 调度与 freshness

以下是消费端轮询建议，不是上游出新数据的承诺：

- NWS active alerts：60 秒。官方建议不要快于每 30 秒请求一次；全公司统一抓取、条件请求与缓存，避免每个 underwriter 自己轮询。[NWS Alerts Web Service](https://www.weather.gov/documentation/services-web-alerts)
- USGS earthquake summary：60 秒。官方 summary feeds 每分钟更新；用近一日或更长滑动窗口避免漏掉重新连接时的事件，定期用较长窗口纠正修订。[USGS GeoJSON feed](https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php)
- NHC/CPHC：有活跃 cyclone 时 5–10 分钟，无活跃 cyclone 时 30 分钟。完整 advisories 通常每六小时发布，特殊更新和部分中间产品更频繁，所以只按六小时抓取会错过特别更新。[NHC 产品说明](https://www.nhc.noaa.gov/aboutnhcprod.shtml)
- 官方野火 incidents / perimeters：15 分钟起步；区分本次成功下载时间和单条 perimeter 的实际制图时间，不能把每十五分钟抓一次解读为边界每十五分钟更新。
- 可选 NWPS/USGS 水位：15 分钟起步，仅针对实际 portfolio 相关站点；站点更新频率、实时性与产品限制单独记录。
- FEMA declarations：60 分钟起步，属于上下文；跨多个过去月份做 `lastRefresh` 或等价更新字段的增量回补，并定期完整对账，不能仅查询“今天声明的灾害”。
- 静态 county / state / NWS zone / flood / NRI 边界：按官方版本发布或定期检查更新；保存版本有效期，不能与实时事件使用同一“5 分钟过期”逻辑。

EventBridge Scheduler 的调用精度为 60 秒，即一个标记为 01:00 的触发可以落在 01:00:00–01:00:59。关键预警的 flexible time window 设为 OFF。`rate(1 minute)` 不能承诺秒级实时；实际延迟等于上游发布延迟、计划触发等待、抓取处理和 UI 刷新延迟之和。[AWS Scheduler](https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html)

初始运维阈值可设为：NWS/USGS 超过 3 分钟无成功完整获取进入 `degraded`，超过 10 分钟进入 `stale`；NHC 活跃期超过 30 分钟无法成功获取进入 `stale`；wildfire 超过 60 分钟无法成功获取进入 `stale`。这些值需通过历史回放与运行数据校准。NHC advisory 连续几小时未变通常是正常节奏，不能按原文发布时间直接判定源故障。

必须同时持久化：`last_attempt_at`、`last_successful_fetch_at`、`last_complete_snapshot_at`、`source_published_at`、`source_updated_at`、`ingested_at`、`effective_at`、`expires_at`。保留异常的 clock skew/未来时间并隔离，不默默改成现在。一个成功 HTTP 200 不等于完整快照：schema、分页、服务错误正文、记录数量异常及几何覆盖率都要过检查。

失联时不把旧记录全部设为结束，也不输出“无风险”。应返回 `unknown / degraded` 与 last known state；有效期自然到期的警报仍可标记 expired，但明确“来源失联，无法确认是否有接续警报”。只在完整快照成功、显式 cancel/expire/update 链或明确的来源生命周期证据下更新相应状态。

## 5. 数据储存与版本

S3 按 `source/yyyy/mm/dd/run_id/` 存 gzip 原始响应和 manifest。manifest 包含 endpoint、请求参数、HTTP status、ETag/Last-Modified、下载时间、checksum、分页计数、记录数、parser/schema 版本和完整性结论。没有变更的 304 可只记运行记录并引用已有内容对象。与业务决定关联的原始证据应按公司记录保留制度保留，不能机械采用公共 feed 的七日窗口。

PostgreSQL 建议分为以下实体：

- `ingestion_run` / `source_health`：每次采集尝试、快照完整性与源健康。
- `event_revision`：append-only 事件版本，原始来源键、标准 hazard、source status、evidence type、时间、原始文本与扩展属性 JSONB、原始对象引用。
- `event_current`：指向最新有效版本的查询视图。新快照只在所有分片处理完后原子发布，不能局部覆盖“当前”。
- `event_geometry`：原始几何与清洗几何、SRID、geometry_role、resolution、来源和版本。geometry_role 区分 warning_polygon、zone_fallback、observed_perimeter、epicenter、forecast_cone、derived_buffer、administrative_area。
- `boundary_version`：county/state/NWS zones 等边界和有效版本。
- `location_match`：property location、建筑/坐标版本、event revision、匹配方法、交叠或距离值和数据完整性。
- `moratorium_policy_revision`、`decision_audit`、`override`、`outbox`：第二阶段的规则和审计。

推荐起步保留完整原始变化版本和标准化历史至少一个完整灾害季；具体保留年限由实际审计、承保与记录保留需求确定。近 90 天是热查询层候选，更旧内容可转低成本归档。这里的期限是容量设计示例，不是法律结论。

不能只用 `(source, event)` 的中文名称去重。同一 storm 可产生多个 peril；多个来源也可能描述同一灾害不同侧面。保留 source identity，并以可撤销的 `incident_cluster_id` 关联。alert dedup 与物理灾害聚合要分开。

## 6. 空间匹配的精度

输入优先为已校验的建筑点或 footprint，而非 ZIP centroid。点与原始/清洗后的 warning polygon 做 `ST_Intersects`，边界点也视为命中；GiST index 支持空间筛选。距离查询使用以米为单位的 geography 或合适投影，不能把经纬度的“度”当成米。[PostGIS ST_Intersects](https://postgis.net/docs/en/ST_Intersects.html)

NWS geometry 可能为空，此时使用 affectedZones / UGC 对应的官方 zone 边界做 fallback，保留较低精度标签。不要把 UGC 的 `C` county 与 `Z` forecast zone 混为一谈，也不要仅靠 `areaDesc` 自由文本推断精确建筑覆盖。

震中/火点不宜在 UI 上伪装成面范围。若引入距离缓冲，保存公司阈值与 policy version，并写明“公司定义的邻近范围”；它是 risk screening，不是官方 damage footprint。野火 perimeter 的制图时间、缺失片段和 incident 点替代也必须透明呈现。

美国范围至少包含 50 州和 DC；territories 单独作为可配置扩展。Alaska 跨日期变更线，不能用一个粗糙美国 bounding box 判断是否命中。邻近加拿大/墨西哥或海上的地震/气旋可能影响美国建筑，应按美国暴露与影响范围筛选，不能简单删掉震中/风暴中心在美国外的事件。

查询结果要区分 `matched`、`no_match_with_current_coverage`、`unknown_due_to_missing_geometry_or_source`，尤其要防止“几何缺失所以查不到”被当成安全。多地点商业财产保单逐地点评估，再由产品规则决定是否整单 referral 或只限制受影响地点。

## 7. 前端与平台 API

alert 阶段可以只影响展示，但需要一个轻量后端做数据采集、统一空间判断、权限、审计和源健康。前端只读 hazard service；无需直接接触官方 API，避免各浏览器得到不同版本和暴露内部规则。

建议契约：

- `GET /hazards?bbox=...&hazard=...`：地图可视区的简化范围与列表，支持 source/time/status filter。
- `GET /locations/{id}/hazard-assessment`：已授权地点的匹配结果。返回 snapshot/version、evaluated_at、source freshness、匹配详情与官方链接。
- `POST /hazard-assessments`：批量地点或提交前的新地址评估；由后端校验地点输入。
- `GET /hazards/{id}/revisions`：显示范围扩大、降级、取消及版本历史。

首版 UI 每 30–60 秒查询自己的 API，使用 ETag / If-None-Match。规模较大或需要即时刷新时再加 SSE/WebSocket，让变化通知驱动前端重新读取；通知本身不包含可直接用于出单的授权。以更新后的事件版本为通知去重键，避免每次抓取原文无变化都弹窗。

underwriter 看见的最小内容：险种相关 hazard 名称、官方产品类别、预报/观测/行政状态、有效时间、最后验证时间、来源、匹配精度、受影响地点数、原文链接、`information / review suggested / restricted / data unavailable` 业务状态。业务建议与原始 severity 分开显示。没有资料时显示“暂无法确认”，不是绿色“无灾害”。

## 8. Moratorium 与 feature flag

推荐三个 flag：`hazard_alerts_enabled`、`moratorium_mode=off|shadow|enforce`、`moratorium_manual_only`，作用域可到 tenant、carrier、product。flag 控制发布模式；一个全局布尔 `can_bind=false` 无法表达产品、地区、时间和交易类型，也难审计。

一条 policy 至少包含：policy_id、不可变 revision、审批状态与审批人、carrier/product/coverage、jurisdiction、peril、允许的数据来源与证据类型、严重程度或其他触发条件、官方/公司地理范围、location match quality 最低要求、适用交易、有效与终止时间、延续与解除条件、例外和 unknown handling。

交易作用域应显式列出 new_business_bind、issue、add_location、increase_limit、coverage_change、renewal。是否限制 future effective date、已报价未出单、endorsement 或 reinstatement，要写入产品规则；不要把“新业务暂停”自动套用于续保、取消或拒绝续保。

policy 生命周期：`draft → reviewed → approved → scheduled/active → released/superseded`。解除与启动同样留痕；可按地理区域或 peril 部分解除。官方 alert expired 不等于公司 moratorium 自动解除，除非已审批规则明确如此。最低停留时间和冷却期也是公司参数，不能伪装成官方结论。源故障禁止触发“自动解除”。

前端可提前提示；服务端在 bind/issue 前重新以最新 location version、policy revision、已发布 hazard snapshot 执行评估并记录。assessment token 即使由服务端签发，也不能因为几分钟前通过就永久有效。核心写入前检查有效期限和规则/数据版本；如果检查后版本又改变，通过事务序列化或提交前版本校验重评，明确出单决定生效的时间点，防止检查和写入之间的竞态。

返回业务枚举 `ALLOW / REFER / BLOCK / UNKNOWN`，附 decision_id、rule revision、source evidence IDs、location matches、reason_codes、evaluated_at、valid_until。多个规则有冲突时由显式优先级合并，例如已批准强制 BLOCK 优先；不能靠随机读到的最后一个 flag 决定。

对于数据失联建议：信息提示阶段继续平台操作但显示 degraded；enforce 阶段受该源/地点/规则影响的新业务交易进入 REFER 或待处理，保持已存在且未解除的限制。不要把全国所有交易一律封禁，也不要把未知自动判为 ALLOW。究竟是否允许 fallback 或授权人工 override 是产品级规则。API 技术错误不能伪装成明确 underwriting BLOCK。

override 需要授权角色、具体交易/地点范围、理由、有效期、原 policy revision 和审计；高影响 override 可设第二人审批。单次 override 不应关闭整个产品 flag。决策日志应足以复现“当时已知的数据和已生效的规则”，同时保留后来修订的事实，避免以今天的几何重算后覆盖昨日决定。

这里的 binding moratorium 与监管要求的禁止取消/不续保不是同一概念。例如 California 公布的野火一年 moratorium 明确针对特定区域的 residential property cancellation/non-renewal，不能直接推导为全国 commercial property 新业务规则。将两类 policy 分域是架构要求；具体产品适用性由公司有权限人员确认。[California Department of Insurance](https://www.insurance.ca.gov/01-consumers/140-catastrophes/MandatoryOneYearMoratoriumNonRenewals.cfm)

## 9. 分阶段完成标准

阶段 A：本地 ETL 证明。每个 source 有真实成功抓取或明确失败记录、原始响应和 checksum、解析统计、hazard mapping、字段清洗、时区归一化、geometry 状态、可重复回放。记录当前快照零记录不代表连接失败；但没有真实 active 样本不能声称已验证该 active 分支。

阶段 B：alert pilot。接 NWS、USGS 与 wildfire，补齐 zone/US boundaries 和 property geocoding；先选实际存在的一个 carrier/product portfolio。至少演练更新、取消、源断连、迟到/乱序、几何缺失、全量快照不完整、重复投递和跨边界地点。量化抓取成功率、上游已发布到平台可见的延迟、匹配准确性与告警噪音。

阶段 C：shadow moratorium。服务端同时计算拟拦截结果但不阻断真实交易；与人工承保决定对照，检查误拦、漏拦、未知率、影响产品和潜在业务中断量。阈值、缓冲距离和解除规则依据回放结果审批。

阶段 D：有限 enforce。按 carrier/product/territory 开启，先用人工审批后激活的 scoped policy；让 bind/issue gate、override、审核与回滚工作流同时上线。验收包括服务不可用、陈旧数据、规则刚更新、已报价后新灾害、地点刚变更、多地点部分命中、直接 API 绕过 UI、重复提交和规则解除。

不可省略的完成证据：无变化的重复 ETL 不产生重复事件/通知；失败或部分快照不清空 active 集合；缺失几何不产生 false clear；原始版本可重放；后端控制对 UI 与直接 API 一致；每次限制或放行都能找到当时 policy 和 evidence。
