#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scrapy MCP Server — expose Scrapy as an MCP server."""

import asyncio
import json
import sys
import subprocess
from typing import Any

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
    HAS_MCP = True
except ImportError:
    HAS_MCP = False

from scrapy.crawler import CrawlerProcess
from scrapy.spiders import Spider
from scrapy.http import Request, Response


server = Server("scrapy")


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="fetch",
            description="Fetch a URL using Scrapy downloader with full middleware stack (user-agent, cookies, etc.)",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "headers": {"type": "object", "description": "Extra HTTP headers"},
                    "method": {"type": "string", "default": "GET", "description": "HTTP method"},
                },
            },
        ),
        Tool(
            name="parse",
            description="Fetch a URL and extract data using CSS or XPath selectors",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch and parse"},
                    "css": {"type": "string", "description": "CSS selector (e.g. 'h1::text', '.product .title')"},
                    "xpath": {"type": "string", "description": "XPath selector (e.g. '//h1/text()', '//div[@class=\"product\"]')"},
                    "many": {"type": "boolean", "default": False, "description": "Return all matches (not just first)"},
                },
            },
        ),
        Tool(
            name="shell",
            description="Run Scrapy shell command and return the result",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to open in Scrapy shell"},
                    "code": {"type": "string", "description": "Python code to execute in the shell (e.g. 'response.css(\"title::text\").get()')"},
                },
            },
        ),
        Tool(
            name="crawl",
            description="Run a Scrapy spider by name (from a Scrapy project)",
            inputSchema={
                "type": "object",
                "properties": {
                    "spider": {"type": "string", "description": "Spider name"},
                    "url": {"type": "string", "description": "Start URL (optional)"},
                    "settings": {"type": "object", "description": "Scrapy settings overrides"},
                },
            },
        ),
        Tool(
            name="list_spiders",
            description="List available Scrapy spiders in a project",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


def _run_scrapy_cmd(args: list) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "scrapy"] + args,
        capture_output=True, text=True, cwd=None
    )
    return result.stdout + result.stderr


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "fetch":
            url = arguments["url"]
            method = arguments.get("method", "GET")
            headers = arguments.get("headers", {})
            cmd = [sys.executable, "-m", "scrapy", "fetch", "--nolog", url]
            if method != "GET":
                cmd.extend(["--method", method])
            for k, v in headers.items():
                cmd.extend(["-H", f"{k}: {v}"])
            output = _run_scrapy_cmd(cmd)
            return [TextContent(type="text", text=output[:5000])]

        elif name == "shell":
            url = arguments["url"]
            code = arguments.get("code", "response.text[:500]")
            # Use scrapy shell in non-interactive mode
            shell_code = f"""
import scrapy
from scrapy.http import Request
from scrapy.shell import inspect_response
from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings

settings = get_project_settings()
process = CrawlerProcess(settings)

class TempSpider(scrapy.Spider):
    name = '_temp'
    def parse(self, response):
        self.logger.info(response.text[:100])
        import json
        result = {{
            'url': response.url,
            'status': response.status,
            'headers': dict(response.headers),
            'text_preview': response.text[:1000],
        }}
        print('SCRAPY_RESULT:', json.dumps(result))

process.crawl(TempSpider, start_urls=[{json.dumps(url)}])
process.start()
"""
            result = subprocess.run(
                [sys.executable, "-c", shell_code],
                capture_output=True, text=True, timeout=30
            )
            output = result.stdout + result.stderr
            return [TextContent(type="text", text=output[:5000])]

        elif name == "parse":
            url = arguments["url"]
            css = arguments.get("css")
            xpath = arguments.get("xpath")
            many = arguments.get("many", False)

            # Build scrapy shell command
            if css:
                selector = f"response.css({css!r})"
                method = f".getall() if {many} else .get()"
                code = f"{selector}{method}"
            elif xpath:
                selector = f"response.xpath({xpath!r})"
                method = f".getall() if {many} else .get()"
                code = f"{selector}{method}"
            else:
                code = "response.text[:500]"

            shell_script = f'''
import sys
sys.path.insert(0, ".")
import scrapy
from scrapy.crawler import CrawlerProcess
from scrapy.http import Request
import json

class ParseSpider(scrapy.Spider):
    name = "_parse"
    def parse(self, response):
        try:
            result = {code}
            if isinstance(result, list):
                result = [str(x) for x in result]
            elif result is None:
                result = ""
            else:
                result = str(result)
            print("PARSE_RESULT:", json.dumps(result))
        except Exception as e:
            print("PARSE_ERROR:", str(e))
        {{}}

process = CrawlerProcess()
process.crawl(ParseSpider, start_urls=["{url}"])
process.start()
'''
            result = subprocess.run(
                [sys.executable, "-c", shell_script],
                capture_output=True, text=True, timeout=30
            )
            output = result.stdout + result.stderr
            if "PARSE_RESULT:" in output:
                parts = output.split("PARSE_RESULT:")
                try:
                    parsed = json.loads(parts[-1].strip().split("\\n")[0])
                    return [TextContent(type="text", text=json.dumps(parsed, ensure_ascii=False, indent=2))]
                except:
                    pass
            return [TextContent(type="text", text=output[:5000])]

        elif name == "list_spiders":
            output = _run_scrapy_cmd(["list"])
            return [TextContent(type="text", text=output)]

        elif name == "crawl":
            spider = arguments["spider"]
            url = arguments.get("url")
            settings = arguments.get("settings", {})
            args = [spider]
            if url:
                args.append(url)
            for k, v in settings.items():
                args.extend(["-s", f"{k}={v}"])
            output = _run_scrapy_cmd(args)
            return [TextContent(type="text", text=output[:5000])]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except subprocess.TimeoutExpired:
        return [TextContent(type="text", text="Timeout (30s)")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {str(e)}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
