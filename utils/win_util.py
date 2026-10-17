"""Windows 沙箱 PowerShell -EncodedCommand 命令构造。

参考 win_demo.py 的 ps(): 固定前缀设置 $ErrorActionPreference 和
OPENCLAW_CONFIG_PATH / OPENCLAW_STATE_DIR / OPENCLAW_GATEWAY_TOKEN, 再拼调用方
脚本, 整体 base64(utf-16le) 编码后走 powershell -EncodedCommand。
"""
from __future__ import annotations

import base64


def ps_command(script: str, remote: str) -> list[str]:
    """把一段 PowerShell 脚本包成 -EncodedCommand 的 exec 命令列表。

    remote: 沙箱内放置 openclaw.json 的目录(前向斜杠)。前缀会把
    $env:OPENCLAW_CONFIG_PATH / OPENCLAW_STATE_DIR 指到该目录, 并从
    openclaw.json 读出 gateway.auth.token 注入 OPENCLAW_GATEWAY_TOKEN。
    """
    prefix = (
        "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
        f"$env:OPENCLAW_CONFIG_PATH='{remote}/openclaw.json'; "
        f"$env:OPENCLAW_STATE_DIR='{remote}'; "
        f"if(Test-Path $env:OPENCLAW_CONFIG_PATH) {{"
        f"$oc=Get-Content $env:OPENCLAW_CONFIG_PATH -Raw | ConvertFrom-Json; "
        f"$env:OPENCLAW_GATEWAY_TOKEN=$oc.gateway.auth.token}}; "
    )
    encoded = base64.b64encode((prefix + script).encode("utf-16le")).decode()
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


# ---- Windows 远程文件读取: base64 通道 -------------------------------------
# PowerShell 把字符串写回 stdout 时按控制台/OEM 代码页重新编码, 中文必然乱码;
# 且 UTF-8 字节一旦被替换成 U+FFFD 就不可逆。改成让 PS 输出文件**原始字节**的
# base64, 由 Python 端解码, 绕开代码页。
_REMOTE_B64_PREFIX = "__HIVE_B64__"


def ps_read_file_b64(path: str) -> str:
    """PowerShell 片段: 输出 '<前缀><base64>'; 文件不存在/读不到则只输出前缀。

    必须显式用 FileShare.ReadWrite 打开: gateway.log 由仍在运行的 gateway 进程
    持续持有写句柄, [IO.File]::ReadAllBytes 只请求读共享, 会撞成
    "正由另一进程使用" 的 IOException。外层还有 try/catch —— $ErrorActionPreference
    ='Stop' 下任何读取异常都会中断整段脚本, 让 gateway 明明起来了却报 S001。
    """
    return (
        f"$b64=''; "
        f"if(Test-Path '{path}') {{ try {{ "
        f"$fs=[IO.File]::Open('{path}',[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::ReadWrite); "
        f"try {{ $ms=New-Object IO.MemoryStream; $fs.CopyTo($ms); "
        f"$b64=[Convert]::ToBase64String($ms.ToArray()) }} finally {{ $fs.Dispose() }} "
        f"}} catch {{ $b64='' }} }}; "
        f"Write-Output ('{_REMOTE_B64_PREFIX}'+$b64)"
    )


def decode_remote_b64(stdout: str) -> str:
    """从 stdout 里取出 base64 段并解码为文本 (优先 UTF-8, 退回 GBK)。"""
    payload = ""
    for line in (stdout or "").splitlines():
        idx = line.find(_REMOTE_B64_PREFIX)
        if idx != -1:
            payload = line[idx + len(_REMOTE_B64_PREFIX):].strip()
            break
    if not payload:
        return ""
    try:
        data = base64.b64decode(payload, validate=True)
    except Exception:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")