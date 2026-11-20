const byId = (id) => document.getElementById(id);
const areas = {
  "asia-pacific": "亚太地区",
  europe: "欧洲",
  americas: "美洲",
  "middle-east-africa": "中东与非洲",
  other: "其他地区",
};
const states = {
  starting: "正在检查",
  unavailable: "服务暂不可用",
  disconnected: "未连接",
  connecting: "正在连接",
  connected: "已连接",
  interrupted: "连接中断",
  reconnecting: "正在重连",
  disconnectingtoreconnect: "正在切换",
  disconnecting: "正在断开",
};
const actionLabels = {
  login: "正在登录 ExpressVPN…",
  logout: "正在退出 ExpressVPN…",
  refresh: "正在刷新节点…",
  "test-proxy": "正在通过 SOCKS5 检测 HTTPS 出口…",
};
const connectionActions = new Set(["connect", "disconnect"]);
const transitionStates = new Set(["connecting", "reconnecting", "disconnectingtoreconnect", "disconnecting"]);
const countryNames = new Intl.DisplayNames(["zh-CN"], { type: "region" });
const smart = { id: "smart", country: null, area: null };
const form = byId("login");
const areaChoice = byId("area");
const countryChoice = byId("country");
const search = byId("search");
const nodeList = byId("nodes");

let snapshot = { state: "starting", regions: [] };
let pendingAction = null;
let connectionRequest = null;
let statusRequest;
let statusUnavailable = false;
let selected = smart.id;
let method = "code";
let nodeSignature = null;

function chip(id, label, tone = "neutral") {
  const element = byId(id);
  element.dataset.tone = tone;
  element.lastElementChild.textContent = label;
}

function notice(message = "", tone = "info", target = "notice") {
  const element = byId(target);
  element.hidden = !message;
  if (element.textContent !== message) element.textContent = message;
  element.dataset.tone = tone;
}

function locationName(id) {
  return id === smart.id ? "智能位置" : id.replaceAll("-", " ").replace(/\b[a-z]/g, (char) => char.toUpperCase());
}

function countryName(code) {
  return code ? countryNames.of(code) : "";
}

function addOptions(select, firstLabel, options) {
  const selectedValue = select.value;
  const next = [["", firstLabel], ...options];
  if (select.options.length !== next.length || next.some(([value], index) => select.options[index].value !== value)) {
    select.replaceChildren(...next.map(([value, label]) => new Option(label, value)));
    if (options.some(([value]) => value === selectedValue)) select.value = selectedValue;
  }
}

function renderNodes(connectedLocation) {
  const { regions } = snapshot;
  if (selected !== smart.id && !regions.some((entry) => entry.id === selected)) selected = smart.id;
  const activeAreas = new Set(regions.map((entry) => entry.area));
  addOptions(areaChoice, "全部地区", Object.entries(areas).filter(([key]) => activeAreas.has(key)));
  const inArea = regions.filter((entry) => !areaChoice.value || entry.area === areaChoice.value);
  const nationOptions = [...new Set(inArea.map((entry) => entry.country).filter(Boolean))]
    .map((code) => [code, countryName(code)])
    .sort((first, second) => first[1].localeCompare(second[1], "zh-CN"));
  addOptions(countryChoice, "全部国家或地区", nationOptions);
  const text = search.value.trim().toLowerCase();
  const visible = inArea.filter((entry) => (!countryChoice.value || entry.country === countryChoice.value)
    && [entry.id, locationName(entry.id), countryName(entry.country)].some((name) => name.toLowerCase().includes(text)));
  if (!areaChoice.value && !countryChoice.value && !text && snapshot.signed_in) visible.unshift(smart);
  byId("location-count").textContent = regions.length
    ? `${visible.filter((entry) => entry !== smart).length} / ${regions.length} 个节点`
    : "暂无节点";
  const signature = JSON.stringify([visible.map((entry) => entry.id), connectedLocation, snapshot.signed_in]);
  if (signature !== nodeSignature) {
    nodeSignature = signature;
    if (visible.length) {
      nodeList.replaceChildren(...visible.map((entry) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "node";
        button.dataset.id = entry.id;
        const code = document.createElement("span");
        code.className = "node-code";
        code.textContent = entry.country || "◎";
        const name = document.createElement("span");
        name.className = "node-name";
        const title = document.createElement("strong");
        title.textContent = locationName(entry.id);
        name.append(title);
        if (entry.country) {
          const label = document.createElement("small");
          label.textContent = countryName(entry.country);
          name.append(label);
        }
        button.append(code, name);
        if (entry.id === connectedLocation) {
          const active = document.createElement("span");
          active.className = "node-active";
          active.textContent = "已连接";
          button.append(active);
        }
        return button;
      }));
    } else {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = snapshot.signed_in ? "当前条件下没有可用节点。" : "登录后会加载可用节点。";
      nodeList.replaceChildren(empty);
    }
  }
  for (const button of nodeList.querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset.id === selected));
  }
  byId("selection").textContent = locationName(selected);
}

function render() {
  const { ready, state, signed_in: signedIn, regions, location, vpn_ip: ip, proxy_ready: proxyReady, dns_ready: dnsReady } = snapshot;
  const action = pendingAction || snapshot.action || connectionRequest?.action;
  const busy = Boolean(action);
  const connecting = action === "connect";
  const disconnecting = action === "disconnect";
  const switching = connecting && (snapshot.action_stage === "switching"
    || state === "disconnectingtoreconnect" || connectionRequest?.switching);
  const operationError = connecting || disconnecting ? "" : snapshot.operation_error;
  const changing = connecting || disconnecting || (!operationError && transitionStates.has(state));
  const clientNotice = statusUnavailable ? "暂未读取到网关状态，正在重试。" : snapshot.client_notice;
  const confirmed = !statusUnavailable && !clientNotice;
  const connected = confirmed && !changing && state === "connected";
  const controlsReady = ready && !statusUnavailable;
  const canCancelConnection = !pendingAction && !statusUnavailable && snapshot.action === "connect";
  const proxyAvailable = proxyReady && connected;
  const connectionState = switching ? "disconnectingtoreconnect" : connecting ? "connecting" : disconnecting ? "disconnecting" : state;
  const title = states[connectionState] || "正在检查";
  const target = connecting ? snapshot.action_location || connectionRequest?.location : null;

  chip("service-state", clientNotice ? "状态待确认" : ready ? "服务就绪" : states[state],
    ready && !clientNotice ? "good" : "warn");
  chip("account-state", signedIn ? "已登录" : "待登录", signedIn ? "good" : "neutral");
  chip("proxy-state", proxyAvailable ? "隧道就绪" : "等待 VPN", proxyAvailable ? "good" : "neutral");
  byId("connection").dataset.tone = connected ? "good" : changing ? "warn" : "neutral";
  const heading = byId("connection-title");
  const headingText = operationError && transitionStates.has(state) ? "操作未完成"
    : confirmed || changing ? title : "状态待确认";
  if (heading.textContent !== headingText) heading.textContent = headingText;
  byId("connection-location").textContent = target ? `目标节点 · ${locationName(target)}`
    : changing ? "等待 VPN 隧道状态更新"
    : connected && location ? locationName(location)
    : !confirmed ? ""
    : ready ? "请选择节点或使用智能位置"
    : state === "starting" ? "等待官方客户端启动" : "";
  notice(clientNotice || "", "info", "client-notice");
  notice(operationError || "", "error", "operation-error");
  byId("vpn-ip").textContent = connected && ip ? ip : "—";
  byId("disconnect").disabled = !canCancelConnection
    && (!controlsReady || busy || (!connected && !transitionStates.has(state) && state !== "interrupted"));
  byId("disconnect").textContent = disconnecting ? "正在断开…" : "断开连接";
  byId("connect").disabled = !controlsReady || !signedIn || busy || changing;
  byId("connect").textContent = connecting ? `${title}…` : connected ? "切换节点" : "连接";
  byId("reload").disabled = !controlsReady || !signedIn || busy;
  for (const field of [areaChoice, countryChoice, search]) field.disabled = !regions.length;
  form.hidden = signedIn === true;
  byId("account-active").hidden = signedIn !== true;
  byId("login-button").disabled = !controlsReady || busy;
  for (const tab of form.querySelectorAll("[data-method]")) tab.disabled = busy;
  byId("logout").disabled = !controlsReady || busy;
  byId("test-proxy").disabled = !controlsReady || !proxyAvailable || !dnsReady || busy;
  byId("endpoint").textContent = `socks5h://${window.location.hostname}:${snapshot.socks_port || 1080}`;
  byId("proxy-user").textContent = snapshot.socks_user || "proxy";
  byId("dns-state").textContent = proxyAvailable && !dnsReady ? "等待 VPN DNS。当前仅支持 IP 地址。" : "域名由代理通过 VPN 解析。";
  renderNodes(connected ? location : null);
}

function refresh() {
  if (statusRequest) return statusRequest;
  statusRequest = (async () => {
    try {
      const result = await fetch("/api/status", { signal: AbortSignal.timeout(5000) });
      if (!result.ok) throw new Error("读取失败");
      snapshot = await result.json();
      statusUnavailable = false;
      if (!pendingAction && !connectionActions.has(snapshot.action)
        && (snapshot.operation_error || !transitionStates.has(snapshot.state))) {
        connectionRequest = null;
      }
    } catch {
      statusUnavailable = true;
    } finally {
      statusRequest = null;
      render();
    }
  })();
  return statusRequest;
}

async function perform(action, data = {}) {
  const cancelsConnection = action === "disconnect" && snapshot.action === "connect";
  if (pendingAction || (snapshot.action && !cancelsConnection)) return;
  pendingAction = action;
  const previousRequest = connectionRequest;
  let accepted = false;
  if (action === "connect") {
    connectionRequest = {
      action,
      location: data.location,
      switching: snapshot.state === "connected" && snapshot.location !== data.location,
    };
  } else if (action === "disconnect") {
    connectionRequest = { action };
  }
  const target = action === "login" ? "login-message" : action === "test-proxy" ? "proxy-message" : "notice";
  if (["connect", "disconnect", "logout"].includes(action)) notice("", "info", "proxy-message");
  notice(actionLabels[action], "info", target);
  render();
  try {
    const result = await fetch(`/api/control/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-VPN-Control": "1" },
      body: JSON.stringify(data),
      signal: AbortSignal.timeout(95000),
    });
    const reply = await result.json().catch(() => ({}));
    if (!result.ok) {
      notice(reply.error || "网关暂时无法处理请求。", "error", target);
    } else {
      accepted = true;
      const message = action === "test-proxy" ? `HTTPS 出口检测通过 · ${reply.ip}` : "";
      notice(message, "info", target);
    }
  } catch {
    notice("请求中断或超时。请检查当前状态后重试。", "error", target);
  } finally {
    if (connectionActions.has(action)) {
      await statusRequest;
      if (!accepted) connectionRequest = previousRequest;
    }
    pendingAction = null;
    await refresh();
  }
}

for (const tab of form.querySelectorAll("[data-method]")) {
  tab.addEventListener("click", () => {
    method = tab.dataset.method;
    for (const choice of form.querySelectorAll("[data-method]")) {
      choice.setAttribute("aria-pressed", String(choice === tab));
    }
    for (const name of ["email", "code"]) {
      const fields = byId(`${name}-fields`);
      fields.hidden = method !== name;
      for (const input of fields.querySelectorAll("input")) input.disabled = fields.hidden;
    }
    notice("", "info", "login-message");
  });
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(form));
  form.elements.password.value = "";
  form.elements.code.value = "";
  perform("login", { method, ...values });
});
nodeList.addEventListener("click", (event) => {
  const row = event.target.closest("button[data-id]");
  if (row) {
    selected = row.dataset.id;
    render();
  }
});
areaChoice.addEventListener("change", () => {
  countryChoice.value = "";
  render();
});
countryChoice.addEventListener("change", render);
search.addEventListener("input", render);
byId("connect").addEventListener("click", () => perform("connect", { location: selected }));
byId("disconnect").addEventListener("click", () => perform("disconnect"));
byId("reload").addEventListener("click", () => perform("refresh"));
byId("logout").addEventListener("click", () => perform("logout"));
byId("test-proxy").addEventListener("click", () => perform("test-proxy"));
byId("copy").addEventListener("click", async () => {
  const endpoint = byId("endpoint");
  try {
    await navigator.clipboard.writeText(endpoint.textContent);
    notice("已复制代理地址。");
  } catch {
    const range = document.createRange();
    range.selectNodeContents(endpoint);
    window.getSelection().removeAllRanges();
    window.getSelection().addRange(range);
  }
});

refresh();
setInterval(refresh, 2500);
