"""Uses the existing TestPilot Playwright installation; checks both Vue builds in Chromium."""
from pathlib import Path
import asyncio
import sys

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        console_errors = []
        for width in (1280, 390):
            context = await browser.new_context(viewport={"width": width, "height": 900})
            page = await context.new_page()
            page.on("pageerror", lambda error: console_errors.append(str(error)))
            await page.goto("http://127.0.0.1:8000/internal-ui")
            await page.get_by_role("button", name="登录", exact=True).click()
            await page.get_by_role("button", name="源码与功能", exact=True).wait_for()
            await page.get_by_role("button", name="查询源码", exact=True).click()
            await page.locator("article p").filter(has_text="以下固定快照中的源码行").wait_for()
            for tab, title in [("手册审核", "版本手册与导入"), ("测试工作室", "用例与来源"), ("TestPilot", "TestPilot 连接与用例对账"), ("问题运营", "问题记录与流程候选"), ("流程与运行", "流程工厂")]:
                await page.get_by_role("button", name=tab, exact=True).click()
                await page.get_by_role("heading", name=title, exact=True).wait_for()
            screenshots = ROOT / "data/validation"
            screenshots.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(screenshots / f"internal-{width}.png"), full_page=True)
            await page.get_by_role("button", name="退出登录").click()
            await page.goto("http://127.0.0.1:8000/customer-ui")
            await page.get_by_role("button", name="登录", exact=True).click()
            await page.get_by_role("button", name="产品帮助", exact=True).wait_for()
            await page.get_by_role("button", name="问答助手", exact=True).click()
            await page.get_by_role("button", name="提问", exact=True).click()
            await page.get_by_role("button", name="有帮助", exact=True).wait_for()
            await page.get_by_role("button", name="有帮助", exact=True).click()
            await page.get_by_text("反馈已收到，支持人员会核查。").wait_for()
            await page.get_by_role("button", name="常用流程", exact=True).click()
            await page.get_by_role("button", name="预览操作", exact=True).click()
            await page.get_by_role("button", name="确认查询", exact=True).click()
            await page.get_by_text("查询已提交", exact=False).wait_for()
            await page.get_by_role("button", name="我的任务", exact=True).click()
            await page.get_by_role("button", name="查看结果", exact=True).first.click()
            await page.get_by_text("符合条件的订单共", exact=False).wait_for(timeout=20000)
            assert await page.locator("body").evaluate("element => element.scrollWidth <= window.innerWidth")
            await page.screenshot(path=str(screenshots / f"customer-{width}.png"), full_page=True)
            await context.close()
        await browser.close()
        assert not console_errors, console_errors
        print("PASS: internal/customer × desktop/mobile; source QA, navigation, feedback, workflow, task result; no page errors")


if __name__ == "__main__":
    asyncio.run(main())
