// Public deterministic OpenAI-compatible fixture, never an LLM/competence claim.
export function handler(config) {
  let turn = 0;
  return (request, response) => {
    let bytes = 0;
    request.on("data", (chunk) => {
      bytes += chunk.length;
      if (bytes > 1048576) request.destroy();
    });
    request.on("end", () => {
      if (request.method !== "POST" || request.url !== "/v1/chat/completions") {
        response.writeHead(404).end(); return;
      }
      if (config.mode === "provider_failure") { response.writeHead(503).end(); return; }
      if (config.mode === "hang") return;
      response.writeHead(200, { "Content-Type": "text/event-stream" });
      const action = config.actions[turn++];
      const delta = action ? { role: "assistant", tool_calls: [{ index: 0, id: `fixture-call-${turn}`,
        type: "function", function: { name: action.tool, arguments: JSON.stringify(action.args) } }] }
        : { role: "assistant", content: "Synthetic fixture complete." };
      const chunk = (delta, finish_reason) => ({ id: `fixture-${turn}`, object: "chat.completion.chunk",
        created: 0, model: "fixture", choices: [{ index: 0, delta, finish_reason }] });
      for (const item of [chunk(delta, null), chunk({}, action ? "tool_calls" : "stop")]) {
        response.write(`data: ${JSON.stringify(item)}\n\n`);
      }
      response.end("data: [DONE]\n\n");
    });
  };
}
