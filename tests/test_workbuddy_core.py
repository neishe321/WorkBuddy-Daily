import ast
import contextlib
import hashlib
import io
import json
import math
import os
import re
import tempfile
import time
import unittest
import uuid
from pathlib import Path

import requests


SOURCE_PATH = Path(__file__).resolve().parents[1] / "workbuddy_daily.py"
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
FUNCTIONS = {
    node.name: node
    for node in TREE.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
}


def load_functions(*names, **globals_):
    namespace = {"__builtins__": __builtins__, **globals_}
    body = [FUNCTIONS[name] for name in names]
    module = ast.Module(body=body, type_ignores=[])
    exec(compile(module, str(SOURCE_PATH), "exec"), namespace)
    return namespace


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload if payload is not None else {}

    def json(self):
        return self.payload


class FakeStreamResponse(FakeResponse):
    def __init__(self, status_code=200, payload=None, lines=None):
        super().__init__(status_code, payload)
        self.lines = list(lines or [])

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_lines(self, decode_unicode=False):
        yield from self.lines


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)


class CoreBehaviorTests(unittest.TestCase):
    def test_claim_ready_prefers_claimable_rows_and_correct_platform(self):
        desktop_tasks = {
            "code": 0,
            "data": {"tasks": [
                {"task_code": "desktop_ready", "accept_status": "accepted",
                 "progress": {"current": 1, "target": 1}},
                {"task_code": "fallback_done", "accept_status": "completed",
                 "progress": {}},
                {"task_code": "already_done", "accept_status": "claimed",
                 "claimed": True, "progress": {"current": 1, "target": 1}},
            ]},
        }
        mp_tasks = {
            "code": 0,
            "data": {"tasks": [
                {"task_code": "desktop_ready", "accept_status": "accepted",
                 "progress": {"current": 0, "target": 1}},
                {"task_code": "mp_ready", "accept_status": "accepted",
                 "progress": {"current": 2, "target": 2}},
            ]},
        }

        class Session:
            def __init__(self):
                self.posts = []

            def get(self, url, **kwargs):
                headers = kwargs.get("headers")
                return FakeResponse(payload=mp_tasks if headers else desktop_tasks)

            def post(self, url, **kwargs):
                self.posts.append((url, kwargs.get("headers")))
                return FakeResponse(payload={
                    "code": 0,
                    "data": {"credit": 10, "energy": 1},
                })

        sleep_calls = []
        module = load_functions(
            "_api_code_ok", "_claim_response_ok", "claim", "_mp_claim",
            "claim_ready_tasks",
            _as_int=lambda value, default=0: int(value or default),
            BASE="https://example.test",
            MP_HEADER={"X-Client-Platform": "miniprogram"},
            MP_TASK_CODES=set(),
            time=type("Time", (), {"sleep": staticmethod(sleep_calls.append)}),
        )
        session = Session()
        logs = []
        module["claim_ready_tasks"](session, logs.append)

        claimed_urls = [url.rsplit("/", 2)[-2] for url, _ in session.posts]
        self.assertEqual({"desktop_ready", "fallback_done", "mp_ready"}, set(claimed_urls))
        self.assertEqual(3, len(session.posts))
        self.assertEqual(
            ["desktop_ready", "fallback_done", "mp_ready"],
            sorted(claimed_urls),
        )
        self.assertIn("达标补领完成", " ".join(logs))
        empty_ok, _, _ = module["_claim_response_ok"](
            FakeResponse(payload={}), {})
        self.assertFalse(empty_ok)

    def test_sign_checks_status_before_claim_and_does_not_fake_empty_success(self):
        calls = []

        def api_retry(session, method, url, **kwargs):
            calls.append(url)
            if url.endswith("checkin-activity-status"):
                return FakeResponse(payload={
                    "code": 0,
                    "data": {"active": True, "today_checked_in": False},
                })
            return FakeResponse(payload={"code": 0, "data": {}})

        module = load_functions(
            "_api_code_ok", "t_sign",
            api_retry=api_retry,
            BASE="https://example.test",
            is_enterprise_denial=lambda status, payload: False,
            _checkin_status=lambda session: {"today_checked_in": False},
            _checkin_tail=lambda session: "（签到连签2天）",
            t_daily_activity=lambda *args: calls.append("daily-activity"),
        )
        logs = []
        module["t_sign"](object(), "u1", "nick", logs.append)
        self.assertEqual(1, sum("checkin-activity-status" in c for c in calls))
        self.assertIn("daily-checkin", " ".join(calls))
        self.assertIn("签到未确认", " ".join(logs))
        self.assertNotIn("签到成功", " ".join(logs))

    def test_sign_10001_requires_fresh_checked_in_state(self):
        fresh_states = [{}, {"today_checked_in": True}]

        def api_retry(session, method, url, **kwargs):
            if url.endswith("checkin-activity-status"):
                return FakeResponse(payload={
                    "code": 0,
                    "data": {"active": True, "today_checked_in": False},
                })
            return FakeResponse(payload={
                "code": 10001,
                "msg": "operation rejected",
            })

        module = load_functions(
            "_api_code_ok", "t_sign",
            api_retry=api_retry,
            BASE="https://example.test",
            is_enterprise_denial=lambda status, payload: False,
            _checkin_status=lambda session: fresh_states.pop(0),
            _checkin_tail=lambda session: "",
            t_daily_activity=lambda *args: None,
        )
        first_logs = []
        module["t_sign"](object(), "u1", "nick", first_logs.append)
        self.assertIn("未确认", " ".join(first_logs))

        second_logs = []
        module["t_sign"](object(), "u1", "nick", second_logs.append)
        self.assertIn("状态回读确认今日已签", " ".join(second_logs))

    def test_travel_claim_uses_record_id_and_real_reward(self):
        response_queue = [
            FakeResponse(payload={"code": 0, "data": {"has_buddy": True}}),
            FakeResponse(payload={
                "code": 0,
                "data": {
                    "state": "arrived",
                    "record_id": "record-42",
                    "daily_limit_reached": True,
                },
            }),
            FakeResponse(payload={
                "code": 0,
                "data": {"reward_credit": 30},
            }),
        ]
        session = FakeSession(response_queue)
        module = load_functions(
            "_api_code_ok", "_dig", "t_travel", BASE="https://example.test")
        logs = []
        module["t_travel"](session, "u1", "nick", logs.append)
        claim_call = [call for call in session.calls
                      if call[1].endswith("/buddy/travel/claim")][0]
        self.assertEqual({"record_id": "record-42"}, claim_call[2]["json"])
        self.assertIn("+30积分", " ".join(logs))

    def test_webchat_success_stops_and_failure_deletes(self):
        module = load_functions(
            "_web_conversation_limit_hit", "_web_conversation_action",
            "_web_conversation_prune", "_web_conversation_release",
            "_web_conversation_create", "webchat2",
            BASE="https://example.test",
            WEB_CONVERSATIONS_PATH="/console/webchat/conversations",
            WEB_CONVERSATION_ACTIVE_STATUSES=("working", "creating", "pending"),
            WEB_CONVERSATION_PRUNE_KEEP=8,
            requests=requests, json=json, uuid=uuid,
        )

        class Session:
            def __init__(self, chat_status, lines):
                self.chat_status = chat_status
                self.lines = lines
                self.headers = {}
                self.calls = []
                self.create_count = 0

            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if url.endswith("/console/webchat/conversations"):
                    self.create_count += 1
                    return FakeResponse(
                        payload={"code": 0, "data": {"conversationId": "conv-1"}})
                if url.endswith("/chat/completions"):
                    return FakeStreamResponse(self.chat_status, lines=self.lines)
                return FakeResponse(payload={"code": 0, "data": {}})

        success = Session(
            200,
            ['data: {"id":"cmb-1","choices":[{"delta":{"content":"OK"}}]}',
             "data: [DONE]"],
        )
        conv_id, text, message_id = module["webchat2"](
            success, "activity", "你好")
        self.assertEqual(("conv-1", "OK", "cmb-1"), (conv_id, text, message_id))
        urls = [url for url, _ in success.calls]
        self.assertTrue(any(url.endswith("/conv-1/stop") for url in urls))
        self.assertFalse(any(url.endswith("/conv-1/delete") for url in urls))

        failed = Session(500, [])
        module["webchat2"](failed, "activity", "你好")
        urls = [url for url, _ in failed.calls]
        self.assertTrue(any(url.endswith("/conv-1/stop") for url in urls))
        self.assertTrue(any(url.endswith("/conv-1/delete") for url in urls))

    def test_web_conversation_limit_prunes_and_retries_create(self):
        class Session:
            def __init__(self):
                self.create_count = 0
                self.stops = []

            def get(self, url, **kwargs):
                rows = [{
                    "id": "old-%02d" % i,
                    "name": "activity-%08x" % i,
                    "status": "working",
                    "createdAt": "2026-10-22T10:%02d:00+08:00" % i,
                } for i in range(10)]
                return FakeResponse(payload={"data": {"conversations": rows}})

            def post(self, url, **kwargs):
                if url.endswith("/conversations"):
                    self.create_count += 1
                    if self.create_count == 1:
                        return FakeResponse(
                            429, {"code": 10105,
                                  "msg": "maximum number of active conversations(10)"})
                    return FakeResponse(
                        payload={"code": 0, "data": {"conversationId": "new-conv"}})
                if url.endswith("/stop"):
                    self.stops.append(url)
                    return FakeResponse(payload={"code": 0, "data": {}})
                return FakeResponse(payload={"code": 0, "data": {}})

        module = load_functions(
            "_web_conversation_limit_hit", "_web_conversation_action",
            "_web_conversation_prune", "_web_conversation_create",
            BASE="https://example.test",
            WEB_CONVERSATIONS_PATH="/console/webchat/conversations",
            WEB_CONVERSATION_ACTIVE_STATUSES=("working", "creating", "pending"),
            WEB_CONVERSATION_PRUNE_KEEP=8,
            requests=requests, json=json, uuid=uuid,
        )
        session = Session()
        conv_id = module["_web_conversation_create"](session, "activity")
        self.assertEqual("new-conv", conv_id)
        self.assertEqual(2, session.create_count)
        self.assertEqual(2, len(session.stops))
        self.assertTrue(session.stops[0].endswith("/old-00/stop"))

    def test_makeup_candidates_use_server_calendar_and_exclude_made_up_dates(self):
        module = load_functions("_dig", "_makeup_candidates", re=re, math=math)
        streak_body = {
            "code": 0,
            "data": {
                "launch_date": "2026-10-10",
                "streak": {"makeup_dates": ["2026-10-19", "2026-10-20"]},
            },
        }
        heatmap_body = {
            "code": 0,
            "data": {
                "today": {"date": "2026-10-21"},
                "cells": [
                    {"date": "2026-09-30", "score": 0},
                    {"date": "2026-10-18", "score": 0},
                    {"date": "2026-10-19", "score": 0},
                    {"date": "2026-10-20", "score": 0},
                    {"date": "2026-10-21", "score": 0},
                ],
            },
        }
        candidates = module["_makeup_candidates"](streak_body, heatmap_body)
        self.assertEqual(["2026-10-18"], candidates)

    def test_compensation_local_guard_prevents_repeated_claims(self):
        class Session:
            def __init__(self):
                self.claim_calls = 0

            def post(self, url, **kwargs):
                if url.endswith("/compensation-status"):
                    return FakeResponse(payload={
                        "code": 0,
                        "data": {
                            "active": True,
                            "claimed": False,
                            "credit_num": 500,
                            "end_time": "2026-10-22 23:59:59",
                        },
                    })
                self.claim_calls += 1
                return FakeResponse(payload={"code": 0, "data": {}})

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "benefit-state.json")
            module = load_functions(
                "_as_int", "_api_code_ok", "_benefit_state_path",
                "_benefit_state_load",
                "_benefit_state_save", "_benefit_guard_key",
                "_benefit_guard_get", "_benefit_guard_mark",
                "_benefit_status", "_benefit_claim",
                "_benefit_actual_credit", "t_compensation",
                os=os, json=json, time=time, re=re, hashlib=hashlib,
                CBDOMAIN="https://example.test",
                BENEFIT_STATE_FILE=state_path,
                api_retry=lambda session, method, url, **kwargs: session.post(url),
                is_enterprise_denial=lambda status, payload: False,
            )
            session = Session()
            logs = []
            module["t_compensation"](session, "uid-1", logs.append)
            module["t_compensation"](session, "uid-1", logs.append)

            self.assertEqual(1, session.claim_calls)
            self.assertIn("未提供实际到账额", " ".join(logs))
            self.assertIn("本地防重", " ".join(logs))

    def test_event_ids_use_official_shapes_without_project_prefixes(self):
        module = load_functions(
            "_new_conversation_id", "_new_message_id", "mp_chat_event",
            uuid=uuid, time=time,
        )
        conv_id = module["_new_conversation_id"]()
        message_id = module["_new_message_id"]()
        self.assertRegex(
            conv_id,
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        )
        self.assertRegex(message_id, r"^[0-9a-f]{32}$")
        self.assertLess(abs(int(message_id[:12], 16) - int(time.time() * 1000)), 5000)

        event = module["mp_chat_event"]("uid", "nick", conv_id)
        self.assertEqual(conv_id, event["conversationId"])
        self.assertRegex(event["requestId"], r"^[0-9a-f]{32}$")
        self.assertEqual(event["requestId"], event["traceId"])
        self.assertEqual(event["requestId"], event["rootRequestId"])
        self.assertEqual("msg-" + event["requestId"][-8:], event["messageId"])
        encoded = json.dumps(event, ensure_ascii=False)
        for prefix in ("wb2api", "wbmp", "wb-daily", "fp-"):
            self.assertNotIn(prefix, encoded)

    def test_desktop_fingerprint_matches_cn_current_shape(self):
        module = load_functions(
            "derive_id", "desktop_fingerprint",
            hashlib=hashlib, time=time,
            DESKTOP_RELEASE_CN=1792029498676,
            DESKTOP_COMMIT_CN="db0e9b51b7fa0b383f41f5b0b83dd442c8557918",
            DESKTOP_VERSION_CN="5.8.3",
        )
        fp = module["desktop_fingerprint"]("uid-1", "nick")
        self.assertEqual("5.8.3", fp["ideVersion"])
        self.assertEqual("5.8.3", fp["extVersion"])
        self.assertEqual("10.0.28000", fp["osVersion"])
        self.assertEqual(8, fp["cpuCores"])
        self.assertEqual(16, fp["memorySize"])
        self.assertRegex(fp["qimei36"], r"^[0-9a-f]{36}$")

    def test_report_desktop_headers_use_host_domain_and_message_id(self):
        captured = {}

        class Response:
            status_code = 200

        class Session:
            trust_env = True

            def post(self, url, **kwargs):
                captured["url"] = url
                captured["headers"] = kwargs["headers"]
                captured["json"] = kwargs["json"]
                return Response()

        requests_module = type("Requests", (), {"Session": Session})
        module = load_functions(
            "_new_message_id", "derive_id", "desktop_fingerprint",
            "report_desktop_events",
            hashlib=hashlib, time=time, uuid=uuid, requests=requests_module,
            DESKTOP_RELEASE_CN=1792029498676,
            DESKTOP_COMMIT_CN="db0e9b51b7fa0b383f41f5b0b83dd442c8557918",
            DESKTOP_VERSION_CN="5.8.3",
            DESKTOP_UA="WorkBuddy/5.8.3 WorkBuddy/5.8.3 CLI/2.161.0",
            DESKTOP_BASE="https://copilot.tencent.com",
        )
        source = type("Source", (), {
            "headers": {"Authorization": "Bearer test"},
        })()
        status = module["report_desktop_events"](
            source, "uid-1", "nick", [{"eventCode": "chat_request_send"}])
        self.assertEqual(200, status)
        self.assertEqual("https://copilot.tencent.com/v2/report", captured["url"])
        self.assertEqual("copilot.tencent.com", captured["headers"]["X-Domain"])
        self.assertRegex(captured["headers"]["X-Request-ID"], r"^[0-9a-f]{32}$")

    def test_heatmap_today_score_reads_server_date(self):
        class Session:
            def get(self, url, **kwargs):
                return FakeResponse(payload={
                    "code": 0,
                    "data": {
                        "today": {"date": "2026-10-22"},
                        "cells": [
                            {"date": "2026-10-21", "score": 4},
                            {"date": "2026-10-22", "score": 12},
                        ],
                    },
                })

        module = load_functions(
            "_heatmap_today_score",
            BASE="https://example.test",
            beijing_today=lambda: __import__("datetime").date(2026, 10, 22),
        )
        self.assertEqual(12, module["_heatmap_today_score"](Session()))

    def test_daily_activity_requires_heatmap_and_uses_chat_fallback(self):
        calls = {"chat": 0}
        scores = [0, 12]

        def heatmap_score(session):
            return scores.pop(0)

        def webchat2(session, kind, prompt):
            calls["chat"] += 1
            return "conversation-id", "OK", "message-id"

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "activity-state.json")
            old_state = os.environ.get("WORKBUDDY_ACTIVITY_STATE")
            os.environ["WORKBUDDY_ACTIVITY_STATE"] = state_path
            try:
                module = load_functions(
                    "_new_conversation_id", "_new_message_id",
                    "t_daily_activity",
                    os=os, json=json, time=time, uuid=uuid,
                    CBDOMAIN="https://www.codebuddy.cn",
                    WORKBUDDY_UA_CN="WorkBuddy/5.8.3 WorkBuddy/5.8.3 CLI/2.161.0",
                    beijing_today=lambda: __import__("datetime").date(2026, 10, 22),
                    _env_enabled=lambda name, default=True: True,
                    _heatmap_today_score=heatmap_score,
                    report=lambda *args, **kwargs: 200,
                    webchat2=webchat2,
                )
                logs = []
                session = type("Session", (), {
                    "get": lambda self, url, **kwargs: FakeResponse(payload={
                        "data": {"streak": {"days": 1}},
                    }),
                })()
                module["t_daily_activity"](session, "uid-1", "nick", logs.append)
            finally:
                if old_state is None:
                    os.environ.pop("WORKBUDDY_ACTIVITY_STATE", None)
                else:
                    os.environ["WORKBUDDY_ACTIVITY_STATE"] = old_state

            self.assertEqual(1, calls["chat"])
            self.assertTrue(Path(state_path).exists())
            self.assertIn("真实对话兜底成功", " ".join(logs))

    def test_daily_activity_does_not_persist_unconfirmed_score(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "activity-state.json")
            old_state = os.environ.get("WORKBUDDY_ACTIVITY_STATE")
            os.environ["WORKBUDDY_ACTIVITY_STATE"] = state_path
            try:
                fake_time = type("Time", (), {
                    "time": staticmethod(time.time),
                    "sleep": staticmethod(lambda seconds: None),
                })
                module = load_functions(
                    "_new_conversation_id", "_new_message_id",
                    "t_daily_activity",
                    os=os, json=json, time=fake_time, uuid=uuid,
                    CBDOMAIN="https://www.codebuddy.cn",
                    WORKBUDDY_UA_CN="WorkBuddy/5.8.3 WorkBuddy/5.8.3 CLI/2.161.0",
                    beijing_today=lambda: __import__("datetime").date(2026, 10, 22),
                    _env_enabled=lambda name, default=True: True,
                    _heatmap_today_score=lambda session: 0,
                    report=lambda *args, **kwargs: 200,
                    webchat2=lambda *args: ("conv", "OK", "msg"),
                )
                logs = []
                session = type("Session", (), {})()
                module["t_daily_activity"](session, "uid-2", "nick", logs.append)
            finally:
                if old_state is None:
                    os.environ.pop("WORKBUDDY_ACTIVITY_STATE", None)
                else:
                    os.environ["WORKBUDDY_ACTIVITY_STATE"] = old_state

            self.assertFalse(Path(state_path).exists())
            self.assertIn("下轮继续重试", " ".join(logs))

    def test_serverchan_sctp_url_and_title_normalization(self):
        posted = {}

        class Response:
            def json(self):
                return {"code": 0}

        class Session:
            trust_env = True

            def post(self, url, **kwargs):
                posted["url"] = url
                posted["data"] = kwargs["data"]
                return Response()

        requests_module = type("Requests", (), {"Session": Session})
        os.environ["SERVERCHAN_SENDKEY"] = "sctp123tabcdef"
        try:
            module = load_functions(
                "_serverchan_notify",
                os=os, re=re, requests=requests_module,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                ok = module["_serverchan_notify"]("标题\n第二行", "正文")
        finally:
            os.environ.pop("SERVERCHAN_SENDKEY", None)

        self.assertTrue(ok)
        self.assertEqual(
            "https://123.push.ft07.com/send/sctp123tabcdef.send",
            posted["url"],
        )
        self.assertEqual("标题 第二行", posted["data"]["title"])


if __name__ == "__main__":
    unittest.main()
