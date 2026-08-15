// Macro Studio -- web UI (Phase 2)
// Plain JS, no build step, no external CDN calls -- everything here is
// served by the local agent and talks only to 127.0.0.1.

const connStatusEl = document.getElementById("connStatus");
const statusGridEl = document.getElementById("statusGrid");
const recordBtn = document.getElementById("recordBtn");
const pauseBtn = document.getElementById("pauseBtn");
const stopBtn = document.getElementById("stopBtn");
const recordNote = document.getElementById("recordNote");
const phaseBadge = document.getElementById("phaseBadge");
const hotkeyHint = document.getElementById("hotkeyHint");
const hotkeyInput = document.getElementById("hotkeyInput");
const saveHotkeyBtn = document.getElementById("saveHotkeyBtn");
const hotkeyNote = document.getElementById("hotkeyNote");
const stepListWrap = document.getElementById("stepListWrap");
const stepListEl = document.getElementById("stepList");
const stepCountEl = document.getElementById("stepCount");
const replayPanel = document.getElementById("replayPanel");
const replayBtn = document.getElementById("replayBtn");
const stopReplayBtn = document.getElementById("stopReplayBtn");
const stopLibraryReplayBtn = document.getElementById("stopLibraryReplayBtn");
const replaySummary = document.getElementById("replaySummary");
const replayNote = document.getElementById("replayNote");
const runResultListEl = document.getElementById("runResultList");
const saveMacroBtn = document.getElementById("saveMacroBtn");
const discardMacroBtn = document.getElementById("discardMacroBtn");
const saveMacroForm = document.getElementById("saveMacroForm");
const macroNameInput = document.getElementById("macroNameInput");
const confirmSaveMacroBtn = document.getElementById("confirmSaveMacroBtn");
const cancelSaveMacroBtn = document.getElementById("cancelSaveMacroBtn");
const selectAllMacros = document.getElementById("selectAllMacros");
const deleteSelectedBtn = document.getElementById("deleteSelectedBtn");
const deleteAllBtn = document.getElementById("deleteAllBtn");
const libraryNote = document.getElementById("libraryNote");
const libraryEmptyState = document.getElementById("libraryEmptyState");
const macroTable = document.getElementById("macroTable");
const macroTableBody = document.getElementById("macroTableBody");
const libraryRunPanel = document.getElementById("libraryRunPanel");
const libraryRunName = document.getElementById("libraryRunName");
const libraryRunNote = document.getElementById("libraryRunNote");
const libraryRunResultList = document.getElementById("libraryRunResultList");

let recordingState = "idle"; // idle | recording | paused | stopped
let macros = [];
let activeRunTarget = null; // null | "last" | macro id -- which list run_step_result WS events append to

function setConnected(isConnected) {
  connStatusEl.classList.toggle("connected", isConnected);
}

function showNote(el, message, kind) {
  el.textContent = message;
  el.className = `inline-note show ${kind}`;
}

function hideNote(el) {
  el.className = "inline-note";
}

// -- recording state -------------------------------------------------

function applyRecordingState(state) {
  recordingState = state;
  recordBtn.textContent = state === "idle" || state === "stopped" ? "Record" : "Recording...";
  recordBtn.disabled = state === "recording" || state === "paused";
  pauseBtn.disabled = state !== "recording" && state !== "paused";
  pauseBtn.textContent = state === "paused" ? "Resume" : "Pause";
  stopBtn.disabled = state !== "recording" && state !== "paused";
  stepListWrap.style.display = state === "idle" ? "none" : "block";
  const hasStoppedSteps = state === "stopped" && stepListEl.children.length > 0;
  replayPanel.style.display = hasStoppedSteps ? "block" : "none";
  saveMacroBtn.style.display = hasStoppedSteps ? "inline-flex" : "none";
  discardMacroBtn.style.display = hasStoppedSteps ? "inline-flex" : "none";
  if (!hasStoppedSteps) saveMacroForm.style.display = "none";
  if (state !== "stopped") {
    runResultListEl.innerHTML = "";
    replaySummary.textContent = "";
    hideNote(replayNote);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderStepDetail(step) {
  switch (step.type) {
    case "click":
    case "double_click": {
      const target = step.semantic && step.semantic.target;
      const label = target && (target.name || target.automation_id)
        ? `${target.control_type || "Element"} "${target.name || target.automation_id}"`
        : "(no accessible element)";
      const windowPart = step.window?.title ? ` in "${step.window.title}"` : "";
      return `${escapeHtml(label)} @ (${step.x}, ${step.y})${escapeHtml(windowPart)}`;
    }
    case "scroll":
      return `dx=${step.dx} dy=${step.dy} @ (${step.x}, ${step.y})`;
    case "key":
    case "hotkey":
      return escapeHtml(step.keys.join(" + "));
    default:
      return escapeHtml(JSON.stringify(step));
  }
}

function typeLabel(type) {
  const labels = {
    click: "Click", double_click: "Double-click", scroll: "Scroll", key: "Key", hotkey: "Hotkey",
    wait: "Wait", wait_for_element: "Wait for element", wait_for_text: "Wait for text",
    find_click_text: "Find & click text", open_url: "Open URL", file_search: "File search",
    file_op: "File op", clipboard: "Clipboard", keyboard_shortcut: "Shortcut",
    get_cursor_position: "Cursor pos", read_control_value: "Read value",
    conditional: "If/Else", loop: "Loop",
  };
  return labels[type] || type;
}

function appendStep(step) {
  const li = document.createElement("li");
  li.className = step.fragile ? "fragile" : "";
  const fragileBadge = step.fragile
    ? `<span class="fragile-badge" title="No accessible UI element found here -- this step uses coordinates and may break if the window moves or resizes.">&#9888; fragile</span>`
    : "";
  li.innerHTML = `
    <span class="step-seq">#${step.seq}</span>
    <span class="step-type">${typeLabel(step.type)}</span>
    <span class="step-detail">${renderStepDetail(step)}</span>
    ${fragileBadge}
    <span class="step-delay">+${step.delay_ms}ms</span>
  `;
  stepListEl.appendChild(li);
  stepListEl.scrollTop = stepListEl.scrollHeight;
  stepCountEl.textContent = `(${stepListEl.children.length})`;
}

function clearSteps() {
  stepListEl.innerHTML = "";
  stepCountEl.textContent = "";
}

async function loadRecordingState() {
  try {
    const res = await fetch("/api/recording/state");
    const body = await res.json();
    clearSteps();
    (body.steps || []).forEach(appendStep);
    applyRecordingState(body.state);
  } catch {
    // status panel already reports connectivity issues
  }
}

async function callRecordingApi(path) {
  try {
    const res = await fetch(`/api/recording/${path}`, { method: "POST" });
    const body = await res.json();
    if (!res.ok) {
      showNote(recordNote, body.detail || `Unexpected error (HTTP ${res.status})`, "error");
      return;
    }
    hideNote(recordNote);
    if (body.state) applyRecordingState(body.state);
    if (path === "start") clearSteps();
  } catch (err) {
    showNote(recordNote, `Could not reach the agent: ${err.message}`, "error");
  }
}

recordBtn.addEventListener("click", () => callRecordingApi("start"));
pauseBtn.addEventListener("click", () => callRecordingApi(recordingState === "paused" ? "resume" : "pause"));
stopBtn.addEventListener("click", () => callRecordingApi("stop"));
discardMacroBtn.addEventListener("click", () => callRecordingApi("cancel"));

saveMacroBtn.addEventListener("click", () => {
  saveMacroForm.style.display = "flex";
  macroNameInput.value = "";
  macroNameInput.focus();
});

cancelSaveMacroBtn.addEventListener("click", () => {
  saveMacroForm.style.display = "none";
});

async function submitSaveMacro() {
  const name = macroNameInput.value.trim();
  if (!name) {
    showNote(recordNote, "Enter a name for the macro.", "error");
    macroNameInput.focus();
    return;
  }
  try {
    const res = await fetch("/api/macros", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const body = await res.json();
    if (!res.ok) {
      showNote(recordNote, body.detail || `Unexpected error (HTTP ${res.status})`, "error");
      return;
    }
    hideNote(recordNote);
    saveMacroForm.style.display = "none";
    applyRecordingState("idle");
    clearSteps();
    loadMacros();
  } catch (err) {
    showNote(recordNote, `Could not reach the agent: ${err.message}`, "error");
  }
}

confirmSaveMacroBtn.addEventListener("click", submitSaveMacro);
macroNameInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") submitSaveMacro();
});

// -- replay --------------------------------------------------------------

function renderRunResult(result, listEl) {
  const li = document.createElement("li");
  const statusColor = { success: "#3dd68c", failed: "#e5484d", skipped: "#9aa2b1", stopped: "#e8a53d" }[result.status] || "#9aa2b1";
  li.style.borderLeft = `3px solid ${statusColor}`;
  const screenshotLink = result.screenshot
    ? ` <a href="${result.screenshot}" target="_blank" rel="noopener" style="color:${statusColor};">screenshot</a>`
    : "";
  li.innerHTML = `
    <span class="step-seq">#${result.seq}</span>
    <span class="step-type" style="color:${statusColor};">${result.status}</span>
    <span class="step-detail">${result.tier ? `[${result.tier}] ` : ""}${escapeHtml(result.reason || "")}${screenshotLink}</span>
    <span class="step-delay">${result.duration_ms}ms</span>
  `;
  listEl.appendChild(li);
  listEl.scrollTop = listEl.scrollHeight;
}

async function runReplay(allowForeground) {
  activeRunTarget = "last";
  replayBtn.disabled = true;
  runResultListEl.innerHTML = "";
  hideNote(replayNote);
  replaySummary.textContent = "Running...";
  try {
    const res = await fetch("/api/replay/last", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ allow_foreground: allowForeground }),
    });
    const body = await res.json();
    if (!res.ok) {
      showNote(replayNote, body.detail || `Unexpected error (HTTP ${res.status})`, "error");
      replaySummary.textContent = "";
      return;
    }
    const { passed, failed, total, stopped } = body.summary;
    replaySummary.textContent = `${passed}/${total} steps succeeded`;

    if (stopped) {
      showNote(replayNote, `Stopped by user after ${body.results.length} step(s) -- see results below.`, "warn");
    } else if (failed > 0 && !allowForeground) {
      showNote(
        replayNote,
        `${failed} step(s) failed -- see reasons below. Some background methods (posted clicks/keys, ` +
          "UIA invoke on a menu that isn't really open) can silently no-op instead of erroring. " +
          'Click "Allow foreground control & replay" to retry with real mouse/keyboard input -- it WILL move your cursor and type.',
        "warn"
      );
      showForegroundConfirm(replayNote, () => runReplay(true));
    } else if (failed > 0) {
      showNote(replayNote, `${failed} step(s) still failed even with foreground control -- see reasons below.`, "error");
    } else {
      showNote(replayNote, "Replay finished successfully.", "info");
    }
  } catch (err) {
    showNote(replayNote, `Could not reach the agent: ${err.message}`, "error");
  } finally {
    replayBtn.disabled = false;
  }
}

function showForegroundConfirm(noteEl, onConfirm) {
  const existing = noteEl.parentElement.querySelector(".foreground-confirm-btn");
  if (existing) existing.remove();
  const btn = document.createElement("button");
  btn.className = "primary foreground-confirm-btn";
  btn.textContent = "Allow foreground control & replay";
  btn.addEventListener("click", () => {
    btn.remove();
    onConfirm();
  });
  noteEl.insertAdjacentElement("afterend", btn);
}

replayBtn.addEventListener("click", () => runReplay(false));

async function stopActiveReplay(btn) {
  btn.disabled = true;
  try {
    await fetch("/api/replay/stop", { method: "POST" });
  } catch {
    // the run's own finish/error handling will surface anything meaningful
  } finally {
    setTimeout(() => (btn.disabled = false), 1000);
  }
}

stopReplayBtn.addEventListener("click", () => stopActiveReplay(stopReplayBtn));
stopLibraryReplayBtn.addEventListener("click", () => stopActiveReplay(stopLibraryReplayBtn));

// -- macro library ---------------------------------------------------------

function formatTimestamp(iso) {
  if (!iso) return "never";
  const d = new Date(iso);
  return isNaN(d) ? "never" : d.toLocaleString();
}

function formatResult(result) {
  if (!result) return "&mdash;";
  const cls = result.failed > 0 ? "result-fail" : "result-pass";
  return `<span class="${cls}">${result.passed}/${result.total}</span>`;
}

function selectedMacroIds() {
  return Array.from(macroTableBody.querySelectorAll(".macro-select:checked")).map((cb) => cb.dataset.id);
}

function updateDeleteSelectedState() {
  deleteSelectedBtn.disabled = selectedMacroIds().length === 0;
}

function renderMacroTable() {
  libraryEmptyState.style.display = macros.length === 0 ? "block" : "none";
  macroTable.style.display = macros.length === 0 ? "none" : "table";
  macroTableBody.innerHTML = macros
    .map(
      (m) => `
    <tr data-id="${m.id}">
      <td><input type="checkbox" class="macro-select" data-id="${m.id}" /></td>
      <td>${escapeHtml(m.name)}</td>
      <td>${m.step_count}</td>
      <td>${formatTimestamp(m.last_run)}</td>
      <td>${formatResult(m.last_result)}</td>
      <td class="actions">
        <button data-action="replay">Replay</button>
        <button data-action="edit">Edit</button>
        <button data-action="rename">Rename</button>
        <button data-action="duplicate">Duplicate</button>
        <button data-action="delete">Delete</button>
      </td>
    </tr>`
    )
    .join("");
  selectAllMacros.checked = false;
  updateDeleteSelectedState();
}

async function loadMacros() {
  try {
    const res = await fetch("/api/macros");
    macros = await res.json();
    renderMacroTable();
  } catch (err) {
    showNote(libraryNote, `Could not reach the agent: ${err.message}`, "error");
  }
}

selectAllMacros.addEventListener("change", () => {
  macroTableBody.querySelectorAll(".macro-select").forEach((cb) => (cb.checked = selectAllMacros.checked));
  updateDeleteSelectedState();
});

macroTableBody.addEventListener("change", (e) => {
  if (e.target.classList.contains("macro-select")) updateDeleteSelectedState();
});

macroTableBody.addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-action]");
  if (!btn) return;
  const row = btn.closest("tr");
  const id = row.dataset.id;
  const macro = macros.find((m) => m.id === id);
  const action = btn.dataset.action;

  if (action === "replay") {
    runLibraryReplay(id, macro.name, false);
  } else if (action === "edit") {
    openEditor(id, macro.name);
  } else if (action === "rename") {
    const name = window.prompt("New name:", macro.name);
    if (!name || !name.trim() || name.trim() === macro.name) return;
    const res = await fetch(`/api/macros/${id}/rename`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }),
    });
    const body = await res.json();
    if (!res.ok) showNote(libraryNote, body.detail || "Rename failed.", "error");
    else hideNote(libraryNote);
    loadMacros();
  } else if (action === "duplicate") {
    const res = await fetch(`/api/macros/${id}/duplicate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const body = await res.json();
    if (!res.ok) showNote(libraryNote, body.detail || "Duplicate failed.", "error");
    else hideNote(libraryNote);
    loadMacros();
  } else if (action === "delete") {
    if (!window.confirm(`Delete macro "${macro.name}"? This can't be undone.`)) return;
    const res = await fetch(`/api/macros/${id}`, { method: "DELETE" });
    if (!res.ok) {
      const body = await res.json();
      showNote(libraryNote, body.detail || "Delete failed.", "error");
    } else {
      hideNote(libraryNote);
    }
    loadMacros();
  }
});

deleteSelectedBtn.addEventListener("click", async () => {
  const ids = selectedMacroIds();
  if (ids.length === 0) return;
  if (!window.confirm(`Delete ${ids.length} selected macro(s)? This can't be undone.`)) return;
  const res = await fetch("/api/macros/delete-selected", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids }),
  });
  if (!res.ok) {
    const body = await res.json();
    showNote(libraryNote, body.detail || "Delete failed.", "error");
  } else {
    hideNote(libraryNote);
  }
  loadMacros();
});

deleteAllBtn.addEventListener("click", async () => {
  if (macros.length === 0) return;
  const typed = window.prompt(`Type DELETE ALL to permanently delete all ${macros.length} macro(s):`);
  if (typed === null) return;
  const res = await fetch("/api/macros/delete-all", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm: typed }),
  });
  const body = await res.json();
  if (!res.ok) {
    showNote(libraryNote, body.detail || "Delete failed.", "error");
  } else {
    hideNote(libraryNote);
  }
  loadMacros();
});

async function runLibraryReplay(id, name, allowForeground) {
  activeRunTarget = id;
  libraryRunPanel.style.display = "block";
  libraryRunName.textContent = name;
  libraryRunResultList.innerHTML = "";
  hideNote(libraryRunNote);
  showNote(libraryRunNote, "Running...", "info");
  try {
    const res = await fetch(`/api/macros/${id}/replay`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ allow_foreground: allowForeground }),
    });
    const body = await res.json();
    if (!res.ok) {
      showNote(libraryRunNote, body.detail || `Unexpected error (HTTP ${res.status})`, "error");
      return;
    }
    const { passed, failed, total, stopped } = body.summary;
    if (stopped) {
      showNote(libraryRunNote, `Stopped by user after ${body.results.length} step(s) -- see results below.`, "warn");
    } else if (failed > 0 && !allowForeground) {
      showNote(
        libraryRunNote,
        `${passed}/${total} succeeded, ${failed} failed -- see reasons below. ` +
          'Click "Allow foreground control & replay" to retry with real mouse/keyboard input.',
        "warn"
      );
      showForegroundConfirm(libraryRunNote, () => runLibraryReplay(id, name, true));
    } else if (failed > 0) {
      showNote(libraryRunNote, `${passed}/${total} succeeded, ${failed} still failed with foreground control.`, "error");
    } else {
      showNote(libraryRunNote, `Replay finished successfully (${passed}/${total}).`, "info");
    }
    if (body.video_error) {
      showNote(libraryRunNote, `${libraryRunNote.textContent} Video: ${body.video_error}`, "warn");
    } else if (body.video) {
      libraryRunNote.insertAdjacentHTML(
        "beforeend",
        ` <a href="${body.video}" target="_blank" rel="noopener" style="color:inherit;text-decoration:underline;">Watch recording</a>`
      );
    }
    loadMacros();
  } catch (err) {
    showNote(libraryRunNote, `Could not reach the agent: ${err.message}`, "error");
  }
}

// -- hotkey settings ---------------------------------------------------

async function loadHotkey() {
  try {
    const res = await fetch("/api/settings/hotkey");
    const body = await res.json();
    hotkeyHint.textContent = `Stop: ${body.display}`;
    hotkeyInput.value = body.keys.join("+");
  } catch {
    hotkeyHint.textContent = "";
  }
}

saveHotkeyBtn.addEventListener("click", async () => {
  const keys = hotkeyInput.value.split("+").map((k) => k.trim()).filter(Boolean);
  if (keys.length === 0) {
    showNote(hotkeyNote, "Enter at least one key.", "error");
    return;
  }
  try {
    const res = await fetch("/api/settings/hotkey", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keys }),
    });
    const body = await res.json();
    if (!res.ok) {
      showNote(hotkeyNote, body.detail || `Unexpected error (HTTP ${res.status})`, "error");
      return;
    }
    hotkeyHint.textContent = `Stop: ${body.display}`;
    showNote(hotkeyNote, `Saved. Stop hotkey is now ${body.display}.`, "info");
  } catch (err) {
    showNote(hotkeyNote, `Could not reach the agent: ${err.message}`, "error");
  }
});

// -- agent status --------------------------------------------------------

function renderStatus(status) {
  const rows = [
    ["Version", status.version],
    ["PID", status.pid],
    ["Python", status.python_version],
    ["Elevated", status.is_admin ? "yes" : "no"],
    ["Listening on", `${status.host}:${status.port}`],
    ["Uptime", `${Math.floor(status.uptime_seconds)}s`],
  ];
  statusGridEl.innerHTML = rows
    .map(
      ([label, value]) => `
      <div class="item">
        <div class="label">${label}</div>
        <div class="value">${value}</div>
      </div>`
    )
    .join("");
  phaseBadge.textContent = `Phase ${status.phase}`;

  if (status.is_admin) {
    showNote(recordNote,
      "The agent is running elevated (as Administrator). It will only be able to control other elevated windows.",
      "info"
    );
  }
}

async function pollStatus() {
  try {
    const res = await fetch("/api/status");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const status = await res.json();
    renderStatus(status);
  } catch (err) {
    statusGridEl.innerHTML = `<div class="item"><div class="label">Error</div><div class="value">Could not reach agent: ${err.message}</div></div>`;
  }
}

// -- websocket -------------------------------------------------------

function connectWebSocket() {
  const ws = new WebSocket(`ws://${location.host}/ws`);

  ws.onopen = () => setConnected(true);
  ws.onclose = () => {
    setConnected(false);
    setTimeout(connectWebSocket, 2000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }
    switch (msg.type) {
      case "connected":
        setConnected(true);
        break;
      case "recording_state":
        applyRecordingState(msg.state);
        break;
      case "step_added":
        appendStep(msg.step);
        break;
      case "hotkey_stop_triggered":
        showNote(recordNote, "Recording stopped via global hotkey.", "info");
        break;
      case "accessibility_warning":
        showNote(recordNote, msg.message, "warn");
        break;
      case "run_step_result":
        renderRunResult(msg.result, activeRunTarget === "last" ? runResultListEl : libraryRunResultList);
        break;
      case "run_state":
        stopReplayBtn.style.display = msg.state === "running" && activeRunTarget === "last" ? "inline-flex" : "none";
        stopLibraryReplayBtn.style.display = msg.state === "running" && activeRunTarget !== "last" && activeRunTarget !== null ? "inline-flex" : "none";
        if (msg.state === "running" && activeRunTarget === "last") {
          replaySummary.textContent = `Running 0/${msg.step_count}...`;
        }
        break;
      case "panic_triggered": {
        const parts = [];
        if (msg.stopped_recording) parts.push("recording");
        if (msg.stopped_replay) parts.push("replay");
        showNote(recordNote, `Panic hotkey (Esc held 1s): stopped ${parts.join(" and ") || "nothing active"}.`, "warn");
        break;
      }
      // heartbeat: no-op, just keeps the connection alive
    }
  };
}

// -- step editor (Phase 6) --------------------------------------------------

const macroEditorPanel = document.getElementById("macroEditorPanel");
const editorMacroName = document.getElementById("editorMacroName");
const editorStepList = document.getElementById("editorStepList");
const editorNote = document.getElementById("editorNote");
const addStepType = document.getElementById("addStepType");
const addStepBtn = document.getElementById("addStepBtn");
const saveEditorBtn = document.getElementById("saveEditorBtn");
const cancelEditorBtn = document.getElementById("cancelEditorBtn");

const STEP_TEMPLATES = {
  wait: { label: "Wait", make: () => ({ type: "wait", duration_ms: 500, delay_ms: 0 }) },
  wait_for_element: {
    label: "Wait for element",
    make: () => ({ type: "wait_for_element", window_title: "", target: { name: "", automation_id: "", control_type: "" }, timeout_ms: 5000, delay_ms: 0 }),
  },
  wait_for_text: {
    label: "Wait for text",
    make: () => ({ type: "wait_for_text", window_title: "", target: { name: "", automation_id: "" }, expected: "", is_regex: false, timeout_ms: 5000, delay_ms: 0 }),
  },
  find_click_text: {
    label: "Find and click by text",
    make: () => ({ type: "find_click_text", window_title: "", text: "", exact: false, delay_ms: 0 }),
  },
  open_url: { label: "Open URL in Chrome", make: () => ({ type: "open_url", url: "https://", delay_ms: 0 }) },
  file_search: {
    label: "File Explorer search",
    make: () => ({ type: "file_search", folder: "", pattern: "*", recursive: false, store_as: "matches", delay_ms: 0 }),
  },
  file_op: {
    label: "File operation",
    make: () => ({ type: "file_op", operation: "copy", source: "", destination: "", overwrite: false, delay_ms: 0 }),
  },
  clipboard: { label: "Clipboard", make: () => ({ type: "clipboard", mode: "write", value: "", store_as: "clip", delay_ms: 0 }) },
  keyboard_shortcut: { label: "Keyboard shortcut", make: () => ({ type: "keyboard_shortcut", keys: ["ctrl", "c"], delay_ms: 0 }) },
  get_cursor_position: { label: "Get cursor position", make: () => ({ type: "get_cursor_position", store_as: "pos", delay_ms: 0 }) },
  read_control_value: {
    label: "Read control value",
    make: () => ({ type: "read_control_value", window_title: "", target: { name: "", automation_id: "" }, store_as: "value", delay_ms: 0 }),
  },
  conditional: {
    label: "Conditional (if/else)",
    make: () => ({ type: "conditional", variable: "", operator: "equals", value: "", then_steps: [], else_steps: [], delay_ms: 0 }),
  },
  loop: {
    label: "Loop",
    make: () => ({ type: "loop", mode: "count", count: 3, variable: "", operator: "equals", value: "", max_iterations: 100, body_steps: [], delay_ms: 0 }),
  },
};

const COMPARE_OPERATORS = ["equals", "not_equals", "contains", "regex", "greater_than", "less_than"];

Object.entries(STEP_TEMPLATES).forEach(([key, def]) => {
  const opt = document.createElement("option");
  opt.value = key;
  opt.textContent = def.label;
  addStepType.appendChild(opt);
});

let editingMacroId = null;
let editingSteps = [];

async function openEditor(id, name) {
  try {
    const res = await fetch(`/api/macros/${id}`);
    const macro = await res.json();
    if (!res.ok) {
      showNote(libraryNote, macro.detail || "Could not load macro.", "error");
      return;
    }
    editingMacroId = id;
    editingSteps = (macro.steps || []).map((s) => JSON.parse(JSON.stringify(s))); // working copy
    editorMacroName.textContent = name;
    macroEditorPanel.style.display = "block";
    hideNote(editorNote);
    renderEditorSteps();
    loadVideoSettings(macro.video || {});
    macroEditorPanel.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    showNote(libraryNote, `Could not reach the agent: ${err.message}`, "error");
  }
}

// -- video recording settings (Phase 10) ------------------------------------

const videoEnabled = document.getElementById("videoEnabled");
const videoMode = document.getElementById("videoMode");
const videoFps = document.getElementById("videoFps");
const videoWindowRow = document.getElementById("videoWindowRow");
const videoWindowTitle = document.getElementById("videoWindowTitle");
const videoRegionRow = document.getElementById("videoRegionRow");
const videoLeft = document.getElementById("videoLeft");
const videoTop = document.getElementById("videoTop");
const videoWidth = document.getElementById("videoWidth");
const videoHeight = document.getElementById("videoHeight");
const saveVideoSettingsBtn = document.getElementById("saveVideoSettingsBtn");
const videoNote = document.getElementById("videoNote");
const ffmpegStatusEl = document.getElementById("ffmpegStatus");

let ffmpegAvailable = null;

async function checkFfmpegStatus() {
  try {
    const res = await fetch("/api/ffmpeg/status");
    const body = await res.json();
    ffmpegAvailable = body.available;
    ffmpegStatusEl.innerHTML = body.available
      ? ""
      : `&mdash; <span style="color:var(--amber);">ffmpeg not found. <a href="${body.download_url}" target="_blank" rel="noopener" style="color:var(--amber);">Download it</a> and add it to PATH.</span>`;
  } catch {
    // status panel already reports connectivity issues
  }
}

function updateVideoModeRows() {
  videoWindowRow.style.display = videoMode.value === "window" ? "flex" : "none";
  videoRegionRow.style.display = videoMode.value === "region" ? "flex" : "none";
}

function loadVideoSettings(v) {
  videoEnabled.checked = !!v.enabled;
  videoMode.value = v.mode || "fullscreen";
  videoFps.value = v.fps || 10;
  videoWindowTitle.value = v.window_title || "";
  const r = v.region || {};
  videoLeft.value = r.left ?? 0;
  videoTop.value = r.top ?? 0;
  videoWidth.value = r.width ?? 800;
  videoHeight.value = r.height ?? 600;
  hideNote(videoNote);
  updateVideoModeRows();
}

videoMode.addEventListener("change", updateVideoModeRows);

saveVideoSettingsBtn.addEventListener("click", async () => {
  if (!editingMacroId) return;
  if (videoEnabled.checked && ffmpegAvailable === false) {
    showNote(videoNote, "ffmpeg isn't installed -- video won't record until it is (see link above).", "warn");
  }
  const body = {
    enabled: videoEnabled.checked,
    mode: videoMode.value,
    fps: Number(videoFps.value) || 10,
    window_title: videoWindowTitle.value || null,
    region: { left: Number(videoLeft.value) || 0, top: Number(videoTop.value) || 0, width: Number(videoWidth.value) || 800, height: Number(videoHeight.value) || 600 },
  };
  try {
    const res = await fetch(`/api/macros/${editingMacroId}/video`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const responseBody = await res.json();
    if (!res.ok) {
      showNote(videoNote, responseBody.detail || `Unexpected error (HTTP ${res.status})`, "error");
      return;
    }
    showNote(videoNote, "Video settings saved.", "info");
    loadMacros();
  } catch (err) {
    showNote(videoNote, `Could not reach the agent: ${err.message}`, "error");
  }
});

function editorInput(type, value, onChange, width) {
  const input = document.createElement("input");
  input.type = type;
  input.className = "editor-input";
  input.value = value ?? (type === "number" ? 0 : "");
  if (width) input.style.width = width;
  input.addEventListener("input", () => onChange(type === "number" ? Number(input.value) || 0 : input.value));
  return input;
}

function editorCheckbox(checked, onChange) {
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = !!checked;
  input.addEventListener("change", () => onChange(input.checked));
  return input;
}

function editorSelect(value, options, onChange) {
  const select = document.createElement("select");
  select.className = "editor-input";
  options.forEach((opt) => {
    const o = document.createElement("option");
    o.value = opt;
    o.textContent = opt;
    if (opt === value) o.selected = true;
    select.appendChild(o);
  });
  select.addEventListener("change", () => onChange(select.value));
  return select;
}

function renderEditorSteps() {
  renderStepArrayInto(editingSteps, editorStepList);
}

function renderStepArrayInto(stepsArray, containerEl) {
  containerEl.innerHTML = "";
  if (stepsArray.length === 0) {
    containerEl.innerHTML = `<li class="empty-state" style="border:none;">No steps here yet.</li>`;
    return;
  }
  stepsArray.forEach((step, i) => containerEl.appendChild(buildStepRow(step, i, stepsArray, containerEl)));
}

function mkActionBtn(label, title, disabled, onClick) {
  const btn = document.createElement("button");
  btn.textContent = label;
  if (title) btn.title = title;
  btn.disabled = !!disabled;
  btn.addEventListener("click", onClick);
  return btn;
}

function buildNestedList(label, stepsArray) {
  const wrap = document.createElement("div");
  wrap.className = "nested-block";

  const heading = document.createElement("div");
  heading.className = "nested-block-label";
  heading.textContent = label;
  wrap.appendChild(heading);

  const list = document.createElement("ol");
  list.className = "step-list editable nested";
  wrap.appendChild(list);
  renderStepArrayInto(stepsArray, list);

  const toolbar = document.createElement("div");
  toolbar.className = "record-row";
  toolbar.style.margin = "6px 0";
  const select = document.createElement("select");
  select.className = "editor-input";
  Object.entries(STEP_TEMPLATES).forEach(([key, def]) => {
    const opt = document.createElement("option");
    opt.value = key;
    opt.textContent = def.label;
    select.appendChild(opt);
  });
  const addBtn = mkActionBtn("+ Add Step", "", false, () => {
    stepsArray.push(STEP_TEMPLATES[select.value].make());
    renderStepArrayInto(stepsArray, list);
  });
  toolbar.append(select, addBtn);
  wrap.appendChild(toolbar);

  return wrap;
}

function buildStepRow(step, index, stepsArray, containerEl) {
  const li = document.createElement("li");
  li.style.flexDirection = "column";
  li.style.alignItems = "stretch";

  const header = document.createElement("div");
  header.style.display = "flex";
  header.style.alignItems = "center";
  header.style.gap = "8px";
  header.style.flexWrap = "wrap";

  const seqSpan = document.createElement("span");
  seqSpan.className = "step-seq";
  seqSpan.textContent = `#${index}`;

  const typeSpan = document.createElement("span");
  typeSpan.className = "step-type";
  typeSpan.textContent = typeLabel(step.type);

  const fields = document.createElement("span");
  fields.className = "step-fields";

  const addField = (label, el) => {
    const wrap = document.createElement("span");
    wrap.append(`${label}:`, el);
    fields.appendChild(wrap);
  };

  if (step.type === "conditional") {
    addField("variable", editorInput("text", step.variable, (v) => (step.variable = v), "90px"));
    addField("op", editorSelect(step.operator, COMPARE_OPERATORS, (v) => (step.operator = v)));
    addField("value", editorInput("text", step.value, (v) => (step.value = v), "100px"));
  } else if (step.type === "loop") {
    addField("mode", editorSelect(step.mode, ["count", "until"], (v) => {
      step.mode = v;
      renderStepArrayInto(stepsArray, containerEl);
    }));
    if (step.mode === "count") {
      addField("count", editorInput("number", step.count, (v) => (step.count = v), "60px"));
    } else {
      addField("variable", editorInput("text", step.variable, (v) => (step.variable = v), "90px"));
      addField("op", editorSelect(step.operator, COMPARE_OPERATORS, (v) => (step.operator = v)));
      addField("value", editorInput("text", step.value, (v) => (step.value = v), "100px"));
      addField("max iterations", editorInput("number", step.max_iterations, (v) => (step.max_iterations = v), "70px"));
    }
  } else if (step.type === "click" || step.type === "double_click") {
    step.semantic = step.semantic || {};
    step.semantic.target = step.semantic.target || {};
    addField("name", editorInput("text", step.semantic.target.name, (v) => (step.semantic.target.name = v), "110px"));
    addField("automation_id", editorInput("text", step.semantic.target.automation_id, (v) => (step.semantic.target.automation_id = v), "100px"));
    addField("x", editorInput("number", step.x, (v) => (step.x = v), "60px"));
    addField("y", editorInput("number", step.y, (v) => (step.y = v), "60px"));
  } else if (step.type === "key" || step.type === "hotkey") {
    addField("keys", editorInput("text", (step.keys || []).join("+"), (v) => {
      step.keys = v.split("+").map((k) => k.trim()).filter(Boolean);
    }, "140px"));
  } else if (step.type === "scroll") {
    addField("dx", editorInput("number", step.dx, (v) => (step.dx = v), "60px"));
    addField("dy", editorInput("number", step.dy, (v) => (step.dy = v), "60px"));
  } else if (step.type === "wait") {
    addField("duration (ms)", editorInput("number", step.duration_ms, (v) => (step.duration_ms = v), "80px"));
  } else if (step.type === "wait_for_element" || step.type === "wait_for_text" || step.type === "read_control_value") {
    step.target = step.target || {};
    addField("window title (blank=last click)", editorInput("text", step.window_title, (v) => (step.window_title = v), "150px"));
    addField("element name", editorInput("text", step.target.name, (v) => (step.target.name = v), "110px"));
    addField("automation_id", editorInput("text", step.target.automation_id, (v) => (step.target.automation_id = v), "100px"));
    if (step.type !== "read_control_value") {
      addField("control_type", editorInput("text", step.target.control_type, (v) => (step.target.control_type = v), "90px"));
    }
    if (step.type === "wait_for_text") {
      addField("expected text/regex", editorInput("text", step.expected, (v) => (step.expected = v), "130px"));
      addField("is regex", editorCheckbox(step.is_regex, (v) => (step.is_regex = v)));
    }
    if (step.type !== "read_control_value") {
      addField("timeout (ms)", editorInput("number", step.timeout_ms, (v) => (step.timeout_ms = v), "80px"));
    } else {
      addField("store as", editorInput("text", step.store_as, (v) => (step.store_as = v), "80px"));
    }
  } else if (step.type === "find_click_text") {
    addField("window title (blank=last click)", editorInput("text", step.window_title, (v) => (step.window_title = v), "150px"));
    addField("visible text", editorInput("text", step.text, (v) => (step.text = v), "140px"));
    addField("exact match", editorCheckbox(step.exact, (v) => (step.exact = v)));
  } else if (step.type === "open_url") {
    addField("URL", editorInput("text", step.url, (v) => (step.url = v), "220px"));
  } else if (step.type === "file_search") {
    addField("folder", editorInput("text", step.folder, (v) => (step.folder = v), "160px"));
    addField("pattern", editorInput("text", step.pattern, (v) => (step.pattern = v), "80px"));
    addField("recursive", editorCheckbox(step.recursive, (v) => (step.recursive = v)));
    addField("store as", editorInput("text", step.store_as, (v) => (step.store_as = v), "80px"));
  } else if (step.type === "file_op") {
    addField("operation", editorSelect(step.operation, ["copy", "move", "rename", "delete"], (v) => (step.operation = v)));
    addField("source", editorInput("text", step.source, (v) => (step.source = v), "160px"));
    addField("destination", editorInput("text", step.destination, (v) => (step.destination = v), "160px"));
    addField("overwrite", editorCheckbox(step.overwrite, (v) => (step.overwrite = v)));
  } else if (step.type === "clipboard") {
    addField("mode", editorSelect(step.mode, ["write", "read"], (v) => {
      step.mode = v;
      renderStepArrayInto(stepsArray, containerEl);
    }));
    if (step.mode === "write") {
      addField("value", editorInput("text", step.value, (v) => (step.value = v), "160px"));
    } else {
      addField("store as", editorInput("text", step.store_as, (v) => (step.store_as = v), "80px"));
    }
  } else if (step.type === "get_cursor_position") {
    addField("store as", editorInput("text", step.store_as, (v) => (step.store_as = v), "80px"));
  }
  addField("delay before (ms)", editorInput("number", step.delay_ms, (v) => (step.delay_ms = v), "80px"));

  const actions = document.createElement("span");
  actions.className = "step-actions";

  const upBtn = mkActionBtn("↑", "Move up", index === 0, () => {
    moveInArray(stepsArray, index, -1);
    renderStepArrayInto(stepsArray, containerEl);
  });
  const downBtn = mkActionBtn("↓", "Move down", index === stepsArray.length - 1, () => {
    moveInArray(stepsArray, index, 1);
    renderStepArrayInto(stepsArray, containerEl);
  });
  const delBtn = mkActionBtn("Delete", "", false, () => {
    stepsArray.splice(index, 1);
    renderStepArrayInto(stepsArray, containerEl);
  });

  actions.append(upBtn, downBtn, delBtn);
  header.append(seqSpan, typeSpan, fields, actions);
  li.appendChild(header);

  if (step.type === "conditional") {
    step.then_steps = step.then_steps || [];
    step.else_steps = step.else_steps || [];
    li.appendChild(buildNestedList("Then:", step.then_steps));
    li.appendChild(buildNestedList("Else:", step.else_steps));
  } else if (step.type === "loop") {
    step.body_steps = step.body_steps || [];
    li.appendChild(buildNestedList("Loop body:", step.body_steps));
  }

  return li;
}

function moveInArray(arr, index, dir) {
  const target = index + dir;
  if (target < 0 || target >= arr.length) return;
  [arr[index], arr[target]] = [arr[target], arr[index]];
}

addStepBtn.addEventListener("click", () => {
  const template = STEP_TEMPLATES[addStepType.value];
  if (!template) return;
  editingSteps.push(template.make());
  renderEditorSteps();
});

cancelEditorBtn.addEventListener("click", () => {
  macroEditorPanel.style.display = "none";
  editingMacroId = null;
  editingSteps = [];
});

saveEditorBtn.addEventListener("click", async () => {
  if (!editingMacroId) return;
  try {
    const res = await fetch(`/api/macros/${editingMacroId}/steps`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ steps: editingSteps }),
    });
    const body = await res.json();
    if (!res.ok) {
      showNote(editorNote, body.detail || `Unexpected error (HTTP ${res.status})`, "error");
      return;
    }
    showNote(editorNote, "Saved.", "info");
    loadMacros();
  } catch (err) {
    showNote(editorNote, `Could not reach the agent: ${err.message}`, "error");
  }
});

pollStatus();
setInterval(pollStatus, 5000);
loadRecordingState();
loadHotkey();
loadMacros();
checkFfmpegStatus();
connectWebSocket();
