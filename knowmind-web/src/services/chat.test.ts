import { describe, expect, it, vi } from "vitest";

import { setAccessToken } from "@/services/auth";
import { streamChatMessage, type ChatStreamHandlers } from "@/services/chat";

function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream({
      start(controller) {
        chunks.forEach((chunk) => controller.enqueue(encoder.encode(chunk)));
        controller.close();
      },
    }),
    { status: 200, headers: { "Content-Type": "text/event-stream" } },
  );
}

describe("streamChatMessage", () => {
  it("handles split SSE chunks and notifies done exactly once", async () => {
    setAccessToken("access-token");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      sseResponse([
        'data: {"type":"trace_id","trace_id":"trace-1"}\n',
        '\ndata: {"type":"delta","text":"你',
        '好"}\n\ndata: {"type":"done"}\n\n',
      ]),
    );
    const handlers: ChatStreamHandlers = {
      onTraceId: vi.fn(),
      onDelta: vi.fn(),
      onDone: vi.fn(),
    };

    await streamChatMessage(
      {
        message: "你好",
        knowledge_base_id: null,
        deep_research: false,
        web_search: false,
      },
      handlers,
    );

    expect(handlers.onTraceId).toHaveBeenCalledWith("trace-1");
    expect(handlers.onDelta).toHaveBeenCalledWith("你好");
    expect(handlers.onDone).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[1]?.headers).toBeInstanceOf(Headers);
    expect((fetchMock.mock.calls[0]?.[1]?.headers as Headers).get("Authorization")).toBe(
      "Bearer access-token",
    );
  });

  it("surfaces an SSE model error and still completes", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      sseResponse(['data: {"type":"error","message":"upstream failed"}\n\n']),
    );
    const onError = vi.fn();
    const onDone = vi.fn();

    await streamChatMessage(
      {
        message: "test",
        knowledge_base_id: null,
        deep_research: false,
        web_search: false,
      },
      { onTraceId: vi.fn(), onDelta: vi.fn(), onError, onDone },
    );

    expect(onError).toHaveBeenCalledWith("upstream failed");
    expect(onDone).toHaveBeenCalledTimes(1);
  });
});
