import assert from "node:assert/strict";
import { execFile, spawn } from "node:child_process";
import { createServer } from "node:http";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { createRequire } from "node:module";
import { extname, join, resolve, sep } from "node:path";
import { tmpdir } from "node:os";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";
import test from "node:test";

let chromium;
const execFileAsync = promisify(execFile);
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

async function launchBrowser(extraArgs = [], headed = false) {
  return chromium.launch({
    executablePath: process.env.CHROMIUM_PATH ?? "/usr/bin/chromium",
    headless: !headed,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--no-proxy-server", "--host-resolver-rules=MAP example.test 127.0.0.1", ...extraArgs],
  });
}

async function launchVisibilityBrowser() {
  const userDataDir = await mkdtemp(join(tmpdir(), "flash-visibility-"));
  const executablePath = process.env.CHROMIUM_PATH ?? "/usr/bin/chromium";
  const child = spawn(executablePath, [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--no-proxy-server",
    "--host-resolver-rules=MAP example.test 127.0.0.1",
    "--no-first-run",
    "--no-default-browser-check",
    "--remote-debugging-address=127.0.0.1",
    "--remote-debugging-port=0",
    `--user-data-dir=${userDataDir}`,
    "about:blank",
  ], { stdio: "ignore" });
  let spawnError;
  child.once("error", (error) => { spawnError = error; });

  let browser;
  try {
    const activePortPath = join(userDataDir, "DevToolsActivePort");
    const expires = Date.now() + 10_000;
    let port;
    while (Date.now() < expires) {
      if (spawnError) throw spawnError;
      if (child.exitCode !== null) throw new Error(`Chromium exited before CDP became ready (${child.exitCode}).`);
      try {
        port = Number((await readFile(activePortPath, "utf8")).split(/\r?\n/, 1)[0]);
        if (Number.isInteger(port) && port > 0) break;
      } catch (error) {
        if (error.code !== "ENOENT") throw error;
      }
      await new Promise((resolveWait) => setTimeout(resolveWait, 25));
    }
    if (!Number.isInteger(port) || port < 1) throw new Error("Chromium did not publish its CDP port within 10 seconds.");
    // Avoid Playwright launch-time focus emulation so X11 minimization drives native visibility.
    browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`, { noDefaults: true });
    return { browser, child, userDataDir };
  } catch (error) {
    await closeVisibilityBrowser(browser, child, userDataDir);
    throw error;
  }
}

async function waitForProcessExit(child, timeoutMs) {
  if (child.exitCode !== null) return;
  await new Promise((resolveExit) => {
    const timeout = setTimeout(resolveExit, timeoutMs);
    child.once("exit", () => {
      clearTimeout(timeout);
      resolveExit();
    });
  });
}

async function closeVisibilityBrowser(browser, child, userDataDir) {
  await browser?.close().catch(() => {});
  if (child && child.exitCode === null) {
    child.kill("SIGTERM");
    await waitForProcessExit(child, 2_000);
  }
  if (child && child.exitCode === null) {
    child.kill("SIGKILL");
    await waitForProcessExit(child, 2_000);
  }
  if (userDataDir) await rm(userDataDir, { recursive: true, force: true });
}

async function observePageLifecycle(page, role) {
  const observer = `(() => {
    const pageRole = ${JSON.stringify(role)};
    const signal = (event, value = '') => navigator.sendBeacon(
      '/__test/' + event + '?role=' + pageRole + (value ? '&value=' + value : ''),
      value,
    );
    const NativeWorker = window.Worker;
    window.Worker = class ObservedWorker extends NativeWorker {
      constructor(...args) {
        super(...args);
        this.addEventListener('message', ({ data }) => {
          if (data?.type === 'LIFECYCLE') signal('worker-lifecycle', data.lifecycle);
        });
      }

      postMessage(message, ...args) {
        if (message?.type === 'VISIBILITY') signal('worker-visibility', message.visibility);
        return super.postMessage(message, ...args);
      }

      terminate() {
        signal('worker-terminated');
        return super.terminate();
      }
    };
    addEventListener('visibilitychange', () => signal('visibility', document.visibilityState));
    addEventListener('pagehide', () => signal('pagehide'), { once: true });
  })();`;
  await page.addInitScript({ content: observer });
}

async function waitForSignal(signals, signal, expectedCount = 1, timeoutMs = 3_000) {
  const expires = Date.now() + timeoutMs;
  while (Date.now() < expires) {
    if (signals.filter((item) => item === signal).length >= expectedCount) return;
    await new Promise((resolveWait) => setTimeout(resolveWait, 10));
  }
  assert.fail(`Timed out waiting for browser lifecycle signal ${signal}`);
}

async function waitForWindowId(title) {
  const { stdout } = await execFileAsync("xdotool", ["getactivewindow"]);
  const windowId = stdout.trim();
  assert.match(windowId, /^(?:0x)?[0-9a-f]+$/i);
  const { stdout: actualTitle } = await execFileAsync("xdotool", ["getwindowname", windowId]);
  assert.ok(actualTitle.includes(title), `Active X11 window title did not include ${title}: ${actualTitle.trim()}`);
  return windowId;
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
    await page.context().route("https://dashboard.ph34r.dev/**", (route) => route.fulfill({
      status: 200,
      contentType: "text/html",
      body: "<title>Dashboard route smoke</title><main>operations dashboard route</main>",
    }));
    await observePageLifecycle(page, "primary");
    await page.goto(url);

    assert.equal(await page.locator("#status").getAttribute("data-state"), "idle");
    assert.equal(await page.locator("#snapshot").textContent(), "No snapshot yet.");
    await assertNoStoredState(page);
    await assertWorkerLoaded(page, false);

    const operationsLink = page.getByRole("link", { name: "Owner-private operations dashboard (Grafana); sign-in required" });
    assert.equal(await operationsLink.getAttribute("href"), "https://dashboard.ph34r.dev/");
    const operationsTabPromise = page.context().waitForEvent("page");
    await operationsLink.click();
    const operationsTab = await operationsTabPromise;
    try {
      await operationsTab.waitForLoadState("domcontentloaded");
      assert.equal(operationsTab.url(), "https://dashboard.ph34r.dev/");
      assert.equal(await operationsTab.title(), "Dashboard route smoke");
      await assertWorkerLoaded(operationsTab, false);
      await assertWorkerLoaded(page, false);
      assert.equal(signals.some((signal) => signal.includes("worker-")), false);
    } finally {
      await operationsTab.close();
    }
    await assertNoStoredState(page);

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

test("native page visibility pauses and resumes its worker when available", {
  ...browserTestOptions,
  timeout: 20_000,
  skip: !chromium
    ? "Playwright is not installed; browser visibility test unavailable"
    : !process.env.DISPLAY && process.env.REQUIRE_VISIBILITY_TEST !== "1"
      ? "A headed browser display is unavailable; CI runs this test under Xvfb and Openbox"
      : false,
}, async (t) => {
  const { server, signals, url } = await startServer();
  let browser;
  let context;
  let browserProcess;
  let userDataDir;
  try {
    ({ browser, child: browserProcess, userDataDir } = await launchVisibilityBrowser());
    context = browser.contexts()[0];
    assert.ok(context, "CDP connection did not expose Chromium's default browser context");
    const page = context.pages()[0] ?? await context.newPage();
    await observePageLifecycle(page, "visibility");
    await page.goto(url);
    await page.getByRole("button", { name: "Join for this tab" }).click();
    await waitForWorkerState(page, ["active"]);
    await page.bringToFront();

    let windowId;
    try {
      windowId = await waitForWindowId("Flash Vessel capability prototype");
      await execFileAsync("xdotool", ["windowminimize", "--sync", windowId]);
    } catch (error) {
      t.skip(`Real visibility unverified: the test window manager could not hide the browser (${error.message}).`);
      return;
    }
    const hidden = "/__test/visibility?role=visibility&value=hidden";
    let actualVisibility;
    try {
      await waitForSignal(signals, hidden, 1, 5_000);
      actualVisibility = await page.evaluate(() => document.visibilityState);
    } catch (error) {
      try {
        actualVisibility = await page.evaluate(() => document.visibilityState);
      } catch (stateError) {
        await execFileAsync("xdotool", ["windowactivate", "--sync", windowId]);
        t.skip(`Real visibility unverified: no hidden event arrived and the minimized page state was unreadable (${stateError.message}).`);
        return;
      }
      if (actualVisibility === "visible") {
        await execFileAsync("xdotool", ["windowactivate", "--sync", windowId]);
        t.skip(`Real visibility unverified: actual window minimization left document.visibilityState=${actualVisibility}; no hidden event arrived within 5 seconds.`);
        return;
      }
      throw error;
    }
    assert.equal(actualVisibility, "hidden");
    await waitForSignal(signals, "/__test/worker-visibility?role=visibility&value=hidden");
    const paused = "/__test/worker-lifecycle?role=visibility&value=paused";
    await waitForSignal(signals, paused);

    await execFileAsync("xdotool", ["windowactivate", "--sync", windowId]);
    await waitForSignal(signals, "/__test/visibility?role=visibility&value=visible", 1, 5_000);
    assert.equal(await page.evaluate(() => document.visibilityState), "visible");
    await waitForSignal(signals, "/__test/worker-visibility?role=visibility&value=visible");
    const active = "/__test/worker-lifecycle?role=visibility&value=active";
    await waitForSignal(signals, active);
    assert.ok(signals.indexOf(paused) < signals.indexOf(active));
    await waitForWorkerState(page, ["active"], 5_000);
  } finally {
    await closeVisibilityBrowser(browser, browserProcess, userDataDir);
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
