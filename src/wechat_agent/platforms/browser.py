"""Visible browser adapters for the public-platform connector.

The adapter intentionally drives the normal web UI with a persistent browser
profile.  It does not alter fingerprints, inject private endpoints, bypass a
challenge, or retry a blocked write.  The browser is opened only when the
desktop operator asks for it, and write actions require a separate explicit
confirmation through :class:`PlatformService`.

The selectors are kept in this module because both platforms change their DOM
more often than the rest of the application.  The data returned by
``browse``/``read_comments`` always comes from the current page.  If a
comment does not expose a stable DOM identifier, it is skipped instead of
guessing an identifier from a display name.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil
from threading import RLock
from time import monotonic, sleep
from typing import Any, Mapping
from urllib.parse import quote, urljoin, urlparse

from .models import BrowseItem, PlatformAction, PlatformComment, PlatformName


class BrowserPlatformError(RuntimeError):
    """Expected connector failure that should be shown in the console."""


class PlatformLoginRequired(BrowserPlatformError):
    """The visible browser needs the operator to complete login."""


class PlatformChallengeDetected(BrowserPlatformError):
    """A login, verification, or risk page was detected; stop safely."""


class PlatformWriteBlocked(BrowserPlatformError):
    """A write was blocked by the local safety gates."""


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    platform: PlatformName
    home_url: str
    search_url: str
    item_patterns: tuple[re.Pattern[str], ...]
    comment_selectors: tuple[str, ...]
    editor_selectors: tuple[str, ...]
    login_markers: tuple[str, ...]
    challenge_markers: tuple[str, ...]
    reply_markers: tuple[str, ...] = ("回复", "Reply")
    send_markers: tuple[str, ...] = ("发送", "评论", "发布", "Send", "Post")


PROFILES: dict[PlatformName, BrowserProfile] = {
    "douyin": BrowserProfile(
        platform="douyin",
        home_url="https://www.douyin.com/",
        search_url="https://www.douyin.com/search/{query}",
        item_patterns=(
            re.compile(r"/video/[A-Za-z0-9_-]+"),
            re.compile(r"/note/[A-Za-z0-9_-]+"),
        ),
        comment_selectors=(
            "[data-comment-id]",
            "[data-e2e*='comment']",
            "[class*='comment-item']",
            "[class*='CommentItem']",
        ),
        editor_selectors=(
            "textarea",
            "[contenteditable='true']",
            "input[placeholder*='评论']",
            "input[placeholder*='回复']",
        ),
        login_markers=("登录", "扫码登录", "Log in", "Sign in"),
        challenge_markers=("验证码", "安全验证", "行为异常", "访问受限", "captcha", "verify"),
    ),
    "xiaohongshu": BrowserProfile(
        platform="xiaohongshu",
        home_url="https://www.xiaohongshu.com/explore",
        search_url="https://www.xiaohongshu.com/search_result?keyword={query}",
        item_patterns=(
            re.compile(r"/explore/[A-Za-z0-9_-]+"),
            re.compile(r"/discovery/item/[A-Za-z0-9_-]+"),
        ),
        comment_selectors=(
            "[data-comment-id]",
            "[data-e2e*='comment']",
            "[class*='comment-item']",
            "[class*='CommentItem']",
        ),
        editor_selectors=(
            "textarea",
            "[contenteditable='true']",
            "input[placeholder*='评论']",
            "input[placeholder*='回复']",
        ),
        login_markers=("登录", "扫码登录", "Log in", "Sign in"),
        challenge_markers=("验证码", "安全验证", "行为异常", "访问受限", "captcha", "verify"),
    ),
}


class BrowserPlatformAdapter:
    """A guarded, visible Playwright session for one platform.

    This class is intentionally synchronous because the desktop HTTP console
    and the Weixin UI connector are synchronous.  The persistent context is
    created lazily by ``open`` and remains owned by the local desktop process.
    """

    def __init__(
        self,
        platform: PlatformName,
        *,
        data_dir: Path,
        executable_path: str | None = None,
        account_id: str = "unbound",
        headless: bool = False,
        scroll_steps: int = 5,
        page_timeout_ms: int = 20_000,
        write_enabled: bool = False,
        action_delay_seconds: float = 30.0,
        max_actions_per_hour: int = 5,
    ):
        if platform not in PROFILES:
            raise ValueError(f"unsupported_platform:{platform}")
        self.platform = platform
        self.profile = PROFILES[platform]
        self.account_id = account_id.strip() or "unbound"
        self.data_dir = Path(data_dir) / platform
        self.executable_path = executable_path.strip() if executable_path else None
        self.headless = bool(headless)
        self.scroll_steps = max(1, min(int(scroll_steps), 20))
        self.page_timeout_ms = max(5_000, min(int(page_timeout_ms), 120_000))
        self.write_enabled = bool(write_enabled)
        self.action_delay_seconds = max(0.0, float(action_delay_seconds))
        self.max_actions_per_hour = max(1, min(int(max_actions_per_hour), 100))
        self._lock = RLock()
        self._playwright: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None
        self._items: dict[str, BrowseItem] = {}
        self._comments: dict[str, PlatformComment] = {}
        self._action_times: list[float] = []
        self._last_error: str | None = None

    def login_status(self) -> dict[str, object]:
        with self._lock:
            status: dict[str, object] = {
                "platform": self.platform,
                "mode": "browser",
                "connected": bool(self._context and self._page),
                "logged_in": False,
                "account_id": self.account_id,
                "profile_dir": str(self.data_dir),
                "requires_manual_login": True,
                "write_enabled": self.write_enabled,
                "last_error": self._last_error,
            }
            if not self._page:
                return status
            status["url"] = self._page.url
            try:
                text = self._body_text()
                challenge = self._challenge_reason(text)
                status["challenge"] = challenge
                logged_in = not self._looks_logged_out(self._page.url, text)
                status["logged_in"] = logged_in and not challenge
                status["requires_manual_login"] = not status["logged_in"]
            except Exception as exc:  # browser may have been closed externally
                status["last_error"] = f"{type(exc).__name__}: {exc}"
            return status

    def open(self) -> dict[str, object]:
        """Launch a visible persistent browser and navigate to the home page."""
        with self._lock:
            try:
                self._ensure_browser()
                if not self._page.url or self._page.url == "about:blank":
                    self._page.goto(self.profile.home_url, wait_until="domcontentloaded")
                else:
                    self._page.goto(self.profile.home_url, wait_until="domcontentloaded")
                self._settle()
                self._last_error = None
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                raise BrowserPlatformError(self._last_error) from exc
            return self.login_status()

    def close(self) -> None:
        with self._lock:
            context, playwright = self._context, self._playwright
            self._context = None
            self._page = None
            self._playwright = None
            if context:
                try:
                    context.close()
                except Exception:
                    pass
            if playwright:
                try:
                    playwright.stop()
                except Exception:
                    pass

    def browse(self, query: str = "", limit: int = 20) -> list[BrowseItem]:
        with self._lock:
            self._ensure_ready()
            self._ensure_no_challenge()
            target = (
                self.profile.search_url.format(query=quote(query.strip()))
                if query.strip()
                else self.profile.home_url
            )
            self._goto(target)
            found: dict[str, BrowseItem] = {}
            for _ in range(self.scroll_steps):
                self._ensure_no_challenge()
                for item in self._extract_items():
                    found.setdefault(item.item_id, item)
                    if len(found) >= max(1, min(int(limit), 100)):
                        break
                if len(found) >= max(1, min(int(limit), 100)):
                    break
                self._scroll_once()
            self._items.update(found)
            return list(found.values())[: max(1, min(int(limit), 100))]

    def read_comments(self, item_id: str, limit: int = 50) -> list[PlatformComment]:
        with self._lock:
            self._ensure_ready()
            item = self._items.get(item_id)
            if not item:
                raise ValueError(f"item_not_found:{item_id}")
            self._ensure_no_challenge()
            self._goto(item.url)
            found: dict[str, PlatformComment] = {}
            for _ in range(min(self.scroll_steps, 6)):
                self._ensure_no_challenge()
                for comment in self._extract_comments(item):
                    found.setdefault(comment.comment_id, comment)
                    if len(found) >= max(1, min(int(limit), 200)):
                        break
                if len(found) >= max(1, min(int(limit), 200)):
                    break
                self._scroll_once()
            self._comments.update(found)
            return list(found.values())[: max(1, min(int(limit), 200))]

    def build_comment(self, item_id: str, text: str, *, dry_run: bool = True) -> PlatformAction:
        item = self._items.get(item_id)
        if not item:
            raise ValueError(f"item_not_found:{item_id}")
        return PlatformAction(
            platform=self.platform,
            account_id=self.account_id,
            action_type="comment",
            target_id=item_id,
            text=_validate_text(text),
            target_url=item.url,
            dry_run=dry_run,
            metadata={"source": "current_page_browse", "operator_confirmation_required": True},
        )

    def build_reply(self, comment_id: str, text: str, *, dry_run: bool = True) -> PlatformAction:
        comment = self._comments.get(comment_id)
        if not comment:
            raise ValueError(f"comment_not_found:{comment_id}")
        item = self._items.get(comment.item_id)
        return PlatformAction(
            platform=self.platform,
            account_id=self.account_id,
            action_type="reply",
            target_id=comment_id,
            parent_comment_id=comment_id,
            text=_validate_text(text),
            target_url=item.url if item else None,
            dry_run=dry_run,
            metadata={"source": "current_page_comments", "operator_confirmation_required": True},
        )

    def execute_action(self, action: Mapping[str, object]) -> dict[str, object]:
        """Execute one already queued action after the console confirms it."""
        with self._lock:
            if bool(action.get("dry_run")):
                raise PlatformWriteBlocked("dry_run_action_cannot_be_executed")
            if not self.write_enabled:
                raise PlatformWriteBlocked("platform_write_disabled")
            self._check_write_budget()
            target_url = str(action.get("target_url") or "").strip()
            if not target_url:
                raise BrowserPlatformError("action_target_url_missing")
            if not self._allowed_target_url(target_url):
                raise PlatformWriteBlocked("action_target_host_not_allowed")
            self._ensure_ready()
            self._ensure_no_challenge()
            self._goto(target_url)
            self._ensure_no_challenge()
            action_type = str(action.get("action_type") or "")
            text = _validate_text(str(action.get("text") or ""))
            if action_type == "reply":
                self._open_reply_editor(str(action.get("parent_comment_id") or action.get("target_id") or ""))
            elif action_type != "comment":
                raise BrowserPlatformError(f"unsupported_action_type:{action_type}")
            self._fill_editor(text)
            self._click_send()
            self._settle()
            self._ensure_no_challenge()
            if not self._text_visible(text):
                raise BrowserPlatformError("write_not_confirmed_in_page")
            self._action_times.append(monotonic())
            return {
                "platform": self.platform,
                "action_type": action_type,
                "target_id": str(action.get("target_id") or ""),
                "confirmed_in_page": True,
            }

    # -- Playwright lifecycle -------------------------------------------------

    def _ensure_browser(self) -> None:
        if self._context and self._page:
            return
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - depends on deployment
            raise BrowserPlatformError("playwright_not_installed") from exc
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            options: dict[str, object] = {
                "user_data_dir": str(self.data_dir),
                "headless": self.headless,
                "viewport": {"width": 1440, "height": 1000},
            }
            browser_executable = self.executable_path or _find_browser_executable()
            if browser_executable:
                options["executable_path"] = browser_executable
            self._context = self._playwright.chromium.launch_persistent_context(**options)
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self._page.set_default_timeout(self.page_timeout_ms)
        except Exception:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
            self._context = None
            self._page = None
            raise

    def _ensure_ready(self) -> None:
        self._ensure_browser()
        if not self._page.url or self._page.url == "about:blank":
            self._page.goto(self.profile.home_url, wait_until="domcontentloaded")
        text = self._body_text()
        self._ensure_no_challenge(text)
        if self._looks_logged_out(self._page.url, text):
            raise PlatformLoginRequired(f"{self.platform}_manual_login_required")

    def _goto(self, target: str) -> None:
        self._page.goto(target, wait_until="domcontentloaded")
        self._settle()
        text = self._body_text()
        self._ensure_no_challenge(text)
        if self._looks_logged_out(self._page.url, text):
            raise PlatformLoginRequired(f"{self.platform}_manual_login_required")

    def _settle(self) -> None:
        # A short bounded wait lets lazy-loaded cards/comments settle without
        # turning a page change into a long blocking sleep.
        try:
            self._page.wait_for_load_state("networkidle", timeout=min(self.page_timeout_ms, 5_000))
        except Exception:
            pass
        sleep(0.35)

    def _scroll_once(self) -> None:
        self._page.mouse.wheel(0, 900)
        sleep(0.8)

    # -- Read extraction ------------------------------------------------------

    def _extract_items(self) -> list[BrowseItem]:
        out: list[BrowseItem] = []
        seen: set[str] = set()
        links = self._page.locator("a[href]")
        try:
            count = min(links.count(), 500)
        except Exception:
            return out
        for index in range(count):
            link = links.nth(index)
            try:
                href = str(link.get_attribute("href") or "").strip()
            except Exception:
                continue
            url = urljoin(self._page.url, href)
            match = next((p.search(urlparse(url).path) for p in self.profile.item_patterns), None)
            if not match:
                continue
            identifier = match.group(0).rstrip("/").rsplit("/", 1)[-1]
            kind = "video" if "/video/" in match.group(0) else "note"
            item_id = f"{self.platform}:{kind}:{identifier}"
            if item_id in seen:
                continue
            seen.add(item_id)
            try:
                title = " ".join((link.inner_text(timeout=500) or "").split())[:300]
            except Exception:
                title = ""
            if not title:
                try:
                    title = str(link.get_attribute("aria-label") or link.get_attribute("title") or "").strip()[:300]
                except Exception:
                    title = ""
            out.append(
                BrowseItem(
                    platform=self.platform,
                    item_id=item_id,
                    url=url,
                    title=title,
                    text=title,
                    metadata={"source_url": self._page.url},
                )
            )
        return out

    def _extract_comments(self, item: BrowseItem) -> list[PlatformComment]:
        out: list[PlatformComment] = []
        seen: set[str] = set()
        for selector in self.profile.comment_selectors:
            nodes = self._page.locator(selector)
            try:
                count = min(nodes.count(), 300)
            except Exception:
                continue
            for index in range(count):
                node = nodes.nth(index)
                comment_id = self._stable_dom_id(node)
                if not comment_id or comment_id in seen:
                    # Never synthesize a target ID from visible names/text.
                    continue
                try:
                    text = " ".join((node.inner_text(timeout=500) or "").split())[:2_000]
                except Exception:
                    continue
                if not text:
                    continue
                author_id = self._first_attr(node, "data-user-id", "data-uid", "data-author-id")
                author_name = self._first_attr(node, "data-author-name", "data-nickname") or ""
                if not author_name:
                    try:
                        author_name = " ".join((node.locator("[class*='name'],[class*='Name'],a").first.inner_text(timeout=300) or "").split())[:120]
                    except Exception:
                        author_name = ""
                parent_id = self._first_attr(node, "data-parent-id", "data-root-id")
                comment = PlatformComment(
                    platform=self.platform,
                    comment_id=comment_id,
                    item_id=item.item_id,
                    author_id=author_id,
                    author_name=author_name,
                    text=text,
                    parent_comment_id=parent_id,
                    metadata={"source_url": item.url},
                )
                seen.add(comment_id)
                out.append(comment)
        return out

    @staticmethod
    def _stable_dom_id(node: Any) -> str | None:
        for attr in ("data-comment-id", "data-id", "data-rpid", "id"):
            try:
                value = str(node.get_attribute(attr) or "").strip()
            except Exception:
                value = ""
            if value:
                return value
        return None

    @staticmethod
    def _first_attr(node: Any, *attrs: str) -> str | None:
        for attr in attrs:
            try:
                value = str(node.get_attribute(attr) or "").strip()
            except Exception:
                value = ""
            if value:
                return value
        return None

    # -- Guarded writes -------------------------------------------------------

    def _check_write_budget(self) -> None:
        now = monotonic()
        hour_ago = now - 3_600
        self._action_times = [value for value in self._action_times if value >= hour_ago]
        if len(self._action_times) >= self.max_actions_per_hour:
            raise PlatformWriteBlocked("platform_hourly_action_limit")
        if self._action_times and now - self._action_times[-1] < self.action_delay_seconds:
            raise PlatformWriteBlocked("platform_action_rate_limit")

    def _open_reply_editor(self, comment_id: str) -> None:
        if not comment_id:
            raise BrowserPlatformError("reply_comment_id_missing")
        target = None
        for selector in self.profile.comment_selectors:
            nodes = self._page.locator(selector)
            try:
                count = min(nodes.count(), 300)
            except Exception:
                continue
            for index in range(count):
                node = nodes.nth(index)
                if self._stable_dom_id(node) == comment_id:
                    target = node
                    break
            if target:
                break
        if target is None:
            raise BrowserPlatformError("reply_comment_not_visible")
        for marker in self.profile.reply_markers:
            try:
                button = target.get_by_text(marker, exact=False).first
                if button.is_visible():
                    button.click()
                    self._settle()
                    return
            except Exception:
                continue
        raise BrowserPlatformError("reply_button_not_found")

    def _fill_editor(self, text: str) -> None:
        for selector in self.profile.editor_selectors:
            editors = self._page.locator(selector)
            try:
                count = min(editors.count(), 20)
            except Exception:
                continue
            for index in range(count):
                editor = editors.nth(index)
                try:
                    if not editor.is_visible():
                        continue
                    editor.fill(text)
                    return
                except Exception:
                    continue
        raise BrowserPlatformError("comment_editor_not_found")

    def _click_send(self) -> None:
        for marker in self.profile.send_markers:
            try:
                button = self._page.get_by_role("button", name=re.compile(re.escape(marker), re.I)).first
                if button.is_visible():
                    button.click()
                    return
            except Exception:
                continue
        for marker in self.profile.send_markers:
            try:
                button = self._page.get_by_text(marker, exact=False).last
                if button.is_visible():
                    button.click()
                    return
            except Exception:
                continue
        raise BrowserPlatformError("comment_send_button_not_found")

    def _text_visible(self, text: str) -> bool:
        needle = " ".join(text.split())
        # Prefer a comment node so the editor itself cannot count as proof.
        for selector in self.profile.comment_selectors:
            nodes = self._page.locator(selector)
            try:
                count = min(nodes.count(), 300)
            except Exception:
                continue
            for index in range(count):
                try:
                    visible = nodes.nth(index).is_visible()
                    body = " ".join((nodes.nth(index).inner_text(timeout=300) or "").split())
                except Exception:
                    continue
                if visible and needle in body:
                    return True
        # Some pages render the newly posted comment in a generic list.  A
        # body match is accepted only after confirming the editor no longer
        # contains the submitted text.
        for selector in self.profile.editor_selectors:
            editors = self._page.locator(selector)
            try:
                count = min(editors.count(), 20)
            except Exception:
                continue
            for index in range(count):
                editor = editors.nth(index)
                try:
                    if not editor.is_visible():
                        continue
                    value = editor.input_value(timeout=300) if selector == "textarea" or selector.startswith("input") else editor.inner_text(timeout=300)
                    if needle == " ".join(str(value or "").split()):
                        return False
                except Exception:
                    continue
        return needle in " ".join(self._body_text().split())

    def _allowed_target_url(self, target_url: str) -> bool:
        host = (urlparse(target_url).hostname or "").casefold().rstrip(".")
        allowed = {
            "douyin": ("douyin.com",),
            "xiaohongshu": ("xiaohongshu.com", "xhslink.com"),
        }[self.platform]
        return any(host == suffix or host.endswith("." + suffix) for suffix in allowed)

    # -- Page guards ----------------------------------------------------------

    def _body_text(self) -> str:
        try:
            return str(self._page.locator("body").inner_text(timeout=1_500) or "")[:30_000]
        except Exception:
            return ""

    def _ensure_no_challenge(self, text: str | None = None) -> None:
        body = text if text is not None else self._body_text()
        reason = self._challenge_reason(body)
        if reason:
            raise PlatformChallengeDetected(f"{self.platform}_challenge_detected:{reason}")

    def _challenge_reason(self, text: str) -> str | None:
        lowered = text.casefold()
        for marker in self.profile.challenge_markers:
            if marker.casefold() in lowered:
                return marker
        return None

    def _looks_logged_out(self, url: str, text: str) -> bool:
        lowered_url = url.casefold()
        if any(token in lowered_url for token in ("login", "passport", "signin")):
            return True
        lowered = text.casefold()
        # A login marker alone is not enough because a page may keep a small
        # login link visible.  Require two markers or a login URL.
        hits = sum(1 for marker in self.profile.login_markers if marker.casefold() in lowered)
        return hits >= 2


def _validate_text(text: str) -> str:
    value = " ".join(str(text).split())
    if not value:
        raise ValueError("action_text_empty")
    if len(value) > 2_000:
        raise ValueError("action_text_too_long")
    return value


def _find_browser_executable() -> str | None:
    """Prefer an installed user browser when Playwright's bundle is absent."""
    candidates = [
        shutil.which("chrome"),
        shutil.which("msedge"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None
