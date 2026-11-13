import hmac
import ipaddress
import os
from dataclasses import dataclass

from .config import ClientConfig


@dataclass
class AuthResult:
    client: ClientConfig | None
    status: int          # 200 = ok, 401 = bad/missing key, 403 = IP not allowed
    reason: str          # for server-side logs only, never returned to the caller


def generate_api_key() -> str:
    """Generate a cryptographically secure API key."""
    return "mcp_" + os.urandom(32).hex()


def ip_in_networks(ip: str, networks) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in networks)


def authenticate(raw_key: str, client_ip: str, clients: list[ClientConfig]) -> AuthResult:
    """Match the API key against every configured client (constant time), then check the IP."""
    if not raw_key:
        return AuthResult(None, 401, "missing API key")

    incoming = raw_key.encode()
    matched: ClientConfig | None = None
    for client in clients:
        # Compare against every client so timing does not reveal which one matched
        if hmac.compare_digest(client.api_key.encode(), incoming):
            matched = client

    if matched is None:
        return AuthResult(None, 401, "invalid API key")
    if not matched.enabled:
        return AuthResult(None, 401, f"client '{matched.alias}' is disabled")
    if not ip_in_networks(client_ip, matched.networks):
        return AuthResult(None, 403, f"IP {client_ip} not allowed for client '{matched.alias}'")
    return AuthResult(matched, 200, "ok")


def main() -> None:
    print(generate_api_key())


if __name__ == "__main__":
    main()
