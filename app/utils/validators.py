"""URL validation and SSRF protection.

Every URL we fetch (the submitted one, each redirect hop, extra pages and
competitor sites) goes through `ensure_public_url`, which resolves the host and
refuses anything that lands on a private, loopback, link-local or otherwise
non-public address.
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

from app.exceptions import BlockedURLError, InvalidURLError, WebsiteFetchError

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {None, 80, 443, 8080, 8443}
BLOCKED_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".intranet", ".lan", ".home.arpa", ".corp")
BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata"}


def normalize_url(raw: str) -> str:
    """Basic syntax checks + normalisation. Does not touch the network."""
    if raw is None:
        raise InvalidURLError("URL is required.")

    url = raw.strip()
    if not url:
        raise InvalidURLError("URL is required.")
    if len(url) > 2048:
        raise InvalidURLError("URL is too long (max 2048 characters).")
    if any(ch.isspace() for ch in url):
        raise InvalidURLError("URL must not contain spaces.")

    # People often paste "example.com" without a scheme
    if "://" not in url:
        url = f"https://{url}"

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidURLError("Only http and https URLs are supported.")
    if parts.username or parts.password:
        raise InvalidURLError("URLs with embedded credentials are not allowed.")

    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise InvalidURLError("URL does not contain a valid host name.")

    try:
        port = parts.port
    except ValueError:
        raise InvalidURLError("URL contains an invalid port.") from None
    if port not in ALLOWED_PORTS:
        raise BlockedURLError(f"Port {port} is not allowed. Use a standard web port (80/443).")

    literal_ip = _parse_ip(host)
    if literal_ip is not None:
        if not is_public_ip(literal_ip):
            raise BlockedURLError("URLs pointing to private or internal IP addresses are not allowed.")
    else:
        if host in BLOCKED_HOSTS or host.endswith(BLOCKED_HOST_SUFFIXES):
            raise BlockedURLError("URLs pointing to local or internal hosts are not allowed.")
        if "." not in host:
            raise InvalidURLError("URL must contain a full domain name, e.g. https://example.com")
        try:
            host.encode("idna")
        except UnicodeError:
            raise InvalidURLError("URL contains an invalid domain name.") from None

    netloc = host if port is None else f"{host}:{port}"
    if literal_ip is not None and literal_ip.version == 6:
        netloc = f"[{host}]" if port is None else f"[{host}]:{port}"

    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _parse_ip(host: str):
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


async def resolve_host(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise WebsiteFetchError(
            f"Could not resolve the domain '{host}'. Check the URL and try again.",
            code="dns_resolution_failed",
        ) from None
    return list({info[4][0] for info in infos})


async def ensure_public_url(url: str) -> str:
    """Normalise the URL and make sure every resolved address is public."""
    normalized = normalize_url(url)
    parts = urlsplit(normalized)
    port = parts.port or (443 if parts.scheme == "https" else 80)

    addresses = await resolve_host(parts.hostname, port)
    for address in addresses:
        ip = _parse_ip(address.split("%")[0])
        if ip is None or not is_public_ip(ip):
            raise BlockedURLError("This URL resolves to a private or internal network address and cannot be analysed.")
    return normalized


def same_site(url_a: str, url_b: str) -> bool:
    """True when both URLs belong to the same site (ignores a leading www.)."""

    def bare(url):
        host = (urlsplit(url).hostname or "").lower()
        return host[4:] if host.startswith("www.") else host

    return bare(url_a) == bare(url_b)
