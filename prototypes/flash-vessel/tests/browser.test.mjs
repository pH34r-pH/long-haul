import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { extname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

let chromium;
try {
  const modulePath = process.env.PLAYWRIGHT_MODULE_PATH ?? "playwright";
  ({ chromium } = createRequire(import.meta.url)(modulePath));
} catch (error) {
  chromium = undefined;
  if (process.env.REQUIRE_BROWSER_TEST === "1") throw error;
}

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const mimeTypes = {
  ".html": "text/html; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
};

async function startServer() {
  const signals = [];
  const server = createServer(async (request, response) => {
    const url = new URL(request.url ?? "/", "http://127.0.0.1");
    if (url.pathname.startsWith("/__test/")) {
      request.resume();
      signals.push(`${url.pathname}${url.search}`);
      response.writeHead(204).end();
      return;
    }
    try {
      const candidate = resolve(root, `.${url.pathname === "/" ? "/index.html" : url.pathname}`);
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
  return { server, signals, url: `http://127.0.0.1:${address.port}/` };
}

async function launchBrowser(extraArgs = []) {
  return chromium.launch({
    executablePath: process.env.CHROMIUM_PATH ?? "/usr/bin/chromium",
    headless: process.env.HEADED_BROWSER_TEST !== "1",
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--no-proxy-server", "--host-resolver-rules=MAP example.test 127.0.0.1", ...extraArgs],
  });
}

async function observePageLifecycle(page, role) {
  const observer = `(() => {
    const pageRole = ${JSON.stringify(role)};
    const signal = (event) => navigator.sendBeacon('/__test/' + event + '?role=' + pageRole, event);
    const terminate = Worker.prototype.terminate;
    Worker.prototype.terminate = function () {
      signal("worker-terminated");
      return terminate.call(this);
    };
    addEventListener("pagehide", () => signal("pagehide"), { once: true });
  })();`;
  await page.addInitScript({ content: observer });
}

async function waitForSignal(signals, signal, expectedCount = 1) {
  const expires = Date.now() + 3_000;
  while (Date.now() < expires) {
    if (signals.filter((item) => item === signal).length >= expectedCount) return;
    await new Promise((resolveWait) => setTimeout(resolveWait, 10));
  }
  assert.fail(`Timed out waiting for browser lifecycle signal ${signal}`);
}

async function waitForState(page, states, timeout = 30_000) {
  await page.waitForFunction((expected) => {
    const state = document.querySelector("#status")?.dataset.state;
    return expected.includes(state);
  }, states, { timeout });
  return page.locator("#status").getAttribute("data-state");
}

async function waitForWorkerState(page, states, timeout) {
  assert.notEqual(await waitForState(page, states, timeout), "error");
}

async function assertWorkerLoaded(page, expected) {
  assert.equal(await page.evaluate(() => performance.getEntriesByType("resource")
    .some((entry) => entry.name.endsWith("/worker.mjs"))), expected);
}

async function assertNoStoredState(page) {
  assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
}

async function readSnapshot(page) {
  await waitForWorkerState(page, ["active", "paused"]);
  const snapshot = JSON.parse(await page.locator("#snapshot").textContent());
  assert.equal(snapshot.schemaVersion, 1);
  return snapshot;
}

const browserTestOptions = {
  skip: !chromium && "Playwright is not installed; browser smoke test unavailable",
  timeout: 30_000,
};

test("stop and actual navigation terminate the page-owned worker", browserTestOptions, async () => {
  const { server, signals, url } = await startServer();
  let browser;
  try {
    browser = await launchBrowser();
    const page = await browser.newPage();
    await observePageLifecycle(page, "primary");
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

    await page.getByRole("button", { name: "Stop this tab's worker" }).click();
    await waitForWorkerState(page, ["stopped"]);
    const terminatedSignal = "/__test/worker-terminated?role=primary";
    await waitForSignal(signals, terminatedSignal);
    assert.equal(JSON.parse(await page.locator("#snapshot").textContent()).flashInstanceId, firstSnapshot.flashInstanceId);

    await page.getByRole("button", { name: "Join for this tab" }).click();
    const nextSnapshot = await readSnapshot(page);
    assert.notEqual(nextSnapshot.flashInstanceId, firstSnapshot.flashInstanceId);
    await page.goto(`${url}?navigate-away=1`);
    await waitForSignal(signals, "/__test/pagehide?role=primary");
    await waitForSignal(signals, terminatedSignal, 2);
    assert.equal(await page.locator("#status").getAttribute("data-state"), "idle");
    await assertWorkerLoaded(page, false);
  } finally {
    await browser?.close();
    await new Promise((resolveClose, rejectClose) => server.close((error) => error ? rejectClose(error) : resolveClose()));
  }
});

test("browser tab visibility pauses and resumes its worker", {
  ...browserTestOptions,
  timeout: 20_000,
  skip: !chromium
    ? "Playwright is not installed; browser visibility test unavailable"
    : !process.env.DISPLAY && process.env.REQUIRE_VISIBILITY_TEST !== "1"
      ? "A headed browser display is unavailable; CI runs this test under Xvfb"
      : false,
}, async () => {
  const { server, url } = await startServer();
  let browser;
  let context;
  try {
    browser = await launchBrowser();
    context = await browser.newContext();
    const page = await context.newPage();
    await page.goto(url);
    await page.getByRole("button", { name: "Join for this tab" }).click();
    await waitForWorkerState(page, ["active"]);

    await page.evaluate(() => {
      const opener = document.createElement("button");
      opener.textContent = "Open another tab";
      opener.onclick = () => window.open(location.href, "_blank");
      document.body.append(opener);
    });
    const popupReady = page.waitForEvent("popup");
    await page.getByRole("button", { name: "Open another tab" }).click();
    const secondTab = await popupReady;
    await secondTab.waitForLoadState();
    await page.waitForFunction(() => document.visibilityState === "hidden", undefined, { timeout: 5_000 });
    await waitForWorkerState(page, ["paused"], 5_000);

    await page.bringToFront();
    await page.waitForFunction(() => document.visibilityState === "visible", undefined, { timeout: 5_000 });
    await waitForWorkerState(page, ["active"], 5_000);
  } finally {
    await context?.close();
    await browser?.close();
    await new Promise((resolveClose, rejectClose) => server.close((error) => error ? rejectClose(error) : resolveClose()));
  }
});

test("unsupported WebGPU stays unknown or unavailable", browserTestOptions, async () => {
  const { server, url } = await startServer();
  let browser;
  try {
    browser = await launchBrowser(["--disable-webgpu"]);
    const page = await browser.newPage();
    await page.goto(url);
    await page.getByRole("button", { name: "Join for this tab" }).click();
    const snapshot = await readSnapshot(page);
    assert.ok(["unknown", "unavailable"].includes(snapshot.capabilities.webGpu.status));
  } finally {
    await browser?.close();
    await new Promise((resolveClose, rejectClose) => server.close((error) => error ? rejectClose(error) : resolveClose()));
  }
});

test("insecure origins fail closed before producing a snapshot", browserTestOptions, async () => {
  const { server, signals, url } = await startServer();
  let browser;
  try {
    browser = await launchBrowser();
    const page = await browser.newPage();
    await observePageLifecycle(page, "insecure");
    await page.goto(url.replace("127.0.0.1", "example.test"));
    assert.equal(await page.evaluate(() => isSecureContext), false);

    await page.getByRole("button", { name: "Join for this tab" }).click();
    assert.equal(await waitForState(page, ["error"]), "error");
    await assertWorkerLoaded(page, true);
    await assertNoStoredState(page);
    assert.equal(await page.locator("#snapshot").textContent(), "Waiting for the browser capability snapshot…");
    await waitForSignal(signals, "/__test/worker-terminated?role=insecure");
  } finally {
    await browser?.close();
    await new Promise((resolveClose, rejectClose) => server.close((error) => error ? rejectClose(error) : resolveClose()));
  }
});
