# Property Hazard Alert — 官方数据可行性 ETL

面向美国 commercial property underwriting。Python 3.11+，仅用标准库；无付费API key，无AWS部署依赖。设计与实测日期为2026-09-14。

先读 [中文方案设计](docs/方案设计.md) 和 [数据验证结果](docs/数据验证.md)。架构是设计，adapter和本地数据处理是真实实现；没有启用任何停保规则。

## 运行

在本目录执行（PowerShell）：

```powershell
$env:PYTHONPATH = "$PWD\src"
$env:HAZARD_USER_AGENT = 'YourCompanyHazardService/1.0 (真实运维联系地址)'
python -m hazard_alert.cli --sources nws usgs nifc nhc nwps --data-dir data
python -m unittest discover -s tests -v
```


这是单次运行，不会创建本地cron、AWS Scheduler或长期后台进程。网络失败会产生manifest与非零exit code；其他source继续处理。`partial`也返回非零，方便未来调度器发现降级；不应因一个有缺陷的几何而悄悄发布“完全成功”。

默认NWPS是美国东南部区域可行性样本，不是全国。可显式选择：

```powershell
$env:NWPS_GAUGE_IDS = 'LOLT2,MLLA1'
python -m hazard_alert.cli --sources nwps --data-dir data-targeted
```

或设置 `NWPS_BBOX=west,south,east,north`；全国模式为 `NWPS_SCOPE=national`（需先清除指定gauge配置，且本次全国请求曾超时）。不同scope应使用不同data-dir；本原型SQLite按source保存水位，生产必须将source+scope作为发布/健康的独立键，不能让小区域覆盖全国状态。

## 文件与数据流

- `src/hazard_alert/adapters/nws.py`：CAP、产品筛选、update/cancel、zone几何fallback。
- `usgs.py`：地震、detail、ShakeMap grid解析与最近节点采样。
- `wildfire.py`：NIFC ID快照分批抓取、IRWIN join、perimeter与时效。
- `nhc.py`：气旋JSON与KMZ/KML几何，退化/缺时效标记。
- `nwps.py`：区域/指定gauge，观测与预报、单位/sentinel、hydrograph清洗。
- `core.py`：允许的官方HTTPS来源、重试/条件请求、raw哈希、校验和SQLite幂等存储。
- `cli.py`：隔离每个source、写manifest/normalized/quarantine、完整快照发布。
- `docs/production_schema.sql`：未执行的PostGIS/审计结构建议。

`data/raw/{source}/{run_id}/`存原始bytes；扩展名`.bin`，实际类型可在manifest查看（JSON/KMZ/XML等）。`data/normalized/{source}/{run_id}/events.jsonl`是清洗后数据，同目录有manifest和quarantine；`data/reports/*_latest.json`是每源最近尝试；`data/hazards.sqlite`存runs/revisions/events/source_state/supersessions。不要把latest attempt等同last successful snapshot。

SQLite仅发布完整运行的current；partial的记录仍在normalized与revisions中可检查。显式supersessions保存CAP关系；任何未来查询服务必须同时检查source_state、missing_from_latest_snapshot、时间与supersessions，不能直接把events表每行都显示成正在发生。原始和大几何文件保留在工作区并由此子项目.gitignore排除，未上传远程。

## 验证与复现

`tests/`使用标准库unittest，包含语义边界和已捕获官方样本回放。少数真实样本回放在样本目录缺失时skip；合成fixture的边界测试不依赖网络。`scripts/verify_delivery.py`重新核对最新manifest的raw哈希、规范化记录数、SQLite重复载入幂等，并生成`data/reports/delivery_verification.json`和中文数据验证摘要。

本轮NHC会因一个零面积初始风圈而报partial，14条可用记录仍可审阅；这是明确保留的不完整标记。USGS/NHC保留全球/海盆候选，不是美国灾害计数。不能据几何point、cone、gauge或数据缺失直接自动停保/恢复承保。

生产补项、polling、frontend API、backend gate、feature flag与政策边界详见设计文档。该原型未执行空间拓扑修复、portfolio匹配、SQL迁移、长期回补或自动承保决策。

## GitHub 版本与固定验证证据

这是独立的 hazard alert 项目。完整方案、工程代码、48项测试和验证摘要均包含在仓库中。
本仓库公开提供方案、报告和可复用代码。没有部署AWS，也没有修改任何承保规则。

`data/reports/` 是2026-09-14本地验证的历史摘要。大型原始响应、完整normalized输出、HTTP缓存和SQLite未上传；manifest中的相对raw_path只说明原采集结构，不表示该文件已随仓库提供。
完整哈希核对命令 `python scripts/verify_delivery.py` 需要先在本机运行ETL生成各source的raw、normalized和latest manifest；新的实时数据可能与历史结果不同。无需重新取数即可运行全部48项语义测试。

`tests/fixtures/` 仅包含约533 KB的官方公开历史样本及来源元数据，用于NHC空间产品和NWPS水文回放；这些是固定样本，不是当前灾情。
环境配置示例见 `.env.example`。程序从进程环境变量读取配置，不会自动读取该模板；无需任何API key。不要将真实联系方式、tokens或本机.env提交到Git。

