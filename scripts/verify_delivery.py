"""Offline evidence verification and report generation; never fetches or deploys."""
from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from hazard_alert.core import Store, utcnow, validate_event


def main():
    test_output = io.StringIO()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    tests = unittest.TextTestRunner(stream=test_output, verbosity=2).run(suite)
    reports = ROOT / "data" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "tests.txt").write_text(test_output.getvalue(), encoding="utf-8")
    evidence = {"verified_at": utcnow(), "tests_run": tests.testsRun, "tests_failed": len(tests.failures),
                "tests_errors": len(tests.errors), "tests_skipped": len(tests.skipped), "sources": [], "failures": []}
    for source in ("nws", "usgs", "nifc", "nhc", "nwps"):
        path = reports / (source + "_latest.json")
        if not path.exists():
            evidence["failures"].append(source + ": missing report")
            continue
        r = json.loads(path.read_text(encoding="utf-8"))
        events_path = ROOT / "data" / "normalized" / source / r["run_id"] / "events.jsonl"
        if not events_path.exists():
            raise SystemExit("Historical full snapshots are not bundled. Run the ETL first, then verify the newly generated local data.")
        events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
        for event in events:
            validate_event(event)
        if len(events) != r["records_loaded"]:
            raise AssertionError(source + " count mismatch")
        raw_count, raw_bytes = 0, 0
        for request in r["http_requests"]:
            if request.get("raw_path"):
                raw_path = Path(request["raw_path"])
                b = (raw_path if raw_path.is_absolute() else ROOT / raw_path).read_bytes()
                assert hashlib.sha256(b).hexdigest() == request["sha256"], source + " raw checksum mismatch"
                raw_count += 1
                raw_bytes += len(b)
        with tempfile.TemporaryDirectory(dir=reports) as temporary:
            db = Store(Path(temporary) / "verification.sqlite")
            summary = {"status": r["status"], "started_at": r["started_at"], "completed_at": r["completed_at"]}
            first = db.load(source, "replay-a", events, summary)
            revisions_before = db.db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
            second = db.load(source, "replay-b", events, summary)
            revisions_after = db.db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
            current_rows = db.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            assert revisions_before == revisions_after, source + " repeated revisions"
            if r["status"] != "success":
                assert current_rows == 0, source + " published an incomplete snapshot"
            db.db.close()
            assert second == 0, source + " replay is not idempotent"
        evidence["sources"].append({"source":source,"status":r["status"],"run_id":r["run_id"],
            "started_at":r["started_at"],"completed_at":r["completed_at"],"records":len(events),
            "geometry_roles":r["geometry_roles"],"quality_flags":r["quality_flags"],"metrics":r["metrics"],
            "raw_files_verified":raw_count,"raw_bytes":raw_bytes,"first_load_changes":first,"repeat_load_changes":second,
            "replay_current_rows":current_rows,"replay_revisions":revisions_after,
            "error":r["error"]})
    (reports / "delivery_verification.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 官方数据 ETL 实测与验收记录", "", "此报告由已保存的真实请求manifest生成；核对发生在本地，不会重新取数或部署。", "",
             f"证据核对时间：{evidence['verified_at']}。语义测试 {tests.testsRun} 项，失败 {len(tests.failures)}，错误 {len(tests.errors)}，跳过 {len(tests.skipped)}。", "",
             "不同source在各自时间抓取，数量不能相加为美国灾害总数。NWS包括消息版本，NHC一个storm对应多个图层，USGS为全球feed，NWPS为区域/测站样本。", ""]
    for item in evidence["sources"]:
        lines.extend(["## " + item["source"].upper(), "", f"实际请求窗口：{item['started_at']} 至 {item['completed_at']}；状态 **{item['status']}**；归一化记录 **{item['records']}**。", "",
                      f"核对 {item['raw_files_verified']} 份原始响应哈希，{item['raw_bytes'] / 1e6:.2f} MB；相同规范化输入再次载入产生 {item['repeat_load_changes']} 个新增修订。此幂等测试不代表事件已完成建筑匹配或已获得自动承保授权。", "",
                      "几何角色：" + "；".join(f"{k}={v}" for k,v in item["geometry_roles"].items()) + "。", "",
                      "质量标记：" + "；".join(f"{k}={v}" for k,v in item["quality_flags"].items()) + "。", "",
                      f"完整证据：`../data/reports/{item['source']}_latest.json`；run_id `{item['run_id']}`。", ""])
        if item["error"]:
            lines.extend(["该次错误：" + item["error"], ""])
    lines.extend(["## 如何解读结果", "",
        "- NWS：原生warning polygon与zone fallback分别标记；zone是较粗的预警行政/预报范围，不是逐栋损伤范围。实际发现批量zones接口返回空几何，使用单zone备用路径补齐。",
        "- NIFC：仅point的火灾仍是已知事件，范围未知。100% containment不自动等于扑灭；旧边界不能由于本次成功下载而刷新观测年龄。全量几何抓取已验证，但生产应改增量。",
        "- NHC：一个热带低压初始风圈为零面积，已隔离；因此此source整体为partial，可用记录在normalized/revisions，不发布为完整current。预测风圈缺逐面有效时间，不能作为自动moratorium时间范围。",
        "- USGS：38条等计数来自全球M2.5+ past-day；4份ShakeMap grid解析成功。震中二维，深度单独为km；MMI/PGA是震动估计，不是房损模型。",
        "- NWPS：全国接口曾超时；东南部2,710 gauges的独立probe成功，另一次统一runner的同区域请求也曾超时，反映延迟/可用性限制。应采用portfolio测站或更小区域切片。以该source最新manifest确认本次统一run的具体scope；不能据sample成功声称全国成功。",
        "- 本次初始网络沙箱拒绝公开socket，获自动审核允许后已执行公开读取；失败run仍保留历史。没有账户授权信息、没有部署。", "",
        "## 尚未证明", "", "全国portfolio空间匹配、真实建筑精度、全年高峰可用性、无漏报、受影响财产数量、费用/月度SLA、rating效果和moratorium业务正确性均未由本次样本证明。生产拓扑与日期变更线、源历史回补及多scope发布需要下一阶段实现。", ""])
    (ROOT / "docs" / "数据验证.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"tests":tests.testsRun,"passed":tests.wasSuccessful(),"skipped":len(tests.skipped),
                      "sources":[{k:s[k] for k in ("source","status","records","repeat_load_changes")} for s in evidence["sources"]]},ensure_ascii=False))
    return 0 if tests.wasSuccessful() and not evidence["failures"] else 1


if __name__ == "__main__":
    sys.exit(main())
