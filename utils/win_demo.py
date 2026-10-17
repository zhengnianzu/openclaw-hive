"""Run sandbox-log/1014.log through env_sdk; closes only its newly created sandbox."""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import tarfile
import time
import uuid
from pathlib import Path

from env_sdk import close_sandbox, create_sandbox, load_config

IMAGE = "swr.cn-east-4.myhuaweicloud.com/rl_team/multiharness:windows-openclaw-2026.6.6-harness-20261010-2177eeb0-defaultpy-noagentd-v2"


async def main(args):
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=False)
    cfg = load_config(path=args.config)
    win = cfg["sandbox"]["windows"]
    win["sandbox"]["image"] = args.image
    win["sandbox"]["boot_source"] = {"type": "image", "ref": args.image}
    win["max_ttl_second"] = max(8000, args.timeout + 1800)
    run_id = "oc1014-" + uuid.uuid4().hex[:10]
    remote = "C:/omni-runs/" + run_id
    state = {"run_id": run_id, "image": args.image, "ok": False, "closed": False}
    client = None

    def save():
        (out / "result.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    async def sdk(method, **kwargs):
        return await asyncio.to_thread(method, **kwargs)

    async def ps(name, script, timeout=120):
        prefix = f"$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; $env:OPENCLAW_CONFIG_PATH='{remote}/openclaw.json'; $env:OPENCLAW_STATE_DIR='{remote}/state'; if(Test-Path $env:OPENCLAW_CONFIG_PATH) {{$oc=Get-Content $env:OPENCLAW_CONFIG_PATH -Raw | ConvertFrom-Json; $env:OPENCLAW_GATEWAY_TOKEN=$oc.gateway.auth.token}}; "
        encoded = base64.b64encode((prefix + script).encode("utf-16le")).decode()
        result = await sdk(client.exec_command, command="powershell.exe -NoProfile -NonInteractive -EncodedCommand " + encoded, timeout_seconds=timeout)
        (out / (name + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(name, json.dumps(result, ensure_ascii=False, default=str), flush=True)
        data = result.get("response", result.get("data", result))
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected SDK response: {name}")
        code = data.get("exit_code", result.get("exit_code"))
        if code != 0 or result.get("success") is False:
            raise RuntimeError(f"SDK command failed: {name}, exit_code={code}")
        return data

    async def download(name, path):
        return await sdk(client.download_file, local_path=str(out / name), remote_path=path, timeout_seconds=120)

    scope_patch = out / "patch_openclaw_scope.py"
    scope_patch.write_text(
        """from pathlib import Path
import sys

path = Path(sys.argv[1])
source = path.read_text(encoding="utf-8")
needle = "        super().__init__(**kwargs)"
replacement = (
    '        kwargs.setdefault("operator_scopes", '
    '["operator.read", "operator.write", "operator.admin"])\\n'
    "        super().__init__(**kwargs)"
)
if source.count(needle) != 1:
    raise SystemExit(f"unexpected ResilientGateway constructor count: {source.count(needle)}")
path.write_text(source.replace(needle, replacement), encoding="utf-8")
print("operator.admin scope injected")
""",
        encoding="utf-8",
    )
    admin_init = out / "approve_openclaw_admin.py"
    admin_init.write_text(
        """import asyncio
import json
import os
import pathlib
import subprocess
import sys

from openclaw_sdk.gateway.protocol import ProtocolGateway

SCOPES = ["operator.read", "operator.write", "operator.admin"]
ALLOWED = set(SCOPES)
CLI = r"C:\\ProgramData\\OmniEnv\\bin\\openclaw.cmd"


def cli(args):
    return subprocess.run(
        ["cmd.exe", "/d", "/c", CLI, *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


def parse_cli(result):
    start = result.stdout.find("{")
    if result.returncode or start < 0:
        raise RuntimeError("official OpenClaw device command failed")
    return json.JSONDecoder().raw_decode(result.stdout[start:])[0]


async def connect(url):
    gateway = ProtocolGateway(
        ws_url=url,
        connect_timeout=15,
        default_timeout=15,
        operator_scopes=SCOPES,
    )
    try:
        await gateway.connect()
        hello = gateway._connect_future.result()
        granted = set(hello.get("auth", {}).get("scopes", []))
        if not ALLOWED.issubset(granted):
            raise RuntimeError("gateway did not grant operator.admin")
        return sorted(granted)
    finally:
        await gateway.close()


async def main():
    config_path = pathlib.Path(os.environ["OPENCLAW_CONFIG_PATH"])
    state = pathlib.Path(os.environ["OPENCLAW_STATE_DIR"])
    cfg = json.loads(config_path.read_text(encoding="utf-8-sig"))
    port = int(cfg.get("gateway", {}).get("port", 18789))
    url = f"ws://127.0.0.1:{port}/gateway"
    if not (state / "identity/device.json").is_file():
        initialized = cli(["gateway", "call", "health", "--json"])
        if initialized.returncode:
            raise RuntimeError("official CLI failed to initialize the local device")
    try:
        granted = await connect(url)
        print(json.dumps({"admin_scope": "already-approved", "granted": granted}))
        return
    except Exception as exc:
        if "pairing" not in str(exc).lower():
            raise

    device = json.loads((state / "identity/device.json").read_text(encoding="utf-8-sig"))
    listing = parse_cli(cli(["devices", "list", "--json"]))
    matches = []
    for request in listing.get("pending", []):
        scopes = set(request.get("scopes", []))
        if request.get("deviceId") == device["deviceId"] and scopes == ALLOWED:
            matches.append(request)
    if len(matches) != 1:
        raise RuntimeError(f"expected one exact local admin scope request, found {len(matches)}")
    approval = parse_cli(cli(["devices", "approve", matches[0]["requestId"], "--json"]))
    if approval.get("device", {}).get("deviceId") != device["deviceId"]:
        raise RuntimeError("approval returned a different device")
    granted = await connect(url)
    print(json.dumps({"admin_scope": "approved", "granted": granted}))


asyncio.run(main())
""",
        encoding="utf-8",
    )
    runtime_config = out / "openclaw.runtime.json"
    config_data = json.loads(Path(args.openclaw_config).read_text(encoding="utf-8-sig"))
    with tarfile.open(args.task_tar) as archive:
        config_members = [
            member for member in archive.getmembers()
            if member.name.endswith(("/config/config_session.json", "/configs/config_session.json"))
        ]
        if len(config_members) != 1:
            raise RuntimeError(f"expected one task config in archive, found {len(config_members)}")
        extracted = archive.extractfile(config_members[0])
        if extracted is None:
            raise RuntimeError("task config could not be read")
        task_config = json.load(extracted)
    agents = config_data.setdefault("agents", {})
    configured = agents.setdefault("list", [])
    configured_ids = {str(item.get("id", "")) for item in configured if isinstance(item, dict)}
    for agent in task_config.get("agents", []):
        agent_id = str(agent.get("name", "")).strip()
        if agent_id and agent_id not in configured_ids:
            configured.append(
                {
                    "id": agent_id,
                    "workspace": f"C:/Windows/System32/config/systemprofile/.openclaw/workspace-{agent_id}",
                }
            )
    runtime_config.write_text(json.dumps(config_data, ensure_ascii=False, indent=2), encoding="utf-8")

    save()
    try:
        print("creating", run_id, flush=True)
        client = await create_sandbox(template_name="windows", config=cfg, sandbox_id_prefix=run_id, wait_until_ready=True, wait_timeout=1200)
        state["sandbox_id"] = client.sandbox_id
        save()
        print("created", client.sandbox_id, flush=True)
        await ps("prepare", f"New-Item -ItemType Directory -Force '{remote}','C:/Users/win11admin/.openclaw' | Out-Null; whoami; python --version; openclaw --version")
        await sdk(client.upload_file, local_path=args.openclaw_config, remote_path="C:/Users/win11admin/.openclaw/openclaw.json")
        await sdk(client.upload_file, local_path=runtime_config, remote_path=remote + "/openclaw.json")
        await ps("gateway", f"$p=Start-Process cmd.exe -ArgumentList '/d /s /c openclaw gateway 1> {remote}/gateway.log 2>&1' -PassThru -WindowStyle Hidden; $p.Id | Set-Content '{remote}/gateway.pid'; $ready=$false; for($i=0;$i -lt 90;$i++) {{ if (Test-NetConnection 127.0.0.1 -Port 18789 -InformationLevel Quiet -WarningAction SilentlyContinue) {{$ready=$true;break}}; Start-Sleep 2 }}; if(!$ready) {{Get-Content '{remote}/gateway.log' -Tail 60; throw 'gateway not ready'}}; Write-Output 'gateway-ready'", timeout=360)
        await sdk(client.upload_file, local_path=admin_init, remote_path=remote + "/approve_openclaw_admin.py")
        await ps("admin-scope", f"python '{remote}/approve_openclaw_admin.py'; if($LASTEXITCODE -ne 0) {{throw 'admin scope approval failed'}}", timeout=180)
        await sdk(client.upload_file, local_path=args.task_tar, remote_path=remote + "/openclaw-task.tar", timeout_seconds=180)
        await ps("extract", f"tar.exe -xf '{remote}/openclaw-task.tar' -C '{remote}'; if($LASTEXITCODE -ne 0) {{throw 'tar failed'}}; $task=Get-ChildItem '{remote}' -Filter harness_automation.py -Recurse | Select-Object -First 1; if(!$task) {{throw 'harness missing'}}; $task.Directory.FullName | Set-Content '{remote}/task-path.txt'; $cfg=Join-Path $task.Directory.FullName 'config/config_session.json'; if(!(Test-Path $cfg)) {{$cfg=Join-Path $task.Directory.FullName 'configs/config_session.json'}}; if(!(Test-Path $cfg)) {{throw 'session config missing'}}; $cfg | Set-Content '{remote}/config-path.txt'; Write-Output $cfg")
        await sdk(client.upload_file, local_path=scope_patch, remote_path=remote + "/patch_openclaw_scope.py")
        await ps("scope-patch", f"$task=(Get-Content '{remote}/task-path.txt').Trim(); python '{remote}/patch_openclaw_scope.py' (Join-Path $task 'src/openclaw_client.py'); if($LASTEXITCODE -ne 0) {{throw 'scope patch failed'}}; Select-String -Path (Join-Path $task 'src/openclaw_client.py') -Pattern 'operator.admin'")
        await ps("launch", f"""$task=(Get-Content '{remote}/task-path.txt').Trim(); $cfg=(Get-Content '{remote}/config-path.txt').Trim(); $bat='@echo off'+[Environment]::NewLine+'cd /d "'+$task+'"'+[Environment]::NewLine+'set PYTHONIOENCODING=utf-8'+[Environment]::NewLine+'python harness_automation.py --config "'+$cfg+'" > run.log 2>&1'+[Environment]::NewLine+'echo %errorlevel% > "{remote}/exit-code.txt"'; Set-Content -Path '{remote}/run.cmd' -Value $bat -Encoding ASCII; Start-Process cmd.exe -ArgumentList '/d /c {remote}/run.cmd' -WorkingDirectory $task -WindowStyle Hidden; Write-Output 'harness-started'""")
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            data = await ps("poll", f"if(Test-Path '{remote}/exit-code.txt') {{Write-Output ('DONE:'+(Get-Content '{remote}/exit-code.txt').Trim())}} else {{Write-Output 'RUNNING'}}")
            print("data:", data)
            if "DONE:" in str(data):
                break
            await asyncio.sleep(20)
        else:
            raise TimeoutError("harness exceeded timeout")
        task_data = await ps("task-path", f"Write-Output ((Get-Content '{remote}/task-path.txt').Trim())")
        task_path = task_data.get("stdout", "").strip()
        await download("run.log", task_path + "/run.log")
        await download("traj_stats_result.json", task_path + "/logs/traj_stats_result.json")
        await download("exit-code.txt", remote + "/exit-code.txt")
        await ps("archive", f"$task=(Get-Content '{remote}/task-path.txt').Trim(); tar.exe -cf '{remote}/task-results.tar' -C $task logs run.log; if($LASTEXITCODE -ne 0) {{throw 'archive failed'}}")
        await download("task-results.tar", remote + "/task-results.tar")
        exit_code = int((out / "exit-code.txt").read_text(encoding="utf-8-sig").strip())
        log = (out / "run.log").read_text(encoding="utf-8", errors="replace")
        state.update(exit_code=exit_code, stats=json.loads((out / "traj_stats_result.json").read_text(encoding="utf-8-sig")))
        state["ok"] = exit_code == 0 and "Traceback (most recent call last)" not in log
        if not state["ok"]:
            raise RuntimeError("harness failed; inspect run.log and task-results.tar")
    except BaseException as exc:
        state["error"] = str(exc)
        raise
    finally:
        if client is not None:
            try:
                await download("gateway.log", remote + "/gateway.log")
            except Exception as exc:
                state["gateway_log_error"] = str(exc)
            try:
                # No attach/list/delete paths: this client comes only from create_sandbox above.
                state["close_result"] = await close_sandbox(client=client)
                state["closed"] = True
                print("closed", client.sandbox_id, flush=True)
            finally:
                save()
        else:
            save()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--openclaw-config", required=True)
    parser.add_argument("--task-tar", required=True)
    parser.add_argument("--output", required=True, help="New directory for this run's evidence")
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--timeout", type=int, default=6000)
    asyncio.run(main(parser.parse_args()))

