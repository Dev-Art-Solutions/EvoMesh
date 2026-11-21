// Page behaviour a modern app would have, then every scenario
// tests/test_browser_page_agent.py checks. Results land in <pre id=results>,
// which headless Chrome's --dump-dom prints.
(() => {
  const events = [];
  const q = document.getElementById("q");
  q.addEventListener("input", () => events.push(`input:${q.value}`));

  // A single-page app: submit is handled in JS, the route changes with
  // pushState, and results render a moment later.
  document.getElementById("search").addEventListener("submit", (event) => {
    event.preventDefault();
    history.pushState({}, "", `?q=${encodeURIComponent(q.value)}`);
    document.getElementById("route").textContent = "results";
    setTimeout(() => {
      const list = document.createElement("ul");
      list.id = "results-list";
      for (const title of ["Python tutorial one", "Python tutorial two", "Python tutorial three"]) {
        const item = document.createElement("li");
        const link = document.createElement("a");
        link.href = `https://example.org/${title.replace(/\s+/g, "-")}`;
        link.textContent = title;
        item.appendChild(link);
        list.appendChild(item);
      }
      document.querySelector("main").appendChild(list);
    }, 400);
  });
  document.getElementById("login").addEventListener("submit", (event) => event.preventDefault());

  setTimeout(() => {
    const late = document.createElement("button");
    late.textContent = "Load more";
    document.getElementById("later").appendChild(late);
  }, 600);

  const feed = document.getElementById("feed");
  const addRows = (n) => {
    for (let i = 0; i < n; i += 1) {
      const row = document.createElement("div");
      row.className = "row";
      row.textContent = `row ${feed.children.length + 1}`;
      feed.appendChild(row);
    }
  };
  addRows(5);
  feed.addEventListener("scroll", () => {
    events.push(`scroll:${feed.scrollTop}/${feed.scrollHeight}`);
    if (feed.scrollTop + feed.clientHeight >= feed.scrollHeight - 5 && feed.children.length < 15) {
      setTimeout(() => addRows(5), 200);
    }
  });

  document.getElementById("open-modal").addEventListener("click", () => {
    document.getElementById("modal").classList.add("open");
  });
  document.getElementById("close").addEventListener("click", () => {
    document.getElementById("modal").classList.remove("open");
  });

  const page = globalThis.__evomeshPage;
  const results = {};
  const run = async (name, request) => {
    results[name] = await page.run(request);
    return results[name];
  };

  (async () => {
    const snap = await run("snapshot", { action: "snapshot" });
    const searchRef = snap.elements.items.find((e) => e.name === "Search").ref;
    await run("structure", { action: "get_page_structure" });
    await run("metadata", { action: "get_page_metadata" });
    await run("links", { action: "get_links", same_origin_only: false });
    await run("ambiguous", { action: "click", target: { role: "button", name: "Add" }, timeout_seconds: 0 });
    await run("nth", { action: "click", target: { role: "button", name: "Add", nth: 2 } });
    await run("disabled", { action: "click", target: { role: "button", name: "Sold out" } });
    await run("hidden", { action: "click", target: { role: "button", name: "Secret action" }, timeout_seconds: 0 });
    await run("missing", { action: "click", target: { role: "button", name: "Nope" }, timeout_seconds: 0 });
    await run("no_target", { action: "click", target: {} });
    await run("bad_css", { action: "click", target: { css: "[[" } });
    await run("by_text", { action: "describe_target", target: { text: "Read more" } });
    await run("by_label", { action: "describe_target", target: { label: "Size" } });
    await run("by_placeholder", { action: "describe_target", target: { placeholder: "type a product" } });
    await run("password_attrs", { action: "get_element_attributes", target: { css: "input[type=password]" } });
    await run("select", { action: "select_option", target: { label: "Size" }, label: "Medium" });
    await run("select_disabled", { action: "select_option", target: { label: "Size" }, value: "l" });
    await run("check", { action: "check", target: { role: "checkbox", name: "Gift wrap" } });
    await run("uncheck", { action: "uncheck", target: { role: "checkbox", name: "Gift wrap" } });
    await run("wait_late", { action: "wait_for_element", target: { role: "button", name: "Load more" }, timeout_seconds: 5 });
    await run("modal_open", { action: "click", target: { css: "#open-modal" } });
    await run("obscured", { action: "click", target: { css: "#under" } });
    await run("modal_close", { action: "click", target: { role: "button", name: "Close promo" } });
    await run("fill", { action: "fill", target: { ref: searchRef }, value: "python" });
    await run("type", { action: "type_text", target: { ref: searchRef }, text: " tutorials" });
    await run("enter", { action: "press_key", target: { ref: searchRef }, key: "Enter" });
    await run("wait_results", { action: "wait_for_element", target: { css: "#results-list a" , nth: 3 }, timeout_seconds: 5 });
    await run("results_links", { action: "get_links", contains: "python tutorial" });
    await run("scroll", { action: "scroll", target: { css: "#feed" }, direction: "bottom", timeout_seconds: 2 });
    await run("file_input_refused", { action: "fill", target: { css: "#receipt" }, value: "C:/secret.txt" });
    await run("checkbox_not_text", { action: "fill", target: { css: "#gift" }, value: "x" });
    await run("bad_key", { action: "press_key", key: "F13" });
    await run("unknown", { action: "evaluate", code: "alert(1)" });
    // A ref to an element that has since been removed is stale, not retargeted.
    const doomed = await run("doomed_ref", { action: "describe_target", target: { text: "Lamp" } });
    document.querySelector(".card").remove();
    await run("stale", { action: "click", target: { ref: doomed.ref } });
    await run("other_epoch", { action: "click", target: { ref: "zzzzzz:e1" } });
    results.events = events;
    results.url = location.href;
    const pre = document.createElement("pre");
    pre.id = "results";
    pre.textContent = JSON.stringify(results);
    document.body.appendChild(pre);
  })();
})();
