"""数据集本地副本：下载 + 哈希基线 + 样本数统计。

数据集注册后**必定**整份下载到本地（平台后续全部读写本地副本，不再每次打 OBS）。
首次下载时对每个文件算 sha256 存进 task_registration_files，作为「云端那一版」的
指纹；本地改动后哈希不一致即标 modified，由 offline/sync_worker.py 异步回传。

- 异步执行（asyncio.create_task），不阻塞保存表单。
- 与 instances._prepare_instance 共用同一份缓存
  （OUTPUT_CACHE/dataset/<path__with__underscores>），不重复下载。
"""

import asyncio
import logging
import os
import shutil

from ..core.config import settings
from ..core.database import get_connection
from . import dataset_files as dfs
from .obs_path import strip_bucket

logger = logging.getLogger(__name__)


def dataset_cache_dir(rel_path: str) -> str:
    """数据集的本地缓存根（与 dataset_files.cache_dir 同一个目录）。

    保留这个函数名是因为 instances.py / 测试都在用；真正的实现已经收到
    dataset_files 里，避免两处各自拼路径后漂移。
    """
    return dfs.cache_dir(rel_path)


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


async def count_sample(reg_id: int, force_download: bool = False) -> None:
    """下载（缺则补）+ 建哈希基线 + 统计样本数。任何异常落库，不抛出。

    force_download=True 时忽略已有缓存重新拉一遍 —— 「重新下载」按钮用。
    注意重新下载会**覆盖本地改动**，所以调用方必须确认过。
    """
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT id, task_path_obs FROM task_registrations WHERE id = ?",
                (reg_id,),
            ).fetchone()
            if not row:
                return
            rel = strip_bucket(row["task_path_obs"])
            if not rel:
                _finish(reg_id, status="error", error="未填写任务路径（task_path_obs）")
                _download(reg_id, status="error", error="未填写用户Config目录")
                return
            conn.execute(
                "UPDATE task_registrations SET download_status='downloading', "
                "download_error='' WHERE id = ?", (reg_id,))

        cache_dir = dfs.cache_dir(rel)

        need_download = force_download or not (
            os.path.isdir(cache_dir) and os.listdir(cache_dir))
        if need_download:
            if force_download and os.path.isdir(cache_dir):
                shutil.rmtree(cache_dir, ignore_errors=True)
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
                _download(reg_id, status="error", error=f"OBS 下载超时（{obs_src}）")
                return
            if returncode != 0:
                shutil.rmtree(cache_dir, ignore_errors=True)
                detail = (stderr or stdout or "").strip()[:500]
                msg = f"OBS 下载失败（{obs_src}，exit={returncode}）: {detail}"
                _finish(reg_id, status="error", error=msg)
                _download(reg_id, status="error", error=msg)
                return

        # 统计 + 建哈希基线。首次下载时所有文件都是「新行」，
        # synced_sha256 直接等于当前哈希 = 视为与云端一致。
        with get_connection() as conn:
            scan = dfs.scan_and_store(conn, reg_id, rel)
            modified = dfs.refresh_modified_count(conn, reg_id)

        count = _count_config_files(cache_dir)
        _finish(reg_id, status="done", count=count)
        _download(reg_id, status="done", count=count, modified=modified,
                  scanned=scan.get("total", 0))
    except Exception as e:
        logger.exception("count_sample error for reg_id=%s", reg_id)
        _finish(reg_id, status="error", error=str(e)[:500])
        _download(reg_id, status="error", error=str(e)[:500])


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


def _download(reg_id: int, *, status: str, error: str = "",
              count: int = 0, modified: int = 0, scanned: int = 0) -> None:
    try:
        with get_connection() as conn:
            if status == "done":
                conn.execute(
                    "UPDATE task_registrations SET download_status='done', "
                    "download_error='', downloaded_at=CURRENT_TIMESTAMP, "
                    "modified_count=? WHERE id=?",
                    (modified, reg_id),
                )
            else:
                conn.execute(
                    "UPDATE task_registrations SET download_status=?, download_error=? "
                    "WHERE id=?",
                    (status, error[:500], reg_id),
                )
    except Exception:
        logger.exception("写回下载状态失败 reg_id=%s", reg_id)
