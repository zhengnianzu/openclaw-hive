---
title: 沙箱与镜像操作手册
category: 平台使用
tags:
- 沙箱
- 镜像
- kubectl
- 操作
summary: 沙箱的查看/进入/手动清理（kubectl）、Linux 镜像的制作与推送（docker）、Windows 镜像制作（简化版）、Windows 沙箱
  VNC 连接、资源监控看板，以及 sync_resource_pools / run_clear 两个操作脚本的用法。
is_pinned: false
sort_order: 55
author: admin
updated_by: admin
created_at: '2026-11-23T10:00:00'
updated_at: '2026-11-26T20:45:13.172248'
---

# 沙箱与镜像操作手册

面向平台操作：沙箱的日常操作、镜像的制作与推送、Windows 沙箱的桌面连接、资源监控，以及两个常用操作脚本。

## 1. 沙箱日常操作（kubectl）

沙箱即 k8s 集群里的 Pod，日常查看、进入、清理都走 kubectl：

```bash
# 获取kubeconfig凭证
申请cce节点之后，去华为云下载kube凭证
# 查看远程镜像仓
https://console.huaweicloud.com/swr/?locale=zh-cn&agencyId=019f5ea83b0071709bf60f1885723dde&region=cn-east-4#/swr/warehouse/list/private
# 查看沙箱（全部 Pod）
kubectl get pods -A
# 打印节点信息
kubectl describe node
# 进入某个沙箱
kubectl -n omni-env-default-worker exec -it sandbox-k8s-podname -- /bin/bash
```

### 1.1 清理沙箱

1. 按名称关键字清理：

```bash
kubectl -n omni-env-default-worker get pods | grep 'frontmdslow' | awk '{print $1}' | xargs -r kubectl -n omni-env-default-worker delete pod
```

2. 批量清理沙箱 run_clear.py

`openclaw-hive/run_clear.py`。按配置文件里的 `sandbox_id_prefix` 过滤相关 Pod 并释放k8s资源

```bash
# 按配置清理指定沙箱（默认扫描 KUBECONFIG_DIR 下全部 worker 集群）
python3 run_clear.py --config config.yaml
# 只清一个集群
python3 run_clear.py --config config.yaml --kubeconfig /path/to/kubeconfig.yaml
```

### 1.2 资源池总量校准 sync_resource_pools.py

脚本位置：`openclaw-hive/sync_resource_pools.py`; 节点池扩缩容后跑一次。
从 k8s 实测节点容量（`node.status.capacity`）统计并回写 `resource_pools.yaml` 的 `total_cpu / total_memory_gb`（首页看板资源分母）。

```bash
# 只看不写（默认，先核对）
python3 sync_resource_pools.py
# 确认无误后回写 yaml
python3 sync_resource_pools.py --write
# 多资源池时显式指定 pool ↔ 集群映射（按 server 末段或 kubeconfig 路径片段匹配）
python3 sync_resource_pools.py --map omni-env-default-worker=192.168.33.116
```

### 1.3 沙箱资源看板

```bash
# SSH 端口转发到监控看板
ssh -N -L 30010:192.168.11.87:30010 root@115.120.48.151
```

浏览器打开：<http://localhost:30010>

## 2. Linux 镜像制作与推送（docker）

### 2.1 手动操作完整流程

```bash
# 1. 登录华东二镜像仓
docker login -u cn-east-4@HPUABVUGOTP2OPODPEKP \
  -p e1451600de5513aea4e1d4468b28342d25ae4aceaf7dcf0c8f6f43f3d3f1dba0 \
  swr.cn-east-4.myhuaweicloud.com

# 2. 拉取基础镜像
docker pull swr.cn-east-4.myhuaweicloud.com/rl_team/multiharness:26.10.24

# 3. 运行容器（挂载宿主机 /mnt 方便传文件；特权+host ipc 供沙箱类工具使用）
docker run -it --name zxtest -v /mnt:/mnt --restart=always --ipc=host --privileged \
  --entrypoint /bin/bash swr.cn-east-4.myhuaweicloud.com/rl_team/multiharness:26.9.24v1

# 4. 进入容器
docker exec -it -u root zxtest /bin/bash

# 5. 把文件拷贝到容器对应目录后 —— 注意：退出容器后 commit 才有效
docker cp <本地文件路径> <容器名>:<容器内路径>

# 6. 容器固化为新镜像（同名即覆盖旧标签）
docker commit zxtest swr.cn-east-4.myhuaweicloud.com/rl_team/multiharness:26.11.24

# 7. 推送镜像仓（同名覆盖）
docker push swr.cn-east-4.myhuaweicloud.com/rl_team/multiharness:26.11.24
```

## 3. Windows 镜像制作升级

> 完整内容见 `obs://s3-asset-b-hd-cce-aifm-nlp-exp/zengxiang/images-make/Windows镜像制作`；

### 3.1 准备工作

在跳板机 Bash 执行，命令无需改字段。

```bash
# 准备工作目录和pod名称
export WORKDIR=/root/sandbox-log/手工制作新镜像
mkdir -p "$WORKDIR"
cd "$WORKDIR"
K=(kubectl --kubeconfig=/root/.kube/config --context=internal -n omni-env-default-worker)
export POD=wh-20261016-manual-01
export IMAGE_TAG=windows-openclaw-2026.6.6-harness-20261016-manual-01
export BASE_IMAGE='swr.cn-east-4.myhuaweicloud.com/rl_team/multiharness:windows-openclaw-2026.6.6-harness-20261010-2177eeb0-defaultpy-noagentd-v2'
cp -a /root/sandbox-log/Windows镜像制作/scripts/. "$WORKDIR/"

# 检查节点和拉取凭据
"${K[@]}" get node 192.168.15.18 -o wide
"${K[@]}" get secret swr-rl-team -o custom-columns=NAME:.metadata.name,TYPE:.type

"${K[@]}" create --dry-run=server -f builder-pod.yaml
"${K[@]}" create -f builder-pod.yaml
"${K[@]}" get pod "$POD" -w

"${K[@]}" exec "$POD" -c build -- df -h /work /home/ma-user/image
"${K[@]}" exec "$POD" -c build -- ls -l /dev/kvm
"${K[@]}" exec "$POD" -c build -- ls -lh /home/ma-user/image
"${K[@]}" get pod "$POD" -o jsonpath='{.metadata.uid}{"\n"}'

## 复制工作盘并启动 Windows
"${K[@]}" cp -c build prepare_work.sh "${POD}:/work/prepare_work.sh"
"${K[@]}" exec "$POD" -c build -- bash /work/prepare_work.sh

"${K[@]}" cp -c build start_work_vm.sh "${POD}:/work/start_work_vm.sh"
"${K[@]}" exec "$POD" -c build -- bash /work/start_work_vm.sh
"${K[@]}" exec "$POD" -c build -- tail -n 20 /work/logs/qemu.log
```

### 3.2 连接 VNC

三步：port-forward → SSH 隧道 → VNC Viewer。

```bash
# 1. 在跳板机上把 Pod 的 5901 转发到本机 15901
kubectl --kubeconfig=/root/.kube/config --context=internal -n omni-env-default-worker port-forward \
  --address=127.0.0.1 pod/wh-20261016-manual-01 15901:5901

# 2. 另开一个 PowerShell / 终端，建立到跳板机的 SSH 隧道
ssh -NT -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -L 127.0.0.1:15901:127.0.0.1:15901 root@115.120.48.151

# 3. 打开本机 VNC Viewer，连接
127.0.0.1::15901
```

### 3.3 封盘并打包镜像

1. 在跳板机 Bash 的工作目录原样执行：

```bash
/usr/bin/python3 -u finish_windows.py prepare
```

2. 在 **VNC PowerShell**原样输入正常关机命令：

```powershell
shutdown.exe /s /t 0
```

3. 回到跳板机 Bash 的工作目录确认：

```bash
/usr/bin/python3 -u finish_windows.py wait-stopped
```

必须出现 `WORK_QEMU_STOPPED`。仅 VNC 断开不等于关机；返回 `STILL_RUNNING` 时不要封盘。
4\. 确认停止后，在同一跳板机终端原样执行一次：

```bash
"${K[@]}" cp -c build seal_work.sh "${POD}:/work/seal_work.sh"
"${K[@]}" exec "$POD" -c build -- bash /work/seal_work.sh
"${K[@]}" exec "$POD" -c build -- cat /work/sealed/virtual-disk-compare.log
```

5. regctl 不存在时下载；已存在时只核对散列：

```bash
mkdir -p registry-tools
if [ ! -f registry-tools/regctl ]; then
  curl -fL --connect-timeout 10 --max-time 180 -o registry-tools/regctl https://github.com/regclient/regclient/releases/download/v0.11.6/regctl-linux-amd64
fi
echo '8e0e62a497fcdb8048d18aa927a139613176ba0531f412bc541044e28f9856bd  registry-tools/regctl' | sha256sum -c -
chmod 700 registry-tools/regctl
```

6. 打包并发布镜像

```bash
# 打包镜像：
/usr/bin/python3 -u fetch_source_config.py --base-image "$BASE_IMAGE" --regctl ./registry-tools/regctl
"${K[@]}" cp -c build assemble_oci.py "${POD}:/work/assemble_oci.py"
"${K[@]}" cp -c build assemble_image.sh "${POD}:/work/assemble_image.sh"
"${K[@]}" cp -c build source-config.private.json "${POD}:/work/source-config.private.json"
set -o pipefail
"${K[@]}" exec "$POD" -c build -- bash /work/assemble_image.sh "$IMAGE_TAG" 2>&1 | tee packaging.log

# 查看本轮打包摘要：
"${K[@]}" exec "$POD" -c build -- cat /work/packaging/summary.json

# 镜像发布准备
/usr/bin/python3 -u prepare_publish.py

# 最终发布镜像
export TARGET="swr.cn-east-4.myhuaweicloud.com/rl_team/multiharness:$IMAGE_TAG"
set -o pipefail
"${K[@]}" exec "$POD" -c build -- python3 -u /work/publish_image.py "$TARGET" 2>&1 | tee publish.log
```

# 4. 基础镜像具体要求：

### 4.1 下载配置obsutil：

```Shell
# 下载 
cd /home/ma-user/ && wget https://obs-community.obs.cn-north-1.myhuaweicloud.com/obsutil/current/obsutil_linux_amd64.tar.gz 
# 解压 
tar -xzvf obsutil_linux_amd64.tar.gz # 移动 mv obsutil_linux_amd64_5.8.3 obsutil 
# 配置 
./obsutil/obsutil config -i=HPUAEGWMKUI3IZO3HKL0 -k=kuKJHgufvXcXNw7AFgRBFVsKsMCGD0pPFMSB12Tt -e=http://obs.cn-east-4.myhuaweicloud.com 
# 验证
./obsutil/obsutil ls -d obs://s3-asset-b-hd-cce-aifm-nlp-exp/ 
# 双向复制 
./obsutil/obsutil cp -r -f <local_dir> <obs_dir>
```

### &#x20;4.2. 上传文件（以taichu为例）：

| 内容     | 源地址                                                                              | 沙箱地址                                                               | 备注                                                 |
| :----- | :------------------------------------------------------------------------------- | :----------------------------------------------------------------- | :------------------------------------------------- |
| 默认通用技能 | obs\://s3-asset-b-hd-cce-aifm-nlp-exp/zengxiang/image_default_skills/skill_test/ | home/ma-user/taichu/agent_instances/{persona_id}/meta_data/skills/ | persona_id=sys_celia_main，下面存放了serper和image-search |
| 临时技能库  | obs\://s3-asset-b-hd-cce-aifm-nlp-exp/skills_library/1112/skills_library         | /home/ma-user/skills_library/                                      | 包含92个skills和1个md文件                                 |
|        |                                                                                  |                                                                    |                                                    |

## 4.3. 安装python依赖和Chromium工具

```markdown
zstandard
fastapi
uvicorn
python-dotenv
pyyaml
python-pptx
# 使用清华镜像源加速：https://pypi.tuna.tsinghua.edu.cn/simple
# 安装其他工具：Chromium
```
