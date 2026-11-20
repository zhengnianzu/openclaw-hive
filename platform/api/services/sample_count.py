"""数据集样本数自动统计。

把数据集的 user_config 目录从 OBS 下载到本地缓存，统计 config 文件数，
回写 task_registrations.data_total。

- 异步执行（asyncio.create_task），不阻塞保存表单。
- 复用 _prepare_instance 已用的全局缓存目录约定
  （platform/downloads/configs/<path__with__underscores>），同一份数据集不重复下载。
"""

import asyncio
import logging
import os
import shutil

from ..core.config import settings
from ..core.database import get_connection
from .obs_path import strip_bucket

logger = logging.getLogger(__name__)


def dataset_cache_dir(rel_path: str) -> str:
    """数据集 user_config 的本地缓存目录。

    与 instances._prepare_instance 共用同一份缓存：先统计过的数据集，
    创建任务实例时直接命中缓存、跳过 OBS 下载。反之亦然。

    rel_path 是剥离桶名后的相对路径；把 / 换成 __ 作为单层目录名，
    放在 OUTPUT_CACHE/dataset/ 下（与产物缓存 OUTPUT_CACHE/<batch>/<leaf> 同根）。
    """
    cache_key_path = rel_path.strip("/").replace("/", "__")
    return os.path.join(settings.OUTPUT_CACHE, "dataset", cache_key_path)


def _count_config_files(root: str) -> int:
    """数已下载的 config 文件数：穿透单层嵌套目录后，统计其中的普通文件。

    与 instances._count_config_files 行为对齐，避免一处改了另一处漂移。
    """
    if not root or not os.path.isdir(root):
        return 0
    actual = root
    for _ in range(5):
        try:
            entries = os.listdir(actual)
        except OSError:
            return 0
        if len(entries) == 1 and os.path.isdir(os.path.join(actual, entries[0])):
            actual = os.path.join(actual, entries[0])
        else:
            break
    try:
        with os.scandir(actual) as it:
            return sum(1 for e in it if e.is_file())
    except OSError:
        return 0


async def _async_subprocess_run(cmd, *, timeout=600):
    """与 instances._async_subprocess_run 等价的本地实现，避免循环导入。"""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def count_sample(reg_id: int) -> None:
    """下载 + 统计 + 回写。任何异常都落库成 error，不抛出。"""
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT task_path_obs FROM task_registrations WHERE id = ?",
                (reg_id,),
            ).fetchone()
        if not row:
            return
        rel = strip_bucket(row["task_path_obs"])
        if not rel:
            _finish(reg_id, status="error", error="未填写任务路径（task_path_obs）")
            return

        cache_dir = dataset_cache_dir(rel)

        # 已有缓存且非空：直接复用，避免每次统计都拉一遍
        if not (os.path.isdir(cache_dir) and os.listdir(cache_dir)):
            os.makedirs(cache_dir, exist_ok=True)
            bucket = settings.OBS_BUCKET.rstrip("/")
            obs_src = f"{bucket}/{rel}"
            if not obs_src.endswith("/"):
                obs_src += "/"
            try:
                returncode, stdout, stderr = await _async_subprocess_run(
                    [settings.OBSUTIL_PATH, "cp", obs_src, cache_dir, "-r", "-f"],
                    timeout=600,
                )
            except asyncio.TimeoutError:
                shutil.rmtree(cache_dir, ignore_errors=True)
                _finish(reg_id, status="error", error=f"OBS 下载超时（{obs_src}）")
                return
            if returncode != 0:
                shutil.rmtree(cache_dir, ignore_errors=True)
                detail = (stderr or stdout or "").strip()[:500]
                _finish(
                    reg_id,
                    status="error",
                    error=f"OBS 下载失败（{obs_src}，exit={returncode}）: {detail}",
                )
                return

        count = _count_config_files(cache_dir)
        _finish(reg_id, status="done", count=count)
    except Exception as e:
        logger.exception("count_sample error for reg_id=%s", reg_id)
        _finish(reg_id, status="error", error=str(e)[:500])


def _finish(reg_id: int, *, status: str, count: int = 0, error: str = "") -> None:
    try:
        with get_connection() as conn:
            conn.execute(
                "UPDATE task_registrations SET count_status=?, count_error=?, "
                "data_total=?, counted_at=CURRENT_TIMESTAMP WHERE id=?",
                (status, error, count, reg_id),
            )
    except Exception:
        logger.exception("写回统计结果失败 reg_id=%s", reg_id)
