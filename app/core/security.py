"""Security utilities: SSRF validation, credential/secret filtering, and constant-time token verification."""
from __future__ import annotations

import hmac
import ipaddress
import re
import socket
from typing import Callable, Iterable, List, Optional, Tuple
from urllib.parse import urlparse


class SSRFValidationError(ValueError):
    """Raised when a URL violates SSRF safety policy."""


ALLOWED_SCHEMES = {"http", "https"}
BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",
    "metadata.google.internal",
    "instance-data",
}
BLOCKED_HOSTNAME_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".home.arpa",
    ".corp",
)

# Regex patterns to identify and strip credentials, Apple IDs, passwords, tokens, and session secrets
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
BEARER_OR_TOKEN_RE = re.compile(
    r"(?i)\b(bearer\s+[A-Za-z0-9._\-~+/]+=*|bot\s+[A-Za-z0-9._\-~+/]+=*|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
)

SENSITIVE_LINE_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(password|passwd|pass|pwd|mật\s*khẩu|mat\s*khau|mk)\s*[:=\-]\s*\S+"),
    re.compile(r"(?i)\b(apple\s*id|id\s*apple|icloud|tài\s*khoản|tai\s*khoan|tk|username|user\s*id)\s*[:=\-]\s*\S+"),
    re.compile(r"(?i)\b(recovery\s*code|recovery\s*key|mã\s*khôi\s*phục|mã\s*bảo\s*mật|2fa|otp|verification\s*code)\s*[:=\-]?\s*\S+"),
    re.compile(r"(?i)\b(session\s*id|session_token|cookie|set-cookie|auth_token|access_token|refresh_token|api_key|secret_key)\s*[:=\-]\s*\S+"),
    re.compile(r"(?i)\b[A-Za-z0-9._%+-]+@(icloud\.com|me\.com|mac\.com|gmail\.com|yahoo\.com|outlook\.com|hotmail\.com)\b"),
)

SENSITIVE_TITLE_KEYWORDS: Tuple[str, ...] = (
    "password",
    "passwd",
    "mật khẩu",
    "mat khau",
    "apple id",
    "id apple",
    "@icloud.com",
    "@gmail.com",
    "recovery code",
    "session cookie",
    "login secret",
    "2fa code",
    "tk:",
    "mk:",
    "pass:",
)


def is_ip_public_and_safe(ip_str: str) -> bool:
    """Return True only if IP is a globally routable public unicast address."""
    try:
        ip_obj = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        ip_obj = ip_obj.ipv4_mapped

    if (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    ):
        return False

    # Carrier-grade NAT 100.64.0.0/10 and cloud metadata 169.254.169.254
    if isinstance(ip_obj, ipaddress.IPv4Address):
        cgnat = ipaddress.IPv4Network("100.64.0.0/10")
        benchmark = ipaddress.IPv4Network("198.18.0.0/15")
        if ip_obj in cgnat or ip_obj in benchmark:
            return False

    return bool(ip_obj.is_global)


def default_dns_resolver(hostname: str, port: int) -> List[str]:
    """Resolve hostname to IP strings via system getaddrinfo."""
    infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    ips: List[str] = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr and isinstance(sockaddr[0], str):
            ip_str = sockaddr[0].split("%", 1)[0]
            if ip_str not in ips:
                ips.append(ip_str)
    return ips


def validate_url_for_ssrf(
    url: str,
    *,
    resolver: Optional[Callable[[str, int], Iterable[str]]] = None,
    resolve_dns: bool = True,
) -> str:
    """Validate URL scheme, hostname, credentials, and resolved IP addresses against SSRF."""
    if not url or not isinstance(url, str):
        raise SSRFValidationError("URL must be a non-empty string")

    cleaned = url.strip()
    parsed = urlparse(cleaned)

    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SSRFValidationError(f"Disallowed URL scheme '{scheme}'; only http and https are allowed")

    if parsed.username is not None or parsed.password is not None:
        raise SSRFValidationError("URLs containing embedded credentials are not permitted")

    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        raise SSRFValidationError("URL must include a valid hostname")

    if hostname in BLOCKED_HOSTNAMES or any(hostname.endswith(sfx) for sfx in BLOCKED_HOSTNAME_SUFFIXES):
        raise SSRFValidationError(f"Blocked internal or local hostname: {hostname}")

    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise SSRFValidationError("Invalid URL port") from exc

    # Check if hostname is already an IP literal
    try:
        ipaddress.ip_address(hostname)
        is_literal_ip = True
    except ValueError:
        is_literal_ip = False

    if is_literal_ip:
        if not is_ip_public_and_safe(hostname):
            raise SSRFValidationError(f"Disallowed non-public IP address: {hostname}")
        return cleaned

    if resolve_dns:
        dns_fn = resolver or default_dns_resolver
        try:
            resolved_ips = list(dns_fn(hostname, port))
        except socket.gaierror as exc:
            raise SSRFValidationError(f"DNS resolution failed for host '{hostname}': {exc}") from exc
        except Exception as exc:
            raise SSRFValidationError(f"Unable to resolve host '{hostname}': {exc}") from exc

        if not resolved_ips:
            raise SSRFValidationError(f"Host '{hostname}' did not resolve to any IP addresses")

        for ip_str in resolved_ips:
            if not is_ip_public_and_safe(ip_str):
                raise SSRFValidationError(
                    f"Host '{hostname}' resolved to disallowed non-public IP address: {ip_str}"
                )

    return cleaned


def contains_sensitive_credentials(text: str) -> bool:
    """Return True if a line or candidate string looks like login credentials or secrets."""
    if not text:
        return False
    stripped = text.strip()
    if not stripped:
        return False

    lower = stripped.lower()
    for kw in SENSITIVE_TITLE_KEYWORDS:
        if kw in lower:
            return True

    if EMAIL_RE.search(stripped):
        return True

    if BEARER_OR_TOKEN_RE.search(stripped):
        return True

    for pattern in SENSITIVE_LINE_PATTERNS:
        if pattern.search(stripped):
            return True

    return False


def redact_sensitive_text(text: str) -> str:
    """Redact passwords, tokens, email addresses, and secrets from a string for safe logging/storage."""
    if not text:
        return ""
    redacted = EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    redacted = BEARER_OR_TOKEN_RE.sub("[REDACTED_TOKEN]", redacted)
    for pattern in SENSITIVE_LINE_PATTERNS:
        redacted = pattern.sub("[REDACTED_CREDENTIAL]", redacted)
    return redacted


def sanitize_text_lines(lines: Iterable[str]) -> List[str]:
    """Filter out any lines containing credentials, emails, session tokens, or login secrets."""
    safe_lines: List[str] = []
    for raw in lines:
        cleaned = " ".join(raw.split()).strip()
        if not cleaned:
            continue
        if contains_sensitive_credentials(cleaned):
            continue
        safe_lines.append(cleaned)
    return safe_lines


def verify_admin_token(provided_token: Optional[str], expected_token: str) -> bool:
    """Constant-time verification of Bearer admin token."""
    if not provided_token or not expected_token:
        return False
    a = provided_token.strip().encode("utf-8")
    b = expected_token.strip().encode("utf-8")
    if len(a) == 0 or len(b) == 0:
        return False
    return hmac.compare_digest(a, b)
