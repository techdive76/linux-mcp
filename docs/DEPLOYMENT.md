# linux-mcp: deployment and client guide

## Tools

| Tool | Purpose |
|---|---|
| `list_servers` | Names, descriptions and `allow_write` of the servers the caller may use (never IPs) |
| `run_read_command(server, command)` | Read-only diagnostics; the strict allow-list validator applies |
| `run_write_command(server, command)` | Only on `allow_write: true` servers; catastrophic commands are still blocked |
| `get_system_snapshot(server)` | A fixed set of read-only diagnostics |

The tools carry MCP annotations (`readOnlyHint` / `destructiveHint`), so AI clients can auto-approve
the read tools and still ask before running `run_write_command`.

## 1. Prepare the host folders

```yaml
config/servers.yaml    # copy from config/servers.example.yaml
config/clients.yaml    # copy from config/clients.example.yaml
ssh/id_rsa             # private key(s) referenced by key_file
ssh/known_hosts        # optional; required when SSH_STRICT_HOST_KEYS=true
```

Generate an SSH key and install it on each remote server:

```bash
ssh-keygen -t ed25519 -f ./ssh/id_rsa -N ""
ssh-copy-id -i ./ssh/id_rsa.pub root@<server-ip>
ssh-keyscan <server-ip> >> ./ssh/known_hosts      # for strict host key checking

# to Test
ssh -i ./ssh/id_rsa root@<server-ip>

```

The container runs as UID 5678, so it must be able to read the key files:

```bash
sudo chown -R 5678 ./ssh && chmod 600 ./ssh/id_*
```

Generate one API key per client and put it in `config/clients.yaml`:

```bash
pip install -e .                  # once, from the repo root
python -m linux_mcp.auth     # or: linux-mcp-keygen
```

## 2. Run

```bash
docker compose up -d --build
docker compose logs -f
curl http://localhost:8000/health
```

Configuration is read once at startup. After you edit `config/*.yaml`, run `docker compose restart`.

## 3. Reverse proxy

Set `TRUSTED_PROXIES` in `.env` to the proxy's IP or CIDR as the container sees it. That is often the
Docker bridge gateway, e.g. `172.17.0.1`, when the proxy runs on the same host. `X-Forwarded-For`
and `X-Real-IP` are honoured __only__ when the request comes from a trusted proxy. Each client's `ip`
in `clients.yaml` must be the real client address.

Nginx example (SSE needs buffering off):

```nginx
location /mcp {
    proxy_pass http://127.0.0.1:8000/mcp;
    proxy_http_version 1.1;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_buffering off;
    proxy_read_timeout 300s;
}
```

## 4. Logging

All logs are written to stdout as JSON, one object per line (`docker compose logs`, or `mcp.log` when
started with `scripts/startup.sh`). Every line has `timestamp` (UTC), `level`, `logger` and `message`.
App events also carry an `event` field:

| `event` | Logger | When | Extra fields |
|---|---|---|---|
| `http.request` | `linux_mcp.access` | Every request to `/mcp` (except `/health`), accepted or rejected | `client`, `client_ip`, `method`, `path`, `status`, `duration_ms`, `user_agent`, `session_id`, `reason` (rejected only) |
| `tool.call` | `linux_mcp.audit` | `list_servers` / `get_system_snapshot` called | `tool`, `server` |
| `command.received` | `linux_mcp.audit` | A command arrives, before validation | `command` |
| `command.blocked` | `linux_mcp.audit` | The validator or a read-only server rejected it | `command`, `reason` |
| `command.completed` | `linux_mcp.audit` | The command ran | `command`, `exit_code`, `stdout`, `stderr`, `duration_ms` |
| `command.failed` | `linux_mcp.audit` | SSH error | `command`, `error`, `duration_ms` |
| `tool.rejected` | `linux_mcp.audit` | Unknown or unauthorized server, unknown tool | `reason` |

All audit events also carry `request_id`, `client`, `client_ip`, `session_id`, `tool` and `server`.
`request_id` is the same as in the `http.request` line and is returned to the caller as the
`X-Request-ID` response header. A snapshot logs one `command.*` pair per command, all with the same
`request_id`.

```json
{"timestamp": "2026-10-30T08:15:02.481+00:00", "level": "INFO", "logger": "linux_mcp.audit", "message": "command completed", "request_id": "d2b007c0…", "client": "ops-agent", "client_ip": "10.0.5.20", "session_id": "231f0f90…", "tool": "run_read_command", "server": "linux-01", "event": "command.completed", "command": "df -h", "exit_code": 0, "duration_ms": 182.4, "stdout": "Filesystem  Size  Used …", "stderr": ""}
```

| Variable | Default | Purpose |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `WARNING` keeps only rejected requests, blocked commands and failures |
| `ACCESS_LOG_FORMAT` | `json` | `json` = the `http.request` events above. `plain` = uvicorn's standard access log instead (`INFO:     10.0.5.20:51234 - "POST /mcp HTTP/1.1" 200 OK`): no client alias or request id, the IP is the direct peer (the proxy, behind a reverse proxy), and `/health` probes are included. Command audit events stay JSON either way |
| `LOG_MAX_OUTPUT_CHARS` | `10000` | Max characters logged per `stdout` / `stderr`; longer output gets `stdout_truncated: true` and `stdout_chars`. `0` = no limit |
| `LOG_FILE` | unset | Also write JSON logs to this file (rotated). In Docker, mount a directory writable by UID 5678 |
| `LOG_FILE_MAX_BYTES` / `LOG_FILE_BACKUPS` | `10485760` / `5` | Rotation of `LOG_FILE` |

Command output can contain sensitive data (config files, environment variables). Treat the logs as
sensitive, or lower `LOG_MAX_OUTPUT_CHARS`.

The JSON lines can be shipped as-is to Loki/Grafana, Elasticsearch/Kibana, Splunk or Datadog. Quick
queries with `jq`:

```bash
docker compose logs --no-log-prefix linux-mcp | jq -c 'select(.event == "command.completed") | {timestamp, client, server, command, exit_code}'
docker compose logs --no-log-prefix linux-mcp | jq -c 'select(.event == "command.blocked" or .status == 401 or .status == 403)'
```

## 5. Client configuration

Authenticate with `Authorization: Bearer <key>` or `X-API-Key: <key>`.

**Claude Code**

```bash
claude mcp add --transport http linux-mcp https://<mcp-host>/mcp \
  --header "Authorization: Bearer mcp_<key>"
```

__Claude Desktop__: see `claude_desktop_config.json`.

**Anthropic Messages API (MCP connector)**: Anthropic's servers make the call, so the MCP endpoint must
be reachable from the internet, and the client's `ip` must cover Anthropic's published egress ranges.

```python
import anthropic

client = anthropic.Anthropic()
response = client.beta.messages.create(
    model="claude-opus-5-5",
    max_tokens=16000,
    betas=["mcp-client-2025-11-20"],
    mcp_servers=[{
        "type": "url",
        "url": "https://<mcp-host>/mcp",
        "name": "linux-mcp",
        "authorization_token": "mcp_<key>",
    }],
    tools=[{"type": "mcp_toolset", "mcp_server_name": "linux-mcp"}],
    messages=[{"role": "user", "content": "Check disk usage on linux-01"}],
)
```

**OpenAI Responses API (remote MCP tool)**: OpenAI's servers make the call, so the same reachability
and IP-range notes apply.

```python
from openai import OpenAI

client = OpenAI()
response = client.responses.create(
    model="<model>",
    tools=[{
        "type": "mcp",
        "server_label": "linux-mcp",
        "server_url": "https://<mcp-host>/mcp",
        "headers": {"Authorization": "Bearer mcp_<key>"},
        "require_approval": {"never": {"tool_names": ["list_servers", "run_read_command", "get_system_snapshot"]}},
    }],
    input="Check disk usage on linux-01",
)
```

__Python MCP SDK__: see `../examples/mcp_client.py`.
