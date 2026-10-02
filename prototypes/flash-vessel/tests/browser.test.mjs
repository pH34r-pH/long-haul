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

test("browser requires Join, pauses on hidden, stops, and creates a new ephemeral ID", {
  skip: !chromium && "Playwright is not installed; browser smoke test unavailable",
  timeout: 20_000,
}, async () => {
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

  let browser;
  try {
    browser = await chromium.launch({
      executablePath: process.env.CHROMIUM_PATH ?? "/usr/bin/chromium",
      headless: true,
      args: ["--no-sandbox", "--disable-dev-shm-usage"],
    });
    const page = await browser.newPage();
    const address = server.address();
    await page.goto(`http://127.0.0.1:${address.port}/`);

    assert.equal(await page.locator("#status").getAttribute("data-state"), "idle");
    assert.equal(await page.locator("#snapshot").textContent(), "No snapshot yet.");
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
    assert.equal(await page.evaluate(() => performance.getEntriesByType("resource")
      .some((entry) => entry.name.endsWith("/worker.mjs"))), false);

    await page.getByRole("button", { name: "Join for this tab" }).click();
    await page.waitForFunction(() => {
      const state = document.querySelector("#status")?.dataset.state;
      return state === "active" || state === "paused" || state === "error";
    });
    assert.notEqual(await page.locator("#status").getAttribute("data-state"), "error");
    assert.equal(await page.evaluate(() => performance.getEntriesByType("resource")
      .some((entry) => entry.name.endsWith("/worker.mjs"))), true);

    const firstSnapshot = JSON.parse(await page.locator("#snapshot").textContent());
    assert.equal(firstSnapshot.schemaVersion, 1);
    assert.match(firstSnapshot.flashInstanceId, /^[0-9a-f-]{36}$/i);
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);

    await page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await page.waitForFunction(() => document.querySelector("#status")?.dataset.state === "paused");

    await page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await page.waitForFunction(() => document.querySelector("#status")?.dataset.state === "active");

    await page.getByRole("button", { name: "Stop this tab's worker" }).click();
    await page.waitForFunction(() => document.querySelector("#status")?.dataset.state === "stopped");

    await page.getByRole("button", { name: "Join for this tab" }).click();
    await page.waitForFunction(() => {
      const state = document.querySelector("#status")?.dataset.state;
      return state === "active" || state === "paused" || state === "error";
    });
    assert.notEqual(await page.locator("#status").getAttribute("data-state"), "error");
    const secondSnapshot = JSON.parse(await page.locator("#snapshot").textContent());
    assert.notEqual(secondSnapshot.flashInstanceId, firstSnapshot.flashInstanceId);
    await page.evaluate(() => window.dispatchEvent(new Event("pagehide")));
    await page.waitForFunction(() => document.querySelector("#status")?.dataset.state === "stopped");
  } finally {
    await browser?.close();
    await new Promise((resolveClose, rejectClose) => server.close((error) => error ? rejectClose(error) : resolveClose()));
  }
});
