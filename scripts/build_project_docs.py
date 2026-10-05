"""Build and check the offline project handbook: uv run python scripts/build_project_docs.py."""

from datetime import datetime
from hashlib import sha256
from html import escape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from markdown_it import MarkdownIt


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
OUTPUT = DOCS / "项目完整说明.html"
DESIGN = "源码驱动的智能手册_问答与流程自动化系统设计方案.md"
ORDER = [DESIGN, "验收报告.md", "源码分析Agent工作流.md", "data-java-fixture功能分析.md"]
LABELS = {
    "README.md": ("运行与使用说明", "当前项目说明"),
    DESIGN: ("原始系统设计方案", "V1.6 · 完整规划"),
    "验收报告.md": ("实施与验收报告", "历史验证与交付边界"),
    "源码分析Agent工作流.md": ("源码分析 Agent 工作流", "实现与审核边界"),
    "data-java-fixture功能分析.md": ("Java 样例功能分析", "工作目录源码分析"),
}

MERMAID_VERSION = "12.0.0"
MERMAID_CONFIG = {
    "theme": "base", "layout": "elk", "htmlLabels": False,
    "fontFamily": '"WenQuanYi Zen Hei", "Microsoft YaHei", "PingFang SC", sans-serif',
    "themeVariables": {
        "fontSize": "18px", "primaryColor": "#edf6f6", "primaryTextColor": "#192a40",
        "primaryBorderColor": "#4b8589", "lineColor": "#45687a",
        "secondaryColor": "#fff4df", "tertiaryColor": "#f4f7fa",
        "clusterBkg": "#f4f7fa", "clusterBorder": "#a4bac8", "edgeLabelBackground": "#ffffff",
    },
    "flowchart": {"useMaxWidth": False, "nodeSpacing": 40, "rankSpacing": 60,
                  "wrappingWidth": 280, "padding": 18, "diagramPadding": 24, "curve": "linear"},
}

CSS = r"""
:root{--ink:#192a40;--muted:#596b7d;--line:#dbe3e9;--paper:#fff;--bg:#f3f5f7;--accent:#076e70;--sidebar:292px;scroll-behavior:smooth;scroll-padding-top:86px}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.85 system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}a{color:var(--accent);text-underline-offset:4px}button,input{font:inherit}button{cursor:pointer}button:focus-visible,a:focus-visible,input:focus-visible,summary:focus-visible,[tabindex]:focus-visible{outline:3px solid #d79939;outline-offset:3px}button{border:1px solid var(--line);border-radius:8px;background:#fff;color:var(--ink);padding:6px 13px}button:hover{background:#edf4f4}.skip{position:fixed;left:16px;top:-80px;z-index:10;padding:10px;background:white}.skip:focus{top:12px}
.topbar{height:62px;position:sticky;top:0;z-index:4;background:#152a40;color:#fff;display:flex;align-items:center;justify-content:space-between;padding:0 26px;gap:16px}.brand{color:inherit;text-decoration:none;font-weight:750;letter-spacing:.03em;font-size:16px}.topbar small{color:#b9cad8;font-size:12px}.toolbar{display:flex;align-items:center;gap:12px}.topbar button{background:#223d55;border-color:#476076;color:#fff;font-size:13px}.menu-button{display:none}.search-box button{flex-shrink:0;white-space:nowrap}
.sidebar{position:fixed;top:62px;bottom:0;left:0;width:var(--sidebar);background:#fff;border-right:1px solid var(--line);padding:22px 18px;overflow:auto;z-index:3}.search-label{display:block;font-size:13px;font-weight:700;margin-bottom:6px}.search-box{display:flex;gap:7px;align-items:center}.search-box input{width:100%;min-width:0;border:1px solid #b8c7d1;border-radius:8px;padding:8px 10px;font-size:14px}.search-box button{padding:7px 9px;font-size:13px}.search-hint,.search-status{font-size:12px;color:var(--muted);margin:7px 0 16px}.toc-root{display:block;text-decoration:none;font-weight:750;font-size:14px;padding:8px 9px;border-radius:6px;background:#edf5f5;margin-bottom:15px}.nav-group{border-top:1px solid var(--line);padding:10px 0}.nav-group summary{cursor:pointer;font-weight:700;font-size:14px;padding:4px 1px}.nav-tag{font-size:11px;color:var(--muted);margin:1px 0 7px 16px}.toc-link{display:block;font-size:12px;line-height:1.65;color:var(--muted);padding:5px 8px 5px 16px;text-decoration:none;border-radius:5px}.toc-link:hover{background:#edf5f5;color:var(--accent)}.result-link{display:block;padding:12px 7px;border-bottom:1px solid var(--line);text-decoration:none;line-height:1.6}.result-link strong{display:block;font-size:13px}.result-link small{display:block;color:var(--muted);font-size:11px;margin:4px 0}.result-link p{font-size:12px;color:var(--ink);margin:0}.result-link mark{background:#fff0bf;color:inherit}
main{margin-left:var(--sidebar);padding:34px clamp(22px,4vw,62px) 70px;max-width:1600px}.hero{background:linear-gradient(125deg,#152a40,#1c465a);color:#fff;border-radius:18px;padding:36px clamp(22px,4vw,46px);margin-bottom:25px}.eyebrow{font-size:12px;font-weight:700;letter-spacing:.13em;color:#a6d6d4}.hero h1{font-size:clamp(26px,3vw,39px);line-height:1.4;margin:10px 0 17px;letter-spacing:-.02em}.hero p{color:#dae8ed;max-width:850px;margin:10px 0}.hero .meta{font-size:12px;color:#aec7d3;border-top:1px solid #4a6273;padding-top:16px;margin-top:22px}.stats{display:flex;flex-wrap:wrap;gap:10px 26px;margin-top:22px}.stat strong{font-size:27px;margin-right:8px;font-variant-numeric:tabular-nums}.stat span{font-size:12px;color:#bed4df}.panel,.source-document{background:var(--paper);border:1px solid var(--line);border-radius:14px;margin:22px 0;padding:28px clamp(18px,3vw,36px);min-width:0}.panel h2{font-size:23px;margin:0 0 15px}.panel p{margin:10px 0}.note{border-left:4px solid #b88436;background:#fff9ec;border-radius:0 8px 8px 0;padding:12px 17px;color:#654e29;font-size:14px}.pipeline{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:11px;margin:22px 0;list-style:none;padding:0;counter-reset:step}.pipeline li{background:#eef5f6;border:1px solid #d8e8e9;border-radius:9px;padding:14px 13px;counter-increment:step;font-size:13px;line-height:1.65}.pipeline li:before{content:counter(step,decimal-leading-zero);display:block;color:var(--accent);font-weight:750;font-size:12px;margin-bottom:6px}.pipeline strong{display:block;margin-bottom:6px}.pipeline span{color:var(--muted)}.reading-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.reading-card{border:1px solid var(--line);border-radius:9px;padding:15px 17px;text-decoration:none}.reading-card strong{display:block;font-size:16px}.reading-card small{display:block;color:var(--muted);font-size:12px;margin-top:5px}.reading-card:hover{background:#f4f9f9}
.document-header{border-bottom:1px solid var(--line);padding-bottom:18px;margin-bottom:24px}.document-label{font-size:12px;text-transform:uppercase;font-weight:700;color:var(--accent);letter-spacing:.05em}.document-header h2{font-size:27px;line-height:1.5;margin:7px 0}.document-meta{font-size:12px;color:var(--muted);overflow-wrap:anywhere}.document-meta a{color:inherit}.chapter{min-width:0}.chapter+.chapter{margin-top:36px;padding-top:6px;border-top:1px solid var(--line)}.source-document h1{font-size:25px;line-height:1.55;margin:14px 0}.source-document h2{font-size:23px;line-height:1.55;margin:26px 0 16px}.source-document h3{font-size:19px;margin:24px 0 13px;line-height:1.6}.source-document h4{font-size:17px;margin:22px 0 12px}.source-document h5,.source-document h6{font-size:16px;margin:19px 0 10px}.source-document :is(h1,h2,h3,h4,h5,h6){scroll-margin-top:80px;overflow-wrap:anywhere}.source-document p{margin:13px 0;overflow-wrap:anywhere}.source-document li{margin:4px 0;overflow-wrap:anywhere}.source-document ul,.source-document ol{padding-left:25px}.source-document blockquote{margin:18px 0;padding:10px 18px;background:#f2f7f8;border-left:4px solid #86acb4;border-radius:0 8px 8px 0;font-size:14px;color:#435c6f}.source-document blockquote p{margin:7px 0}.source-document hr{border:0;border-top:1px solid var(--line);margin:28px 0}code{font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;font-size:.86em;background:#edf1f5;border-radius:4px;padding:2px 5px;overflow-wrap:anywhere}.code-block{margin:20px 0;border-radius:9px;background:#172638;color:#e4edf3;overflow:hidden;min-width:0}.code-header{background:#20354a;display:flex;justify-content:space-between;align-items:center;padding:7px 13px;gap:12px;font-size:11px;color:#c2d5e1}.code-header button{padding:2px 9px;background:transparent;border-color:#6c8496;color:#dfebf0;font-size:11px}.code-block pre{margin:0;padding:16px 18px;overflow:auto;line-height:1.65;font-size:13px;tab-size:4}.code-block code{background:none;padding:0;color:inherit;font-size:inherit;border-radius:0;overflow-wrap:normal;white-space:pre}pre{max-width:100%;overflow:auto}.table-scroll{max-width:100%;overflow:auto;margin:20px 0;border:1px solid var(--line);border-radius:9px}.table-scroll table{margin:0;border:0;width:100%;border-collapse:collapse;font-size:13px;line-height:1.75}.table-scroll th,.table-scroll td{text-align:left;padding:11px 14px;border-bottom:1px solid var(--line);vertical-align:top;min-width:110px;overflow-wrap:anywhere}.table-scroll th{background:#edf4f6;font-weight:750;color:#25475e}.table-scroll tr:last-child td{border-bottom:0}.table-scroll tbody tr:nth-child(even){background:#fafcfd}.table-scroll td code{font-size:11px;white-space:normal}.source-document img{max-width:100%;height:auto}.source-document a{overflow-wrap:anywhere}.footer{font-size:12px;color:var(--muted);margin:30px 0;text-align:center}.back-top{display:inline-block;margin-top:20px;font-size:12px}.source-list summary{cursor:pointer;font-size:14px;font-weight:700}.source-list code{font-size:11px}.source-list table td:first-child{min-width:200px}.source-list .hash{font-size:11px;word-break:break-all}.noscript{margin:0;background:#fff3d9;padding:12px 24px;font-size:14px} [hidden]{display:none!important}
@media(min-width:1700px){main{margin-right:auto}}
@media(max-width:1100px){:root{--sidebar:250px}.sidebar{padding:18px 13px}main{padding:24px 22px 55px}.pipeline{grid-template-columns:repeat(3,minmax(0,1fr))}.topbar small{display:none}}
@media(max-width:760px){:root{scroll-padding-top:78px}.topbar{padding:0 15px;gap:9px}.brand{font-size:13px;line-height:1.45;max-width:190px}.toolbar{gap:7px}.menu-button{display:block}.sidebar{width:min(340px,90vw);transform:translateX(-100%);box-shadow:10px 0 35px #12293b22;transition:transform .15s ease}.sidebar.is-open{transform:translateX(0)}main{margin-left:0;padding:18px 12px 45px}.hero{padding:26px 22px;border-radius:12px}.hero h1{font-size:clamp(20px,6.3vw,26px)}.hero p{font-size:14px}.panel,.source-document{padding:23px 17px;border-radius:10px}.pipeline{grid-template-columns:repeat(2,minmax(0,1fr))}.reading-list{grid-template-columns:1fr}.stats{gap:7px 22px}.stat strong{font-size:23px}.source-document h1,.document-header h2{font-size:23px}.source-document h2,.panel h2{font-size:21px}.source-document{font-size:15px}.source-document blockquote{padding:9px 13px}.code-block pre{padding:14px;font-size:12px}.table-scroll th,.table-scroll td{padding:9px 11px}.no-js .sidebar{position:static;transform:none;width:auto;max-height:420px}.no-js main{margin-left:0}}
@media(prefers-reduced-motion:reduce){:root{scroll-behavior:auto}.sidebar{transition:none}}
@media print{@page{margin:18mm}body{background:#fff;font-size:10pt;line-height:1.65}.topbar,.sidebar,.toolbar,.back-top,.code-header button,.skip,.noscript{display:none!important}main{margin:0;padding:0;max-width:none}.hero{background:#fff;color:#162b40;padding:0;border-radius:0}.hero p,.hero .meta,.eyebrow,.stat span{color:#43586b}.hero .meta{border-color:#bbb}.panel,.source-document{border:0;border-radius:0;padding:0;margin:24px 0}.source-document{break-before:page;font-size:10pt}.source-document h1,.source-document h2,.source-document h3{break-after:avoid}.table-scroll{overflow:visible;border-radius:0}.table-scroll th,.table-scroll td{min-width:0;padding:5px 7px;font-size:8pt}.table-scroll tr{break-inside:avoid}.code-block,.code-header{background:#f2f4f6;color:#172638;border:1px solid #ccc}.code-block pre{white-space:pre-wrap;font-size:8pt}.code-block code{white-space:pre-wrap;overflow-wrap:anywhere}a{color:inherit;text-decoration:underline}.reading-list,.pipeline{display:block}.reading-card{display:block;margin:9px 0}.pipeline li{display:inline-block;width:18%;vertical-align:top;padding:8px;margin:3px}.source-list{display:block}.footer{text-align:left}}
"""

CSS += r"""
.diagram{margin:24px 0;border:1px solid #bdcfd7;border-radius:12px;overflow:hidden;background:#fff;min-width:0}.diagram-toolbar{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;padding:12px 16px;background:#edf5f6;border-bottom:1px solid #d5e3e7}.diagram-toolbar strong{font-size:14px}.diagram-actions{display:flex;gap:6px;flex-wrap:wrap}.diagram-actions button{font-size:12px;padding:4px 10px}.diagram-hint{font-size:12px;color:var(--muted);padding:8px 16px;border-bottom:1px solid #edf1f4}.diagram-viewport{overflow:auto;padding:24px;max-width:100%;background:#fff}.diagram-viewport>svg{display:block;width:var(--diagram-width);max-width:none!important;height:auto;margin:0 auto}.diagram-source{border-top:1px solid var(--line);padding:10px 16px}.diagram-source summary{cursor:pointer;font-size:13px;color:var(--muted)}.diagram-source .code-block{margin:12px 0 0}.diagram:fullscreen{width:100vw;height:100vh;max-width:none;margin:0;border:0;border-radius:0;display:flex;flex-direction:column}.diagram:fullscreen .diagram-viewport{flex:1;min-height:0}.diagram:fullscreen .diagram-source{display:none}.no-js .diagram-actions{display:none}
@media(max-width:760px){.diagram-toolbar{padding:10px}.diagram-actions button{padding:4px 8px}.diagram-viewport{padding:16px}.diagram-hint,.diagram-source{padding:8px 10px}}
@media print{.diagram{break-inside:avoid;border:1px solid #bdcfd7}.diagram-actions,.diagram-hint,.diagram-source{display:none!important}.diagram-toolbar{padding:6px 10px}.diagram-viewport{padding:10px;overflow:visible}.diagram-viewport>svg{width:100%;max-height:230mm;object-fit:contain}}
.diagram .edgeLabel rect{opacity:1!important}
"""

JS = r"""
document.body.classList.remove('no-js');
const sidebar = document.querySelector('.sidebar');
const menu = document.querySelector('.menu-button');
const mobile = matchMedia('(max-width: 760px)');
const toggleMenu = open => {
  sidebar.classList.toggle('is-open', open); menu.setAttribute('aria-expanded', String(open));
  if (!open && mobile.matches && sidebar.contains(document.activeElement)) menu.focus({preventScroll: true});
  sidebar.inert = mobile.matches && !open;
};
mobile.addEventListener('change', () => toggleMenu(false));
toggleMenu(false);
menu.addEventListener('click', () => toggleMenu(!sidebar.classList.contains('is-open')));
sidebar.addEventListener('click', event => { if (event.target.closest('a')) toggleMenu(false); });
document.querySelector('[data-print]').addEventListener('click', () => window.print());
const search = document.querySelector('#doc-search');
const toc = document.querySelector('#toc');
const results = document.querySelector('#search-results');
const status = document.querySelector('#search-status');
const chapters = [...document.querySelectorAll('.chapter')].map(section => ({
  title: section.dataset.title, document: section.closest('article').dataset.title,
  anchor: section.dataset.anchor, text: section.textContent.replace(/\s+/gu, ' ').trim()
}));
chapters.forEach(chapter => chapter.lower = chapter.text.toLocaleLowerCase());
function runSearch() {
  const query = search.value.trim().toLocaleLowerCase();
  results.replaceChildren(); toc.hidden = Boolean(query); results.hidden = !query;
  if (!query) { status.textContent = '按章节搜索完整正文，也可使用浏览器 Ctrl / ⌘ + F。'; return; }
  const terms = query.split(/\s+/u);
  const found = chapters.filter(chapter => terms.every(term => chapter.lower.includes(term)));
  status.textContent = `找到 ${found.length} 个章节${found.length > 40 ? '，显示前 40 项' : ''}；正文保持完整。`;
  for (const chapter of found.slice(0, 40)) {
    const link = document.createElement('a'); link.href = '#' + chapter.anchor; link.className = 'result-link';
    const title = document.createElement('strong'); title.textContent = chapter.title;
    const label = document.createElement('small'); label.textContent = chapter.document;
    const snippet = document.createElement('p');
    const at = chapter.lower.indexOf(terms[0]), start = Math.max(0, at - 38), end = Math.min(chapter.text.length, at + 105);
    if (start) snippet.append('…');
    snippet.append(chapter.text.slice(start, at));
    const mark = document.createElement('mark'); mark.textContent = chapter.text.slice(at, at + terms[0].length); snippet.append(mark);
    snippet.append(chapter.text.slice(at + terms[0].length, end));
    if (end < chapter.text.length) snippet.append('…');
    link.append(title, label, snippet); results.append(link);
  }
}
search.addEventListener('input', runSearch);
document.querySelector('[data-clear]').addEventListener('click', () => { search.value = ''; runSearch(); search.focus(); });
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') toggleMenu(false);
  if (event.key === '/' && !/INPUT|TEXTAREA|SELECT/.test(event.target.tagName) && !event.target.isContentEditable) {
    event.preventDefault(); if (mobile.matches) toggleMenu(true); search.focus();
  }
});
document.querySelectorAll('[data-copy]').forEach(button => button.addEventListener('click', async () => {
  const code = button.closest('.code-block').querySelector('code').textContent;
  try { await navigator.clipboard.writeText(code); button.textContent = '已复制'; }
  catch { button.textContent = '请选中文本复制'; }
  setTimeout(() => { button.textContent = '复制'; }, 1800);
}));
document.querySelectorAll('.diagram').forEach(figure => {
  const viewport = figure.querySelector('.diagram-viewport');
  const svg = viewport.querySelector('svg');
  const width = svg.viewBox.baseVal.width;
  let scale = 1;
  figure.querySelectorAll('[data-diagram-action]').forEach(button => button.addEventListener('click', async () => {
    const action = button.dataset.diagramAction;
    if (action === 'fullscreen') {
      try {
        if (document.fullscreenElement === figure) await document.exitFullscreen();
        else await figure.requestFullscreen();
      } catch { button.textContent = '全屏不可用'; }
      return;
    }
    if (action === 'fit') scale = (viewport.clientWidth - parseFloat(getComputedStyle(viewport).paddingLeft) * 2) / width;
    else if (action === 'reset') scale = 1;
    else scale *= action === 'in' ? 1.2 : 1 / 1.2;
    scale = Math.max(.25, Math.min(3, scale));
    figure.style.setProperty('--diagram-width', `${width * scale}px`);
  }));
});
document.addEventListener('fullscreenchange', () => {
  document.querySelectorAll('[data-diagram-action="fullscreen"]').forEach(button => {
    button.textContent = document.fullscreenElement === button.closest('.diagram') ? '退出全屏' : '全屏查看';
  });
});
"""


def sources():
    paths = [ROOT / "README.md", *(DOCS / name for name in ORDER)]
    paths.extend(path for path in sorted(DOCS.rglob("*.md")) if path not in paths)
    documents = []
    for number, path in enumerate(paths, 1):
        raw = path.read_bytes()
        title, tag = LABELS.get(path.name, (path.stem, "补充文档"))
        documents.append({
            "path": path, "id": f"doc-{number}", "title": title, "tag": tag,
            "text": raw.decode("utf-8"), "bytes": len(raw), "hash": sha256(raw).hexdigest(),
            "relative": path.relative_to(ROOT).as_posix(),
        })
    return documents


def rewrite_link(href, source, documents):
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return href
    target = (source.parent / unquote(parsed.path)).resolve()
    for document in documents:
        if document["path"].resolve() == target and not parsed.fragment:
            return "#" + document["id"]
    relative = quote(os.path.relpath(target, DOCS).replace(os.sep, "/"), safe="/")
    return urlunsplit(("", "", relative, parsed.query, parsed.fragment))


def render_mermaid(documents, markdown):
    """Use the official CLI at build time; the resulting handbook needs no renderer."""
    diagrams = []
    for document in documents:
        title = document["title"]
        tokens = markdown.parse(document["text"])
        for index, token in enumerate(tokens):
            if token.type == "heading_open":
                title = tokens[index + 1].content
            if token.type == "fence" and token.info.strip().split()[:1] == ["mermaid"]:
                diagrams.append({"source": token.content, "title": title})
    if not diagrams:
        return diagrams
    with TemporaryDirectory(prefix="handbook-mermaid-") as directory:
        directory = Path(directory)
        # Vertical ranks keep long pipelines readable without changing their edges.
        definition = "\n".join("```mermaid\n" + re.sub(r"^(flowchart|graph)\s+LR\b", r"\1 TB", d["source"]) + "```" for d in diagrams)
        (directory / "input.md").write_text(definition, encoding="utf-8")
        (directory / "config.json").write_text(json.dumps(MERMAID_CONFIG), encoding="utf-8")
        launch = {"args": ["--no-sandbox"]} if hasattr(os, "geteuid") and os.geteuid() == 0 else {}
        chromium = os.environ.get("PUPPETEER_EXECUTABLE_PATH")
        if not chromium:
            installed = sorted((Path.home() / ".cache/ms-playwright").glob("chromium-*/chrome-linux*/chrome"))
            chromium = str(installed[-1]) if installed else None
        if chromium:
            launch["executablePath"] = chromium
        (directory / "browser.json").write_text(json.dumps(launch), encoding="utf-8")
        subprocess.run(["npx", "--yes", f"--package=@mermaid-js/mermaid-cli@{MERMAID_VERSION}", "mmdc",
                        "-i", str(directory / "input.md"), "-o", str(directory / "rendered.md"),
                        "-c", str(directory / "config.json"), "-p", str(directory / "browser.json"),
                        "-j", "2", "-q"], check=True, timeout=180)
        for number, diagram in enumerate(diagrams, 1):
            svg = (directory / f"rendered-{number}.svg").read_text(encoding="utf-8")
            # Each embedded SVG must have its own node, marker, and CSS IDs.
            ids = {value: f"diagram-{number}-{value}" for value in re.findall(r'\bid="([^"]+)"', svg)}
            svg = re.sub(r'\bid="([^"]+)"', lambda m: f'id="{ids[m[1]]}"', svg)
            svg = re.sub(r"#([\w:.-]+)", lambda m: "#" + ids.get(m[1], m[1]), svg)
            width = float(re.search(r'viewBox="[^\"]*? ([\d.]+) [\d.]+"', svg)[1])
            diagram.update(svg=svg, width=width)
    return diagrams


def render_document(document, documents, markdown):
    tokens = markdown.parse(document["text"])
    headings, breaks = [], [0]
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            anchor = f'{document["id"]}-h{len(headings) + 1}'
            token.attrSet("id", anchor)
            token.attrSet("data-source-heading", "true")
            title = tokens[index + 1].content
            headings.append((token.tag, anchor, title))
            if token.tag == "h2":
                breaks.append(index)
        for child in token.children or []:
            if child.type == "link_open":
                child.attrSet("href", rewrite_link(child.attrGet("href") or "", document["path"], documents))
            elif child.type == "image":
                child.attrSet("src", rewrite_link(child.attrGet("src") or "", document["path"], documents))
    breaks = sorted(set([*breaks, len(tokens)]))
    chapters = []
    for start, end in zip(breaks, breaks[1:]):
        heading = tokens[start] if tokens[start].type == "heading_open" else None
        title = tokens[start + 1].content if heading else document["title"]
        anchor = heading.attrGet("id") if heading else document["id"]
        chapters.append(
            f'<section class="chapter" data-title="{escape(title, quote=True)}" data-anchor="{anchor}">'
            + markdown.renderer.render(tokens[start:end], markdown.options, {}) + "</section>"
        )
    document["headings"] = len(headings)
    document["tables"] = sum(token.type == "table_open" for token in tokens)
    document["codes"] = [token.content for token in tokens if token.type in ("fence", "code_block")]
    document["chapters"] = len(chapters)
    links = "".join(
        f'<a class="toc-link" href="#{anchor}">{escape(title)}</a>'
        for tag, anchor, title in headings if tag == "h2"
    )
    navigation = (
        f'<details class="nav-group" open><summary>{escape(document["title"])}</summary>'
        f'<p class="nav-tag">{escape(document["tag"])}</p>'
        f'<a class="toc-link" href="#{document["id"]}">文档开篇</a>{links}</details>'
    )
    article = (
        f'<article class="source-document" id="{document["id"]}" data-source="{escape(document["relative"])}" '
        f'data-title="{escape(document["title"])}" data-sha256="{document["hash"]}">'
        f'<header class="document-header"><div class="document-label">文档 {document["id"][4:]} · {escape(document["tag"])}</div>'
        f'<h2>{escape(document["title"])}</h2><div class="document-meta">来源：'
        f'<a href="{rewrite_link(document["path"].name, document["path"], [])}">{escape(document["relative"])}</a>'
        f' · {len(document["text"].splitlines()):,} 行 · 正文完整保留</div></header>'
        + '<div class="document-body">' + "".join(chapters) + '</div><a class="back-top" href="#overview">返回项目概览 ↑</a></article>'
    )
    return navigation, article


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


class HandbookCheck(HTMLParser):
    """A generation-time check for lost content, duplicate IDs, and broken navigation."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids, self.anchors, self.documents = [], [], {}
        self.current = None
        self.in_code = False
        self.body_depth = 0
        self.in_code_header = False
        self.ui_depth = 0
        self.diagrams = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.ui_depth:
            self.ui_depth += 1
        elif "data-doc-ui" in attrs:
            self.ui_depth = 1
        if tag == "svg":
            self.diagrams += 1
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "a" and attrs.get("href", "").startswith("#"):
            self.anchors.append(attrs["href"][1:])
        if tag == "script":
            assert "src" not in attrs, "The handbook must work without external scripts"
        if tag == "link":
            assert attrs.get("rel") != "stylesheet", "The handbook must embed its styles"
        if tag == "article" and "data-source" in attrs:
            self.current = {"headings": 0, "tables": 0, "codes": [], "text": []}
            self.documents[attrs["data-source"]] = self.current
        if self.current is not None:
            if tag == "div" and attrs.get("class") == "document-body":
                self.body_depth = 1
            elif tag == "div" and self.body_depth:
                self.body_depth += 1
            if tag == "div" and attrs.get("class") == "code-header":
                self.in_code_header = True
            if "data-source-heading" in attrs:
                self.current["headings"] += 1
            if tag == "table":
                self.current["tables"] += 1
            if tag == "pre":
                self.in_code = True
                self.current["codes"].append("")

    def handle_endtag(self, tag):
        if self.ui_depth:
            self.ui_depth -= 1
        if tag == "div" and self.body_depth:
            self.body_depth -= 1
            self.in_code_header = False
        if tag == "pre":
            self.in_code = False
        if tag == "article":
            self.current = None

    def handle_data(self, data):
        if self.current is not None and self.body_depth and not self.in_code_header and not self.ui_depth:
            self.current["text"].append(data)
        if self.current is not None and self.in_code:
            self.current["codes"][-1] += data


def check(html, documents):
    checker = HandbookCheck()
    checker.feed(html)
    assert len(checker.ids) == len(set(checker.ids)), "Duplicate HTML IDs"
    assert set(checker.anchors) <= set(checker.ids), "Broken chapter links"
    assert len(checker.documents) == len(documents), "Missing source document"
    assert checker.diagrams == sum(len(re.findall(r"^```mermaid\s*$", doc["text"], re.M)) for doc in documents), "Missing rendered Mermaid diagram"
    original_renderer = MarkdownIt("commonmark", {"html": False}).enable("table")
    for document in documents:
        rendered = checker.documents[document["relative"]]
        assert rendered["headings"] == document["headings"], document["relative"]
        assert rendered["tables"] == document["tables"], document["relative"]
        assert rendered["codes"] == document["codes"], document["relative"]
        original = PlainText()
        original.feed(original_renderer.render(document["text"]))
        assert " ".join("".join(rendered["text"]).split()) == " ".join("".join(original.parts).split()), f'Lost source text: {document["relative"]}'
    assert rewrite_link("../README.md", DOCS / ORDER[1], documents) == "#doc-1"
    assert rewrite_link("https://example.com/a?q=1#b", DOCS / DESIGN, documents) == "https://example.com/a?q=1#b"


def main():
    documents = sources()
    markdown = MarkdownIt("commonmark", {"html": False}).enable("table")
    markdown.renderer.rules["table_open"] = lambda *_: '<div class="table-scroll" role="region" aria-label="文档表格，可横向滚动" tabindex="0"><table>\n'
    markdown.renderer.rules["table_close"] = lambda *_: "</table></div>\n"
    diagrams = iter(enumerate(render_mermaid(documents, markdown), 1))
    fence = markdown.renderer.rules["fence"]

    def render_fence(tokens, index, options, env):
        language = tokens[index].info.strip().split()[0] if tokens[index].info.strip() else "text"
        label = "Mermaid 流程定义 · 原文" if language == "mermaid" else language.upper()
        code = (
            f'<div class="code-block"><div class="code-header"><span>{escape(label)}</span>'
            '<button type="button" data-copy aria-label="复制代码">复制</button></div>'
            + fence(tokens, index, options, env) + "</div>\n"
        )
        if language != "mermaid":
            return code
        number, diagram = next(diagrams)
        buttons = "".join(f'<button type="button" data-diagram-action="{action}">{text}</button>'
                          for action, text in [("out", "缩小"), ("in", "放大"), ("fit", "适应宽度"),
                                               ("reset", "原始大小"), ("fullscreen", "全屏查看")])
        return (
            f'<figure class="diagram" style="--diagram-width:{diagram["width"]}px">'
            f'<figcaption class="diagram-toolbar" data-doc-ui><strong>流程图 {number} · {escape(diagram["title"])}</strong>'
            f'<div class="diagram-actions">{buttons}</div></figcaption>'
            '<div class="diagram-hint" data-doc-ui>按清晰字号显示；宽图可横向滚动，也可放大或全屏查看。</div>'
            f'<div class="diagram-viewport" role="region" aria-label="流程图 {number}，可滚动查看" tabindex="0" data-doc-ui>{diagram["svg"]}</div>'
            '<details class="diagram-source"><summary data-doc-ui>查看 Mermaid 原始定义</summary>'
            + code + '</details></figure>\n'
        )

    markdown.renderer.rules["fence"] = render_fence
    rendered = [render_document(document, documents, markdown) for document in documents]
    toc = "".join(nav for nav, _ in rendered)
    articles = "".join(article for _, article in rendered)
    cards = "".join(
        f'<a class="reading-card" href="#{doc["id"]}"><strong>{escape(doc["title"])}</strong>'
        f'<small>{escape(doc["tag"])} · {doc["chapters"]} 个阅读段落</small></a>' for doc in documents
    )
    rows = "".join(
        f'<tr><td>{escape(doc["relative"])}</td><td>{doc["bytes"]:,} 字节</td>'
        f'<td>{doc["headings"]}</td><td class="hash">{doc["hash"]}</td></tr>' for doc in documents
    )
    generated = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M %Z")
    heading_count = sum(doc["headings"] for doc in documents)
    table_count = sum(doc["tables"] for doc in documents)
    line_count = sum(len(doc["text"].splitlines()) for doc in documents)
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="源码驱动的智能手册与流程平台完整项目说明：原始设计、运行说明、实施验收、源码 Agent 和 Java 样例分析。">
<title>源码驱动的智能手册与流程平台 · 项目完整说明</title><style>{CSS}</style></head>
<body class="no-js"><a class="skip" href="#content">跳到正文</a>
<header class="topbar"><a class="brand" href="#overview">源码驱动的智能手册与流程平台</a><div class="toolbar"><small>项目文档 · 离线合订本</small><button type="button" class="menu-button" aria-controls="sidebar" aria-expanded="false">目录</button><button type="button" data-print>打印 / PDF</button></div></header>
<noscript><p class="noscript">完整正文和目录无需 JavaScript；章节搜索与复制按钮需要启用 JavaScript。打印可使用浏览器菜单。</p></noscript>
<aside class="sidebar" id="sidebar" aria-label="文档导航"><label class="search-label" for="doc-search">搜索完整文档</label><div class="search-box"><input type="search" id="doc-search" placeholder="如：租户、LangGraph、JDT" autocomplete="off" aria-controls="search-results"><button type="button" data-clear aria-label="清空搜索">清空</button></div><p class="search-status" id="search-status" role="status">按章节搜索完整正文，也可使用浏览器 Ctrl / ⌘ + F。</p><nav id="toc" aria-label="章节目录"><a class="toc-root" href="#overview">项目概览与阅读指南</a>{toc}</nav><div id="search-results" hidden aria-label="搜索结果"></div></aside>
<main id="content"><section class="hero" id="overview"><div class="eyebrow">SOURCE → EVIDENCE → KNOWLEDGE → ACTION</div><h1>把源码证据变成可信手册、<br>可追溯问答与受控流程</h1><p>本项目面向内部研发、测试和文档人员，以及外部产品客户。从固定版本的 Java 源码建立事实索引，由模型形成待审核的分析与内容，再按实际部署版本、租户和权限提供客户知识与流程服务。</p><div class="stats"><div class="stat"><strong>{len(documents)}</strong><span>份完整文档</span></div><div class="stat"><strong>38</strong><span>章原始设计 + 3 个附录</span></div><div class="stat"><strong>{heading_count}</strong><span>个原文标题</span></div><div class="stat"><strong>{table_count}</strong><span>张原文表格</span></div></div><p class="meta">整合生成：{generated} · 共 {line_count:,} 行源文档 · 样式、搜索和正文均内嵌，可直接离线打开</p></section>
<section class="panel"><h2>项目定位与实际工作链路</h2><p>系统将源码索引、内部分析、知识发布和客户执行连接起来。DeepSeek 负责解释证据、归纳和生成候选；应用与数据库维护授权范围、版本、预算、审核摘要、发布指针和任务状态。源码分析结果先进入内部知识，经过独立内容审核与发布才成为客户可见资料。</p><ol class="pipeline" aria-label="系统工作链路"><li><strong>接入与固定版本</strong><span>受控 Git / 目录、完整 SHA、Java release 与依赖上下文。</span></li><li><strong>建立源码事实</strong><span>JDT 提取符号、调用候选、框架声明、SQL 和行号证据。</span></li><li><strong>分析与人工审核</strong><span>有界 LangGraph Agent、七个只读工具、校验、冻结与人审。</span></li><li><strong>发布客户知识</strong><span>功能映射、手册候选、内容批准、完整知识快照和版本投影。</span></li><li><strong>问答与受控执行</strong><span>租户问答、反馈、固定只读流程的预览、确认、执行与对账。</span></li></ol><p>当前实现采用 Python / FastAPI、PostgreSQL（兼容 SQLite）、Java / JDT，以及 Vue 3 + TypeScript + Vite 双前端。TestPilot 通过独立认证的 HTTP API 提供测试探索、脚本资产和运行结果；DeepSeek 的服务端配置沿用 TestPilot 设置。</p><div class="note">阅读时请区分三个范围：原始 V1.6 文档描述完整平台的设计目标；README 和 Agent 文档说明已实现机制；验收报告记录 2026-10-04 的场景与结果。历史结果、样本结果和规划指标均按原文保留，本文整合不产生新的业务验收结论。</div></section>
<section class="panel"><h2>按需要阅读</h2><p>了解怎样运行，从运行说明开始；理解完整规划，阅读设计方案；核对交付范围，查看验收报告；追踪模型分析机制，查看 Agent 工作流；了解订单导出示例，查看 Java 样例分析。</p><div class="reading-list">{cards}</div><p>后续接入的 Java 业务项目统一放在 <code>data/repositories/&lt;项目名&gt;/</code>，保留各自 Git 历史；<code>fixtures/</code> 存放随平台代码管理的回归测试模板。样例的工作目录分析和按 Git ref 生成的固定快照分析具有各自范围，详见原文。</p><p>所有下列章节均为源文档完整转换。文档之间的链接可在本页跳转；源码、验证产物及 Markdown 原文链接按仓库相对路径保留。外部参考资料需要联网访问。Mermaid 流程图已内嵌为离线矢量图，支持缩放与全屏查看；原始定义可展开核对。</p></section>
{articles}
<section class="panel source-list"><h2>合订来源与更新方法</h2><p>正文来自以下文件，未改写原文中的时间、指标、设计结论或未完成项。源文档 SHA-256 用于核对本合订本对应的输入。</p><details><summary>查看来源清单与完整性摘要</summary><div class="table-scroll" role="region" aria-label="来源清单，可横向滚动" tabindex="0"><table><thead><tr><th>源文件</th><th>大小</th><th>标题数</th><th>SHA-256</th></tr></thead><tbody>{rows}</tbody></table></div></details><p>更新 Markdown 后，在仓库根目录运行 <code>uv run python scripts/build_project_docs.py</code> 重新生成本页。生成器同时核对文档数量、标题、表格、代码块与目录锚点；新增的 <code>docs/**/*.md</code> 会自动并入。</p></section>
<footer class="footer">源码驱动的智能手册与流程平台 · 完整项目文档 · <a href="#overview">返回顶部</a></footer></main><script>{JS}</script></body></html>
"""
    check(html, documents)
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"Generated {OUTPUT.relative_to(ROOT)}: {len(documents)} documents, {heading_count} headings, {table_count} tables; integrity checks passed.")


if __name__ == "__main__":
    main()
