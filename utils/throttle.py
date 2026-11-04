"""运行时并发闸门 + 容量熔断 (ThrottleController)。

为什么需要它
------------
一个实例 = 一个 hive.py 进程, 并发 2000 不是 2000 个进程, 而是进程内 2000 个
asyncio worker 协程, 每个协程同一时刻持有 1 个沙箱。`concurrent_num` 在
`main()` 里只读一次就固定了(hive.py), 之后无法从外部调小 —— 于是集群容量耗尽时,
实例会一路把全部任务烧成 C003。实测最坏个例 1688/1688 全损、9467/10000 全损。

为什么不是「砍 worker 数量」
--------------------------
worker 协程在 `asyncio.gather` 里一次性创建, 且退出条件是**队列为空**。中途砍
协程要么得 cancel task(中断正在跑的任务、丢掉沙箱), 要么重构整个 gather 结构。
所以这里换个思路: **worker 数不变, 收紧「在飞沙箱数」**。多余的 worker 自然阻塞
在闸门上等待, 不中断任何正在跑的任务。

标准 `asyncio.Semaphore` 不支持动态改容量(改 _value 是私有行为且唤醒语义不对),
故用 `asyncio.Condition` 守一对 (limit, in_flight) 自己实现。

三档状态机
---------
信号 = 滑动窗口内 **throttle 类失败占比**(集群容量不足 / gateway 起不来)。
注意 C003 必须先经 `classify_c003` 按 detail 二次分类, 否则 DNS 抖动会被误判成
容量不足而白降并发。

    正常      占比 < shrink_ratio   连续成功达阈值则阶梯升回(不超过初始值)
    降并发    占比 >= shrink_ratio  limit 腰斩(2000→1000→…→floor), 带冷却期
    熔断暂停  占比 >= pause_ratio   limit=0, 每 probe_interval 秒探测放行 1 个

熔断这档正是防「全军覆没」的关键: 100% C003 的实例会在前几百个任务内停下来等
集群恢复, 而不是烧完全部任务。因为容量类失败 6.4s 就返回(实测), 窗口能迅速填满,
状态机反应及时; 也因此失败几乎不占配额, 退避等待是安全的。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections import deque


# 默认参数 —— 都可被 config.yaml 的 run_config.throttle 覆盖。
DEFAULT_WINDOW = 200          # 滑动窗口: 最近多少个任务结果参与判定
DEFAULT_MIN_SAMPLES = 30      # 样本不足时不做任何判定, 避免开跑头几个任务就误降
DEFAULT_SHRINK_RATIO = 0.30   # throttle 占比 >= 此值 → 降并发
DEFAULT_PAUSE_RATIO = 0.80    # throttle 占比 >= 此值 → 熔断暂停
DEFAULT_RECOVER_RATIO = 0.15  # throttle 占比 < 此值且连续成功够多 → 阶梯升回
DEFAULT_FLOOR = 50            # 降并发下限, 再差也保留一点吞吐
DEFAULT_COOLDOWN = 120.0      # 两次降档之间的冷却秒数, 防连续腰斩
DEFAULT_PROBE_INTERVAL = 60.0 # 熔断期间探测放行的间隔秒数
DEFAULT_GROW_FACTOR = 1.25    # 升回步长
DEFAULT_RECOVER_STREAK = 20   # 需要连续多少个非 throttle 结果才升回

STATE_NORMAL = "normal"
STATE_SHRUNK = "shrunk"
STATE_PAUSED = "paused"


class ThrottleController:
    """可动态调容量的并发闸门, 按 throttle 类失败占比自动降并发 / 熔断。

    用法:
        throttle = ThrottleController(initial=2000)
        async with throttle.slot():
            result = await task.run(...)
        throttle.record(status, error_code, detail)

    线程/协程安全性: 仅供单个事件循环内的协程使用(hive.py 的 worker 都在同一个
    loop 里), 所有状态变更都在 Condition 的锁内完成。
    """

    def __init__(
        self,
        initial: int,
        *,
        floor: int = DEFAULT_FLOOR,
        window: int = DEFAULT_WINDOW,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        shrink_ratio: float = DEFAULT_SHRINK_RATIO,
        pause_ratio: float = DEFAULT_PAUSE_RATIO,
        recover_ratio: float = DEFAULT_RECOVER_RATIO,
        cooldown: float = DEFAULT_COOLDOWN,
        probe_interval: float = DEFAULT_PROBE_INTERVAL,
        grow_factor: float = DEFAULT_GROW_FACTOR,
        recover_streak: int = DEFAULT_RECOVER_STREAK,
        logger=None,
        state_file: str = "",
    ) -> None:
        initial = max(1, int(initial or 1))
        # floor 不能超过初始并发: 用户故意设 concurrent_num=10 时,
        # 默认 floor=50 不该把并发反向抬到 50。
        self._ceiling = initial          # 上界(= 用户配置的并发), 升回不超过它
        self._limit = initial            # 当前许可数
        self._floor = max(1, min(int(floor), initial))
        self._in_flight = 0

        self._window = max(1, int(window))
        self._min_samples = max(1, int(min_samples))
        self._shrink_ratio = float(shrink_ratio)
        self._pause_ratio = float(pause_ratio)
        self._recover_ratio = float(recover_ratio)
        self._cooldown = float(cooldown)
        self._probe_interval = float(probe_interval)
        self._grow_factor = float(grow_factor)
        self._recover_streak = max(1, int(recover_streak))

        self._logger = logger
        self._state_file = state_file

        # 滑动窗口只存 bool: True = throttle 类失败。用 deque(maxlen) 自动淘汰。
        self._outcomes: deque[bool] = deque(maxlen=self._window)
        self._ok_streak = 0
        self._state = STATE_NORMAL
        self._last_shrink_at = 0.0
        self._last_probe_at = 0.0
        self._paused_at = 0.0
        self._pre_pause_limit = initial

        self._cond = asyncio.Condition()

    # ---- 对外只读属性 ----

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def state(self) -> str:
        return self._state

    @property
    def in_flight(self) -> int:
        return self._in_flight

    # ---- 闸门 ----

    @contextlib.asynccontextmanager
    async def slot(self):
        """占用一个并发槽位; limit 被调小/熔断时在此阻塞, 不中断已在跑的任务。"""
        await self.acquire()
        try:
            yield
        finally:
            await self.release()

    async def acquire(self) -> None:
        async with self._cond:
            while not self._can_admit():
                # 熔断期间的放行条件是**时间到了**, 而 Condition.wait() 只在被
                # notify 时醒。若无限等, 探测任务跑完后 release 的 notify 会因
                # 「间隔还没到」被挡回去, 之后再没有任何事件能唤醒等待者 ——
                # 实例就永久挂死。所以这一档必须带超时轮询。
                timeout = self._wait_timeout()
                if timeout is None:
                    await self._cond.wait()
                else:
                    try:
                        await asyncio.wait_for(self._cond.wait(), timeout)
                    except asyncio.TimeoutError:
                        # 超时后重新进入 while 判定: 此时间隔可能已经满足
                        pass
            self._in_flight += 1
            if self._state == STATE_PAUSED:
                # 熔断期间放出去的这一个是探测请求, 记下时间以便按间隔节流
                self._last_probe_at = time.monotonic()

    def _wait_timeout(self) -> float | None:
        """等待唤醒的超时秒数; None 表示可以无限等(靠 notify 唤醒即可)。

        只有熔断档需要超时 —— 它的放行条件含时间项。返回「距下次可探测还差多久」,
        下限 0.01s 避免 busy-loop。
        """
        if self._state != STATE_PAUSED:
            return None
        remaining = self._probe_interval - (time.monotonic() - self._last_probe_at)
        return max(0.01, remaining)

    async def release(self) -> None:
        async with self._cond:
            self._in_flight = max(0, self._in_flight - 1)
            self._cond.notify_all()

    def _can_admit(self) -> bool:
        """能否再放一个任务进去(必须在 _cond 锁内调用)。"""
        if self._state == STATE_PAUSED:
            # 熔断: 只允许一个在飞的探测任务, 且要等够探测间隔
            if self._in_flight > 0:
                return False
            return (time.monotonic() - self._last_probe_at) >= self._probe_interval
        return self._in_flight < self._limit

    # ---- 记账与状态机 ----

    def record(self, status: str, error_code: str = "", detail: str = "") -> None:
        """登记一个任务结果, 必要时调整并发。

        只有 policy 为 throttle 的失败才算「容量压力」信号 —— 任务侧失败
        (T 类 / S008)再多也不该降并发, 那不是集群的问题。
        """
        # 延迟导入: prepare_config 导入本模块的风险更低, 但仍避免模块级环形依赖
        from prepare_config import POLICY_THROTTLE, error_policy

        is_throttle = bool(error_code) and error_policy(error_code, detail) == POLICY_THROTTLE
        self._outcomes.append(is_throttle)
        if is_throttle:
            self._ok_streak = 0
        else:
            self._ok_streak += 1
        self._reconcile()

    def _throttle_ratio(self) -> float:
        if not self._outcomes:
            return 0.0
        return sum(self._outcomes) / len(self._outcomes)

    def _reconcile(self) -> None:
        """按当前窗口决定升/降/熔断。调用方: record()。

        每次真正做出决策后都会清空窗口(见 _consume_window): 降档的依据是「这一批
        样本里容量类失败占比过高」, 档位降下来之后这批样本就用过了。否则窗口里的
        旧失败会在后续每个 record() 上反复命中阈值, 把 2000 一路腰斩到 floor
        (实测 20 个失败就从 2000 掉到 50), 而冷却期只能拖慢、不能阻止这种误降。
        """
        if len(self._outcomes) < self._min_samples:
            return

        ratio = self._throttle_ratio()
        now = time.monotonic()

        if ratio >= self._pause_ratio:
            self._pause(ratio)
            self._consume_window()
        elif ratio >= self._shrink_ratio:
            # 冷却期内不降档, 但也不清窗口 —— 保留证据, 等冷却结束再判定
            if now - self._last_shrink_at >= self._cooldown:
                self._shrink(ratio, now)
                self._consume_window()
        elif ratio < self._recover_ratio and self._ok_streak >= self._recover_streak:
            self._grow(ratio)
            self._consume_window()

    def _consume_window(self) -> None:
        """清空滑动窗口 —— 一次决策消耗掉它所依据的样本。"""
        self._outcomes.clear()

    def _pause(self, ratio: float) -> None:
        if self._state == STATE_PAUSED:
            return
        self._state = STATE_PAUSED
        self._paused_at = time.monotonic()
        # 记下熔断前的档位: 恢复时回到「它的一半」而不是直接掉到 floor。
        # 并发 2000 的实例因一次集群抖动熔断后, 恢复时跳回 50 会让剩下几万个
        # 任务以 1/40 的速度爬完, 比降并发本身的代价大得多。
        self._pre_pause_limit = self._limit
        # 立刻允许一次探测, 不必再等一个完整间隔
        self._last_probe_at = 0.0
        self._log(
            f"[throttle] PAUSE 容量类失败占比 {ratio:.0%} >= {self._pause_ratio:.0%}, "
            f"暂停领取新任务, 每 {self._probe_interval:.0f}s 探测 1 个"
        )
        self._dump_state()

    def _shrink(self, ratio: float, now: float) -> None:
        if self._limit <= self._floor:
            # 已到下限: 不再降, 但要把状态标出来, 否则看着像「正常」
            if self._state != STATE_SHRUNK:
                self._state = STATE_SHRUNK
                self._dump_state()
            return
        old = self._limit
        self._limit = max(self._floor, self._limit // 2)
        self._state = STATE_SHRUNK
        self._last_shrink_at = now
        self._log(
            f"[throttle] SHRINK 容量类失败占比 {ratio:.0%} >= {self._shrink_ratio:.0%}, "
            f"有效并发 {old} -> {self._limit}"
        )
        self._dump_state()
        self._wake()

    def _grow(self, ratio: float) -> None:
        if self._state == STATE_NORMAL and self._limit >= self._ceiling:
            return
        old = self._limit
        if self._state == STATE_PAUSED:
            # 熔断恢复: 回到熔断前档位的一半(至少 floor), 再按常规阶梯往上爬。
            # 取一半而不是原值: 集群刚缓过来, 立刻打回满并发很容易再次打挂。
            self._limit = max(self._floor, self._pre_pause_limit // 2)
        else:
            self._limit = min(self._ceiling, max(self._floor, int(self._limit * self._grow_factor) + 1))
        self._state = STATE_NORMAL if self._limit >= self._ceiling else STATE_SHRUNK
        self._ok_streak = 0
        if self._limit != old or old == 0:
            self._log(
                f"[throttle] GROW 容量类失败占比 {ratio:.0%}, 连续 {self._recover_streak} 个任务未撞容量, "
                f"有效并发 {old} -> {self._limit}"
            )
            self._dump_state()
            self._wake()

    # ---- 手动调整(复用「每任务重读 YAML」通道) ----

    def set_ceiling(self, n: int) -> None:
        """用户在平台改了 concurrent_num → 调整上界。

        手动值是**上界**: 自动降并发仍可在其下继续收紧, 否则用户一改配置就把
        熔断/降档的保护抹掉了。
        """
        n = max(1, int(n or 1))
        if n == self._ceiling:
            return
        old_ceiling, old_limit = self._ceiling, self._limit
        self._ceiling = n
        self._floor = max(1, min(self._floor, n))
        # 调小时立即收紧; 调大时不直接放开, 交给状态机按窗口逐步升回
        if self._limit > n:
            self._limit = n
        self._log(
            f"[throttle] 配置并发变更 {old_ceiling} -> {n}, 有效并发 {old_limit} -> {self._limit}"
        )
        self._dump_state()
        self._wake()

    def _wake(self) -> None:
        """limit 变化后唤醒等待者。

        不能直接 await(本类的状态机是同步调用的), 故用 call_soon 丢回 loop。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._notify())

    async def _notify(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    # ---- 可观测性 ----

    def snapshot(self) -> dict:
        return {
            "state": self._state,
            "limit": self._limit,
            "ceiling": self._ceiling,
            "floor": self._floor,
            "in_flight": self._in_flight,
            "throttle_ratio": round(self._throttle_ratio(), 4),
            "window": len(self._outcomes),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _log(self, msg: str) -> None:
        if self._logger:
            self._logger.warning(msg)

    def _dump_state(self) -> None:
        """把当前档位写到 _throttle.json, 供平台详情页显示「有效并发」。

        写失败不影响主流程 —— 这只是观测用途。
        """
        if not self._state_file:
            return
        try:
            tmp = f"{self._state_file}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.snapshot(), f, ensure_ascii=False)
            os.replace(tmp, self._state_file)
        except OSError:
            pass


def build_from_config(concurrent_num: int, run_cfg, logger=None, state_file: str = "") -> ThrottleController:
    """按 config.yaml 的 run_config.throttle 段构造控制器。

    配置缺失 / 字段缺失一律回落到默认值, 保证老配置文件不用改也能跑。
    `enabled: false` 时返回一个上界=初始并发、阈值不可达的控制器, 行为等同于
    没有闸门(只是多一层 Condition), 便于一键关掉这套机制。
    """
    cfg = {}
    try:
        raw = run_cfg.get("throttle") if hasattr(run_cfg, "get") else None
        if raw:
            cfg = dict(raw)
    except Exception:
        cfg = {}

    def _num(key, default):
        try:
            val = cfg.get(key)
            return default if val is None else type(default)(val)
        except (TypeError, ValueError):
            return default

    if cfg.get("enabled") is False:
        # 阈值设成 >1 即永不触发; 仍保留闸门以统一代码路径
        return ThrottleController(
            concurrent_num, floor=concurrent_num,
            shrink_ratio=2.0, pause_ratio=2.0, recover_ratio=-1.0,
            logger=logger, state_file=state_file,
        )

    return ThrottleController(
        concurrent_num,
        floor=_num("floor", DEFAULT_FLOOR),
        window=_num("window", DEFAULT_WINDOW),
        min_samples=_num("min_samples", DEFAULT_MIN_SAMPLES),
        shrink_ratio=_num("shrink_ratio", DEFAULT_SHRINK_RATIO),
        pause_ratio=_num("pause_ratio", DEFAULT_PAUSE_RATIO),
        recover_ratio=_num("recover_ratio", DEFAULT_RECOVER_RATIO),
        cooldown=_num("cooldown", DEFAULT_COOLDOWN),
        probe_interval=_num("probe_interval", DEFAULT_PROBE_INTERVAL),
        grow_factor=_num("grow_factor", DEFAULT_GROW_FACTOR),
        recover_streak=_num("recover_streak", DEFAULT_RECOVER_STREAK),
        logger=logger,
        state_file=state_file,
    )
