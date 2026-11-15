from __future__ import annotations

import asyncio
import hmac
import ipaddress
import os
import re
import socket
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

APP_PASSWORD = os.environ.get("IMPORTER_PASSWORD", "").strip()
BAMBUDDY_URL = os.environ.get("BAMBUDDY_URL", "http://host.docker.internal:8756").rstrip("/")
BAMBUDDY_API_KEY = os.environ.get("BAMBUDDY_API_KEY", "").strip()
THINGIVERSE_TOKEN = os.environ.get("THINGIVERSE_TOKEN", "").strip()
MAX_DOWNLOAD_BYTES = int(os.environ.get("MAX_DOWNLOAD_MB", "512")) * 1024 * 1024
PRINTABLES_API = "https://api.printables.com/graphql/"
THINGIVERSE_API = "https://api.thingiverse.com"
ALLOWED_EXTENSIONS = {".stl", ".3mf", ".step", ".stp", ".obj", ".amf"}

app = FastAPI(title="Bambuddy Model Importer", docs_url=None, redoc_url=None)


class ResolveRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class ImportRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    file_id: str | None = None
    file_type: str | None = None
    filename: str | None = None


class DirectImportRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


PRINTABLES_FILES_QUERY = """
query ModelFiles($id: ID!) {
  model: print(id: $id) {
    id name slug premium price
    stls { id name fileSize folder note filePreviewPath }
    gcodes { id name fileSize folder note filePreviewPath }
    slas { id name fileSize folder note filePreviewPath }
    otherFiles { id name fileSize folder note filePreviewPath }
    downloadPacks { id name fileSize fileType }
  }
}
"""

PRINTABLES_DOWNLOAD_QUERY = """
mutation GetDownloadLink($id: ID!, $modelId: ID!, $fileType: DownloadFileTypeEnum!) {
  result: getDownloadLink(id: $id, printId: $modelId, fileType: $fileType, source: model_detail) {
    ok
    errors { field messages }
    output { link ttl }
  }
}
"""


class SourceError(Exception):
    pass


def check_password(password: str | None) -> None:
    if not APP_PASSWORD:
        raise HTTPException(503, "Importer is not configured: set IMPORTER_PASSWORD in the .env file.")
    if password is None or not hmac.compare_digest(password, APP_PASSWORD):
        raise HTTPException(401, "Incorrect importer password.")


def parse_printables_id(url: str) -> str | None:
    parsed = urlparse(url if "://" in url else "https://" + url)
    if (parsed.hostname or "").lower() not in {"printables.com", "www.printables.com"}:
        return None
    match = re.search(r"/(?:[a-z]{2}(?:-[a-z]{2,4})?/)?model/(\d+)(?:[-/]|$)", parsed.path, re.I)
    return match.group(1) if match else None


def parse_thingiverse_id(url: str) -> str | None:
    parsed = urlparse(url if "://" in url else "https://" + url)
    if (parsed.hostname or "").lower() not in {"thingiverse.com", "www.thingiverse.com"}:
        return None
    match = re.search(r"(?:/things?/|/thing:|^thing:)(\d+)", parsed.path, re.I)
    return match.group(1) if match else None


def safe_filename(name: str) -> str:
    cleaned = str(name or "model.stl").replace("\\", "/")
    filename = PurePosixPath(cleaned).name.strip().replace("\x00", "")
    if not filename or filename in {".", ".."}:
        filename = "model.stl"
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported model file type: {suffix or '(no extension)'}. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}")
    return filename[:180]


async def _resolve_public_host(host: str) -> None:
    """Reject private/reserved destinations before fetching an upstream URL."""
    if not host or host.lower() in {"localhost", "localhost.localdomain"}:
        raise HTTPException(400, "The download URL has an invalid host.")
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        try:
            records = await asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise HTTPException(502, f"Could not resolve download host: {host}") from exc
        addresses = []
        for record in records:
            try:
                addresses.append(ipaddress.ip_address(record[4][0]))
            except ValueError:
                continue
    if not addresses or any(not address.is_global for address in addresses):
        raise HTTPException(400, "The download URL resolves to a non-public address and was blocked.")


async def validate_download_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(400, "Only public HTTPS download URLs are accepted.")
    await _resolve_public_host(parsed.hostname)


async def download_public_file(url: str) -> bytes:
    current = url
    timeout = httpx.Timeout(90, connect=20)
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "BambuddyModelImporter/1.0"}) as client:
        for _ in range(6):
            await validate_download_url(current)
            async with client.stream("GET", current, follow_redirects=False) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise HTTPException(502, "Download server returned an invalid redirect.")
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    raise HTTPException(502, f"Download server returned HTTP {response.status_code}.")
                length = response.headers.get("content-length")
                if length and length.isdigit() and int(length) > MAX_DOWNLOAD_BYTES:
                    raise HTTPException(413, f"File exceeds the configured {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB limit.")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise HTTPException(413, f"File exceeds the configured {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB limit.")
                    chunks.append(chunk)
                payload = b"".join(chunks)
                if not payload:
                    raise HTTPException(502, "The source returned an empty file.")
                return payload
    raise HTTPException(502, "Too many redirects while downloading the model.")


async def bambuddy_upload(filename: str, payload: bytes, source_url: str) -> dict:
    if not BAMBUDDY_API_KEY:
        raise HTTPException(503, "BAMBUDDY_API_KEY is missing from the importer .env file.")
    endpoint = f"{BAMBUDDY_URL}/api/v1/library/files"
    headers = {"X-API-Key": BAMBUDDY_API_KEY}
    timeout = httpx.Timeout(180, connect=20)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                endpoint,
                headers=headers,
                files={"file": (filename, payload, "application/octet-stream")},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Could not connect to Bambuddy at {BAMBUDDY_URL}: {exc}") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        raise HTTPException(response.status_code, f"Bambuddy rejected the upload: {detail}")
    try:
        result = response.json()
    except ValueError as exc:
        raise HTTPException(502, "Bambuddy returned a non-JSON upload response.") from exc
    result["source_url"] = source_url
    return result


async def printables_graphql(query: str, variables: dict) -> dict:
    timeout = httpx.Timeout(45, connect=15)
    try:
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "BambuddyModelImporter/1.0"}) as client:
            response = await client.post(PRINTABLES_API, json={"query": query, "variables": variables})
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Could not reach the Printables public API: {exc}") from exc
    if response.status_code >= 400:
        raise HTTPException(502, f"Printables API returned HTTP {response.status_code}.")
    try:
        data = response.json()
    except ValueError as exc:
        raise HTTPException(502, "Printables API returned invalid JSON.") from exc
    if data.get("errors"):
        messages = "; ".join(str(x.get("message", x)) for x in data["errors"])
        raise HTTPException(502, f"Printables API error: {messages}")
    return data.get("data") or {}


def _printables_file_options(model: dict) -> list[dict]:
    options: list[dict] = []
    for key, kind in (("stls", "stl"), ("otherFiles", "other")):
        for item in model.get(key) or []:
            name = str(item.get("name") or "").replace("\\", "/").split("/")[-1]
            suffix = PurePosixPath(name).suffix.lower()
            if suffix not in ALLOWED_EXTENSIONS:
                continue
            options.append({
                "id": str(item.get("id", "")),
                "kind": kind,
                "name": name,
                "size": item.get("fileSize"),
                "note": item.get("note"),
            })
    return options


async def resolve_printables(url: str) -> dict:
    model_id = parse_printables_id(url)
    if not model_id:
        raise HTTPException(400, "That is not a supported Printables model URL. Paste a URL like https://www.printables.com/model/123456-name")
    data = await printables_graphql(PRINTABLES_FILES_QUERY, {"id": model_id})
    model = data.get("model")
    if not model:
        raise HTTPException(404, "Printables model not found or not publicly accessible.")
    files = _printables_file_options(model)
    if not files:
        raise HTTPException(404, "No supported STL, 3MF, STEP, OBJ or AMF files were found. G-code, resin and ZIP packs are not offered by this importer.")
    is_paid = bool(model.get("premium")) or (model.get("price") not in (None, 0, "0", "0.0"))
    return {
        "source": "Printables",
        "model_id": model_id,
        "title": model.get("name") or f"Printables model {model_id}",
        "url": url,
        "paid_or_club": is_paid,
        "files": files,
    }


async def import_printables(url: str, file_id: str, file_type: str, filename: str) -> dict:
    model_id = parse_printables_id(url)
    if not model_id:
        raise HTTPException(400, "Invalid Printables model URL.")
    resolved = await resolve_printables(url)
    selected = next((f for f in resolved["files"] if f["id"] == str(file_id) and f["kind"] == file_type), None)
    if not selected:
        raise HTTPException(400, "The selected file does not belong to this public Printables model.")
    file_name = safe_filename(filename or selected["name"])
    result = await printables_graphql(PRINTABLES_DOWNLOAD_QUERY, {
        "id": str(file_id), "modelId": model_id, "fileType": file_type,
    })
    download_result = result.get("result") or {}
    output = download_result.get("output") or {}
    download_url = output.get("link")
    if not download_result.get("ok") or not download_url:
        messages = [m for error in (download_result.get("errors") or []) for m in (error.get("messages") or [])]
        reason = "; ".join(messages) or "Printables did not issue a download link. The file may require a login, purchase or Club membership."
        raise HTTPException(403, reason)
    payload = await download_public_file(download_url)
    if file_name.lower().endswith(".3mf") and not payload.startswith(b"PK\x03\x04"):
        raise HTTPException(400, "The downloaded .3mf was not a valid ZIP/3MF container.")
    return await bambuddy_upload(file_name, payload, url)


async def resolve_thingiverse(url: str) -> dict:
    thing_id = parse_thingiverse_id(url)
    if not thing_id:
        raise HTTPException(400, "That is not a supported Thingiverse thing URL.")
    if not THINGIVERSE_TOKEN:
        raise HTTPException(424, "Thingiverse's official API requires a Thingiverse App Token. Without one, account-free imports from Thingiverse are not supported. Set THINGIVERSE_TOKEN in the importer .env file to enable it.")
    headers = {"Authorization": f"Bearer {THINGIVERSE_TOKEN}", "User-Agent": "BambuddyModelImporter/1.0"}
    try:
        async with httpx.AsyncClient(timeout=45, headers=headers) as client:
            thing_response, files_response = await asyncio.gather(
                client.get(f"{THINGIVERSE_API}/things/{thing_id}"),
                client.get(f"{THINGIVERSE_API}/things/{thing_id}/files"),
            )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Could not reach Thingiverse API: {exc}") from exc
    if thing_response.status_code == 401 or files_response.status_code == 401:
        raise HTTPException(401, "Thingiverse rejected THINGIVERSE_TOKEN. Check the App Token in your importer .env file.")
    if thing_response.status_code == 404 or files_response.status_code == 404:
        raise HTTPException(404, "Thingiverse model was not found.")
    if thing_response.status_code >= 400 or files_response.status_code >= 400:
        raise HTTPException(502, f"Thingiverse API returned HTTP {thing_response.status_code}/{files_response.status_code}.")
    try:
        thing = thing_response.json()
        raw_files = files_response.json()
    except ValueError as exc:
        raise HTTPException(502, "Thingiverse returned invalid JSON.") from exc
    if not isinstance(raw_files, list):
        raise HTTPException(502, "Thingiverse returned an unexpected file list.")
    options=[]
    for item in raw_files:
        name = str(item.get("name") or "").replace("\\", "/").split("/")[-1]
        suffix = PurePosixPath(name).suffix.lower()
        direct_url = item.get("public_url") or item.get("download_url")
        if suffix in ALLOWED_EXTENSIONS and direct_url:
            options.append({"id": str(item.get("id", "")), "kind": "thingiverse", "name": name, "size": item.get("size")})
    if not options:
        raise HTTPException(404, "No supported downloadable STL, 3MF, STEP, OBJ or AMF files found for this Thingiverse model.")
    # Retain the server-fetched URL mapping only in this response; import re-fetches it securely.
    for option in options:
        item = next(x for x in raw_files if str(x.get("id", "")) == option["id"])
        option["download_url"] = item.get("public_url") or item.get("download_url")
    return {"source": "Thingiverse", "model_id": thing_id, "title": thing.get("name") or f"Thing {thing_id}", "url": url, "files": options}


async def import_thingiverse(url: str, file_id: str, filename: str) -> dict:
    resolved = await resolve_thingiverse(url)
    selected = next((f for f in resolved["files"] if f["id"] == str(file_id)), None)
    if not selected:
        raise HTTPException(400, "The selected file does not belong to this Thingiverse model.")
    file_name = safe_filename(filename or selected["name"])
    payload = await download_public_file(selected["download_url"])
    if file_name.lower().endswith(".3mf") and not payload.startswith(b"PK\x03\x04"):
        raise HTTPException(400, "The downloaded .3mf was not a valid ZIP/3MF container.")
    return await bambuddy_upload(file_name, payload, url)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return HTML


@app.post("/api/resolve")
async def api_resolve(body: ResolveRequest, x_importer_password: str | None = Header(default=None)) -> dict:
    check_password(x_importer_password)
    if parse_printables_id(body.url):
        return await resolve_printables(body.url)
    if parse_thingiverse_id(body.url):
        result = await resolve_thingiverse(body.url)
        for item in result["files"]:
            item.pop("download_url", None)
        return result
    parsed = urlparse(body.url)
    suffix = PurePosixPath(parsed.path).suffix.lower()
    if suffix in ALLOWED_EXTENSIONS:
        return {"source": "Direct file URL", "title": PurePosixPath(parsed.path).name, "url": body.url,
                "files": [{"id": "direct", "kind": "direct", "name": PurePosixPath(parsed.path).name, "size": None}]}
    raise HTTPException(400, "Supported inputs: public Printables model links, Thingiverse links (with optional App Token), or direct HTTPS links to a model file.")


@app.post("/api/import")
async def api_import(body: ImportRequest, x_importer_password: str | None = Header(default=None)) -> dict:
    check_password(x_importer_password)
    if parse_printables_id(body.url):
        if not body.file_id or not body.file_type or not body.filename:
            raise HTTPException(400, "Missing selected Printables file details.")
        result = await import_printables(body.url, body.file_id, body.file_type, body.filename)
    elif parse_thingiverse_id(body.url):
        if not body.file_id or not body.filename:
            raise HTTPException(400, "Missing selected Thingiverse file details.")
        result = await import_thingiverse(body.url, body.file_id, body.filename)
    else:
        raise HTTPException(400, "Use the direct file URL import endpoint for direct-download URLs.")
    return result


@app.post("/api/import-direct")
async def api_import_direct(body: DirectImportRequest, x_importer_password: str | None = Header(default=None)) -> dict:
    check_password(x_importer_password)
    parsed = urlparse(body.url)
    filename = safe_filename(PurePosixPath(parsed.path).name)
    payload = await download_public_file(body.url)
    if filename.lower().endswith(".3mf") and not payload.startswith(b"PK\x03\x04"):
        raise HTTPException(400, "The downloaded .3mf was not a valid ZIP/3MF container.")
    return await bambuddy_upload(filename, payload, body.url)


HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bambuddy Model Importer</title>
<style>
:root{color-scheme:dark;--bg:#111827;--panel:#1f2937;--line:#374151;--muted:#9ca3af;--accent:#22c55e;--text:#f9fafb}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:850px;margin:0 auto;padding:22px 16px 48px}.head{display:flex;align-items:center;gap:12px;margin-bottom:22px}.mark{width:42px;height:42px;border-radius:12px;background:#14532d;display:grid;place-items:center;font-size:23px}
h1{font-size:22px;margin:0}p{color:var(--muted);line-height:1.5}.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px;margin:14px 0}
label{display:block;font-weight:600;font-size:14px;margin-bottom:7px}input{width:100%;padding:13px;border:1px solid #4b5563;border-radius:9px;background:#111827;color:var(--text);font:inherit;min-width:0}
.row{display:flex;gap:9px}.row input{flex:1}button{border:0;border-radius:9px;padding:12px 16px;background:var(--accent);color:#052e16;font-weight:700;font:inherit;cursor:pointer;white-space:nowrap}button:disabled{opacity:.5;cursor:wait}.secondary{background:#374151;color:var(--text);border:1px solid #4b5563}.file{display:flex;align-items:center;justify-content:space-between;gap:16px;border-top:1px solid var(--line);padding:13px 0}.file:first-child{border-top:0}.file small{display:block;color:var(--muted);margin-top:3px;overflow-wrap:anywhere}.status{white-space:pre-wrap;padding:12px;border-radius:9px;background:#111827;color:#d1d5db;margin-top:12px;display:none}.status.error{color:#fecaca;border:1px solid #7f1d1d}.hint{font-size:13px}.hidden{display:none!important}.tag{color:#86efac;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.06em}
@media(max-width:560px){.row{flex-direction:column}.row button{width:100%}.file{align-items:flex-start;flex-direction:column}.file button{width:100%}}
</style></head><body><main>
<div class="head"><div class="mark">⬇</div><div><h1>Bambuddy Model Importer</h1><div class="hint" style="color:var(--muted)">Fetch a public model directly into your Bambuddy library</div></div></div>
<section class="panel" id="loginPanel"><label for="password">Importer password</label><div class="row"><input id="password" type="password" autocomplete="current-password" placeholder="Password configured in .env"><button id="unlock">Unlock</button></div><div class="hint" style="color:var(--muted);margin-top:8px">The password is kept only in this browser tab. Your Bambuddy API key stays on the server.</div><div id="loginStatus" class="status"></div></section>
<section id="mainPanel" class="hidden">
 <div class="panel"><label for="modelUrl">Model page URL</label><div class="row"><input id="modelUrl" type="url" placeholder="https://www.printables.com/model/..."><button id="resolve">Find files</button></div><p class="hint">Printables public/free models work without a Printables account. Thingiverse requires an optional App Token. Direct HTTPS links to STL, 3MF, STEP, OBJ or AMF files also work.</p><div id="resolveStatus" class="status"></div></div>
 <div id="modelPanel" class="panel hidden"><div class="tag" id="source"></div><h2 id="modelTitle" style="font-size:19px;margin:7px 0"></h2><div id="fileList"></div></div>
 <div class="panel"><label for="directUrl">Direct model-file URL</label><div class="row"><input id="directUrl" type="url" placeholder="https://example.com/model.3mf"><button id="directImport" class="secondary">Import file</button></div><p class="hint">This must be a direct file-download URL, not a model's web page.</p><div id="directStatus" class="status"></div></div>
</section>
<p class="hint">After import, open Bambuddy → File Manager, select the new file, then use the OrcaSlicer sidecar to slice it.</p>
</main>
<script>
let importerPassword='';let currentModel=null;
const $=id=>document.getElementById(id);
function showStatus(id,message,error=false){const el=$(id);el.textContent=message;el.classList.remove('hidden');el.style.display='block';el.classList.toggle('error',error)}
async function api(path,body){const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Importer-Password':importerPassword},body:JSON.stringify(body)});let data={};try{data=await response.json()}catch{}if(!response.ok)throw new Error(data.detail||`HTTP ${response.status}`);return data}
$('unlock').onclick=()=>{importerPassword=$('password').value;if(!importerPassword){showStatus('loginStatus','Enter the importer password.',true);return}$('loginPanel').classList.add('hidden');$('mainPanel').classList.remove('hidden')};
$('password').addEventListener('keydown',e=>{if(e.key==='Enter')$('unlock').click()});
$('resolve').onclick=async()=>{const url=$('modelUrl').value.trim();if(!url)return showStatus('resolveStatus','Paste a model URL first.',true);$('resolve').disabled=true;$('modelPanel').classList.add('hidden');try{currentModel=await api('/api/resolve',{url});$('source').textContent=currentModel.source;$('modelTitle').textContent=currentModel.title;$('fileList').replaceChildren();for(const file of currentModel.files){const row=document.createElement('div');row.className='file';const info=document.createElement('div');const title=document.createElement('strong');title.textContent=file.name;const sub=document.createElement('small');const size=file.size?` • ${(Number(file.size)/1048576).toFixed(2)} MB`:'';sub.textContent=`${file.kind||'file'}${size}${file.note?' • '+file.note:''}`;info.append(title,sub);const button=document.createElement('button');button.textContent='Import to Bambuddy';button.onclick=()=>doImport(file,button);row.append(info,button);$('fileList').append(row)}$('modelPanel').classList.remove('hidden');showStatus('resolveStatus',`Found ${currentModel.files.length} supported file(s). Select the one to import.`)}catch(e){showStatus('resolveStatus',e.message,true)}finally{$('resolve').disabled=false}};
async function doImport(file,button){button.disabled=true;button.textContent='Importing…';try{const result=await api('/api/import',{url:currentModel.url,file_id:file.id,file_type:file.kind,filename:file.name});button.textContent='Imported';showStatus('resolveStatus',`Imported ${result.filename||file.name} into Bambuddy (library file ID ${result.id??result.library_file_id??'created'}).`);const id=result.id??result.library_file_id;if(id)window.parent.postMessage({type:'bambuddy:navigate',path:`/files?highlight=${encodeURIComponent(id)}`},'*')}catch(e){button.disabled=false;button.textContent='Retry import';showStatus('resolveStatus',e.message,true)}}
$('directImport').onclick=async()=>{const url=$('directUrl').value.trim();if(!url)return showStatus('directStatus','Paste a direct HTTPS file URL first.',true);$('directImport').disabled=true;try{const result=await api('/api/import-direct',{url});showStatus('directStatus',`Imported ${result.filename||'file'} into Bambuddy. Open File Manager to slice it.`)}catch(e){showStatus('directStatus',e.message,true)}finally{$('directImport').disabled=false}};
</script></body></html>'''
