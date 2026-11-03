# -*- coding: utf-8 -*-
"""沙箱产物浏览端点（实例详情「工作区」tab）。

**读路径全部走本地磁盘，零 obsutil 调用。** 产物由 offline/output_worker.py 的
`_consume_artifacts` 全量预下载到 `OUTPUT_CACHE/<batch>/<leaf>/`，这里只做遍历、
读取与预览。唯一会触发 OBS 的是 `POST /request`（running 实例按需下载），
它只写一行 pending，真正的下载在 worker 进程里做。

安全：
  - 所有路径过 `clean_rel` + `safe_join`（realpath 包含性校验），拒绝 `..`/反斜杠/NUL。
  - task 必须在该实例已知集合内，防止枚举别的实例的目录。
  - `/raw` 强制 `Content-Disposition: attachment`：沙箱 agent 产出的 html 若在平台
    同源下内联打开，里面的 `<script>` 能读 localStorage 的 token。
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from ..core.config import settings, ARTIFACT_PRIORITY_USER
from ..core.database import async_execute, async_query
from ..core.security import get_current_user, require_operator
from ..models.artifact import ArtifactTaskItem, ArtifactTasksResponse, ArtifactTreeResponse
from ..services import artifact_store as store

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])

# 内联响应的 CSP：iframe srcdoc/blob 预览时，这条头是唯一真正的沙箱边界
_INLINE_CSP = ("sandbox; default-src 'none'; img-src data: blob:; "
               "style-src 'unsafe-inline'; script-src 'unsafe-inline'")
_PDF_CSP = "sandbox; default-src 'none'; object-src 'none'"

# /inline 与 /pdf 是整文件读进内存再返回的（要 preview 必须一次性给出完整字节），
# 单任务上限是 2 GiB，不设阈值的话一个大文件就能把 API 进程打爆。
# 超过阈值的预览请求改走 /raw（FileResponse 流式，不吃内存）。
INLINE_MAX_BYTES = 64 * 1024 * 1024


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _cache_subdir(obs_base: str, task: str) -> str:
    """OBS 前缀 → 本地缓存子目录，规则与 deep 详情完全一致。"""
    from src import offline_analysis as oa
    task_obs = obs_base.rstrip("/") + "/" + task.strip("/") + "/"
    return oa._cache_subdir_for(task_obs)


async def _resolve(instance_id: str, task: str) -> tuple[dict, str, str]:
    """取实例、校验 task 归属，返回 (inst, obs_base, task_dir)。

    task 必须在 `task_traj_records` 里（worker 按实例 config 列表建行）——只做前缀包含不够，
    否则能枚举同 batch 下别的实例的任务目录。
    """
    rows = await async_query("SELECT * FROM task_instances WHERE id=?", (instance_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="实例不存在")
    inst = rows[0]

    from src import offline_analysis as oa
    obs_base = oa.obs_base_from_snapshot(inst.get("config_snapshot"))
    if not obs_base:
        config_path = inst.get("config_path")
        if config_path and os.path.exists(config_path):
            try:
                from omegaconf import OmegaConf
                cfg = OmegaConf.load(config_path)
                obs = cfg.run_config.obs
                obs_base = (f"{str(obs.traj_save_bucket).rstrip('/')}/"
                            f"{str(obs.traj_save_path).strip('/')}/")
            except Exception:
                obs_base = None
    if not obs_base:
        raise HTTPException(status_code=400,
                            detail="该实例缺少 OBS 轨迹配置（config_snapshot 与 config.yaml 都取不到）")

    task = (task or "").strip().strip("/")
    if not task:
        raise HTTPException(status_code=400, detail="缺少 task 参数")
    known = await async_query(
        "SELECT traj_name FROM task_traj_records WHERE instance_id=?", (instance_id,))
    if task not in {r["traj_name"] for r in known}:
        raise HTTPException(status_code=404, detail=f"task 不属于该实例: {task}")

    try:
        subdir = _cache_subdir(obs_base, task)
        task_dir = store.task_local_dir(subdir)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"非法路径: {e}")
    return inst, obs_base, task_dir


def _file_path(task_dir: str, key: str) -> str:
    try:
        rel = store.clean_rel(key)
        path = store.safe_join(task_dir, rel)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"非法路径: {e}")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"文件不存在或尚未下载: {key}")
    return path


# ============ task 列表 ============

@router.get("/{instance_id}/tasks", response_model=ArtifactTasksResponse)
async def list_artifact_tasks(instance_id: str, user: dict = Depends(get_current_user)):
    """该实例的 task 列表 + 每个 task 的下载状态。零 obsutil。"""
    insts = await async_query("SELECT * FROM task_instances WHERE id=?", (instance_id,))
    if not insts:
        raise HTTPException(status_code=404, detail="实例不存在")

    from src import offline_analysis as oa
    inst = insts[0]
    obs_base = oa.obs_base_from_snapshot(inst.get("config_snapshot"))

    rows = await async_query(
        "SELECT traj_name, artifacts_status AS status, error, "
        "artifact_files AS file_count, artifact_bytes AS total_bytes, "
        "artifact_downloaded_at AS downloaded_at, artifact_complete "
        "FROM task_traj_records WHERE instance_id=? ORDER BY traj_name", (instance_id,))

    # 懒发现条件：该实例在浅层队列里还没有任何「已完成」的任务。
    #
    # 不能只看 rows（旧写法 `if not rows`）：浅层分析会先建 complete=0 的行，
    # running 实例又从不参与后台自动发现，于是「有 888 行、全是 complete=0」的
    # 实例兜底不触发，列表永远空 —— 实测单实例 7161 个 task 里 1419 个早已跑完，
    # 界面上一个都看不到，全库曾有 1479 个实例处于这个状态。
    #
    # 也不能只看 task_traj_records.artifact_complete：发现已经迁到 task_tracker，
    # 它按实例增量投递，一个从没被 tracker 扫过的新实例在 task_traj_records 里
    # 全是 complete=0，但 shallow_queue 里可能已经有可投递的行。
    if not any(r["artifact_complete"] for r in rows):
        # 注意：不管 _discover_one_instance 返回几，都要重新查一次 ——
        # 它的返回值是「本次新投递的条数」，而前端要的是重新读出来的状态。
        await async_execute(_discover_one_instance, instance_id, inst)
        rows = await async_query(
            "SELECT traj_name, artifacts_status AS status, error, "
            "artifact_files AS file_count, artifact_bytes AS total_bytes, "
            "artifact_downloaded_at AS downloaded_at, artifact_complete "
            "FROM task_traj_records WHERE instance_id=? ORDER BY traj_name",
            (instance_id,))

    result: list[ArtifactTaskItem] = []
    for r in rows:
        # 「已下载」必须看 DB 状态，不能只看目录是否存在：
        # 深层详情下载会先把 <batch>/<leaf>/ 目录建出来（里面只有 logs/ workdir/），
        # 交付物实际在 <leaf>/<leaf>/ 这一层。用 isdir 判断的话，凡是跑过深层详情的
        # 任务都会被误标成「已下载」——实测单个实例 11678 行全被标成已下载，
        # 其中 11459 行其实一条交付物都没下。
        downloaded = r["status"] == "done"
        result.append(ArtifactTaskItem(
            traj_name=r["traj_name"],
            status=r["status"] or "queued",
            error=r["error"],
            file_count=r["file_count"],
            total_bytes=r["total_bytes"],
            downloaded_at=r["downloaded_at"],
            downloaded=downloaded,
            complete=bool(r["artifact_complete"]),
        ))
    # 已完成的排前面：用户点开 tab 想先看到能用的那些，未完成的长尾沉到后面。
    # 前端列表长（单实例可上万条），这个顺序直接影响第一屏。
    result.sort(key=lambda t: (not t.complete, t.traj_name))
    return ArtifactTasksResponse(instance_id=instance_id, obs_base=obs_base, tasks=result)


# ============ 目录树（一次返回全部，本地 IO） ============

@router.get("/{instance_id}/tree", response_model=ArtifactTreeResponse)
async def get_artifact_tree(
    instance_id: str,
    task: str = Query(description="task 目录名"),
    user: dict = Depends(get_current_user),
):
    """整个 task 的文件列表。"""
    inst, obs_base, task_dir = await _resolve(instance_id, task)
    if not os.path.isdir(task_dir):
        raise HTTPException(status_code=409,
                            detail="该 task 尚未下载到本地，请先点「下载此任务」")
    # 遍历跑在 NFS 上，单个 task 实测可达 3.3 万文件（light_variant_21098 = 33207）。
    # 同步 walk 放在 async handler 里会把事件循环钉住，必须丢线程池。
    entries, truncated = await async_execute(store.walk_tree_limited, task_dir)
    return ArtifactTreeResponse(task=task, root=task_dir, entries=entries,
                                truncated=truncated)


# ============ 内容 / 下载 / 预览 ============

@router.get("/{instance_id}/content")
async def get_artifact_content(
    instance_id: str,
    task: str = Query(...),
    key: str = Query(..., description="task 内相对路径"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200000, ge=1, le=2000000),
    user: dict = Depends(get_current_user),
):
    """文本分页读取（按字符，同 logs.py obs-view 的语义）。"""
    inst, obs_base, task_dir = await _resolve(instance_id, task)
    path = _file_path(task_dir, key)
    out = store.read_text_slice(path, offset, limit)
    out.update({"task": task, "key": store.clean_rel(key), "size": os.path.getsize(path)})
    return out


@router.get("/{instance_id}/raw")
async def download_artifact(
    instance_id: str,
    task: str = Query(...),
    key: str = Query(...),
    user: dict = Depends(get_current_user),
):
    """下载原文件。强制 attachment（防同源 XSS）。"""
    inst, obs_base, task_dir = await _resolve(instance_id, task)
    path = _file_path(task_dir, key)
    name = os.path.basename(store.clean_rel(key))
    # 不能手工拼 Content-Disposition：Starlette 用 latin-1 编码 header 值，
    # 中文文件名会直接 UnicodeEncodeError → 500（沙箱交付物里 pptx/docx 中文名极常见）。
    # 只交 filename=，Starlette 自己生成 RFC 5987 的 filename*=utf-8''... 形式。
    return FileResponse(
        path,
        filename=name,
        media_type="application/octet-stream",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/{instance_id}/inline")
async def inline_artifact(
    instance_id: str,
    task: str = Query(...),
    key: str = Query(...),
    user: dict = Depends(get_current_user),
):
    """内联返回文件字节，供前端 blob/iframe 预览。"""
    inst, obs_base, task_dir = await _resolve(instance_id, task)
    path = _file_path(task_dir, key)
    size = os.path.getsize(path)
    if size > INLINE_MAX_BYTES:
        # 整文件读进内存再返回：2 GiB 的单任务上限内，任何一个大文件都足以把
        # API 进程打爆。超过阈值让前端改用 /raw（FileResponse 是流式的）。
        raise HTTPException(
            status_code=413,
            detail=(f"文件过大（{size / 1048576:.1f} MB > "
                    f"{INLINE_MAX_BYTES / 1048576:.0f} MB），不支持在线预览，请下载查看"))
    media, inline_ok = store.media_type(store.clean_rel(key))
    body = await async_execute(_read_bytes, path)
    headers = {"X-Content-Type-Options": "nosniff"}
    if inline_ok:
        headers["Content-Security-Policy"] = _INLINE_CSP
    return Response(content=body, media_type=media, headers=headers)


@router.get("/{instance_id}/pdf")
async def artifact_pdf(
    request: Request,
    instance_id: str,
    task: str = Query(...),
    key: str = Query(...),
    user: dict = Depends(get_current_user),
):
    """office 文件转 PDF 预览。未安装 LibreOffice 时 503。

    转换是 CPU/进程密集（soffice 单实例），放到线程池里跑，不阻塞事件循环。
    命中缓存时整条路径只是几次 stat + 读文件，所以做了 ETag：前端切回同一文件
    时可以直接 304，省掉几十 MB 的 PDF 传输。
    """
    inst, obs_base, task_dir = await _resolve(instance_id, task)
    if not store.soffice_available():
        raise HTTPException(
            status_code=503,
            detail="服务器未安装 LibreOffice，无法在线预览该格式，请下载后查看")

    rel = store.clean_rel(key)
    src = _file_path(task_dir, rel)
    try:
        pdf = await async_execute(store.ensure_pdf, task_dir, rel)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"文件不存在: {key}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"转换失败: {str(e)[:300]}")

    st = os.stat(pdf)
    if st.st_size > INLINE_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail="转换后的 PDF 过大，不支持在线预览，请下载原始文件查看")
    etag = f'"{int(st.st_mtime)}-{st.st_size}"'
    headers = {"Content-Security-Policy": _PDF_CSP,
               "X-Content-Type-Options": "nosniff",
               "ETag": etag,
               "Cache-Control": "private, max-age=0, must-revalidate"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    body = await async_execute(_read_bytes, pdf)
    return Response(content=body, media_type="application/pdf", headers=headers)


# ============ 按需下载 ============

@router.post("/{instance_id}/request")
async def request_artifact_download(
    instance_id: str,
    task: str = Query(...),
    user: dict = Depends(require_operator),
):
    """把某 task 排进产物下载队列（worker 下一轮消费）。

    running 实例不自动下载，用户点开工作区时才走这里。
    """
    insts = await async_query("SELECT * FROM task_instances WHERE id=?", (instance_id,))
    if not insts:
        raise HTTPException(status_code=404, detail="实例不存在")

    task_name = task.strip("/")
    rows = await async_query(
        "SELECT traj_name, artifacts_status FROM task_traj_records "
        "WHERE instance_id=? AND traj_name=?",
        (instance_id, task_name))
    if not rows:
        # 该 task 还没被 worker 发现过（running 实例从不自动发现，
        # 或实例刚建还没来得及扫）。就地补一次发现，只针对这一个实例。
        # 否则用户点开「沙箱交付物」只会看到「等 worker 扫过该批次后重试」，
        # 而 worker 对 running 实例永远不会扫 —— 永远等不到。
        found = await async_execute(
            _discover_one_instance, instance_id, dict(insts[0]))
        if found:
            # 补发现后必须重新读一次，否则拿到的还是空行
            rows = await async_query(
                "SELECT traj_name, artifacts_status FROM task_traj_records "
                "WHERE instance_id=? AND traj_name=?",
                (instance_id, task_name))
        if not rows:
            raise HTTPException(
                status_code=404,
                detail="该实例暂无已完成的可下载 task"
                       "（complete.jsonl 为空/取不到，或该 task 尚未跑完）")

    cur = rows[0]
    status = cur["artifacts_status"]
    if status == "downloading":
        return {"task": task, "status": "downloading"}
    if status == "done":
        return {"task": task, "status": "done"}

    await async_execute(lambda: _requeue_artifact(instance_id, task_name))
    return {"task": task, "status": "queued"}


@router.post("/{instance_id}/request-batch")
async def request_artifact_download_batch(
    instance_id: str,
    tasks: list[str] = Body(..., embed=True),
    user: dict = Depends(require_operator),
):
    """把**一批** task 排进产物下载队列（整实例/当前页批量「下载交付物」）。

    为什么需要这个：tracker 不再自动把交付物入队（全量下载会写爆磁盘 ——
    线上实测积压 97.4 万条、磁盘只剩 214G），产物改成纯点击驱动。
    但用户常常要的是「把这一页看到的任务都下下来」，逐个 POST /request
    要发几十个请求。这里一次收一个列表。

    与单 task 入口完全同口径：复用 `_requeue_artifact`，置
    artifact_state='queued' + artifact_priority=100（实时车道），
    由 deep-realtime worker 2 秒内领走。

    上限 500：再多就该用整实例的背景补全，而不是让一个请求写几万行。
    """
    insts = await async_query("SELECT 1 FROM task_instances WHERE id=?", (instance_id,))
    if not insts:
        raise HTTPException(status_code=404, detail="实例不存在")

    uniq = list(dict.fromkeys(t.strip("/") for t in tasks if t and t.strip("/")))
    if not uniq:
        raise HTTPException(status_code=400, detail="tasks 为空")
    if len(uniq) > 500:
        raise HTTPException(status_code=400,
                            detail=f"一次最多 500 个 task（收到 {len(uniq)}）")

    def _queue_all() -> int:
        for name in uniq:
            _requeue_artifact(instance_id, name)
        return len(uniq)

    n = await async_execute(_queue_all)
    return {"instance_id": instance_id, "queued": n, "status": "queued"}


def _discover_one_instance(instance_id: str, inst: dict) -> int:
    """按需发现单个实例的交付物（仅供 POST /request 用 / GET /tasks 懒发现）。

    复用 task_tracker 的增量读逻辑（resolve_output_dir / find_records / submit_batch），
    不再走 output_worker._discover_artifacts 的全量 UPSERT —— 那个按 configs/ 下
    全部 json 建行，单实例 3.6 万 config，用户点一下就要几十秒到十几分钟。
    这里只投 complete.jsonl 里真实存在的任务，天然就是「已跑完、可下载」的集合。
    """
    try:
        from offline import task_tracker as tt
        from ..core.database import get_connection

        out_dir = tt.resolve_output_dir(inst.get("config_path") or "")
        if not out_dir:
            return 0
        path = tt.find_records(out_dir, "complete.jsonl")
        if not path:
            return 0
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                names = [ln.strip() for ln in f if ln.strip()]
        except OSError:
            return 0
        uniq = list(dict.fromkeys(os.path.basename(n) for n in names))
        if not uniq:
            return 0
        with get_connection() as conn:
            n = tt.submit_batch(conn, instance_id, uniq, artifact_ready=True)
            # 同时建 task_traj_records 的镜像行 —— 工作区列表（GET /tasks）读的是
            # **这张表**，而 submit_batch 只写 shallow_queue。
            #
            # 以前镜像行由 `--shallow-only` / `--deep-only` 两个 worker 的
            # _upsert_traj_record 顺手建；产物改成纯点击驱动后那两个进程不再常驻，
            # 于是没有任何东西建镜像行 → 列表永远空，界面显示
            # 「暂无可浏览的任务。实例还没产出交付物，或 complete.jsonl 尚未生成」，
            # 而 shallow_queue 里其实已经有上千行（实测某实例 1429 行 vs 镜像 0 行）。
            #
            # 这里只写列表需要的三列（身份 + artifact_complete）：分级、token 数等
            # 由浅层链路另行回填，不在这里瞎猜。artifact_complete 用 MAX() 语义，
            # 不能把用户点过下载的 1 覆盖回 0。
            _mirror_discovered(conn, instance_id, uniq)
            return n
    except Exception as e:
        # 不能静默返回 0：用户点「沙箱交付物」没反应时，日志里必须留下原因。
        print(f"[artifacts] 按需发现失败 {instance_id}: {str(e)[:200]}", flush=True)
        return 0


def _mirror_discovered(conn, instance_id: str, config_names: list[str]) -> None:
    """为发现到的 config 建/更新 task_traj_records 镜像行（仅列表需要的列）。

    traj_name = config 去掉 .json 后缀，与 _requeue_artifact / trigger_deep 的
    口径一致（它们按 traj_name 定位行），否则会出现「点下载建了一行、
    列表又建一行」的重复。
    """
    rows = [(instance_id, cn, cn[:-5] if cn.endswith(".json") else cn)
            for cn in config_names]
    conn.executemany(
        "INSERT INTO task_traj_records "
        "  (instance_id, config_name, traj_name, artifact_complete, "
        "   artifacts_status, updated_at) "
        # artifacts_status='pending_click' 与 shallow_queue.artifact_state 同口径：
        # 没人点过 = 没排队也没下载。写 NULL 的话 GET /tasks 会 `or 'queued'`
        # 兜成「排队中」，界面上 1426 个任务全显示在排队，而其实一个都没在下。
        "VALUES (?,?,?,1,'pending_click',CURRENT_TIMESTAMP) "
        "ON CONFLICT(instance_id, config_name, traj_name) DO UPDATE SET "
        # MAX 语义：已是 1（用户点过下载）的不回退
        "  artifact_complete=MAX(task_traj_records.artifact_complete, 1)",
        rows,
    )


def _requeue_artifact(instance_id: str, task: str) -> None:
    """把某 task 排进产物下载队列（shallow_queue 的 artifact 维）。

    队列已从 task_traj_records 迁到 shallow_queue.artifact_state：
    发现由 task_tracker 在 complete.jsonl 增量读时完成，worker 只负责领取下载。
    这里同时写两处——
      - shallow_queue：权威队列状态，worker 只认它；
      - task_traj_records：镜像，前端 GET /tasks 读的是这张表。
    两处口径必须一致，否则会出现「界面显示已排队、worker 却永远不领」。
    """
    from ..core.database import get_connection
    leaf = task.strip("/")
    config_name = leaf if leaf.endswith(".json") else leaf + ".json"
    with get_connection() as conn:
        # 用户显式点击：重置重试计数与退避，突破 max_attempts 的死行。
        # artifact_priority=100 写的是**产物维自己的**车道号，不是 priority ——
        # priority 是浅层「整实例插队」的语义，tracker 每 5 秒会把它重写回 100，
        # 拿它分车道会让整个实例的行都涌进实时车道。
        conn.execute(
            "INSERT INTO shallow_queue "
            "  (instance_id, config_name, local_state, obs_state, "
            "   artifact_state, artifact_priority, artifact_attempts, artifact_note) "
            "VALUES (?,?,'queued','queued','queued',?,0,'用户点击') "
            "ON CONFLICT(instance_id, config_name) DO UPDATE SET "
            "  artifact_priority=MAX(shallow_queue.artifact_priority, "
            "                         excluded.artifact_priority), "
            "  artifact_state='queued', artifact_attempts=0, artifact_retry_at=NULL, "
            "  artifact_claimed_by=NULL, artifact_claimed_at=NULL, "
            "  updated_at=CURRENT_TIMESTAMP",
            (instance_id, config_name, ARTIFACT_PRIORITY_USER))
        # 镜像：artifact_complete 必须置 1，前端才会把它当「可下载」的任务展示。
        conn.execute(
            "UPDATE task_traj_records SET artifacts_status='queued', "
            "artifact_complete=1, artifact_attempts=0, artifact_priority=?, "
            "error=NULL, updated_at=CURRENT_TIMESTAMP "
            "WHERE instance_id=? AND traj_name=? AND artifacts_status<>'downloading'",
            (ARTIFACT_PRIORITY_USER, instance_id, leaf))
        conn.commit()
