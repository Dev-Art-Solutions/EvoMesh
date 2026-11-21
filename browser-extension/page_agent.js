/**
 * EvoMesh page agent -- runs inside a page, injected by background.js with
 * chrome.scripting.executeScript({files: ["page_agent.js"]}).
 *
 * It is the whole of what an agent can do to a page: a closed set of named
 * operations over deterministic element targeting. There is deliberately no
 * "evaluate this JavaScript" entry point -- every action an agent can take is
 * one listed in OPERATIONS below, with validated arguments.
 *
 * Targeting is DOM inspection, not guessing: a target is a previous
 * snapshot's ref, a CSS selector, or semantic properties (role, accessible
 * name, label, placeholder, visible text). A target that matches nothing, or
 * more than one thing without an `nth`, is an error -- never a fallback to
 * "something close".
 *
 * Refs are "<epoch>:e<n>". The epoch is fixed per document, so a ref taken
 * before a navigation is reported stale instead of silently matching an
 * element on the next page; a ref whose element left the DOM is stale too.
 *
 * Values of password, hidden and payment-looking fields are never returned.
 *
 * The same file is loaded by tests/fixtures/browser/*.html, so the targeting
 * and observation logic is tested in a real Chrome without the extension.
 */
(() => {
  const VERSION = "2.0.0";
  if (globalThis.__evomeshPage && globalThis.__evomeshPage.version === VERSION) {
    return;
  }

  const MAX_WAIT_MS = 30000;
  const state = {
    epoch: Math.random().toString(36).slice(2, 8),
    ids: new WeakMap(),
    refs: new Map(),
    next: 1,
  };

  class PageError extends Error {
    constructor(code, message, details) {
      super(message);
      this.code = code;
      this.details = details || {};
    }
  }

  // -- element facts -------------------------------------------------------

  const IMPLICIT_ROLES = {
    A: (el) => (el.hasAttribute("href") ? "link" : ""),
    BUTTON: () => "button",
    SELECT: (el) => (el.multiple || el.size > 1 ? "listbox" : "combobox"),
    TEXTAREA: () => "textbox",
    OPTION: () => "option",
    IMG: (el) => (el.getAttribute("alt") === "" ? "presentation" : "img"),
    H1: () => "heading", H2: () => "heading", H3: () => "heading",
    H4: () => "heading", H5: () => "heading", H6: () => "heading",
    NAV: () => "navigation", MAIN: () => "main", FORM: () => "form",
    HEADER: () => "banner", FOOTER: () => "contentinfo", ASIDE: () => "complementary",
    DIALOG: () => "dialog", SUMMARY: () => "button", UL: () => "list", OL: () => "list",
    LI: () => "listitem", TABLE: () => "table",
    INPUT: (el) => {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (["button", "submit", "reset", "image"].includes(type)) return "button";
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "range") return "slider";
      if (type === "search") return "searchbox";
      if (type === "hidden") return "";
      return "textbox";
    },
  };

  function roleOf(el) {
    const explicit = (el.getAttribute("role") || "").trim().split(/\s+/)[0];
    if (explicit) return explicit.toLowerCase();
    const implicit = IMPLICIT_ROLES[el.tagName];
    if (implicit) return implicit(el);
    if (el.isContentEditable && el.getAttribute("contenteditable") !== null) return "textbox";
    return "";
  }

  function clean(text, limit = 160) {
    const flat = String(text || "").replace(/\s+/g, " ").trim();
    return flat.length > limit ? `${flat.slice(0, limit - 1)}…` : flat;
  }

  function labelText(el) {
    const parts = [];
    if (el.labels) {
      for (const label of el.labels) parts.push(label.innerText || label.textContent || "");
    }
    return clean(parts.join(" "));
  }

  function accessibleName(el) {
    const labelledby = el.getAttribute("aria-labelledby");
    if (labelledby) {
      const text = labelledby
        .split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map((node) => node.innerText || node.textContent || "")
        .join(" ");
      if (clean(text)) return clean(text);
    }
    const aria = el.getAttribute("aria-label");
    if (aria && clean(aria)) return clean(aria);
    const tag = el.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
      const type = (el.getAttribute("type") || "").toLowerCase();
      if (["button", "submit", "reset"].includes(type)) {
        return clean(el.value || el.getAttribute("value") || (type === "submit" ? "Submit" : ""));
      }
      if (type === "image") return clean(el.getAttribute("alt") || "");
      const label = labelText(el);
      if (label) return label;
      return clean(el.getAttribute("title") || el.getAttribute("placeholder") || "");
    }
    if (tag === "IMG") return clean(el.getAttribute("alt") || el.getAttribute("title") || "");
    const text = clean(el.innerText || el.textContent || "");
    if (text) return text;
    return clean(el.getAttribute("title") || "");
  }

  function isVisible(el) {
    if (!el.isConnected) return false;
    if (typeof el.checkVisibility === "function") {
      if (!el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
    } else {
      const style = getComputedStyle(el);
      if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") {
        return false;
      }
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function isDisabled(el) {
    if (el.disabled) return true;
    if (el.getAttribute("aria-disabled") === "true") return true;
    const fieldset = el.closest("fieldset[disabled]");
    return Boolean(fieldset && !fieldset.querySelector("legend")?.contains(el));
  }

  const SECRET_NAME = /pass|token|secret|csrf|xsrf|otp|cvv|cvc|card.?num|ccnum|security.?code|pin\b/i;

  function isSecretField(el) {
    if (!(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement)) return false;
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (type === "password" || type === "hidden") return true;
    const autocomplete = (el.getAttribute("autocomplete") || "").toLowerCase();
    if (autocomplete.startsWith("cc-") || autocomplete.includes("one-time-code")
        || autocomplete.includes("password")) {
      return true;
    }
    return SECRET_NAME.test(`${el.name || ""} ${el.id || ""}`);
  }

  const INTERACTIVE = [
    "a[href]", "button", "input:not([type=hidden])", "select", "textarea", "summary",
    "[role=button]", "[role=link]", "[role=checkbox]", "[role=radio]", "[role=tab]",
    "[role=menuitem]", "[role=option]", "[role=switch]", "[role=combobox]", "[role=textbox]",
    "[role=searchbox]", "[contenteditable='']", "[contenteditable=true]",
    "[tabindex]:not([tabindex='-1'])",
  ].join(",");

  function refFor(el) {
    let id = state.ids.get(el);
    if (!id) {
      id = `${state.epoch}:e${state.next++}`;
      state.ids.set(el, id);
      state.refs.set(id, new WeakRef(el));
    }
    return id;
  }

  function describe(el, { detail = false } = {}) {
    const info = { ref: refFor(el), role: roleOf(el) || el.tagName.toLowerCase() };
    const name = accessibleName(el);
    if (name) info.name = name;
    const tag = el.tagName.toLowerCase();
    if (detail || !["a", "button"].includes(tag)) info.tag = tag;
    if (el instanceof HTMLInputElement) info.type = (el.getAttribute("type") || "text").toLowerCase();
    if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement
        || el instanceof HTMLSelectElement) {
      if (isSecretField(el)) {
        info.value_redacted = true;
      } else if (el instanceof HTMLSelectElement) {
        const selected = el.selectedOptions[0];
        if (selected) info.value = clean(selected.label || selected.value, 80);
      } else if (!["checkbox", "radio"].includes(info.type) && el.value) {
        info.value = clean(el.value, 80);
      }
      if (el.required) info.required = true;
      const placeholder = el.getAttribute("placeholder");
      if (placeholder) info.placeholder = clean(placeholder, 80);
      const label = labelText(el);
      if (label && label !== info.name) info.label = label;
    }
    if ("checked" in el && (info.type === "checkbox" || info.type === "radio")) {
      info.checked = Boolean(el.checked);
    } else if (el.getAttribute("aria-checked")) {
      info.checked = el.getAttribute("aria-checked") === "true";
    }
    if (isDisabled(el)) info.disabled = true;
    if (el instanceof HTMLAnchorElement && el.href) info.href = el.href;
    if (detail) {
      info.visible = isVisible(el);
      const rect = el.getBoundingClientRect();
      info.in_viewport = rect.bottom > 0 && rect.right > 0
        && rect.top < innerHeight && rect.left < innerWidth;
    }
    return info;
  }

  // -- targeting -------------------------------------------------------------

  function matches(actual, wanted, exact) {
    const have = clean(actual, 10000).toLowerCase();
    const want = clean(wanted, 10000).toLowerCase();
    if (!want) return false;
    return exact ? have === want : have.includes(want);
  }

  function validateTarget(target) {
    if (!target || typeof target !== "object" || Array.isArray(target)) {
      throw new PageError("invalid_request", "target must be an object");
    }
    const keys = ["ref", "css", "role", "name", "label", "placeholder", "text"];
    if (!keys.some((key) => target[key] !== undefined && target[key] !== "")) {
      throw new PageError(
        "invalid_request",
        `target needs one of: ${keys.join(", ")}`,
      );
    }
    if (target.nth !== undefined && (!Number.isInteger(target.nth) || target.nth < 1)) {
      throw new PageError("invalid_request", "target.nth is 1-based and must be a positive integer");
    }
  }

  function byRef(ref) {
    const [epoch] = String(ref).split(":");
    if (epoch !== state.epoch) {
      throw new PageError(
        "stale_ref",
        `ref ${ref} is from an earlier page; take a new snapshot`,
      );
    }
    const weak = state.refs.get(ref);
    const el = weak && weak.deref();
    if (!el || !el.isConnected) {
      throw new PageError("stale_ref", `ref ${ref} is no longer in the page; take a new snapshot`);
    }
    return el;
  }

  function candidates(target) {
    let pool;
    if (target.css) {
      try {
        pool = Array.from(document.querySelectorAll(target.css));
      } catch (err) {
        throw new PageError("invalid_request", `invalid CSS selector: ${target.css}`);
      }
    } else {
      pool = Array.from(document.body ? document.body.querySelectorAll("*") : []);
    }
    const exact = Boolean(target.exact);
    let found = pool.filter((el) => {
      if (target.role && roleOf(el) !== String(target.role).toLowerCase()) return false;
      if (target.name !== undefined && !matches(accessibleName(el), target.name, exact)) {
        return false;
      }
      if (target.label !== undefined && !matches(labelText(el), target.label, exact)) return false;
      if (target.placeholder !== undefined
          && !matches(el.getAttribute("placeholder") || "", target.placeholder, exact)) {
        return false;
      }
      if (target.text !== undefined
          && !matches(el.innerText || el.textContent || "", target.text, exact)) {
        return false;
      }
      return true;
    });
    if (target.text !== undefined && !target.css && !target.role) {
      // Text matches every ancestor of the text too: keep the innermost, then
      // the interactive element it sits in, so "Search" means the button and
      // not <body>.
      found = found.filter((el) => !found.some((other) => other !== el && el.contains(other)));
      found = found.map((el) => el.closest(INTERACTIVE) || el);
      found = Array.from(new Set(found));
    }
    return found;
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function waitMs(request, fallback) {
    const seconds = request.timeout_seconds;
    if (seconds === undefined || seconds === null) return fallback;
    const value = Number(seconds);
    if (!Number.isFinite(value) || value < 0) {
      throw new PageError("invalid_request", "timeout_seconds must be a non-negative number");
    }
    return Math.min(value * 1000, MAX_WAIT_MS);
  }

  async function resolve(target, request, fallbackWait = 3000) {
    validateTarget(target);
    if (target.ref) return byRef(target.ref);
    const deadline = Date.now() + waitMs(request, fallbackWait);
    let hidden = 0;
    for (;;) {
      const all = candidates(target);
      const wantVisible = target.visible !== false;
      const usable = wantVisible ? all.filter(isVisible) : all;
      hidden = all.length - usable.length;
      if (usable.length === 1 && !target.nth) return usable[0];
      if (target.nth && usable.length >= target.nth) return usable[target.nth - 1];
      if (usable.length > 1 && !target.nth) {
        throw new PageError(
          "ambiguous_target",
          `${usable.length} elements match; add "nth", narrow the target, or use a ref`,
          { matches: usable.slice(0, 5).map((el) => describe(el)), count: usable.length },
        );
      }
      if (Date.now() >= deadline) break;
      await sleep(100);
    }
    if (hidden > 0) {
      throw new PageError(
        "element_hidden",
        `${hidden} element(s) match but none is visible`,
        { hidden },
      );
    }
    throw new PageError("element_not_found", "nothing in the page matches the target");
  }

  function requireUsable(el) {
    if (!el.isConnected) throw new PageError("stale_ref", "the element left the page");
    if (!isVisible(el)) throw new PageError("element_hidden", "the element is not visible", describe(el));
    if (isDisabled(el)) throw new PageError("element_disabled", "the element is disabled", describe(el));
  }

  // -- observation -------------------------------------------------------------

  function pageText() {
    return document.body ? document.body.innerText || "" : "";
  }

  function window_(text, offset, limit) {
    const start = Math.max(0, Number(offset) || 0);
    const slice = text.slice(start, start + limit);
    return {
      content: slice,
      offset: start,
      total: text.length,
      truncated: start + slice.length < text.length,
      next_offset: start + slice.length < text.length ? start + slice.length : null,
    };
  }

  function safeOrigin(url) {
    try {
      const parsed = new URL(url, location.href);
      return `${parsed.origin}${parsed.pathname}`;
    } catch (err) {
      return "";
    }
  }

  function forms() {
    return Array.from(document.forms).filter(isVisible).slice(0, 10).map((form) => ({
      ref: refFor(form),
      name: clean(form.getAttribute("aria-label") || form.getAttribute("name") || form.id || "", 80),
      action: safeOrigin(form.getAttribute("action") || location.href),
      method: (form.getAttribute("method") || "get").toLowerCase(),
      fields: Array.from(form.elements)
        .filter((el) => el.tagName !== "FIELDSET" && el.type !== "hidden" && isVisible(el))
        .slice(0, 25)
        .map((el) => {
          const info = describe(el);
          if (!info.value_redacted && "value" in el) info.has_value = Boolean(el.value);
          return info;
        }),
    }));
  }

  function alerts() {
    const found = Array.from(document.querySelectorAll(
      "[role=alert], [role=alertdialog], [aria-live=assertive], [aria-invalid=true]",
    ));
    const messages = [];
    for (const el of found) {
      if (!isVisible(el)) continue;
      const text = el.getAttribute("aria-invalid") === "true"
        ? `invalid: ${accessibleName(el)}`
        : clean(el.innerText || el.textContent || "", 200);
      if (text && !messages.includes(text)) messages.push(text);
    }
    return messages.slice(0, 10);
  }

  function dialogs() {
    return Array.from(document.querySelectorAll("dialog[open], [role=dialog], [aria-modal=true]"))
      .filter(isVisible)
      .slice(0, 5)
      .map((el) => ({ ref: refFor(el), name: accessibleName(el).slice(0, 80) }));
  }

  const HUMAN_CHECK = [
    /verify (that )?you are (a )?human/i, /are you a robot/i, /checking your browser/i,
    /unusual traffic/i, /complete the security check/i, /press (and|&) hold/i,
  ];

  function humanRequired() {
    const frames = Array.from(document.querySelectorAll("iframe")).map((f) => f.src || "");
    if (frames.some((src) => /recaptcha|hcaptcha|challenges\.cloudflare\.com|turnstile/i.test(src))) {
      return "a CAPTCHA is on the page";
    }
    const sample = `${document.title} ${pageText().slice(0, 3000)}`;
    const hit = HUMAN_CHECK.find((pattern) => pattern.test(sample));
    return hit ? "the page asks for a human verification" : "";
  }

  function interactive(offset, limit) {
    const all = Array.from(document.querySelectorAll(INTERACTIVE)).filter(isVisible);
    const start = Math.max(0, Number(offset) || 0);
    return {
      items: all.slice(start, start + limit).map((el) => describe(el)),
      offset: start,
      total: all.length,
      next_offset: start + limit < all.length ? start + limit : null,
    };
  }

  function links(request) {
    const limit = clampInt(request.limit, 15, 1, 100);
    const offset = clampInt(request.offset, 0, 0, 100000);
    const needle = request.contains ? String(request.contains).toLowerCase() : "";
    const seen = new Set();
    const all = [];
    for (const anchor of document.querySelectorAll("a[href]")) {
      if (!isVisible(anchor)) continue;
      const href = anchor.href;
      if (!/^https?:/i.test(href) || seen.has(href)) continue;
      const text = accessibleName(anchor);
      if (needle && !`${text} ${href}`.toLowerCase().includes(needle)) continue;
      if (request.same_origin_only && new URL(href).origin !== location.origin) continue;
      seen.add(href);
      all.push({ ref: refFor(anchor), text: clean(text, 120), href });
    }
    return {
      items: all.slice(offset, offset + limit),
      offset,
      total: all.length,
      next_offset: offset + limit < all.length ? offset + limit : null,
    };
  }

  function clampInt(value, fallback, low, high) {
    if (value === undefined || value === null) return fallback;
    const number = Number(value);
    if (!Number.isInteger(number) || number < low || number > high) {
      throw new PageError("invalid_request", `expected an integer between ${low} and ${high}`);
    }
    return number;
  }

  function basics() {
    return { url: location.href, title: document.title, ready_state: document.readyState };
  }

  function snapshot(request) {
    const textChars = clampInt(request.max_text_chars, 1500, 0, 20000);
    const elements = clampInt(request.max_elements, 40, 0, 200);
    const result = {
      ...basics(),
      text: window_(pageText(), request.text_offset, textChars),
      elements: interactive(request.element_offset, elements),
      links: links({ limit: clampInt(request.max_links, 15, 0, 100) }),
      forms: forms(),
      alerts: alerts(),
      dialogs: dialogs(),
      scroll: {
        y: Math.round(scrollY),
        height: document.documentElement.scrollHeight,
        viewport: innerHeight,
      },
    };
    const human = humanRequired();
    if (human) result.human_required = human;
    return result;
  }

  function structure() {
    const headings = Array.from(document.querySelectorAll("h1,h2,h3,h4,h5,h6,[role=heading]"))
      .filter(isVisible)
      .slice(0, 60)
      .map((el) => ({
        ref: refFor(el),
        level: Number(el.getAttribute("aria-level") || el.tagName.slice(1)) || 2,
        text: accessibleName(el).slice(0, 120),
      }));
    const landmarks = Array.from(document.querySelectorAll(
      "header,nav,main,aside,footer,[role=banner],[role=navigation],[role=main],"
      + "[role=complementary],[role=contentinfo],[role=search],[role=region][aria-label]",
    ))
      .filter(isVisible)
      .slice(0, 30)
      .map((el) => ({ ref: refFor(el), role: roleOf(el), name: (el.getAttribute("aria-label") || "").slice(0, 80) }));
    return {
      ...basics(),
      headings,
      landmarks,
      forms: forms(),
      dialogs: dialogs(),
      iframes: document.querySelectorAll("iframe").length,
    };
  }

  function elements(request) {
    const limit = clampInt(request.limit, 30, 1, 200);
    const offset = clampInt(request.offset, 0, 0, 100000);
    let all;
    if (request.target) {
      validateTarget(request.target);
      all = request.target.ref ? [byRef(request.target.ref)] : candidates(request.target);
    } else {
      all = Array.from(document.querySelectorAll(INTERACTIVE));
    }
    if (!request.include_hidden) all = all.filter(isVisible);
    return {
      items: all.slice(offset, offset + limit).map((el) => describe(el, { detail: true })),
      offset,
      total: all.length,
      next_offset: offset + limit < all.length ? offset + limit : null,
    };
  }

  const SAFE_ATTRIBUTES = /^(id|class|name|type|role|href|src|alt|title|placeholder|for|action|method|lang|dir|target|rel|tabindex|data-testid|data-test|data-qa|aria-[a-z-]+)$/;

  async function attributes(request) {
    const el = await resolve(request.target, request, 0);
    const attrs = {};
    for (const attr of el.attributes) {
      if (SAFE_ATTRIBUTES.test(attr.name)) attrs[attr.name] = clean(attr.value, 300);
    }
    const rect = el.getBoundingClientRect();
    return {
      element: describe(el, { detail: true }),
      attributes: attrs,
      rect: { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height) },
    };
  }

  function metadata() {
    const meta = (selector) => document.querySelector(selector)?.getAttribute("content") || "";
    const og = {};
    for (const el of document.querySelectorAll("meta[property^='og:']")) {
      og[el.getAttribute("property")] = clean(el.getAttribute("content"), 300);
    }
    return {
      ...basics(),
      lang: document.documentElement.lang || "",
      description: clean(meta("meta[name=description]"), 300),
      canonical: document.querySelector("link[rel=canonical]")?.href || "",
      open_graph: og,
      viewport: { width: innerWidth, height: innerHeight, device_pixel_ratio: devicePixelRatio },
      counts: {
        links: document.querySelectorAll("a[href]").length,
        forms: document.forms.length,
        images: document.images.length,
        iframes: document.querySelectorAll("iframe").length,
      },
    };
  }

  // -- interaction ---------------------------------------------------------

  function center(el) {
    el.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
    const rect = el.getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  }

  function ensureNotObscured(el, point) {
    const hit = document.elementFromPoint(point.x, point.y);
    if (hit && hit !== el && !el.contains(hit) && !hit.contains(el)) {
      throw new PageError(
        "element_obscured",
        "another element covers it; close the dialog or overlay first",
        { covered_by: describe(hit) },
      );
    }
  }

  function mouse(el, type, point, detail = 1) {
    const init = {
      bubbles: true, cancelable: true, composed: true, view: window,
      clientX: point.x, clientY: point.y, button: 0, detail,
    };
    const Ctor = type.startsWith("pointer") ? PointerEvent : MouseEvent;
    el.dispatchEvent(new Ctor(type, { ...init, pointerType: "mouse", isPrimary: true }));
  }

  async function click(request, double = false) {
    const el = await resolve(request.target, request);
    requireUsable(el);
    const point = center(el);
    ensureNotObscured(el, point);
    const described = describe(el);
    const before = location.href;
    for (let round = 1; round <= (double ? 2 : 1); round += 1) {
      mouse(el, "pointerdown", point, round);
      mouse(el, "mousedown", point, round);
      if (typeof el.focus === "function") el.focus({ preventScroll: true });
      mouse(el, "pointerup", point, round);
      mouse(el, "mouseup", point, round);
      el.click();
    }
    if (double) mouse(el, "dblclick", point, 2);
    return { element: described, url_before: before };
  }

  function nativeSetter(el) {
    const proto = el instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : el instanceof HTMLSelectElement
        ? HTMLSelectElement.prototype
        : HTMLInputElement.prototype;
    return Object.getOwnPropertyDescriptor(proto, "value").set;
  }

  function editable(el) {
    if (el instanceof HTMLInputElement) {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (type === "file") {
        throw new PageError("not_supported", "file inputs cannot be filled; uploading files is not supported");
      }
      if (["checkbox", "radio", "button", "submit", "reset", "image", "range", "color"].includes(type)) {
        throw new PageError("invalid_request", `a ${type} input is not a text field; use check/uncheck/click`);
      }
      return "input";
    }
    if (el instanceof HTMLTextAreaElement) return "input";
    if (el.isContentEditable) return "contenteditable";
    throw new PageError("invalid_request", "the target is not a text field");
  }

  function setValue(el, value) {
    nativeSetter(el).call(el, value);
    el.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, inputType: "insertText", data: value }));
  }

  async function fill(request) {
    const el = await resolve(request.target, request);
    requireUsable(el);
    const kind = editable(el);
    const value = request.value === undefined ? "" : String(request.value);
    el.focus({ preventScroll: false });
    if (kind === "contenteditable") {
      el.textContent = value;
      el.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, inputType: "insertText", data: value }));
    } else {
      setValue(el, value);
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }
    return { element: describe(el) };
  }

  function key(el, type, keyName) {
    const codes = { Enter: "Enter", Tab: "Tab", Escape: "Escape", Backspace: "Backspace", " ": "Space" };
    el.dispatchEvent(new KeyboardEvent(type, {
      key: keyName,
      code: codes[keyName] || (keyName.length === 1 ? `Key${keyName.toUpperCase()}` : keyName),
      bubbles: true, cancelable: true, composed: true,
    }));
  }

  async function typeText(request) {
    const el = await resolve(request.target, request);
    requireUsable(el);
    const kind = editable(el);
    const text = String(request.text ?? request.value ?? "");
    if (text.length > 5000) throw new PageError("invalid_request", "text is limited to 5000 characters");
    el.focus();
    for (const ch of text) {
      key(el, "keydown", ch);
      key(el, "keypress", ch);
      if (kind === "contenteditable") {
        el.textContent += ch;
        el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: ch }));
      } else {
        setValue(el, el.value + ch);
      }
      key(el, "keyup", ch);
    }
    if (kind !== "contenteditable") el.dispatchEvent(new Event("change", { bubbles: true }));
    return { element: describe(el) };
  }

  const KEYS = new Set([
    "Enter", "Tab", "Escape", "Backspace", "Delete", "ArrowUp", "ArrowDown", "ArrowLeft",
    "ArrowRight", "Home", "End", "PageUp", "PageDown", " ",
  ]);

  async function pressKey(request) {
    const keyName = String(request.key || "");
    if (!KEYS.has(keyName) && !/^[a-zA-Z0-9]$/.test(keyName)) {
      throw new PageError("invalid_request", `key must be one of ${Array.from(KEYS).join(", ")} or one letter/digit`);
    }
    const el = request.target ? await resolve(request.target, request) : document.activeElement || document.body;
    if (request.target) {
      requireUsable(el);
      el.focus();
    }
    const before = location.href;
    key(el, "keydown", keyName);
    let submitted = false;
    if (keyName === "Enter" && el.form && el instanceof HTMLInputElement) {
      el.form.requestSubmit();
      submitted = true;
    } else if (keyName === "Enter" && (el instanceof HTMLButtonElement || el instanceof HTMLAnchorElement)) {
      el.click();
    }
    key(el, "keyup", keyName);
    return { element: el === document.body ? null : describe(el), key: keyName, submitted, url_before: before };
  }

  async function scroll(request) {
    const direction = String(request.direction || "down");
    if (!["down", "up", "top", "bottom"].includes(direction)) {
      throw new PageError("invalid_request", "direction is down, up, top or bottom");
    }
    const box = request.target ? await resolve(request.target, request) : null;
    if (box) box.scrollIntoView({ block: "nearest", behavior: "instant" });
    const scroller = box || document.scrollingElement || document.documentElement;
    const pixels = clampInt(request.pixels, Math.round(innerHeight * 0.8), 1, 100000);
    const beforeY = scroller.scrollTop;
    const beforeHeight = scroller.scrollHeight;
    const to = { down: beforeY + pixels, up: beforeY - pixels, top: 0, bottom: scroller.scrollHeight }[direction];
    scroller.scrollTo({ top: to, behavior: "instant" });
    // The browser fires its own scroll event on the next rendering frame --
    // which a background or headless tab may not get for a while. One more,
    // now, is what a real user's scroll produces many of anyway.
    (box || document).dispatchEvent(new Event("scroll", { bubbles: !box }));
    // Infinite scroll loads after the scroll event: wait (bounded) for the
    // page to grow, rather than a fixed delay that is either too short or
    // wasted.
    const deadline = Date.now() + waitMs(request, 1500);
    while (scroller.scrollHeight === beforeHeight && Date.now() < deadline) {
      await sleep(100);
    }
    return {
      scroll_y: Math.round(scroller.scrollTop),
      moved: Math.round(scroller.scrollTop) !== Math.round(beforeY),
      height_before: beforeHeight,
      height_after: scroller.scrollHeight,
      new_content: scroller.scrollHeight > beforeHeight,
      at_bottom: scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2,
    };
  }

  async function selectOption(request) {
    const el = await resolve(request.target, request);
    requireUsable(el);
    if (!(el instanceof HTMLSelectElement)) {
      throw new PageError("invalid_request", "select_option needs a <select> element");
    }
    const options = Array.from(el.options);
    let option;
    if (request.value !== undefined) option = options.find((o) => o.value === String(request.value));
    else if (request.label !== undefined) {
      option = options.find((o) => clean(o.label).toLowerCase() === clean(request.label).toLowerCase());
    } else if (request.index !== undefined) option = options[clampInt(request.index, 0, 0, 10000)];
    else throw new PageError("invalid_request", "select_option needs value, label or index");
    if (!option) {
      throw new PageError("option_not_found", "no such option", {
        options: options.slice(0, 20).map((o) => ({ value: o.value, label: clean(o.label, 60) })),
      });
    }
    if (option.disabled) throw new PageError("element_disabled", "that option is disabled");
    nativeSetter(el).call(el, option.value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { element: describe(el), selected: { value: option.value, label: clean(option.label, 80) } };
  }

  async function setChecked(request, wanted) {
    const el = await resolve(request.target, request);
    requireUsable(el);
    const read = () => ("checked" in el && el instanceof HTMLInputElement
      ? el.checked
      : el.getAttribute("aria-checked") === "true");
    const role = roleOf(el);
    if (!["checkbox", "radio", "switch", "menuitemcheckbox"].includes(role)) {
      throw new PageError("invalid_request", `check/uncheck needs a checkbox, radio or switch, not ${role || el.tagName.toLowerCase()}`);
    }
    if (role === "radio" && !wanted) {
      throw new PageError("invalid_request", "a radio button is unchecked by checking another one");
    }
    if (read() !== wanted) el.click();
    if (read() !== wanted) {
      throw new PageError("action_failed", "the element did not change state", describe(el));
    }
    return { element: describe(el), checked: read() };
  }

  async function waitForElement(request) {
    const wanted = String(request.state || "visible");
    if (!["visible", "hidden", "attached", "detached"].includes(wanted)) {
      throw new PageError("invalid_request", "state is visible, hidden, attached or detached");
    }
    validateTarget(request.target);
    const deadline = Date.now() + waitMs(request, 10000);
    const started = Date.now();
    for (;;) {
      let found = [];
      try {
        found = request.target.ref ? [byRef(request.target.ref)] : candidates(request.target);
      } catch (err) {
        if (err.code !== "stale_ref") throw err;
      }
      const visible = found.filter(isVisible);
      const done = {
        visible: visible.length > 0,
        hidden: visible.length === 0,
        attached: found.length > 0,
        detached: found.length === 0,
      }[wanted];
      if (done) {
        const first = visible[0] || found[0];
        return { state: wanted, waited_ms: Date.now() - started, element: first ? describe(first) : null, count: found.length };
      }
      if (Date.now() >= deadline) {
        throw new PageError("timeout", `the target did not become ${wanted} within ${Math.round((deadline - started) / 1000)}s`);
      }
      await sleep(100);
    }
  }

  async function elementRect(request) {
    const el = await resolve(request.target, request);
    if (!isVisible(el)) throw new PageError("element_hidden", "the element is not visible");
    el.scrollIntoView({ block: "center", behavior: "instant" });
    await sleep(50);
    const rect = el.getBoundingClientRect();
    return {
      element: describe(el),
      rect: {
        x: Math.max(0, rect.left), y: Math.max(0, rect.top),
        width: Math.min(rect.width, innerWidth), height: Math.min(rect.height, innerHeight),
      },
      device_pixel_ratio: devicePixelRatio,
      viewport: { width: innerWidth, height: innerHeight },
    };
  }

  async function describeTarget(request) {
    const el = await resolve(request.target, request);
    const info = describe(el, { detail: true });
    const form = el.form || el.closest("form");
    if (form) {
      info.form = {
        action: safeOrigin(form.getAttribute("action") || location.href),
        method: (form.getAttribute("method") || "get").toLowerCase(),
        has_password: Boolean(form.querySelector("input[type=password]")),
        has_file: Boolean(form.querySelector("input[type=file]")),
        name: accessibleName(form).slice(0, 80),
      };
      const type = (el.getAttribute("type") || "").toLowerCase();
      info.submits = el instanceof HTMLButtonElement
        ? (type || "submit") === "submit"
        : el instanceof HTMLInputElement && ["submit", "image"].includes(type);
    }
    info.secret_field = isSecretField(el);
    info.text = clean(el.innerText || el.textContent || "", 120);
    return info;
  }

  const OPERATIONS = {
    snapshot,
    read_page: (request) => ({
      ...basics(),
      text: window_(pageText(), request.offset, clampInt(request.max_chars, 4000, 1, 20000)),
      human_required: humanRequired() || undefined,
    }),
    get_page_structure: structure,
    get_elements: elements,
    get_element_attributes: attributes,
    get_page_metadata: metadata,
    get_links: (request) => ({ ...basics(), links: links(request) }),
    click: (request) => click(request, false),
    double_click: (request) => click(request, true),
    fill,
    clear: (request) => fill({ ...request, value: "" }),
    type_text: typeText,
    press_key: pressKey,
    scroll,
    select_option: selectOption,
    check: (request) => setChecked(request, true),
    uncheck: (request) => setChecked(request, false),
    wait_for_element: waitForElement,
    element_rect: elementRect,
    describe_target: describeTarget,
    page_state: basics,
  };

  async function run(request) {
    const action = request && request.action;
    const operation = OPERATIONS[action];
    if (!operation) {
      return { success: false, error: { code: "invalid_request", message: `unknown page action: ${action}` } };
    }
    try {
      const result = await operation(request);
      return { success: true, action, ...result };
    } catch (err) {
      if (err instanceof PageError) {
        return { success: false, action, error: { code: err.code, message: err.message, ...err.details } };
      }
      return { success: false, action, error: { code: "page_error", message: String((err && err.message) || err) } };
    }
  }

  globalThis.__evomeshPage = { version: VERSION, run, actions: Object.keys(OPERATIONS) };
})();
