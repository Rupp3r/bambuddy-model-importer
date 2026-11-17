const $ = id => document.getElementById(id);
const STORAGE = ["importerUrl", "sessionToken"];
let settings = null;
let contract = null;
let currentModel = null;
let currentTabUrl = "";

function setState(name) {
  for (const id of ["state-config", "state-loading", "state-error", "state-model"]) {
    $(id).classList.toggle("hidden", id !== name);
  }
}
function showError(message, title = "Could not import this page") {
  $("error-title").textContent = title;
  $("error-detail").textContent = message;
  setState("state-error");
}
function setResult(message, isError = false) {
  const el = $("result");
  el.textContent = message;
  el.classList.remove("hidden", "error");
  if (isError) el.classList.add("error");
}
function readStoredSettings() {
  return new Promise(resolve => chrome.storage.local.get(STORAGE, resolve));
}
async function loadContract() {
  if (contract) return contract;
  const response = await fetch(chrome.runtime.getURL("api-contract.json"));
  if (!response.ok) throw new Error("Extension API contract file is missing.");
  contract = await response.json();
  return contract;
}
function endpoint(path) {
  return settings.importerUrl.replace(/\/+$/, "") + path;
}
async function importerFetch(path, options = {}) {
  const headers = {
    "X-Importer-Password": settings.sessionToken,
    ...(options.body ? {"Content-Type":"application/json"} : {}),
    ...(options.headers || {})
  };
  let response;
  try {
    response = await fetch(endpoint(path), {
      method: options.method || "GET",
      headers,
      body: options.body ? JSON.stringify(options.body) : undefined,
      credentials: "omit",
      cache: "no-store",
      redirect: "error"
    });
  } catch (error) {
    throw new Error("Could not reach the importer. Check the importer URL, browser permission and network connection. " + error.message);
  }
  let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok) {
    const detail = data.detail ?? data.message ?? ("HTTP " + response.status);
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}
async function checkCompatibility() {
  const info = await importerFetch(contract.routes.info);
  if (!contract.supported_api_majors.includes(info.api_major)) {
    throw new Error("Importer API v" + info.api_major + " is not supported by this extension. Update the extension or use a compatible importer release.");
  }
  const serverFeatures = new Set(info.features || []);
  const missing = contract.required_features.filter(name => !serverFeatures.has(name));
  if (missing.length) {
    throw new Error("The importer is missing required features: " + missing.join(", ") + ". Update the importer.");
  }
  return info;
}
function updateSelection() {
  const checks = [...document.querySelectorAll(".file-check")];
  const selected = checks.filter(check => check.checked).length;
  $("selection-count").textContent = selected + " selected";
  $("import-selected").disabled = selected === 0;
  $("select-all").textContent = selected === checks.length ? "Clear selection" : "Select all";
}
function selectedFiles() {
  return [...document.querySelectorAll(".file-check:checked")].map(check => check._file);
}
function renderTags(tags) {
  const target = $("source-tags");
  target.replaceChildren();
  if (!Array.isArray(tags) || !tags.length) {
    target.classList.add("hidden");
    return;
  }
  for (const name of tags) {
    const chip = document.createElement("span");
    chip.className = "tag";
    chip.textContent = name;
    target.append(chip);
  }
  target.classList.remove("hidden");
}
function renderFiles(files) {
  const list = $("file-list");
  list.replaceChildren();
  for (const file of files || []) {
    const row = document.createElement("label");
    row.className = "file-row";
    const check = document.createElement("input");
    check.type = "checkbox";
    check.className = "file-check";
    check.checked = true;
    check._file = file;
    check.addEventListener("change", updateSelection);
    const copy = document.createElement("span");
    copy.className = "file-copy";
    const name = document.createElement("span");
    name.className = "file-name";
    name.textContent = file.name || "Model file";
    const details = document.createElement("span");
    details.className = "file-meta";
    const size = Number(file.size) > 0 ? " · " + (Number(file.size) / 1048576).toFixed(2) + " MB" : "";
    details.textContent = (file.kind || "file") + size + (file.note ? " · " + file.note : "");
    copy.append(name, details);
    row.append(check, copy);
    list.append(row);
  }
  updateSelection();
}
async function resolveUrl() {
  const url = $("model-url").value.trim();
  if (!url) throw new Error("Enter a model URL.");
  const data = await importerFetch(contract.routes.resolve, {method:"POST", body:{url}});
  currentModel = data;
  $("source").textContent = data.source || "Model source";
  $("model-title").textContent = data.title || "Untitled model";
  $("file-count").textContent = (data.files || []).length + " file(s)";
  renderTags(data.tags);
  renderFiles(data.files || []);
  setState("state-model");
  $("result").classList.add("hidden");
  $("open-importer").classList.add("hidden");
}
async function boot() {
  setState("state-loading");
  try {
    settings = await readStoredSettings();
    if (!settings.importerUrl || !settings.sessionToken) {
      setState("state-config");
      $("subtitle").textContent = "Extension setup required";
      return;
    }
    contract = await loadContract();
    const [tab] = await chrome.tabs.query({active:true, currentWindow:true});
    currentTabUrl = tab?.url || "";
    $("model-url").value = currentTabUrl;
    $("subtitle").textContent = (() => {
      try { return new URL(currentTabUrl).hostname; } catch { return "Paste a supported model URL"; }
    })();
    await checkCompatibility();
    await resolveUrl();
  } catch (error) {
    showError(error.message);
  }
}
async function importSelected() {
  const files = selectedFiles();
  if (!files.length || !currentModel) return;
  const button = $("import-selected");
  button.disabled = true;
  button.textContent = "Importing…";
  $("result").classList.add("hidden");
  try {
    let result;
    if (currentModel.source === "Direct file URL") {
      result = await importerFetch(contract.routes.import_direct, {
        method:"POST", body:{url:currentModel.url}
      });
      setResult("Imported " + (result.filename || files[0].name) + " into Bambuddy.");
    } else {
      result = await importerFetch(contract.routes.import_batch, {
        method:"POST",
        body:{
          url:currentModel.url,
          files:files.map(file => ({
            file_id:String(file.id),
            file_type:file.kind || null,
            filename:file.name
          }))
        }
      });
      const lines = ["Imported " + (result.files_imported || 0) + " file(s) into Bambuddy."];
      if (result.folder) lines.push("Folder: " + result.folder.name);
      if (result.tags_assigned?.length) lines.push("Tags: " + result.tags_assigned.join(", "));
      if (result.tag_warning) lines.push("Tag warning: " + result.tag_warning);
      if (result.failed_files?.length) {
        lines.push("Failed: " + result.failed_files.map(item => item.filename + " — " + item.error).join("; "));
      }
      setResult(lines.join("\n"), (result.files_imported || 0) === 0);
    }
    $("open-importer").classList.remove("hidden");
    button.textContent = "Import selected files";
    updateSelection();
  } catch (error) {
    setResult(error.message, true);
    button.textContent = "Retry import";
    updateSelection();
  }
}
document.addEventListener("DOMContentLoaded", async () => {
  $("settings").addEventListener("click", () => chrome.runtime.openOptionsPage());
  $("configure").addEventListener("click", () => chrome.runtime.openOptionsPage());
  $("retry").addEventListener("click", boot);
  $("resolve").addEventListener("click", async () => {
    $("resolve").disabled = true;
    try { await resolveUrl(); } catch (error) { showError(error.message); }
    finally { $("resolve").disabled = false; }
  });
  $("select-all").addEventListener("click", () => {
    const checks = [...document.querySelectorAll(".file-check")];
    const shouldCheck = checks.some(check => !check.checked);
    checks.forEach(check => { check.checked = shouldCheck; });
    updateSelection();
  });
  $("import-selected").addEventListener("click", importSelected);
  $("open-importer").addEventListener("click", () => {
    chrome.tabs.create({url: settings.importerUrl});
  });
  await boot();
});
