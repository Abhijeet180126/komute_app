"""
Render docs/user-stories-2026-08-23.md to a styled PDF via headless Chrome.
Run with: python3 docs/scripts/generate_user_stories_pdf.py  (from repo root)
"""
import html
import re
import subprocess
import sys
from pathlib import Path

SRC = Path("docs/user-stories-2026-08-23.md")
HTML_OUT = Path("docs/user-stories-2026-08-23.html")
PDF_OUT = Path("docs/user-stories-2026-08-23.pdf")


def inline(text):
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    return text


def convert(md_text):
    lines = md_text.splitlines()
    out = []
    in_list = False
    in_ul_nested = False

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw in lines:
        line = raw.rstrip()

        if not line.strip():
            close_list()
            continue

        if line.startswith("---"):
            close_list()
            out.append("<hr/>")
            continue

        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            close_list()
            level = len(m.group(1))
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            continue

        m = re.match(r"^-\s+(.*)$", line)
        if m:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(m.group(1))}</li>")
            continue

        close_list()
        out.append(f"<p>{inline(line)}</p>")

    close_list()
    return "\n".join(out)


CSS = """
@page { size: A4; margin: 20mm 18mm 22mm 18mm; }
* { box-sizing: border-box; }
body {
    font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
    color: #1c2321;
    font-size: 10.5pt;
    line-height: 1.55;
}
h1 {
    color: #0d5c2f;
    font-size: 22pt;
    margin: 0 0 4pt 0;
    border-bottom: 3px solid #0d5c2f;
    padding-bottom: 8pt;
}
h1 + p {
    color: #55605a;
    font-size: 9.5pt;
    margin-top: 6pt;
}
h2 {
    color: #0d5c2f;
    font-size: 15pt;
    margin: 22pt 0 8pt 0;
    padding: 6pt 10pt;
    background: #eaf5ee;
    border-left: 4px solid #0d5c2f;
    page-break-after: avoid;
    break-after: avoid;
}
h3 {
    color: #14532d;
    font-size: 11.5pt;
    margin: 14pt 0 4pt 0;
    page-break-after: avoid;
    break-after: avoid;
}
h3::before {
    content: "";
}
p { margin: 5pt 0; text-align: justify; }
ul { margin: 4pt 0 10pt 0; padding-left: 18pt; }
li { margin: 3pt 0; }
li::marker { color: #0d5c2f; }
code {
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
    background: #f1f4f2;
    border: 1px solid #dfe6e1;
    border-radius: 3px;
    padding: 1px 4px;
    font-size: 9pt;
    color: #1a3d24;
}
a { color: #0d5c2f; text-decoration: none; border-bottom: 1px solid #b7d8c3; }
hr {
    border: none;
    border-top: 1.5px solid #d8e3dc;
    margin: 18pt 0;
}
strong { color: #14351f; }
.persona-block { background: #f7faf8; border: 1px solid #dfe6e1; border-radius: 6px; padding: 10pt 14pt; margin: 10pt 0 16pt 0; }
h3 { page-break-inside: avoid; break-inside: avoid; }
h3 ~ ul { page-break-inside: avoid; }
"""

TITLE = "Komute — User Stories"
SUBTITLE = "Generated 2026-08-23 &middot; grounded against komute/models.py, komute/views.py, komute/urls.py"


def build_html(body_html):
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{TITLE}</title>
<style>{CSS}</style>
</head>
<body>
<h1>{TITLE}</h1>
<p>{SUBTITLE}</p>
{body_html}
</body>
</html>"""


def main():
    md_text = SRC.read_text(encoding="utf-8")
    # Drop the original H1 title line (we render our own header above).
    md_text = re.sub(r"^# .*\n", "", md_text, count=1)
    body_html = convert(md_text)
    HTML_OUT.write_text(build_html(body_html), encoding="utf-8")

    chrome_bin = None
    for candidate in ("google-chrome", "chromium", "chromium-browser"):
        from shutil import which
        if which(candidate):
            chrome_bin = candidate
            break
    if not chrome_bin:
        print("No Chrome/Chromium binary found on PATH.", file=sys.stderr)
        sys.exit(1)

    cmd = [
        chrome_bin,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-pdf-header-footer",
        f"--print-to-pdf={PDF_OUT.resolve()}",
        str(HTML_OUT.resolve()),
    ]
    subprocess.run(cmd, check=True)
    print(f"Wrote {PDF_OUT}")


if __name__ == "__main__":
    main()
