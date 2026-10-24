"""麦当劳 MCP 客户端（Streamable HTTP + JSON-RPC 2.0）。

只依赖标准库。设计要点（对应 PRD §1.4.3 / §1.4.7）：
- 工具返回的是「说明文字 + JSON + 尾部提示」的混合文本，必须剥出 JSON 再解析；
- 会话 id 由服务端通过 Mcp-Session-Id 头下发，需在后续请求带上；
- 鉴权类失败与非鉴权类失败要能区分，供「连接失效 / 查询失败」两种状态使用。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_ENDPOINT = "https://mcp.mcd.cn"


class MCDError(RuntimeError):
    """工具调用返回 success=false 或响应不可解析。"""

    def __init__(self, tool: str, code, message: str, payload: dict | None = None):
        self.tool = tool
        self.code = code
        self.message = message
        self.payload = payload or {}
        super().__init__(f"{tool} 失败(code={code}): {message}")


class MCDAuthError(MCDError):
    """凭据无效或过期（对应 PRD §1.4.7 的「连接失效」）。"""


def extract_json(text: str) -> dict | None:
    """从混合文本中取出第一个完整 JSON 对象。"""
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


@dataclass
class MCDClient:
    token: str
    endpoint: str = DEFAULT_ENDPOINT
    timeout: float = 30.0
    _session_id: str | None = field(default=None, init=False, repr=False)
    _opened: bool = field(default=False, init=False, repr=False)

    # ---------- 传输层 ----------

    def _post(self, payload: dict, *, notify: bool = False) -> dict | None:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid
                ctype = resp.headers.get("Content-Type", "")
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            msg = f"HTTP {exc.code}: {body}"
            if exc.code in (401, 403):
                raise MCDAuthError(payload.get("method", "?"), exc.code, msg) from exc
            raise MCDError(payload.get("method", "?"), exc.code, msg) from exc
        except urllib.error.URLError as exc:
            raise MCDError(payload.get("method", "?"), "network", str(exc.reason)) from exc

        if notify:
            return None
        if "text/event-stream" in ctype:
            data_lines = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
            raw = data_lines[-1] if data_lines else ""
        if not raw.strip():
            return None
        return json.loads(raw)

    def open(self) -> None:
        if self._opened:
            return
        if not self.token:
            raise MCDAuthError("initialize", "no-token", "未配置凭据")
        self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "mcd-order-assistant", "version": "0.1"},
                },
            }
        )
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, notify=True)
        self._opened = True

    # ---------- 工具层 ----------

    def call(self, tool: str, arguments: dict | None = None) -> dict:
        """调用工具，返回完整 payload（含 success/code/data）。成功与否由调用方判断。"""
        self.open()
        resp = self._post(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments or {}},
            }
        )
        result = (resp or {}).get("result") or {}
        if result.get("isError"):
            raise MCDError(tool, "tool-error", json.dumps(result, ensure_ascii=False)[:300])

        content = result.get("content") or []
        if content and content[0].get("type") == "text":
            parsed = extract_json(content[0]["text"])
            if parsed is None:
                raise MCDError(tool, "unparsable", content[0]["text"][:300])
            return parsed
        if isinstance(result.get("structuredContent"), dict):
            return result["structuredContent"]
        raise MCDError(tool, "empty", "工具未返回可解析内容")

    def data(self, tool: str, arguments: dict | None = None):
        """调用工具并只取 data；success=false 时抛错。"""
        payload = self.call(tool, arguments)
        if payload.get("success") is False:
            code = payload.get("code")
            msg = payload.get("message") or ""
            if code in (600001, 600002, 401) or "登录" in msg or "token" in msg.lower():
                raise MCDAuthError(tool, code, msg, payload)
            raise MCDError(tool, code, msg, payload)
        return payload.get("data")


def probe_status(exc: Exception) -> str:
    """把异常映射成 PRD §1.4.7 的四态之一（用于页面展示）。"""
    if isinstance(exc, MCDAuthError):
        return "连接失效"
    if isinstance(exc, MCDError):
        return "查询失败"
    return "查询失败"
