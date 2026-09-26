#!/usr/bin/env python3
"""Trigger browser OAuth login for github-mcp-server."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    binary = Path.home() / ".local" / "bin" / "github-mcp-server"
    if not binary.exists():
        print(f"Error: {binary} not found", file=sys.stderr)
        return 1

    print("==================================================")
    print(" GitHub MCP Server Browser OAuth Login")
    print("==================================================")
    print("1. A browser window will open to github.com.")
    print("2. Click the green 'Authorize' button.")
    print("3. Return here to see confirmation.\n")

    proc = subprocess.Popen(
        [str(binary), "stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    # Initialize
    init_req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "github-mcp-login", "version": "1.0"},
        },
    }
    proc.stdin.write(json.dumps(init_req) + "\n")
    proc.stdin.flush()

    # Trigger tool call to prompt OAuth flow
    call_req = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "get_me", "arguments": {}},
    }
    proc.stdin.write(json.dumps(call_req) + "\n")
    proc.stdin.flush()

    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            if "opened browser for GitHub authorization" in line:
                print(">>> Browser tab opened! Please switch to your browser and click 'Authorize'...")
            elif line.startswith("{"):
                try:
                    data = json.loads(line)
                    if data.get("id") == 2:
                        print("\n🎉 Authorization successful! Logged in as:")
                        result = data.get("result", {})
                        print(json.dumps(result, indent=2))
                        return 0
                except json.JSONDecodeError:
                    pass
            elif "level=ERROR" in line:
                print("Error:", line.strip(), file=sys.stderr)
                return 1
    except KeyboardInterrupt:
        print("\nLogin cancelled by operator.")
        return 130
    finally:
        proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
