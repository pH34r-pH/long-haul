// The sole explicitly loaded extension. Policy and keys stay in the host.
export default function (pi) {
  pi.on("tool_call", async (event, ctx) => {
    if (!ctx.hasUI || !["read", "write", "edit"].includes(event.toolName)) {
      return { block: true, terminate: true, reason: "Unsupported worker operation" };
    }
    let allowed = false;
    try {
      const binding = JSON.parse(process.env.LONG_HAUL_ATTEMPT_BINDING);
      allowed = await ctx.ui.confirm("long-haul-tool-admission-v1", JSON.stringify({
        ...binding, toolCallId: event.toolCallId, toolName: event.toolName, args: event.input,
      }), { timeout: 15000, signal: ctx.signal });
    } catch {
      allowed = false;
    }
    return allowed === true ? undefined : {
      block: true, terminate: true, reason: "Host admission denied or unavailable",
    };
  });
}
