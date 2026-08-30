import { spawn } from "node:child_process";
import { access, mkdir, mkdtemp, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const targetUrl = process.argv[2] || "https://status.ivrm.jp/";
const target = new URL(targetUrl);
const isLocalFixture = ["127.0.0.1", "localhost"].includes(target.hostname);
const outputDir = process.env.OUTPUT_DIR || "/tmp/ivrm-status-browser";
await mkdir(outputDir, { recursive: true });

async function findBrowser() {
  const candidates = [process.env.CHROME_PATH, "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"].filter(Boolean);
  for (const candidate of candidates) {
    try { await access(candidate); return candidate; } catch { /* next */ }
  }
  throw new Error("Chrome/Chromium was not found");
}

function sleep(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

async function fetchWithRetry(url, attempts = 75) {
  let lastError;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(2000) });
      if (response.ok) return response;
      lastError = new Error(`HTTP ${response.status} from ${url}`);
    } catch (error) { lastError = error; }
    await sleep(200);
  }
  throw lastError || new Error(`Unable to fetch ${url}`);
}

const browserPath = await findBrowser();
const profileDir = await mkdtemp(path.join(os.tmpdir(), "ivrm-status-chrome-"));
const port = 9222;
const chromeLog = [];
let chromeExit = null;
const chrome = spawn(browserPath, [
  "--headless",
  "--no-sandbox",
  "--disable-gpu",
  "--disable-dev-shm-usage",
  "--disable-default-apps",
  "--disable-extensions",
  "--disable-sync",
  "--no-first-run",
  "--remote-debugging-address=127.0.0.1",
  `--remote-debugging-port=${port}`,
  `--user-data-dir=${profileDir}`,
  "about:blank",
], { stdio: ["ignore", "ignore", "pipe"] });
chrome.stderr.setEncoding("utf8");
chrome.stderr.on("data", (chunk) => chromeLog.push(chunk));
chrome.once("exit", (code, signal) => { chromeExit = { code, signal }; });

let socket;
const pending = new Map();
let commandId = 0;
const events = { console: [], exceptions: [], logEntries: [], loadingFailed: [], responses: [] };

function send(method, params = {}, timeoutMs = 10000) {
  commandId += 1;
  const id = commandId;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP command timed out: ${method}`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer, method });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

function serializeRemoteObject(value) {
  if (!value) return null;
  if (Object.hasOwn(value, "value")) return value.value;
  return value.description || value.type || null;
}

async function evaluate(expression, { awaitPromise = false } = {}) {
  const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || "Runtime.evaluate failed");
  return result.result.value;
}

async function connectToDevTools() {
  try {
    const versionResponse = await fetchWithRetry(`http://127.0.0.1:${port}/json/version`);
    const version = await versionResponse.json();
    const targetsResponse = await fetchWithRetry(`http://127.0.0.1:${port}/json/list`);
    const targets = await targetsResponse.json();
    const pageTarget = targets.find((item) => item.type === "page");
    if (!pageTarget?.webSocketDebuggerUrl) throw new Error("Chrome page target was not found");
    return { version, pageTarget };
  } catch (error) {
    const stderr = chromeLog.join("").slice(-6000);
    throw new Error(`Chrome DevTools did not start (${browserPath}, exit=${JSON.stringify(chromeExit)}): ${error.message}\n${stderr}`);
  }
}

try {
  const sourceResponse = await fetch(`${targetUrl}?source_check=${Date.now()}`, { signal: AbortSignal.timeout(20000) });
  await writeFile(path.join(outputDir, "source.html"), await sourceResponse.text(), "utf8");
  await writeFile(path.join(outputDir, "headers.json"), JSON.stringify(Object.fromEntries(sourceResponse.headers.entries()), null, 2), "utf8");

  const apiResponse = await fetch(`${targetUrl.replace(/\/$/, "")}/api/status.json?api_check=${Date.now()}`, {
    signal: AbortSignal.timeout(20000), headers: { Accept: "application/json" },
  });
  const apiText = await apiResponse.text();
  await writeFile(path.join(outputDir, "status.json"), apiText, "utf8");
  const apiData = JSON.parse(apiText);
  if (!Array.isArray(apiData.services)) throw new Error("Status API did not return services");

  const { version, pageTarget } = await connectToDevTools();

  socket = new WebSocket(pageTarget.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("CDP WebSocket connection timed out")), 10000);
    socket.addEventListener("open", () => { clearTimeout(timer); resolve(); }, { once: true });
    socket.addEventListener("error", () => { clearTimeout(timer); reject(new Error("CDP WebSocket connection failed")); }, { once: true });
  });

  socket.addEventListener("message", (event) => {
    const message = JSON.parse(String(event.data));
    if (message.id && pending.has(message.id)) {
      const item = pending.get(message.id);
      pending.delete(message.id);
      clearTimeout(item.timer);
      if (message.error) item.reject(new Error(`${item.method}: ${message.error.message}`)); else item.resolve(message.result);
      return;
    }
    if (message.method === "Runtime.consoleAPICalled") events.console.push({ type: message.params.type, args: message.params.args.map(serializeRemoteObject) });
    else if (message.method === "Runtime.exceptionThrown") events.exceptions.push(message.params.exceptionDetails);
    else if (message.method === "Log.entryAdded") events.logEntries.push(message.params.entry);
    else if (message.method === "Network.loadingFailed") events.loadingFailed.push(message.params);
    else if (message.method === "Network.responseReceived") {
      const response = message.params.response;
      events.responses.push({ url: response.url, status: response.status, mimeType: response.mimeType, protocol: response.protocol });
    }
  });

  await Promise.all([send("Page.enable"), send("Runtime.enable"), send("Log.enable"), send("Network.enable")]);

  const navigationUrl = `${targetUrl}?browser_check=${Date.now()}`;
  await send("Page.navigate", { url: navigationUrl });
  await sleep(5000);

  const pageState = await evaluate(`(() => {
    const text = (id) => document.getElementById(id)?.textContent?.trim() ?? null;
    return {
      url: location.href,
      title: document.title,
      readyState: document.readyState,
      overallTitle: text("overallTitle"),
      serviceCount: text("serviceCount"),
      operationalCount: text("operationalCount"),
      activeIncidentCount: text("activeIncidentCount"),
      freshnessText: text("freshnessText"),
      timelineButtons: document.querySelectorAll("button.uptime-bar[data-portal-ready='true']").length,
      activePublicStatusHidden: document.getElementById("activePublicStatus")?.hidden ?? null,
      upcomingMaintenanceHidden: document.getElementById("upcomingMaintenance")?.hidden ?? null,
      recentText: text("incidentList"),
      xssImageCount: document.querySelectorAll('img[src="x"]')?.length || 0,
      bodyScrollWidth: document.body.scrollWidth,
      viewportWidth: innerWidth,
    };
  })()`);

  const desktopShot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  await writeFile(path.join(outputDir, "desktop.png"), Buffer.from(desktopShot.data, "base64"));

  let interactionState = { skipped: true };
  if (isLocalFixture) {
    interactionState = await evaluate(`(async () => {
      const rows = [...document.querySelectorAll(".service-row")];
      const rowByName = (name) => rows.find((row) => row.querySelector(".service-identity strong")?.textContent?.trim() === name);
      const lastTimelineButton = (row) => [...(row?.querySelectorAll("button.uptime-bar[data-portal-ready='true']") || [])].at(-1);
      const related = lastTimelineButton(rowByName("Minecraft Network"));
      const noCause = lastTimelineButton(rowByName("Herta"));
      if (!related || !noCause) return { missingButtons: true };

      const relatedAriaLabel = related.getAttribute("aria-label") || "";
      related.focus();
      await new Promise((resolve) => setTimeout(resolve, 20));
      const focusOpened = !document.getElementById("timelinePopover")?.hidden;

      related.click();
      await new Promise((resolve) => setTimeout(resolve, 20));
      const keyboardActivationKeptOpen = !document.getElementById("timelinePopover")?.hidden;
      const relatedText = document.getElementById("timelinePopover")?.textContent || "";

      const escapeClose = document.querySelector("#timelinePopover .timeline-popover-close");
      escapeClose?.focus();
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      await new Promise((resolve) => setTimeout(resolve, 20));
      const escapeClosed = document.getElementById("timelinePopover")?.hidden === true;
      const escapeRestoredFocus = document.activeElement === related;

      related.click();
      await new Promise((resolve) => setTimeout(resolve, 20));
      const closeButton = document.querySelector("#timelinePopover .timeline-popover-close");
      closeButton?.focus();
      closeButton?.click();
      await new Promise((resolve) => setTimeout(resolve, 20));
      const closeButtonClosed = document.getElementById("timelinePopover")?.hidden === true;
      const closeButtonRestoredFocus = document.activeElement === related;

      noCause.focus();
      await new Promise((resolve) => setTimeout(resolve, 20));
      const focusSwitchOpened = !document.getElementById("timelinePopover")?.hidden;
      const focusSwitchText = document.getElementById("timelinePopover")?.textContent || "";

      noCause.click();
      await new Promise((resolve) => setTimeout(resolve, 20));
      const tapOpened = !document.getElementById("timelinePopover")?.hidden;
      const noCauseText = document.getElementById("timelinePopover")?.textContent || "";
      document.body.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
      const outsideClosed = document.getElementById("timelinePopover")?.hidden === true;

      let copied = null;
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async (value) => { copied = value; } } });
      const copyButton = document.querySelector("[data-portal-copy-url]");
      copyButton?.click();
      await new Promise((resolve) => setTimeout(resolve, 20));

      return {
        focusOpened,
        relatedAriaLabel,
        relatedText,
        keyboardActivationKeptOpen,
        escapeClosed,
        escapeRestoredFocus,
        closeButtonClosed,
        closeButtonRestoredFocus,
        focusSwitchOpened,
        focusSwitchText,
        tapOpened,
        noCauseText,
        outsideClosed,
        copied,
        missingButtons: false,
        skipped: false,
      };
    })()`, { awaitPromise: true });
  }

  const origin = target.origin;
  let durableDeepLinkState = { skipped: true };
  if (isLocalFixture) {
    await send("Page.navigate", { url: `${origin}/?notice=MNT-DEFABC123456` });
    await sleep(3500);
    durableDeepLinkState = await evaluate(`(() => ({
      sectionVisible: document.getElementById("deepLinkedPublicRecord")?.hidden === false,
      deepLinked: Boolean(document.getElementById("public-MNT-DEFABC123456")?.classList.contains("is-deep-linked")),
      text: document.getElementById("deepLinkedPublicRecord")?.textContent || "",
      skipped: false,
    }))()`);
  }

  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await sleep(150);
  const mobileState = await evaluate(`({ viewportWidth: innerWidth, bodyScrollWidth: document.body.scrollWidth })`);
  const mobileShot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  await writeFile(path.join(outputDir, "mobile.png"), Buffer.from(mobileShot.data, "base64"));

  await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
  const reducedMotion = await evaluate(`matchMedia("(prefers-reduced-motion: reduce)").matches`);

  const historyUrl = isLocalFixture ? `${origin}/history/?notice=MNT-DEFABC123456` : `${origin}/history/`;
  await send("Page.navigate", { url: historyUrl });
  await sleep(3500);
  const historyState = await evaluate(`(() => ({
    archivePresent: Boolean(document.getElementById("publicArchive")),
    filters: ["archiveTypeFilter", "archiveStatusFilter", "archiveServiceFilter", "archiveFromDate", "archiveToDate"].every((id) => Boolean(document.getElementById(id))),
    resultCount: document.getElementById("archiveResultCount")?.textContent?.trim() || null,
    deepLinked: Boolean(document.getElementById("public-MNT-DEFABC123456")?.classList.contains("is-deep-linked")),
    oldMaintenanceText: document.getElementById("public-MNT-DEFABC123456")?.textContent || "",
    serviceHistoryPresent: Boolean(document.getElementById("historyServiceList")),
    bodyScrollWidth: document.body.scrollWidth,
    viewportWidth: innerWidth,
  }))()`);

  const diagnostic = {
    browser: version.Browser,
    browserPath,
    isLocalFixture,
    apiServices: apiData.services.length,
    apiOverallStatus: apiData.overall_status,
    pageState,
    interactionState,
    durableDeepLinkState,
    mobileState,
    reducedMotion,
    historyState,
    exceptions: events.exceptions.length,
    loadingFailed: events.loadingFailed.length,
  };
  await writeFile(path.join(outputDir, "page-state.json"), JSON.stringify(diagnostic, null, 2), "utf8");
  await writeFile(path.join(outputDir, "browser-events.json"), JSON.stringify(events, null, 2), "utf8");
  console.log(JSON.stringify(diagnostic, null, 2));

  if (!/^\d+$/.test(pageState.serviceCount || "")) throw new Error(`Rendered serviceCount is not numeric: ${JSON.stringify(pageState.serviceCount)}`);
  if (!pageState.overallTitle || pageState.overallTitle === "サービス状況を確認しています") throw new Error("Rendered page did not leave loading state");
  if (pageState.timelineButtons < 24) throw new Error(`Timeline buttons were not enhanced: ${pageState.timelineButtons}`);
  if (pageState.xssImageCount !== 0) throw new Error("Untrusted public content created an executable image element");

  if (isLocalFixture) {
    if (interactionState.missingButtons) throw new Error("Fixture timeline buttons were not found");
    if (!interactionState.relatedAriaLabel.includes("公開済みの接続障害情報")) throw new Error(`Related timeline bucket was not selected: ${JSON.stringify(interactionState.relatedAriaLabel)}`);
    if (!interactionState.focusOpened) throw new Error("Keyboard focus did not open the timeline popover");
    if (!interactionState.keyboardActivationKeptOpen || !interactionState.relatedText.includes("Incident詳細を見る")) throw new Error("Keyboard activation did not keep related Incident details open");
    if (!interactionState.escapeClosed || !interactionState.escapeRestoredFocus) throw new Error("Escape did not close the timeline popover and restore focus safely");
    if (!interactionState.closeButtonClosed || !interactionState.closeButtonRestoredFocus) throw new Error("Popover close button did not close and restore focus safely");
    if (!interactionState.tapOpened || !interactionState.noCauseText.includes("公開された原因情報はありません")) throw new Error("Tap/no-related-Incident popover behavior failed");
    if (!interactionState.outsideClosed) throw new Error("Outside click did not close timeline popover");
    if (!String(interactionState.copied || "").includes("?")) throw new Error("Clipboard URL copy did not execute");
    if (!durableDeepLinkState.sectionVisible || !durableDeepLinkState.deepLinked || !durableDeepLinkState.text.includes("過去のメンテナンス")) throw new Error(`Home durable deep-link regression: ${JSON.stringify(durableDeepLinkState)}`);
  }

  if (mobileState.bodyScrollWidth > mobileState.viewportWidth) throw new Error(`Mobile layout overflowed: ${JSON.stringify(mobileState)}`);
  if (!reducedMotion) throw new Error("Reduced motion emulation was not applied");
  if (!historyState.archivePresent || !historyState.filters || !historyState.serviceHistoryPresent) throw new Error(`History archive regression: ${JSON.stringify(historyState)}`);
  if (isLocalFixture && (!historyState.deepLinked || !historyState.oldMaintenanceText.includes("過去のメンテナンス"))) throw new Error(`Retention-independent History deep-link regression: ${JSON.stringify(historyState)}`);
  if (historyState.bodyScrollWidth > historyState.viewportWidth) throw new Error(`History mobile layout overflowed: ${JSON.stringify(historyState)}`);
  if (events.exceptions.length > 0) throw new Error(`Browser reported ${events.exceptions.length} uncaught JavaScript exception(s)`);

  console.log("Rendered UI check passed");
} finally {
  await writeFile(path.join(outputDir, "chrome.log"), chromeLog.join(""), "utf8");
  if (socket?.readyState === WebSocket.OPEN) socket.close();
  chrome.kill("SIGTERM");
  await Promise.race([new Promise((resolve) => chrome.once("exit", resolve)), sleep(3000)]);
  if (!chrome.killed) chrome.kill("SIGKILL");
}
