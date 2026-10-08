"use strict";

// La interfaz solo MUESTRA el estado. Todas las reglas (exclusividad, mantenimiento,
// fallos) las aplica el servidor; si la interfaz se equivoca, el servidor rechaza.

const RADIOS = ["R1", "R2"];
const HEADERS = { "Content-Type": "application/json", "X-Antenna-Client": "web" };

let state = null;
let busy = false;
let socket = null;
let pollTimer = null;

const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function api(path, body) {
  const options = { headers: HEADERS };
  if (body !== undefined) {
    options.method = "POST";
    options.body = JSON.stringify(body);
  }
  const res = await fetch(path, options);
  let data = null;
  try { data = await res.json(); } catch (_) { /* cuerpo no JSON */ }
  if (!res.ok) {
    const detail = data && data.detail;
    let message = "Error " + res.status;
    if (detail && typeof detail === "object" && detail.message) message = detail.message;
    else if (typeof detail === "string") message = detail;
    throw new Error(message);
  }
  return data;
}

function showMessage(text) {
  const box = $("msg");
  box.textContent = text || "";
  box.hidden = !text;
}

function setState(next) {
  // Dentro del mismo arranque del servidor, ignorar estados más antiguos que el actual.
  if (state && next.boot === state.boot && next.seq < state.seq) return;
  state = next;
  render();
}

function render() {
  if (!state) return;

  const fault = $("fault");
  fault.hidden = !state.fault;
  if (state.fault) {
    fault.textContent =
      "FALLO DEL SISTEMA (" + state.fault + "): conmutación bloqueada. " +
      "Revisa el equipo y usa la parada de emergencia para rearmarlo.";
  }

  const panels = $("panels");
  panels.replaceChildren(...RADIOS.map(renderPanel));

  const status = $("status");
  status.replaceChildren();
  const relays = el("span", state.relays_ok ? "ok" : "bad",
    state.relays_ok ? "Relés verificados" : "¡El estado real de los relés no coincide!");
  status.append(relays, document.createTextNode(" · actualizado " + new Date(state.updated_at).toLocaleTimeString()));

  $("emergency").disabled = busy;
}

function renderPanel(radio) {
  const info = state.radios[radio];
  const panel = el("section", "panel");
  panel.append(el("h2", "", info.label + " (" + radio + ")"));

  const current = el("p", "current");
  current.append("Antena: ", el("strong", "", info.antenna || "ninguna"));
  panel.append(current);

  const list = el("div", "antennas");
  for (const antenna of state.antennas) {
    list.append(renderAntenna(radio, info, antenna));
  }
  const none = el("button", "ant none");
  none.type = "button";
  none.append(el("span", "name", "Sin antena"));
  none.disabled = busy || !!state.fault || info.antenna_id === null;
  none.addEventListener("click", () => select(radio, null));
  list.append(none);
  panel.append(list);
  return panel;
}

function renderAntenna(radio, info, antenna) {
  const selected = info.antenna_id === antenna.id;
  const takenByOther = antenna.in_use_by && antenna.in_use_by !== radio;

  const button = el("button", "ant" + (selected ? " selected" : ""));
  button.type = "button";
  button.setAttribute("aria-pressed", String(selected));
  button.append(el("span", "name", antenna.name));

  if (takenByOther) button.append(el("span", "sub", "En uso por " + antenna.in_use_by));
  else if (antenna.maintenance) button.append(el("span", "sub", "Fuera de servicio: " + antenna.maintenance));
  else if (antenna.bands) button.append(el("span", "sub", antenna.bands));

  const blocked = takenByOther || (antenna.maintenance && !selected);
  button.disabled = busy || !!state.fault || selected || !!blocked;
  button.addEventListener("click", () => select(radio, antenna.id));
  return button;
}

async function run(action) {
  if (busy) return;
  busy = true;
  showMessage("");
  render();
  try {
    setState(await action());
  } catch (err) {
    showMessage(err.message);
    // La petición pudo cambiar algo (p. ej. un FAULT): refrescar sin esperar al WebSocket.
    api("/api/state").then(setState).catch(() => {});
  } finally {
    busy = false;
    render();
  }
}

function select(radio, antennaId) {
  return run(() => api("/api/switch", { radio: radio, antenna_id: antennaId }));
}

function emergency() {
  if (!window.confirm("¿Soltar TODAS las antenas de ambas radios?")) return;
  return run(() => api("/api/emergency/release-all", {}));
}

// ---- tiempo real: WebSocket con reserva por sondeo ----------------------

function setConnection(up) {
  const badge = $("conn");
  badge.textContent = up ? "En directo" : "Reconectando…";
  badge.className = "badge " + (up ? "up" : "down");
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    try { setState(await api("/api/state")); } catch (_) { /* se reintenta */ }
  }, 3000);
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

function connect() {
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(scheme + "//" + location.host + "/ws");
  socket.addEventListener("open", () => { setConnection(true); stopPolling(); });
  socket.addEventListener("message", (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "state") setState(msg.state);
  });
  socket.addEventListener("close", () => {
    setConnection(false);
    startPolling();
    setTimeout(connect, 2000);
  });
}

$("emergency").addEventListener("click", emergency);
connect();
api("/api/state").then(setState).catch((err) => showMessage(err.message));
