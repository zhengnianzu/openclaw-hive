# -*- coding: utf-8 -*-
"""创建参数（create_params）的组装逻辑。

从 `api/routers/instances.py::get_create_params` 抽出：详情页「配置信息 → 创建参数」卡片
和任务详情导出必须**同一口径**，导出时展示什么、导出就是什么。抽成纯函数后两条路径
共用一份实现，不再各写一遍。

与端点版本的差异只有三点：
  1. 入参是 `task_instances` 整行（调用方已查好行，批量导出时避免 N 次重复 SELECT）；
  2. 没有 create_params 时返回空 dict，而不是抛 404 —— 导出时一条坏数据不该炸整包；
  3. 额外返回 snapshot_meta，供导出标注「快照缺失/解析失败」。
"""
from __future__ import annotations

import json

from omegaconf import OmegaConf

from ..core.database import get_connection


def _resolve_suffix_api_keys(row: dict) -> bool:
    """该实例是否开启了 key 沙箱后缀（只读 DB 列，不解析快照）。

    与 instances._resolve_suffix_api_keys 语义一致；单独实现是为了避免
    services → routers 的反向 import。
    """
    return bool(row.get("suffix_api_keys"))


def build_create_params(row: dict) -> tuple[dict, dict]:
    """把 task_instances 行组装成详情页/导出共用的创建参数字典。

    返回 (params, snapshot_meta)：
      params        —— 与 GET /api/instances/{id}/create-params 完全一致的结构；
                       无 create_params 记录时为空 dict。
      snapshot_meta —— {"has_snapshot": bool, "snapshot_error": str | None}，
                       仅提供导出标注用，端点忽略。
    """
    meta = {"has_snapshot": False, "snapshot_error": None}
    if not row.get("create_params"):
        # 快照本身仍可用来标注；只是没有创建参数可回填。
        meta["has_snapshot"] = bool(row.get("config_snapshot"))
        return {}, meta

    params = json.loads(row["create_params"])
    params.pop("copy_complete_from", None)  # 不继承
    # 以 DB 列为准（创建/迁移时已写入），历史实例同样有值
    params["suffix_api_keys"] = _resolve_suffix_api_keys(row)
    # OBS 路径的最终生效值从 config_snapshot 回填：既兼容历史实例（自动生成的
    # traj_save_path 未写入 create_params），也区分普通 OBS bucket 与轨迹 bucket。
    snap = None
    if row.get("config_snapshot"):
        meta["has_snapshot"] = True
        try:
            snap = OmegaConf.create(row["config_snapshot"])
            params["obs_bucket"] = OmegaConf.select(snap, "s3.bucket_name")
            traj_bucket = OmegaConf.select(snap, "run_config.obs.traj_save_bucket")
            if traj_bucket:
                params["traj_save_bucket"] = traj_bucket
            if not params.get("traj_save_path"):
                params["traj_save_path"] = (
                    OmegaConf.select(snap, "run_config.obs.traj_save_path") or ""
                )
        except Exception as e:
            snap = None
            meta["snapshot_error"] = str(e)[:200]
    # 沙箱配置：create_params 只存了 id，这里补上名称/类型/文件，供详情页展示。
    # 实例可能引用了已删除的沙箱配置，此时查不到，保持 sandbox_config_name 为空。
    sb_id = params.get("sandbox_config_id")
    # Harness 配置：历史实例只有 harness_config_id，这里补上方案名/版本号；
    # 版本行可能已被删除，能查到时才补。
    hv_id = params.get("harness_config_version_id")
    if hv_id:
        with get_connection() as conn:
            hv = conn.execute(
                "SELECT v.version, c.name AS config_name FROM harness_config_versions v "
                "LEFT JOIN harness_configs c ON c.id = v.config_id WHERE v.id = ?",
                (hv_id,),
            ).fetchone()
        if hv:
            params["harness_config_version"] = hv["version"]
            if hv["config_name"]:
                params["harness_config_name"] = hv["config_name"]
    if sb_id:
        with get_connection() as conn:
            sb = conn.execute(
                "SELECT name, sandbox_type, sandbox_key, file FROM sandbox_configs WHERE id = ?",
                (sb_id,),
            ).fetchone()
        if sb:
            params["sandbox_config_name"] = sb["name"]
            params["sandbox_type"] = sb["sandbox_type"]
            params["sandbox_key"] = sb["sandbox_key"]
            params["sandbox_file"] = sb["file"]
    # 实际生效的 sandbox key / 镜像：从 config_snapshot 读（可能与当前沙箱配置不同，
    # 因为实例创建时的值已固化进快照）
    if snap is not None:
        try:
            key = OmegaConf.select(snap, "sandbox_active_key")
            params["sandbox_active_key"] = key
            img = OmegaConf.select(snap, f"sandbox.{key}.sandbox.image") if key else None
            if img:
                params["sandbox_image"] = img
            # 资源池与单沙箱资源：与首页看板同源（config_snapshot），
            # 沙箱配置事后被改/删也不影响这里显示的历史值。
            params["sandbox_namespace"] = OmegaConf.select(snap, "sandbox_namespace") or ""
        except Exception:
            pass
    return params, meta
