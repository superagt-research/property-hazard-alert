# USGS 地震与 NIFC/WFIGS 山火：数据可行性验证

实测日期：2026-09-14，UTC。本文只涉及公开数据读取与本地 ETL；未部署 AWS、未修改产品开关、未建立保险禁保规则。

## 可直接用于第一阶段的结论

USGS 可用于“某处已发生地震”的快速提示；ShakeMap 是后续评估某个地址经历多强震动的补充。地震震中、震级、PAGER 全国/事件级颜色等级都不能替代建筑物层面的损伤、风险定价或禁保范围。

NIFC/WFIGS 可用于山火位置及已知燃烧边界提示，但应明确显示观测时间和未知范围。实测约六成事件没有边界，且多数已有边界超过一天未更新。定期成功下载与灾害事实的新鲜程度必须分开管理。

## 官方来源与更新安排

1. **USGS GeoJSON M2.5+ past day**：`https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson`。官方说明 past-day feed 每分钟更新；建议每 60 秒拉取，使用 HTTP ETag/Last-Modified，网络异常重试并保持最后成功数据。震级 2.5 是低成本的采集门槛，并不是 property insurance 的损伤阈值。来源：[USGS GeoJSON Summary](https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php)、[USGS 实时 feed 说明](https://earthquake.usgs.gov/earthquakes/feed/)。

2. **USGS event detail / ShakeMap**：跟随 summary 的 `properties.detail`，从 `products.shakemap` 选取有效且 preferredWeight 最高的产品，同 source/code 先按更新时间取最新版本，处理 DELETE/CANCELLED，排除 TEST/SCENARIO。来源：[GeoJSON Detail](https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson_detail.php)、[ShakeMap 产品定义](https://ghsc.code-pages.usgs.gov/hazdev/pdl/userguide/products/known-types/shakemap.html)。建议新事件立即检查，首小时每分钟、之后 24 小时每 5 分钟、再之后按业务保留窗口复查；这是应用设计建议，不是官方发布 SLA。生产上按 event/product revision 排队，只下载变化产品。

3. **NIFC current incidents**：[官方 Incidents REST Layer](https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Incident_Locations_Current/FeatureServer/0)。**NIFC current perimeters**：[官方 Perimeters REST Layer](https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Interagency_Perimeters_Current/FeatureServer/0)。只采集 `IncidentTypeCategory='WF'`；prescribed fire `RX` 不作为自然山火告警默认输入，complex `CX` 作为可扩展的聚合关系处理。两层建议每 5 分钟检查。官方元数据说明服务每 5 分钟刷新、边界变化可能最多需 15 分钟显示；这是管道延迟，还没有包含现场测绘间隔。来源：[NIFC 官方元数据](https://www.arcgis.com/sharing/rest/content/items/d1c32af3212341869b3c810f1a215824/info/metadata/metadata.xml?format=default&output=html)、[WFIGS 数据计划](https://data-nifc.opendata.arcgis.com/pages/d6ef1367fadc4405b5f09c98e52ed972)。

Current 视图存在按火面积及 3/8/14 天未更新清除记录的规则，且火灾属性与边界取自不同业务来源；消失不等于已经扑灭。应保留记录并触发结束状态复核。事件点所属州、county FIPS 是起火点位置，并不是整县影响范围。缺 perimeter 不能根据点随意画一个被称为“灾区”的圆。

## 本次实际运行结果

### USGS

2026-09-14T12:58:13Z 开始的 `collect()` 请求读取 38 条全球地震，全部通过统一数据校验；4 条存在可用 ShakeMap，4 份 grid.xml 成功读取，均无 enrichment 错误。原始请求、时间、字节数、HTTP 状态与哈希记录在 `data/probes/adapter_live/usgs_manifest.json`。统一 runner 的独立实跑结果见 `data/reports/usgs_latest.json`。

这里的 38 **不是美国地震数量**。USGS summary 是全球 feed；ETL 故意保留原始全球范围并标注 `global_feed_requires_us_or_portfolio_spatial_filter`，后置使用真实美国边界/投保地址做范围判断。跨境地震可能影响美国，不能只用震中在美国境内作为损伤相关性筛选。美国范围需明确包含 Alaska、Hawaii，以及产品实际承保的 territories。

Hawaii 示例 `hv75034997` 的 event detail 使用 `us7000th7l` 的 ShakeMap code；可见 event ID、别名和产品 code 不一定相同，不能用字符串相等强行连接。该图 version 4，含 203×202=41,006 个节点；MMI 最大 3.7，PGA 最大 1.936 **%g**。原始 product 的其他汇总值不能在未检查单位时直接按 grid 的 `%g` 解释。示例坐标 -155.48, 19.17 最近节点的 MMI 为 3.6、PGA 为 1.871 %g，标记为 modelled estimate；纽约坐标在图外，函数返回 unknown，不返回 0。

代码按 XML `grid_field.index/name/units` 读取，检查字段顺序、行数、有限数值和预计网格尺寸。`sample_shakemap_grid()` 提供最近节点采样可行性演示，明确不是损失模型。生产级地址采样应确定插值方法、保留不确定性、处理跨日界线网格，并按官方现行 HDF/raster 输出升级。`grid.xml` 本次可获取，但不应忽略产品演进。

USGS 说明平滑 GeoJSON 等值线主要用于展示，不适合精细分析或损失建模，因此未把 `cont_mmi.json` 的线、网格 extent 或震中 buffer 伪造为 damage footprint。来源：[ShakeMap Products and Formats](https://ghsc.code-pages.usgs.gov/esi/shakemap/docs2020/manual4_0/ug_products.html)、[现行 ShakeMap Software Guide](https://ghsc.code-pages.usgs.gov/esi/shakemap/index.html)。

### NIFC/WFIGS

统一 runner 在 2026-09-14T12:58:34Z–12:58:48Z 完成：19 次请求，约 37.6 MB 响应内容；**446 条归一化事件，0 条 quarantine**。该完整快照中：

- 180 条有可连接的 observed perimeter，266 条只有事件点。
- 156 个 perimeter 的观测时间超过 24 小时；18 个没有观测时间。因此仅有 6 个在本次设计的 24 小时时效阈值内且时间已知。
- 317 条 incident 的记录修改时间超过 24 小时，prototype 将其 lifecycle 标成 unknown，保留历史记录与提示。
- 249 条 containment 未知；93 条已报告 100% contained 但没有 fire-out 时间，不能将其转换成已扑灭。

24 小时是本 prototype 为暴露数据时效问题而选的可配置阈值，**不是 NIFC 官方可信度等级，也不是保险禁保或解除阈值**。incident 修改时间可能只是属性编辑，不一定代表现场重新观测；perimeter 另存 `perimeter_observed_at` 与 `perimeter_record_updated_at`，不得用 HTTP 下载时间或后台同步时间刷新边界的现场观测年龄。

准确证据以 `data/reports/nifc_latest.json` 及其引用的 raw snapshots 为准。较早独立 probe 的 318 条 stale / 92 条 fully-contained 与之后统一运行的 317 / 93 不同，符合实时业务数据更新；不得混用为同一时点统计。

## ETL 实现与落地约束

`adapters/usgs.py`、`adapters/wildfire.py` 均实现 `collect(client, now)`，只通过共享 client 抓取官方数据，返回标准事件及运行完整性。USGS 使用稳定 event ID，保留官方 aliases、震级类型、深度 km、MMI、审核状态与 ShakeMap 产品版本。M2.5 past-day 滚动窗口退出不能代表事件结束，另外应建设 catalog/backfill 及已跟踪事件 detail 的后续复查，以免大于 24 小时的灾害从雷达消失。

NIFC 先获取 ID 快照，再分批读取指定 objectIds，避免 offset 分页时新增/删除造成跳页。请求到的 ID 缺失或 ArcGIS transfer-limit 标记都会使快照 incomplete。实测一次 200 IDs 加字段列表触发 HTTP 404；改为 incident 每批 50、perimeter 每批 25 后成功。每次 IDs 捕获后新建事件会在下一次定时运行出现。

IRWIN ID 去花括号并统一大小写连接点和边界。属性保留 null；负面积、超出 0–100 的 containment 清洗为 unknown 并标记异常。日期由毫秒 epoch 转为 UTC ISO8601。`outSR=4326` 统一坐标系；origin county code 不转换为影响 county code。perimeter 优先保留官方 Polygon/MultiPolygon；缺失/异常保留 point，并明确未知范围。暂时重复身份选择最新记录并附质量标记；perimeter-only 的事件通过 attr 字段保留，避免 inner join 丢失灾情。

prototype 为验证完整数据，仍会全量请求当前 perimeters；以本次大小推算，每 5 分钟全量抓取约 10.8 GB/天响应，**这不是建议的生产带宽预算**。正式实现应先查 objectIds、修改时间与必要属性，下载 changed geometry；每日做一次完整 reconciliation，S3 内容寻址去重原始包，业务库保留当前版本与 revision history。若 upstream 不支持可靠的统一变更 watermark，就保留小型全量属性快照，再 diff 决定需要取哪些几何。不能仅依赖 service lastEditDate 当所有 geometry 的观测时间。

## 测试与尚未完成的生产能力

16 项针对语义和故障的测试通过，包括地震类型过滤、无伪造范围、非有限数值、ShakeMap 删除优先级、网格声明列序及单位、网格外 unknown、截断 XML、optional enrichment 失败仍保留地震、IRWIN 连接、缺 perimeter、stale 数据、contained 不等于 out、未来 fire-out 时间不提前标成结束、RX 排除、perimeter-only 保留、ID 缺失、ArcGIS HTTP 200 error object、perimeter feed 故障。USGS 的第三坐标为深度 km，归一化后放入 depth_km 并使用二维地理坐标，避免被 GIS 当成米制海拔。

本子模块未执行真实投保组合地址匹配、美国边界精确裁剪、GIS 拓扑修复、跨日界线处理、地址到房屋真实 footprint 的匹配、损伤模型、moratorium 签批或后端 bind 拦截。它验证官方免费数据可获取、字段可清洗、快照完整性可检测、未知状态能显式传递；不能将此解释为生产保险决策已经认证。
