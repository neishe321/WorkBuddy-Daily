import importlib.util
import unittest
from pathlib import Path


SOURCE_PATH = Path(__file__).resolve().parents[1] / "workbuddy_intl_daily.py"
SPEC = importlib.util.spec_from_file_location("workbuddy_intl_daily_test", SOURCE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Response:
    def __init__(self, status_code, payload, text=""):
        self.status_code = status_code
        self.payload = payload
        self.text = text

    def json(self):
        return self.payload

    def close(self):
        return None


class InternationalBehaviorTests(unittest.TestCase):
    def test_keepalive_403_is_model_permission_not_account_restriction(self):
        response = Response(403, {"code": 403, "msg": "model forbidden"})

        class FakeSession:
            def __init__(self):
                self.trust_env = True

            def post(self, *args, **kwargs):
                return response

        old_session = MODULE.requests.Session
        old_chain = MODULE.model_chain
        MODULE.requests.Session = FakeSession
        MODULE.model_chain = lambda: ["free-model"]
        try:
            ok, note = MODULE.keepalive("token", "uid")
        finally:
            MODULE.requests.Session = old_session
            MODULE.model_chain = old_chain
        self.assertFalse(ok)
        self.assertIn("非账号风控", note)

    def test_keepalive_11140_still_raises_account_restricted(self):
        response = Response(403, {"code": 11140, "msg": "request illegal"})

        class FakeSession:
            def __init__(self):
                self.trust_env = True

            def post(self, *args, **kwargs):
                return response

        old_session = MODULE.requests.Session
        old_chain = MODULE.model_chain
        MODULE.requests.Session = FakeSession
        MODULE.model_chain = lambda: ["free-model"]
        try:
            with self.assertRaises(MODULE.AccountRestricted):
                MODULE.keepalive("token", "uid")
        finally:
            MODULE.requests.Session = old_session
            MODULE.model_chain = old_chain

    def test_preflight_stops_on_11140_before_refresh_or_writes(self):
        MODULE.post_json = lambda *args, **kwargs: Response(
            403, {"code": 11140, "msg": "request illegal"})
        with self.assertRaises(MODULE.AccountRestricted):
            MODULE.preflight_restricted("token", "uid")

    def test_plain_401_is_left_for_normal_refresh_path(self):
        MODULE.post_json = lambda *args, **kwargs: Response(
            401, {"code": 401, "msg": "token expired"})
        response, payload = MODULE.preflight_restricted("token", "uid")
        self.assertEqual(401, response.status_code)
        self.assertEqual(401, payload["code"])

    def test_claim_label_distinguishes_restricted_from_expired(self):
        self.assertIn("账号受限", MODULE.claim_label(11140, ""))
        self.assertIn("登录状态失效", MODULE.claim_label(401, ""))


if __name__ == "__main__":
    unittest.main()
