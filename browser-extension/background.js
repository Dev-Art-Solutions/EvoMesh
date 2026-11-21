/**
 * EvoMesh Browser Bridge -- the Chrome side.
 *
 * Connects to the native-messaging host (evomesh's browser_bridge.py,
 * spawned by Chrome itself once this connects -- see
 * scripts/install-chrome-bridge.ps1) and executes the closed set of actions
 * below. Tab and navigation actions run here; page actions run in the tab
 * through page_agent.js. There is no action that runs arbitrary code.
 * Nothing here reaches out to the network or a server of its own; it only
 * talks to the local host process Chrome started.
 *
 * MV3 service workers are ephemeral -- Chrome can and will kill this one
 * when it has been idle, dropping the native-messaging port with it. The
 * alarm below is what notices and reconnects; an open native port also
 * keeps the worker alive while it is connected.
 */

const HOST_NAME = "com.evomesh.browser_bridge";
const RECONNECT_ALARM = "evomesh-reconnect";
// Under native messaging's 1 MB limit for one message to the host, with
// room for the envelope. A larger reply (a screenshot) goes in pieces.
const CHUNK_CHARS = 512 * 1024;
const MAX_WAIT_MS = 30000;
const OWN_TABS_KEY = "evomeshOwnTabs";

let port = null;

function connect() {
  if (port) {
    return;
  }
  try {
    port = chrome.runtime.connectNative(HOST_NAME);
  } catch (err) {
    console.warn("EvoMesh Browser Bridge: connectNative failed", err);
    port = null;
    return;
  }
  port.onMessage.addListener(onRequest);
  port.onDisconnect.addListener(() => {
    const err = chrome.runtime.lastError;
    if (err) {
      console.warn("EvoMesh Browser Bridge: native host disconnected:", err.message);
    }
    port = null;
  });
}

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
chrome.alarms.create(RECONNECT_ALARM, { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === RECONNECT_ALARM) {
    connect();
  }
});
connect();

class ActionError extends Error {
  constructor(code, message, details) {
    super(message);
    this.code = code;
    this.details = details || {};
  }
}

function reply(message) {
  if (!port) return;
  const text = JSON.stringify(message);
  if (text.length <= CHUNK_CHARS) {
    port.postMessage(message);
    return;
  }
  const count = Math.ceil(text.length / CHUNK_CHARS);
  for (let index = 0; index < count; index += 1) {
    port.postMessage({
      id: message.id,
      chunk_index: index,
      chunk_count: count,
      part: text.slice(index * CHUNK_CHARS, (index + 1) * CHUNK_CHARS),
    });
  }
}

async function onRequest(message) {
  const { id, action } = message || {};
  if (!id) {
    console.warn("EvoMesh Browser Bridge: request with no id, ignored", message);
    return;
  }
  try {
    const result = await dispatch(action, message);
    reply({ id, ...result });
  } catch (err) {
    const code = err instanceof ActionError ? err.code : "browser_error";
    const details = err instanceof ActionError ? err.details : {};
    reply({
      id,
      success: false,
      action,
      error: { code, message: String((err && err.message) || err), ...details },
    });
  }
}

const PAGE_ACTIONS = new Set([
  "snapshot", "read_page", "get_page_structure", "get_elements", "get_element_attributes",
  "get_page_metadata", "get_links", "wait_for_element", "click", "double_click", "type_text",
  "fill", "clear", "press_key", "scroll", "select_option", "check", "uncheck",
  "describe_target",
]);
// After these, a navigation may follow; the reply reports where the tab went.
const MAY_NAVIGATE = new Set(["click", "double_click", "press_key", "select_option", "check"]);

async function dispatch(action, message) {
  if (PAGE_ACTIONS.has(action)) return pageAction(action, message);
  switch (action) {
    case "ping":
      return {
        success: true,
        extension_id: chrome.runtime.id,
        extension_version: chrome.runtime.getManifest().version,
        page_agent_actions: Array.from(PAGE_ACTIONS),
      };
    case "list_tabs":
      return listTabs();
    case "get_active_tab":
      return { success: true, action, tab: describeTab(await tabFor(undefined)) };
    case "new_tab":
      return newTab(message);
    case "close_tab":
      return closeTab(message);
    case "switch_tab":
      return switchTab(message);
    case "navigate":
      return navigate(message);
    case "back":
    case "forward":
    case "reload":
      return history(action, message);
    case "wait_for_navigation":
      return waitForNavigation(message);
    case "screenshot":
      return screenshot(message, false);
    case "element_screenshot":
      return screenshot(message, true);
    default:
      throw new ActionError("invalid_request", `unknown action: ${action}`);
  }
}

// -- tabs ------------------------------------------------------------------

function describeTab(tab) {
  return {
    tab_id: tab.id,
    url: tab.url || tab.pendingUrl || "",
    title: tab.title || "",
    active: Boolean(tab.active),
    status: tab.status || "",
    window_id: tab.windowId,
  };
}

async function tabFor(tabId) {
  if (tabId !== undefined && tabId !== null) {
    try {
      return await chrome.tabs.get(Number(tabId));
    } catch (err) {
      throw new ActionError("tab_not_found", `no tab with id ${tabId}; list_tabs shows the open ones`);
    }
  }
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (tab) return tab;
  const [any] = await chrome.tabs.query({ active: true });
  if (any) return any;
  throw new ActionError("tab_not_found", "no active tab (is a Chrome window open?)");
}

async function ownTabs() {
  const stored = await chrome.storage.session.get(OWN_TABS_KEY);
  return new Set(stored[OWN_TABS_KEY] || []);
}

async function rememberTab(tabId, keep) {
  const tabs = await ownTabs();
  if (keep) tabs.add(tabId);
  else tabs.delete(tabId);
  await chrome.storage.session.set({ [OWN_TABS_KEY]: Array.from(tabs) });
}

async function listTabs() {
  const tabs = await chrome.tabs.query({});
  const own = await ownTabs();
  return {
    success: true,
    action: "list_tabs",
    tabs: tabs.map((tab) => ({ ...describeTab(tab), opened_by_evomesh: own.has(tab.id) })),
  };
}

async function newTab(message) {
  const tab = await chrome.tabs.create({ url: message.url || "about:blank", active: message.active !== false });
  await rememberTab(tab.id, true);
  if (message.url) await waitForLoad(tab.id, waitMs(message, 20000));
  return { success: true, action: "new_tab", tab: describeTab(await chrome.tabs.get(tab.id)) };
}

async function closeTab(message) {
  const tab = await tabFor(message.tab_id);
  if (message.only_own && !(await ownTabs()).has(tab.id)) {
    throw new ActionError(
      "permission_denied",
      "in your own Chrome, EvoMesh only closes tabs it opened itself",
    );
  }
  await chrome.tabs.remove(tab.id);
  await rememberTab(tab.id, false);
  return { success: true, action: "close_tab", closed: tab.id };
}

async function switchTab(message) {
  if (message.tab_id === undefined) throw new ActionError("invalid_request", "switch_tab needs tab_id");
  const tab = await tabFor(message.tab_id);
  await chrome.tabs.update(tab.id, { active: true });
  await chrome.windows.update(tab.windowId, { focused: true });
  return { success: true, action: "switch_tab", tab: describeTab(await chrome.tabs.get(tab.id)) };
}

// -- navigation --------------------------------------------------------------

function waitMs(message, fallback) {
  const seconds = Number(message.timeout_seconds);
  return Number.isFinite(seconds) && seconds >= 0 ? Math.min(seconds * 1000, MAX_WAIT_MS) : fallback;
}

// Resolves with how the load ended: "complete", "timeout" or an error from
// webNavigation (a DNS failure, a refused connection).
function waitForLoad(tabId, timeoutMs, { requireStart = false } = {}) {
  return new Promise((resolve) => {
    let started = !requireStart;
    let settled = false;
    let timer = null;
    const finish = (outcome) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(onUpdated);
      chrome.webNavigation.onErrorOccurred.removeListener(onError);
      resolve(outcome);
    };
    const onUpdated = (id, change) => {
      if (id !== tabId) return;
      if (change.status === "loading" || change.url) started = true;
      if (started && change.status === "complete") finish({ state: "complete" });
    };
    const onError = (details) => {
      if (details.tabId === tabId && details.frameId === 0) {
        finish({ state: "error", error: details.error });
      }
    };
    chrome.tabs.onUpdated.addListener(onUpdated);
    chrome.webNavigation.onErrorOccurred.addListener(onError);
    timer = setTimeout(() => finish({ state: started ? "timeout" : "not_started" }), timeoutMs);
  });
}

async function settle(tabId, outcome) {
  const tab = describeTab(await chrome.tabs.get(tabId));
  if (outcome.state === "error") {
    throw new ActionError("navigation_failed", `the page did not load: ${outcome.error}`, { tab });
  }
  return { tab, load: outcome.state };
}

async function navigate(message) {
  const tab = await tabFor(message.tab_id);
  const loaded = waitForLoad(tab.id, waitMs(message, 20000), { requireStart: true });
  await chrome.tabs.update(tab.id, { url: message.url });
  const result = await settle(tab.id, await loaded);
  return { success: true, action: "navigate", tab_id: tab.id, url: result.tab.url, title: result.tab.title, load: result.load };
}

async function history(action, message) {
  const tab = await tabFor(message.tab_id);
  const loaded = waitForLoad(tab.id, waitMs(message, 15000), { requireStart: true });
  if (action === "back") await chrome.tabs.goBack(tab.id);
  else if (action === "forward") await chrome.tabs.goForward(tab.id);
  else await chrome.tabs.reload(tab.id);
  const result = await settle(tab.id, await loaded);
  return { success: true, action, tab_id: tab.id, url: result.tab.url, title: result.tab.title, load: result.load };
}

async function waitForNavigation(message) {
  const tab = await tabFor(message.tab_id);
  const wanted = message.url_contains ? String(message.url_contains) : "";
  const deadline = Date.now() + waitMs(message, 15000);
  for (;;) {
    const current = await chrome.tabs.get(tab.id);
    const url = current.url || current.pendingUrl || "";
    if (current.status === "complete" && (!wanted || url.includes(wanted))) {
      return { success: true, action: "wait_for_navigation", tab_id: tab.id, url, title: current.title };
    }
    if (Date.now() >= deadline) {
      throw new ActionError(
        "navigation_timeout",
        wanted ? `the tab did not reach a URL containing "${wanted}"` : "the tab did not finish loading",
        { tab: describeTab(current) },
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
}

// -- page actions ----------------------------------------------------------

async function inject(tabId) {
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["page_agent.js"] });
  } catch (err) {
    throw new ActionError(
      "page_not_scriptable",
      `this page cannot be read or driven (${err.message}); chrome:// pages and the Web Store never can`,
    );
  }
}

async function runInPage(tabId, request) {
  await inject(tabId);
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId },
    func: (req) => globalThis.__evomeshPage.run(req),
    args: [request],
  });
  return result;
}

async function pageAction(action, message) {
  const tab = await tabFor(message.tab_id);
  const request = { ...message };
  delete request.id;
  const before = tab.url;
  const navigation = MAY_NAVIGATE.has(action)
    ? waitForLoad(tab.id, Number(message.wait_after_ms ?? 1500), { requireStart: true })
    : null;
  let result;
  try {
    result = await runInPage(tab.id, request);
  } catch (err) {
    // A click that navigated tears the page down under the script that ran
    // it; the navigation below is the real outcome then.
    if (!navigation) throw err;
    result = { success: true, action, note: "the page navigated while the action ran" };
  }
  if (!result) {
    throw new ActionError("page_error", "the page returned nothing (did it navigate away?)");
  }
  const reply = { ...result, tab_id: tab.id };
  if (navigation) {
    let outcome = await navigation;
    if (outcome.state === "timeout") {
      // It started but has not finished: give it the rest of the budget.
      outcome = await waitForLoad(tab.id, waitMs(message, 15000));
    }
    const after = await chrome.tabs.get(tab.id);
    if (outcome.state === "error") {
      reply.navigation = { state: "error", error: outcome.error };
    } else if (outcome.state !== "not_started" || after.url !== before) {
      reply.navigation = { state: outcome.state, url: after.url, title: after.title };
    }
    reply.url = after.url;
  }
  return reply;
}

// -- screenshots -----------------------------------------------------------

async function screenshot(message, element) {
  const tab = await tabFor(message.tab_id);
  if (!tab.active) {
    throw new ActionError("tab_not_active", "only the visible tab can be captured; switch_tab to it first");
  }
  const format = message.format === "png" ? "png" : "jpeg";
  let rect = null;
  let viewport = null;
  let dpr = 1;
  let described = null;
  if (element) {
    const result = await runInPage(tab.id, { action: "element_rect", target: message.target, timeout_seconds: message.timeout_seconds });
    if (!result.success) {
      throw new ActionError(result.error.code, result.error.message);
    }
    ({ rect, viewport } = result);
    dpr = result.device_pixel_ratio || 1;
    described = result.element;
  } else {
    const meta = await runInPage(tab.id, { action: "get_page_metadata" }).catch(() => null);
    if (meta && meta.success) {
      viewport = { width: meta.viewport.width, height: meta.viewport.height };
      dpr = meta.viewport.device_pixel_ratio || 1;
    }
  }
  let dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format, quality: 80 });
  if (element) dataUrl = await crop(dataUrl, rect, dpr, format);
  return {
    success: true,
    action: element ? "element_screenshot" : "screenshot",
    tab_id: tab.id,
    url: tab.url,
    viewport,
    device_pixel_ratio: dpr,
    element: described,
    data_url: dataUrl,
  };
}

async function crop(dataUrl, rect, dpr, format) {
  const blob = await (await fetch(dataUrl)).blob();
  const bitmap = await createImageBitmap(blob);
  const x = Math.round(rect.x * dpr);
  const y = Math.round(rect.y * dpr);
  const width = Math.max(1, Math.min(Math.round(rect.width * dpr), bitmap.width - x));
  const height = Math.max(1, Math.min(Math.round(rect.height * dpr), bitmap.height - y));
  const canvas = new OffscreenCanvas(width, height);
  canvas.getContext("2d").drawImage(bitmap, x, y, width, height, 0, 0, width, height);
  const out = await canvas.convertToBlob({ type: `image/${format}`, quality: 0.8 });
  const bytes = new Uint8Array(await out.arrayBuffer());
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return `data:image/${format};base64,${btoa(binary)}`;
}
