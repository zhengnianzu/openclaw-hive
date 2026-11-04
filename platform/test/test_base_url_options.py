# -*- coding: utf-8 -*-
"""模型 Base URL 下拉候选聚合测试（unittest，无 pytest 依赖）。

覆盖 proxy.build_base_url_options 的几条关键行为：
  1. 归一化去重 —— 同一台代理的 4 种写法（裸/尾斜杠//v1）在下拉里只出现一次；
  2. 排序稳定 —— 按用量降序，同用量按地址字典序（否则刷新一次顺序就变）；
  3. 未登记代理不丢 —— 实测 115.120.87.159 用过 400+ 次却没登记代理名，
     只列已登记代理会把它从界面抹掉；
  4. 已登记但没用过的代理补默认端口进候选。

运行：cd platform && python -m unittest discover -s test -p "test_base_url_options.py" -v
"""
import os
import sys
import unittest

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.services import proxy as proxy_service  # noqa: E402


class TestBuildBaseUrlOptions(unittest.TestCase):
    def test_normalizes_and_dedups_same_proxy(self):
        """同一台代理的四种写法必须并成一条，used 累加。"""
        urls = [
            "http://115.120.62.121:8082",
            "http://115.120.62.121:8082/",
            "http://115.120.62.121:8082/v1",
            "http://115.120.62.121:8082/v1/",
        ]
        opts = proxy_service.build_base_url_options(urls)
        self.assertEqual(len(opts), 1)
        self.assertEqual(opts[0]["value"], "http://115.120.62.121:8082")
        self.assertEqual(opts[0]["used"], 4)

    def test_sorted_by_usage_desc(self):
        urls = ["http://a:8082"] + ["http://b:8082"] * 3 + ["http://c:8082"] * 2
        opts = proxy_service.build_base_url_options(urls)
        self.assertEqual([o["value"] for o in opts],
                         ["http://b:8082", "http://c:8082", "http://a:8082"])

    def test_ties_broken_by_address_for_stable_order(self):
        """同用量时按地址排序：否则每次请求下拉顺序都可能不同。"""
        urls = ["http://z:8082", "http://a:8082", "http://m:8082"]
        first = proxy_service.build_base_url_options(urls)
        second = proxy_service.build_base_url_options(list(reversed(urls)))
        self.assertEqual([o["value"] for o in first], [o["value"] for o in second])
        self.assertEqual(first[0]["value"], "http://a:8082")

    def test_attaches_proxy_name_when_registered(self):
        opts = proxy_service.build_base_url_options(
            ["http://115.33.106.57:8082"], {"115.33.106.57": "proxy-004-latest"}
        )
        self.assertEqual(opts[0]["proxy_name"], "proxy-004-latest")
        self.assertEqual(opts[0]["host"], "115.33.106.57")

    def test_unregistered_proxy_is_kept_with_empty_name(self):
        """回归点：未登记的高频端点不能被过滤掉，只是没名字。"""
        opts = proxy_service.build_base_url_options(
            ["http://115.120.87.159:8082"] * 443, {"115.33.106.57": "proxy-004"}
        )
        self.assertEqual(len(opts), 1)
        self.assertEqual(opts[0]["value"], "http://115.120.87.159:8082")
        self.assertEqual(opts[0]["proxy_name"], "")
        self.assertEqual(opts[0]["used"], 443)

    def test_registered_but_unused_proxy_added_with_default_port(self):
        opts = proxy_service.build_base_url_options(
            [], proxy_map={"10.0.0.1": "new-proxy"}, proxy_ips={"10.0.0.1": "new-proxy"}
        )
        self.assertEqual(len(opts), 1)
        self.assertEqual(opts[0]["value"], "http://10.0.0.1:8082")
        self.assertEqual(opts[0]["proxy_name"], "new-proxy")
        self.assertEqual(opts[0]["used"], 0)

    def test_registered_proxy_already_used_is_not_duplicated(self):
        """已登记代理在历史里用过非默认端口时，不该再补一条 8082。"""
        opts = proxy_service.build_base_url_options(
            ["http://10.0.0.1:3000"],
            proxy_map={"10.0.0.1": "p"}, proxy_ips={"10.0.0.1": "p"},
        )
        self.assertEqual([o["value"] for o in opts], ["http://10.0.0.1:3000"])

    def test_non_ip_gateway_survives(self):
        """外部网关（域名、无端口）也要能当候选。"""
        url = "https://apigw-dgg-b0.huawei.com/api/pangu"
        opts = proxy_service.build_base_url_options([url])
        self.assertEqual(opts[0]["value"], url)
        self.assertEqual(opts[0]["host"], "apigw-dgg-b0.huawei.com")

    def test_ignores_empty_and_none(self):
        opts = proxy_service.build_base_url_options([None, "", "   ", "http://a:8082"])
        self.assertEqual([o["value"] for o in opts], ["http://a:8082"])

    def test_empty_input(self):
        self.assertEqual(proxy_service.build_base_url_options([]), [])
        self.assertEqual(proxy_service.build_base_url_options(None), [])


class TestV1SuffixHandling(unittest.TestCase):
    """pi / dsh 的 anthropic-messages 要去掉 /v1。

    原实现写的是 `base_url -= "v1"`（字符串不支持 -=），会抛 TypeError
    导致创建实例直接失败。这里固化修好后的语义。
    """

    @staticmethod
    def _strip_v1(base_url: str) -> str:
        # 与 instances.py 里 pi/dsh 分支修正后的表达式一致
        if base_url.endswith("/v1"):
            return base_url[:-3].rstrip("/")
        return base_url

    def test_strips_v1(self):
        self.assertEqual(self._strip_v1("http://x:8082/v1"), "http://x:8082")

    def test_leaves_bare_url_untouched(self):
        self.assertEqual(self._strip_v1("http://x:8082"), "http://x:8082")

    def test_does_not_touch_midpath_v1(self):
        url = "http://x:8082/api/v1/infer"
        self.assertEqual(self._strip_v1(url), url)

    def test_source_has_no_string_minus_assign(self):
        """守住回归：源码里不该再出现 `-= "v1"`。"""
        path = os.path.join(PLATFORM_DIR, "api", "routers", "instances.py")
        with open(path, encoding="utf-8") as f:
            self.assertNotIn('-= "v1"', f.read())


if __name__ == "__main__":
    unittest.main()
