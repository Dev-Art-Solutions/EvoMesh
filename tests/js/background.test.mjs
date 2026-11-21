// background.js against a mocked chrome.* API, run by
// tests/test_browser_extension.py with `node`. Prints one JSON line of
// results; the Python test asserts on it.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const source = readFileSync(
  fileURLToPath(new URL("../../browser-extension/background.js", import.meta.url)),
  "utf-8",
);

function listeners() {
  const set = new Set();
  return {
    addListener: (fn) => set.add(fn),
    removeListener: (fn) => set.delete(fn),
    fire: (...args) => [...set].forEach((fn) => fn(...args)),
    get size() { return set.size; },
  };
}

const sent = [];
const portMessage = listeners();
const port = {
  onMessage: portMessage,
  onDisconnect: listeners(),
  postMessage: (message) => sent.push(message),
};
const tabs = new Map([
  [1, { id: 1, url: "https://fixture.invalid/", title: "Home", active: true, status: "complete", windowId: 9 }],
  [2, { id: 2, url: "https://fixture.invalid/other", title: "Other", active: false, status: "complete", windowId: 9 }],
]);
let nextTab = 3;
const session = {};
const onUpdated = listeners();
const onError = listeners();
const pageResults = [];

function loadLater(id, url, fail) {
  setTimeout(() => {
    const tab = tabs.get(id);
    tab.status = "loading";
    onUpdated.fire(id, { status: "loading", url });
    setTimeout(() => {
      if (fail) {
        onError.fire({ tabId: id, frameId: 0, error: "net::ERR_NAME_NOT_RESOLVED" });
        return;
      }
      tab.url = url;
      tab.title = `Title of ${url}`;
      tab.status = "complete";
      onUpdated.fire(id, { status: "complete" });
    }, 20);
  }, 10);
}

const chrome = {
  runtime: {
    id: "fgefcelipaiabacacgjcdkgachkdchcm",
    lastError: null,
    connectNative: () => port,
    getManifest: () => ({ version: "2.0.0" }),
    onStartup: listeners(),
    onInstalled: listeners(),
  },
  alarms: { create: () => {}, onAlarm: listeners() },
  storage: {
    session: {
      get: async (key) => ({ [key]: session[key] }),
      set: async (values) => Object.assign(session, values),
    },
  },
  webNavigation: { onErrorOccurred: onError },
  windows: { update: async () => ({}) },
  tabs: {
    onUpdated,
    query: async (q) => [...tabs.values()].filter((t) => (q.active ? t.active : true)),
    get: async (id) => {
      if (!tabs.has(id)) throw new Error(`No tab with id: ${id}`);
      return { ...tabs.get(id) };
    },
    create: async ({ url }) => {
      const tab = { id: nextTab++, url: "about:blank", title: "", active: true, status: "complete", windowId: 9 };
      tabs.set(tab.id, tab);
      if (url && url !== "about:blank") loadLater(tab.id, url, false);
      return { ...tab };
    },
    remove: async (id) => tabs.delete(id),
    update: async (id, props) => {
      if (props.url) loadLater(id, props.url, props.url.includes("does-not-resolve"));
      if (props.active) for (const t of tabs.values()) t.active = t.id === id;
      return { ...tabs.get(id) };
    },
    goBack: async (id) => loadLater(id, "https://fixture.invalid/previous", false),
    goForward: async () => {},
    reload: async (id) => loadLater(id, tabs.get(id).url, false),
    captureVisibleTab: async () => `data:image/jpeg;base64,${"A".repeat(1_300_000)}`,
  },
  scripting: {
    executeScript: async ({ files, func, args }) => {
      if (files) return [{ result: undefined }];
      const request = args[0];
      pageResults.push(request.action);
      if (request.action === "get_page_metadata") {
        return [{ result: { success: true, viewport: { width: 800, height: 600, device_pixel_ratio: 1 } } }];
      }
      return [{ result: { success: true, action: request.action, echoed: request.target || null } }];
    },
  },
};

// A capture the size of the live one (1510x1570 CSS px at DPR 1.25).
const drawn = [];
const createImageBitmap = async () => ({ width: 1888, height: 1963 });
class OffscreenCanvas {
  constructor(width, height) {
    this.width = width;
    this.height = height;
  }
  getContext() {
    return { drawImage: (...args) => drawn.push({ canvas: [this.width, this.height], args: args.slice(1) }) };
  }
  async convertToBlob() {
    return { arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer };
  }
}

vm.runInNewContext(source, { chrome, console, setTimeout, clearTimeout, fetch, URL, Promise, Date, JSON, Math, Number, String, Array, Set, Map, Object, Error, Uint8Array, btoa, createImageBitmap, OffscreenCanvas });

let counter = 0;
async function ask(request) {
  const id = `r${++counter}`;
  const before = sent.length;
  portMessage.fire({ ...request, id });
  for (let i = 0; i < 400; i += 1) {
    const mine = sent.slice(before).filter((m) => m.id === id);
    if (mine.length && (!("chunk_count" in mine[0]) || mine.length === mine[0].chunk_count)) {
      if ("chunk_count" in mine[0]) {
        return { chunks: mine.length, ...JSON.parse(mine.map((m) => m.part).join("")) };
      }
      return mine[0];
    }
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  return { timeout: true };
}

const results = {};
results.ping = await ask({ action: "ping" });
results.list = await ask({ action: "list_tabs" });
results.missing_tab = await ask({ action: "read_page", tab_id: 77 });
results.new_tab = await ask({ action: "new_tab", url: "https://fixture.invalid/new" });
results.close_not_own = await ask({ action: "close_tab", tab_id: 2, only_own: true });
results.close_own = await ask({ action: "close_tab", tab_id: results.new_tab.tab.tab_id, only_own: true });
results.navigate = await ask({ action: "navigate", tab_id: 1, url: "https://fixture.invalid/search" });
results.navigate_fail = await ask({ action: "navigate", tab_id: 1, url: "https://does-not-resolve.invalid/" });
results.back = await ask({ action: "back", tab_id: 1 });
results.page = await ask({ action: "snapshot", tab_id: 1 });
results.click = await ask({ action: "click", tab_id: 1, target: { ref: "x:e1" }, wait_after_ms: 50 });
results.inactive_shot = await ask({ action: "screenshot", tab_id: 2 });
results.shot = await ask({ action: "screenshot", tab_id: 1 });
results.drawn_full = drawn.length;
results.small_shot = await ask({ action: "screenshot", tab_id: 1, max_side: 1024 });
results.drawn = drawn;
results.unknown = await ask({ action: "evaluate", code: "1" });
results.wait_nav = await ask({ action: "wait_for_navigation", tab_id: 1, url_contains: "never", timeout_seconds: 0.2 });
results.listeners_left = { updated: onUpdated.size, errors: onError.size };
results.max_message = Math.max(...sent.map((m) => JSON.stringify(m).length));
results.page_actions = pageResults;
if (results.shot.data_url) results.shot.data_url = results.shot.data_url.length;
console.log(JSON.stringify(results));
