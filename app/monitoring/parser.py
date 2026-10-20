"""HTML parser for extracting public account credentials (<div class="copy-buttons">) and catalog metadata."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup, Comment

from app.core.security import (
    contains_sensitive_credentials,
    redact_sensitive_text,
    sanitize_text_lines,
)
from app.services.catalog import normalize_app_name

# Tags removed after extracting copy-buttons input values
NOISE_TAGS = (
    "script",
    "style",
    "noscript",
    "iframe",
    "form",
    "input",
    "button",
    "select",
    "textarea",
    "svg",
    "canvas",
    "nav",
    "footer",
    "aside",
    "header",
)

NOISE_ATTR_TOKENS = (
    "comment",
    "sidebar",
    "footer",
    "navbar",
    "nav-",
    "share-button",
    "social",
    "advert",
    "adsense",
    "banner",
    "cookie",
    "popup",
    "timestamp",
    "post-footer",
    "post-timestamp",
    "blog-pager",
    "popular-posts",
    "attribution",
    "widget",
    "header",
    "menu",
    "breadcrumb",
)

IGNORED_CANDIDATE_PHRASES = (
    "hướng dẫn",
    "huong dan",
    "sử dụng",
    "su dung",
    "lưu ý",
    "luu y",
    "quan trọng",
    "quan trong",
    "không đăng nhập",
    "khong dang nhap",
    "chỉ đăng nhập",
    "chi dang nhap",
    "đăng nhập",
    "dang nhap",
    "đăng xuất",
    "dang xuat",
    "tài khoản",
    "tai khoan",
    "mật khẩu",
    "mat khau",
    "nhấn nút",
    "nhan nut",
    "sao chép",
    "sao chep",
    "id apple",
    "apple id",
    "app store",
    "icloud",
    "cài đặt",
    "cai dat",
    "bước 1",
    "bước 2",
    "bước 3",
    "bước 4",
    "bước 5",
    "buoc 1",
    "buoc 2",
    "buoc 3",
    "buoc 4",
    "buoc 5",
    "danh sách game",
    "danh sach game",
    "danh sách ứng dụng",
    "danh sach ung dung",
    "tải game",
    "tai game",
    "tải xuống",
    "tai xuong",
    "đã mua",
    "da mua",
    "không cập nhật",
    "khong cap nhat",
    "bài đăng",
    "bai dang",
    "bình luận",
    "binh luan",
    "chia sẻ",
    "chia se",
    "xem thêm",
    "xem them",
    "trang chủ",
    "trang chu",
    "liên hệ",
    "lien he",
    "thông báo",
    "thong bao",
    "app list",
    "game list",
    "click here",
    "read more",
    "older post",
    "newer post",
    "home",
    "subscribe",
    "telegram",
    "discord",
    "facebook",
    "youtube",
    "powered by blogger",
)

BULLET_LINE_RE = re.compile(r"^\s*(?:[\-\*\•\+\➢\➤\✓\✔]|\d+[\.\)])\s+(.+?)\s*$")
DYNAMIC_NOISE_RE = re.compile(
    r"(?i)\b(updated\s+at|last\s+updated|views|lượt\s+xem|posted\s+on)\s*[:\-]?\s*[\d/:,\-\sAPMapm]+"
)
EMAIL_INPUT_IDS = {"email", "user", "username", "account", "taikhoan", "tk", "idapple", "appleid"}
PASSWORD_INPUT_IDS = {"password", "pass", "pwd", "matkhau", "mk"}


@dataclass
class ParsedArticle:
    """Structured representation of a monitored listing page including copy-buttons credentials."""

    title: str
    apps: List[str] = field(default_factory=list)
    sanitized_body_lines: List[str] = field(default_factory=list)
    content_hash: str = ""
    had_redacted_lines: bool = False
    account_email: Optional[str] = None
    account_password: Optional[str] = None


def _extract_copy_buttons_credentials(soup: BeautifulSoup) -> Tuple[Optional[str], Optional[str]]:
    """Extract account email and password from <div class='copy-buttons'> or matching <input> fields."""
    email_val: Optional[str] = None
    password_val: Optional[str] = None

    # 1. Inspect <input> tags by id or name attribute
    for inp in soup.find_all("input"):
        if inp.attrs is None:
            continue
        raw_val = (inp.get("value") or "").strip()
        if not raw_val:
            continue
        ident = f"{inp.get('id') or ''} {inp.get('name') or ''}".strip().lower()
        ident_clean = re.sub(r"[^a-z0-9]", "", ident)
        if email_val is None and any(k in ident_clean for k in EMAIL_INPUT_IDS):
            email_val = raw_val
        elif password_val is None and any(k in ident_clean for k in PASSWORD_INPUT_IDS):
            password_val = raw_val

    # 2. Fallback: look inside .copy-buttons container for two consecutive <input value="..."> tags
    if email_val is None or password_val is None:
        copy_box = soup.select_one(".copy-buttons")
        if copy_box is not None:
            vals = [
                (inp.get("value") or "").strip()
                for inp in copy_box.find_all("input")
                if (inp.get("value") or "").strip()
            ]
            if len(vals) >= 2:
                if email_val is None:
                    email_val = vals[0]
                if password_val is None:
                    password_val = vals[1]

    return email_val, password_val


def is_ignored_candidate_title(text: str) -> bool:
    """Return True if text matches known instructional phrases or non-app UI boilerplate."""
    if not text:
        return True
    lower = " ".join(text.split()).strip().lower()
    if any(phrase.strip() in lower for phrase in IGNORED_CANDIDATE_PHRASES):
        return True
    if lower.endswith((":", ";", "?", "...")):
        return True
    return False


def _clean_candidate_app_title(raw: str) -> Optional[str]:
    """Validate and clean a potential app/game title, returning None if noise or credential line."""
    if not raw:
        return None
    text = " ".join(raw.split()).strip()
    m = BULLET_LINE_RE.match(text)
    if m:
        text = m.group(1).strip()

    if len(text) < 2 or len(text) > 90:
        return None

    lower = text.lower()
    if "http://" in lower or "https://" in lower or "www." in lower:
        return None
    if contains_sensitive_credentials(text):
        return None
    if is_ignored_candidate_title(text):
        return None
    if len(text.split()) > 9:
        return None
    if not normalize_app_name(text):
        return None
    return text


def compute_stable_content_hash(
    title: str,
    apps: List[str],
    body_lines: List[str],
    *,
    account_email: Optional[str] = None,
    account_password: Optional[str] = None,
) -> str:
    """Compute a deterministic SHA-256 hash over extracted credentials and normalized content."""
    clean_title = " ".join((title or "").split()).strip().lower()
    clean_title = DYNAMIC_NOISE_RE.sub("", clean_title).strip()

    normalized_apps = [
        normalize_app_name(a, apply_spelling_fixes=True)
        for a in apps
        if normalize_app_name(a, apply_spelling_fixes=True)
    ]

    normalized_body: List[str] = []
    for line in body_lines:
        if contains_sensitive_credentials(line):
            continue
        scrubbed = DYNAMIC_NOISE_RE.sub("", line)
        scrubbed = " ".join(scrubbed.split()).strip().lower()
        if scrubbed:
            normalized_body.append(scrubbed)

    payload = {
        "title": clean_title,
        "apps": normalized_apps,
        "body": normalized_body[:100],
        "account_email": (account_email or "").strip(),
        "account_password": (account_password or "").strip(),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_public_listing_html(html: str) -> ParsedArticle:
    """Parse HTML safely, extracting <div class='copy-buttons'> credentials and any listed apps."""
    if not html or not html.strip():
        return ParsedArticle(title="", apps=[], sanitized_body_lines=[], content_hash="")

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    # Extract <input id="email"> and <input id="password"> BEFORE decomposing input/button tags
    account_email, account_password = _extract_copy_buttons_credentials(soup)

    for tag_name in NOISE_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    for el in list(soup.find_all(True)):
        if el.attrs is None:
            continue
        id_val = str(el.get("id") or "").lower()
        cls_list = el.get("class") or []
        cls_val = " ".join(str(c) for c in cls_list).lower()
        combined = f"{id_val} {cls_val}"
        if any(tok.lower() in combined for tok in NOISE_ATTR_TOKENS):
            el.decompose()

    title = ""
    for sel in (
        "h1.post-title",
        "h3.post-title",
        "h1.entry-title",
        "article h1",
        "main h1",
        "h1",
        "title",
    ):
        node = soup.select_one(sel)
        if node:
            candidate_title = " ".join(node.get_text("\n", strip=True).splitlines()[0:1]).strip()
            if candidate_title:
                title = redact_sensitive_text(candidate_title)
                break

    container = (
        soup.select_one(".post-body")
        or soup.select_one(".entry-content")
        or soup.select_one("article")
        or soup.select_one("main")
        or soup.select_one("#content")
        or soup.body
        or soup
    )

    extracted_apps: List[str] = []
    seen_norms: set[str] = set()
    had_redacted = False

    def _try_add_app(raw_text: str) -> None:
        nonlocal had_redacted
        if contains_sensitive_credentials(raw_text):
            had_redacted = True
            return
        cleaned = _clean_candidate_app_title(raw_text)
        if not cleaned:
            return
        norm = normalize_app_name(cleaned, apply_spelling_fixes=True)
        if norm and norm not in seen_norms:
            seen_norms.add(norm)
            extracted_apps.append(cleaned)

    for node in container.select("[data-app-title], .app-title, .game-title, .app-item"):
        attr_title = node.get("data-app-title")
        if isinstance(attr_title, str) and attr_title.strip():
            _try_add_app(attr_title)
        else:
            for line in node.get_text("\n", strip=True).splitlines():
                _try_add_app(line)

    for li in container.find_all("li"):
        if li.find("ul") or li.find("ol"):
            continue
        for line in li.get_text("\n", strip=True).splitlines():
            _try_add_app(line)

    raw_text_lines = container.get_text("\n", strip=True).splitlines()
    for line in raw_text_lines:
        if contains_sensitive_credentials(line):
            had_redacted = True
            continue
        m = BULLET_LINE_RE.match(line)
        if m:
            _try_add_app(m.group(1))

    sanitized_lines = sanitize_text_lines(raw_text_lines)
    if len(sanitized_lines) < len([ln for ln in raw_text_lines if ln.strip()]):
        had_redacted = True

    content_hash = compute_stable_content_hash(
        title,
        extracted_apps,
        sanitized_lines,
        account_email=account_email,
        account_password=account_password,
    )

    return ParsedArticle(
        title=title,
        apps=extracted_apps,
        sanitized_body_lines=sanitized_lines,
        content_hash=content_hash,
        had_redacted_lines=had_redacted,
        account_email=account_email,
        account_password=account_password,
    )
