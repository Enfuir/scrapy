#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scrapy MCP Server — lightweight wrapper around Scrapy CLI as MCP."""

import asyncio
import json
import subprocess
import sys
import tempfile
import os

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
    HAS_MCP = True
except ImportError:
    HAS_MCP = False
    print("MCP not installed. Install with: pip install mcp", file=sys.stderr)
    sys.exit(1)


server = Server("scrapy")


def _scrapy(args: list, timeout=30) -> str:
    """Run scrapy CLI command and return stdout+stderr."""
    result = subprocess.run(
        [sys.executable, "-m", "scrapy"] + args,
        capture_output=True, text=True, timeout=timeout
    )
    return (result.stdout + "\n" + result.stderr).strip() or result.stdout


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="fetch",
            description="Fetch a URL using Scrapy downloader (respects robots.txt, user-agent, cookies, middleware). Returns HTML.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "redirect": {"type": "boolean", "default": True, "description": "Follow redirects"},
                },
            },
        ),
        Tool(
            name="parse_item",
            description="Fetch URL and extract data using CSS or XPath selectors.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch and parse"},
                    "css": {"type": "string", "description": "CSS selector (e.g. 'h1::text', '.item .title::text')"},
                    "xpath": {"type": "string", "description": "XPath selector (e.g. '//h1/text()', '//div[@class=\"item\"]')"},
                    "all": {"type": "boolean", "default": False, "description": "Return all matches (not just first)"},
                },
            },
        ),
        Tool(
            name="shell_eval",
            description="Evaluate Python code in Scrapy shell against a URL. Use response.css(), response.xpath(), response.re()",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to open in shell"},
                    "code": {"type": "string", "description": "Python expression to evaluate (e.g. 'response.css(\"title::text\").get()')"},
                },
            },
        ),
        Tool(
            name="list_spiders",
            description="List all available Scrapy spiders in the current project.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="run_spider",
            description="Run a Scrapy spider by name (from a project directory).",
            inputSchema={
                "type": "object",
                "properties": {
                    "spider": {"type": "string", "description": "Spider name"},
                    "url": {"type": "string", "description": "Optional start URL override"},
                    "output": {"type": "string", "description": "Output file (JSON, JSON Lines, CSV, etc.)"},
                },
            },
        ),
        Tool(
            name="version",
            description="Get Scrapy version info.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


def _run_spider_code(spider_name: str, start_url: str, selector_type: str, selector: str, get_all: bool):
    """Generate and run a temporary spider."""
    method_map = {
        "css": f"response.css({selector!r}).{'getall()' if get_all else 'get()'}",
        "xpath": f"response.xpath({selector!r}).{'getall()' if get_all else 'get()'}",
    }
    method = method_map.get(selector_type, "response.text")
    if isinstance(method, list):
        method_expr = f"response.css({selector!r}).getall()" if selector_type == "css" else f"response.xpath({selector!r}).getall()"
    else:
        method_expr = method_map.get(selector_type, "response.text[:500]")

    code = f'''
import scrapy
import json

class {spider_name}(scrapy.Spider):
    name = "_temp_parse"
    start_urls = [{json.dumps(start_url)}]

    def parse(self, response):
        result = {method_expr}
        if isinstance(result, list):
            result = [str(x).strip() for x in result if str(x).strip()]
        elif result is None:
            result = ""
        else:
            result = str(result).strip()
        print("SCRAPY_OUTPUT:", json.dumps(result, ensure_ascii=False))
'''

    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(code)
        tmp = f.name

    try:
        result = subprocess.run(
            [sys.executable, "-m", "scrapy", "runspider", tmp, "--nolog", "-s", "LOG_LEVEL=ERROR"],
            capture_output=True, text=True, timeout=30
        )
        output = result.stdout + result.stderr
        if "SCRAPY_OUTPUT:" in output:
            parts = output.split("SCRAPY_OUTPUT:")
            try:
                return json.loads(parts[-1].split("\\n")[0].strip())
            except:
                pass
        return output[:3000] or "(no output)"
    finally:
        os.unlink(tmp)


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "fetch":
            url = arguments["url"]
            redirect = arguments.get("redirect", True)
            output = _scrapy(["fetch", "--nolog", url])
            if not output:
                output = "(no output — URL may be blocked or unreachable)"
            return [TextContent(type="text", text=output[:8000])]

        elif name == "parse_item":
            url = arguments["url"]
            css = arguments.get("css")
            xpath = arguments.get("xpath")
            get_all = arguments.get("all", False)

            sel_type = "css" if css else "xpath" if xpath else None
            sel = css or xpath or None

            if not sel:
                return [TextContent(type="text", text="Error: provide css or xpath selector")]

            result = _run_spider_code("_parse", url, sel_type, sel, get_all)
            text = json.dumps(result, ensure_ascii=False, indent=2) if isinstance(result, (list, str)) else str(result)
            return [TextContent(type="text", text=text)]

        elif name == "shell_eval":
            url = arguments["url"]
            code = arguments.get("code", "response.text[:200]")

            spider_code = f'''
import scrapy
import json

class _ShellSpider(scrapy.Spider):
    name = "_shell"
    start_urls = [{json.dumps(url)}]

    def parse(self, response):
        try:
            result = {code}
            if isinstance(result, list):
                result = [str(x).strip() for x in result]
            elif result is None:
                result = ""
            else:
                result = str(result).strip()
        except Exception as e:
            result = f"Error: {{str(e)}}"
        print("SHELL_OUTPUT:", json.dumps(result, ensure_ascii=False))
'''
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
                f.write(spider_code)
                tmp = f.name

            try:
                result = subprocess.run(
                    [sys.executable, "-m", "scrapy", "runspider", tmp, "--nolog", "-s", "LOG_LEVEL=ERROR"],
                    capture_output=True, text=True, timeout=30
                )
                output = result.stdout + result.stderr
                if "SHELL_OUTPUT:" in output:
                    parts = output.split("SHELL_OUTPUT:")
                    try:
                        parsed = json.loads(parts[-1].split("\\n")[0].strip())
                        return [TextContent(type="text", text=json.dumps(parsed, ensure_ascii=False, indent=2))]
                    except:
                        pass
                return [TextContent(type="text", text=output[:5000] or "(no output)")]
            finally:
                os.unlink(tmp)

        elif name == "list_spiders":
            output = _scrapy(["list"])
            return [TextContent(type="text", text=output or "(no spiders found — run from a Scrapy project directory)")]

        elif name == "run_spider":
            spider = arguments["spider"]
            url = arguments.get("url")
            output_file = arguments.get("output")
            args = ["crawl", spider]
            if url:
                args.append(url)
            if output_file:
                args.extend(["-o", output_file])
            out = _scrapy(args, timeout=60)
            return [TextContent(type="text", text=out[:5000] or "Done.")]

        elif name == "version":
            out = _scrapy(["version"])
            return [TextContent(type="text", text=out)]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except subprocess.TimeoutExpired:
        return [TextContent(type="text", text="Timeout after 30s")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {str(e)}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
