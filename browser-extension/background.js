/**
 * EvoMesh Browser Bridge -- the Chrome side.
 *
 * Connects to the native-messaging host (evomesh's browser_bridge.py,
 * spawned by Chrome itself once this connects -- see
 * scripts/install-chrome-bridge.ps1) and executes whatever it is asked:
 * list open tabs, read one, or navigate it. Nothing here ever reaches out
 * to the network or a server of its own; it only talks to the local host
 * process Chrome started.
 *
 * MV3 service workers are ephemeral -- Chrome can and will kill this one
 * when it has been idle, dropping the native-messaging port with it. The
 * alarm below is what notices and reconnects; without it, a browser left
 * idle for a while would look permanently disconnected to every agent
 * until Chrome happened to wake this worker for some unrelated reason.
 */

const HOST_NAME = "com.evomesh.browser_bridge";
const RECONNECT_ALARM = "evomesh-reconnect";

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
// A service worker can start already-should-be-connected (Chrome woke it
// for some unrelated event); this is the same reconnect attempt as the
// alarm, just not waiting up to a minute for the first one.
connect();

async function onRequest(message) {
  const { id, action } = message || {};
  if (!id) {
    console.warn("EvoMesh Browser Bridge: request with no id, ignored", message);
    return;
  }
  try {
    const result = await dispatch(action, message);
    port.postMessage({ id, ...result });
  } catch (err) {
    port.postMessage({ id, error: String((err && err.message) || err) });
  }
}

async function dispatch(action, message) {
  switch (action) {
    case "list_tabs":
      return listTabs();
    case "read_page":
      return readPage(message.tab_id);
    case "navigate":
      return navigate(message.url, message.tab_id, message.wait_seconds);
    default:
      throw new Error(`unknown action: ${action}`);
  }
}

async function listTabs() {
  const tabs = await chrome.tabs.query({});
  return {
    tabs: tabs.map((tab) => ({
      id: tab.id,
      url: tab.url,
      title: tab.title,
      active: Boolean(tab.active),
      window_id: tab.windowId,
    })),
  };
}

async function activeTabId() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab) {
    throw new Error("no active tab (is a Chrome window open?)");
  }
  return tab.id;
}

async function readPage(tabId) {
  const id = tabId ?? (await activeTabId());
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: id },
    func: () => ({
      url: location.href,
      title: document.title,
      // innerText, not innerHTML/textContent: it approximates what a
      // sighted user actually sees (collapsed whitespace, hidden elements
      // excluded), which is the whole reason to read a live tab instead of
      // a static fetch of the same URL in the first place.
      text: document.body ? document.body.innerText : "",
    }),
  });
  // A very large page would blow the native-messaging 1MB outbound cap and
  // the harness tool's own transcript budget besides -- truncated here,
  // where the real length is still known, rather than silently by whatever
  // reads it last.
  const MAX_CHARS = 20000;
  if (result.text.length > MAX_CHARS) {
    result.text = result.text.slice(0, MAX_CHARS) + `\n...[truncated, ${result.text.length} chars total]`;
  }
  return result;
}

async function navigate(url, tabId, waitSeconds) {
  if (!url) {
    throw new Error("navigate needs a url");
  }
  const timeoutMs = (waitSeconds || 20) * 1000;
  const id = tabId ?? (await activeTabIdOrNewTab());
  const loaded = waitForLoad(id, timeoutMs);
  await chrome.tabs.update(id, { url });
  await loaded;
  const tab = await chrome.tabs.get(id);
  return { tab_id: tab.id, url: tab.url, title: tab.title };
}

async function activeTabIdOrNewTab() {
  const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (tabs[0]) {
    return tabs[0].id;
  }
  const created = await chrome.tabs.create({});
  return created.id;
}

function waitForLoad(tabId, timeoutMs) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) {
        return;
      }
      settled = true;
      chrome.tabs.onUpdated.removeListener(onUpdated);
      resolve();
    };
    const onUpdated = (id, changeInfo) => {
      if (id === tabId && changeInfo.status === "complete") {
        finish();
      }
    };
    chrome.tabs.onUpdated.addListener(onUpdated);
    // Best-effort: a page that never reaches "complete" (a long-polling
    // dashboard, say) must not hang the request forever -- the caller
    // still gets the tab's state as it is once this fires.
    setTimeout(finish, timeoutMs);
  });
}
