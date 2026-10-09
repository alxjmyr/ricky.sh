"""One real Chrome boundary for the packaged offline workflow viewer."""

from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect

from ricky.browser.chrome import require_chrome
from ricky.config import RickySettings
from ricky.tools.registry import ToolRegistry
from ricky.workflows.inspection import inspect_workflow
from ricky.workflows.visualization import export_visualization
from workflow_visualization_support import GUIDANCE, INSTRUCTION, write_design


@pytest.mark.browser_integration
async def test_blueprint_chrome_interactions_and_mobile_layout(bundled_root: Path, tmp_path: Path):
    bundle = write_design(bundled_root)
    spec = bundle / "workflow.toml"
    spec.write_text(
        spec.read_text()
        + """
[[steps]]
id = "echo-emails"
kind = "foreach"
collection = { ref = "trigger.emails" }
item_key = { ref = "item.source" }
outputs = { echo = { ref = "item.steps.echo.output" } }
[[steps.body]]
id = "echo"
kind = "message"
message = { ref = "item.source" }
"""
    )
    settings = RickySettings()
    scope = settings.resolve_profile_scope()
    view = inspect_workflow("email-triage", settings=settings, scope=scope, tools=ToolRegistry([]))
    assert view.steps[1].instruction is not None
    view.steps[1].instruction += "\n</script><script>window.injected=true</script>"
    path = export_visualization(view, settings=settings, scope=scope)
    executable = await require_chrome(settings)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=str(executable), headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            requests = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.append(request.url))
            await page.goto(path.as_uri())
            await expect(page.locator("h1")).to_have_text("bundled/email-triage")
            await expect(page.locator("#graph .node")).to_have_count(7)
            await page.get_by_role("button", name="Expand classify-emails body", exact=True).click()
            await expect(page.locator("#graph .node")).to_have_count(8)
            collapse = page.get_by_role("button", name="Collapse classify-emails body", exact=True)
            await expect(collapse).to_be_focused()
            await expect(collapse).to_have_attribute("aria-expanded", "true")
            await collapse.press("Enter")
            await expect(page.locator("#graph .node")).to_have_count(7)
            await page.get_by_role("button", name="Expand classify-emails body", exact=True).press(
                "Space"
            )
            await expect(page.locator("#graph .node")).to_have_count(8)
            await page.get_by_role("button", name="Collapse body", exact=True).click()
            await page.get_by_label("Prompts / instructions", exact=True).check()
            await expect(page.locator("#inspector")).to_contain_text("4 matching steps")
            await expect(page.locator("#graph .node:not(.dim)")).to_have_count(4)
            await expect(page.locator('.node[data-step="classify"]:not(.dim)')).to_be_attached()
            await expect(page.locator('.node[data-step="review"]:not(.dim)')).to_be_attached()
            await expect(page.locator('.node[data-step="report"].dim')).to_be_attached()
            await page.get_by_role("searchbox").fill("billing")
            await expect(page.locator("#inspector")).to_contain_text("1 matching steps")
            await page.get_by_role("button", name="classify · agent", exact=True).click()
            await expect(page.locator("#inspector")).to_contain_text(INSTRUCTION)
            await page.get_by_role("searchbox").fill("no-such-prompt")
            await expect(page.locator("#inspector")).to_contain_text("0 matching steps")
            await page.get_by_role("button", name="Clear selection").click()
            await expect(page.get_by_label("Prompts / instructions", exact=True)).to_be_checked()
            await expect(page.locator("#inspector")).to_contain_text("4 matching steps")
            await page.get_by_label("Prompts / instructions", exact=True).uncheck()
            await expect(page.locator("#graph .node.dim")).to_have_count(0)
            await page.get_by_role(
                "button", name="Collapse classify-emails body", exact=True
            ).click()
            await page.get_by_role("button", name="classify-emails, foreach", exact=True).click()
            await page.get_by_role("button", name="Expand body", exact=True).click()
            await expect(page.locator("#graph .node")).to_have_count(8)
            await page.get_by_role("button", name="classify, agent", exact=True).click()
            await expect(page.locator("#inspector")).to_contain_text(INSTRUCTION)
            await expect(page.locator("#inspector")).to_contain_text(GUIDANCE)
            assert await page.evaluate("typeof window.injected") == "undefined"
            await page.locator("#inspector .binding").filter(has_text="item.source").first.click()
            await expect(page.locator('.node[data-step="echo-emails"].dim')).to_be_attached()
            await page.get_by_role("button", name="Inspect classify", exact=True).click()
            node_bounds = await page.get_by_role(
                "button", name="classify, agent", exact=True
            ).bounding_box()
            assert node_bounds is not None
            assert 0 < node_bounds["y"] < 900
            assert node_bounds["height"] > 30
            await page.screenshot(path=str(tmp_path / "blueprint-light.png"), full_page=True)
            await page.get_by_role("button", name="Toggle light and dark mode").click()
            await expect(page.locator("html")).to_have_attribute("data-theme", "dark")
            await page.reload()
            await expect(page.locator("html")).to_have_attribute("data-theme", "dark")
            await page.get_by_role("searchbox").fill("billing")
            await expect(page.locator("#inspector")).to_contain_text("1 matching steps")
            await page.get_by_role("button", name="classify · agent", exact=True).click()
            await expect(page.locator("#inspector")).to_contain_text(INSTRUCTION)
            await page.get_by_role("button", name="Clear selection").click()
            await page.get_by_role("button", name="draft, model", exact=True).click()
            await (
                page.locator("#inspector .binding")
                .filter(has_text="steps.draft.output.body")
                .first.click()
            )
            await expect(page.locator("#inspector")).to_contain_text("Connection detail")
            await expect(page.locator("#graph .dim").first).to_be_attached()
            await page.get_by_label("Data", exact=True).uncheck()
            await expect(page.locator("#graph .edge.data")).to_have_count(0)
            await page.get_by_label("Data", exact=True).check()
            before = await page.locator("#graph").get_attribute("viewBox")
            await page.get_by_role("button", name="Zoom in", exact=True).click()
            assert await page.locator("#graph").get_attribute("viewBox") != before
            await page.get_by_role("button", name="Fit graph", exact=True).click()
            await page.screenshot(path=str(tmp_path / "blueprint-dark.png"), full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.get_by_role("button", name="Clear selection").click()
            await page.get_by_role("button", name="draft, model", exact=True).click()
            await expect(page.locator("#inspector")).to_contain_text("Write a digest")
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path=str(tmp_path / "blueprint-mobile.png"), full_page=True)
            assert not errors
            assert requests == [path.as_uri(), path.as_uri()]
        finally:
            await browser.close()
