"""Shared user-facing presentation guidance for messaging surfaces."""

MOBILE_MARKDOWN_GUIDANCE = """Messaging presentation guidance:
- Produce concise, mobile-first portable Markdown. Lead with the outcome and keep
  paragraphs short.
- Use headings, emphasis, lists, task lists, compact tables, block quotes, links,
  inline code, fenced code, formulas, and footnotes when they improve comprehension.
- Keep tables to at most three short columns. Replace wide or text-heavy tables
  with one labeled bullet group per item.
- Keep code lines short. Use attachments for large reports, datasets, files, or media.
- Do not emit raw HTML, embedded media or Markdown images, platform-specific links,
  mentions, tags, buttons, maps, or other interactive controls.
"""

__all__ = ["MOBILE_MARKDOWN_GUIDANCE"]
