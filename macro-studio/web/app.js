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
const replaySummary = document.getElementById("replaySummary");
const replayNote = document.getElementById("replayNote");
const runResultListEl = document.getElementById("runResultList");
const saveMacroBtn = document.getElementById("saveMacroBtn");
const discardMacroBtn = document.getElementById("discardMacroBtn");
const saveMacroForm = document.getElementById("saveMacroForm");
const macroNameInput = document.getElementById("macroNameInput");
const confirmSaveMacroBtn = document.getElementById("confirmSaveMacroBtn");
const cancelSaveMacroBtn = document.getElementById("cancelSaveMacroBtn");
const openCdpBtn = document.getElementById("openCdpBtn");
const closeCdpBtn = document.getElementById("closeCdpBtn");
const reloadCdpBtn = document.getElementById("reloadCdpBtn");
const hideCdpBtn = document.getElementById("hideCdpBtn");
const showCdpBtn = document.getElementById("showCdpBtn");
const webRecordBtn = document.getElementById("webRecordBtn");
const webRecordStopBtn = document.getElementById("webRecordStopBtn");
const webRecordUrl = document.getElementById("webRecordUrl");
const webRecordNote = document.getElementById("webRecordNote");
const libraryNote = document.getElementById("libraryNote");
const viewerNote = document.getElementById("viewerNote");
const viewerEmptyState = document.getElementById("viewerEmptyState");
const macroViewer = document.getElementById("macroViewer");
const addCategoryBtn = document.getElementById("addCategoryBtn");
const uploadMacroBtn = document.getElementById("uploadMacroBtn");
const uploadMacroInput = document.getElementById("uploadMacroInput");
const uploadNote = document.getElementById("uploadNote");
const copyGuideList = document.getElementById("copyGuideList");
const macrosDirPath = document.getElementById("macrosDirPath");
const copyMacrosDirBtn = document.getElementById("copyMacrosDirBtn");
const editLibList = document.getElementById("editLibList");
const editLibSearch = document.getElementById("editLibSearch");
const viewerScreen = document.getElementById("viewerScreen");
const editScreen = document.getElementById("editScreen");
const editMacrosBtn = document.getElementById("editMacrosBtn");
const backToViewerBtn = document.getElementById("backToViewerBtn");
const editorEmpty = document.getElementById("editorEmpty");
const logPanel = document.getElementById("logPanel");
const logList = document.getElementById("logList");
const logTitle = document.getElementById("logTitle");
const logChip = document.getElementById("logChip");
const logPip = document.getElementById("logPip");
const logNote = document.getElementById("logNote");
const logFoot = document.getElementById("logFoot");
const logBubble = document.getElementById("logBubble");
const bubbleCount = document.getElementById("bubbleCount");
const logMinimiseBtn = document.getElementById("logMinimiseBtn");
const stopRunBtn = document.getElementById("stopRunBtn");
const checkOverlay = document.getElementById("checkOverlay");
const checkRunBtn = document.getElementById("checkRunBtn");
const checkCloseBtn = document.getElementById("checkCloseBtn");

let recordingState = "idle"; // idle | recording | paused | stopped
let macros = [];
// null | "last" | macro id -- which list run_step_result WS events append to
let activeRunTarget = null;
let runStepsSeen = 0;
let runStepsTotal = 0;

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
    find_click_text: "Find & click text", open_url: "Open URL", open_file: "Open file",
    file_search: "File search", sheet_read: "Read spreadsheet", file_wait: "Wait for file",
    web_download: "Web: download file",
    file_op: "File op", clipboard: "Clipboard", keyboard_shortcut: "Shortcut",
    get_cursor_position: "Cursor pos", read_control_value: "Read value",
    conditional: "If/Else", loop: "Loop",
    cdp_launch: "Launch Chrome (CDP)", cdp_close: "Close Chrome (CDP)",
    web_goto: "Web: go to", web_click: "Web: click",
    web_hover: "Web: hover",
    web_wait_for: "Web: wait for", web_type: "Web: type", web_upload: "Web: upload file",
    web_drop_files: "Web: drop files on",
    web_read: "Web: read",
    web_print_pdf: "Web: save PDF", web_switch_tab: "Web: follow tab", web_show_tab: "Web: show tab", web_close_tab: "Web: close tab",
    web_wait_loaded: "Web: wait until loaded", web_reload: "Web: refresh",
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

openCdpBtn.addEventListener("click", async () => {
  openCdpBtn.disabled = true;
  try {
    const res = await fetch("/api/cdp/launch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: webRecordUrl.value.trim() }),
    });
    const body = await res.json();
    showNote(webRecordNote, body.detail || `Unexpected error (HTTP ${res.status})`, res.ok ? "info" : "error");
  } catch (err) {
    showNote(webRecordNote, `Could not reach the agent: ${err.message}`, "error");
  } finally {
    openCdpBtn.disabled = false;
  }
});

closeCdpBtn.addEventListener("click", async () => {
  closeCdpBtn.disabled = true;
  try {
    const res = await fetch("/api/cdp/close", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const body = await res.json();
    showNote(webRecordNote, body.detail || `Unexpected error (HTTP ${res.status})`, res.ok ? "info" : "error");
  } catch (err) {
    showNote(webRecordNote, `Could not reach the agent: ${err.message}`, "error");
  } finally {
    closeCdpBtn.disabled = false;
  }
});

// Minimising the debugging browser stops it rendering, and a page that
// isn't rendering can't open a hover menu -- so "get it out of the way"
// means off-screen here, not minimised.
[[hideCdpBtn, "/api/cdp/hide"], [showCdpBtn, "/api/cdp/show"], [reloadCdpBtn, "/api/cdp/reload"]].forEach(([btn, path]) => {
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      const res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const body = await res.json();
      showNote(webRecordNote, body.detail || `Unexpected error (HTTP ${res.status})`, res.ok ? "info" : "error");
    } catch (err) {
      showNote(webRecordNote, `Could not reach the agent: ${err.message}`, "error");
    } finally {
      btn.disabled = false;
    }
  });
});

function applyWebRecordingState(state) {
  const recording = state === "recording";
  webRecordBtn.disabled = recording;
  webRecordStopBtn.disabled = !recording;
  webRecordBtn.classList.toggle("recording", recording);
}

webRecordBtn.addEventListener("click", async () => {
  try {
    const res = await fetch("/api/web-recording/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: webRecordUrl.value.trim() }),
    });
    const body = await res.json();
    if (!res.ok) {
      showNote(webRecordNote, body.detail || `Unexpected error (HTTP ${res.status})`, "error");
      return;
    }
    clearSteps();
    (body.steps || []).forEach(appendStep);
    applyWebRecordingState("recording");
    showNote(webRecordNote, "Recording that browser. Click through the page -- steps appear above.", "info");
  } catch (err) {
    showNote(webRecordNote, `Could not reach the agent: ${err.message}`, "error");
  }
});

webRecordStopBtn.addEventListener("click", async () => {
  try {
    const res = await fetch("/api/web-recording/stop", { method: "POST" });
    const body = await res.json();
    if (!res.ok) {
      showNote(webRecordNote, body.detail || `Unexpected error (HTTP ${res.status})`, "error");
      return;
    }
    applyWebRecordingState("stopped");
    applyRecordingState("stopped");
    showNote(webRecordNote, `Captured ${(body.steps || []).length} steps -- Save them above, or Replay to test.`, "info");
  } catch (err) {
    showNote(webRecordNote, `Could not reach the agent: ${err.message}`, "error");
  }
});

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

// -- the macro viewer (Phase 11) --------------------------------------------
// A macro here is a name and a play button. Everything else -- steps, video,
// rename, delete -- lives behind "Edit macros", because the list you look at
// twenty times a morning should be the short one.

let layout = { categories: [], placement: {} };
// Set once the first fetch has landed, so a later refresh of the list
// (after a run, after a delete) does not snap open categories shut again.
let layoutSeen = false;

function placementOf(id) {
  return layout.placement[id] || { category: "", order: null };
}

async function loadLayout() {
  try {
    const res = await fetch("/api/library/layout");
    if (res.ok) {
      layout = await res.json();
      // Collapsed on every visit, not just the first: the point of the
      // viewer is that the page opens as a short list of headings. What you
      // left open last night is not a thing to inherit this morning.
      if (!layoutSeen) layout.categories.forEach((c) => { c.collapsed = true; });
      layoutSeen = true;
    }
  } catch {
    // The arrangement is a convenience; a macro list with no categories is
    // still a usable macro list, so a failure here must not empty the page.
  }
}

async function saveLayout() {
  try {
    const res = await fetch("/api/library/layout", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ categories: layout.categories, placement: layout.placement }),
    });
    if (res.ok) layout = await res.json();
  } catch (err) {
    showNote(viewerNote, `Could not save the arrangement: ${err.message}`, "error");
  }
}

function macrosIn(categoryId) {
  // A macro nobody has dragged yet has no order at all, and those come
  // first, newest first -- so the one you saved a minute ago is at the top
  // where you are looking for it, rather than alphabetically buried.
  const rank = (m) => placementOf(m.id).order;
  return macros
    .filter((m) => placementOf(m.id).category === categoryId)
    .sort((a, b) => {
      const ra = rank(a), rb = rank(b);
      if (ra === null && rb === null) {
        return String(b.created_at || "").localeCompare(String(a.created_at || ""))
               || a.name.localeCompare(b.name);
      }
      if (ra === null) return -1;
      if (rb === null) return 1;
      return (ra - rb) || a.name.localeCompare(b.name);
    });
}

// Drag state lives up here because the drop can land on a row in one
// category or on the heading of another, and both need to know what moved.
let dragMacroId = null;

function renderViewer() {
  macroViewer.innerHTML = "";
  viewerEmptyState.style.display = macros.length === 0 ? "block" : "none";

  // Named categories first, in the order they were made; the fallback drawer
  // last, so a tidy library ends with whatever hasn't been filed.
  const groups = layout.categories.map((c) => ({ ...c, uncategorised: false }));
  groups.push({ id: "", name: "Uncategorised", collapsed: true, uncategorised: true });

  groups.forEach((group) => {
    const inside = macrosIn(group.id);
    // An empty named category still shows -- it is the thing you drag onto.
    if (group.uncategorised && inside.length === 0 && layout.categories.length) return;

    const box = document.createElement("div");
    box.className = "cat";
    box.dataset.category = group.id;

    const head = document.createElement("div");
    head.className = "cat-head" + (group.collapsed ? "" : " open");
    head.innerHTML = `<span class="chev">&#9654;</span>
      <span class="nm"></span><span class="n">${inside.length}</span>`;
    head.querySelector(".nm").textContent = group.name;

    if (!group.uncategorised) {
      const tools = document.createElement("span");
      tools.className = "tools";
      const rename = mkActionBtn("Rename", "Rename this category", false, async (event) => {
        event.stopPropagation();
        const name = window.prompt("Category name:", group.name);
        if (!name || !name.trim()) return;
        const cat = layout.categories.find((c) => c.id === group.id);
        cat.name = name.trim();
        await saveLayout();
        renderAll();
      });
      const remove = mkActionBtn("Delete", "Delete the category -- its macros are kept", false, async (event) => {
        event.stopPropagation();
        if (!window.confirm(`Delete the category "${group.name}"?\n\nThe ${inside.length} macro(s) in it are kept -- they move to Uncategorised.`)) return;
        layout.categories = layout.categories.filter((c) => c.id !== group.id);
        inside.forEach((m) => { layout.placement[m.id] = { category: "", order: 0 }; });
        await saveLayout();
        renderAll();
      });
      rename.className = "btn-xs";
      remove.className = "btn-xs danger";
      tools.append(rename, remove);
      head.appendChild(tools);
    }

    head.addEventListener("click", async () => {
      if (group.uncategorised) {
        // Not stored: there is no row for the fallback drawer, so it only
        // opens and closes for as long as the page is open.
        box.classList.toggle("open-local");
        body.style.display = box.classList.contains("open-local") ? "" : "none";
        head.classList.toggle("open");
        return;
      }
      const cat = layout.categories.find((c) => c.id === group.id);
      cat.collapsed = !cat.collapsed;
      await saveLayout();
      renderAll();
    });

    const body = document.createElement("div");
    body.className = "cat-body";
    if (group.collapsed && !group.uncategorised) body.style.display = "none";
    if (group.uncategorised) body.style.display = "none";

    if (inside.length === 0) {
      body.innerHTML = `<div class="empty-state">Drag a macro here.</div>`;
    } else {
      inside.forEach((m) => body.appendChild(macroRow(m, group.id)));
    }

    // Dropping on the heading files a macro under it, which is the gesture
    // that works whether or not the category is open.
    head.addEventListener("dragover", (event) => {
      if (!dragMacroId) return;
      event.preventDefault();
      box.classList.add("drop-target");
    });
    head.addEventListener("dragleave", () => box.classList.remove("drop-target"));
    head.addEventListener("drop", async (event) => {
      if (!dragMacroId) return;
      event.preventDefault();
      box.classList.remove("drop-target");
      layout.placement[dragMacroId] = { category: group.id, order: macrosIn(group.id).length };
      dragMacroId = null;
      await saveLayout();
      renderAll();
    });

    box.append(head, body);
    macroViewer.appendChild(box);
  });
}

function macroRow(macro, categoryId) {
  const row = document.createElement("div");
  row.className = "macro-row";
  row.dataset.id = macro.id;
  if (activeRunTarget === macro.id) row.classList.add("running");

  const grip = document.createElement("span");
  grip.className = "grip";
  grip.textContent = "⠿";
  grip.title = "Drag to reorder, or onto a category";
  grip.draggable = true;
  grip.addEventListener("dragstart", (event) => {
    dragMacroId = macro.id;
    row.classList.add("dragging");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", macro.id);  // Firefox wants a payload
  });
  grip.addEventListener("dragend", () => {
    row.classList.remove("dragging");
    dragMacroId = null;
  });

  const name = document.createElement("span");
  name.className = "nm";
  name.textContent = macro.name;
  name.title = `${macro.step_count} step(s) · ${formatResult(macro.last_result)} · ${formatTimestamp(macro.last_run)}`;

  const play = document.createElement("button");
  play.className = "play";
  play.textContent = "▶";
  play.title = `Run ${macro.name}`;
  play.addEventListener("click", () => runMacro(macro.id, macro.name, false));

  row.addEventListener("dragover", (event) => {
    if (!dragMacroId || dragMacroId === macro.id) return;
    event.preventDefault();
    const box = row.getBoundingClientRect();
    row.classList.toggle("drop-after", event.clientY > box.top + box.height / 2);
    row.classList.toggle("drop-before", event.clientY <= box.top + box.height / 2);
  });
  row.addEventListener("dragleave", () => row.classList.remove("drop-before", "drop-after"));
  row.addEventListener("drop", async (event) => {
    if (!dragMacroId || dragMacroId === macro.id) return;
    event.preventDefault();
    const after = row.classList.contains("drop-after");
    row.classList.remove("drop-before", "drop-after");
    // Renumber the whole category rather than nudging one value: orders that
    // are only ever incremented drift into ties, and ties sort at random.
    const order = macrosIn(categoryId).filter((m) => m.id !== dragMacroId);
    const at = order.findIndex((m) => m.id === macro.id) + (after ? 1 : 0);
    order.splice(Math.max(0, at), 0, { id: dragMacroId });
    order.forEach((m, i) => { layout.placement[m.id] = { category: categoryId, order: i }; });
    dragMacroId = null;
    await saveLayout();
    renderAll();
  });

  row.append(grip, name, play);
  return row;
}

if (addCategoryBtn) {
  addCategoryBtn.addEventListener("click", async () => {
    const name = window.prompt("Name the category:");
    if (!name || !name.trim()) return;
    layout.categories.push({
      id: freshStepId(), name: name.trim(), collapsed: false,
    });
    await saveLayout();
    renderAll();
  });
}

// -- copying a macro between machines ---------------------------------------
// The folder comes from the agent rather than being written into the page:
// it is the actual path on THIS machine, which is the whole point of showing
// it -- a guessed path helps nobody standing in front of Explorer.

let macrosDir = "";

function setMacrosDir(dir) {
  macrosDir = dir || "";
  if (macrosDirPath && macrosDir) macrosDirPath.textContent = macrosDir;
}

function renderCopyGuide() {
  if (!copyGuideList) return;
  if (macros.length === 0) {
    copyGuideList.innerHTML = '<div class="empty-state">No macros here yet to copy.</div>';
    return;
  }
  copyGuideList.innerHTML = "";
  [...macros].sort((a, b) => a.name.localeCompare(b.name)).forEach((m) => {
    const row = document.createElement("div");
    row.className = "cg-row";
    const nm = document.createElement("span");
    nm.className = "cg-name";
    nm.textContent = m.name;
    const file = document.createElement("code");
    file.className = "mono cg-file";
    file.textContent = `${m.id}.json`;
    const btn = document.createElement("button");
    btn.className = "btn-sm";
    btn.textContent = "Copy filename";
    btn.addEventListener("click", () => copyText(`${m.id}.json`, btn));
    // For sending one on: a colleague needs the file, and digging it out
    // of the folder is the step that goes wrong -- wrong macro, or the
    // versions folder by mistake.
    const dl = document.createElement("a");
    dl.className = "btn-sm cg-dl";
    dl.textContent = "Download";
    dl.href = `/api/macros/${m.id}/file`;
    dl.download = `${m.id}.json`;
    row.append(nm, file, btn, dl);
    copyGuideList.append(row);
  });
}

async function copyText(text, btn) {
  const was = btn.textContent;
  try {
    await navigator.clipboard.writeText(text);
    btn.textContent = "Copied";
  } catch (err) {
    btn.textContent = "Press Ctrl+C";
    window.prompt("Copy this:", text);
  }
  setTimeout(() => { btn.textContent = was; }, 1500);
}

if (copyMacrosDirBtn) {
  copyMacrosDirBtn.addEventListener("click", () => copyText(macrosDirPath.textContent, copyMacrosDirBtn));
}

if (uploadMacroBtn) {
  uploadMacroBtn.addEventListener("click", () => uploadMacroInput.click());

  uploadMacroInput.addEventListener("change", async () => {
    const picked = [...uploadMacroInput.files];
    // Cleared straight away: picking the same file twice in a row is a
    // normal thing to do after fixing it, and an unchanged input fires
    // no change event.
    uploadMacroInput.value = "";
    if (picked.length === 0) return;

    uploadMacroBtn.disabled = true;
    showNote(uploadNote, `Reading ${picked.length} file${picked.length === 1 ? "" : "s"}...`, "info");
    try {
      const files = await Promise.all(picked.map(async (f) => ({
        filename: f.name, content: await f.text(),
      })));
      const res = await fetch("/api/macros/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ files }),
      });
      const body = await res.json();
      if (!res.ok) {
        showNote(uploadNote, body.detail || `Unexpected error (HTTP ${res.status})`, "error");
        return;
      }
      await loadMacros();
      showNote(uploadNote, summariseImport(body), body.failed ? "error" : "info");
    } catch (err) {
      showNote(uploadNote, `Could not upload: ${err.message}`, "error");
    } finally {
      uploadMacroBtn.disabled = false;
    }
  });
}

function summariseImport(body) {
  const parts = [];
  if (body.updated) parts.push(`${body.updated} updated to the newer file`);
  if (body.added) parts.push(`${body.added} added`);
  const bad = body.results.filter((r) => !r.ok);
  if (bad.length) parts.push(bad.map((r) => `${r.filename}: ${r.detail}`).join(" | "));
  const named = body.results.filter((r) => r.ok).map((r) => r.name);
  if (named.length) parts.push(`(${named.join(", ")})`);
  return parts.length ? parts.join(" -- ") : "Nothing was imported.";
}

// -- the edit screen's library ----------------------------------------------

function renderEditLibrary() {
  const needle = (editLibSearch.value || "").trim().toLowerCase();
  const shown = macros
    .filter((m) => !needle || m.name.toLowerCase().includes(needle))
    .sort((a, b) => a.name.localeCompare(b.name));

  editLibList.innerHTML = "";
  if (shown.length === 0) {
    editLibList.innerHTML = `<div class="empty-state">${
      needle ? "Nothing matches that." : "No macros yet."}</div>`;
    return;
  }

  shown.forEach((m) => {
    const item = document.createElement("div");
    item.className = "m" + (editingMacroId === m.id ? " on" : "");
    const top = document.createElement("div");
    top.className = "top";
    const nm = document.createElement("span");
    nm.className = "nm";
    nm.textContent = m.name;
    const n = document.createElement("span");
    n.className = "n";
    n.textContent = m.step_count;
    top.append(nm, n);
    item.appendChild(top);

    const tools = document.createElement("div");
    tools.className = "tools";
    tools.append(
      mkActionBtn("Rename", "", false, (e) => { e.stopPropagation(); renameMacro(m); }),
      mkActionBtn("Duplicate", "", false, (e) => { e.stopPropagation(); duplicateMacro(m); }),
      mkActionBtn("Delete", "", false, (e) => { e.stopPropagation(); deleteMacro(m); }),
    );
    [...tools.children].forEach((b, i) => { b.className = i === 2 ? "btn-xs danger" : "btn-xs"; });
    item.appendChild(tools);

    item.addEventListener("click", () => openEditor(m.id, m.name));
    editLibList.appendChild(item);
  });
}

if (editLibSearch) editLibSearch.addEventListener("input", renderEditLibrary);

async function renameMacro(macro) {
  const name = window.prompt("New name:", macro.name);
  if (!name || !name.trim() || name.trim() === macro.name) return;
  const res = await fetch(`/api/macros/${macro.id}/rename`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name.trim() }),
  });
  const body = await res.json();
  if (!res.ok) showNote(libraryNote, body.detail || "Rename failed.", "error");
  else {
    hideNote(libraryNote);
    if (editingMacroId === macro.id) editorMacroName.textContent = name.trim();
  }
  loadMacros();
}

async function duplicateMacro(macro) {
  const res = await fetch(`/api/macros/${macro.id}/duplicate`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
  });
  const body = await res.json();
  if (!res.ok) showNote(libraryNote, body.detail || "Duplicate failed.", "error");
  else hideNote(libraryNote);
  loadMacros();
}

async function deleteMacro(macro) {
  if (!window.confirm(`Delete macro "${macro.name}"? This can't be undone.`)) return;
  const res = await fetch(`/api/macros/${macro.id}`, { method: "DELETE" });
  if (!res.ok) {
    const body = await res.json();
    showNote(libraryNote, body.detail || "Delete failed.", "error");
  } else {
    hideNote(libraryNote);
    if (editingMacroId === macro.id) closeEditor();
  }
  loadMacros();
}

function renderAll() {
  renderViewer();
  renderEditLibrary();
  renderCopyGuide();
}

async function loadMacros() {
  try {
    const res = await fetch("/api/macros");
    macros = await res.json();
    await loadLayout();
    renderAll();
  } catch (err) {
    showNote(viewerNote, `Could not reach the agent: ${err.message}`, "error");
  }
}

// -- which screen is showing -------------------------------------------------

function showEditScreen(on) {
  viewerScreen.style.display = on ? "none" : "";
  editScreen.style.display = on ? "" : "none";
  editMacrosBtn.style.display = on ? "none" : "";
  backToViewerBtn.style.display = on ? "" : "none";
  document.body.classList.toggle("editing", on);
  document.getElementById("app").classList.toggle("editing", on);
  if (on) renderEditLibrary();
}

if (editMacrosBtn) {
  editMacrosBtn.addEventListener("click", () => showEditScreen(true));
  backToViewerBtn.addEventListener("click", () => {
    stopEditorRecordingIfRunning();
    showEditScreen(false);
  });
}

// -- Check a run, over the page ---------------------------------------------

function openCheckOverlay(on) {
  checkOverlay.style.display = on ? "flex" : "none";
  if (on) loadChecks();
}

if (checkRunBtn) {
  checkRunBtn.addEventListener("click", () => openCheckOverlay(true));
  checkCloseBtn.addEventListener("click", () => openCheckOverlay(false));
  checkOverlay.addEventListener("click", (event) => {
    if (event.target === checkOverlay) openCheckOverlay(false);
  });
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && checkOverlay.style.display === "flex") openCheckOverlay(false);
});

// -- Current Logs ------------------------------------------------------------
// It opens itself when a run starts and can be minimised but not closed:
// while something is running this is the only account of what is happening.

let runFinished = false;

function setBubble(state, text) {
  logBubble.className = `log-bubble ${state}`;
  bubbleCount.textContent = text;
}

function openLogs() {
  logPanel.style.display = "flex";
  logBubble.style.display = "none";
}

function minimiseLogs() {
  logPanel.style.display = "none";
  logBubble.style.display = "inline-flex";
}

function dismissLogs() {
  logPanel.style.display = "none";
  logBubble.style.display = "none";
}

if (logMinimiseBtn) {
  logMinimiseBtn.addEventListener("click", () => {
    // Once the run is over the bubble has nothing left to report, so
    // minimising a finished run puts it away for good.
    if (runFinished) dismissLogs();
    else minimiseLogs();
  });
  logBubble.addEventListener("click", openLogs);
  stopRunBtn.addEventListener("click", () => stopActiveReplay(stopRunBtn));
}

async function runMacro(id, name, allowForeground, startAt = 0) {
  activeRunTarget = id;
  runFinished = false;
  logList.innerHTML = "";
  logTitle.textContent = startAt
    ? `Current Logs · ${name} — from #${startAt}`
    : `Current Logs · ${name}`;
  logChip.className = "chip warn";
  logChip.textContent = "running";
  logPip.className = "pip now";
  logFoot.textContent = "";
  stopRunBtn.style.display = "";
  logMinimiseBtn.textContent = "Minimise";
  hideNote(logNote);
  openLogs();
  renderViewer();

  try {
    const res = await fetch(`/api/macros/${id}/replay`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ allow_foreground: allowForeground, start_at: startAt }),
    });
    const body = await res.json();
    if (!res.ok) {
      finishLogs("bad", body.detail || `Unexpected error (HTTP ${res.status})`, "error");
      return;
    }
    const { passed, failed, total, stopped } = body.summary;
    logFoot.textContent = `${passed} passed · ${failed} failed · ${total} steps`;

    if (stopped) {
      finishLogs("bad", `Stopped after ${body.results.length} step(s).`, "warn");
    } else if (failed > 0 && !allowForeground) {
      finishLogs("bad", `${passed}/${total} succeeded, ${failed} failed.`, "warn");
      showForegroundConfirm(logNote, () => runMacro(id, name, true, startAt));
    } else if (failed > 0) {
      finishLogs("bad", `${passed}/${total} succeeded, ${failed} still failed with foreground control.`, "error");
    } else {
      finishLogs("done", `Finished — ${passed}/${total} steps.`, "info");
    }

    if (body.video_error) {
      showNote(logNote, `${logNote.textContent} Video: ${body.video_error}`, "warn");
    } else if (body.video) {
      logNote.insertAdjacentHTML("beforeend",
        ` <a href="${body.video}" target="_blank" rel="noopener" style="color:inherit;">Watch recording</a>`);
    }
    loadMacros();
  } catch (err) {
    finishLogs("bad", `Could not reach the agent: ${err.message}`, "error");
  }
}

function finishLogs(state, message, kind) {
  runFinished = true;
  activeRunTarget = null;
  stopRunBtn.style.display = "none";
  logMinimiseBtn.textContent = "Close";
  logPip.className = `pip ${state === "done" ? "ok" : "no"}`;
  logChip.className = `chip ${state === "done" ? "on" : "bad"}`;
  logChip.textContent = state === "done" ? "finished" : "problems";
  setBubble(state, state === "done" ? "done" : "problems");
  showNote(logNote, message, kind);
  renderViewer();
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
  setMacrosDir(status.macros_dir);

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
        if (editorRecording) {
          editingSteps.push(msg.step);
          renderEditorSteps();
        } else {
          appendStep(msg.step);
        }
        break;
      case "web_recording_state":
        applyWebRecordingState(msg.state);
        break;
      case "hotkey_stop_triggered":
        showNote(recordNote, "Recording stopped via global hotkey.", "info");
        break;
      case "accessibility_warning":
        showNote(recordNote, msg.message, "warn");
        break;
      case "run_step_result":
        renderRunResult(msg.result, activeRunTarget === "last" ? runResultListEl : logList);
        if (activeRunTarget !== "last" && activeRunTarget !== null) {
          runStepsSeen += 1;
          logChip.textContent = `running · ${runStepsSeen} / ${runStepsTotal || "?"}`;
          setBubble("", `${runStepsSeen} / ${runStepsTotal || "?"}`);
        }
        break;
      case "run_state":
        stopReplayBtn.style.display = msg.state === "running" && activeRunTarget === "last" ? "inline-flex" : "none";
        if (msg.state === "running") {
          runStepsSeen = 0;
          runStepsTotal = msg.step_count || 0;
          if (activeRunTarget === "last") replaySummary.textContent = `Running 0/${msg.step_count}...`;
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
  open_url: { label: "Open URL in Chrome", make: () => ({ type: "open_url", url: "https://", new_window: false, delay_ms: 0 }) },
  open_file: {
    label: "Open a file (Excel, PDF, anything)",
    make: () => ({ type: "open_file", path: "", window_title: "", timeout_ms: 20000, delay_ms: 0 }),
  },
  sheet_read: {
    label: "Read a spreadsheet column",
    make: () => ({ type: "sheet_read", path: "", sheet: "", column: "A", first_row: 2, limit: 500, encoding: "", store_as: "rows", delay_ms: 0 }),
  },
  file_wait: {
    label: "Wait for a file to arrive",
    make: () => ({ type: "file_wait", folder: "", pattern: "*.pdf", timeout_ms: 30000, store_as: "downloaded", delay_ms: 0 }),
  },
  file_search: {
    label: "File Explorer search",
    make: () => ({ type: "file_search", folder: "", pattern: "*", recursive: false, newest_first: true, limit: 1, store_as: "file", delay_ms: 0 }),
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
  cdp_close: {
    label: "Web: close Chrome (CDP)",
    make: () => ({ type: "cdp_close", port: 9222, delay_ms: 0 }),
  },
  cdp_launch: {
    label: "Web: launch Chrome (CDP)",
    make: () => ({ type: "cdp_launch", port: 9222, user_data_dir: "", url: "", timeout_ms: 20000, delay_ms: 0 }),
  },
  web_reload: {
    label: "Web: refresh the page",
    make: () => ({ type: "web_reload", port: 9222, tab_match: "", ignore_cache: false, timeout_ms: 20000, delay_ms: 0 }),
  },
  web_goto: {
    label: "Web: go to URL",
    make: () => ({ type: "web_goto", port: 9222, url: "https://", new_tab: true, tab_match: "", timeout_ms: 10000, delay_ms: 0 }),
  },
  web_click: {
    label: "Web: click",
    make: () => ({ type: "web_click", port: 9222, tab_match: "", selector: "", text: "", exact: false, match_index: 0, button: "left", hover_selector: "", hover_text: "", hover_exact: true, until_selector: "", until_text: "", until_exact: false, timeout_ms: 8000, delay_ms: 0 }),
  },
  web_hover: {
    label: "Web: hover (opens hover menus)",
    make: () => ({ type: "web_hover", port: 9222, tab_match: "", selector: "", text: "", exact: false, match_index: 0, timeout_ms: 8000, delay_ms: 0 }),
  },
  web_wait_for: {
    label: "Web: wait for",
    make: () => ({ type: "web_wait_for", port: 9222, tab_match: "", selector: "", text: "", exact: false, timeout_ms: 10000, delay_ms: 0 }),
  },
  web_type: {
    label: "Web: type into field",
    make: () => ({ type: "web_type", port: 9222, tab_match: "", selector: "", value: "", submit: false, timeout_ms: 10000, delay_ms: 0 }),
  },
  web_upload: {
    label: "Web: upload a file",
    make: () => ({ type: "web_upload", port: 9222, tab_match: "", selector: "input[type=file]", files: "", match_index: 0, timeout_ms: 10000, delay_ms: 0 }),
  },
  web_drop_files: {
    label: "Web: drag a file onto the page",
    make: () => ({ type: "web_drop_files", port: 9222, tab_match: "", selector: "", text: "", exact: false, files: "", match_index: 0, timeout_ms: 10000, delay_ms: 0 }),
  },
  web_download: {
    label: "Web: click to download a file",
    make: () => ({ type: "web_download", port: 9222, tab_match: "", selector: "", text: "", exact: false, match_index: 0, folder: "", save_as: "", store_as: "downloaded", timeout_ms: 60000, delay_ms: 0 }),
  },
  web_switch_tab: {
    label: "Web: follow new tab",
    make: () => ({ type: "web_switch_tab", port: 9222, mode: "new", tab_match: "", timeout_ms: 15000, delay_ms: 0 }),
  },
  web_show_tab: {
    label: "Web: bring tab to the front",
    make: () => ({ type: "web_show_tab", port: 9222, tab_match: "", timeout_ms: 8000, delay_ms: 0 }),
  },
  web_close_tab: {
    label: "Web: close tab",
    make: () => ({ type: "web_close_tab", port: 9222, tab_match: "", timeout_ms: 8000, delay_ms: 0 }),
  },
  web_wait_loaded: {
    label: "Web: wait until loaded",
    make: () => ({ type: "web_wait_loaded", port: 9222, tab_match: "", quiet_ms: 800, min_ms: 0, timeout_ms: 30000, delay_ms: 0 }),
  },
  web_print_pdf: {
    label: "Web: save page as PDF",
    make: () => ({ type: "web_print_pdf", port: 9222, tab_match: "",
                   destination: "C:\Users\Admin\Downloads\Page {{date:DD-MM-YYYY}}.pdf",
                   landscape: false, paper: "A4", scale: 1, background: false,
                   margin_inches: 0.4, store_as: "pdf", timeout_ms: 60000, delay_ms: 0 }),
  },
  web_read: {
    label: "Web: read text",
    make: () => ({ type: "web_read", port: 9222, tab_match: "", selector: "", store_as: "value", timeout_ms: 10000, delay_ms: 0 }),
  },
  conditional: {
    label: "Conditional (if/else)",
    make: () => ({ type: "conditional", variable: "", operator: "equals", value: "", then_steps: [], else_steps: [], delay_ms: 0 }),
  },
  loop: {
    label: "Loop",
    make: () => ({ type: "loop", mode: "count", count: 3, variable: "", operator: "equals", value: "", list_variable: "rows", item_as: "row", max_iterations: 100, body_steps: [], delay_ms: 0 }),
  },
};

// One line per step type, in plain language: what it does and when you'd
// reach for it. Shown under the Add Step picker and as each row's tooltip,
// since a list of type names alone doesn't tell you which one you want.
const STEP_HELP = {
  file_search: "Finds files in a folder and remembers where they are, so later steps can say \"that file\" without knowing its name. Newest first + keep 1 = whatever arrived last. EXAMPLE: folder %USERPROFILE%\\Desktop\\For Macro, pattern *.xlsx, newest first, keep 1, store as files -- later steps write {{files}}.",
  sheet_read: "Reads a column out of a spreadsheet into a list, without opening Excel. Then a Loop can go down that list one row at a time. EXAMPLE: spreadsheet {{files}}, column A, first row of data 1, store as rows. Use A-G instead of A to read a whole row -- each cell becomes {{row_A}}, {{row_B}}, or {{row_SKU}} by heading.",
  open_file: "Opens a file the way double-clicking it would -- Excel for a spreadsheet, Acrobat for a PDF. EXAMPLE: file {{files}}. Keystroke steps after it go to that app window.",
  file_op: "Copies, moves, renames or deletes one file. EXAMPLE: move, source {{downloaded}}, destination %USERPROFILE%\\Desktop\\For Macro\\{{row}}.pdf.",
  file_wait: "Waits for a file to land in a folder and finish writing -- for a download started some other way. EXAMPLE: folder %USERPROFILE%\\Downloads, pattern *.pdf, store as downloaded.",
  clipboard: "Puts text on the clipboard, or reads what is on it into a variable. EXAMPLE: write, value {{row}} -- then a Shortcut step can paste it with ctrl+v.",
  cdp_launch: "Opens the separate Chrome that all the Web steps drive. Put it before them. Sign in to a site once in that window and it stays signed in. EXAMPLE: port 9222, URL https://portal.worldfirst.com/statement.",
  cdp_close: "Quits that Chrome at the end of a run. Your normal Chrome is never touched.",
  web_goto: "Opens a web address in that Chrome. Tick new tab to leave the current page alone. EXAMPLE: https://www.google.com/search?q={{row}} -- the number from the spreadsheet goes straight into the address.",
  web_reload: "Reloads the page -- for a screen showing stale numbers, or a retry after something did not take.",
  web_switch_tab: "Follows a tab that just opened because of the previous click, so the steps after it act on the new page. Put it after a click that opens a PDF or a preview.",
  web_show_tab: "Puts the tab the macro is working on in front, so you can watch it. For after a click that opens a tab of its own -- a print job, a preview -- which leaves the browser showing that tab while the run carries on behind it. Changes nothing about how the steps work; only what you see.",
  web_close_tab: "Closes the tab the macro is on and goes back to the one before it.",
  web_click: "Clicks something on the page -- a button, a link, a row. Say which by its visible words, or by a CSS selector (the name the page uses for it: press F12 in the CDP Chrome, click the arrow icon, click the thing, right-click the highlighted line, Copy > Copy selector). EXAMPLE: visible text 搜索, exact match on. Fill in \"until this appears\" with what the click should produce -- a dialog, a panel -- and it presses again until that shows up, which fixes a button that exists a moment before it works.",
  web_type: "Types into a box on the page. No clicking first, no clipboard -- the value goes straight in. EXAMPLE: CSS selector #fuzzyName, value {{row}}, press Enter after on.",
  web_hover: "Moves the pointer over something without clicking -- what you need to open a menu that drops down on hover.",
  web_upload: "Attaches a file to an upload box, with no Windows file picker involved. EXAMPLE: selector input[type=file], file {{files}}.",
  web_drop_files: "Drops a file onto a drop zone, as if you had dragged it off the desktop. For the cards that have no upload box behind them -- their button opens the Windows picker, which nothing can drive. EXAMPLE: selector #bundleOrdersCard, file {{files}}.",
  web_download: "Clicks a download button and puts the file where you say, renaming it, then waits until it has finished writing. EXAMPLE: text Download, into folder %USERPROFILE%\\Desktop\\For Macro\\{{row}}, save as {{row}}.pdf.",
  web_print_pdf: "Saves the page itself as a PDF -- no Ctrl+P, no dialog. For a page you can read but not download. EXAMPLE: save as %USERPROFILE%\\Desktop\\For Macro\\{{row}}\\{{row}}.pdf.",
  web_read: "Copies text off the page into a variable, so a later step can check it or use it in a file name. EXAMPLE: selector body, store as check -- then an If step can test what the page said.",
  web_wait_for: "Waits until something is actually on the page. The most reliable wait there is: it waits for the thing you care about, not for a guess at how long the page takes. EXAMPLE: CSS selector #fuzzyName, or visible text 订单详情.",
  web_wait_loaded: "Waits until the page stops changing. A page that has not started yet looks like one that has finished, so set \"wait at least\" for content that arrives on a timer. EXAMPLE: quiet for 800, wait at least 2000.",
  loop: "Repeats the steps inside it. Set mode to each and point it at a list -- the current one is {{row}} inside the body, and its position is {{row_number}}. EXAMPLE: mode each, list variable rows, each one as row. The steps that repeat go in the \"Loop body\" box on the step itself, not at the bottom of the page.",
  conditional: "Runs one set of steps when a variable matches, another when it does not -- for skipping the ones that are not there. EXAMPLE: variable check, contains, {{row}} -- then only save the PDF if the page really is that order.",
  wait: "Just pauses. Use a proper wait step where you can -- this one is a guess. EXAMPLE: 2000 (two seconds).",
  click: "A click in a desktop app window, from a recording. Not for web pages -- the Web steps are better there.",
  double_click: "A double-click in a desktop app window, from a recording.",
  scroll: "A recorded scroll wheel movement in a desktop app.",
  key: "A key pressed into the desktop window the last click landed in.",
  hotkey: "A recorded key combination, e.g. ctrl+c.",
  keyboard_shortcut: "A key combination you type in yourself, sent to the desktop window the last click landed in. EXAMPLE: ctrl+v. Office apps ignore these unless the run is started with foreground control allowed.",
  wait_for_element: "Waits for a named control to exist in a desktop app, and fails the run if it never shows.",
  wait_for_text: "Waits until a desktop control's text matches what you expect.",
  find_click_text: "Clicks a desktop app control by the words on it, instead of by where it sits on screen.",
  read_control_value: "Reads a desktop control's value into a variable.",
  get_cursor_position: "Saves where the mouse currently is, as a variable.",
  open_url: "Opens a page in your normal Chrome -- for looking at, not for automating. Nothing after it can click inside that page: the Web steps are the ones that can.",
};

const COMPARE_OPERATORS = ["equals", "not_equals", "contains", "regex", "greater_than", "less_than"];

// Thirty step types in one flat list is a wall. Grouped, each list is
// short enough to read, and the order matches the order a macro is built
// in: get the data, open the browser, do the thing.
const STEP_GROUPS = [
  ["Spreadsheets and files", ["file_search", "sheet_read", "open_file", "file_op", "file_wait", "clipboard"]],
  ["Web: getting there", ["cdp_launch", "web_goto", "web_reload", "web_switch_tab", "web_show_tab", "web_close_tab", "cdp_close"]],
  ["Web: doing things", ["web_click", "web_type", "web_hover", "web_upload", "web_drop_files", "web_download", "web_print_pdf", "web_read"]],
  ["Web: waiting", ["web_wait_for", "web_wait_loaded"]],
  ["Repeating and deciding", ["loop", "conditional", "wait"]],
  ["Desktop apps (recorded)", ["click", "double_click", "key", "hotkey", "keyboard_shortcut", "scroll",
                               "find_click_text", "wait_for_element", "wait_for_text",
                               "read_control_value", "get_cursor_position", "open_url"]],
];

function fillStepPicker() {
  const placed = new Set();
  STEP_GROUPS.forEach(([label, keys]) => {
    const group = document.createElement("optgroup");
    group.label = label;
    keys.forEach((key) => {
      if (!STEP_TEMPLATES[key]) return;
      placed.add(key);
      const opt = document.createElement("option");
      opt.value = key;
      opt.textContent = STEP_TEMPLATES[key].label;
      group.appendChild(opt);
    });
    if (group.children.length) addStepType.appendChild(group);
  });
  // Anything added later and not yet sorted into a group still shows up.
  const rest = document.createElement("optgroup");
  rest.label = "Other";
  Object.entries(STEP_TEMPLATES).forEach(([key, def]) => {
    if (placed.has(key)) return;
    const opt = document.createElement("option");
    opt.value = key;
    opt.textContent = def.label;
    rest.appendChild(opt);
  });
  if (rest.children.length) addStepType.appendChild(rest);
}

fillStepPicker();

// A recipe is a whole working macro, dropped in ready to edit. Starting
// from something that runs and changing the folder in it is a different
// job from assembling ten steps and finding out at the end which field
// was wrong.
function recipeStep(type, changes) {
  return Object.assign(STEP_TEMPLATES[type].make(), changes || {});
}

const RECIPES = {
  search_each: {
    label: "Look up every number from a spreadsheet on a website",
    note: "Reads column A of the newest spreadsheet in a folder, then opens a search page once per number. Change the folder and the web address, then run it.",
    make: () => [
      recipeStep("file_search", { folder: "%USERPROFILE%\\Desktop\\For Macro", pattern: "*.xlsx", newest_first: true, limit: 1, store_as: "files" }),
      recipeStep("sheet_read", { path: "{{files}}", column: "A", first_row: 1, store_as: "rows" }),
      recipeStep("cdp_launch", { url: "https://www.google.com" }),
      recipeStep("loop", {
        mode: "each", list_variable: "rows", item_as: "row",
        body_steps: [
          recipeStep("web_goto", { url: "https://www.google.com/search?q={{row}}", new_tab: false }),
          recipeStep("web_wait_loaded", { min_ms: 1500 }),
        ],
      }),
    ],
  },
  pdf_each: {
    label: "Save a PDF of a page for every number",
    note: "Same start, but each page is saved as its own PDF, named after the number, in its own folder. Skips numbers whose page didn't load.",
    make: () => [
      recipeStep("file_search", { folder: "%USERPROFILE%\\Desktop\\For Macro", pattern: "*.xlsx", newest_first: true, limit: 1, store_as: "files" }),
      recipeStep("sheet_read", { path: "{{files}}", column: "A", first_row: 1, store_as: "rows" }),
      recipeStep("cdp_launch", {}),
      recipeStep("loop", {
        mode: "each", list_variable: "rows", item_as: "row",
        body_steps: [
          recipeStep("web_goto", { url: "https://example.com/page?id={{row}}", new_tab: true }),
          recipeStep("web_wait_loaded", { min_ms: 2000 }),
          recipeStep("web_read", { selector: "body", store_as: "check" }),
          recipeStep("conditional", {
            variable: "check", operator: "contains", value: "{{row}}",
            then_steps: [recipeStep("web_print_pdf", {
              destination: "%USERPROFILE%\\Desktop\\For Macro\\{{row}}\\{{row}}.pdf",
            })],
          }),
          recipeStep("web_close_tab", {}),
        ],
      }),
    ],
  },
  download_each: {
    label: "Download a file for every number, into its own folder",
    note: "Types each number into a site's search box, clicks through to the file, and saves it under the number. Fill in the three selectors with what the site actually uses.",
    make: () => [
      recipeStep("file_search", { folder: "%USERPROFILE%\\Desktop\\For Macro", pattern: "*.xlsx", newest_first: true, limit: 1, store_as: "files" }),
      recipeStep("sheet_read", { path: "{{files}}", column: "A", first_row: 1, store_as: "rows" }),
      recipeStep("cdp_launch", { url: "https://example.com" }),
      recipeStep("loop", {
        mode: "each", list_variable: "rows", item_as: "row",
        body_steps: [
          recipeStep("web_goto", { url: "https://example.com", new_tab: false }),
          recipeStep("web_wait_for", { selector: "#search" }),
          recipeStep("web_type", { selector: "#search", value: "{{row}}", submit: true }),
          recipeStep("web_wait_loaded", { min_ms: 2000 }),
          recipeStep("web_click", { text: "View details", exact: true, until_selector: ".ant-drawer-open" }),
          recipeStep("web_download", {
            text: "Download", exact: true,
            folder: "%USERPROFILE%\\Desktop\\For Macro\\{{row}}",
            save_as: "{{row}}.pdf",
          }),
        ],
      }),
    ],
  },
};

let editingMacroId = null;
let editingSteps = [];
// What the file holds, so "run from here" can tell you when what you are
// looking at is not what would actually run.
let savedSteps = [];

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
    savedSteps = JSON.parse(JSON.stringify(editingSteps));
    editorMacroName.textContent = name;
    showEditScreen(true);
    macroEditorPanel.style.display = "block";
    editorEmpty.style.display = "none";
    pickedSteps.clear();
    lastPick = null;
    undoStack.length = 0;
    updateUndoButton();
    hideNote(editorNote);
    renderEditorSteps();
    renderEditLibrary();
    loadVideoSettings(macro.video || {});
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
  updateChunkBar();
}

let dragSource = null;

// -- copy a chunk, paste it where you click ---------------------------------
// Duplicating one step at a time is fine for one step. A run of six --
// open the drawer, tick Fee, download, close it -- is the unit people
// actually repeat, and rebuilding it by hand is where mistakes come from.
// Tick the rows, Copy chunk, then Paste on the step it should follow.

// Held by identity, not index: a re-render rebuilds every row but reuses
// the same step objects, so ticks survive it.
const pickedSteps = new Set();
let chunkClipboard = null;   // array of steps, already detached copies
// Where the last tick happened, so shift-tick can fill in the run between.
let lastPick = null;         // { array, index }

const copyChunkBtn = document.getElementById("copyChunkBtn");
const clearPickBtn = document.getElementById("clearPickBtn");
const forgetChunkBtn = document.getElementById("forgetChunkBtn");
const chunkStatus = document.getElementById("chunkStatus");

function pickedInOrder() {
  // Document order, not tick order -- a chunk pasted in the sequence the
  // boxes happened to be clicked would be nonsense.
  const out = [];
  const walk = (steps) => {
    steps.forEach((step) => {
      if (pickedSteps.has(step)) out.push(step);
      ["then_steps", "else_steps", "body_steps"].forEach((key) => {
        if (Array.isArray(step[key])) walk(step[key]);
      });
    });
  };
  walk(editingSteps);
  return out;
}

function updateChunkBar() {
  if (!chunkStatus) return;
  const picked = pickedInOrder().length;
  copyChunkBtn.disabled = picked === 0;
  clearPickBtn.disabled = picked === 0;
  forgetChunkBtn.style.display = chunkClipboard ? "" : "none";
  const held = chunkClipboard
    ? ` Holding ${chunkClipboard.length} step(s) -- press Paste on the step it should follow.`
    : "";
  chunkStatus.textContent = picked
    ? `${picked} step(s) ticked.${held}`
    : (held || "Tick the boxes on a run of steps, then Copy chunk. Shift-tick picks a whole run.");
}

if (copyChunkBtn) {
  copyChunkBtn.addEventListener("click", () => {
    const picked = pickedInOrder();
    if (!picked.length) return;
    chunkClipboard = picked.map(cloneStep);
    pickedSteps.clear();
    lastPick = null;
    renderEditorSteps();
    showNote(editorNote, `Copied ${chunkClipboard.length} step(s). Press Paste on the step they should go after -- any list, including inside a loop.`, "info");
  });

  clearPickBtn.addEventListener("click", () => {
    pickedSteps.clear();
    lastPick = null;
    renderEditorSteps();
  });

  forgetChunkBtn.addEventListener("click", () => {
    chunkClipboard = null;
    renderEditorSteps();
  });
}

function pasteChunkInto(stepsArray, at, containerEl) {
  if (!chunkClipboard) return;
  markUndo();
  // Cloned again on every paste: the same chunk can go in three places,
  // and they must not end up sharing step ids.
  const copies = chunkClipboard.map(cloneStep);
  stepsArray.splice(at, 0, ...copies);
  renderStepArrayInto(stepsArray, containerEl);
  updateChunkBar();
  showNote(editorNote, `Pasted ${copies.length} step(s). Save Changes to keep them.`, "info");
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
    markUndo();
    stepsArray.push(STEP_TEMPLATES[select.value].make());
    renderStepArrayInto(stepsArray, list);
  });
  toolbar.append(select, addBtn);
  if (chunkClipboard) {
    // An empty loop body has no row to press Paste on, and pasting at the
    // end is what you want anyway when you are filling one in.
    const pasteEnd = mkActionBtn("Paste chunk here", `Paste the copied ${chunkClipboard.length}-step chunk at the end of this list`,
      false, () => pasteChunkInto(stepsArray, stepsArray.length, list));
    pasteEnd.className = "paste-here";
    toolbar.appendChild(pasteEnd);
  }
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

  const pick = document.createElement("input");
  pick.type = "checkbox";
  pick.className = "step-pick";
  pick.checked = pickedSteps.has(step);
  pick.title = "Tick to include this step in the chunk (shift-tick for a run)";
  pick.addEventListener("click", (event) => {
    if (event.shiftKey && lastPick && lastPick.array === stepsArray) {
      const [from, to] = [lastPick.index, index].sort((a, b) => a - b);
      for (let k = from; k <= to; k += 1) {
        if (pick.checked) pickedSteps.add(stepsArray[k]);
        else pickedSteps.delete(stepsArray[k]);
      }
      renderStepArrayInto(stepsArray, containerEl);
    } else if (pick.checked) {
      pickedSteps.add(step);
      li.classList.add("picked");
    } else {
      pickedSteps.delete(step);
      li.classList.remove("picked");
    }
    lastPick = { array: stepsArray, index };
    updateChunkBar();
  });
  if (pick.checked) li.classList.add("picked");

  const seqSpan = document.createElement("span");
  seqSpan.className = "step-seq";
  seqSpan.textContent = `#${index}`;

  const typeSpan = document.createElement("span");
  typeSpan.className = "step-type";
  typeSpan.textContent = typeLabel(step.type);
  if (STEP_HELP[step.type]) {
    typeSpan.title = STEP_HELP[step.type];
    li.title = STEP_HELP[step.type];
  }

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
    addField("mode", editorSelect(step.mode, ["count", "each", "until"], (v) => {
      step.mode = v;
      renderStepArrayInto(stepsArray, containerEl);
    }));
    if (step.mode === "count") {
      addField("count", editorInput("number", step.count, (v) => (step.count = v), "60px"));
    } else if (step.mode === "each") {
      addField("list variable", editorInput("text", step.list_variable, (v) => (step.list_variable = v), "90px"));
      addField("each one as", editorInput("text", step.item_as, (v) => (step.item_as = v), "90px"));
      addField("max iterations", editorInput("number", step.max_iterations, (v) => (step.max_iterations = v), "70px"));
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
    addField("own window", editorCheckbox(step.new_window, (v) => (step.new_window = v)));
  } else if (step.type === "open_file") {
    addField("file", editorInput("text", step.path, (v) => (step.path = v), "260px"));
    addField("window title (blank=the file's name)", editorInput("text", step.window_title, (v) => (step.window_title = v), "160px"));
    addField("wait for window (ms, 0=don't)", editorInput("number", step.timeout_ms, (v) => (step.timeout_ms = v), "80px"));
  } else if (step.type === "cdp_launch") {
    addField("port", editorInput("number", step.port, (v) => (step.port = v), "60px"));
    addField("URL (optional)", editorInput("text", step.url, (v) => (step.url = v), "200px"));
    addField("profile folder (blank=default)", editorInput("text", step.user_data_dir, (v) => (step.user_data_dir = v), "180px"));
    addField("timeout (ms)", editorInput("number", step.timeout_ms, (v) => (step.timeout_ms = v), "80px"));
  } else if (step.type === "cdp_close") {
    addField("port", editorInput("number", step.port, (v) => (step.port = v), "60px"));
  } else if (step.type === "web_goto") {
    addField("port", editorInput("number", step.port, (v) => (step.port = v), "60px"));
    addField("URL", editorInput("text", step.url, (v) => (step.url = v), "220px"));
    addField("new tab", editorCheckbox(step.new_tab, (v) => (step.new_tab = v)));
    addField("tab match (blank=this run's tab)", editorInput("text", step.tab_match, (v) => (step.tab_match = v), "150px"));
  } else if (step.type === "web_click" || step.type === "web_hover" || step.type === "web_wait_for") {
    addField("port", editorInput("number", step.port, (v) => (step.port = v), "60px"));
    addField("tab match (blank=this run's tab)", editorInput("text", step.tab_match, (v) => (step.tab_match = v), "150px"));
    addField("CSS selector", editorInput("text", step.selector, (v) => (step.selector = v), "160px"));
    addField("and/or visible text", editorInput("text", step.text, (v) => (step.text = v), "140px"));
    addField("exact match", editorCheckbox(step.exact, (v) => (step.exact = v)));
    if (step.type !== "web_wait_for") {
      addField("match # (0 = first)", editorInput("number", step.match_index, (v) => (step.match_index = v), "70px"));
    }
    if (step.type === "web_click") {
      addField("button", editorSelect(step.button || "left", ["left", "right"], (v) => (step.button = v)));
      addField("until this appears: selector", editorInput("text", step.until_selector, (v) => (step.until_selector = v), "150px"));
      addField("until this appears: text", editorInput("text", step.until_text, (v) => (step.until_text = v), "130px"));
      addField("open menu: hover selector (>> for nested)", editorInput("text", step.hover_selector, (v) => (step.hover_selector = v), "150px"));
      addField("open menu: hover text (>> for nested)", editorInput("text", step.hover_text, (v) => (step.hover_text = v), "150px"));
    }
    addField("timeout (ms)", editorInput("number", step.timeout_ms, (v) => (step.timeout_ms = v), "80px"));
  } else if (step.type === "web_type") {
    addField("port", editorInput("number", step.port, (v) => (step.port = v), "60px"));
    addField("tab match (blank=this run's tab)", editorInput("text", step.tab_match, (v) => (step.tab_match = v), "150px"));
    addField("CSS selector", editorInput("text", step.selector, (v) => (step.selector = v), "160px"));
    addField("value", editorInput("text", step.value, (v) => (step.value = v), "140px"));
    addField("press Enter after", editorCheckbox(step.submit, (v) => (step.submit = v)));
  } else if (step.type === "web_upload") {
    addField("port", editorInput("number", step.port, (v) => (step.port = v), "60px"));
    addField("tab match (blank=this run's tab)", editorInput("text", step.tab_match, (v) => (step.tab_match = v), "150px"));
    addField("CSS selector", editorInput("text", step.selector, (v) => (step.selector = v), "160px"));
    addField("file (or {{files}} from a File search)", editorInput("text", step.files, (v) => (step.files = v), "260px"));
    addField("match # (0 = first)", editorInput("number", step.match_index, (v) => (step.match_index = v), "70px"));
    addField("timeout (ms)", editorInput("number", step.timeout_ms, (v) => (step.timeout_ms = v), "80px"));
  } else if (step.type === "web_drop_files") {
    addField("port", editorInput("number", step.port, (v) => (step.port = v), "60px"));
    addField("tab match (blank=this run's tab)", editorInput("text", step.tab_match, (v) => (step.tab_match = v), "150px"));
    addField("CSS selector of the drop zone", editorInput("text", step.selector, (v) => (step.selector = v), "170px"));
    addField("or its visible words", editorInput("text", step.text, (v) => (step.text = v), "140px"));
    addField("file (or {{files}} from a File search)", editorInput("text", step.files, (v) => (step.files = v), "260px"));
    addField("match # (0 = first)", editorInput("number", step.match_index, (v) => (step.match_index = v), "70px"));
    addField("timeout (ms)", editorInput("number", step.timeout_ms, (v) => (step.timeout_ms = v), "80px"));
  } else if (step.type === "web_reload") {
    addField("port", editorInput("number", step.port, (v) => (step.port = v), "60px"));
    addField("tab match (blank=this run's tab)", editorInput("text", step.tab_match, (v) => (step.tab_match = v), "150px"));
    addField("ignore cache", editorCheckbox(step.ignore_cache, (v) => (step.ignore_cache = v)));
    addField("timeout (ms)", editorInput("number", step.timeout_ms, (v) => (step.timeout_ms = v), "80px"));
  } else if (step.type === "web_wait_loaded") {
    addField("port", editorInput("number", step.port, (v) => (step.port = v), "60px"));
    addField("tab match (blank=this run's tab)", editorInput("text", step.tab_match, (v) => (step.tab_match = v), "150px"));
    addField("quiet for (ms)", editorInput("number", step.quiet_ms, (v) => (step.quiet_ms = v), "80px"));
    addField("wait at least (ms)", editorInput("number", step.min_ms, (v) => (step.min_ms = v), "80px"));
    addField("timeout (ms)", editorInput("number", step.timeout_ms, (v) => (step.timeout_ms = v), "80px"));
  } else if (step.type === "web_show_tab") {
    addField("port", editorInput("number", step.port, (v) => (step.port = v), "60px"));
    addField("URL/title contains (optional)", editorInput("text", step.tab_match, (v) => (step.tab_match = v), "170px"));
    addField("timeout ms", editorInput("number", step.timeout_ms, (v) => (step.timeout_ms = v), "80px"));
  } else if (step.type === "web_switch_tab") {
    addField("port", editorInput("number", step.port, (v) => (step.port = v), "60px"));
    addField("which", editorSelect(step.mode || "new", ["new", "match"], (v) => (step.mode = v)));
    addField("URL/title contains (optional)", editorInput("text", step.tab_match, (v) => (step.tab_match = v), "170px"));
    addField("timeout (ms)", editorInput("number", step.timeout_ms, (v) => (step.timeout_ms = v), "80px"));
  } else if (step.type === "web_close_tab") {
    addField("port", editorInput("number", step.port, (v) => (step.port = v), "60px"));
    addField("tab match (blank=this run's tab)", editorInput("text", step.tab_match, (v) => (step.tab_match = v), "150px"));
  } else if (step.type === "web_print_pdf") {
    addField("port", editorInput("number", step.port, (v) => (step.port = v), "60px"));
    addField("tab match (blank=this run's tab)", editorInput("text", step.tab_match, (v) => (step.tab_match = v), "150px"));
    addField("save as", editorInput("text", step.destination, (v) => (step.destination = v), "260px"));
    addField("landscape", editorCheckbox(step.landscape, (v) => (step.landscape = v)));
    addField("paper", editorSelect(step.paper || "A4", ["A4", "A3", "A5", "Letter", "Legal", "Tabloid"], (v) => (step.paper = v)));
    addField("scale", editorInput("number", step.scale, (v) => (step.scale = v), "60px"));
    addField("margins (in)", editorInput("number", step.margin_inches, (v) => (step.margin_inches = v), "70px"));
    addField("backgrounds (off = white paper)", editorCheckbox(step.background, (v) => (step.background = v)));
    addField("store as", editorInput("text", step.store_as, (v) => (step.store_as = v), "80px"));
  } else if (step.type === "web_read") {
    addField("port", editorInput("number", step.port, (v) => (step.port = v), "60px"));
    addField("tab match (blank=this run's tab)", editorInput("text", step.tab_match, (v) => (step.tab_match = v), "150px"));
    addField("CSS selector", editorInput("text", step.selector, (v) => (step.selector = v), "160px"));
    addField("store as", editorInput("text", step.store_as, (v) => (step.store_as = v), "80px"));
  } else if (step.type === "sheet_read") {
    addField("spreadsheet", editorInput("text", step.path, (v) => (step.path = v), "240px"));
    addField("sheet (blank=first)", editorInput("text", step.sheet, (v) => (step.sheet = v), "110px"));
    addField("column(s): A, A-G, or headings", editorInput("text", step.column, (v) => (step.column = v), "120px"));
    addField("first row of data", editorInput("number", step.first_row, (v) => (step.first_row = v), "70px"));
    addField("keep", editorInput("number", step.limit, (v) => (step.limit = v), "70px"));
    addField("CSV encoding (blank=auto)", editorInput("text", step.encoding, (v) => (step.encoding = v), "90px"));
    addField("store as", editorInput("text", step.store_as, (v) => (step.store_as = v), "80px"));
  } else if (step.type === "file_wait") {
    addField("folder", editorInput("text", step.folder, (v) => (step.folder = v), "220px"));
    addField("pattern", editorInput("text", step.pattern, (v) => (step.pattern = v), "80px"));
    addField("timeout (ms)", editorInput("number", step.timeout_ms, (v) => (step.timeout_ms = v), "80px"));
    addField("store as", editorInput("text", step.store_as, (v) => (step.store_as = v), "80px"));
  } else if (step.type === "web_download") {
    addField("port", editorInput("number", step.port, (v) => (step.port = v), "60px"));
    addField("tab match (blank=this run's tab)", editorInput("text", step.tab_match, (v) => (step.tab_match = v), "150px"));
    addField("CSS selector", editorInput("text", step.selector, (v) => (step.selector = v), "160px"));
    addField("and/or visible text", editorInput("text", step.text, (v) => (step.text = v), "140px"));
    addField("exact match", editorCheckbox(step.exact, (v) => (step.exact = v)));
    addField("match # (0 = first)", editorInput("number", step.match_index, (v) => (step.match_index = v), "70px"));
    addField("into folder", editorInput("text", step.folder, (v) => (step.folder = v), "240px"));
    addField("save as (blank=site's name)", editorInput("text", step.save_as, (v) => (step.save_as = v), "160px"));
    addField("store as", editorInput("text", step.store_as, (v) => (step.store_as = v), "80px"));
    addField("timeout (ms)", editorInput("number", step.timeout_ms, (v) => (step.timeout_ms = v), "80px"));
  } else if (step.type === "file_search") {
    addField("folder", editorInput("text", step.folder, (v) => (step.folder = v), "160px"));
    addField("pattern", editorInput("text", step.pattern, (v) => (step.pattern = v), "80px"));
    addField("recursive", editorCheckbox(step.recursive, (v) => (step.recursive = v)));
    addField("newest first", editorCheckbox(step.newest_first, (v) => (step.newest_first = v)));
    addField("keep", editorInput("number", step.limit, (v) => (step.limit = v), "60px"));
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
  addField("stop run if this fails", editorCheckbox(step.stop_on_fail, (v) => (step.stop_on_fail = v)));

  const actions = document.createElement("span");
  actions.className = "step-actions";

  const upBtn = mkActionBtn("↑", "Move up", index === 0, () => {
    markUndo();
    moveInArray(stepsArray, index, -1);
    renderStepArrayInto(stepsArray, containerEl);
  });
  const downBtn = mkActionBtn("↓", "Move down", index === stepsArray.length - 1, () => {
    markUndo();
    moveInArray(stepsArray, index, 1);
    renderStepArrayInto(stepsArray, containerEl);
  });
  const copyBtn = mkActionBtn("Copy", "Duplicate this step, just below", false, () => {
    markUndo();
    stepsArray.splice(index + 1, 0, cloneStep(step));
    renderStepArrayInto(stepsArray, containerEl);
  });
  const delBtn = mkActionBtn("Delete", "", false, () => {
    markUndo();
    pickedSteps.delete(step);
    stepsArray.splice(index, 1);
    renderStepArrayInto(stepsArray, containerEl);
    updateChunkBar();
  });

  actions.append(upBtn, downBtn, copyBtn, delBtn);

  // Only for top-level steps of the macro being edited: "start at #31" means
  // the thirty-first step of the run, and a step inside a loop body does not
  // have one of those numbers.
  if (stepsArray === editingSteps && editingMacroId) {
    const fromHere = mkActionBtn("▶ from here", `Run this macro starting at step #${index}`,
      false, () => runFromStep(index));
    fromHere.className = "from-here";
    actions.appendChild(fromHere);
  }

  if (chunkClipboard) {
    const pasteBtn = mkActionBtn("Paste", `Paste the copied ${chunkClipboard.length}-step chunk just below this step`,
      false, () => pasteChunkInto(stepsArray, index + 1, containerEl));
    pasteBtn.className = "paste-here";
    actions.appendChild(pasteBtn);
  }

  // Drag to reorder. The handle is its own element rather than the whole
  // row because every field in that row is an input -- making the row
  // draggable would fight with selecting text inside them.
  const grip = document.createElement("span");
  grip.className = "step-grip";
  grip.textContent = "⠿";
  grip.title = "Drag to reorder";
  grip.draggable = true;
  grip.addEventListener("dragstart", (event) => {
    dragSource = { array: stepsArray, container: containerEl, index };
    li.classList.add("dragging");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", String(index));  // Firefox needs a payload
    event.dataTransfer.setDragImage(li, 20, 12);
  });
  grip.addEventListener("dragend", () => {
    li.classList.remove("dragging");
    dragSource = null;
  });

  li.addEventListener("dragover", (event) => {
    // Only within the same list: a step can't be half in a loop body.
    if (!dragSource || dragSource.array !== stepsArray) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    const box = li.getBoundingClientRect();
    li.classList.toggle("drop-after", event.clientY > box.top + box.height / 2);
    li.classList.toggle("drop-before", event.clientY <= box.top + box.height / 2);
  });
  li.addEventListener("dragleave", () => li.classList.remove("drop-before", "drop-after"));
  li.addEventListener("drop", (event) => {
    if (!dragSource || dragSource.array !== stepsArray) return;
    event.preventDefault();
    const after = li.classList.contains("drop-after");
    li.classList.remove("drop-before", "drop-after");
    markUndo();
    let to = index + (after ? 1 : 0);
    const [moved] = stepsArray.splice(dragSource.index, 1);
    if (dragSource.index < to) to -= 1;
    stepsArray.splice(Math.max(0, Math.min(to, stepsArray.length)), 0, moved);
    dragSource = null;
    renderStepArrayInto(stepsArray, containerEl);
  });

  header.append(pick, grip, seqSpan, typeSpan, fields, actions);
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

// Running from part-way down is for the third attempt at a macro whose
// first twenty steps already worked. It runs what is SAVED, so an unsaved
// edit would otherwise run the old version of the step you are looking at.
async function runFromStep(index) {
  const macro = macros.find((m) => m.id === editingMacroId);
  if (!macro) return;
  const dirty = JSON.stringify(editingSteps) !== JSON.stringify(savedSteps);
  if (dirty && !window.confirm(
      `This macro has unsaved changes, and a run always uses the saved version.\n\n` +
      `Run the saved macro from step #${index} anyway?`)) return;
  runMacro(macro.id, macro.name, false, index);
}

function freshStepId() {
  return (crypto.randomUUID ? crypto.randomUUID() : String(Math.random())).replace(/-/g, "");
}

// A copied step needs its own id, and so does everything nested inside a
// conditional or a loop -- two steps sharing an id makes the run report
// ambiguous about which one a result belongs to.
function cloneStep(step) {
  const copy = JSON.parse(JSON.stringify(step));
  const reid = (s) => {
    s.id = freshStepId();
    ["then_steps", "else_steps", "body_steps"].forEach((key) => {
      if (Array.isArray(s[key])) s[key].forEach(reid);
    });
  };
  reid(copy);
  return copy;
}

function moveInArray(arr, index, dir) {
  const target = index + dir;
  if (target < 0 || target >= arr.length) return;
  [arr[index], arr[target]] = [arr[target], arr[index]];
}

const addStepHelp = document.getElementById("addStepHelp");
const editorRecordBtn = document.getElementById("editorRecordBtn");
const editorRecordStopBtn = document.getElementById("editorRecordStopBtn");

// While this is on, steps the browser recorder captures land in the macro
// being edited rather than in the unsaved-recording list -- which is the
// whole point: adding three steps to a working macro shouldn't mean
// recording it from the top again.
let editorRecording = false;

function applyEditorRecordingState(on) {
  editorRecording = on;
  if (!editorRecordBtn) return;
  editorRecordBtn.disabled = on;
  editorRecordBtn.classList.toggle("recording", on);
  editorRecordStopBtn.disabled = !on;
}

if (editorRecordBtn) {
  editorRecordBtn.addEventListener("click", async () => {
    try {
      const res = await fetch("/api/web-recording/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // No seed steps and no URL: attach to whatever that browser is
        // already showing, since the macro being edited got it there.
        body: JSON.stringify({ seed: false }),
      });
      const body = await res.json();
      if (!res.ok) {
        showNote(editorNote, body.detail || `Unexpected error (HTTP ${res.status})`, "error");
        return;
      }
      applyEditorRecordingState(true);
      showNote(editorNote, "Recording. Click through the CDP browser -- steps append to the end of this list. Drag them where you want, then Save Changes.", "info");
    } catch (err) {
      showNote(editorNote, `Could not reach the agent: ${err.message}`, "error");
    }
  });

  editorRecordStopBtn.addEventListener("click", async () => {
    try {
      const res = await fetch("/api/web-recording/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ adopt: false }),
      });
      const body = await res.json();
      applyEditorRecordingState(false);
      if (!res.ok) {
        showNote(editorNote, body.detail || `Unexpected error (HTTP ${res.status})`, "error");
        return;
      }
      showNote(editorNote, "Stopped. The new steps are at the end -- Save Changes to keep them.", "info");
    } catch (err) {
      showNote(editorNote, `Could not reach the agent: ${err.message}`, "error");
    }
  });
}
const tallEditor = document.getElementById("tallEditor");
const freeScrollEditor = document.getElementById("freeScrollEditor");

if (tallEditor) {
  const applyEditorHeight = () => {
    const free = !freeScrollEditor || freeScrollEditor.checked;
    editorStepList.classList.toggle("freescroll", free);
    editorStepList.classList.toggle("compact", !tallEditor.checked);
    // With the list uncapped the page does the scrolling, so a height for
    // the box is not a thing that exists any more.
    tallEditor.disabled = free;
    tallEditor.parentElement.style.opacity = free ? "0.45" : "";
  };
  tallEditor.addEventListener("change", applyEditorHeight);
  if (freeScrollEditor) freeScrollEditor.addEventListener("change", applyEditorHeight);
  applyEditorHeight();
}

function updateAddStepHelp() {
  if (!addStepHelp) return;
  addStepHelp.textContent = STEP_HELP[addStepType.value] || "";
}

addStepType.addEventListener("change", updateAddStepHelp);
updateAddStepHelp();

const addRecipeType = document.getElementById("addRecipeType");
const addRecipeBtn = document.getElementById("addRecipeBtn");
const addRecipeHelp = document.getElementById("addRecipeHelp");

if (addRecipeType) {
  Object.entries(RECIPES).forEach(([key, def]) => {
    const opt = document.createElement("option");
    opt.value = key;
    opt.textContent = def.label;
    addRecipeType.appendChild(opt);
  });
  const updateRecipeHelp = () => {
    if (addRecipeHelp) addRecipeHelp.textContent = (RECIPES[addRecipeType.value] || {}).note || "";
  };
  addRecipeType.addEventListener("change", updateRecipeHelp);
  updateRecipeHelp();
  addRecipeBtn.addEventListener("click", () => {
  markUndo();
    const recipe = RECIPES[addRecipeType.value];
    if (!recipe) return;
    recipe.make().forEach((step) => editingSteps.push(step));
    renderEditorSteps();
    showNote(editorNote, `Added "${recipe.label}". Edit the folder and web address in it, then Save Changes.`, "info");
  });
}

// The words someone types when they don't know what the step is called.
// Chinese included because half the sites these macros drive are in it,
// and "download" is as likely to be typed 下载.
const STEP_KEYWORDS = {
  sheet_read: "excel spreadsheet xlsx csv read column rows numbers list order numbers data sheet 表格 excel 读取 订单号",
  file_search: "find file folder newest latest downloaded arrived which file 文件 查找 最新",
  open_file: "open excel open pdf launch app double click file 打开 打开文件",
  file_op: "move copy rename delete file put file into folder 移动 复制 重命名 删除",
  file_wait: "wait for download finish downloaded file appear 等待 下载完成",
  clipboard: "copy paste clipboard ctrl+c ctrl+v 复制 粘贴 剪贴板",
  cdp_launch: "open browser chrome start browser login website first step 打开浏览器",
  cdp_close: "close browser quit chrome 关闭浏览器",
  web_goto: "go to website open page url address navigate search url 打开网页 网址",
  web_reload: "refresh reload page again 刷新",
  web_switch_tab: "new tab opened follow tab popup 新标签 切换",
  web_show_tab: "show tab front focus watch see what it is doing bring forward 前台 切换显示",
  web_close_tab: "close tab 关闭标签",
  web_click: "click button link press tap open menu 点击 按钮 搜索",
  web_type: "type enter fill search box input write text into field paste number 输入 填写 搜索框",
  web_hover: "hover mouse over dropdown menu 悬停 菜单",
  web_upload: "upload attach file picture photo choose file 上传 附件",
  web_drop_files: "drag drop file onto drop zone drag and drop dropzone 拖拽 拖放",
  web_download: "download file pdf invoice receipt statement save file to folder 下载 保存文件 发票",
  web_print_pdf: "save page as pdf print pdf screenshot page to pdf 打印 保存 pdf 订单详情单",
  web_read: "read text from page copy text check what page says get value 读取 文字",
  web_wait_for: "wait until appears wait for element wait for text loaded 等待 出现",
  web_wait_loaded: "wait for page to load loading slow page settle 等待 加载",
  loop: "repeat every row each one for all one by one again and again 循环 每一个 逐个",
  conditional: "if only when skip missing check first condition 如果 判断 跳过",
  wait: "pause sleep wait seconds delay 等待 暂停",
  keyboard_shortcut: "keyboard shortcut ctrl key press hotkey 快捷键",
  find_click_text: "click by name in a desktop app window button label",
  wait_for_element: "wait for desktop app window control",
  wait_for_text: "wait for desktop app text",
  read_control_value: "read value from desktop app field",
  get_cursor_position: "mouse position coordinates",
  open_url: "open link in my normal chrome browser",
};

const RECIPE_KEYWORDS = {
  search_each: "search every number google website look up each row excel search 搜索 每一个",
  pdf_each: "save pdf for every order print each page pdf per row 保存 pdf 每一个",
  download_each: "download file for every order into folder per order receipts invoices 下载 每一个 文件夹",
};

function searchSteps(query) {
  // Two-letter English words ("do", "it", "my") match half the help text
  // as substrings and drown out the real terms. Chinese doesn't have that
  // problem -- 下载 is two characters and means exactly one thing.
  const cjk = /[㐀-鿿]/;
  const words = query.toLowerCase().split(/[\s,]+/)
    .filter((w) => w.length >= 3 || cjk.test(w));
  if (!words.length) return [];
  const score = (haystacks) => {
    let total = 0;
    words.forEach((word) => {
      haystacks.forEach(([text, weight]) => {
        if (text.includes(word)) total += weight;
      });
    });
    return total;
  };
  const hits = [];
  Object.entries(RECIPES).forEach(([key, def]) => {
    const points = score([
      [(RECIPE_KEYWORDS[key] || "").toLowerCase(), 4],
      [def.label.toLowerCase(), 3],
      [(def.note || "").toLowerCase(), 1],
    ]);
    // A recipe answers a whole sentence; a step answers a word. Nudged up
    // so "download a pdf for every order" offers the macro, not six steps.
    if (points) hits.push({ key, kind: "recipe", points: points + 1, label: def.label, why: def.note });
  });
  Object.entries(STEP_TEMPLATES).forEach(([key, def]) => {
    const points = score([
      [(STEP_KEYWORDS[key] || "").toLowerCase(), 4],
      [def.label.toLowerCase(), 3],
      [(STEP_HELP[key] || "").toLowerCase(), 1],
    ]);
    if (points) hits.push({ key, kind: "step", points, label: def.label, why: STEP_HELP[key] || "" });
  });
  return hits.sort((a, b) => b.points - a.points).slice(0, 6);
}

const stepSearch = document.getElementById("stepSearch");
const stepSearchResults = document.getElementById("stepSearchResults");
const stepSearchClear = document.getElementById("stepSearchClear");

function renderStepSearch() {
  if (!stepSearchResults) return;
  stepSearchResults.innerHTML = "";
  const query = (stepSearch.value || "").trim();
  if (!query) return;
  const hits = searchSteps(query);
  if (!hits.length) {
    const empty = document.createElement("div");
    empty.className = "search-none";
    empty.textContent = "Nothing matched. Try the words for the thing itself -- excel, download, "
      + "click, wait, every row.";
    stepSearchResults.appendChild(empty);
    return;
  }
  hits.forEach((hit) => {
    const row = document.createElement("div");
    row.className = "search-hit";

    const body = document.createElement("div");
    body.className = "hit-body";
    const name = document.createElement("div");
    name.className = "hit-name";
    name.textContent = hit.label;
    const kind = document.createElement("span");
    kind.className = "hit-kind";
    kind.textContent = hit.kind === "recipe" ? "whole macro" : "step";
    name.appendChild(kind);
    const why = document.createElement("div");
    why.className = "hit-why";
    why.textContent = hit.why;
    body.appendChild(name);
    body.appendChild(why);

    const add = document.createElement("button");
    add.textContent = hit.kind === "recipe" ? "+ Add these steps" : "+ Add step";
    add.addEventListener("click", () => {
      markUndo();
      if (hit.kind === "recipe") {
        RECIPES[hit.key].make().forEach((step) => editingSteps.push(step));
        showNote(editorNote, `Added "${hit.label}". Edit the folder and web address in it, then Save Changes.`, "info");
      } else {
        editingSteps.push(STEP_TEMPLATES[hit.key].make());
        showNote(editorNote, `Added "${hit.label}" at the end. Fill in its boxes, then Save Changes.`, "info");
      }
      renderEditorSteps();
    });

    row.appendChild(body);
    row.appendChild(add);
    stepSearchResults.appendChild(row);
  });
}

if (stepSearch) {
  stepSearch.addEventListener("input", renderStepSearch);
  stepSearchClear.addEventListener("click", () => {
    stepSearch.value = "";
    renderStepSearch();
    stepSearch.focus();
  });
}

addStepBtn.addEventListener("click", () => {
  markUndo();
  const template = STEP_TEMPLATES[addStepType.value];
  if (!template) return;
  editingSteps.push(template.make());
  renderEditorSteps();
});

function stopEditorRecordingIfRunning() {
  if (!editorRecording) return;
  applyEditorRecordingState(false);
  fetch("/api/web-recording/stop", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ adopt: false }),
  }).catch(() => {});
}

function closeEditor() {
  stopEditorRecordingIfRunning();
  macroEditorPanel.style.display = "none";
  editorEmpty.style.display = "";
  editingMacroId = null;
  editingSteps = [];
  savedSteps = [];
  pickedSteps.clear();
  lastPick = null;
  undoStack.length = 0;
  updateUndoButton();
  renderEditLibrary();
}

cancelEditorBtn.addEventListener("click", closeEditor);

saveEditorBtn.addEventListener("click", async () => {
  stopEditorRecordingIfRunning();
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
    savedSteps = JSON.parse(JSON.stringify(editingSteps));
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

// -- Check a Run ------------------------------------------------------------
// The checks in checks/ on a button. Same code Check.bat runs, run the same
// way -- one process per check -- so the two can't drift into disagreeing.

const checkType = document.getElementById("checkType");
const runCheckBtn = document.getElementById("runCheckBtn");
const runAllChecksBtn = document.getElementById("runAllChecksBtn");
const checkNote = document.getElementById("checkNote");
const checkOutput = document.getElementById("checkOutput");

async function loadChecks() {
  if (!checkType) return;
  try {
    const res = await fetch("/api/checks");
    const body = await res.json();
    const checks = body.checks || [];
    checkType.innerHTML = "";
    checks.forEach((check) => {
      const opt = document.createElement("option");
      opt.value = check.name;
      opt.textContent = check.title;
      checkType.appendChild(opt);
    });
    const none = checks.length === 0;
    runCheckBtn.disabled = none;
    runAllChecksBtn.disabled = none;
    if (none) {
      const opt = document.createElement("option");
      opt.textContent = "No checks in the checks folder yet";
      checkType.appendChild(opt);
    }
  } catch (err) {
    showNote(checkNote, `Could not reach the agent: ${err.message}`, "error");
  }
}

function showCheckOutput(text, failed) {
  checkOutput.style.display = "block";
  checkOutput.textContent = text || "(that check printed nothing)";
  checkOutput.classList.toggle("pass", !failed);
  checkOutput.classList.toggle("fail", !!failed);
}

async function runOneCheck(name) {
  const res = await fetch(`/api/checks/${name}/run`, { method: "POST" });
  const body = await res.json();
  if (!res.ok) throw new Error(body.detail || `Unexpected error (HTTP ${res.status})`);
  return body;
}

async function runChecks(names) {
  runCheckBtn.disabled = true;
  runAllChecksBtn.disabled = true;
  showNote(checkNote, names.length > 1 ? `Running ${names.length} checks...` : "Running...", "info");
  try {
    const parts = [];
    let failed = 0;
    for (const name of names) {
      const result = await runOneCheck(name);
      const title = [...checkType.options].find((o) => o.value === name);
      if (names.length > 1) parts.push(`=== ${title ? title.textContent : name} ===`);
      parts.push(result.output);
      if (result.exit_code !== 0) failed += 1;
    }
    showCheckOutput(parts.join("\n"), failed > 0);
    if (failed) {
      showNote(checkNote, failed === names.length && names.length === 1
        ? "Problems found -- see below."
        : `${failed} of ${names.length} check(s) found problems.`, "error");
    } else {
      showNote(checkNote, names.length > 1 ? "All checks passed." : "Passed.", "info");
    }
  } catch (err) {
    showNote(checkNote, err.message, "error");
  } finally {
    runCheckBtn.disabled = false;
    runAllChecksBtn.disabled = false;
  }
}

if (runCheckBtn) {
  runCheckBtn.addEventListener("click", () => runChecks([checkType.value]));
  runAllChecksBtn.addEventListener("click", () =>
    runChecks([...checkType.options].map((o) => o.value).filter(Boolean)));
  loadChecks();
}

// Arriving from the Pigu app's "Check a run" button, which links here with
// #checks on the end. The panel is most of a page down, and a page that
// opens at the top after you pressed a button called Check looks like the
// button did nothing.
// Arriving from the Pigu app's "Check a run" button, which links here with
// #checks on the end. It is an overlay now, so there is nothing to scroll to
// -- it simply opens.
if (window.location.hash === "#checks") openCheckOverlay(true);


// -- undo, copy, paste (Phase 11) -------------------------------------------
// Ctrl+Z, Ctrl+C and Ctrl+V do in the editor what they do everywhere else.
// They are deliberately inert while you are typing in a field: a step that
// holds a CSS selector is a text box, and stealing Ctrl+C from it would be
// worse than not having the shortcut at all.

const undoStack = [];
const UNDO_DEPTH = 40;
const undoEditBtn = document.getElementById("undoEditBtn");

function updateUndoButton() {
  if (!undoEditBtn) return;
  undoEditBtn.disabled = undoStack.length === 0;
  undoEditBtn.textContent = undoStack.length ? `Undo (${undoStack.length})` : "Undo";
}

// A whole-list snapshot rather than a diff: the steps are a small tree that
// nests, and "put the previous one back" is both correct and cheap, where
// reversing an individual edit is neither.
function markUndo() {
  if (!editingMacroId) return;
  undoStack.push(JSON.stringify(editingSteps));
  if (undoStack.length > UNDO_DEPTH) undoStack.shift();
  updateUndoButton();
}

function undoOnce() {
  if (!undoStack.length) return;
  editingSteps = JSON.parse(undoStack.pop());
  pickedSteps.clear();
  lastPick = null;
  updateUndoButton();
  renderEditorSteps();
  showNote(editorNote, undoStack.length
    ? `Undone. ${undoStack.length} more step(s) back.`
    : "Undone — back to where this edit started.", "info");
}

if (undoEditBtn) undoEditBtn.addEventListener("click", undoOnce);

function typingInAField(target) {
  if (!target) return false;
  const tag = String(target.tagName || "").toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select" || target.isContentEditable;
}

document.addEventListener("keydown", (event) => {
  if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
  if (editScreen.style.display === "none" || !editingMacroId) return;
  const key = event.key.toLowerCase();
  if (!["c", "v", "z"].includes(key)) return;
  if (typingInAField(event.target)) return;   // the field's own copy/paste wins

  if (key === "c") {
    if (!pickedInOrder().length) return;
    event.preventDefault();
    copyChunkBtn.click();
  } else if (key === "v") {
    if (!chunkClipboard) return;
    event.preventDefault();
    // With no step named, the chunk goes on the end -- which is where "paste"
    // with nothing selected means everywhere else too.
    const at = lastPick && lastPick.array === editingSteps
      ? lastPick.index + 1 : editingSteps.length;
    pasteChunkInto(editingSteps, at, editorStepList);
  } else if (key === "z") {
    if (!undoStack.length) return;
    event.preventDefault();
    undoOnce();
  }
});
