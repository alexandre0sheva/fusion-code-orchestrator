"""Reports on a benchmark run: Markdown for the docs, one self-contained HTML file, or JSON."""

from fusion.bench.report.data import Report, build_report
from fusion.bench.report.html import render_html
from fusion.bench.report.markdown import render_markdown

__all__ = ["Report", "build_report", "render_html", "render_markdown"]
