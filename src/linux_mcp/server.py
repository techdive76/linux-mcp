import asyncio
import contextlib
import logging
import time

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import mcp.types as types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from .config import ClientConfig, ServerConfig, get_clients, get_servers, get_settings
from .logging_config import output_fields, setup_logging
from .middleware import AuthMiddleware
from .validator import validate_read_command, validate_write_command
from .ssh_client import execute_command

settings = get_settings()
setup_logging(settings)
logger = logging.getLogger("linux_mcp.server")
audit = logging.getLogger("linux_mcp.audit")

# ── Initialize MCP Server ──────────────────────────────────────────────────
app = Server("linux-mcp")

SERVER_PARAM = {
    "type": "string",
    "description": "Name of the target server, as returned by list_servers. IP addresses are not accepted.",
}
COMMAND_HINT = "Avoid using double quotes in the command and use single quotes instead to prevent parsing issues."

SNAPSHOT_COMMANDS = {
    "OS Info":            "uname -a",
    "Uptime":             "uptime",
    "CPU Usage":          "mpstat 1 1",
    "Memory Usage":       "free -h",
    "Disk Usage":         "df -h",
    "Top Processes":      "ps aux --sort=-%mem | head -15",
    "Network Interfaces": "ip addr show",
    "Open Ports":         "ss -tuln",
    "Running Services":   "systemctl list-units --type=service --state=running",
    "Last Logins":        "last -n 10",
    "Kernel Messages":    "dmesg | tail -20",
}


class ToolError(Exception):
    """Raised for request errors that are returned to the model as text."""


# ── List Tools ─────────────────────────────────────────────────────────────
@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="list_servers",
            description=(
                "List the remote Linux servers you are allowed to use, with their description "
                "and whether write commands are permitted (allow_write)."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
            annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        ),
        types.Tool(
            name="run_read_command",
            description=(
                "Execute a READ-ONLY diagnostic command on a named remote Linux server over SSH. "
                "Only safe, non-destructive commands are permitted (e.g. ps, df, netstat, cat, grep, journalctl). "
                "Any command that writes, modifies, deletes, or restarts services is strictly blocked."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server": SERVER_PARAM,
                    "command": {
                        "type": "string",
                        "description": f"The read-only Linux shell command to execute. {COMMAND_HINT}",
                    },
                },
                "required": ["server", "command"],
            },
            annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=True),
        ),
        types.Tool(
            name="run_write_command",
            description=(
                "Execute a command that MODIFIES a named remote Linux server over SSH "
                "(edit files, install packages, restart services, ...). "
                "Only works on servers with allow_write=true. Catastrophic commands "
                "(recursive delete of system paths, disk formatting, reboot/shutdown, user/password changes, "
                "privilege escalation, network lock-out) are always blocked. "
                "Prefer run_read_command for anything that does not need to change the system."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server": SERVER_PARAM,
                    "command": {
                        "type": "string",
                        "description": f"The Linux shell command to execute. {COMMAND_HINT}",
                    },
                },
                "required": ["server", "command"],
            },
            annotations=types.ToolAnnotations(
                readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
            ),
        ),
        types.Tool(
            name="get_system_snapshot",
            description=(
                "Run a predefined set of safe diagnostic commands on a named server and return a full system snapshot. "
                "Covers CPU, memory, disk, top processes, network interfaces, and running services."
            ),
            inputSchema={
                "type": "object",
                "properties": {"server": SERVER_PARAM},
                "required": ["server"],
            },
            annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=True),
        ),
    ]


# ── Helpers ────────────────────────────────────────────────────────────────
def _current_client() -> ClientConfig:
    """The authenticated caller, attached to the HTTP request by AuthMiddleware."""
    request = app.request_context.request
    client = getattr(getattr(request, "state", None), "client", None)
    if client is None:
        raise ToolError("Unauthenticated request")
    return client


def _accessible_servers(client: ClientConfig) -> list[ServerConfig]:
    return [s for s in get_servers().values() if client.can_access(s.name)]


def resolve_server(client: ClientConfig, name) -> ServerConfig:
    """Map a server name to its config; unknown and unauthorized names get the same error."""
    server = get_servers().get(name) if isinstance(name, str) else None
    if server is None or not client.can_access(server.name):
        available = ", ".join(s.name for s in _accessible_servers(client)) or "(none)"
        raise ToolError(f"Unknown or unauthorized server '{name}'. Available servers: {available}")
    return server


def _text(text: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=text)]


def _audit_context(client: ClientConfig) -> dict:
    """Fields shared by every audit event of this request; request_id links them to the access log."""
    request = app.request_context.request
    state = getattr(request, "state", None)
    return {
        "request_id": getattr(state, "request_id", None),
        "client": client.alias,
        "client_ip": getattr(state, "client_ip", None),
        "session_id": request.headers.get("Mcp-Session-Id") if request else None,
    }


async def _run(ctx: dict, server: ServerConfig, command: str) -> str:
    """Execute a validated command and audit-log its result."""
    start = time.perf_counter()
    try:
        result = await asyncio.to_thread(execute_command, server, command)
    except Exception as e:
        audit.error("command failed", extra={
            **ctx, "event": "command.failed", "command": command,
            "error": str(e), "duration_ms": _ms(start),
        })
        return f"[SSH ERROR]: {e}"

    limit = settings.log_max_output_chars
    audit.info("command completed", extra={
        **ctx, "event": "command.completed", "command": command,
        "exit_code": result.exit_code, "duration_ms": _ms(start),
        **output_fields("stdout", result.stdout, limit),
        **output_fields("stderr", result.stderr, limit),
    })
    return result.to_text()


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


# ── Handle Tool Calls ──────────────────────────────────────────────────────
@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    ctx = {"tool": name, "server": arguments.get("server")}
    try:
        client = _current_client()
        ctx = {**_audit_context(client), **ctx}

        if name == "list_servers":
            audit.info("tool called", extra={**ctx, "event": "tool.call"})
            lines = [
                f"- {s.name} (allow_write={str(s.allow_write).lower()})" + (f": {s.description}" if s.description else "")
                for s in _accessible_servers(client)
            ]
            return _text("\n".join(lines) or "No servers available.")

        server = resolve_server(client, arguments.get("server"))

        if name in ("run_read_command", "run_write_command"):
            command = str(arguments.get("command", "")).strip()
            audit.info("command received", extra={**ctx, "event": "command.received", "command": command})

            if name == "run_write_command":
                if not server.allow_write:
                    _audit_blocked(ctx, command, "server does not allow write commands")
                    return _text(
                        f"[BLOCKED] Server '{server.name}' does not allow write commands. "
                        "Use run_read_command for diagnostics."
                    )
                is_allowed, reason = validate_write_command(command)
            else:
                is_allowed, reason = validate_read_command(command)

            if not is_allowed:
                _audit_blocked(ctx, command, reason)
                scope = "write" if name == "run_write_command" else "read-only diagnostic"
                return _text(f"[BLOCKED] Command rejected: {reason}\nOnly {scope} commands are permitted by this tool.")

            return _text(await _run(ctx, server, command))

        if name == "get_system_snapshot":
            audit.info("tool called", extra={**ctx, "event": "tool.call"})
            results = []
            for label, cmd in SNAPSHOT_COMMANDS.items():
                audit.info("command received", extra={**ctx, "event": "command.received", "command": cmd})
                is_allowed, reason = validate_read_command(cmd)
                if not is_allowed:
                    _audit_blocked(ctx, cmd, reason)
                    results.append(f"### {label}\n[SKIPPED]: {reason}")
                    continue
                output = await _run(ctx, server, cmd)
                results.append(f"### {label}\n```\n{output}\n```")
            return _text(f"# System snapshot: {server.name}\n\n" + "\n\n".join(results))

        audit.warning("unknown tool", extra={**ctx, "event": "tool.rejected", "reason": "unknown tool"})
        return _text(f"[ERROR]: Unknown tool '{name}'")

    except ToolError as e:
        audit.warning("tool call rejected", extra={**ctx, "event": "tool.rejected", "reason": str(e)})
        return _text(f"[ERROR]: {e}")


def _audit_blocked(ctx: dict, command: str, reason: str) -> None:
    audit.warning("command blocked", extra={**ctx, "event": "command.blocked", "command": command, "reason": reason})


# ── StreamableHTTP Setup ───────────────────────────────────────────────────
session_manager = StreamableHTTPSessionManager(
    app=app,
    event_store=None,       # Use InMemoryEventStore() if you need resumability
    json_response=False,    # Set True to disable SSE streaming within HTTP
    stateless=False,        # Set True for fully stateless (no session tracking)
)


class MCPEndpoint:
    """ASGI endpoint so /mcp is served at the exact path (no 307 redirect to /mcp/)."""

    async def __call__(self, scope, receive, send):
        await session_manager.handle_request(scope, receive, send)


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@contextlib.asynccontextmanager
async def lifespan(starlette_app: Starlette):
    """Validate configuration, then start and cleanly shut down the session manager."""
    servers = get_servers()
    clients = get_clients()
    logger.info(f"Loaded {len(servers)} server(s): {', '.join(servers)}")
    logger.info(f"Loaded {len(clients)} client(s): {', '.join(c.alias for c in clients)}")
    async with session_manager.run():
        logger.info(f"SSH Diagnostic MCP server running at http://{settings.host}:{settings.port}/mcp")
        yield


# ── Starlette App ──────────────────────────────────────────────────────────

starlette_app = Starlette(
    lifespan=lifespan,
    middleware=[
        Middleware(AuthMiddleware),         # ← Protects ALL routes except /health
    ],
    routes=[
        Route("/health", endpoint=health, methods=["GET"]),
        Route("/mcp", endpoint=MCPEndpoint()),
    ],
)


# ── Entry Point ────────────────────────────────────────────────────────────
def main() -> None:
    uvicorn.run(
        starlette_app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        log_config=None,        # keep the JSON logging set up by setup_logging()
        proxy_headers=False,
    )
