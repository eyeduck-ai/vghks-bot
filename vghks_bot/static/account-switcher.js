"use strict";

// Account presentation only. Authentication and workspace ownership stay in bot.js.
window.AccountSwitcher = (() => {
  const get = id => document.getElementById(id);
  const make = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  let actions, data, current = "", pending = "", pendingMessage = "", opened = false, native = false;
  const panel = () => get("accountPanel"), trigger = () => get("sidebarAccount");
  function nickname(item) {
    const label = (item?.label || "").trim(), username = (item?.username || "").trim();
    return label.toLowerCase() === username.toLowerCase() ? "" : label;
  }
  const identity = item => [item?.campus, item?.username, nickname(item)].filter(Boolean).join(" · ");
  function connectionMessage(text) {
    const message = (text || "").trim().replace(/^已登入\s*·\s*/, "");
    return ["已登入", "已連線"].includes(message) ? "" : message;
  }

  function position() {
    if (!opened) return;
    const anchor = trigger().getBoundingClientRect(), element = panel();
    element.style.maxHeight = Math.max(0, window.innerHeight - 16) + "px";
    const box = element.getBoundingClientRect();
    element.style.left = Math.max(8, Math.min(anchor.left, window.innerWidth - box.width - 8)) + "px";
    const below = anchor.bottom + 8;
    element.style.top = Math.max(8, Math.min(below, window.innerHeight - box.height - 8)) + "px";
  }
  function close(returnFocus = true) {
    if (!opened) return;
    const inside = panel().contains(document.activeElement);
    opened = false;
    trigger().setAttribute("aria-expanded", "false");
    if (native) panel().hidePopover();
    else panel().hidden = true;
    if (returnFocus && inside) trigger().focus({preventScroll: true});
  }
  function open() {
    if (pending) return;
    if (opened) return close();
    opened = true;
    trigger().setAttribute("aria-expanded", "true");
    if (native) panel().showPopover();
    else panel().hidden = false;
    position();
    const selected = panel().querySelector('[aria-pressed="true"]') || panel().querySelector(".account-option");
    selected?.focus({preventScroll: true});
    actions.onOpen();
  }
  function state(item, login = false) {
    if (pending === item.id) return pendingMessage;
    if (data.read_only) return "唯讀檢閱";
    if (!login && item.id === current) return connectionMessage(get("connectionStatus").textContent);
    return item.online ? "" : "未連線";
  }
  function option(item, login = false) {
    const button = make("button", undefined, "account-option");
    button.type = "button";
    button.dataset.accountId = item.id;
    const copy = make("span", undefined, "account-option-copy"), heading = make("span", undefined, "account-option-heading");
    heading.append(make("span", undefined, "account-campus"), make("strong", undefined, "account-option-name"));
    copy.append(heading, make("span", undefined, "account-nickname"), make("span", undefined, "account-option-status"));
    const marker = make("span", undefined, "account-option-marker");
    marker.setAttribute("aria-hidden", "true");
    button.append(copy, marker);
    button.addEventListener("click", () => {
      if (pending) return;
      if (!login && item.id === current) return close();
      return login ? actions.onLogin(item.id) : actions.onSwitch(item.id);
    });
    return button;
  }
  function updateOption(button, item, login = false) {
    const selected = login ? get("loginAccount").value === item.id : current === item.id;
    button.setAttribute("aria-pressed", String(selected));
    button.setAttribute("aria-label", (data.read_only && login ? "離線檢閱" : login ? "登入" : "切換至") + " " + identity(item));
    button.disabled = !!pending;
    button.querySelector(".account-campus").textContent = item.campus || "院區未設定";
    button.querySelector(".account-option-name").textContent = item.username || "未設定帳號";
    const caption = button.querySelector(".account-nickname"), status = button.querySelector(".account-option-status");
    caption.textContent = nickname(item);caption.hidden = !caption.textContent;
    status.textContent = state(item, login);status.hidden = !status.textContent;
    button.querySelector(".account-option-marker").textContent = selected ? "✓" : "";
  }
  function syncConnection() {
    const indicator = get("connectionStatus"), message = data?.read_only ? "唯讀" : connectionMessage(indicator.textContent);
    indicator.textContent = message;indicator.hidden = !message;indicator.title = message;
    const item = data?.accounts.find(value => value.id === current);
    const button = [...get("accountOptions").children].find(value => value.dataset.accountId === current);
    if (item && button) updateOption(button, item);
  }
  function reconcile(container, create, update) {
    const existing = new Map([...container.children].map(element => [element.dataset.accountId, element]));
    const children = data.accounts.map(item => {
      const element = existing.get(item.id) || create(item);element.dataset.accountId = item.id;
      update(element, item);return element;
    });
    // Bootstrap refreshes update existing buttons, preserving pointer targets and focus.
    if (children.length !== container.children.length || children.some((element, index) => element !== container.children[index])) container.replaceChildren(...children);
  }
  function loginCard(item) {
    const card = make("article", undefined, "saved-account"), tools = make("div", undefined, "saved-account-actions");
    card.append(option(item, true));
    for (const [text, callback] of [["離線檢閱", actions.onOffline], ["登入選項", actions.onOptions]]) {
      const control = make("button", text, "quiet");control.type = "button";
      control.addEventListener("click", () => {if (!pending) return callback(item.id);});tools.append(control);
    }
    card.append(tools);return card;
  }
  function render(next = data, owner = current) {
    data = next; current = owner;
    if (!data) return;
    const focused = document.activeElement, focusedId = panel().contains(focused) ? focused?.dataset.accountId : "";
    const item = data.accounts.find(value => value.id === current);
    get("currentAccountCampus").textContent = item?.campus || "院區";
    get("currentAccountUsername").textContent = item?.username || "";
    trigger().title = item ? "切換帳號 · " + identity(item) : "切換帳號";
    trigger().setAttribute("aria-label", item ? "切換帳號，" + identity(item) : "切換帳號");
    trigger().disabled = !!pending;
    syncConnection();
    reconcile(get("accountOptions"), item => option(item), updateOption);
    reconcile(get("savedLoginAccounts"), loginCard, (card, value) => {
      updateOption(card.children[0], value, true);
      for (const control of card.children[1].children) {
        control.setAttribute("aria-label", control.textContent + " " + identity(value));
        control.disabled = !!pending || data.read_only && control.textContent === "登入選項";
      }
    });
    get("savedLoginAccounts").hidden = !data.accounts.length;
    for (const id of ["newLoginAccount", "addWorkspaceAccount", "reconnect", "logout"])
      get(id).disabled = !!pending || !!data.read_only;
    get("accountSettings").disabled = !!pending;
    get("loginFields").disabled = !!pending || !!data.read_only || get("loginFields").hidden;
    get("content").inert = !!pending;
    for (const button of document.querySelectorAll("[data-page],[data-tool]")) button.disabled = !!pending;
    get("accountSwitchStatus").textContent = pendingMessage;
    get("accountSwitchStatus").hidden = !pending;
    panel().setAttribute("aria-busy", String(!!pending));
    get("loginForm").setAttribute("aria-busy", String(!!pending));
    if (focusedId && !pending) [...get("accountOptions").children].find(value => value.dataset.accountId === focusedId)?.focus({preventScroll: true});
    position();
  }
  function busy(id = "", message = "") {pending = id;pendingMessage = message;render();}
  function init(callbacks) {
    actions = callbacks;native = typeof panel().showPopover === "function";
    // Handle the trigger ourselves so the unsupported-browser path has the same behavior.
    trigger().removeAttribute("popovertarget");
    if (!native) {panel().removeAttribute("popover");panel().classList.add("account-panel-fallback");panel().hidden = true;}
    trigger().addEventListener("click", open);
    get("accountPanelClose").addEventListener("click", () => close());
    get("newLoginAccount").addEventListener("click", () => {if (!pending) actions.onNew();});
    get("addWorkspaceAccount").addEventListener("click", () => {if (!pending) actions.onNew();});
    if (native) panel().addEventListener("toggle", event => {
      if (event.newState === "closed") {
        opened = false;trigger().setAttribute("aria-expanded", "false");
        if (panel().contains(document.activeElement)) trigger().focus({preventScroll: true});
      }
    });
    document.addEventListener("click", event => {if (opened && !panel().contains(event.target) && !trigger().contains(event.target)) close();});
    document.addEventListener("keydown", event => {
      if (opened && event.key === "Escape") {event.preventDefault();close();}
    });
    window.addEventListener("resize", position);
    window.addEventListener("scroll", event => {if (opened && !panel().contains(event.target)) close();}, true);
  }
  return {init, render, busy, close, syncConnection, isBusy: () => !!pending};
})();
