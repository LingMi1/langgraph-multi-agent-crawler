"""
MCP Client 演示 — 拉起 tools/mcp_server.py（stdio 子进程）并调通协议全链路：
initialize → list_tools → call_tool ×3（fetch_page / quality_judge / apply_config）。

面试叙事：能讲 server 与 client 双视角（协议握手、工具发现、参数 JSON Schema
校验、错误通道 isError），而不是只会写 server 等别人来调。

运行：
  python tools/mcp_client.py                        # 默认对 zztzmjg.com 演示
  python tools/mcp_client.py https://example.com/   # 指定侦察 URL
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER_SCRIPT = str(Path(__file__).resolve().parent / "mcp_server.py")


def _text(result) -> dict:
    """CallToolResult.content[0].text → dict（约定 server 返回 JSON 字符串）。"""
    for block in (result.content or []):
        if getattr(block, "text", ""):
            return json.loads(block.text)
    return {}


async def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "http://www.zztzmjg.com/"
    params = StdioServerParameters(command=sys.executable, args=["-B", SERVER_SCRIPT])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            # ── 1. 协议握手 ──
            init = await session.initialize()
            info = getattr(init, "server_info", None) or init.serverInfo  # 2.x snake_case
            print(f"[mcp-client] 握手完成 | server={info.name} "
                  f"v{info.version} | protocol={getattr(init, 'protocol_version', None) or init.protocolVersion}")

            # ── 2. 工具发现 ──
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print(f"[mcp-client] 工具发现 | {names}")
            assert set(names) >= {"fetch_page", "apply_config", "quality_judge"}, names

            # ── 3. call_tool：侦察抓取（真实网络） ──
            r1 = await session.call_tool("fetch_page", {"url": target})
            d1 = _text(r1)
            print(f"[mcp-client] fetch_page | status={d1.get('status')} "
                  f"len={d1.get('content_len')} title={d1.get('title', '')[:40]}")

            # ── 4. call_tool：确定性质量打分 ──
            r2 = await session.call_tool("quality_judge", {
                "sample": "郑州天筑膜结构工程有限公司主营膜结构车棚设计与安装，"
                          "服务涵盖方案设计、施工安装与售后维护。",
                "criteria": "企业官网简介页",
            })
            d2 = _text(r2)
            print(f"[mcp-client] quality_judge | score={d2.get('score')} "
                  f"reason={str(d2.get('reason', ''))[:60]}")

            # ── 5. call_tool：白名单配置生成 ──
            r3 = await session.call_tool("apply_config", {"needs_js_render": True})
            d3 = _text(r3)
            print(f"[mcp-client] apply_config | {d3}")

            # ── 6. 错误通道：未知工具 → isError=True（fail-closed） ──
            r4 = await session.call_tool("no_such_tool", {})
            is_err = getattr(r4, "is_error", None)
            if is_err is None:
                is_err = r4.isError  # 旧版字段
            print(f"[mcp-client] 未知工具 | isError={is_err} | {_text(r4)}")

            print("[mcp-client] MCP 双向链路验证通过")


if __name__ == "__main__":
    asyncio.run(main())
