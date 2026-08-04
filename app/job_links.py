import ipaddress
import socket
from urllib.parse import urlsplit


OFFICIAL_SOURCE_DOMAINS = {
    "greenhouse": ("greenhouse.io",),
    "lever": ("lever.co",),
    "usajobs": ("usajobs.gov",),
}
DISCOVERY_DOMAINS = (
    "linkedin.com",
    "indeed.com",
    "adzuna.com",
    "himalayas.app",
    "remotive.com",
)


def public_https_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or len(url) > 2048 or any(ord(character) < 32 for character in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    hostname = (parts.hostname or "").lower().rstrip(".")
    if (
        parts.scheme != "https"
        or not hostname
        or parts.username is not None
        or parts.password is not None
        or port not in {None, 443}
        or hostname == "localhost"
        or hostname.endswith(
            (".localhost", ".local", ".internal", ".lan", ".home")
        )
        or "%" in hostname
    ):
        return None
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            return None
    try:
        ipv4_bytes = socket.inet_aton(hostname)
    except OSError:
        pass
    else:
        if not ipaddress.ip_address(ipv4_bytes).is_global:
            return None
    return url


def hostname_matches(hostname: str, domains: tuple[str, ...]) -> bool:
    return any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in domains
    )


def job_link_role(source: object, url: object) -> str:
    validated = public_https_url(url)
    if validated is None:
        return "discovery"
    hostname = (urlsplit(validated).hostname or "").lower().rstrip(".")
    normalized_source = str(source or "").strip().lower()
    expected_domains = OFFICIAL_SOURCE_DOMAINS.get(normalized_source)
    if expected_domains and hostname_matches(hostname, expected_domains):
        return "official"
    if normalized_source == "manual" and not hostname_matches(
        hostname, DISCOVERY_DOMAINS
    ):
        return "official"
    return "discovery"


def validate_manual_application_url(value: object) -> str:
    url = public_https_url(value)
    if url is None:
        raise ValueError("Application URL must be a public HTTPS URL")
    hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    if hostname_matches(hostname, DISCOVERY_DOMAINS):
        raise ValueError("Application URL must not be a discovery-provider URL")
    return url
