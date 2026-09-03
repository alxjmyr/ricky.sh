"""Domain-neutral Workflow data operator tests."""

from __future__ import annotations

import pytest

from ricky.workflows.operators import PartitionResult, ValueResult, default_operator_registry


def test_project_filter_sort_limit_and_merge_are_pure_typed_operations() -> None:
    registry = default_operator_registry()
    source = [
        {"id": "b", "kind": "keep", "rank": 2, "extra": "x"},
        {"id": "a", "kind": "drop", "rank": 1, "extra": "y"},
    ]

    filtered = registry.run("filter", {"items": source, "path": "kind", "equals": "keep"})
    assert ValueResult.model_validate(filtered).value == [source[0]]
    projected = registry.run("project", {"value": source, "fields": ["id", "rank"]})
    assert ValueResult.model_validate(projected).value == [
        {"id": "b", "rank": 2},
        {"id": "a", "rank": 1},
    ]
    sorted_result = registry.run("sort", {"items": source, "path": "rank", "descending": False})
    assert ValueResult.model_validate(sorted_result).value == [source[1], source[0]]
    limited = registry.run("limit", {"items": source, "count": 1})
    assert ValueResult.model_validate(limited).value == [source[0]]
    merged = registry.run("merge", {"values": [[1], [2, 3]], "mode": "lists"})
    assert ValueResult.model_validate(merged).value == [1, 2, 3]


def test_partition_uses_declared_domain_neutral_categories() -> None:
    registry = default_operator_registry()
    source = [{"state": "ready"}, {"state": "failed"}, {"state": "other"}]

    result = registry.run(
        "partition",
        {
            "items": source,
            "path": "state",
            "categories": {"ok": "ready", "bad": "failed"},
        },
    )

    parsed = PartitionResult.model_validate(result)
    assert parsed.buckets == {"ok": [source[0]], "bad": [source[1]]}
    assert parsed.unmatched == [source[2]]


def test_operator_missing_path_and_wrong_shape_fail_closed() -> None:
    registry = default_operator_registry()

    with pytest.raises(ValueError, match="missing"):
        registry.run("filter", {"items": [{"x": 1}], "path": "other", "equals": 1})
    with pytest.raises(ValueError, match="not an object"):
        registry.run("project", {"value": [1], "fields": ["id"]})
