import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from mcp.server import Server

from linux_mcp import server as srv
from linux_mcp.config import AppSettings, ClientConfig, ServerConfig
from linux_mcp.logging_config import JsonFormatter, output_fields, setup_logging
from linux_mcp.ssh_client import CommandResult

KEY = "mcp_" + "a" * 64


# ── Formatter ──────────────────────────────────────────────────────────────
def _format(msg, **extra):
    record = logging.LogRecord("linux_mcp.audit", logging.INFO, __file__, 1, msg, None, None)
    record.__dict__.update(extra)
    return json.loads(JsonFormatter().format(record))


def test_formatter_emits_json_with_extra_fields():
    entry = _format("command completed", event="command.completed", command="df -h", exit_code=0)
    assert entry["message"] == "command completed"
    assert entry["level"] == "INFO" and entry["logger"] == "linux_mcp.audit"
    assert entry["event"] == "command.completed" and entry["command"] == "df -h" and entry["exit_code"] == 0
    assert entry["timestamp"].endswith("+00:00")
    assert "args" not in entry and "lineno" not in entry


def test_formatter_handles_unicode_and_multiline():
    entry = _format("x", stdout="line1\nline2 – ✓")
    assert entry["stdout"] == "line1\nline2 – ✓"


def test_plain_access_keeps_uvicorn_format():
    fmt = JsonFormatter(plain_access=True)
    access = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("10.0.5.20:51234", "POST", "/mcp", "1.1", 200), None,
    )
    assert fmt.format(access) == 'INFO:     10.0.5.20:51234 - "POST /mcp HTTP/1.1" 200 OK'
    other = logging.LogRecord("linux_mcp.audit", logging.INFO, __file__, 1, "x", None, None)
    assert json.loads(fmt.format(other))["message"] == "x"


@pytest.mark.parametrize("fmt, uvicorn_disabled", [("json", True), ("plain", False)])
def test_access_log_format_selects_one_access_log(fmt, uvicorn_disabled, monkeypatch):
    monkeypatch.setenv("ACCESS_LOG_FORMAT", fmt)
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        setup_logging(AppSettings.from_env())
        assert logging.getLogger("uvicorn.access").disabled is uvicorn_disabled
        assert logging.getLogger("linux_mcp.access").disabled is not uvicorn_disabled
    finally:
        root.handlers[:], level = saved
        root.setLevel(level)
        setup_logging(AppSettings())    # back to the default (json) access log


def test_access_log_format_rejects_unknown(monkeypatch):
    monkeypatch.setenv("ACCESS_LOG_FORMAT", "xml")
    with pytest.raises(ValueError):
        AppSettings.from_env()


def test_output_fields_truncates():
    assert output_fields("stdout", "abc", 10) == {"stdout": "abc"}
    assert output_fields("stdout", "abcdef", 3) == {"stdout": "abc", "stdout_truncated": True, "stdout_chars": 6}
    assert output_fields("stdout", "abcdef", 0) == {"stdout": "abcdef"}


# ── Audit events from tool calls ───────────────────────────────────────────
@pytest.fixture
def tool_env(monkeypatch):
    servers = {
        "ro": ServerConfig(name="ro", host="10.0.0.1"),
        "rw": ServerConfig(name="rw", host="10.0.0.2", allow_write=True),
    }
    client = ClientConfig(alias="tester", api_key=KEY, ip="127.0.0.1")
    request = SimpleNamespace(
        state=SimpleNamespace(client=client, client_ip="127.0.0.1", request_id="req-1"),
        headers={"Mcp-Session-Id": "sess-1"},
    )
    monkeypatch.setattr(Server, "request_context", property(lambda self: SimpleNamespace(request=request)))
    monkeypatch.setattr(srv, "get_servers", lambda: servers)
    monkeypatch.setattr(srv, "execute_command", lambda server, cmd: CommandResult("out", "warn", 2))


def _audit_events(caplog):
    return [r for r in caplog.records if r.name == "linux_mcp.audit"]


def test_read_command_logs_received_and_completed(tool_env, caplog):
    caplog.set_level(logging.INFO, logger="linux_mcp.audit")
    result = asyncio.run(srv.call_tool("run_read_command", {"server": "ro", "command": "df -h"}))

    assert result[0].text == "out\n[STDERR]: warn\n[Exit Code]: 2"
    received, completed = _audit_events(caplog)
    assert received.event == "command.received" and received.command == "df -h"
    assert completed.event == "command.completed"
    assert (completed.stdout, completed.stderr, completed.exit_code) == ("out", "warn", 2)
    for r in (received, completed):
        assert (r.request_id, r.client, r.client_ip, r.session_id) == ("req-1", "tester", "127.0.0.1", "sess-1")
        assert (r.tool, r.server) == ("run_read_command", "ro")


def test_blocked_command_is_logged(tool_env, caplog):
    caplog.set_level(logging.INFO, logger="linux_mcp.audit")
    asyncio.run(srv.call_tool("run_read_command", {"server": "ro", "command": "   "}))
    events = [r.event for r in _audit_events(caplog)]
    assert events == ["command.received", "command.blocked"]


def test_write_on_read_only_server_is_logged(tool_env, caplog):
    caplog.set_level(logging.INFO, logger="linux_mcp.audit")
    asyncio.run(srv.call_tool("run_write_command", {"server": "ro", "command": "touch /tmp/x"}))
    blocked = _audit_events(caplog)[-1]
    assert blocked.event == "command.blocked" and "does not allow write" in blocked.reason


def test_ssh_failure_is_logged(tool_env, caplog, monkeypatch):
    def boom(server, cmd):
        raise OSError("connection refused")
    monkeypatch.setattr(srv, "execute_command", boom)
    caplog.set_level(logging.INFO, logger="linux_mcp.audit")
    result = asyncio.run(srv.call_tool("run_read_command", {"server": "ro", "command": "uptime"}))
    assert result[0].text.startswith("[SSH ERROR]")
    failed = _audit_events(caplog)[-1]
    assert failed.event == "command.failed" and failed.error == "connection refused"


def test_unknown_server_is_logged(tool_env, caplog):
    caplog.set_level(logging.INFO, logger="linux_mcp.audit")
    asyncio.run(srv.call_tool("run_read_command", {"server": "nope", "command": "uptime"}))
    rejected = _audit_events(caplog)[-1]
    assert rejected.event == "tool.rejected" and rejected.server == "nope"
