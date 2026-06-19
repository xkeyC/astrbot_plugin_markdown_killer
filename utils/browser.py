"""Singleton Playwright browser management for table rendering.

The module is import-safe even when ``playwright`` is not installed: the
``playwright`` imports are deferred to call time so that the plugin can still
load and fall back to plain-text table output.
"""

from __future__ import annotations

import asyncio

from astrbot.api import logger

# Singleton instances; lazily initialized by ``get_browser``.
_playwright_instance = None
_browser_instance = None

# Lock guarding singleton initialization to prevent the race where two
# concurrent coroutines both see ``_browser_instance is None`` and each try to
# launch its own browser (N2).
_browser_lock = asyncio.Lock()


async def get_browser():
    """Return a connected singleton browser, creating one if necessary.

    Returns ``None`` if Playwright is unavailable or browser launch fails.

    Initialization is guarded by ``_browser_lock`` to avoid duplicate launches
    under concurrent first-time callers. The fast path (already-connected
    browser) skips the lock entirely.
    """
    global _playwright_instance, _browser_instance

    # Fast path: a connected browser is already cached.
    if _browser_instance and _playwright_instance:
        try:
            if _browser_instance.is_connected():
                return _browser_instance
        except Exception:
            pass

    async with _browser_lock:
        # Re-check under lock — another coroutine may have just initialized it
        # while we were waiting to acquire the lock.
        if _browser_instance and _playwright_instance:
            try:
                if _browser_instance.is_connected():
                    return _browser_instance
            except Exception:
                pass

        try:
            from playwright.async_api import async_playwright

            _playwright_instance = await async_playwright().start()

            chrome_args = [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
                "--disable-extensions",
                "--disable-default-apps",
            ]

            _browser_instance = await _playwright_instance.chromium.launch(
                headless=True,
                args=chrome_args,
            )
            return _browser_instance
        except Exception as e:
            logger.error(f"初始化浏览器失败: {e}")
            return None


async def close_browser():
    """Shut down the singleton browser and Playwright driver (best-effort)."""
    global _playwright_instance, _browser_instance

    if _browser_instance:
        try:
            await _browser_instance.close()
        except Exception:
            pass
        _browser_instance = None

    if _playwright_instance:
        try:
            await _playwright_instance.stop()
        except Exception:
            pass
        _playwright_instance = None


async def render_html_to_image(
    html_content: str,
    selector: str = "body",
    width: int = 1400,
    scale_factor: int = 2,
    timeout: int = 30000,
) -> bytes | None:
    """Render ``html_content`` and screenshot the element matched by ``selector``.

    Returns PNG bytes on success, or ``None`` on any failure.
    The browser context and page are always closed in the ``finally`` block.
    """
    browser = await get_browser()
    if not browser:
        return None

    context = None
    page = None
    try:
        from playwright.async_api import ViewportSize

        context = await browser.new_context(
            viewport=ViewportSize(width=width, height=10000),
            device_scale_factor=scale_factor,
        )
        page = await context.new_page()

        await page.set_content(html_content, wait_until="networkidle", timeout=timeout)

        locator = page.locator(selector)
        if await locator.count() > 0:
            screenshot_bytes = await locator.screenshot(
                type="png",
                omit_background=False,
                animations="disabled",
            )
        else:
            screenshot_bytes = await page.screenshot(
                full_page=True,
                type="png",
                animations="disabled",
            )

        return screenshot_bytes
    except Exception as e:
        logger.error(f"渲染 HTML 失败: {e}")
        return None
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass
        if context:
            try:
                await context.close()
            except Exception:
                pass
