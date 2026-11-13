import ipaddress
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _parse_networks(value: str | list[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    items = [value] if isinstance(value, str) else value
    return [ipaddress.ip_network(v.strip(), strict=False) for v in items if v and v.strip()]


# ── Settings (environment) ─────────────────────────────────────────────────
class AppSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    servers_config: Path = Path("./config/servers.yaml")
    clients_config: Path = Path("./config/clients.yaml")
    ssh_key_dir: Path = Path("./ssh")
    ssh_strict_host_keys: bool = False
    trusted_proxies: str = ""
    log_level: str = "INFO"
    access_log_format: Literal["json", "plain"] = "json"   # plain = uvicorn's standard access log
    log_file: Path | None = None           # optional JSON log file, in addition to stdout
    log_file_max_bytes: int = 10 * 1024 * 1024
    log_file_backups: int = 5
    log_max_output_chars: int = 10000      # per stdout/stderr field in the audit log; 0 = no limit

    @classmethod
    def from_env(cls) -> "AppSettings":
        return cls(
            host=os.getenv("MCP_HOST", "0.0.0.0"),
            port=int(os.getenv("MCP_PORT", "8000")),
            servers_config=Path(os.getenv("SERVERS_CONFIG", "./config/servers.yaml")),
            clients_config=Path(os.getenv("CLIENTS_CONFIG", "./config/clients.yaml")),
            ssh_key_dir=Path(os.getenv("SSH_KEY_DIR", "./ssh")),
            ssh_strict_host_keys=_env_bool("SSH_STRICT_HOST_KEYS"),
            trusted_proxies=os.getenv("TRUSTED_PROXIES", ""),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            access_log_format=os.getenv("ACCESS_LOG_FORMAT", "json").strip().lower(),
            log_file=Path(os.environ["LOG_FILE"]) if os.getenv("LOG_FILE") else None,
            log_file_max_bytes=int(os.getenv("LOG_FILE_MAX_BYTES", str(10 * 1024 * 1024))),
            log_file_backups=int(os.getenv("LOG_FILE_BACKUPS", "5")),
            log_max_output_chars=int(os.getenv("LOG_MAX_OUTPUT_CHARS", "10000")),
        )

    @property
    def trusted_proxy_networks(self):
        return _parse_networks(self.trusted_proxies.split(","))

    @property
    def known_hosts_file(self) -> Path:
        return self.ssh_key_dir / "known_hosts"


# ── Remote Servers ─────────────────────────────────────────────────────────
class ServerConfig(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    host: str                       # never exposed to MCP clients
    description: str = ""
    allow_write: bool = False
    username: str = "root"
    port: int = 22
    key_file: str = "id_rsa"
    timeout: int = 30

    def key_path(self, settings: AppSettings) -> str:
        path = Path(self.key_file)
        return str(path if path.is_absolute() else settings.ssh_key_dir / path)


# ── MCP Clients ────────────────────────────────────────────────────────────
class ClientConfig(BaseModel):
    alias: str
    api_key: str = Field(min_length=16)
    ip: str | list[str]             # single IP/CIDR or a list of them
    enabled: bool = True
    allowed_servers: list[str] | None = None   # None = all servers

    @field_validator("ip")
    @classmethod
    def _validate_ip(cls, v):
        _parse_networks(v)          # raises ValueError on invalid entries
        return v

    @property
    def networks(self):
        return _parse_networks(self.ip)

    def can_access(self, server_name: str) -> bool:
        return self.allowed_servers is None or server_name in self.allowed_servers


# ── Loaders ────────────────────────────────────────────────────────────────
def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_servers(path: Path) -> dict[str, ServerConfig]:
    data = _read_yaml(path)
    defaults = data.get("defaults") or {}
    servers: dict[str, ServerConfig] = {}
    for entry in data.get("servers") or []:
        server = ServerConfig(**{**defaults, **entry})
        if server.name in servers:
            raise ValueError(f"Duplicate server name in {path}: {server.name}")
        servers[server.name] = server
    if not servers:
        raise ValueError(f"No servers defined in {path}")
    return servers


def load_clients(path: Path, servers: dict[str, ServerConfig]) -> list[ClientConfig]:
    data = _read_yaml(path)
    clients: list[ClientConfig] = []
    aliases, keys = set(), set()
    for entry in data.get("clients") or []:
        client = ClientConfig(**entry)
        if client.alias in aliases:
            raise ValueError(f"Duplicate client alias in {path}: {client.alias}")
        if client.api_key in keys:
            raise ValueError(f"Duplicate API key in {path} (client {client.alias})")
        unknown = set(client.allowed_servers or []) - servers.keys()
        if unknown:
            raise ValueError(f"Client {client.alias} references unknown servers: {sorted(unknown)}")
        aliases.add(client.alias)
        keys.add(client.api_key)
        clients.append(client)
    if not clients:
        raise ValueError(f"No clients defined in {path}")
    return clients


@lru_cache
def get_settings() -> AppSettings:
    return AppSettings.from_env()


@lru_cache
def get_servers() -> dict[str, ServerConfig]:
    return load_servers(get_settings().servers_config)


@lru_cache
def get_clients() -> list[ClientConfig]:
    return load_clients(get_settings().clients_config, get_servers())


#- Generate private and public key
# ssh-keygen -t rsa -b 4096 -f ./ssh/id_rsa

#- copy the public key to each remote server
# ssh-copy-id -i ./ssh/id_rsa.pub root@<server-ip>
