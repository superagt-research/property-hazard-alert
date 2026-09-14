# NHC 与 NWPS：来源、ETL 与实测边界

实测时间：2026-09-14，12:48–12:55 UTC。只调用公开免费接口，没有部署，也没有账户、密钥或商业 API。原始响应与抓取元数据保存在 `data/probes/`；测试明确区分官方实时样本和人工构造的边界案例。

## NHC 热带气旋

入口采用官方 [CurrentStorms.json](https://www.nhc.noaa.gov/CurrentStorms.json)，跟随每个气旋中的版本化产品链接，不猜测 storm 编号。官方提供 [JSON 示例与产品目录](https://www.nhc.noaa.gov/productexamples/) 及 [字段参考](https://www.nhc.noaa.gov/productexamples/NHC_Tropical_Cyclone_Status_JSON_File_Reference.pdf)。本次 current 接口返回 HTTP 200、9,239 bytes、2 个气旋：Norbert（ep142026）、Fifteen-E（ep152026），advisory 均为 2026-09-14 09:00 UTC。这是整个相关海盆的候选事件，不代表美国陆地正在受灾。

`nhc.py` 实现四种产品的下载和 KML/KMZ → GeoJSON：trackCone、forecastTrack、initialWindExtent、forecastWindRadiiGIS，并保留气旋中心点。8 个 KMZ 均为 200。当前样本成功产出 12 条几何记录，加 2 条中心记录；有 1 个初始风圈退化为零面积而被隔离。零面积对应当时 30 kt 的热带低压，不能把这一几何当作真实面积。原始样本 `nhc_sample.json` 另保留官方 2023 示例，标识为历史示例，不能当成当前灾害。

清洗包括：字符串数字转有限数值；坐标采用 WGS84 经度、纬度；时间统一 UTC；风速保留 kt、气压保留 hPa；GeoJSON 保留 polygon holes 和 MultiPolygon；连续重复点清理并闭合 ring；零面积、非法坐标和未修复的跨日界线几何隔离。实测 cone 使用 KML 2.1，而 track/wind 使用 KML 2.2，解析器兼容两个 namespace。KML 的样式、HTML、外部链接不会进入可执行前端内容。

风圈与 cone 应是不同图层、不同证据类型。NHC 的 [图形解释](https://www.nhc.noaa.gov/aboutnhcgraphics.shtml) 说明 cone 表示气旋中心路径的不确定性，灾害影响可以远超 cone；当前风圈也不保证每个覆盖点都经历相应风速。ETL 因而给 cone 加 `cone_is_not_wind_footprint`，初始风圈加 `analyzed_extent_not_ground_observation`，气旋中心本身不作为 property hit。初始风圈的 `evidence=unknown` 表示“分析估计”，不能误读为地面实测。

本次 forecastWindRadiiGIS KMZ 中的 placemark 提供 34 kt 阈值，但没有每个 polygon 的 UTC valid time；ETL 保留几何、阈值与 advisory 时间，将 `effective_at=null` 并标 `forecast_valid_time_unavailable`。这部分通过了地图可行性验证，尚不能支持按未来时段自动停保。若要用于精细触发，需要追加带预测时效字段的 shapefile 或 forecast advisory 解码，并对时间与风阈值作交叉校验。官方 [预报验证说明](https://www.nhc.noaa.gov/verification/verify2.shtml) 与 [2025 产品更新](https://www.nhc.noaa.gov/pdf/NHC_New_Products_Updates_2025.pdf) 确认 34/50 kt 风圈可至 120 小时、64 kt 至 72 小时；不应继续使用旧文档的 48 小时假设。

建议每 5 分钟检查 JSON；按 `advNum + fileUpdateTime + URL` 判断需要拉取的 GIS 产品，生产版应加条件请求与缓存。同一个气旋的 JSON 和 GIS 更新时间不完全一致，应允许短期 partial，不能以新 JSON 中旧 GIS 的缺失解除 alert。官方 [advisory 说明](https://www.nhc.noaa.gov/pdf/NHC_Product_Description.pdf) 的常规发报周期为每 6 小时一次（03/09/15/21 UTC）；5 分钟是建议的发现频率，不是声称官方每 5 分钟发布。特殊公告可能在常规周期外发布。

现有实现保留 surge、wind speed probabilities、arrival time 等产品的 URL/版本作为 metrics 中的扩展入口；本次**没有实现这些产品的空间 ETL**。Storm Surge Watch/Warning 先从 NWS CAP ingest，NHC potential surge 栅格与概率/到达时间图层留作下一阶段。所有 NHC 事件带 `us_boundary_spatial_match_pending`，需要与真正的美国边界、产品地域和 property 点位相交；不能用一个大 bbox 声称已完成美国范围判断。

## NOAA NWPS 河流水位

采用 [NWPS 官方 API](https://api.water.noaa.gov/about/api) 与 [Swagger schema](https://api.water.noaa.gov/nwps/v1/docs/)。这些接口提供 gauge 元数据、当前观测与预报状态，以及完整 stageflow。NWM/HEFS 是另外的模型/实验性服务；本 adapter 没有把模型输出当成官方洪水告警。Gauge point 不是淹没范围，更不是“该 county 每栋楼正在被淹”。

`nwps.py` 支持全国 inventory、bbox 和指定 gauge；默认明确使用 `[-90,24,-75,37]` 的美国东南部**样本范围**，不声称已完成全国收集。此 bbox endpoint 实测 200、2,850,162 bytes、2,710 gauges。观测状态包括 1 个 minor、7 个 action；预报状态有 3 个 minor、11 个 action。实时正例是 MLLA1、minor、2026-09-14 12:00 UTC。另有大量 `obs_not_current`、`fcst_not_current`、`not_defined`、`out_of_service`，均保留在原始响应和 health 计数，不能转成安全状态。Action stage 只产生 `potential` 证据，不声称洪水已经发生。

全国 `/gauges` 两次请求分别超时（50 秒、45 秒），所以本次**没有验证全国 inventory 下载成功**。生产方案宜按 portfolio 相关 basin/region 切片，缓存 metadata，用调度任务独立抓取各 shard，再按 gauge ID 去重，并追踪各 shard 完成度。`complete=true` 只表示指定 query scope 的响应处理完成，不表示全国覆盖。全国模式可通过 `NWPS_SCOPE=national` 显式请求；bbox 用 `NWPS_BBOX=west,south,east,north`；指定站点用 `NWPS_GAUGE_IDS=LOLT2,MLLA1`（优先于 bbox）。

本次 [LOLT2 元数据](https://api.water.noaa.gov/nwps/v1/gauges/LOLT2) 与 [LOLT2 stageflow](https://api.water.noaa.gov/nwps/v1/gauges/LOLT2/stageflow) 均为 200。Stageflow 含 2,788 个 observed 点，forecast 为空；这是有效的缺失预报，不是零流量或没有洪水。一个探索性的 HGXT2 站点请求返回 404；生产 gauge ID 必须从 inventory/portfolio 配置发现，不能猜测。

清洗规则：`-999/-9999/-99999`、非有限数值和公元 0001 年时间 → null；**有效负水位保留**；主/次变量分别保留单位和 PEDTS，不假设 primary 一定是 Stage，也不把 kcfs 当 cfs。Summary 的 `forecast.validTime` 是预报有效时间，不是发报时间，因此 `issued_at`/`source_updated_at` 留空、quality flag 明示。观测 validTime 也不冒充下载/处理时间。若要判断 forecast 新鲜度，必须进一步读取 stageflow 的 forecast issuedTime。

`normalize_stageflow` 已实现 targeted hydrograph 的观测/预报分离、时间与 sentinel 清洗、重复 valid time 按 generated time 取最新修订；它不是默认每 15 分钟全站 hydrograph 抓取。样本小时级读数只代表站点 datum 上的高度，不是房屋积水深度。建议 inventory/summary 每 15 分钟、受影响 portfolio 的 gauge 每 5–15 分钟、metadata 每 24 小时。观测超过 3 小时会降为 unknown，这是可配置的内部 freshness 设计假设，而非 NOAA 官方失效标准。

使用方式：先用 NWS Flood/Flash Flood Warning 做区域提示，NWPS gauge observed/forecast 作证据补充；后续接入有覆盖和适用说明的 FIM/inundation polygon 才能做更精确的 property flood exposure。附近 gauge 或 bbox 命中只支持进一步检查，不直接触发自动 moratorium。

## 验证结果与实现范围

18 个语义测试通过，包括：KML 2.1/2.2、cone 与 wind 区分、零面积与跨日界线隔离、预测风圈不杜撰有效时间、有效负水位、sentinel、观测/预报分开、老观测 unknown、action stage potential、out-of-service、hydrograph 修订去重，以及官方实时 KMZ/hydrograph 回放。测试夹具中的 SYNTHETIC 数据只用于边界测试。

这两个 adapter 只负责获取、规范化和质量标记。它们不会修改 rating、feature flag 或 moratorium，也不把数据源中某条记录消失当作恢复承保授权。
