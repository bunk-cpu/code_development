"""Check the offline handbook with Playwright: uv run --with playwright python scripts/check_project_docs.py."""

import json
from pathlib import Path
import re

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data/validation/project-html"


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    results, errors, requests = [], [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-sandbox"])
        context = browser.new_context(offline=True, viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: requests.append(request.url) if request.url.startswith(("http:", "https:")) else None)
        page.goto((ROOT / "docs/项目完整说明.html").as_uri())
        page.evaluate("document.fonts.ready")
        charts = page.locator(".diagram")
        sources = [ROOT / "README.md", *(ROOT / "docs").rglob("*.md")]
        expected = sum(len(re.findall(r"^```mermaid\s*$", path.read_text(), re.M)) for path in sources)
        assert charts.count() == expected, "Missing Mermaid diagram"
        for index in range(expected):
            chart = charts.nth(index)
            svg = chart.locator("svg")
            source = chart.locator("code.language-mermaid").text_content()
            edges = len(re.findall(r"-->|-\.[^\n]*?\.->", source))
            assert svg.locator(".flowchart-link").count() == edges, f"Changed edges in diagram {index + 1}"
            assert not chart.locator("details").get_attribute("open"), "Source must start collapsed"
            assert svg.evaluate("el => parseFloat(getComputedStyle(el.querySelector('text')).fontSize)") >= 18
            overflow = svg.evaluate("""el => [...el.querySelectorAll('.node')].filter(node => {
                const label = node.querySelector('.label'), shape = node.querySelector('.label-container');
                if (!label || !shape) return false;
                const a = label.getBoundingClientRect(), b = shape.getBoundingClientRect();
                return a.left < b.left - 2 || a.right > b.right + 2 || a.top < b.top - 2 || a.bottom > b.bottom + 2;
            }).map(node => node.id)""")
            assert not overflow, f"Labels overflow their nodes: {overflow}"
            for marker in svg.locator("[marker-end]").evaluate_all("els => els.map(el => el.getAttribute('marker-end'))"):
                marker_id = re.search(r"#([^)'\"]+)", marker)[1]
                assert page.locator(f'[id="{marker_id}"]').count() == 1, "Broken arrowhead"
            width = svg.bounding_box()["width"]
            chart.locator('[data-diagram-action="in"]').click()
            assert svg.bounding_box()["width"] > width, "Zoom did not enlarge SVG"
            chart.locator('[data-diagram-action="reset"]').click()
            assert abs(svg.bounding_box()["width"] - width) < 1
            chart.locator('[data-diagram-action="fit"]').click()
            assert svg.bounding_box()["width"] <= chart.locator(".diagram-viewport").bounding_box()["width"]
            chart.locator('[data-diagram-action="reset"]').click()
            results.append({"diagram": index + 1, "edges": edges, "width": width})
        charts.first.locator('[data-diagram-action="fullscreen"]').click()
        page.wait_for_function("Boolean(document.fullscreenElement)")
        assert page.evaluate("Boolean(document.fullscreenElement)"), "Fullscreen did not open"
        charts.first.locator('[data-diagram-action="fullscreen"]').click()
        page.wait_for_function("!document.fullscreenElement")
        page.add_style_tag(content=".topbar{visibility:hidden}")
        for index in (0, 6, 8):
            charts.nth(index).screenshot(path=OUTPUT / f"mermaid-{index + 1}-desktop.png")
        for width in (390, 320):
            page.set_viewport_size({"width": width, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Page overflows on mobile"
            charts.nth(8).screenshot(path=OUTPUT / f"mermaid-9-mobile-{width}.png")
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.emulate_media(media="print")
        assert charts.first.locator("svg").bounding_box()["width"] <= charts.first.bounding_box()["width"]
        assert not charts.first.locator(".diagram-actions").is_visible()
        context.close()
        context = browser.new_context(java_script_enabled=False, offline=True)
        page = context.new_page()
        page.goto((ROOT / "docs/项目完整说明.html").as_uri())
        assert page.locator(".diagram svg").count() == expected, "Diagrams require JavaScript"
        assert not errors and not requests, {"errors": errors, "external_requests": requests}
        browser.close()
    report = {"diagrams": results, "mobile_widths": [390, 320], "offline": True,
              "without_javascript": True, "zoom": True, "fullscreen": True, "print": True}
    (OUTPUT / "mermaid-checks.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"All {expected} diagrams passed offline, edge, zoom, fullscreen, mobile, print, and no-JavaScript checks.")


if __name__ == "__main__":
    main()
