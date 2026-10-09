#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦麦懂你 (mcd-chat-buddy) · 麦当劳 MCP 客户端

纯 Python 标准库实现的 MCP Streamable HTTP 客户端。
因为比赛要求「只用标准库」，所以不能用官方 mcp SDK，这里自己把
JSON-RPC 2.0 握手 + SSE 分帧那套东西实现了一遍。

环境变量
--------
MCD_MCP_TOKEN   必填。麦当劳 MCP Token。缺失时进入演示模式（读 mock/ 目录）。
MCD_MCP_URL     选填。默认 https://mcp.mcd.cn

命令行
------
    python3 scripts/mcp_client.py --list                    # 列出全部工具
    python3 scripts/mcp_client.py --list --json             # 以 JSON 输出工具清单
    python3 scripts/mcp_client.py --call now-time-info      # 调用单个工具
    python3 scripts/mcp_client.py --call query-nearby-stores '{"address":"上海市徐汇区"}'
    python3 scripts/mcp_client.py --dump-mock               # 拉取只读工具真实返回存进 mock/

作为库使用
----------
    from mcp_client import McDMcpClient
    c = McDMcpClient()
    if c.demo_mode:
        ...
    tools = c.list_tools()
    result = c.call_tool("query-meals", {"storeCode": "..."})
    text = text_of(result)
"""

from __future__ import annotations

import argparse
import http.client
import io
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

DEFAULT_URL = "https://mcp.mcd.cn"
CLIENT_NAME = "mcd-chat-buddy"
CLIENT_VERSION = "0.1.0"

# 服务端在 initialize 时返回 2025-06-18，我们按同一版本说话
PROTOCOL_VERSION = "2025-06-18"

# 兼容服务端可能返回的更老版本，按优先级降级重试
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

REPO_ROOT = Path(__file__).resolve().parent.parent
MOCK_DIR = REPO_ROOT / "mock"

# 第 1 步要跑通的只读工具（不含 create-order / cancel-order 等写操作）
READONLY_TOOLS = (
    "now-time-info",
    "query-nearby-stores",
    "query-meals",
    "query-meal-detail",
    "list-nutrition-foods",
    "query-store-coupons",
    "calculate-price",
)


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------


class McpError(RuntimeError):
    """所有 MCP 相关错误的基类。"""


class McpTransportError(McpError):
    """网络层失败（DNS、超时、连接被拒）。"""


class McpAuthError(McpError):
    """401：Token 无效 / 过期 / 未提供。"""


class McpRateLimitError(McpError):
    """429：超过 600 次/分钟限流。"""


class McpProtocolError(McpError):
    """JSON-RPC 层报错，或响应不符合预期。"""


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


class McDMcpClient:
    """麦当劳 MCP 的 Streamable HTTP 客户端。

    一次会话的生命周期：
        initialize  ->  服务器可能下发 Mcp-Session-Id
        notifications/initialized  ->  握手完成
        tools/list / tools/call    ->  后续所有请求都带上 Session-Id
    """

    def __init__(
        self,
        token: str | None = None,
        url: str | None = None,
        timeout: int = 60,
        verbose: bool = False,
    ) -> None:
        self.token = (token if token is not None else os.environ.get("MCD_MCP_TOKEN", "")).strip()
        self.url = (url or os.environ.get("MCD_MCP_URL") or DEFAULT_URL).strip()
        self.timeout = timeout
        self.verbose = verbose

        self.session_id: str | None = None
        self.server_info: dict | None = None
        self.negotiated_version: str | None = None
        self.capabilities: dict | None = None

        self._next_request_id = 1
        self._initialized = False
        #: 复用的长连接（见 `_connection()`，省掉每次 1.4–2 秒的 TCP+TLS 建连）
        self._conn: http.client.HTTPSConnection | None = None
        self._conn_path = "/"

    # -- 基础属性 ---------------------------------------------------------

    @property
    def demo_mode(self) -> bool:
        """没有 Token 时进入演示模式：所有调用改读 mock/ 目录。"""
        return not self.token

    def _log(self, *parts: object) -> None:
        if self.verbose:
            print("[mcp]", *parts, file=sys.stderr)

    # -- HTTP -------------------------------------------------------------

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            # 必须同时声明接受两种，否则规范允许服务端直接 406
            "Accept": "application/json, text/event-stream",
            # 关掉压缩，标准库不自动解 gzip
            "Accept-Encoding": "identity",
            "User-Agent": f"{CLIENT_NAME}/{CLIENT_VERSION} (python-stdlib)",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self._initialized:
            headers["MCP-Protocol-Version"] = self.negotiated_version or PROTOCOL_VERSION
        return headers

    def _connection(self) -> http.client.HTTPSConnection:
        """拿一条**可复用**的 HTTPS 连接。

        为什么要自己管连接：`urllib.urlopen` 每次调用都重新建 TCP + TLS，
        这台机器上实测**建连 1.4–2.1 秒**，而服务端真正处理只要 **0.7 秒** ——
        九成时间花在握手上。保持长连接后，同进程的后续调用降到 0.7 秒左右。

        连接断了（对端 keep-alive 超时被回收）会自动重连一次，见 `_send()`。
        """
        if self._conn is None:
            parts = urllib.parse.urlsplit(self.url)
            if parts.scheme != "https":
                raise McpTransportError(f"只支持 https，收到 {self.url!r}")
            if not parts.hostname:
                raise McpTransportError(f"URL 里没有主机名：{self.url!r}")
            self._conn = http.client.HTTPSConnection(
                parts.hostname,
                parts.port or 443,
                timeout=self.timeout,
                context=ssl.create_default_context(),
            )
            self._conn_path = parts.path or "/"
            if parts.query:
                self._conn_path += "?" + parts.query
        return self._conn

    def _close_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def close(self) -> None:
        """主动收掉连接。脚本退出前调一下，别把半开的 socket 留在那。"""
        self._close_conn()

    def __enter__(self) -> "McDMcpClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _send(self, body: bytes, headers: dict[str, str]):
        """POST 一次，返回 (状态码, 响应头, 原始字节)。

        复用连接；如果对端已经把连接关了（keep-alive 到期很常见），
        重连一次再发 —— 只重试一次，避免网络真挂了时死循环。
        """
        last_error: Exception | None = None
        for attempt in (0, 1):
            conn = self._connection()
            try:
                conn.request("POST", self._conn_path, body=body, headers=headers)
                response = conn.getresponse()
                status, resp_headers = response.status, response.headers
                return status, resp_headers, response.read()
            except (http.client.HTTPException, OSError) as exc:
                last_error = exc
                self._close_conn()
                if attempt == 0:
                    self._log("连接不可用，重连一次：", exc)
        raise McpTransportError(f"连不上 {self.url}：{last_error}") from last_error

    def _post(self, payload: dict) -> dict | None:
        """发一次 POST，返回解析后的 JSON-RPC 消息（通知类请求可能返回 None）。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._log("->", payload.get("method"), "id=", payload.get("id"))

        status, resp_headers, raw = self._send(body, self._build_headers())

        if status >= 400:
            # 复用已有的错误翻译：伪造一个 HTTPError，省得两套逻辑各写一遍
            raise self._http_error(urllib.error.HTTPError(
                self.url, status, resp_headers.get("Reason") or "",
                resp_headers, io.BytesIO(raw)))

        session_id = resp_headers.get("Mcp-Session-Id")
        if session_id:
            self.session_id = session_id
            self._log("session:", session_id)
        content_type = (resp_headers.get("Content-Type") or "").lower()

        if not raw.strip():
            # 通知类请求正常返回空体（204/202）
            return None

        text = raw.decode("utf-8", errors="replace").strip()
        if "text/event-stream" in content_type or text.startswith("event:") or text.startswith("data:"):
            return self._parse_sse(text, payload.get("id"))
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise McpProtocolError(
                f"响应既不是 JSON 也不是 SSE，前 200 字：{text[:200]!r}"
            ) from exc

    @staticmethod
    def _parse_sse(text: str, expected_id: object) -> dict | None:
        """从 SSE 流里挑出 id 匹配的那条 JSON-RPC 消息。

        规范允许服务端在一个流里先推 notification 再推 response，
        所以不能只取第一条，得按 id 对上。
        """
        events: list[list[str]] = []
        buffer: list[str] = []
        for line in text.splitlines():
            if line.startswith(":"):
                continue  # SSE 注释/心跳
            if line.startswith("data:"):
                buffer.append(line[5:].lstrip())
            elif not line.strip():
                if buffer:
                    events.append(buffer)
                    buffer = []
        if buffer:
            events.append(buffer)

        fallback: dict | None = None
        for chunk in events:
            try:
                message = json.loads("\n".join(chunk))
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if "id" in message and message.get("id") == expected_id:
                return message
            if fallback is None and ("result" in message or "error" in message):
                fallback = message
        return fallback

    def _http_error(self, exc: urllib.error.HTTPError) -> McpError:
        try:
            detail = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:  # pragma: no cover - 读取失败就用状态码兜底
            detail = ""
        detail = detail[:300]

        if exc.code == 401:
            return McpAuthError(
                "Token 无效、已过期或未提供。请检查环境变量 MCD_MCP_TOKEN，"
                f"或到 https://open.mcd.cn/mcp 重新申请。原始响应：{detail}"
            )
        if exc.code == 429:
            return McpRateLimitError(
                f"触发限流（每分钟上限 600 次）。稍后重试。原始响应：{detail}"
            )
        if exc.code == 404:
            return McpProtocolError(
                f"404，接入地址不对。麦当劳 MCP 的地址就是 https://mcp.mcd.cn，"
                f"不要再加 /mcp 之类的路径。原始响应：{detail}"
            )
        return McpProtocolError(f"HTTP {exc.code}：{detail}")

    # -- JSON-RPC ---------------------------------------------------------

    def _rpc(self, method: str, params: dict | None = None, *, notify: bool = False):
        payload: dict = {"jsonrpc": "2.0", "method": method}
        request_id = None
        if not notify:
            request_id = self._next_request_id
            self._next_request_id += 1
            payload["id"] = request_id
        if params is not None:
            payload["params"] = params

        message = self._post(payload)
        if message is None:
            if notify:
                return None
            raise McpProtocolError(f"{method} 没有返回任何消息")
        if "error" in message and message["error"]:
            error = message["error"]
            raise McpProtocolError(
                f"{method} 返回错误 [{error.get('code')}] {error.get('message')}"
            )
        return message.get("result")

    # -- 握手 -------------------------------------------------------------

    def initialize(self) -> dict:
        """完成 MCP 握手。重复调用是幂等的。"""
        if self._initialized:
            return {
                "serverInfo": self.server_info,
                "protocolVersion": self.negotiated_version,
            }

        attempts = SUPPORTED_PROTOCOL_VERSIONS
        last_error: Exception | None = None
        for index, version in enumerate(attempts):
            try:
                result = self._rpc(
                    "initialize",
                    {
                        "protocolVersion": version,
                        "capabilities": {},
                        "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                    },
                )
                break
            except McpProtocolError as exc:
                last_error = exc
                if index == len(attempts) - 1:
                    raise
                self._log(f"协议版本 {version} 被拒，降级重试：{exc}")
        else:  # pragma: no cover - 循环必定 break 或 raise
            raise McpProtocolError(str(last_error))

        result = result or {}
        self.server_info = result.get("serverInfo")
        self.capabilities = result.get("capabilities")
        self.negotiated_version = result.get("protocolVersion") or version

        # 握手最后一步：通知服务端「我准备好了」。这是 notification，没有 id，正常返回空体。
        self._initialized = True
        self._rpc("notifications/initialized", {}, notify=True)
        return result

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            self.initialize()

    # -- 工具 -------------------------------------------------------------

    def list_tools(self) -> list[dict]:
        """列出服务端全部可用工具，自动翻页。"""
        self._ensure_initialized()
        tools: list[dict] = []
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self._rpc("tools/list", params) or {}
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        """调用一个工具，返回原始 result（含 content / isError / structuredContent）。"""
        self._ensure_initialized()
        return self._rpc(
            "tools/call", {"name": name, "arguments": arguments or {}}
        ) or {}

    # -- 演示模式 ---------------------------------------------------------

    def mock_path(self, tool_name: str) -> Path:
        return MOCK_DIR / f"{tool_name}.json"

    def call_tool_or_mock(self, name: str, arguments: dict | None = None) -> tuple[dict, bool]:
        """有 Token 就真调，没有就回退读 mock/。返回 (结果, 是否来自 mock)。"""
        if self.demo_mode:
            return self.load_mock(name), True
        return self.call_tool(name, arguments), False

    def load_mock(self, tool_name: str) -> dict:
        path = self.mock_path(tool_name)
        if not path.exists():
            raise McpError(
                f"演示模式缺少数据：{path}。请在有 Token 的机器上跑一次 "
                f"`python3 scripts/mcp_client.py --dump-mock` 生成。"
            )
        return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 结果解析小工具
# --------------------------------------------------------------------------


def text_of(result: dict) -> str:
    """把 tools/call 的 content 数组里的 text 片段拼成一段字符串。"""
    if not isinstance(result, dict):
        return ""
    parts: list[str] = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text") or "")
    return "\n".join(parts)


def structured_of(result: dict):
    """优先取结构化结果；没有就把 text 当 JSON 解析，再不行返回 None。"""
    if not isinstance(result, dict):
        return None
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    raw = text_of(result).strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def find_json_blob(text: str):
    """从一段夹着说明文字的回复里，抠出第一个看起来像 JSON 的块。"""
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            candidate = text[start : end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    return None


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------


def _print_tools_table(tools: list[dict]) -> None:
    width = max((len(t.get("name", "")) for t in tools), default=10)
    print(f"共 {len(tools)} 个工具\n")
    for tool in tools:
        name = tool.get("name", "")
        desc = (tool.get("description") or "").replace("\n", " ")
        if len(desc) > 60:
            desc = desc[:59] + "…"
        print(f"  {name.ljust(width)}  {desc}")


def _cmd_list(client: McDMcpClient, as_json: bool) -> int:
    if client.demo_mode:
        print("⚠️  未检测到 MCD_MCP_TOKEN，无法在线拉取工具清单。", file=sys.stderr)
        cached = MOCK_DIR / "tools-list.json"
        if cached.exists():
            tools = json.loads(cached.read_text(encoding="utf-8")).get("tools", [])
            print(f"（下面来自缓存 {cached.name}）\n", file=sys.stderr)
        else:
            return 1
    else:
        info = client.initialize()
        print(
            f"已连接 {client.url}\n"
            f"  服务端 {info.get('serverInfo')}\n"
            f"  协议版本 {info.get('protocolVersion')}\n"
            f"  会话 ID {client.session_id or '(服务端未下发)'}\n"
        )
        tools = client.list_tools()

    if as_json:
        print(json.dumps({"tools": tools}, ensure_ascii=False, indent=2))
    else:
        _print_tools_table(tools)
    return 0


def _cmd_call(client: McDMcpClient, name: str, raw_args: str | None) -> int:
    arguments = json.loads(raw_args) if raw_args else {}
    result = client.call_tool(name, arguments)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result.get("isError") else 2


def _cmd_dump_mock(client: McDMcpClient, only: list[str] | None) -> int:
    if client.demo_mode:
        print("⚠️  没有 Token，无法生成 mock 数据。", file=sys.stderr)
        return 1

    MOCK_DIR.mkdir(parents=True, exist_ok=True)
    info = client.initialize()
    (MOCK_DIR / "tools-list.json").write_text(
        json.dumps(
            {"serverInfo": info.get("serverInfo"), "tools": client.list_tools()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"✓ tools-list.json（{len(client.list_tools())} 个工具）")

    for name in only or READONLY_TOOLS:
        # 这些工具都带必填参数，没参数时先跳过，由外部按需单独调用
        try:
            result = client.call_tool(name, {})
        except McpError as exc:
            print(f"· {name}  跳过：{str(exc)[:120]}")
            continue
        (MOCK_DIR / f"{name}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"✓ {name}.json")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="麦当劳 MCP 客户端（纯标准库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="列出全部可用工具")
    group.add_argument("--call", metavar="TOOL", help="调用指定工具")
    group.add_argument("--dump-mock", action="store_true", help="拉取只读工具真实返回并存入 mock/")
    parser.add_argument("args", nargs="?", help="--call 时传给工具的参数（JSON 字符串）")
    parser.add_argument("--json", action="store_true", help="--list 时输出原始 JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印握手细节")
    opts = parser.parse_args(argv)

    client = McDMcpClient(verbose=opts.verbose)
    try:
        if opts.list:
            return _cmd_list(client, opts.json)
        if opts.call:
            return _cmd_call(client, opts.call, opts.args)
        return _cmd_dump_mock(client, None)
    except McpError as exc:
        print(f"\n✗ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
