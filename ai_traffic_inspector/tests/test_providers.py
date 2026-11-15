import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import providers as P  # noqa: E402


def sse(*events):
    return "".join(f"event: x\ndata: {json.dumps(e)}\n\n" for e in events)


def test_identify_and_client():
    assert P.identify_app("claude.ai").name == "Claude"
    assert P.identify_app("chatgpt.com").name == "ChatGPT"
    assert P.identify_app("grok.com").name == "Grok"
    assert P.identify_app("www.meta.ai").name.startswith("Meta AI")
    assert P.identify_app("example.com") is None
    assert P.detect_client("Mozilla/5.0 (Macintosh) AppleWebKit Claude/1.2.3 Chrome/130 Electron/33 Safari/537") == "Claude Desktop"
    assert P.detect_client("ChatGPT/1.2025.300 (Mac OS X 15.1; arm64; build 1)") == "ChatGPT app"
    assert P.detect_client("Mozilla/5.0 (Macintosh) AppleWebKit/605 Version/18.0 Safari/605") == "Safari"


def test_claude_with_web_search():
    app = P.identify_app("claude.ai")
    body = json.dumps({"prompt": "best cap rates for industrial 2026?", "timezone": "America/New_York"}).encode()
    resp = sse(
        {"type": "message_start", "message": {"model": "claude-x"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "name": "web_search", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"query\": \"industrial "}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "cap rates 2026\"}"}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_result", "content": [
            {"type": "knowledge", "title": "Industrial Outlook", "url": "https://www.cbre.com/insights/industrial-2026"},
            {"type": "knowledge", "title": "Cap Rate Survey", "url": "https://www.jll.com/en-us/cap-rates"}]}},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "Industrial cap rates "}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "are about 5.5%."}},
    )
    a = P.analyze_exchange(app, "/api/organizations/x/chat_conversations/y/completion", body, "application/json", resp)
    assert a["prompt"] == "best cap rates for industrial 2026?"
    assert a["response_text"] == "Industrial cap rates are about 5.5%."
    assert a["model"] == "claude-x"
    assert a["search_queries"] == ["industrial cap rates 2026"]
    assert [s["domain"] for s in a["sources"]] == ["cbre.com", "jll.com"]
    assert a["sources"][0]["title"] == "Industrial Outlook"


def test_chatgpt_delta_encoding():
    app = P.identify_app("chatgpt.com")
    body = json.dumps({"action": "next", "model": "auto", "messages": [
        {"author": {"role": "user"}, "content": {"content_type": "text", "parts": ["who owns 1 main st?"]}}]}).encode()
    resp = sse(
        {"v": {"message": {"id": "t1", "author": {"role": "assistant"}, "recipient": "web",
                           "content": {"content_type": "code", "text": "search(...)"},
                           "metadata": {"search_queries": [{"type": "search", "q": "1 main st owner"}]}}}},
        {"v": {"message": {"id": "m1", "author": {"role": "assistant"}, "content": {"content_type": "text", "parts": [""]},
                           "metadata": {"model_slug": "gpt-5", "search_result_groups": [
                               {"domain": "zillow.com", "entries": [{"url": "https://www.zillow.com/x", "title": "1 Main"}]}]}}}},
        {"p": "/message/content/parts/0", "o": "append", "v": "It is owned "},
        {"v": "by ACME LLC."},
        {"p": "", "o": "patch", "v": [{"p": "/message/content/parts/0", "o": "append", "v": " Source: county records."},
                                     {"p": "/message/status", "o": "replace", "v": "finished_successfully"}]},
    ) + "data: [DONE]\n\n"
    a = P.analyze_exchange(app, "/backend-api/f/conversation", body, "application/json", resp)
    assert a["prompt"] == "who owns 1 main st?"
    assert a["response_text"] == "It is owned by ACME LLC. Source: county records."
    assert a["model"] == "gpt-5"
    assert a["search_queries"] == ["1 main st owner"]
    assert a["sources"][0]["domain"] == "zillow.com"


def test_grok_ndjson():
    app = P.identify_app("grok.com")
    body = json.dumps({"message": "latest on fed rates", "modelName": "grok-4"}).encode()
    lines = [
        {"result": {"response": {"token": "thinking", "isThinking": True}}},
        {"result": {"response": {"token": "Rates ", "isThinking": False}}},
        {"result": {"response": {"token": "held.", "isThinking": False}}},
        {"result": {"response": {"webSearchResults": {"results": [{"url": "https://www.reuters.com/a", "title": "Fed"}]}}}},
    ]
    resp = "\n".join(json.dumps(x) for x in lines)
    a = P.analyze_exchange(app, "/rest/app-chat/conversations/new", body, "application/json", resp)
    assert a["prompt"] == "latest on fed rates"
    assert a["response_text"] == "Rates held."
    assert a["model"] == "grok-4"
    assert a["sources"][0]["domain"] == "reuters.com"


def test_generic_meta_graphql_form():
    app = P.identify_app("www.meta.ai")
    variables = json.dumps({"message": {"sensitive_string_value": "what is a REIT?"}, "entrypoint": "ABRA__CHAT__TEXT"})
    body = f"fb_api_req_friendly_name=useAbraSendMessageMutation&variables={variables}".encode()
    resp = "\n".join(json.dumps({"data": {"node": {"bot_response_message": {"snippet": s, "sources": [
        {"url": "https://www.investopedia.com/terms/r/reit.asp", "title": "REIT"}]}}}}) for s in
        ["A REIT", "A REIT is a company", "A REIT is a company that owns income-producing real estate."])
    a = P.analyze_exchange(app, "/api/graphql/", body, "application/x-www-form-urlencoded", resp)
    assert a["prompt"] == "what is a REIT?"
    assert a["response_text"] == "A REIT is a company that owns income-producing real estate."
    assert a["sources"][0]["domain"] == "investopedia.com"


def test_own_domains_excluded():
    srcs = P.extract_sources([{"a": "see https://claude.ai/chat/1 and https://cdn.oaistatic.com/x.png and https://example.org/page."}])
    assert [s["url"] for s in srcs] == ["https://example.org/page"]
