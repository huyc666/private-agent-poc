"""代码执行沙箱服务。

⚠️ 安全前提：本服务只能运行在按 docker-compose.sandbox.yml 锁定的容器内
（无网络、只读文件系统、CPU/内存/PID 限额、no-new-privileges）。
不要直接在宿主机上运行它来执行不可信代码。

两个端点：
- POST /run       执行任意 Python 代码片段（run_python_code 工具）
- POST /run_tool  执行 custom_tools 目录中已审批的自定义工具（tools 目录以只读卷挂载）
"""
import json
import os
import re
import subprocess
import sys
import tempfile

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(title="poc-sandbox")

RUN_TIMEOUT = int(os.environ.get("SANDBOX_RUN_TIMEOUT", "15"))
MAX_OUTPUT = 8000
TOOLS_DIR = os.environ.get("SANDBOX_TOOLS_DIR", "/tools")
PACKS_DIR = os.environ.get("SANDBOX_PACKS_DIR", "/packs")
# 共享令牌（Agent 侧 SANDBOX_AUTH_TOKEN 配置同一个值才启用鉴权）。
# 未配置令牌时默认拒绝执行（fail-closed，v0.17.4 安全审计 F3）：
# 仅显式设置 SANDBOX_OPEN_MODE=true 才放行，兼容未启用鉴权的既有部署。
SANDBOX_AUTH_TOKEN = os.environ.get("SANDBOX_AUTH_TOKEN", "")
SANDBOX_OPEN_MODE = os.environ.get("SANDBOX_OPEN_MODE", "false").lower() in ("1", "true", "yes")
_TOOL_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,49}$")
_PACK_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")


def check_token(x_sandbox_token: str = Header(default="")):
    """共享令牌鉴权（fail-closed）：
    1) 配置了 SANDBOX_AUTH_TOKEN → 强制校验 X-Sandbox-Token，不匹配 401；
    2) 未配置令牌 → 默认拒绝（503），仅 SANDBOX_OPEN_MODE=true 时放行
       （兼容既有未启用鉴权的部署）。"""
    if SANDBOX_AUTH_TOKEN:
        if x_sandbox_token != SANDBOX_AUTH_TOKEN:
            raise HTTPException(status_code=401, detail="unauthorized")
        return
    if not SANDBOX_OPEN_MODE:
        raise HTTPException(status_code=503, detail="sandbox auth token not configured")

# 在隔离子进程里加载工具文件并调用同名函数，结果以标记行输出
# argv: name, 工具文件所在目录, args_json
_RUNNER = r'''
import importlib.util, json, sys
name, tools_dir, args_json = sys.argv[1], sys.argv[2], sys.argv[3]
spec = importlib.util.spec_from_file_location("ct_" + name, tools_dir + "/" + name + ".py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
fn = getattr(mod, name)
args = json.loads(args_json)
try:
    r = fn(**args)
except Exception as e:
    print("__RESULT__" + json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
else:
    print("__RESULT__" + json.dumps({"ok": True, "result": str(r)}, ensure_ascii=False))
'''


class RunRequest(BaseModel):
    code: str


class ToolRequest(BaseModel):
    name: str
    args: dict = {}
    pack: str = ""   # 领域包名；空 = custom_tools 目录
    env: dict = {}   # 包级环境变量（注入隔离子进程，仅接受合法变量名）


@app.get("/health")
def health():
    return {"status": "ok", "timeout": RUN_TIMEOUT,
            "tools_dir": TOOLS_DIR, "packs_dir": PACKS_DIR}


def _run_isolated(argv: list[str], extra_env: dict | None = None) -> dict:
    """把脚本写入临时文件后在 -I 隔离子进程中执行。extra_env 为附加环境变量。
    子进程环境剔除沙箱自身的鉴权配置（SANDBOX_AUTH_TOKEN 等）——执行的是
    不可信代码，令牌只应留在服务进程，不能让代码读走后伪造合法调用方。"""
    path = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(argv["code"])
            path = f.name
        child_env = {k: v for k, v in os.environ.items()
                     if k not in ("SANDBOX_AUTH_TOKEN", "SANDBOX_OPEN_MODE")}
        proc = subprocess.run(
            [sys.executable, "-I", path] + argv.get("extra", []),
            capture_output=True, text=True, timeout=RUN_TIMEOUT,
            cwd=tempfile.gettempdir(),
            env={**child_env, **(extra_env or {})},
        )
        output = (proc.stdout + proc.stderr)[-MAX_OUTPUT:]
        return {"output": output, "returncode": proc.returncode}
    except subprocess.TimeoutExpired:
        return {"error": f"执行超时（>{RUN_TIMEOUT}s），已终止"}
    except Exception as e:
        return {"error": f"沙箱内部错误: {e}"}
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


@app.post("/run", dependencies=[Depends(check_token)])
def run(req: RunRequest):
    if len(req.code) > 20_000:
        return {"error": "代码过长（>20KB），已拒绝"}
    return _run_isolated({"code": req.code})


@app.post("/run_tool", dependencies=[Depends(check_token)])
def run_tool(req: ToolRequest):
    """执行已审批的工具。工具代码来自 Agent 侧人工审批落盘的文件，
    此处只做名称合法性/存在性校验，执行本身走与 /run 相同的隔离子进程。
    pack 为空时从 custom_tools 目录取，否则从领域包的 tools/ 子目录取。"""
    if not _TOOL_NAME.match(req.name):
        return {"error": "非法工具名"}
    if req.pack:
        if not _PACK_NAME.match(req.pack):
            return {"error": "非法领域包名"}
        tool_dir = os.path.join(PACKS_DIR, req.pack, "tools")
    else:
        tool_dir = TOOLS_DIR
    if not os.path.isfile(os.path.join(tool_dir, req.name + ".py")):
        return {"error": f"工具「{req.name}」不存在"}
    try:
        args_json = json.dumps(req.args, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        return {"error": f"参数无法序列化: {e}"}
    if len(args_json) > 8000:
        return {"error": "参数过大（>8KB），已拒绝"}
    # 包级环境变量：只接受合法变量名，限制数量与值长度（防注入/防滥用）
    env_extra = {}
    _ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    for k, v in (req.env or {}).items():
        if not _ENV_KEY.match(str(k)):
            return {"error": f"非法环境变量名: {k}"}
        env_extra[str(k)] = str(v)[:2000]
    if len(env_extra) > 50:
        return {"error": "环境变量过多（>50），已拒绝"}
    res = _run_isolated({"code": _RUNNER, "extra": [req.name, tool_dir, args_json]},
                        extra_env=env_extra)
    if "error" in res:
        return res
    for line in res.get("output", "").splitlines():
        if line.startswith("__RESULT__"):
            try:
                return json.loads(line[len("__RESULT__"):])
            except json.JSONDecodeError:
                break
    return {"error": f"工具未返回结果（输出: {res.get('output', '')[-500:]}）"}
