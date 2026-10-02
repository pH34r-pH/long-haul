import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { extname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

let chromium;
try {
  ({ chromium } = createRequire(import.meta.url)("playwright"));
} catch {
  chromium = undefined;
}

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const mimeTypes = {
  ".html": "text/html; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
};

async function startServer() {
  const server = createServer(async (request, response) => {
    try {
      const pathname = new URL(request.url ?? "/", "http://127.0.0.1").pathname;
      const candidate = resolve(root, `.${pathname === "/" ? "/index.html" : pathname}`);
      if (candidate !== root && !candidate.startsWith(`${root}${sep}`)) {
        response.writeHead(403).end("forbidden");
        return;
      }
      const bytes = await readFile(candidate);
      response.writeHead(200, { "content-type": mimeTypes[extname(candidate)] ?? "application/octet-stream" });
      response.end(bytes);
    } catch {
      response.writeHead(404).end("not found");
    }
  });
  await new Promise((resolveListen) => server.listen(0, "127.0.0.1", resolveListen));
  const address = server.address();
  return { server, url: `http://127.0.0.1:${address.port}/` };
}

async function waitForWorkerState(page, states) {
  await page.waitForFunction((expected) => {
    const state = document.querySelector("#status")?.dataset.state;
    return expected.includes(state);
  }, states);
  assert.notEqual(await page.locator("#status").getAttribute("data-state"), "error");
}

async function assertWorkerLoaded(page, expected) {
  assert.equal(await page.evaluate(() => performance.getEntriesByType("resource")
    .some((entry) => entry.name.endsWith("/worker.mjs"))), expected);
}

async function assertNoStoredState(page) {
  assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
}

async function setPageVisibility(page, state) {
  await page.evaluate((visibility) => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: visibility });
    document.dispatchEvent(new Event("visibilitychange"));
  }, state);
  await waitForWorkerState(page, [state === "hidden" ? "paused" : "active"]);
}

async function readSnapshot(page) {
  await waitForWorkerState(page, ["active", "paused"]);
  const snapshot = JSON.parse(await page.locator("#snapshot").textContent());
  assert.equal(snapshot.schemaVersion, 1);
  return snapshot;
}

test("browser requires Join, pauses on hidden, stops, and creates a new ephemeral ID", {
  skip: !chromium && "Playwright is not installed; browser smoke test unavailable",
  timeout: 20_000,
}, async () => {
  const { server, url } = await startServer();

  let browser;
  try {
    browser = await chromium.launch({
      executablePath: process.env.CHROMIUM_PATH ?? "/usr/bin/chromium",
      headless: true,
      args: ["--no-sandbox", "--disable-dev-shm-usage"],
    });
    const page = await browser.newPage();
    await page.goto(url);

    assert.equal(await page.locator("#status").getAttribute("data-state"), "idle");
    assert.equal(await page.locator("#snapshot").textContent(), "No snapshot yet.");
    await assertNoStoredState(page);
    await assertWorkerLoaded(page, false);

    await page.getByRole("button", { name: "Join for this tab" }).click();
    await waitForWorkerState(page, ["active", "paused"]);
    await assertWorkerLoaded(page, true);
    const firstSnapshot = await readSnapshot(page);
    assert.match(firstSnapshot.flashInstanceId, /^[0-9a-f-]{36}$/i);
    await assertNoStoredState(page);

    await setPageVisibility(page, "hidden");
    await setPageVisibility(page, "visible");

    await page.getByRole("button", { name: "Stop this tab's worker" }).click();
    await waitForWorkerState(page, ["stopped"]);

    await page.getByRole("button", { name: "Join for this tab" }).click();
    const secondSnapshot = await readSnapshot(page);
    assert.notEqual(secondSnapshot.flashInstanceId, firstSnapshot.flashInstanceId);
    await page.evaluate(() => window.dispatchEvent(new Event("pagehide")));
    await waitForWorkerState(page, ["stopped"]);
  } finally {
    await browser?.close();
    await new Promise((resolveClose, rejectClose) => server.close((error) => error ? rejectClose(error) : resolveClose()));
  }
});
