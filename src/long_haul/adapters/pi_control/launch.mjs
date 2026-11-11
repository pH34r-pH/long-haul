// Trusted lifecycle wrapper. Candidate bytes are never imported or executed.
import { spawn } from "node:child_process";
import { mkdirSync, readFileSync } from "node:fs";
import { createServer } from "node:http";
const config = JSON.parse(readFileSync("/control/launch.json", "utf8"));
mkdirSync("/tmp/home", { recursive: true });
mkdirSync("/tmp/sessions", { recursive: true });
let server;
if (config.synthetic_provider) {
  const { handler } = await import("./provider.mjs");
  server = createServer(handler(config.synthetic_provider));
  await new Promise((resolve) => server.listen(18765, "127.0.0.1", resolve));
}
const child = spawn("/usr/local/bin/node", [
  "/runtime/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js",
  "--mode", "rpc", "--no-session", "--session-dir", "/tmp/sessions", "--no-approve",
  "--no-context-files", "--no-extensions", "--no-mcp", "--no-skills",
  "--no-prompt-templates", "--no-themes", "--offline", "--provider", "long-haul-local",
  "--model", "fixture", "--thinking", "off", "--tools", config.tools.join(","),
  "--system-prompt", "/agent/SYSTEM.md", "--append-system-prompt", "/agent/APPEND_SYSTEM.md",
  "-e", "/control/guard.mjs",
], { cwd: "/workspace", stdio: ["pipe", "pipe", "pipe"], env: {
  PATH: "/usr/local/bin:/usr/bin:/bin", HOME: "/tmp/home", LANG: "C.UTF-8",
  PI_CODING_AGENT_DIR: "/agent", PI_OFFLINE: "1", PI_SKIP_VERSION_CHECK: "1",
  LONG_HAUL_ATTEMPT_BINDING: JSON.stringify(config.binding),
} });
process.stdin.pipe(child.stdin);
child.stdout.pipe(process.stdout);
child.stderr.pipe(process.stderr);
child.on("error", () => process.exit(70));
child.on("exit", (code) => { server?.close(); process.exit(code ?? 71); });
process.stderr.write("LONG_HAUL_PI_WORKER_STARTED_V1\n");
