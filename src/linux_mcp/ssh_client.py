import threading
from dataclasses import dataclass

import paramiko

from .config import ServerConfig, get_settings


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int

    def to_text(self) -> str:
        """The result as returned to the MCP client."""
        result = self.stdout
        if self.stderr:
            result += f"\n[STDERR]: {self.stderr}"
        if self.exit_code != 0:
            result += f"\n[Exit Code]: {self.exit_code}"
        return result or "(no output)"

# One cached SSH connection per configured server name
_clients: dict[str, paramiko.SSHClient] = {}
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(name: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(name, threading.Lock())


def _is_active(client: paramiko.SSHClient | None) -> bool:
    transport = client.get_transport() if client else None
    return bool(transport and transport.is_active())


def _connect(server: ServerConfig) -> paramiko.SSHClient:
    settings = get_settings()
    client = paramiko.SSHClient()
    if settings.ssh_strict_host_keys:
        client.load_host_keys(str(settings.known_hosts_file))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        server.host,
        port=server.port,
        username=server.username,
        key_filename=server.key_path(settings),
        timeout=server.timeout,
        allow_agent=False,
        look_for_keys=False,
    )
    return client


def get_ssh_client(server: ServerConfig) -> paramiko.SSHClient:
    """Returns the cached SSH client for a server, reconnecting if needed."""
    with _lock_for(server.name):
        client = _clients.get(server.name)
        if not _is_active(client):
            if client:
                client.close()
            client = _connect(server)
            _clients[server.name] = client
        return client


def _drop_client(server: ServerConfig) -> None:
    with _lock_for(server.name):
        client = _clients.pop(server.name, None)
        if client:
            client.close()


def execute_command(server: ServerConfig, command: str) -> CommandResult:
    """Execute a pre-validated command on the given server and return its output and exit code."""
    ssh = get_ssh_client(server)
    try:
        _, stdout, stderr = ssh.exec_command(command, timeout=server.timeout)
    except (paramiko.SSHException, OSError, EOFError):
        # Stale connection — reconnect once
        _drop_client(server)
        ssh = get_ssh_client(server)
        _, stdout, stderr = ssh.exec_command(command, timeout=server.timeout)

    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    exit_code = stdout.channel.recv_exit_status()
    return CommandResult(stdout=out, stderr=err, exit_code=exit_code)
