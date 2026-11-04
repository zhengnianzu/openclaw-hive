# -*- coding: utf-8 -*-
"""申请 API Key 的错误透出测试（unittest，无 pytest 依赖）。

背景：tokenfly 网关用「HTTP 200 + success:false + message」表示失败，
`resp.raise_for_status()` 拦不住，原先一律被包成一句笼统的
「上游服务未返回 api_key」。实测 key 名字超长时上游明确回了
`{"message":"Token name is too long"}`，这句现成的答案被吞掉，
用户看不出该把名字改短。

这里固化：上游给了原因就原样透出；没给原因/空响应体才退回兜底说明。

运行：cd platform && python -m unittest discover -s test -p "test_key_gen_errors.py" -v
"""
import asyncio
import os
import sys
import unittest

import httpx
from fastapi import HTTPException

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.routers import instances as inst  # noqa: E402

USER = {"username": "tester", "role": "admin"}
BASE = "http://115.120.127.45:8082"


class _FakeClient:
    """假的 httpx.AsyncClient：固定返回一个响应，不出网。"""

    def __init__(self, body, status=200, text=None):
        self._body, self._status, self._text = body, status, text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def _resp(self, *a, **kw):
        req = httpx.Request("GET", "http://fake/api/invite")
        if self._text is not None:
            return httpx.Response(self._status, text=self._text, request=req)
        return httpx.Response(self._status, json=self._body, request=req)

    get = post = _resp


class KeyGenErrorTest(unittest.TestCase):
    def setUp(self):
        self._orig = inst.httpx.AsyncClient
        self.addCleanup(setattr, inst.httpx, "AsyncClient", self._orig)

    def _call(self, body, status=200, text=None, name="n"):
        inst.httpx.AsyncClient = lambda *a, **kw: _FakeClient(body, status, text)
        return asyncio.run(
            inst.generate_api_key(
                invite_code="self_host", name=name, base_url=BASE, current_user=USER
            )
        )

    def _detail(self, **kw):
        with self.assertRaises(HTTPException) as cm:
            self._call(**kw)
        return cm.exception.status_code, cm.exception.detail

    # ---- 核心回归：200 + success:false 要透出 message ----

    def test_name_too_long_message_is_surfaced(self):
        """实测过的真实失败体：这句必须原样到用户眼前。"""
        status, detail = self._detail(
            body={"message": "Token name is too long", "success": False}
        )
        self.assertEqual(status, 502)
        self.assertIn("Token name is too long", detail)

    def test_invalid_invite_code_message_is_surfaced(self):
        msg = "Group xyz does not exist or is not available to you"
        _, detail = self._detail(body={"message": msg, "success": False})
        self.assertIn(msg, detail)

    def test_alternate_message_fields(self):
        for field in ("msg", "error", "detail", "error_msg"):
            with self.subTest(field=field):
                _, detail = self._detail(body={field: "upstream says no"})
                self.assertIn("upstream says no", detail)

    def test_nested_error_object(self):
        _, detail = self._detail(body={"error": {"message": "nested reason"}})
        self.assertIn("nested reason", detail)

    def test_no_message_falls_back(self):
        """上游没给原因时不能是空白提示。"""
        _, detail = self._detail(body={"success": False})
        self.assertIn("未返回 api_key", detail)

    # ---- 非 2xx 分支 ----

    def test_non_2xx_with_message(self):
        _, detail = self._detail(body={"message": "Token not provided"}, status=403)
        self.assertIn("Token not provided", detail)

    def test_429_empty_body_explains_rate_limit(self):
        """限流只给状态码、响应体为空——不能弹一个没有原因的空报错。"""
        _, detail = self._detail(body=None, status=429, text="")
        self.assertIn("429", detail)
        self.assertIn("限流", detail)

    def test_non_2xx_empty_body_mentions_status(self):
        _, detail = self._detail(body=None, status=500, text="")
        self.assertIn("500", detail)

    # ---- 成功路径不受影响 ----

    def test_success_returns_key(self):
        key = "sk-" + "a" * 48
        self.assertEqual(self._call(body={"api_key": key}), {"api_key": key})

    def test_missing_invite_code_is_400(self):
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(
                inst.generate_api_key(
                    invite_code="", name="n", base_url=BASE, current_user=USER
                )
            )
        self.assertEqual(cm.exception.status_code, 400)


class UpstreamMessageTest(unittest.TestCase):
    def test_priority_and_blank_handling(self):
        self.assertEqual(inst._upstream_message({"message": "a", "msg": "b"}), "a")
        self.assertEqual(inst._upstream_message({"message": "   "}), "")
        self.assertEqual(inst._upstream_message({}), "")
        self.assertEqual(inst._upstream_message(None), "")
        self.assertEqual(inst._upstream_message("not a dict"), "")

    def test_strips_whitespace(self):
        self.assertEqual(inst._upstream_message({"message": "  x  "}), "x")


if __name__ == "__main__":
    unittest.main()
