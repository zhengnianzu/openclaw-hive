"""错误分类策略单测 —— 纯函数, 不依赖集群/数据库。

C003 二次分类的准确性是降并发信号的前提: 把 DNS 抖动误判成容量不足会白降并发,
把容量不足误判成可重试会把已经过载的集群继续打满。所以三种 detail 各覆盖一例。
detail 文本取自 sfs_turbo 上的真实 task 日志。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prepare_config import (
    ERROR_CATALOG,
    ERROR_POLICY,
    POLICY_FAIL_FAST,
    POLICY_RETRY,
    POLICY_TASK_SIDE,
    POLICY_THROTTLE,
    classify_c003,
    error_max_retries,
    error_policy,
)


def test_classify_c003_real_details():
    """三种真实 detail 文本(实采自生产日志)。"""
    cases = [
        # 实例 2610211021-ZhangYihao2610211020-a6a9: 9467/10000 全是这个
        ("no_cluster_matches_request", POLICY_THROTTLE),
        # 实例 2611022050-c008450182611022049-aa0a: 1688/1688 全是这个
        ("[Errno -3] Temporary failure in name resolution", POLICY_RETRY),
        ("Sandbox pod sandbox network setup failed: PodInitializing", POLICY_RETRY),
        ("Sandbox is running", POLICY_RETRY),
    ]
    for detail, expected in cases:
        got = classify_c003(detail)
        assert got == expected, f"{detail!r}: expected {expected}, got {got}"
    print("OK classify_c003: 4 real production details classified correctly")


def test_classify_c003_unknown_is_conservative():
    """认不出来时保守归 throttle —— C003 的压倒性多数是容量不足。"""
    for detail in ("", "something brand new from the controlplane", None):
        assert classify_c003(detail) == POLICY_THROTTLE, detail
    print("OK classify_c003: unknown/empty detail falls back to throttle")


def test_error_policy_routes_c003_by_detail():
    assert error_policy("C003", "no_cluster_matches_request") == POLICY_THROTTLE
    assert error_policy("C003", "Temporary failure in name resolution") == POLICY_RETRY
    print("OK error_policy: C003 routed by detail, not by code alone")


def test_error_policy_buckets():
    expected = {
        # 可重试
        "C002": POLICY_RETRY, "S004": POLICY_RETRY, "S005": POLICY_RETRY,
        "S006": POLICY_RETRY, "S007": POLICY_RETRY, "S009": POLICY_RETRY,
        "T007": POLICY_RETRY,
        # 容量压力
        "S001": POLICY_THROTTLE, "S002": POLICY_THROTTLE,
        # 配置错误
        "C001": POLICY_FAIL_FAST, "C004": POLICY_FAIL_FAST, "S003": POLICY_FAIL_FAST,
        # 任务侧(含 S008 —— 它虽是 S 前缀但语义是任务侧兜底)
        "S008": POLICY_TASK_SIDE, "T001": POLICY_TASK_SIDE, "T002": POLICY_TASK_SIDE,
        "T004": POLICY_TASK_SIDE, "T006": POLICY_TASK_SIDE, "T009": POLICY_TASK_SIDE,
        "T010": POLICY_TASK_SIDE, "X999": POLICY_TASK_SIDE,
    }
    for code, want in expected.items():
        got = error_policy(code)
        assert got == want, f"{code}: expected {want}, got {got}"
    print(f"OK error_policy: {len(expected)} codes bucketed as designed")


def test_every_catalog_code_has_policy():
    """ERROR_CATALOG 里每个码都必须有策略, 否则新增错误码会静默落进 task_side。"""
    missing = [c for c in ERROR_CATALOG if c not in ERROR_POLICY]
    assert not missing, f"codes without policy: {missing}"
    extra = [c for c in ERROR_POLICY if c not in ERROR_CATALOG]
    assert not extra, f"policy for unknown codes: {extra}"
    print(f"OK coverage: all {len(ERROR_CATALOG)} catalog codes have a policy, no orphans")


def test_unknown_code_is_task_side():
    """未登记的码按 task_side 处理: 不重试、不降并发, 最保守。"""
    assert error_policy("ZZZ9") == POLICY_TASK_SIDE
    assert error_max_retries("ZZZ9") == 0
    print("OK unknown code -> task_side, 0 retries")


def test_max_retries():
    assert error_max_retries("S005") == 3
    assert error_max_retries("T007") == 1      # 超时重试成本高, 只给一次
    assert error_max_retries("S008") == 0      # 任务侧不重试
    assert error_max_retries("S003") == 0      # 配置错误不重试
    print("OK max_retries: S005=3, T007=1, task-side/fail-fast=0")


def main():
    test_classify_c003_real_details()
    test_classify_c003_unknown_is_conservative()
    test_error_policy_routes_c003_by_detail()
    test_error_policy_buckets()
    test_every_catalog_code_has_policy()
    test_unknown_code_is_task_side()
    test_max_retries()
    print("\nall error-policy tests passed")


if __name__ == "__main__":
    main()
