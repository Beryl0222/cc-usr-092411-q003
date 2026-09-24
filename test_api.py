"""HTTP 端到端测试：完整跑通采集 → 判定 → 复核 → 告知 → 复测 → 回潮链路。"""

import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import service
from service import Handler

NOW = 1_700_000_000
APP_ID = "com.example.news"
SDK_ID = "shake-sdk-9"

def call(method, url, body=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"} if data else {}
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)


def ad(ad_id, placement="splash"):
    return {"event_id": f"e-{ad_id}-shown", "seq": 1, "type": "ad_shown",
            "occurred_at": NOW + 100, "payload": {"ad_id": ad_id, "placement": placement}}


def close(ad_id, seq, after, size, reader=True):
    return {"event_id": f"e-{ad_id}-close-{seq}", "seq": seq, "type": "close_affordance",
            "occurred_at": NOW + 100 + after,
            "payload": {"ad_id": ad_id, "present": True, "visible_after_seconds": after,
                        "touch_target_dp": size, "screen_reader_actionable": reader}}


def jump(ad_id, seq, at, trigger, sdk=None, **extra):
    payload = {"ad_id": ad_id, "trigger": trigger, "target_url": "https://shop.example/p"}
    if sdk:
        payload["sdk"] = {"id": sdk, "name": "摇一摇SDK"}
    payload.update(extra)
    return {"event_id": f"e-{ad_id}-jump-{seq}", "seq": seq, "type": "jump",
            "occurred_at": at, "payload": payload}


def event(event_id, seq=1, type="ad_shown", at=NOW + 100, payload=None):
    return {"event_id": event_id, "seq": seq, "type": type,
            "occurred_at": at, "payload": {} if payload is None else payload}


class IdempotencyApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        service.reset_store()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        service.reset_store()
        self.post("/admin/regulations", 201,
                  {"version": "v2025.1", "effective_at": 0, "params": {"rectification_days": 10}})
        self.post("/devices", 201,
                  {"device_id": "dev-A", "model": "Pixel 6", "os_version": "Android 12"})
        self.post("/builds", 201, {
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1001, "version_name": "8.1.0"})

    def post(self, path, expected, body):
        status, payload = call("POST", self.base + path, body)
        self.assertEqual(status, expected, payload)
        return payload

    def get(self, path, expected=200):
        status, payload = call("GET", self.base + path)
        self.assertEqual(status, expected, payload)
        return payload

    def _task(self, device="dev-A", track="normal"):
        return self.post("/tasks", 201,
                         {"build_id": f"{APP_ID}:1001", "device_id": device, "track": track})

    def _ingest(self, tid, events):
        return self.post(f"/tasks/{tid}/events", 202, {"events": events})

    def test_mixed_batch_new_replay_conflict_reports_per_row(self):
        task = self._task()["task_id"]
        self._ingest(task, [event("c1", payload={"ad_id": "a1", "placement": "splash"})])
        batch = [
            event("new-1", seq=2, type="gesture"),
            event("c1", payload={"ad_id": "a1", "placement": "splash"}),       # 完全重放
            event("c1", payload={"ad_id": "a1", "placement": "lockscreen"}),   # 冲突
        ]
        res = self._ingest(task, batch)
        self.assertEqual(res["accepted"], ["new-1"])
        self.assertEqual(res["duplicates"], ["c1"])
        rows = {r["event_id"]: r for r in res["results"]}
        self.assertEqual(rows["new-1"]["status"], "accepted")
        self.assertEqual(rows["c1"]["status"], "conflict")
        self.assertIn("payload.placement", rows["c1"]["differing_fields"])
        conflict = res["conflicts"][0]
        self.assertEqual(conflict["first"]["payload"]["placement"], "splash")
        self.assertEqual(conflict["conflicting"]["payload"]["placement"], "lockscreen")

        # 任务报告可定位冲突编号与差异字段，首次证据未被覆盖
        report = self.get(f"/tasks/{task}")
        self.assertEqual(report["event_count"], 2)
        self.assertEqual([c["event_id"] for c in report["conflicts"]], ["c1"])
        self.assertIn("payload.placement", report["conflicts"][0]["differing_fields"])

    def test_field_reordering_is_duplicate(self):
        task = self._task()["task_id"]
        original = event("e1", payload={"ad_id": "a1", "placement": "splash",
                                        "advertiser": {"id": "brand-x"}})
        reordered = {
            "payload": {"advertiser": {"id": "brand-x"}, "placement": "splash", "ad_id": "a1"},
            "occurred_at": NOW + 100, "type": "ad_shown", "seq": 1, "event_id": "e1",
        }
        self._ingest(task, [original])
        again = self._ingest(task, [reordered])
        self.assertEqual(again["duplicates"], ["e1"])
        self.assertEqual(again["conflicts"], [])
        self.assertEqual(again["accepted"], [])

    def test_same_id_in_other_task_is_independent(self):
        t1 = self._task()["task_id"]
        self.post("/devices", 201,
                  {"device_id": "dev-B", "model": "Pixel 8", "os_version": "Android 14"})
        t2 = self._task(device="dev-B")["task_id"]
        self._ingest(t1, [event("dup", payload={"ad_id": "a1"})])
        res = self._ingest(t2, [event("dup", payload={"ad_id": "a1", "placement": "feed"})])
        self.assertEqual(res["results"][0]["status"], "accepted")  # 跨任务是新事件
        self.assertEqual(self.get(f"/tasks/{t1}")["conflicts"], [])

    def test_conflict_after_completion_is_not_late_and_late_still_appends(self):
        task = self._task()["task_id"]
        self._ingest(task, [ad("a1"), close("a1", 2, after=5, size=36)])
        self.post(f"/tasks/{task}/complete", 200, {})
        report_before = self.get(f"/tasks/{task}")

        conflict = self._ingest(task, [ad("a1", placement="lockscreen")])
        self.assertEqual(conflict["results"][0]["status"], "conflict")
        self.assertNotIn("assessment", conflict)  # 冲突不触发重新判定
        self.assertNotIn("late_arrivals", conflict)

        report_after = self.get(f"/tasks/{task}")
        self.assertEqual(report_after["event_count"], report_before["event_count"])
        self.assertEqual(len(report_after["findings"]), len(report_before["findings"]))
        self.assertEqual([c["event_id"] for c in report_after["conflicts"]], ["e-a1-shown"])

        # 真正迟到新事件仍按原规则追加
        late = self._ingest(task, [jump("a1", 9, NOW + 300, "auto", sdk=SDK_ID)])
        self.assertEqual(late["accepted"], ["e-a1-jump-9"])
        self.assertTrue(late["late_arrivals"])
        self.assertIn("assessment", late)

    def test_concurrent_identical_submissions_accepted_exactly_once(self):
        task = self._task()["task_id"]
        ev = event("race", payload={"ad_id": "a1", "placement": "splash"})
        outcomes = self._run_concurrently(lambda _i: call("POST", f"{self.base}/tasks/{task}/events",
                                                          {"events": [ev]}), 8)
        statuses = [s for s, _ in outcomes]
        self.assertEqual(set(statuses), {202})
        accepted = [b for _, b in outcomes if b["accepted"] == ["race"]]
        duplicates = [b for _, b in outcomes if b["duplicates"] == ["race"]]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(duplicates), 7)
        # 证据只落一份，任务索引一致
        self.assertEqual(self.get(f"/tasks/{task}")["event_count"], 1)

    def test_concurrent_divergent_same_id_one_wins_others_conflict(self):
        task = self._task()["task_id"]
        variants = [
            event("race2", payload={"ad_id": "a1", "placement": "splash"}),
            event("race2", payload={"ad_id": "a1", "placement": "lockscreen"}),
            event("race2", payload={"ad_id": "a1", "placement": "feed"}),
        ]
        outcomes = self._run_concurrently(
            lambda i: call("POST", f"{self.base}/tasks/{task}/events",
                           {"events": [variants[i]]}), len(variants))
        self.assertEqual({s for s, _ in outcomes}, {202})
        accepted = [b for _, b in outcomes if b["accepted"] == ["race2"]]
        conflicts = [b for _, b in outcomes if b["results"][0]["status"] == "conflict"]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(conflicts), 2)
        # 只有一条进入任务索引，首次证据保留；冲突可定位编号与差异字段
        report = self.get(f"/tasks/{task}")
        self.assertEqual(report["event_count"], 1)
        record = report["conflicts"][0]
        self.assertEqual(record["event_id"], "race2")
        self.assertIn("payload.placement", record["differing_fields"])

    def test_conflict_persists_across_restart_with_first_fingerprint(self):
        fd, path = tempfile.mkstemp(prefix="lab-", suffix=".json")
        os.close(fd)
        os.unlink(path)
        try:
            service.reset_store(path)
            self.post("/admin/regulations", 201, {"version": "v2025.1", "effective_at": 0})
            self.post("/devices", 201,
                      {"device_id": "dev-A", "model": "Pixel 6", "os_version": "Android 12"})
            self.post("/builds", 201, {
                "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
                "version_code": 1001})
            task = self._task()["task_id"]
            self._ingest(task, [event("persist", payload={"ad_id": "a1", "placement": "splash"})])
            self._ingest(task, [event("persist", payload={"ad_id": "a1", "placement": "feed"})])

            service.reset_store(path)  # 进程恢复
            dup = self._ingest(task, [event("persist", payload={"ad_id": "a1",
                                                                "placement": "splash"})])
            self.assertEqual(dup["duplicates"], ["persist"])  # 沿用首次指纹判重
            again = self._ingest(task, [event("persist",
                                              payload={"ad_id": "a1", "placement": "feed"})])
            self.assertEqual(again["results"][0]["status"], "conflict")
            report = self.get(f"/tasks/{task}")
            self.assertEqual(report["conflicts"][0]["first"]["payload"]["placement"], "splash")
        finally:
            if os.path.exists(path):
                os.unlink(path)

    @staticmethod
    def _run_concurrently(fn, count):
        barrier = threading.Barrier(count)
        results = []

        def worker(i):
            barrier.wait()
            results.append(fn(i))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        return results


class ApiFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        service.reset_store()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        service.reset_store()

    def _seed(self):
        self.post("/admin/regulations", 201, {
            "version": "v2025.1", "effective_at": 0, "params": {"rectification_days": 10}})
        self.post("/admin/scripts", 201,
                  {"script_id": "ad-trip", "version": "1.0", "created_at": NOW})
        self.post("/devices", 201,
                  {"device_id": "dev-A", "model": "Pixel 6", "os_version": "Android 12"})
        self.post("/devices", 201,
                  {"device_id": "dev-B", "model": "Pixel 8", "os_version": "Android 14"})
        self.post("/builds", 201, {
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1001, "version_name": "8.1.0"})

    def post(self, path, expected, body):
        status, payload = call("POST", self.base + path, body)
        self.assertEqual(status, expected, payload)
        return payload

    def get(self, path, expected=200):
        status, payload = call("GET", self.base + path)
        self.assertEqual(status, expected, payload)
        return payload

    def _create_task(self, device, track):
        return self.post("/tasks", 201,
                         {"build_id": f"{APP_ID}:1001", "device_id": device, "track": track})

    def test_full_enforcement_flow_over_http(self):
        self._seed()

        # 三条轨迹 + 设备 B 的并存任务
        normal = self._create_task("dev-A", "normal")
        reader = self._create_task("dev-A", "screen_reader")
        elder = self._create_task("dev-A", "elderly")
        normal_b = self._create_task("dev-B", "normal")
        for task in (normal, reader, elder, normal_b):
            self.assertEqual(task["regulation_version"], "v2025.1")
            self.assertEqual(task["script"], {"script_id": "ad-trip", "version": "1.0"})

        # 普通轨迹：关闭入口迟到 + SDK 自动跳转
        batch = [ad("a1"), close("a1", 2, after=5, size=36),
                 jump("a1", 3, NOW + 106, "auto", sdk=SDK_ID)]
        accepted = self.post(f"/tasks/{normal['task_id']}/events", 202, {"events": batch})
        self.assertEqual(len(accepted["accepted"]), 3)
        retried = self.post(f"/tasks/{normal['task_id']}/events", 202, {"events": batch})
        self.assertEqual(retried["accepted"], [])
        self.assertEqual(len(retried["duplicates"]), 3)  # 重传统一去重

        # 读屏轨迹：关闭入口不可聚焦
        self.post(f"/tasks/{reader['task_id']}/events", 202, {"events": [
            ad("a2"), close("a2", 2, after=1, size=48, reader=False)]})
        # 老人轨迹：点区不达标
        self.post(f"/tasks/{elder['task_id']}/events", 202, {"events": [
            ad("a3"), close("a3", 2, after=1, size=48)]})
        # 设备 B：合规
        self.post(f"/tasks/{normal_b['task_id']}/events", 202, {"events": [
            ad("b1"), close("b1", 2, after=1, size=48)]})
        for task in (normal, reader, elder, normal_b):
            self.post(f"/tasks/{task['task_id']}/complete", 200, {})

        report = self.get(f"/builds/{APP_ID}:1001/report")
        by_track = {(r["device_id"], r["track"]): r for r in report["tracks"]}
        self.assertEqual(by_track[("dev-A", "normal")]["suspected"], 2)
        self.assertEqual(by_track[("dev-A", "screen_reader")]["suspected"], 1)
        self.assertEqual(by_track[("dev-A", "elderly")]["suspected"], 1)
        self.assertEqual(by_track[("dev-B", "normal")]["suspected"], 0)

        # 复核：确认应用侧关闭路径问题，驳回 SDK 跳转问题
        normal_report = self.get(f"/tasks/{normal['task_id']}")
        close_finding = next(
            f for f in normal_report["findings"] if f["rule_id"] == "R-CLOSE-001")
        jump_finding = next(
            f for f in normal_report["findings"] if f["rule_id"] == "R-JUMP-001")
        self.assertEqual(close_finding["status"], "suspected")  # 自动规则只标涉嫌
        self.post(f"/findings/{close_finding['finding_id']}/review", 200, {
            "decision": "confirmed", "reviewer": "复核员乙", "comment": "关闭路径不可用"})
        self.post(f"/findings/{jump_finding['finding_id']}/review", 200, {
            "decision": "dismissed", "reviewer": "复核员乙", "comment": "确有后台返回异常"})

        # 未确认不得出告知材料
        status, body = call("POST", f"{self.base}/subjects/sdk/{SDK_ID}/notices",
                            {"issued_by": "承办人甲"})
        self.assertEqual(status, 404)

        notice = self.post(f"/subjects/app/{APP_ID}/notices", 201,
                           {"issued_by": "承办人甲"})
        self.assertEqual(len(notice["findings"]), 1)
        self.assertEqual(notice["rectification_days"], 10)
        evidence_types = {e["type"] for e in notice["findings"][0]["evidence"]}
        self.assertEqual(evidence_types, {"ad_shown", "close_affordance"})
        self.assertEqual(notice["regulation_versions"], ["v2025.1"])

        # 整改：新构建复测通过，问题时段保留
        self.post("/builds", 201, {
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1002, "version_name": "8.2.0"})
        retest_task = self.post("/tasks", 201, {
            "build_id": f"{APP_ID}:1002", "device_id": "dev-A", "track": "normal"})
        self.post(f"/tasks/{retest_task['task_id']}/events", 202,
                  {"events": [ad("c1"), close("c1", 2, after=1, size=48)]})
        self.post(f"/tasks/{retest_task['task_id']}/complete", 200, {})
        retest = self.post(f"/subjects/app/{APP_ID}/retests", 201,
                           {"task_id": retest_task["task_id"], "by": "复测员丙"})
        self.assertEqual(retest["result"], "passed")

        view = self.get(f"/subjects/app/{APP_ID}")
        self.assertEqual(view["status"], "rectified")
        self.assertEqual(view["relapse_count"], 0)
        self.assertEqual(view["cycles"][0]["problem_period"]["first_observed_at"], NOW + 100)
        self.assertEqual(view["cycles"][0]["notices"][0]["finding_count"], 1)

        # 回潮：1003 版恢复旧行为
        self.post("/builds", 201, {
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1003, "version_name": "8.3.0"})
        relapse_task = self.post("/tasks", 201, {
            "build_id": f"{APP_ID}:1003", "device_id": "dev-A", "track": "elderly"})
        self.post(f"/tasks/{relapse_task['task_id']}/events", 202,
                  {"events": [ad("d1"), close("d1", 2, after=6, size=40)]})
        self.post(f"/tasks/{relapse_task['task_id']}/complete", 200, {})
        relapse_report = self.get(f"/tasks/{relapse_task['task_id']}")
        relapse_finding = relapse_report["findings"][0]
        self.post(f"/findings/{relapse_finding['finding_id']}/review", 200,
                  {"decision": "confirmed", "reviewer": "复核员乙"})

        view = self.get(f"/subjects/app/{APP_ID}")
        self.assertEqual(view["status"], "open")
        self.assertEqual(view["relapse_count"], 1)
        self.assertEqual(len(view["cycles"]), 2)
        self.assertEqual(view["current_cycle_seq"], 2)
        # 旧周期完整保留，旧构建证据仍可查
        self.assertEqual(view["cycles"][0]["status"], "rectified")
        old = self.get(f"/tasks/{normal['task_id']}")
        self.assertEqual(old["build"]["version_code"], 1001)

        # 非法输入与未知路由
        status, body = call("POST", f"{self.base}/tasks",
                            {"build_id": "missing", "device_id": "dev-A", "track": "normal"})
        self.assertEqual(status, 404)
        status, _ = call("GET", f"{self.base}/unknown")
        self.assertEqual(status, 404)

    def test_snapshot_file_persistence(self):
        fd, path = tempfile.mkstemp(prefix="lab-", suffix=".json")
        os.close(fd)
        os.unlink(path)
        try:
            service.reset_store(path)
            self.post("/admin/regulations", 201,
                      {"version": "v2025.1", "effective_at": 0})
            self.post("/devices", 201,
                      {"device_id": "dev-A", "model": "Pixel 6", "os_version": "Android 12"})
            # 用同一文件重建仓储，证据不丢
            service.reset_store(path)
            status, _ = call("POST", f"{self.base}/devices",
                             {"device_id": "dev-A", "model": "Pixel 6",
                              "os_version": "Android 12"})
            self.assertEqual(status, 400)  # 设备已从快照恢复，重复登记被拒
        finally:
            if os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    unittest.main()
