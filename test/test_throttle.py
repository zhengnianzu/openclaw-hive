"""ThrottleController 单测 —— 不依赖集群, 纯 asyncio。

重点覆盖计划里点名的回归点:
  1. 容量收紧后在飞数不超过新 limit
  2. limit=0(熔断)时 worker 全阻塞, 探测放行 1 个有效
  3. 阶梯升回不超过初始值; floor 生效
  4. 降 limit 时**不中断**已在跑的任务  ← 最关键
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.throttle import ThrottleController, STATE_NORMAL, STATE_SHRUNK, STATE_PAUSED


def _feed(tc, *, throttle_n=0, ok_n=0):
    """往窗口里喂结果: throttle_n 个容量类失败 + ok_n 个成功。

    注意按「交错」顺序喂, 不是先全失败再全成功 —— 后者会先形成 100% 的瞬时占比
    触发熔断, 测不出想测的降档档位。生产里失败和成功本来就是混着来的。
    """
    total = throttle_n + ok_n
    if total == 0:
        return
    emitted_t = 0
    for i in range(total):
        # 按比例均匀铺开容量类失败
        want_t = round((i + 1) * throttle_n / total)
        if want_t > emitted_t:
            tc.record("任务异常", "C003", "no_cluster_matches_request")
            emitted_t += 1
        else:
            tc.record("任务成功", "", "")


def test_shrink_on_capacity_failures():
    # 90% 容量失败 → 熔断档
    tc = ThrottleController(2000, floor=50, min_samples=10)
    _feed(tc, throttle_n=45, ok_n=5)
    assert tc.state == STATE_PAUSED, tc.state

    # 40% 容量失败 → 降并发一档。cooldown 用默认值(120s): 它是防「连续腰斩」的
    # 关键闸门, 测试里置 0 会让同一批证据反复触发降档(2000 一路掉到 floor)。
    tc2 = ThrottleController(2000, floor=50, min_samples=10)
    _feed(tc2, throttle_n=20, ok_n=30)
    assert tc2.state == STATE_SHRUNK, tc2.state
    assert tc2.limit == 1000, tc2.limit
    print("OK shrink: 2000 -> 1000 @40%; pause @90%")


def test_cooldown_prevents_runaway_shrink():
    """持续高失败率在冷却期内只应降一档, 不能一路腰斩到 floor。"""
    tc = ThrottleController(2000, floor=50, min_samples=10)
    for i in range(500):
        if i % 5 in (0, 1):
            tc.record("任务异常", "C003", "no_cluster_matches_request")
        else:
            tc.record("任务成功", "", "")
    assert tc.limit == 1000, tc.limit
    print(f"OK cooldown: 500 tasks @40% capacity-fail -> single shrink ({tc.limit})")


def test_task_side_failures_do_not_shrink():
    """T 类 / S008 再多也不该降并发 —— 那不是集群的问题。"""
    tc = ThrottleController(2000, floor=50, min_samples=10)
    for _ in range(100):
        tc.record("任务异常", "T004", "AgentExecutionError")
    for _ in range(100):
        tc.record("任务异常", "S008", "main.py")
    assert tc.state == STATE_NORMAL, tc.state
    assert tc.limit == 2000, tc.limit
    print("OK task-side failures (T004/S008) never shrink")


def test_dns_failures_do_not_shrink():
    """C003 的 DNS 变种是 retry 类, 不该被当成容量不足去降并发。"""
    tc = ThrottleController(2000, floor=50, min_samples=10)
    for _ in range(100):
        tc.record("任务异常", "C003", "[Errno -3] Temporary failure in name resolution")
    assert tc.state == STATE_NORMAL, tc.state
    assert tc.limit == 2000, tc.limit
    print("OK C003(DNS) classified retry -> no shrink")


def test_floor_respected():
    """反复降档(每次都跨过冷却期)最终触底于 floor, 不会降到 0 或负数。"""
    tc = ThrottleController(2000, floor=50, min_samples=10, cooldown=0.0)
    import time as _t
    for _ in range(12):
        _feed(tc, throttle_n=20, ok_n=30)
        tc._last_shrink_at = 0.0      # 人为跨过冷却期, 模拟长时间持续容量不足
    assert tc.limit == 50, tc.limit
    print("OK floor respected: limit bottoms at 50")


def test_pause_recovery_resumes_near_prepause():
    """熔断恢复不该直接掉到 floor —— 2000 并发掉到 50 比降并发本身代价更大。

    只喂刚够触发**一次**恢复的成功数: 再多喂会继续阶梯升回(1000→1251→…→2000),
    那是预期行为, 但会盖住「第一步回到哪里」这个要测的点。
    """
    tc = ThrottleController(2000, floor=50, min_samples=10, recover_streak=5)
    _feed(tc, throttle_n=45, ok_n=5)          # 熔断
    assert tc.state == STATE_PAUSED
    assert tc._pre_pause_limit == 2000

    # min_samples=10 决定了要先攒够 10 个样本才会做判定
    for _ in range(10):
        tc.record("任务成功", "", "")
    assert tc.limit == 1000, tc.limit          # 熔断前 2000 的一半
    assert tc.state == STATE_SHRUNK, tc.state
    assert tc.limit > tc._floor, (tc.limit, tc._floor)
    print(f"OK pause recovery: resumed at {tc.limit} (half of pre-pause 2000), not floor 50")


def test_pause_recovery_then_climbs_to_ceiling():
    """恢复后继续持续成功, 应一路阶梯升回配置并发。"""
    tc = ThrottleController(2000, floor=50, min_samples=10, recover_streak=5)
    _feed(tc, throttle_n=45, ok_n=5)
    for _ in range(400):
        tc.record("任务成功", "", "")
    assert tc.limit == 2000, tc.limit
    assert tc.state == STATE_NORMAL, tc.state
    print("OK pause recovery climbs back to ceiling 2000 after sustained success")


def test_floor_not_above_initial():
    """用户故意设 concurrent_num=10 时, 默认 floor=50 不该把并发反向抬上去。"""
    tc = ThrottleController(10, floor=50)
    assert tc.limit == 10, tc.limit
    _feed(tc, throttle_n=40, ok_n=10)
    assert tc.limit <= 10, tc.limit
    print("OK floor clamped to initial when concurrent_num < floor")


def test_grow_never_exceeds_ceiling():
    tc = ThrottleController(100, floor=10, min_samples=10, recover_streak=5)
    _feed(tc, throttle_n=20, ok_n=30)          # 先降到 50
    assert tc.limit == 50, tc.limit
    for _ in range(300):                        # 持续成功 → 阶梯升回
        tc.record("任务成功", "", "")
    assert tc.limit == 100, tc.limit
    assert tc.state == STATE_NORMAL, tc.state
    print("OK grow back to ceiling (100), never beyond")


async def test_gate_enforces_limit():
    """在飞数不得超过 limit。"""
    tc = ThrottleController(4, floor=1)
    peak = 0
    cur = 0
    lock = asyncio.Lock()

    async def job():
        nonlocal peak, cur
        async with tc.slot():
            async with lock:
                cur += 1
                peak = max(peak, cur)
            await asyncio.sleep(0.02)
            async with lock:
                cur -= 1

    await asyncio.gather(*(job() for _ in range(40)))
    assert peak <= 4, peak
    print(f"OK gate: peak in-flight {peak} <= limit 4")


async def test_shrink_does_not_interrupt_running_tasks():
    """最关键的回归点: 降 limit 时已在跑的任务必须跑完, 不被 cancel。"""
    tc = ThrottleController(10, floor=1, min_samples=5)
    finished = []
    started = []

    async def job(i):
        async with tc.slot():
            started.append(i)
            await asyncio.sleep(0.05)      # 模拟任务执行
            finished.append(i)

    tasks = [asyncio.create_task(job(i)) for i in range(10)]
    await asyncio.sleep(0.01)              # 让前一批进入闸门
    in_flight_before = tc.in_flight
    # 此刻把并发砍到 1
    tc.set_ceiling(1)
    await asyncio.gather(*tasks)

    assert in_flight_before > 1, in_flight_before
    assert len(finished) == 10, finished
    assert sorted(finished) == list(range(10))
    print(f"OK shrink mid-flight: {in_flight_before} running tasks all completed "
          f"({len(finished)}/10), none cancelled")


async def test_pause_blocks_then_probes():
    """熔断: limit=0 时全阻塞, 过了探测间隔放行 1 个。"""
    tc = ThrottleController(10, floor=2, min_samples=10, cooldown=0,
                            probe_interval=0.05)
    _feed(tc, throttle_n=45, ok_n=5)       # 90% → 熔断
    assert tc.state == STATE_PAUSED, tc.state

    admitted = []

    async def job(i):
        async with tc.slot():
            admitted.append(i)
            await asyncio.sleep(0.01)

    tasks = [asyncio.create_task(job(i)) for i in range(5)]
    await asyncio.sleep(0.02)
    # 第一个探测是立即放行的(_last_probe_at=0), 所以此刻应恰好 1 个
    assert len(admitted) == 1, admitted
    await asyncio.sleep(0.20)              # 再过几个探测间隔
    assert 1 < len(admitted) <= 5, admitted
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    print(f"OK pause: serialized probes, {len(admitted)} admitted one-at-a-time")


async def test_set_ceiling_raise_is_gradual():
    """手动调大并发不该立刻放开闸门, 应交给状态机逐步升回。"""
    tc = ThrottleController(100, floor=10, min_samples=10)
    _feed(tc, throttle_n=20, ok_n=30)      # 降到 50
    assert tc.limit == 50
    tc.set_ceiling(1000)                   # 上界抬高
    assert tc.limit == 50, tc.limit        # 但有效并发不跳变
    print("OK set_ceiling(up) keeps effective limit; growth stays gradual")


def main():
    test_shrink_on_capacity_failures()
    test_cooldown_prevents_runaway_shrink()
    test_task_side_failures_do_not_shrink()
    test_dns_failures_do_not_shrink()
    test_floor_respected()
    test_pause_recovery_resumes_near_prepause()
    test_pause_recovery_then_climbs_to_ceiling()
    test_floor_not_above_initial()
    test_grow_never_exceeds_ceiling()
    asyncio.run(test_gate_enforces_limit())
    asyncio.run(test_shrink_does_not_interrupt_running_tasks())
    asyncio.run(test_pause_blocks_then_probes())
    asyncio.run(test_set_ceiling_raise_is_gradual())
    print("\nall throttle tests passed")


if __name__ == "__main__":
    main()
