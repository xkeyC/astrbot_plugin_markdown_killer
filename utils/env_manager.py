"""Playwright/Chromium environment manager.

Auto-installs Chromium on first run via ``python -m playwright install chromium``
and tracks completion via a ``.playwright_installed`` flag file in the plugin
data directory.
"""

import asyncio
import os
import sys

from astrbot.api import logger


class EnvManager:
    """Manages Playwright/Chromium installation for the plugin."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.flag_file = os.path.join(data_dir, ".playwright_installed")

    async def verify_playwright(self) -> bool:
        """Verify that Chromium can be launched and closed. Returns True on success."""
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                )
                await browser.close()
            return True
        except Exception as e:
            logger.debug(f"Playwright 环境验证失败: {e}")
            return False

    async def install_dependencies(self) -> None:
        """Run ``python -m playwright install chromium`` and write the flag file on success.

        A 5-minute (300s) timeout guards against stalled network installs (N5):
        on timeout we kill the subprocess and return without writing the flag
        file, so the next run will retry.
        """
        logger.info("正在初始化插件依赖 (Playwright)...")
        try:
            logger.info("正在安装 Playwright Chromium...")
            process = await asyncio.create_subprocess_shell(
                f"{sys.executable} -m playwright install chromium",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                msg = line.decode(errors="ignore").strip()
                if msg:
                    logger.info(f"[Playwright] {msg}")

            try:
                await asyncio.wait_for(process.wait(), timeout=300)
            except asyncio.TimeoutError:
                logger.error(
                    "Playwright Chromium 安装超时 (300s)，请检查网络或手动安装"
                )
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                except Exception:
                    pass
                try:
                    await process.wait()
                except Exception:
                    pass
                return

            if process.returncode == 0:
                if await self.verify_playwright():
                    logger.info("Playwright Chromium 安装并验证成功")
                    os.makedirs(os.path.dirname(self.flag_file) or ".", exist_ok=True)
                    with open(self.flag_file, "w", encoding="utf-8") as f:
                        f.write("installed")
                else:
                    logger.warning(
                        "Playwright 安装后验证依然失败，请检查网络或手动安装依赖。"
                    )
            else:
                logger.warning(
                    f"Playwright Chromium 安装返回错误码: {process.returncode}"
                )
        except Exception as e:
            logger.error(f"依赖安装流程失败: {e}")

    def is_installed(self) -> bool:
        """Return True if the install flag file exists."""
        return os.path.exists(self.flag_file)
