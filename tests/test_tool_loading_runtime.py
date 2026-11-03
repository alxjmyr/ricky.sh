"""Tool loading at real chat and persistent foreground composition boundaries."""

from pathlib import Path

from gateway_conversation_support import ScriptedProvider, _answer, _ingest, _settings, _tool
from ricky.agent import AgentSession
from ricky.config import RickySettings
from ricky.gateway.conversations import ConversationCoordinator
from ricky.llm import TextPart, ToolResultPart
from ricky.runtime import build_session_runtime
from ricky.tools.integrations.web_search import WebSearchToolset
from ricky.tools.integrations.web_search.types import WebSearchRequest, WebSearchResponse


class SearchProvider:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.closed = False

    async def search(self, request: WebSearchRequest) -> WebSearchResponse:
        self.queries.append(request.query)
        return WebSearchResponse(query=request.query, sources=[])

    async def aclose(self) -> None:
        self.closed = True


async def test_chat_runtime_loads_native_schema_executes_and_resets_next_turn(
    tmp_path: Path,
) -> None:
    settings = RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / "project"),
            "memory": {"enabled": False},
            "workflow": {"enabled": False},
        }
    )
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    search = SearchProvider()
    provider = ScriptedProvider(
        [
            [_tool("load", "load_tools", {"names": ["web_search"]})],
            [_tool("search", "web_search", {"query": "fixture"})],
            [_answer("done")],
            [_answer("next turn")],
        ]
    )
    async with build_session_runtime(
        settings,
        session=session,
        provider=provider,
        project_root=tmp_path,
        web_search_factory=lambda _: WebSearchToolset(search),
    ) as runtime:
        full_spec = next(tool for tool in runtime.registry.specs() if tool.name == "web_search")
        for user_input in ("Search for fixture", "Thank you"):
            events = [event async for event in runtime.agent_loop.run_turn(session, user_input)]
            assert not any(event.kind == "agent_error" for event in events)
        initial, loaded, stable, reset = provider.requests
        assert "load_tools" in {tool.name for tool in initial.tools}
        assert "web_search" not in {tool.name for tool in initial.tools}
        assert "read_file" in {tool.name for tool in initial.tools}
        assert next(tool for tool in loaded.tools if tool.name == "web_search") == full_spec
        assert loaded.tools == stable.tools
        assert reset.tools == initial.tools
        catalog = "\n".join(
            part.text
            for message in initial.messages
            for part in message.content
            if isinstance(part, TextPart)
        )
        assert "web_search" in catalog
        results = [
            part
            for message in session.history
            for part in message.content
            if isinstance(part, ToolResultPart)
        ]
        assert len(results) == 2
        assert all(not result.is_error for result in results)
        assert search.queries == ["fixture"]
    assert provider.closed
    assert search.closed


async def test_gateway_loader_is_scoped_and_resets_across_runtime_reconstruction(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    (tmp_path / "visible.txt").write_text("fixture")
    first_provider = ScriptedProvider(
        [
            [_tool("forbidden", "load_tools", {"names": ["write_file"]})],
            [_tool("load", "load_tools", {"names": ["list_dir"]})],
            [_tool("list", "list_dir", {"path": str(tmp_path)})],
            [_answer("Listed the project")],
        ]
    )
    first = await _ingest(settings, suffix="a", text="List the project")
    first_result = await ConversationCoordinator(
        settings, provider_factory=lambda _name, _settings: first_provider
    ).process(first.id)
    initial, rejected, loaded, completed = first_provider.requests
    assert "delegate_task" in {tool.name for tool in initial.tools}
    assert "list_dir" not in {tool.name for tool in initial.tools}
    assert rejected.tools == initial.tools
    assert "list_dir" in {tool.name for tool in loaded.tools}
    assert loaded.tools == completed.tools
    assert all(
        "write_file" not in {tool.name for tool in request.tools}
        for request in first_provider.requests
    )
    results = [
        part
        for message in completed.messages
        for part in message.content
        if isinstance(part, ToolResultPart)
    ]
    assert results[0].is_error
    assert not results[-1].is_error
    assert "visible.txt" in results[-1].content

    second_provider = ScriptedProvider([[_answer("You're welcome")]])
    second = await _ingest(settings, suffix="b", text="Thanks")
    second_result = await ConversationCoordinator(
        settings, provider_factory=lambda _name, _settings: second_provider
    ).process(second.id)
    assert second_result.session_id == first_result.session_id
    assert second_provider.requests[0].tools == initial.tools
    assert any(
        isinstance(part, ToolResultPart) and "visible.txt" in part.content
        for message in second_provider.requests[0].messages
        for part in message.content
    )
