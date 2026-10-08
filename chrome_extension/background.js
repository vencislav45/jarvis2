// Jarvis Bridge — connects this Chrome to the Jarvis assistant running on this PC.
//
// Jarvis (the server, ws://127.0.0.1:PORT) sends commands; this worker carries them
// out with the tabs and debugger APIs and replies. It never connects anywhere else.
// While a tab is being controlled Chrome shows its "started debugging this browser"
// banner; Jarvis detaches when it finishes.

const PORT = 48761;              // keep in sync with JARVIS_BRIDGE_PORT (default 48761)
const BRIDGE_URL = `ws://127.0.0.1:${PORT}/jarvis-bridge`;
const attached = new Set();
let ws = null;

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  try {
    ws = new WebSocket(BRIDGE_URL);
  } catch (e) {
    ws = null;
    return;
  }
  ws.onopen = () => send({ type: "hello", version: chrome.runtime.getManifest().version });
  ws.onmessage = (event) => onMessage(event.data);
  ws.onclose = () => { ws = null; };
  ws.onerror = () => {};   // onclose follows; the alarm retries
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

async function onMessage(raw) {
  let msg;
  try { msg = JSON.parse(raw); } catch (e) { return; }
  if (!msg || msg.type === "ping" || msg.id === undefined) return;
  try {
    send({ id: msg.id, ok: true, result: await handle(msg.cmd, msg.args || {}) });
  } catch (e) {
    send({ id: msg.id, ok: false, error: String((e && e.message) || e) });
  }
}

function tabInfo(t) {
  return t ? { id: t.id, windowId: t.windowId, url: t.url || "", title: t.title || "",
               active: !!t.active, status: t.status || "" } : null;
}

async function focusTab(tabId) {
  const tab = await chrome.tabs.update(tabId, { active: true });
  await chrome.windows.update(tab.windowId, { focused: true });
  return tabInfo(tab);
}

async function cdp(tabId, method, params) {
  if (!attached.has(tabId)) {
    await chrome.debugger.attach({ tabId }, "1.3");
    attached.add(tabId);
  }
  return await chrome.debugger.sendCommand({ tabId }, method, params || {});
}

async function handle(cmd, a) {
  switch (cmd) {
    case "list_tabs":
      return (await chrome.tabs.query({})).map(tabInfo);
    case "active_tab": {
      const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
      return tabInfo(tab);
    }
    case "tab_info":
      return tabInfo(await chrome.tabs.get(a.tabId));
    case "open_tab": {
      const tab = await chrome.tabs.create({ url: a.url || "about:blank", active: a.active !== false });
      if (a.active !== false) await chrome.windows.update(tab.windowId, { focused: true });
      return tabInfo(tab);
    }
    case "navigate":
      return tabInfo(await chrome.tabs.update(a.tabId, { url: a.url }));
    case "activate":
      return await focusTab(a.tabId);
    case "close_tab":
      await chrome.tabs.remove(a.tabId);
      return true;
    case "back":
      await chrome.tabs.goBack(a.tabId);
      return true;
    case "forward":
      await chrome.tabs.goForward(a.tabId);
      return true;
    case "reload":
      await chrome.tabs.reload(a.tabId);
      return true;
    case "cdp":
      return await cdp(a.tabId, a.method, a.params);
    case "detach":
      if (attached.has(a.tabId)) {
        attached.delete(a.tabId);
        try { await chrome.debugger.detach({ tabId: a.tabId }); } catch (e) { /* already gone */ }
      }
      return true;
    default:
      throw new Error(`Unknown command: ${cmd}`);
  }
}

chrome.debugger.onDetach.addListener((source) => attached.delete(source.tabId));
chrome.tabs.onRemoved.addListener((tabId) => attached.delete(tabId));

// Service workers sleep when idle: the alarm wakes this one to reconnect when Jarvis starts later.
chrome.alarms.create("jarvis-reconnect", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(connect);
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
setInterval(connect, 3000);
connect();
