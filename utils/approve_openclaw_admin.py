import asyncio
import json
import os
import pathlib
import subprocess
import sys

from openclaw_sdk.gateway.protocol import ProtocolGateway

SCOPES = ["operator.read", "operator.write", "operator.admin"]
ALLOWED = set(SCOPES)
CLI = r"C:\ProgramData\OmniEnv\bin\openclaw.cmd"


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
