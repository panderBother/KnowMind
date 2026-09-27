import json

from app.services import mcp_registry
from app.services.mcp_url_client import (
    list_enabled_url_bindings,
    parse_qualified_tool_name,
    qualified_tool_name,
)


def test_import_rejects_command_and_accepts_url(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mcp_registry, "_REGISTRY_DIR", tmp_path)
    uid = "user-url-1"
    raw = json.dumps(
        {
            "mcpServers": {
                "local-only": {"command": "node", "args": ["server.js"]},
                "remote": {"url": "https://mcp.example.com/sse"},
            },
        },
    )
    out = mcp_registry.import_mcp_json(uid, raw)
    assert out.imported == 1
    assert out.skipped == 1
    assert any("command/stdio" in detail for detail in out.skip_details)
    tools = mcp_registry.list_tools(uid)
    assert len(tools.custom) == 1
    by_name = {c.name: c for c in tools.custom}
    assert by_name["remote"].config.url == "https://mcp.example.com/sse"


def test_import_skips_invalid_and_duplicate(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mcp_registry, "_REGISTRY_DIR", tmp_path)
    uid = "user-url-dup"
    raw = json.dumps({"mcpServers": {"remote": {"url": "https://mcp.example.com/sse"}}})
    mcp_registry.import_mcp_json(uid, raw)
    out = mcp_registry.import_mcp_json(uid, raw)
    assert out.imported == 0
    assert out.skipped == 1
    assert any("已存在" in s for s in out.skip_details)

    out2 = mcp_registry.import_mcp_json(
        uid,
        json.dumps({"mcpServers": {"bad": {"env": {"X": "1"}}}}),
    )
    assert out2.imported == 0
    assert any("缺少 url 或 command" in s for s in out2.skip_details)

    out3 = mcp_registry.import_mcp_json(
        uid,
        json.dumps({"mcpServers": {"insecure": {"url": "http://mcp.example.com/sse"}}}),
    )
    assert out3.imported == 0
    assert any("HTTPS" in s for s in out3.skip_details)


def test_list_enabled_url_bindings_merges_headers(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mcp_registry, "_REGISTRY_DIR", tmp_path)
    uid = "user-url-headers"
    raw = json.dumps(
        {
            "mcpServers": {
                "remote": {
                    "url": "https://a.example/mcp",
                    "headers": {"Authorization": "Bearer from-headers"},
                    "env": {"HEADER_X-Custom": "yes"},
                },
            },
        },
    )
    mcp_registry.import_mcp_json(uid, raw)
    bindings = list_enabled_url_bindings(uid)
    assert len(bindings) == 1
    assert bindings[0].headers["Authorization"] == "Bearer from-headers"
    assert bindings[0].headers["X-Custom"] == "yes"


def test_list_enabled_url_bindings_respects_toggle(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mcp_registry, "_REGISTRY_DIR", tmp_path)
    uid = "user-url-2"
    raw = json.dumps(
        {
            "mcpServers": {
                "svc-a": {"url": "https://a.example/mcp"},
                "svc-b": {"command": "node", "args": ["server.js"]},
            },
        },
    )
    mcp_registry.import_mcp_json(uid, raw)
    dto = mcp_registry.list_tools(uid)
    assert len(dto.custom) == 1
    cid = dto.custom[0].id
    mcp_registry.update_custom_enabled(uid, cid, False)

    bindings = list_enabled_url_bindings(uid)
    assert bindings == []


def test_qualified_tool_name_roundtrip() -> None:
    q = qualified_tool_name("abc-123", "search")
    assert q == "abc-123::search"
    parsed = parse_qualified_tool_name(q)
    assert parsed == ("abc-123", "search")
    assert parse_qualified_tool_name("no-sep") is None
