import datetime
import importlib.util
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "credits_report.py"
SPEC = importlib.util.spec_from_file_location("credits_report", MODULE_PATH)
credits_report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(credits_report)


class CreditsReportTests(unittest.TestCase):
    def test_personal_query_body_matches_client_pagination_and_expiry_window(self):
        now = datetime.datetime(2026, 10, 29, 12, 34, 56)
        self.assertEqual({
            "PageNumber": 1,
            "PageSize": 100,
            "ProductCode": "p_tcaca",
            "Status": [0, 3],
            "PackageEndTimeRangeBegin": "2026-10-29 12:34:56",
            "PackageEndTimeRangeEnd": "2127-10-05 12:34:56",
        }, credits_report.credits_query_body(now))

    def test_fetch_accounts_posts_query_body_and_keeps_legacy_envelope(self):
        captured = {}

        def fake_post(url, token, extra_headers=None, body=None):
            captured.update({
                "url": url,
                "token": token,
                "extra_headers": extra_headers,
                "body": body,
            })
            return 200, {
                "code": 0,
                "data": {
                    "Response": {
                        "Data": {
                            "Accounts": [{"CapacityType": 4, "CycleCapacityRemain": 500}],
                        },
                    },
                },
            }

        original_post = credits_report.post
        credits_report.post = fake_post
        try:
            now = datetime.datetime(2026, 10, 29, 12, 34, 56)
            accounts = credits_report.fetch_accounts(
                "https://copilot.tencent.com", "token-1", now)
        finally:
            credits_report.post = original_post

        self.assertEqual("https://copilot.tencent.com/billing/meter/get-user-resource",
                         captured["url"])
        self.assertEqual("token-1", captured["token"])
        self.assertIsNone(captured["extra_headers"])
        self.assertEqual(credits_report.credits_query_body(now), captured["body"])
        self.assertEqual(500, accounts[0]["CycleCapacityRemain"])

    def test_expiry_line_uses_cycle_family_and_monthly_type(self):
        now = datetime.datetime(2026, 10, 29, 12, 0, 0)
        accounts = [
            {
                "CapacityType": 4,
                "CycleCapacityRemain": 500,
                "CycleCapacityRemainPrecise": "500.00",
                "CapacityRemain": 999,
                "CycleEndTime": "2026-10-31 23:59:59",
            },
            {
                "CapacityType": 4,
                "CycleCapacityRemain": 0,
                "CapacityRemain": 777,
                "CycleEndTime": "2026-10-31 23:59:59",
            },
            {
                "CapacityType": 4,
                "CapacityRemain": 888,
                "CycleEndTime": "2026-10-31 23:59:59",
            },
            {
                "CapacityType": 1,
                "CycleCapacityRemain": 0,
                "CycleCapacityRemainPrecise": "1696.61",
                "ExpiredTime": "2026-11-12 23:59:59",
            },
            {
                "CapacityType": 1,
                "CycleCapacityRemain": 300,
                "ExpiredTime": "2026-10-01 00:00:00",
            },
        ]
        line, warn = credits_report.expiry_line(accounts, now, days=7)
        self.assertIn("10-31 月包清零 500 分", line)
        self.assertIn("11-12 到期 1696.61 分", line)
        self.assertNotIn("777", line)
        self.assertNotIn("888", line)
        self.assertNotIn("300", line)
        self.assertIn("500 分将在 7 天内失效", warn)

    def test_expiry_line_accepts_old_capacity_remain_shape(self):
        now = datetime.datetime(2026, 10, 29, 12, 0, 0)
        line, _ = credits_report.expiry_line([
            {"CapacityRemain": 42, "CycleEndTime": "2026-11-05 23:59:59"},
        ], now, days=7)
        self.assertIn("11-05 到期 42 分", line)

    def test_enterprise_usage_parses_snake_camel_and_unlimited(self):
        self.assertEqual({
            "unlimited": False,
            "limit": 2000.0,
            "used": 8.35,
            "remaining": 1991.65,
        }, credits_report.parse_enterprise_usage({
            "code": 0,
            "data": {"limit_num": 2000, "used_num": 8.35},
        }))
        self.assertEqual({
            "unlimited": True,
            "limit": None,
            "used": 12.0,
            "remaining": None,
        }, credits_report.parse_enterprise_usage({
            "code": 0,
            "data": {"data": {"limitNum": -1, "usedNum": 12}},
        }))
        self.assertIsNone(credits_report.parse_enterprise_usage({
            "code": 0,
            "data": {"limit_num": 100},
        }))

    def test_enterprise_usage_falls_back_to_web_path_on_404(self):
        calls = []

        def fake_post(url, token, extra_headers=None, body=None):
            calls.append((url, extra_headers, body))
            if url.endswith("/v2/billing/meter/get-enterprise-user-usage"):
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return 200, {
                "code": 0,
                "data": {"Data": {"limitNum": 1000, "usedNum": 250}},
            }

        original_post = credits_report.post
        credits_report.post = fake_post
        try:
            usage = credits_report.fetch_enterprise_usage(
                "https://copilot.tencent.com", "token-1", "ent-1")
        finally:
            credits_report.post = original_post

        self.assertEqual(750.0, usage["remaining"])
        self.assertEqual([
            "https://copilot.tencent.com/v2/billing/meter/get-enterprise-user-usage",
            "https://copilot.tencent.com/billing/meter/get-enterprise-user-usage",
        ], [call[0] for call in calls])
        self.assertEqual("ent-1", calls[0][1]["X-Enterprise-Id"])
        self.assertEqual({}, calls[0][2])

    def test_report_cn_routes_explicit_enterprise_id_to_read_only_usage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store_path = os.path.join(temp_dir, "wb_refresh_tokens.json")
            with open(store_path, "w", encoding="utf-8") as handle:
                json.dump({
                    "enterprise-user": {
                        "access_token": "token-1",
                        "refresh_token": "rt-1",
                        "enterpriseId": "ent-1",
                    },
                }, handle)

            original_usage = credits_report.fetch_enterprise_usage
            original_accounts = credits_report.fetch_accounts
            original_summary = credits_report.fetch_summary
            personal_calls = []

            def fake_usage(base, token, enterprise_id):
                self.assertEqual("ent-1", enterprise_id)
                return {
                    "unlimited": False,
                    "limit": 1000.0,
                    "used": 250.0,
                    "remaining": 750.0,
                }

            credits_report.fetch_enterprise_usage = fake_usage
            credits_report.fetch_accounts = lambda *args, **kwargs: (
                personal_calls.append("accounts") or [])
            credits_report.fetch_summary = lambda *args, **kwargs: (
                personal_calls.append("summary") or {})
            try:
                body, _ = credits_report.report_cn(store_path, 7)
            finally:
                credits_report.fetch_enterprise_usage = original_usage
                credits_report.fetch_accounts = original_accounts
                credits_report.fetch_summary = original_summary

        self.assertIn("企业积分 剩 750", body)
        self.assertIn("已用 250 / 总额 1000", body)
        self.assertIn("企业账号不支持每日签到", body)
        self.assertEqual([], personal_calls)


if __name__ == "__main__":
    unittest.main()
