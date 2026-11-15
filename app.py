from __future__ import annotations

import asyncio
import hmac
import ipaddress
import os
import re
import socket
import io
import zipfile
import time
from html import unescape
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlparse, unquote, quote, parse_qs

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
NASA_API = "https://science.nasa.gov/wp-json/wp/v2/topic"
NIH_API = "https://3d.nih.gov/api/entries"
SMITHSONIAN_3D_API = "https://3d-api.si.edu/api/v1.0/content/file/search"
WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
NASA_REPO_API = "https://api.github.com/repos/nasa/NASA-3D-Resources/git/trees/master?recursive=1"
NASA_REPO_RAW = "https://raw.githubusercontent.com/nasa/NASA-3D-Resources/master/"
ARCHIVE_METADATA = "https://archive.org/metadata/"
ARCHIVE_DOWNLOAD = "https://archive.org/download/"
NASA_TREE_CACHE: dict = {"expires_at": 0.0, "tree": []}
POLYHAVEN_API = "https://api.polyhaven.com"
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




def _parsed_url(url: str):
    return urlparse(url if "://" in url else "https://" + url)


def _strip_html(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", str(value)))).strip()


async def external_json(url: str, params: dict | None = None, user_agent: str = "BambuddyModelImporter/1.0"):
    timeout = httpx.Timeout(45, connect=15)
    try:
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": user_agent, "Accept": "application/json"}) as client:
            response = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Could not reach model source API: {exc}") from exc
    if response.status_code == 404:
        raise HTTPException(404, "The model was not found or is not publicly available.")
    if response.status_code >= 400:
        raise HTTPException(502, f"Model source API returned HTTP {response.status_code}.")
    try:
        data = response.json()
    except ValueError as exc:
        raise HTTPException(502, "Model source API returned invalid JSON.") from exc
    if not isinstance(data, (dict, list)):
        raise HTTPException(502, "Model source API returned an unexpected response.")
    return data



def parse_nasa_github_folder(url: str) -> str | None:
    parsed = _parsed_url(url)
    if (parsed.hostname or "").lower() != "github.com":
        return None
    match = re.match(r"^/nasa/NASA-3D-Resources/(?:tree|blob)/master/(.+)$", parsed.path, re.I)
    if not match:
        return None
    path = unquote(match.group(1)).strip("/")
    if not path:
        return None
    if PurePosixPath(path).suffix.lower() in ALLOWED_EXTENSIONS:
        path = path.rsplit("/", 1)[0] if "/" in path else ""
    return path or None


async def resolve_nasa_github(url: str) -> dict:
    folder = parse_nasa_github_folder(url)
    if not folder:
        raise HTTPException(400, "Paste a NASA 3D Resources model folder or file URL from github.com/nasa/NASA-3D-Resources.")
    if not NASA_TREE_CACHE["tree"] or time.monotonic() >= NASA_TREE_CACHE["expires_at"]:
        data = await external_json(NASA_REPO_API, user_agent="BambuddyModelImporter/1.0")
        NASA_TREE_CACHE["tree"] = data.get("tree") or []
        NASA_TREE_CACHE["expires_at"] = time.monotonic() + 21600
    tree = NASA_TREE_CACHE["tree"]
    files = []
    prefix = folder.rstrip("/") + "/"
    for entry in tree:
        path = str(entry.get("path") or "")
        if entry.get("type") != "blob" or not path.startswith(prefix):
            continue
        suffix = PurePosixPath(path).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            continue
        filename = unquote(PurePosixPath(path).name)
        raw_url = NASA_REPO_RAW + quote(path, safe="/")
        files.append({
            "id": path,
            "kind": "nasa_github",
            "name": safe_filename(filename),
            "size": entry.get("size"),
            "note": "NASA media usage guidelines apply",
            "download_url": raw_url,
        })
    if not files:
        raise HTTPException(404, "No supported printable files were found in this NASA 3D Resources folder.")
    files.sort(key=lambda item: (item["name"].lower(), item["id"].lower()))
    title = folder.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
    return {
        "source": "NASA 3D Resources (official GitHub archive)",
        "model_id": folder,
        "title": title,
        "url": url,
        "files": files,
    }


async def import_nasa_github(url: str, file_id: str, filename: str) -> dict:
    resolved = await resolve_nasa_github(url)
    selected = next((f for f in resolved["files"] if f["id"] == file_id and f["kind"] == "nasa_github"), None)
    if not selected:
        raise HTTPException(400, "The selected file does not belong to this NASA model folder.")
    file_name = safe_filename(filename or selected["name"])
    payload = await download_public_file(selected["download_url"])
    if file_name.lower().endswith(".3mf") and not payload.startswith(b"PK\x03\x04"):
        raise HTTPException(400, "The downloaded .3mf was not a valid ZIP/3MF container.")
    return await bambuddy_upload(file_name, payload, url)


def parse_nasa_slug(url: str) -> str | None:
    parsed = _parsed_url(url)
    host = (parsed.hostname or "").lower()
    if host == "science.nasa.gov":
        match = re.match(r"^/3d-resources/([a-z0-9-]+)/?$", parsed.path, re.I)
    elif host == "nasa3d.arc.nasa.gov":
        match = re.match(r"^/detail/([a-z0-9-]+)/?$", parsed.path, re.I)
    else:
        return None
    return match.group(1) if match else None


async def resolve_nasa(url: str) -> dict:
    slug = parse_nasa_slug(url)
    if not slug:
        raise HTTPException(400, "Paste a NASA 3D Resources model page URL.")
    params = {
        "parent": "447593",
        "slug": slug,
        "per_page": 1,
        "_fields": "id,slug,link,title,excerpt,content,featured_image_url,date,modified",
    }
    data = await external_json(NASA_API, params=params)
    if isinstance(data, list):
        item = data[0] if data else None
    else:
        item = data.get("items", [None])[0]
    if not isinstance(item, dict):
        raise HTTPException(404, "NASA 3D model page was not found.")
    rendered = unescape((item.get("content") or {}).get("rendered", ""))
    urls = set()
    for match in re.finditer(r"https://assets\.science\.nasa\.gov/content/dam/[^\"'\s<>]+", rendered, re.I):
        candidate = match.group(0).rstrip("),.;")
        parsed_file = urlparse(candidate)
        if parsed_file.scheme == "https" and (parsed_file.hostname or "").lower() == "assets.science.nasa.gov" and PurePosixPath(parsed_file.path).suffix.lower() in ALLOWED_EXTENSIONS:
            urls.add(candidate)
    if not urls:
        raise HTTPException(404, "No directly downloadable STL, 3MF, STEP, OBJ or AMF files were found on this NASA page.")
    files = []
    for file_url in sorted(urls):
        filename = unquote(PurePosixPath(urlparse(file_url).path).name)
        files.append({
            "id": file_url,
            "kind": "nasa",
            "name": safe_filename(filename),
            "size": None,
            "note": "NASA media usage guidelines apply",
        })
    return {
        "source": "NASA 3D Resources",
        "model_id": str(item.get("id", slug)),
        "title": _strip_html((item.get("title") or {}).get("rendered")) or slug,
        "url": url,
        "files": files,
    }


async def import_nasa(url: str, file_id: str, filename: str) -> dict:
    resolved = await resolve_nasa(url)
    selected = next((f for f in resolved["files"] if f["id"] == file_id and f["kind"] == "nasa"), None)
    if not selected:
        raise HTTPException(400, "The selected file does not belong to this NASA model page.")
    file_name = safe_filename(filename or selected["name"])
    payload = await download_public_file(selected["id"])
    if file_name.lower().endswith(".3mf") and not payload.startswith(b"PK\x03\x04"):
        raise HTTPException(400, "The downloaded .3mf was not a valid ZIP/3MF container.")
    return await bambuddy_upload(file_name, payload, url)


def parse_nih_id(url: str) -> str | None:
    parsed = _parsed_url(url)
    if (parsed.hostname or "").lower() not in {"3d.nih.gov", "www.3d.nih.gov"}:
        return None
    match = re.match(r"^/entries/([a-z0-9_-]+)/?$", parsed.path, re.I)
    return match.group(1) if match else None


async def resolve_nih(url: str) -> dict:
    entry_id = parse_nih_id(url)
    if not entry_id:
        raise HTTPException(400, "Paste an NIH 3D entry URL, for example https://3d.nih.gov/entries/3DPX-000914.")
    entry = await external_json(f"{NIH_API}/{quote(entry_id, safe='')}")
    if not isinstance(entry, dict):
        raise HTTPException(404, "NIH 3D entry was not found.")
    entry = entry.get("data", entry)
    submissions = [
        item for item in (entry.get("submissions") or [])
        if item.get("submissionStatus") == "Published"
    ]
    if not submissions:
        raise HTTPException(404, "This NIH 3D entry has no published submission.")
    submissions.sort(key=lambda item: float(item.get("version") or 0), reverse=True)
    submission = submissions[0]
    meta = submission.get("metadata") or {}
    submission_id = str(submission.get("submissionId") or "")
    if not submission_id:
        raise HTTPException(404, "The NIH 3D entry has no downloadable published submission.")
    source_files = list(submission.get("inputFiles") or [])
    for run in submission.get("workflowRuns") or []:
        source_files.extend(run.get("outputFiles") or [])
    files = []
    seen = set()
    for item in source_files:
        file_id = str(item.get("fileId") or "")
        if not file_id:
            continue
        raw_name = str(item.get("fileName") or item.get("filename") or item.get("name") or "")
        file_format = str(item.get("fileFormat") or "").lower().lstrip(".")
        suffix = PurePosixPath(raw_name).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            if "." + file_format not in ALLOWED_EXTENSIONS:
                continue
            raw_name = (PurePosixPath(raw_name).stem if raw_name else entry_id) + "." + file_format
        try:
            filename = safe_filename(raw_name)
        except HTTPException:
            continue
        download_url = (
            f"https://3d.nih.gov/api/download?submissionId={quote(submission_id, safe='')}"
            f"&fileIds={quote(file_id, safe='')}"
        )
        if download_url in seen:
            continue
        seen.add(download_url)
        license_name = str(meta.get("license") or meta.get("licenseType") or "").strip()
        files.append({
            "id": download_url,
            "kind": "nih",
            "name": filename,
            "size": item.get("fileSize") or item.get("size"),
            "note": f"License: {license_name}" if license_name else "Check the NIH 3D entry for its individual license",
        })
    if not files:
        raise HTTPException(404, "No published NIH 3D files in a supported printable format were found.")
    canonical_id = str(entry.get("threedpxId") or entry.get("entryId") or entry_id)
    return {
        "source": "NIH 3D",
        "model_id": canonical_id,
        "title": str(meta.get("title") or f"NIH 3D {canonical_id}"),
        "url": url,
        "files": files,
    }


async def import_nih(url: str, file_id: str, filename: str) -> dict:
    resolved = await resolve_nih(url)
    selected = next((item for item in resolved["files"] if item["id"] == file_id and item["kind"] == "nih"), None)
    if not selected:
        raise HTTPException(400, "The selected file does not belong to this NIH 3D entry.")
    file_name = safe_filename(filename or selected["name"])
    payload = await download_public_file(selected["id"])
    if file_name.lower().endswith(".3mf") and not payload.startswith(b"PK\x03\x04"):
        raise HTTPException(400, "The downloaded .3mf was not a valid ZIP/3MF container.")
    return await bambuddy_upload(file_name, payload, url)


def parse_smithsonian_id(url: str) -> str | None:
    parsed = _parsed_url(url)
    if (parsed.hostname or "").lower() not in {"3d.si.edu", "www.3d.si.edu", "3d-api.si.edu"}:
        return None
    match = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", url, re.I)
    return match.group(1) if match else None


async def _smithsonian_rows(model_id: str) -> list[dict]:
    data = await external_json(
        SMITHSONIAN_3D_API,
        params={"model_url": f"3d_package:{model_id}", "rows": 200},
    )
    rows = [row for row in (data.get("rows") or []) if isinstance(row, dict) and isinstance(row.get("content"), dict)]
    if not rows:
        raise HTTPException(404, "No public Smithsonian 3D files were found for this object.")
    return rows


def _smithsonian_stl_options(rows: list[dict]) -> list[dict]:
    options = []
    seen = set()
    for row in rows:
        content = row.get("content") or {}
        file_url = str(content.get("uri") or "")
        parsed = urlparse(file_url)
        if content.get("usage") != "Download3D":
            continue
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {"3d-api.si.edu", "ids.si.edu"}:
            continue
        if str(content.get("model_type") or "").lower() != "stl":
            continue
        filename = unquote(PurePosixPath(parsed.path).name)
        suffix = PurePosixPath(filename).suffix.lower()
        if suffix not in {".stl", ".zip"} or file_url in seen:
            continue
        seen.add(file_url)
        options.append({
            "id": file_url,
            "kind": "smithsonian_stl",
            "name": filename,
            "size": content.get("file_size"),
            "note": "Print-ready STL from Smithsonian 3D. Check the object's usage conditions before reuse.",
        })
    return options


async def resolve_smithsonian(url: str) -> dict:
    model_id = parse_smithsonian_id(url)
    if not model_id:
        raise HTTPException(400, "Paste a Smithsonian 3D object page URL containing its model ID.")
    rows = await _smithsonian_rows(model_id)
    files = _smithsonian_stl_options(rows)
    if not files:
        raise HTTPException(404, "This Smithsonian object has no public print-ready STL download.")
    title = next((str(row.get("title")) for row in rows if row.get("title")), f"Smithsonian 3D {model_id}")
    return {
        "source": "Smithsonian 3D",
        "model_id": model_id,
        "title": title,
        "url": url,
        "files": files,
    }


async def import_smithsonian(url: str, file_id: str, filename: str) -> dict:
    resolved = await resolve_smithsonian(url)
    selected = next((f for f in resolved["files"] if f["id"] == file_id and f["kind"] == "smithsonian_stl"), None)
    if not selected:
        raise HTTPException(400, "The selected file does not belong to this Smithsonian 3D object.")
    payload = await download_public_file(selected["id"])
    suffix = PurePosixPath(urlparse(selected["id"]).path).suffix.lower()
    if suffix == ".zip":
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                candidates = [
                    info for info in archive.infolist()
                    if not info.is_dir()
                    and not (info.flag_bits & 1)
                    and PurePosixPath(info.filename.replace("\\", "/")).suffix.lower() == ".stl"
                    and 0 < info.file_size <= MAX_DOWNLOAD_BYTES
                ]
                candidates.sort(key=lambda info: (info.file_size, info.filename.lower()), reverse=True)
                if not candidates:
                    raise HTTPException(404, "The Smithsonian archive contains no STL mesh files.")
                candidates = candidates[:30]
                if sum(info.file_size for info in candidates) > MAX_DOWNLOAD_BYTES:
                    raise HTTPException(413, "The extracted Smithsonian STL files exceed the configured download limit.")
                imported = []
                for info in candidates:
                    mesh_name = safe_filename(PurePosixPath(info.filename.replace("\\", "/")).name)
                    mesh_data = archive.read(info)
                    if len(mesh_data) != info.file_size:
                        raise HTTPException(400, "The Smithsonian archive contains an incomplete STL file.")
                    imported.append(await bambuddy_upload(mesh_name, mesh_data, url))
        except zipfile.BadZipFile as exc:
            raise HTTPException(400, "The Smithsonian model archive is not a valid ZIP file.") from exc
        return {
            "imported_files": imported,
            "filenames": [item.get("filename") for item in imported if item.get("filename")],
            "source_url": url,
        }
    mesh_name = safe_filename(filename or selected["name"])
    return await bambuddy_upload(mesh_name, payload, url)



def parse_archive_id(url: str) -> str | None:
    parsed = _parsed_url(url)
    if (parsed.hostname or "").lower() not in {"archive.org", "www.archive.org"}:
        return None
    match = re.match(r"^/(?:details|download)/(thingiverse-\d{1,12})(?:/|$)", parsed.path, re.I)
    return match.group(1).lower() if match else None


def _archive_license(value: str | None) -> str:
    value = str(value or "").strip()
    match = re.search(r"creativecommons\.org/(licenses|publicdomain)/([a-z-]+)/?([0-9.]*)", value, re.I)
    if not match:
        return value[:120] or "No licence metadata supplied; check the archive item"
    kind, code, version = match.groups()
    if kind.lower() == "publicdomain":
        return "CC0" if code.lower() in {"zero", "mark"} else "Public domain"
    return f"CC {code.upper()}{' ' + version if version else ''}"


async def _archive_item(identifier: str) -> dict:
    data = await external_json(f"{ARCHIVE_METADATA}{quote(identifier, safe='')}")
    if not isinstance(data.get("metadata"), dict) or not isinstance(data.get("files"), list):
        raise HTTPException(404, "That archived Thingiverse item was not found.")
    return data


async def resolve_archive(url: str) -> dict:
    identifier = parse_archive_id(url)
    if not identifier:
        raise HTTPException(400, "Paste an Internet Archive item URL like https://archive.org/details/thingiverse-123456.")
    data = await _archive_item(identifier)
    meta = data["metadata"]
    license_name = _archive_license(meta.get("licenseurl"))
    raw_files = []
    for item in data["files"]:
        name = str(item.get("name") or "")
        if item.get("source") != "original" or not name or "/" in name:
            continue
        suffix = PurePosixPath(name).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS and suffix != ".zip":
            continue
        raw_files.append({
            "id": name,
            "kind": "archive",
            "name": name,
            "size": int(item["size"]) if str(item.get("size", "")).isdigit() else None,
            "note": f"Archive licence: {license_name}",
        })
    if not raw_files:
        raise HTTPException(404, "This Internet Archive item contains no original supported model files.")
    creator = meta.get("creator") or ""
    if isinstance(creator, list):
        creator = ", ".join(str(value) for value in creator[:3])
    return {
        "source": "Internet Archive (Thingiverse archive)",
        "model_id": identifier,
        "title": re.sub(r"\s+\(\d+\)$", "", str(meta.get("title") or identifier)).strip(),
        "url": url,
        "files": raw_files,
        "creator": str(creator),
        "license": license_name,
    }


async def import_archive(url: str, file_id: str, filename: str) -> dict:
    resolved = await resolve_archive(url)
    selected = next((f for f in resolved["files"] if f["id"] == file_id and f["kind"] == "archive"), None)
    if not selected:
        raise HTTPException(400, "The selected file does not belong to this Internet Archive item.")
    download_url = f"{ARCHIVE_DOWNLOAD}{quote(resolved['model_id'], safe='')}/{quote(file_id, safe='')}"
    payload = await download_public_file(download_url)
    suffix = PurePosixPath(file_id).suffix.lower()
    if suffix == ".zip":
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                candidates = [
                    info for info in archive.infolist()
                    if not info.is_dir()
                    and not (info.flag_bits & 1)
                    and PurePosixPath(info.filename.replace("\\", "/")).suffix.lower() in ALLOWED_EXTENSIONS
                    and 0 < info.file_size <= MAX_DOWNLOAD_BYTES
                ]
                candidates.sort(key=lambda info: (info.file_size, info.filename.lower()), reverse=True)
                if not candidates:
                    raise HTTPException(404, "The archive contains no supported model files.")
                candidates = candidates[:30]
                if sum(info.file_size for info in candidates) > MAX_DOWNLOAD_BYTES:
                    raise HTTPException(413, "The extracted model files exceed the configured download limit.")
                imported = []
                for info in candidates:
                    mesh_name = safe_filename(PurePosixPath(info.filename.replace("\\", "/")).name)
                    mesh_data = archive.read(info)
                    if len(mesh_data) != info.file_size:
                        raise HTTPException(400, "The archive contains an incomplete model file.")
                    if mesh_name.lower().endswith(".3mf") and not mesh_data.startswith(b"PK\x03\x04"):
                        continue
                    imported.append(await bambuddy_upload(mesh_name, mesh_data, url))
        except zipfile.BadZipFile as exc:
            raise HTTPException(400, "The Internet Archive model archive is not a valid ZIP file.") from exc
        if not imported:
            raise HTTPException(404, "The archive did not contain any valid supported model files.")
        return {
            "imported_files": imported,
            "filenames": [item.get("filename") for item in imported if item.get("filename")],
            "source_url": url,
        }
    mesh_name = safe_filename(filename or file_id)
    if mesh_name.lower().endswith(".3mf") and not payload.startswith(b"PK\x03\x04"):
        raise HTTPException(400, "The downloaded .3mf was not a valid ZIP/3MF container.")
    return await bambuddy_upload(mesh_name, payload, url)


def parse_wikimedia_title(url: str) -> str | None:
    parsed = _parsed_url(url)
    if (parsed.hostname or "").lower() not in {"commons.wikimedia.org", "www.commons.wikimedia.org"}:
        return None
    match = re.match(r"^/wiki/File:(.+)$", parsed.path, re.I)
    title = unquote(match.group(1)) if match else None
    if not title:
        query_title = parse_qs(parsed.query).get("title", [""])[0]
        if query_title.lower().startswith("file:"):
            title = query_title[5:]
    return title.strip() if title else None


async def resolve_wikimedia(url: str) -> dict:
    file_title = parse_wikimedia_title(url)
    if not file_title:
        raise HTTPException(400, "Paste a Wikimedia Commons File page URL.")
    data = await external_json(
        WIKIMEDIA_API,
        params={
            "action": "query",
            "format": "json",
            "formatversion": 2,
            "prop": "imageinfo",
            "iiprop": "url|extmetadata",
            "titles": f"File:{file_title}",
        },
        user_agent="BambuddyModelImporter/1.0 (https://github.com/Rupp3r/bambuddy-model-importer)",
    )
    pages = (data.get("query") or {}).get("pages") or []
    page = next((p for p in pages if not p.get("missing")), None)
    if not page:
        raise HTTPException(404, "Wikimedia Commons file page was not found.")
    imageinfo = (page.get("imageinfo") or [{}])[0]
    direct_url = imageinfo.get("url")
    if not direct_url:
        raise HTTPException(404, "Wikimedia Commons did not provide a public download URL for this file.")
    parsed_file = urlparse(direct_url)
    if parsed_file.scheme != "https" or (parsed_file.hostname or "").lower() not in {"upload.wikimedia.org", "commons.wikimedia.org"}:
        raise HTTPException(400, "Wikimedia returned an unexpected file host.")
    filename = unquote(PurePosixPath(parsed_file.path).name)
    if PurePosixPath(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(415, "This Wikimedia Commons file is not STL, 3MF, STEP, STP, OBJ or AMF.")
    metadata = imageinfo.get("extmetadata") or {}
    license_meta = metadata.get("LicenseShortName") or {}
    artist_meta = metadata.get("Artist") or {}
    license_value = license_meta.get("value") if isinstance(license_meta, dict) else license_meta
    artist_value = artist_meta.get("value") if isinstance(artist_meta, dict) else artist_meta
    license_name = _strip_html(license_value) or "License not stated in API metadata"
    artist = _strip_html(artist_value)
    note = f"License: {license_name}"
    if artist:
        note += f" | Credit: {artist[:180]}"
    return {
        "source": "Wikimedia Commons",
        "model_id": page.get("title") or file_title,
        "title": page.get("title") or file_title,
        "url": url,
        "files": [{
            "id": direct_url,
            "kind": "wikimedia",
            "name": safe_filename(filename),
            "size": imageinfo.get("size"),
            "note": note,
        }],
    }


async def import_wikimedia(url: str, file_id: str, filename: str) -> dict:
    resolved = await resolve_wikimedia(url)
    selected = next((f for f in resolved["files"] if f["id"] == file_id and f["kind"] == "wikimedia"), None)
    if not selected:
        raise HTTPException(400, "The selected file does not belong to this Wikimedia Commons page.")
    file_name = safe_filename(filename or selected["name"])
    payload = await download_public_file(selected["id"])
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
    if parse_nasa_github_folder(body.url):
        result = await resolve_nasa_github(body.url)
        for item in result["files"]:
            item.pop("download_url", None)
        return result
    if parse_nasa_slug(body.url):
        return await resolve_nasa(body.url)
    if parse_nih_id(body.url):
        return await resolve_nih(body.url)
    if parse_smithsonian_id(body.url):
        return await resolve_smithsonian(body.url)
    if parse_archive_id(body.url):
        return await resolve_archive(body.url)
    if parse_wikimedia_title(body.url):
        return await resolve_wikimedia(body.url)
    parsed = urlparse(body.url)
    suffix = PurePosixPath(parsed.path).suffix.lower()
    if suffix in ALLOWED_EXTENSIONS:
        return {"source": "Direct file URL", "title": PurePosixPath(parsed.path).name, "url": body.url,
                "files": [{"id": "direct", "kind": "direct", "name": PurePosixPath(parsed.path).name, "size": None}]}
    raise HTTPException(400, "Supported inputs: public Printables, NASA 3D Resources, NIH 3D, Smithsonian 3D and Wikimedia Commons model pages; Thingiverse pages with an optional App Token; or direct HTTPS model-file URLs.")


@app.post("/api/import")
async def api_import(body: ImportRequest, x_importer_password: str | None = Header(default=None)) -> dict:
    check_password(x_importer_password)
    if not body.file_id or not body.filename:
        raise HTTPException(400, "Missing selected file details.")
    if parse_printables_id(body.url):
        if not body.file_type:
            raise HTTPException(400, "Missing selected Printables file type.")
        result = await import_printables(body.url, body.file_id, body.file_type, body.filename)
    elif parse_thingiverse_id(body.url):
        result = await import_thingiverse(body.url, body.file_id, body.filename)
    elif parse_nasa_github_folder(body.url):
        result = await import_nasa_github(body.url, body.file_id, body.filename)
    elif parse_nasa_slug(body.url):
        result = await import_nasa(body.url, body.file_id, body.filename)
    elif parse_nih_id(body.url):
        result = await import_nih(body.url, body.file_id, body.filename)
    elif parse_smithsonian_id(body.url):
        result = await import_smithsonian(body.url, body.file_id, body.filename)
    elif parse_archive_id(body.url):
        result = await import_archive(body.url, body.file_id, body.filename)
    elif parse_wikimedia_title(body.url):
        result = await import_wikimedia(body.url, body.file_id, body.filename)
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
 <div class="panel"><label for="modelUrl">Model page URL</label><div class="row"><input id="modelUrl" type="url" placeholder="https://www.printables.com/model/..."><button id="resolve">Find files</button></div><p class="hint">No source-site account is needed for public/free Printables, NASA 3D Resources, NIH 3D, Smithsonian print-ready STL files, Wikimedia Commons and archived Thingiverse models on Internet Archive. Direct HTTPS model-file links also work. Thingiverse's live API still requires its own App Token.</p><div id="resolveStatus" class="status"></div></div>
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
async function doImport(file,button){button.disabled=true;button.textContent='Importing…';try{const result=await api('/api/import',{url:currentModel.url,file_id:file.id,file_type:file.kind,filename:file.name});button.textContent='Imported';const importedNames=result.filenames?.join(', ')||result.imported_files?.map(item=>item.filename).filter(Boolean).join(', ');showStatus('resolveStatus',`Imported ${importedNames||result.filename||file.name} into Bambuddy library.`);const id=result.id??result.library_file_id;if(id)window.parent.postMessage({type:'bambuddy:navigate',path:`/files?highlight=${encodeURIComponent(id)}`},'*')}catch(e){button.disabled=false;button.textContent='Retry import';showStatus('resolveStatus',e.message,true)}}
$('directImport').onclick=async()=>{const url=$('directUrl').value.trim();if(!url)return showStatus('directStatus','Paste a direct HTTPS file URL first.',true);$('directImport').disabled=true;try{const result=await api('/api/import-direct',{url});showStatus('directStatus',`Imported ${result.filename||'file'} into Bambuddy. Open File Manager to slice it.`)}catch(e){showStatus('directStatus',e.message,true)}finally{$('directImport').disabled=false}};
</script></body></html>'''
