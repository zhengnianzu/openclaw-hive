# 改动安全性分级 + 新模式方案

> 背景：当前 **26 个 hive 进程在跑**（最老的已跑 25 小时），**25 个实例排队待启动**。
> 排队的那 25 个一旦放行，用的就是磁盘上的代码 —— 这是真正的暴露面。

---

## 一、已改的东西，哪些一定没问题

### ✅ 绝对安全（已验证，可以不管）

| 改动 | 为什么一定没问题 | 验证方式 |
|---|---|---|
| `utils/throttle.py`（新文件） | 全新文件，**没有任何代码 import 它** | `grep -r "throttle" hive.py` 无命中 |
| `test/test_throttle.py`（新文件） | 测试文件，不在运行路径上 | 同上 |
| `test/test_error_policy.py`（新文件） | 同上 | 同上 |
| `prepare_config.py` 的 120 行新增 | **纯新增，0 删除**（`120 insertions(+)`），只加了 4 个新符号 + 1 个新常量表，没有改动任何现有行 | `git diff` 的 `^-` 行为空 |

**对正在跑的 26 个进程：零影响。** 已验证三条：
1. 进程持有的 `.py` 文件句柄数 = **0** —— Python 启动时已把模块编译进内存，之后不再读盘。
2. 全仓**没有任何** `importlib.reload` / `importlib.import_module` / `__import__` —— 不存在运行时重新加载的路径。
3. `prepare_config` 导入正常，`ERROR_CATALOG` 仍 24 条，`_classify_task_stdout`、`HiveError` 均完好；`utils/win_util.py` 依赖的 4 个符号（`HiveError`、`_log_sandbox_detail`、`_classify_task_stdout`、`ERROR_CATALOG`）全部健在。

### 测试结论

```
test_error_policy.py   7/7 通过   （24 个错误码全覆盖，无遗漏无孤儿）
test_throttle.py      13/13 通过
```

其中测出并修掉了 **3 个真 bug**（见第三节）。

---

## 二、还没改、且**不建议直接改**的东西

| 文件 | 计划中的改动 | 风险 |
|---|---|---|
| `hive.py` | 包住 `make()` 做重试、`_worker` 加闸门、`task.run` 加返回值 | ⚠️ **排队中的 25 个实例会立刻用上**，未经真机验证 |
| `platform/api/routers/instances.py` | `error_tree` 带 policy | 需重启平台进程才生效（由你执行） |
| `InstanceDetail.vue` | policy 标签 | 需重新构建前端 |

**所以 `hive.py` 我一行都没动。** 下面是不动它的办法。

---

## 三、新模式方案：让新机制只在「明确开启」时生效

### 方案 A：环境变量开关（推荐，改动最小）

`hive.py` 的改动全部包在一个开关后面，默认**关闭**，行为与现在**逐字节一致**：

```python
# hive.py 顶部
THROTTLE_ENABLED = os.environ.get("HIVE_THROTTLE", "").lower() in ("1", "true", "on")
```

- 开关关闭 → `throttle = None`，`_worker` 走原来的裸调用路径，`make()` 不包重试。
  **排队中的 25 个实例完全不受影响。**
- 开关打开 → 平台启动 hive 时注入 `HIVE_THROTTLE=1`（`subprocess.Popen` 已经在传
  `env`，见 `instances.py:1949` 的 `env["RLXF_CLEAN_LOG_PATH"]`，加一个键即可）。

灰度路径：平台侧加一个「启用自适应并发」勾选框 → 只对勾选的新实例注入环境变量 →
先拿 1 个小并发实例（如 cc=10）跑通 → 再放到 cc=600 → 最后才给 cc=2000。

### 方案 B：配置文件开关

`config.yaml` 里 `run_config.throttle.enabled`。`utils/throttle.py` 的
`build_from_config()` **已经实现了** `enabled: false` 分支。

缺点：每个实例的 `config.yaml` 是平台生成的，要改 `prepare_config.py` 的模板；
而且老实例的 config 里没这个字段，得靠默认值兜住。

### 方案 C：复制一份 `hive_v2.py`

完全隔离，但后续两份代码会分叉，不建议。

**我的建议：方案 A。** 理由：环境变量注入通道已经存在（`env` 字典已在用），
不碰 config 模板，开关粒度是「单个实例」，且默认关闭时新代码一行都不执行。

---

## 四、哪些要测试后才能继续

按风险从高到低：

### 🔴 必须真机验证（不能只靠单测）

1. **`task.run` 加返回值** —— 它现在返回 `None`，`_worker` 里没人接。改成返回
   `(status, error_code, detail)` 后要确认：
   - `_worker` 的 `except Exception` / `finally: task_queue.task_done()` 不受影响；
   - `failed.jsonl` / `complete.jsonl` 的写入时机不变（这两个文件的计数被平台用来
     算进度，曾出过「进度 212%」的坑）。
   - **验证方式**：拿 cc=10 的实例跑 10 个任务，比对 `complete.jsonl` 行数与
     平台显示的 completed_tasks。

2. **`make()` 包重试** —— 重试会**延长单任务耗时**，可能撞上
   `wait_timeout_seconds: 1200` 或 `max_ttl_second: 14400`。
   - **验证方式**：确认重试的总退避时间 + 单任务耗时 < 沙箱 TTL。

3. **闸门串进 `_worker`** —— 单测已证明「降并发不中断在跑的任务」，但真机还要确认
   闸门没有拖慢正常路径（`async with` 在 limit 充足时应几乎零开销）。

### 🟡 建议验证

4. **S005 转 fail_fast 的阈值逻辑** —— `config_error_ratio: 0.8` 需要「实例累计
   失败率」，而单个 task 进程内看不到全局。这块**计划里没写清楚怎么实现**，
   需要先定方案（放平台侧判定？还是 hive 进程内自己累计？）。

5. **重试对 OBS/控制面的额外压力** —— S005 重试 3 次意味着下载流量 ×3，
   大并发下可能反而打挂 OBS。建议重试带抖动（复用 S009 的 `random.uniform`）。

### 🟢 低风险

6. 平台侧 `error_tree` 带 policy、前端 policy 标签 —— 纯展示，错了也只是显示不对，
   不影响任务执行。

---

## 五、建议的下一步顺序

```
1. 先定「新模式开关」用方案 A        ← 需要你确认
2. hive.py 改动全部包在开关后面（默认关）
3. 单测 + 语法检查
4. 你重启平台后，用 1 个 cc=10 实例开开关验证
5. 比对 complete.jsonl 与平台进度一致
6. 逐步放大到 cc=600 / cc=2000
7. 最后才做平台侧 + 前端展示
```

**第 1 步需要你拍板**，我不自己选 —— 因为这决定了后续所有改动的形态。
