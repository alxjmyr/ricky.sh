"""Portable Markdown normalization, fallback, and splitting tests."""

from ricky.messaging.markdown import (
    compose_notification_text,
    normalize_portable_markdown,
    portable_markdown_to_plain_text,
    split_message_text,
)


def test_supported_portable_markdown_survives_normalization() -> None:
    source = """# Result

**Done** with `code` and $x + y$.[^1]

> Note

- [x] Checked

| Item | State |
| --- | --- |
| One | Ready |

```python
print("ok")
```

[^1]: Formula note.
"""

    assert normalize_portable_markdown(source) == source.strip()


def test_unsafe_provider_features_are_neutralized_outside_code() -> None:
    normalized = normalize_portable_markdown(
        "<b>unsafe</b> ![chart](https://example.com/chart.png) "
        "![logo][asset] [channel](https://t.me/admin) @admin "
        "[open](tg://resolve?domain=admin) tg://user?id=1 https://t.me/admin "
        "`<b>literal</b> tg://literal`\n```html\n<b>literal</b>\n"
    )

    assert "&lt;b&gt;unsafe&lt;/b&gt;" in normalized
    assert "Image: [chart](https://example.com/chart.png)" in normalized
    assert "Image: logo" in normalized
    assert "[channel](https://t.me" not in normalized
    assert "![" not in normalized
    assert "[open](tg://" not in normalized
    assert "tg://user" not in normalized
    assert "https://t.me" not in normalized
    assert "@admin" not in normalized
    assert "@\u200badmin" in normalized
    assert "`<b>literal</b> tg://literal`" in normalized
    assert normalized.endswith("```")


def test_all_supported_link_forms_apply_destination_policy() -> None:
    normalized = normalize_portable_markdown(
        '[docs](https://example.com "open") '
        "[thread](https://mail.google.com/mail/u/0/#inbox/abc) "
        "[profile](https://example.com/users/@admin) "
        '[admin](https://t.me/admin "open")\n'
        "[guide][safe] [message][] [channel][unsafe] [unsafe]\n\n"
        '[safe]: https://example.com/guide "Guide"\n'
        "[message]: https://mail.google.com/mail/u/0/#inbox/abc\n"
        "[unsafe]: tg://resolve?domain=admin\n"
    )

    assert '[docs](https://example.com "open")' in normalized
    assert "[thread](https://mail.google.com/mail/u/0/#inbox/abc)" in normalized
    assert "[profile](https://example.com/users/@admin)" in normalized
    assert '[guide](https://example.com/guide "Guide")' in normalized
    assert "[message](https://mail.google.com/mail/u/0/#inbox/abc)" in normalized
    assert "admin" in normalized
    assert "channel" in normalized
    assert "[admin](" not in normalized
    assert "[channel][unsafe]" not in normalized
    assert "[unsafe]" not in normalized
    assert "tg://" not in normalized


def test_title_is_plain_text_escaped_before_markdown_composition() -> None:
    assert (
        compose_notification_text(
            title="Build [ready] *now*",
            body="**Succeeded.**",
            text_format="portable_markdown_v1",
        )
        == "## Build \\[ready\\] \\*now\\*\n\n**Succeeded.**"
    )


def test_plain_projection_keeps_readable_content_and_link_destinations() -> None:
    assert (
        portable_markdown_to_plain_text(
            "## Result\n\n**Done** with [docs](https://example.com).\n\n```\nvalue = 1\n```"
        )
        == "Result\n\nDone with docs (https://example.com).\n\nvalue = 1"
    )


def test_plain_text_splitting_preserves_legacy_exact_chunks() -> None:
    text = "a" * 12
    assert split_message_text(text, text_format="plain_text", limit=5) == [
        "a" * 5,
        "a" * 5,
        "aa",
    ]


def test_markdown_splitting_balances_fences_and_repeats_table_header() -> None:
    fenced = "```text\n" + ("a" * 55) + "\n```"
    fence_parts = split_message_text(
        fenced,
        text_format="portable_markdown_v1",
        limit=30,
    )
    table = "| Name | State |\n| --- | --- |\n" + "\n".join(
        f"| item-{index} | ready |" for index in range(8)
    )
    table_parts = split_message_text(
        table,
        text_format="portable_markdown_v1",
        limit=80,
    )

    assert all(len(part) <= 30 and part.count("```") == 2 for part in fence_parts)
    assert len(table_parts) > 1
    assert all(part.startswith("| Name | State |\n| --- | --- |") for part in table_parts)
    assert all(len(part) <= 80 for part in table_parts)


def test_long_inline_markdown_is_closed_and_reopened_in_every_part() -> None:
    target = "https://example.com/report"
    cases = [
        ("**" + ("a" * 120) + "**", "**", "**"),
        ("`" + ("b" * 120) + "`", "`", "`"),
        ("[" + ("c" * 120) + f"]({target})", "[", f"]({target})"),
    ]

    for source, opener, closer in cases:
        parts = split_message_text(
            source,
            text_format="portable_markdown_v1",
            limit=50,
        )

        assert len(parts) > 1
        assert all(len(part) <= 50 for part in parts)
        assert all(part.startswith(opener) and part.endswith(closer) for part in parts)


def test_exact_inline_boundaries_cover_every_paired_portable_delimiter() -> None:
    for delimiter in ("**", "__", "*", "_", "~~", "==", "||", "$", "$$"):
        parts = split_message_text(
            f"{delimiter}abcdefghij{delimiter}",
            text_format="portable_markdown_v1",
            limit=8 if len(delimiter) == 2 else 6,
        )

        assert parts == [
            f"{delimiter}abcd{delimiter}",
            f"{delimiter}efgh{delimiter}",
            f"{delimiter}ij{delimiter}",
        ]


def test_long_list_item_repeats_its_prefix_and_balances_nested_formatting() -> None:
    parts = split_message_text(
        "- **" + ("mobile " * 30) + "**",
        text_format="portable_markdown_v1",
        limit=60,
    )

    assert len(parts) > 1
    assert all(len(part) <= 60 for part in parts)
    assert all(part.startswith("- ") for part in parts)
    assert all(part[2:].strip().startswith("**") for part in parts)
    assert all(part[2:].strip().endswith("**") for part in parts)


def test_nested_quote_list_and_link_remain_complete_at_every_boundary() -> None:
    target = "https://example.com/report"
    parts = split_message_text(
        "> - **[" + ("linked result " * 20) + f"]({target})**",
        text_format="portable_markdown_v1",
        limit=80,
    )

    assert len(parts) > 1
    assert all(len(part) <= 80 for part in parts)
    assert all(part.startswith("> - **[") for part in parts)
    assert all(part.rstrip().endswith(f"]({target})**") for part in parts)


def test_pathological_link_overhead_degrades_to_balanced_readable_text() -> None:
    parts = split_message_text(
        f"[report](https://example.com/{'x' * 100})",
        text_format="portable_markdown_v1",
        limit=30,
    )

    assert len(parts) > 1
    assert all(part and len(part) <= 30 for part in parts)
    assert all("[" not in part and "]" not in part for part in parts)
    assert "report" in "".join(parts)
