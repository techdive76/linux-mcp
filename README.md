# linux-mcp

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server that lets AI assistants
(Claude, ChatGPT, LangChain agents, or any MCP client) run **validated** diagnostic and admin
commands on your remote Linux servers over SSH.

It runs as a single container in your network. AI clients connect over HTTP and refer to servers
**by name only**. Each command is checked by a validator before it reaches a server, and every call
is written to a structured JSON audit log.

```
┌──────────────┐  HTTPS + API key   ┌──────────────────────┐   SSH (key auth)   ┌──────────────┐
│  AI client   │ ─────────────────▶ │   linux-mcp          │ ─────────────────▶ │ Linux server │
│ Claude, GPT, │   /mcp endpoint     │ auth → IP check →   │                     │ prod-db01    │
│ LangChain …  │ ◀───────────────── │ validator → audit    │ ◀───────────────── │ test-app01 … │
└──────────────┘                    └──────────────────────┘                    └──────────────┘
```

> [!WARNING]
> **The server speaks plain HTTP.** In production, always expose it over **HTTPS** behind a
> TLS-terminating reverse proxy such as nginx (see
> [Running behind a reverse proxy](#running-behind-a-reverse-proxy)). Over plain HTTP, API keys
> and command output travel unencrypted.

> [!CAUTION]
> **This service gives an AI direct administrative access to your servers.** Do not expose it
> to the public internet. Run it only inside your trusted network, and allow only the clients
> that need it. See [Security](#security).

## Features

- **Four MCP tools**: `list_servers`, `run_read_command`, `run_write_command`, `get_system_snapshot`.
- **Two-layer command validation**
  - *Read tool*: allow-list of diagnostic programs (`ps`, `df`, `journalctl`, `cat`, `grep`, `ss`, …)
    plus a deny-list of write, delete, service-control, privilege-escalation and subshell patterns.
  - *Write tool*: only on servers marked `allow_write: true`. Normal admin work is allowed, but
    catastrophic commands are always blocked: recursive delete of system paths, disk formatting,
    reboot or shutdown, user and password changes, `sudo`, firewall flushes, and stopping SSH.
- **Per-client authentication**: each client has its own API key **and** an IP/CIDR allow-list. Both
  must match.
- **Per-client server scoping**: limit a client to a subset of servers with `allowed_servers`.
- **Host IPs are never exposed**: clients only see server names, descriptions and `allow_write`.
- **Structured JSON logs**: access log plus a full audit trail (who ran what, where, exit code,
  output, duration), correlated by `request_id`.
- **Reverse-proxy aware**: `X-Forwarded-For` is honored only from trusted proxies.
- **MCP tool annotations** (`readOnlyHint` / `destructiveHint`): clients can auto-approve read
  tools and still ask before running write commands.
- Ships as a small, non-root Docker image with a health check.

## Use cases

linux-mcp is most powerful when it is wired into an **agent** that knows your platform. The
server gives the model safe, audited hands on your machines; the agent adds the domain
knowledge: which node to check, how to source the environment, what "healthy" looks like, and
when to stop and ask before changing something.

### Featured example: Oracle E-Business Suite agent

[`examples/agents/oracle-ebs-agent`](examples/agents/oracle-ebs-agent/SKILL.md) is a Claude
skill that turns Claude into an Oracle EBS 12.2 DBA / apps-admin assistant for a TEST
environment. It combines linux-mcp (OS access to the apps and database nodes) with the SQLcl MCP
server (SQL access to the APPS schema), so a single request can cover both layers:

- *"Is TEST healthy?"* runs `get_system_snapshot` on both nodes, checks listeners, apps services
  and concurrent managers, scans the DB alert log, and queries tablespace usage and invalid
  objects. The answer is one report that flags anything abnormal.
- *"Why are concurrent requests stuck?"* correlates manager OS processes and `$APPLCSF` logs on
  the apps node with `FND_CONCURRENT_REQUESTS` / `FND_CONCURRENT_PROCESSES` and blocking sessions
  in the database.
- *"Free up disk space on appnode"* diagnoses with read-only commands first, then proposes the exact
  cleanup commands and runs them through `run_write_command` only after you confirm.

What makes it work well:

- **Domain routing.** The skill knows which tier owns which component (OHS/WebLogic/adop on the
  apps node, alert log/listener/RMAN on the DB node) and picks the server name accordingly.
- **Correct environment handling.** Commands run as the EBS owner with the right `.env` sourced,
  in the non-interactive form linux-mcp requires.
- **Layered safety.** linux-mcp's validator, per-server `allow_write` and audit log are combined
  with the agent's own rule to confirm every write command and every DDL/DML before it runs.
- **Bounded output.** It uses `tail`, `grep -m` and similar so large logs never flood the model.

Use it as a template: copy the folder into your skills directory, adjust the node names, SID,
paths and connection names, and point it at your own linux-mcp server. The same pattern works for
any platform with a runbook, for example SAP, Kubernetes nodes, web stacks or backup servers.

## Table of contents

1. [Use cases](#use-cases)
2. [Quick start (Docker)](#quick-start-docker)
3. [MCP tools](#mcp-tools)
4. [Environment variables](#environment-variables)
5. [Configuration files](#configuration-files)
   - [servers.yaml](#configservers-yaml)
   - [clients.yaml](#configclients-yaml)
6. [Step-by-step: add a new server](#step-by-step-add-a-new-server)
7. [Step-by-step: add a new client](#step-by-step-add-a-new-client)
8. [Connecting AI clients](#connecting-ai-clients)
9. [Running behind a reverse proxy](#running-behind-a-reverse-proxy)
10. [Logging and auditing](#logging-and-auditing)
11. [Running without Docker](#running-without-docker)
12. [Development](#development)
13. [Security](#security)

---

## Quick start (Docker)

### Prerequisites

- Docker 20.10+ and Docker Compose v2
- SSH access (key-based) from the Docker host to the Linux servers you want to manage
- Python 3.12+ on any machine, only to generate API keys (or use the container, shown below)

### 1. Get the project

```bash
git clone https://github.com/techdive76/linux-mcp.git
cd linux-mcp
```

### 2. Create your local configuration

```bash
cp .env.example .env
cp config/servers.example.yaml config/servers.yaml
cp config/clients.example.yaml config/clients.yaml
```

`.env`, `config/*.yaml` (except the examples) and everything in `ssh/` are git-ignored, so your
real hosts, keys and API keys are never committed.

### 3. Create an SSH key for the MCP server

```bash
ssh-keygen -t ed25519 -f ./ssh/id_ed25519 -N "" -C "linux-mcp"
```

Then install it on each server and register the server in `config/servers.yaml`. The full
procedure is in [Step-by-step: add a new server](#step-by-step-add-a-new-server).

### 4. Generate an API key for each client

```bash
# Using the published image (no local Python needed)
docker run --rm ghcr.io/techdive76/linux-mcp:latest python -m linux_mcp.auth

# Or locally
pip install -e . && linux-mcp-keygen
```

Put the key into `config/clients.yaml`. See [Step-by-step: add a new client](#step-by-step-add-a-new-client).

### 5. Fix key file ownership

The container runs as **UID 5678**, so that user must be able to read the keys:

```bash
sudo chown -R 5678 ./ssh
chmod 700 ./ssh
chmod 600 ./ssh/id_*
```

### 6. Start the server

**Option A: published image** (recommended)

```bash
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

**Option B: build from source**

```bash
docker compose up -d --build
```

**Option C: plain `docker run`**

```bash
docker run -d --name linux-mcp --restart unless-stopped \
  -p 8000:8000 \
  -v "$PWD/config:/config:ro" \
  -v "$PWD/ssh:/ssh:ro" \
  -e LOG_LEVEL=INFO \
  ghcr.io/techdive76/linux-mcp:latest
```

### 7. Verify

```bash
curl http://localhost:8000/health          # → {"status":"ok"}
docker compose logs -f                     # shows "Loaded N server(s)" and "Loaded N client(s)"
```

The MCP endpoint is now at `http://<docker-host>:8000/mcp`.

> Configuration is read **once at startup**. After editing `config/*.yaml` or `.env`, run
> `docker compose restart` (or `up -d` after `.env` changes).
> If a config file is invalid, the container exits on startup with an error that names the
> problem. Check `docker compose logs`.

---

## MCP tools

| Tool | Arguments | Description |
|---|---|---|
| `list_servers` | none | Lists the servers this client may use, with description and `allow_write`. Never shows IPs. |
| `run_read_command` | `server`, `command` | Runs a read-only diagnostic command. Strict allow-list and deny-list. |
| `run_write_command` | `server`, `command` | Runs a modifying command. Only on servers with `allow_write: true`; catastrophic commands are still blocked. |
| `get_system_snapshot` | `server` | Runs a fixed set of diagnostics: OS, uptime, CPU, memory, disk, top processes, network, open ports, services, logins, kernel messages. |

Rejected commands are returned to the model as `[BLOCKED] …` with the reason, and logged as
`command.blocked`. Unknown server names and servers the client may not use get the same error,
so a client cannot find out which servers exist outside its scope.

---

## Environment variables

There are two groups of variables:

- **Compose variables** go in `.env`. Docker Compose reads them to build the container (ports,
  volumes, image).
- **Application variables** are read by the Python process inside the container. The compose files
  already pass the common ones through from `.env`.

### Compose variables (`.env`)

| Variable | Default | Description |
|---|---|---|
| `APP_IMAGE` | `ghcr.io/techdive76/linux-mcp:latest` | Image used by `docker-compose.prod.yml`. Point it at your own registry or a pinned tag (for example `:sha-abc1234`). |
| `HOST_PORT` | `8000` | Port published on the Docker host. The container always listens on `8000`. |
| `CONFIG_DIR` | `./config` | Host directory with `servers.yaml` and `clients.yaml`, mounted read-only at `/config`. |
| `SSH_DIR` | `./ssh` | Host directory with private keys and optional `known_hosts`, mounted read-only at `/ssh`. |

### Application variables

| Variable | Default | Description |
|---|---|---|
| `SERVERS_CONFIG` | `./config/servers.yaml` (`/config/servers.yaml` in Docker) | Path to the servers file. |
| `CLIENTS_CONFIG` | `./config/clients.yaml` (`/config/clients.yaml` in Docker) | Path to the clients file. |
| `SSH_KEY_DIR` | `./ssh` (`/ssh` in Docker) | Directory where relative `key_file` values and `known_hosts` are resolved. |
| `SSH_STRICT_HOST_KEYS` | `false` | `true` rejects any SSH host not listed in `${SSH_KEY_DIR}/known_hosts`. **Recommended in production.** With `false`, unknown host keys are accepted automatically. |
| `TRUSTED_PROXIES` | *(empty)* | Comma-separated IPs/CIDRs of reverse proxies. `X-Forwarded-For` / `X-Real-IP` are honored **only** when the request comes from one of these. Leave empty if clients connect directly. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. `WARNING` keeps only rejected requests, blocked commands and failures. |
| `ACCESS_LOG_FORMAT` | `json` | `json` gives structured `http.request` events with client alias and real IP. `plain` gives uvicorn's standard access log. Audit events are JSON either way. |
| `LOG_MAX_OUTPUT_CHARS` | `10000` | Max characters of each command's `stdout` / `stderr` written to the audit log. `0` means no limit. |
| `LOG_FILE` | *(unset)* | Also write JSON logs to this file, with rotation. In Docker, mount a directory writable by UID 5678. |
| `LOG_FILE_MAX_BYTES` | `10485760` | Rotation size for `LOG_FILE` (10 MB). |
| `LOG_FILE_BACKUPS` | `5` | Number of rotated `LOG_FILE` backups to keep. |
| `MCP_HOST` | `0.0.0.0` | Bind address. Only used when started with the `linux-mcp` command; the Docker image always binds `0.0.0.0:8000`. |
| `MCP_PORT` | `8000` | Bind port. Only used when started with the `linux-mcp` command. |

`LOG_FILE`, `LOG_FILE_MAX_BYTES` and `LOG_FILE_BACKUPS` are not passed through by the compose
files. To use them, add them to the `environment:` section and mount a writable volume:

```yaml
    environment:
      LOG_FILE: /logs/linux-mcp.log
    volumes:
      - ./logs:/logs          # sudo chown 5678 ./logs
```

---

## Configuration files

Both files are YAML and are validated at startup. Any error (unknown key type, duplicate name,
duplicate API key, invalid IP, a client referencing a server that doesn't exist) stops the server
with a clear message.

### `config/servers.yaml`

Defines the Linux servers the MCP server may SSH into.

```yaml
# Values in `defaults` apply to every server unless overridden per server.
defaults:
  username: root
  port: 22
  key_file: id_ed25519      # relative to SSH_KEY_DIR (/ssh in Docker), or an absolute path
  timeout: 30

servers:
  - name: prod-db01                     # what the AI passes as the `server` argument
    host: 10.0.0.10                     # IP or DNS name, never shown to clients
    description: "Production database node"
    allow_write: false                  # read-only: run_write_command is refused

  - name: test-app01
    host: test-app01.internal.example.com
    description: "Test application server"
    allow_write: true                   # enables run_write_command (still validated)
    username: admin                     # per-server overrides
    port: 2222
    key_file: test_app01_ed25519
    timeout: 60
```

| Field | Required | Default | Description |
|---|---|---|---|
| `name` | yes | none | Unique identifier used by AI clients. Letters, digits, `_`, `.`, `-` only. |
| `host` | yes | none | IP address or hostname to SSH to. **Never exposed to clients.** |
| `description` | no | `""` | Shown to the AI in `list_servers`. Describe the server's role (for example "Oracle DB, PROD") so the model picks the right one. |
| `allow_write` | no | `false` | Enables `run_write_command` on this server. |
| `username` | no | `root` | SSH user. A dedicated, least-privileged user is recommended (see [Security](#security)). |
| `port` | no | `22` | SSH port. |
| `key_file` | no | `id_rsa` | Private key file name in `SSH_KEY_DIR`, or an absolute path inside the container. |
| `timeout` | no | `30` | Seconds for the SSH connection and command execution. |

Any field except `name` and `host` can go in `defaults`.

### `config/clients.yaml`

Defines who may call the MCP server. A request is accepted only if the **API key matches an
enabled client _and_ the caller's IP is in that client's `ip` list**.

```yaml
clients:
  - alias: claude-desktop                # name shown in logs and the audit trail
    api_key: mcp_3f9c…                   # generate with: python -m linux_mcp.auth
    ip: 10.10.5.20                       # single IP or CIDR
    enabled: true

  - alias: langchain-agent
    api_key: mcp_8a1d…
    ip:                                  # or a list of IPs/CIDRs
      - 10.10.6.0/24
      - 192.168.1.15
    enabled: true
    allowed_servers: [test-app01]        # restrict to these servers; omit to allow all
```

| Field | Required | Default | Description |
|---|---|---|---|
| `alias` | yes | none | Unique, human-readable client name used in logs. |
| `api_key` | yes | none | Secret, at least 16 characters, unique per client. Use the built-in generator (`mcp_` + 64 hex chars). |
| `ip` | yes | none | A single IP/CIDR or a list. IPv4 and IPv6 are supported. Use `0.0.0.0/0` only if you really mean "any IPv4 address". |
| `enabled` | no | `true` | `false` rejects the client (401) without deleting its entry. |
| `allowed_servers` | no | all servers | List of server `name`s this client may use. Unknown names fail validation at startup. |

**HTTP responses:** a missing or invalid key returns `401 Unauthorized`. A valid key from an IP
that is not allowed returns `403 Forbidden`. The detailed reason is only written to the server
logs, never returned to the caller.

---

## Step-by-step: add a new server

This example adds a server called `web-prod01` at `10.0.20.15`.

### Step 1: Create a dedicated SSH user on the target server *(recommended)*

On `web-prod01`:

```bash
sudo useradd -m -s /bin/bash mcpdiag
# Optional: let it read logs without being root
sudo usermod -aG adm,systemd-journal mcpdiag
```

You can use `root` instead, but a dedicated user limits the damage if something goes wrong. The
validator is a strong safeguard, not a sandbox.

### Step 2: Create or choose an SSH key

You can reuse the key from the quick start (`ssh/id_ed25519`) or create a separate key for this
server:

```bash
ssh-keygen -t ed25519 -f ./ssh/web_prod01_ed25519 -N "" -C "linux-mcp@web-prod01"
```

> Keys must be **without a passphrase**. The server loads them non-interactively.

### Step 3: Install the public key on the server

```bash
ssh-copy-id -i ./ssh/web_prod01_ed25519.pub mcpdiag@10.0.20.15
```

If `ssh-copy-id` isn't available, append the contents of the `.pub` file to
`~mcpdiag/.ssh/authorized_keys` on the server (permissions `700` for `.ssh`, `600` for the file).

### Step 4: Test the connection from the Docker host

```bash
ssh -i ./ssh/web_prod01_ed25519 -o IdentitiesOnly=yes mcpdiag@10.0.20.15 'uname -a'
```

This must work without a password prompt. Also make sure the Docker host can reach the server on
the SSH port: the container connects out through the host's network.

### Step 5: Record the host key *(required when `SSH_STRICT_HOST_KEYS=true`)*

```bash
ssh-keyscan -p 22 10.0.20.15 >> ./ssh/known_hosts
```

Check the fingerprint against the server (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on
the server) before you trust it. Use the same value in `ssh-keyscan` that you will put in `host`
(the IP or the DNS name).

### Step 6: Add the server to `config/servers.yaml`

```yaml
servers:
  # … existing servers …

  - name: web-prod01
    host: 10.0.20.15
    description: "Production Nginx web server (customer portal)"
    allow_write: false
    username: mcpdiag
    key_file: web_prod01_ed25519
```

Start with `allow_write: false`, and enable writes only on servers where AI-driven changes are
acceptable.

### Step 7: Grant clients access

Clients **without** `allowed_servers` can use the new server automatically. Clients **with**
`allowed_servers` need the name added:

```yaml
  - alias: ops-agent
    # …
    allowed_servers: [prod-db01, web-prod01]
```

### Step 8: Fix permissions and restart

```bash
sudo chown -R 5678 ./ssh && chmod 600 ./ssh/*_ed25519 ./ssh/id_*
docker compose restart          # or: docker compose -f docker-compose.prod.yml restart
docker compose logs --tail 20   # expect "Loaded N server(s): …, web-prod01"
```

### Step 9: Verify from an AI client

Ask your assistant *"List the available Linux servers"* and then *"Check disk usage on
web-prod01"*. You can also use the Python example:

```bash
MCP_URL=http://localhost:8000/mcp MCP_API_KEY=mcp_… MCP_SERVER=web-prod01 \
  python examples/mcp_client.py
```

**Troubleshooting**

| Symptom | Likely cause |
|---|---|
| Container exits on start: `Client … references unknown servers` | A name in `allowed_servers` doesn't match any `name` in `servers.yaml`. |
| `[SSH ERROR]: Authentication failed` | Wrong `username` or `key_file`, the public key isn't in `authorized_keys`, or the key has a passphrase. |
| `[SSH ERROR]: [Errno 13] Permission denied: '/ssh/…'` | The key isn't readable by UID 5678. Run `chown 5678`. |
| `[SSH ERROR]: Server '…' not found in known_hosts` | `SSH_STRICT_HOST_KEYS=true` and the host isn't in `ssh/known_hosts`. Repeat step 5. |
| `[SSH ERROR]: timed out` | Firewall or routing between the Docker host and the server, or the wrong `port`. |
| `Unknown or unauthorized server` | Typo in the name, or the client's `allowed_servers` doesn't include it. |

---

## Step-by-step: add a new client

### Step 1: Generate an API key

```bash
docker run --rm ghcr.io/techdive76/linux-mcp:latest python -m linux_mcp.auth
# mcp_5b0e2c…(64 hex chars)
```

### Step 2: Find the client's source IP *as the MCP server sees it*

- **Local tools** (Claude Code, Claude Desktop with `mcp-remote`, scripts): the IP of the machine
  they run on.
- **Cloud-hosted connectors** (Anthropic Messages API MCP connector, claude.ai connectors, OpenAI
  remote MCP): the provider's servers make the call. Use the provider's **published egress IP
  ranges**, and the endpoint must be reachable from the internet.
- **Behind a reverse proxy**: the real client IP. Configure `TRUSTED_PROXIES` (see
  [Running behind a reverse proxy](#running-behind-a-reverse-proxy)).

If unsure, make a request and check the `client_ip` field of the rejected `http.request` log line.

### Step 3: Add the client to `config/clients.yaml`

```yaml
clients:
  - alias: alice-claude-code
    api_key: mcp_5b0e2c…
    ip: 10.10.7.42
    enabled: true
    allowed_servers: [web-prod01]   # optional
```

### Step 4: Restart and test

```bash
docker compose restart
curl -i -X POST http://localhost:8000/mcp \
  -H "Authorization: Bearer mcp_5b0e2c…" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

`200` means you're in. `401` means the key is wrong or the client is disabled. `403` means the IP
isn't allowed.

To **revoke** a client, set `enabled: false` (or delete the entry) and restart. To **rotate** a key,
replace `api_key` and restart.

---

## Connecting AI clients

Authenticate with either header:

```
Authorization: Bearer mcp_<key>
X-API-Key: mcp_<key>
```

API keys are never accepted in the query string.

### Claude Code

```bash
claude mcp add --transport http linux-mcp https://<mcp-host>/mcp \
  --header "Authorization: Bearer mcp_<key>"
```

Or in a project `.mcp.json`:

```json
{
  "mcpServers": {
    "linux-mcp": {
      "type": "http",
      "url": "https://<mcp-host>/mcp",
      "headers": { "Authorization": "Bearer ${LINUX_MCP_KEY}" }
    }
  }
}
```

### Claude Desktop

Use the [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) bridge so the connection comes
from your own machine (whose IP you allow in `clients.yaml`). Edit `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "linux-mcp": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote", "https://<mcp-host>/mcp",
        "--header", "Authorization:${AUTH_HEADER}"
      ],
      "env": { "AUTH_HEADER": "Bearer mcp_<key>" }
    }
  }
}
```

### Anthropic Messages API (MCP connector)

Anthropic's servers make the call. The endpoint must be publicly reachable over HTTPS, and the
client's `ip` must cover Anthropic's published egress ranges.

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
    messages=[{"role": "user", "content": "Check disk usage on prod-db01"}],
)
```

### OpenAI Responses API (remote MCP)

The same reachability and IP-range notes apply.

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
    input="Check disk usage on prod-db01",
)
```

### Python MCP SDK / LangChain

See [examples/mcp_client.py](examples/mcp_client.py) for a minimal client using the official
`mcp` Python SDK. Any framework that speaks MCP over **Streamable HTTP** works the same way:
point it at `/mcp` and send the auth header.

---

## Running behind a reverse proxy

Put the server behind a TLS-terminating proxy for anything beyond a trusted LAN. Set
`TRUSTED_PROXIES` in `.env` to the proxy's IP as the **container** sees it. When the proxy runs on
the same host, that is often the Docker bridge gateway (for example `172.17.0.1`, or the compose
network's gateway: check with `docker network inspect`).

Nginx example (SSE requires buffering off):

```nginx
location /mcp {
    proxy_pass http://127.0.0.1:8000/mcp;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_buffering off;
    proxy_read_timeout 300s;
}
```

Forwarding headers from untrusted peers are ignored, so callers cannot spoof their IP.

---

## Logging and auditing

All logs go to stdout as JSON, one object per line (`docker compose logs`). Every request to
`/mcp` produces an `http.request` event, and every tool call produces audit events
(`command.received`, `command.blocked`, `command.completed`, `command.failed`, `tool.rejected`)
with the client alias, client IP, server, command, exit code, output and duration. Events from the
same request share a `request_id`, which is also returned as the `X-Request-ID` response header.

```json
{"timestamp": "2026-10-30T08:15:02.481+00:00", "level": "INFO", "logger": "linux_mcp.audit", "message": "command completed", "request_id": "d2b007c0…", "client": "ops-agent", "client_ip": "10.0.5.20", "tool": "run_read_command", "server": "prod-db01", "event": "command.completed", "command": "df -h", "exit_code": 0, "duration_ms": 182.4, "stdout": "Filesystem  Size  Used …", "stderr": ""}
```

Quick queries:

```bash
docker compose logs --no-log-prefix linux-mcp | jq -c 'select(.event == "command.completed") | {timestamp, client, server, command, exit_code}'
docker compose logs --no-log-prefix linux-mcp | jq -c 'select(.event == "command.blocked" or .status == 401 or .status == 403)'
```

The lines can be shipped as-is to Loki, Elasticsearch, Splunk or Datadog. The full event reference
is in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#4-logging).

> ⚠️ Command output can contain sensitive data (config files, environment variables). Treat logs
> as sensitive, or lower `LOG_MAX_OUTPUT_CHARS`.

---

## Running without Docker

Requires Python 3.12+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env            # then edit config/ and ssh/ as above
set -a && source .env && set +a
linux-mcp                  # listens on MCP_HOST:MCP_PORT (default 0.0.0.0:8000)
```

For a simple background service, use `scripts/startup.sh`, `scripts/status.sh` and
`scripts/shutdown.sh`. They read `.env` and write logs to `mcp.log`.

---

## Development

```bash
pip install -e ".[dev]"
python -m pytest -q
```

Project layout:

```
src/linux_mcp/
  server.py          MCP tools, Starlette app, entry point
  middleware.py      API-key + IP authentication, proxy handling, access log
  auth.py            key matching (constant-time) and key generator
  validator.py       read / write command validation rules
  ssh_client.py      pooled Paramiko SSH connections
  config.py          env settings and YAML loaders (pydantic)
  logging_config.py  JSON logging
config/              *.example.yaml templates
ssh/                 your keys (git-ignored)
docs/                deployment guide, client config samples
examples/            Python MCP client example
examples/agents/     example agent skills built on linux-mcp (Oracle EBS)
tests/               pytest suite
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs the tests on every push and pull
request. On pushes to `main` it publishes the image to GHCR with the `latest` and `sha-<short>`
tags.

---

## Security

> [!CAUTION]
> linux-mcp lets an AI run commands on your servers with the privileges of the configured SSH
> user. Keep it inside your trusted network. Never publish it directly on the internet, and always
> put HTTPS in front of it in production.

### Built-in protections

Every request to `/mcp` passes through these layers in order. A failure at any layer stops the
request.

| Layer | Protection | Where it is configured |
|---|---|---|
| **API key** | Every request must send a key in `Authorization: Bearer <key>` or `X-API-Key`. Keys in the query string are ignored, so they don't end up in proxy or browser logs. Keys are compared in constant time against every client, so response timing doesn't reveal which client matched. A missing or wrong key returns `401`. | `api_key` in `clients.yaml` |
| **Strong, unique keys** | Keys must be at least 16 characters and unique across clients. The bundled generator produces `mcp_` + 64 hex chars from the OS random source. Invalid config stops the server at startup. | `linux-mcp-keygen` / `python -m linux_mcp.auth` |
| **Client enable switch** | A client can be disabled without removing it. Its key then returns `401`. | `enabled` in `clients.yaml` |
| **IP allow-list** | After the key matches, the caller's IP must be in that client's IP/CIDR list (IPv4 and IPv6). A valid key from any other address returns `403`. A stolen key is useless from outside the allowed range. | `ip` in `clients.yaml` |
| **Spoof-proof client IP** | `X-Forwarded-For` / `X-Real-IP` are honored **only** when the direct peer is a trusted proxy. Otherwise they are ignored, so callers can't fake an allowed IP. Uvicorn's own proxy-header handling is turned off (`--no-proxy-headers`) so only this logic applies. | `TRUSTED_PROXIES` in `.env` |
| **Server scoping** | A client only sees and uses the servers listed for it. Unknown names fail validation at startup. | `allowed_servers` in `clients.yaml` |
| **Host IPs hidden** | Clients refer to servers by name only. Hostnames, IPs, usernames and key paths are never returned. | `servers.yaml` |
| **Read/write separation** | `run_read_command` accepts only allow-listed diagnostic programs and blocks write, delete, service-control, privilege-escalation and subshell patterns. `run_write_command` is refused unless the server has `allow_write: true`, and catastrophic commands are blocked even then. | `allow_write` in `servers.yaml` |
| **SSH host verification** | With strict mode on, connections to hosts not in `known_hosts` are refused, which prevents man-in-the-middle attacks. | `SSH_STRICT_HOST_KEYS=true` + `ssh/known_hosts` |
| **Timeouts** | SSH connections and commands are cut off after the configured number of seconds. | `timeout` in `servers.yaml` |
| **Minimal error disclosure** | Callers only get `Unauthorized` or `Forbidden`. The detailed reason is written to the server log only. | n/a |
| **Audit trail** | Every request and command is logged as JSON with client alias, real client IP, server, command, exit code, output and duration, correlated by `request_id`. | `LOG_LEVEL`, `LOG_FILE` |
| **Container hardening** | The image runs as a non-root user (UID 5678). Config and SSH keys are mounted **read-only**. Only `/health` is unauthenticated, and it returns nothing but `{"status": "ok"}`. | `Dockerfile`, compose files |

### Recommendations

- **Do not expose the service to the internet.** Bind or firewall the published port so only your
  trusted network (or your reverse proxy) can reach it.
- **The validator reduces risk; it is not a sandbox.** Pattern-based validation cannot catch every
  harmful command. Use a dedicated, least-privileged SSH user (not `root`) wherever possible, and
  keep `allow_write: false` unless you need it.
- Read-only commands like `cat`, `env` and `journalctl` can still **read secrets**. Choose the SSH
  user's file permissions with that in mind.
- Enable `SSH_STRICT_HOST_KEYS=true` in production to prevent man-in-the-middle attacks.
- Serve the endpoint over **HTTPS** through a reverse proxy such as nginx in production. The
  server itself only speaks HTTP, and API keys are bearer tokens that anyone who intercepts them can
  reuse.
- Give each client its own key, the narrowest `ip` range possible, and `allowed_servers` when it
  doesn't need everything.
- Prefer clients that ask for confirmation before `run_write_command`. The tool is annotated
  `destructiveHint: true` for this purpose.

If you find a security issue, please report it privately via GitHub Security Advisories rather than
a public issue.

## Contributing

Issues and pull requests are welcome. Please include tests for validator changes. Every new allowed
or blocked pattern should come with a test case in `tests/test_security.py`.

## License

Licensed under the [Apache License, Version 2.0](https://www.apache.org/licenses/LICENSE-2.0).
See [LICENSE](LICENSE) for the full text.

Unless required by applicable law or agreed to in writing, software distributed under the License
is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
implied. See the License for the specific language governing permissions and limitations under the
License.
