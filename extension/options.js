const $ = id => document.getElementById(id);
let contract;

function setStatus(message, isError = false) {
  const el = $("status");
  el.textContent = message;
  el.classList.remove("hidden", "error");
  if (isError) el.classList.add("error");
}
function normalizeImporterUrl(value) {
  const parsed = new URL(value.trim());
  if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) {
    throw new Error("Use an http:// or https:// importer URL without embedded credentials.");
  }
  if (parsed.search || parsed.hash) throw new Error("The importer URL must not contain a query string or fragment.");
  return parsed.href.replace(/\/+$/, "");
}
function hostPattern(baseUrl) {
  const parsed = new URL(baseUrl);
  return parsed.protocol + "//" + parsed.hostname + "/*";
}
async function requestPermission(baseUrl) {
  const pattern = hostPattern(baseUrl);
  // Start the permission request directly in the click handler's user gesture.
  const permissionRequest = chrome.permissions.request({origins:[pattern]});
  const granted = await permissionRequest;
  if (!granted) throw new Error("Browser permission to contact the importer was denied.");
}
async function callApi(baseUrl, path, passwordOrToken, method = "GET", body = undefined) {
  const response = await fetch(baseUrl + path, {
    method,
    headers: {
      "X-Importer-Password": passwordOrToken,
      ...(body === undefined ? {} : {"Content-Type":"application/json"})
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "omit",
    cache: "no-store",
    redirect: "error"
  });
  let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok) {
    const detail = data.detail ?? data.message ?? ("HTTP " + response.status);
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}
async function connectAndSave() {
  const button = $("test-save");
  button.disabled = true;
  button.textContent = "Checking…";
  try {
    const baseUrl = normalizeImporterUrl($("importer-url").value);
    const password = $("importer-password").value;
    if (!password) throw new Error("Enter the importer password.");
    await requestPermission(baseUrl);

    // Exchange the password once. Store only the signed session token.
    const session = await callApi(baseUrl, contract.routes.session, password, "POST", {});
    if (!session.token) throw new Error("Importer did not return a session token.");
    const info = await callApi(baseUrl, contract.routes.info, session.token);
    if (!contract.supported_api_majors.includes(info.api_major)) {
      throw new Error("Importer API v" + info.api_major + " is not supported by extension v" + chrome.runtime.getManifest().version + ".");
    }
    const features = new Set(info.features || []);
    const missing = contract.required_features.filter(name => !features.has(name));
    if (missing.length) throw new Error("Importer is missing required features: " + missing.join(", "));

    await chrome.storage.local.set({
      importerUrl: baseUrl,
      sessionToken: session.token,
      apiMajor: info.api_major
    });
    $("importer-password").value = "";
    setStatus("Connected to importer. API v" + info.api_major + " is compatible. Session expires in about one year.");
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Test connection and save";
  }
}
async function loadSettings() {
  $("version").textContent = "v" + chrome.runtime.getManifest().version;
  const stored = await chrome.storage.local.get(["importerUrl"]);
  $("importer-url").value = stored.importerUrl || "";
}
document.addEventListener("DOMContentLoaded", async () => {
  try {
    contract = await loadApiContract();
    await loadSettings();
  } catch (error) {
    setStatus(error.message, true);
  }
  $("test-save").addEventListener("click", connectAndSave);
  $("forget").addEventListener("click", async () => {
    const stored = await chrome.storage.local.get(["importerUrl"]);
    if (stored.importerUrl) {
      try { await chrome.permissions.remove({origins:[hostPattern(stored.importerUrl)]}); } catch {}
    }
    await chrome.storage.local.remove(["importerUrl","sessionToken","apiMajor"]);
    $("importer-url").value = "";
    $("importer-password").value = "";
    setStatus("Connection and its saved browser permission have been removed.");
  });
});
