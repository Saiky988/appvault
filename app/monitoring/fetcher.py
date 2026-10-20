"""Resilient, SSRF-safe asynchronous HTTP fetcher for monitored public pages."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from app.core.config import Settings, get_settings
from app.core.security import SSRFValidationError, redact_sensitive_text, validate_url_for_ssrf


@dataclass
class FetchResult:
    """Outcome of a bounded, SSRF-checked HTTP GET operation."""

    url: str
    final_url: Optional[str] = None
    ok: bool = False
    not_modified: bool = False
    status_code: Optional[int] = None
    content: str = ""
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    error: Optional[str] = None


class SafePageFetcher:
    """Fetches configured public listing pages with SSRF protection, size limits, and retries."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        dns_resolver: Optional[Callable[[str, int], Iterable[str]]] = None,
        resolve_dns: bool = True,
        sleep_func: Callable[[float], any] = asyncio.sleep,
    ) -> None:
        self.settings = settings or get_settings()
        self.transport = transport
        self.dns_resolver = dns_resolver
        self.resolve_dns = resolve_dns
        self._sleep = sleep_func
        self._robots_cache: Dict[str, RobotFileParser] = {}
        self.max_redirects = 5

    def _validate_target(self, url: str) -> str:
        return validate_url_for_ssrf(
            url,
            resolver=self.dns_resolver,
            resolve_dns=self.resolve_dns,
        )

    async def _is_allowed_by_robots(self, client: httpx.AsyncClient, target_url: str) -> bool:
        """Check robots.txt for the target origin if RESPECT_ROBOTS_TXT is enabled."""
        if not self.settings.respect_robots_txt:
            return True
        parsed = urlparse(target_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin in self._robots_cache:
            return self._robots_cache[origin].can_fetch(self.settings.user_agent, target_url)

        robots_url = f"{origin}/robots.txt"
        rp = RobotFileParser()
        try:
            self._validate_target(robots_url)
            resp = await client.get(
                robots_url,
                headers={"User-Agent": self.settings.user_agent},
                follow_redirects=False,
            )
            if resp.status_code == 200 and len(resp.text) <= 65536:
                rp.parse(resp.text.splitlines())
            else:
                rp.allow_all = True
        except Exception:
            rp.allow_all = True

        self._robots_cache[origin] = rp
        return rp.can_fetch(self.settings.user_agent, target_url)

    async def fetch(
        self,
        url: str,
        *,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> FetchResult:
        """Fetch a public URL safely with SSRF checks, redirect validation, and bounded retries."""
        try:
            validated_url = self._validate_target(url)
        except SSRFValidationError as exc:
            return FetchResult(url=url, ok=False, error=f"SSRF validation blocked URL: {exc}")

        timeout = httpx.Timeout(
            connect=self.settings.http_timeout_seconds,
            read=self.settings.http_timeout_seconds,
            write=self.settings.http_timeout_seconds,
            pool=self.settings.http_timeout_seconds,
        )

        headers: Dict[str, str] = {
            "User-Agent": self.settings.user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8,vi;q=0.7",
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        max_attempts = max(1, self.settings.max_retries + 1)
        last_error: Optional[str] = None
        last_status: Optional[int] = None

        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            if not await self._is_allowed_by_robots(client, validated_url):
                return FetchResult(
                    url=url,
                    ok=False,
                    status_code=403,
                    error="Blocked by site robots.txt rules",
                )

            for attempt in range(1, max_attempts + 1):
                current_url = validated_url
                try:
                    for _hop in range(self.max_redirects + 1):
                        self._validate_target(current_url)
                        async with client.stream("GET", current_url, headers=headers) as response:
                            last_status = response.status_code

                            # Handle redirects manually to enforce SSRF validation on every hop
                            if response.status_code in (301, 302, 303, 307, 308):
                                location = response.headers.get("Location")
                                if not location:
                                    return FetchResult(
                                        url=url,
                                        final_url=current_url,
                                        ok=False,
                                        status_code=response.status_code,
                                        error="Redirect response missing Location header",
                                    )
                                next_url = urljoin(current_url, location.strip())
                                self._validate_target(next_url)
                                current_url = next_url
                                continue

                            if response.status_code == 304:
                                return FetchResult(
                                    url=url,
                                    final_url=current_url,
                                    ok=True,
                                    not_modified=True,
                                    status_code=304,
                                    etag=response.headers.get("ETag") or etag,
                                    last_modified=response.headers.get("Last-Modified") or last_modified,
                                )

                            if response.status_code == 429 or response.status_code >= 500:
                                retry_after_hdr = response.headers.get("Retry-After")
                                wait_sec = min(float(retry_after_hdr), 10.0) if (retry_after_hdr and retry_after_hdr.isdigit()) else (0.5 * (2 ** (attempt - 1)))
                                last_error = f"HTTP {response.status_code} from upstream"
                                if attempt < max_attempts:
                                    await self._sleep(wait_sec)
                                    break
                                return FetchResult(
                                    url=url,
                                    final_url=current_url,
                                    ok=False,
                                    status_code=response.status_code,
                                    error=last_error,
                                )

                            if response.status_code >= 400:
                                return FetchResult(
                                    url=url,
                                    final_url=current_url,
                                    ok=False,
                                    status_code=response.status_code,
                                    error=f"HTTP {response.status_code} client error",
                                )

                            content_type = (response.headers.get("Content-Type") or "").lower()
                            if content_type and not any(
                                ct in content_type for ct in ("text/html", "application/xhtml+xml", "text/plain")
                            ):
                                return FetchResult(
                                    url=url,
                                    final_url=current_url,
                                    ok=False,
                                    status_code=response.status_code,
                                    error=f"Unsupported Content-Type: {content_type[:64]}",
                                )

                            content_length_hdr = response.headers.get("Content-Length")
                            if content_length_hdr and content_length_hdr.isdigit():
                                if int(content_length_hdr) > self.settings.max_response_bytes:
                                    return FetchResult(
                                        url=url,
                                        final_url=current_url,
                                        ok=False,
                                        status_code=response.status_code,
                                        error=(
                                            f"Response Content-Length ({content_length_hdr} bytes) exceeds "
                                            f"MAX_RESPONSE_BYTES ({self.settings.max_response_bytes})"
                                        ),
                                    )

                            chunks: list[bytes] = []
                            total_bytes = 0
                            async for chunk in response.aiter_bytes():
                                total_bytes += len(chunk)
                                if total_bytes > self.settings.max_response_bytes:
                                    return FetchResult(
                                        url=url,
                                        final_url=current_url,
                                        ok=False,
                                        status_code=response.status_code,
                                        error=(
                                            f"Response body exceeded MAX_RESPONSE_BYTES "
                                            f"({self.settings.max_response_bytes} bytes)"
                                        ),
                                    )
                                chunks.append(chunk)

                            raw_bytes = b"".join(chunks)
                            encoding = response.encoding or "utf-8"
                            decoded = raw_bytes.decode(encoding, errors="replace")

                            return FetchResult(
                                url=url,
                                final_url=current_url,
                                ok=True,
                                not_modified=False,
                                status_code=response.status_code,
                                content=decoded,
                                etag=response.headers.get("ETag"),
                                last_modified=response.headers.get("Last-Modified"),
                            )
                    else:
                        return FetchResult(
                            url=url,
                            final_url=current_url,
                            ok=False,
                            status_code=last_status,
                            error=f"Exceeded maximum redirects ({self.max_redirects})",
                        )

                except SSRFValidationError as exc:
                    return FetchResult(
                        url=url,
                        final_url=current_url,
                        ok=False,
                        status_code=last_status,
                        error=f"SSRF validation blocked request: {exc}",
                    )
                except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
                    last_error = f"Network/transport error: {redact_sensitive_text(str(exc))}"
                    if attempt < max_attempts:
                        await self._sleep(0.5 * (2 ** (attempt - 1)))
                        continue
                except Exception as exc:
                    last_error = f"Unexpected fetch failure: {redact_sensitive_text(str(exc))}"
                    break

        return FetchResult(
            url=url,
            ok=False,
            status_code=last_status,
            error=last_error or "Failed to fetch URL",
        )
