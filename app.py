from __future__ import annotations

import asyncio
import hmac
import hashlib
import json
import ipaddress
import os
import re
import socket
import io
import zipfile
import time
from html import unescape
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlparse, unquote, quote, parse_qs

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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
NASA_TAGS_API = "https://science.nasa.gov/wp-json/wp/v2/tags"
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

EXTENSION_CONTRACT_PATH = Path(__file__).resolve().parent / "extension" / "api-contract.json"
with EXTENSION_CONTRACT_PATH.open("r", encoding="utf-8") as contract_file:
    EXTENSION_API_CONTRACT = json.load(contract_file)

app = FastAPI(title="Bambuddy Model Importer", docs_url=None, redoc_url=None)
# Browser extensions have their own origin. Accept Chromium and Firefox extension
# origins so the extension can call this service cross-origin; API requests still
# require the signed importer session token. No cookies are allowed.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(chrome-extension|moz-extension)://[a-z0-9-]+$",
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Importer-Password"],
    allow_credentials=False,
    max_age=3600,
)


class ResolveRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class ImportRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    file_id: str | None = None
    file_type: str | None = None
    filename: str | None = None


class DirectImportRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class ImportSelection(BaseModel):
    file_id: str = Field(min_length=1, max_length=2048)
    file_type: str | None = None
    filename: str = Field(min_length=1, max_length=255)


class BatchImportRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    files: list[ImportSelection] = Field(min_length=1, max_length=100)


PRINTABLES_FILES_QUERY = """
query ModelFiles($id: ID!) {
  model: print(id: $id) {
    id name slug premium price
    tags { name }
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


SESSION_TOKEN_TTL_SECONDS = 365 * 24 * 60 * 60


def create_session_token(secret: str | None = None, now: int | None = None) -> str:
    """Create a long-lived, signed browser session token without storing the password."""
    signing_secret = APP_PASSWORD if secret is None else secret
    if not signing_secret:
        raise HTTPException(503, "Importer is not configured: set IMPORTER_PASSWORD in the .env file.")
    issued_at = int(time.time() if now is None else now)
    payload = f"bambuddy-importer-session:v1:{issued_at}"
    signature = hmac.new(signing_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"v1.{issued_at}.{signature}"


def verify_session_token(token: str | None, secret: str | None = None, now: int | None = None) -> bool:
    """Verify a signed browser session token. Changing IMPORTER_PASSWORD revokes all tokens."""
    signing_secret = APP_PASSWORD if secret is None else secret
    if not signing_secret or not token:
        return False
    try:
        version, issued_text, supplied_signature = token.split(".", 2)
        if version != "v1" or not issued_text.isdigit():
            return False
        issued_at = int(issued_text)
    except (ValueError, AttributeError):
        return False
    current_time = int(time.time() if now is None else now)
    if issued_at > current_time + 300 or current_time - issued_at > SESSION_TOKEN_TTL_SECONDS:
        return False
    payload = f"bambuddy-importer-session:v1:{issued_at}"
    expected = hmac.new(signing_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied_signature, expected)


def check_password(password: str | None) -> None:
    if not APP_PASSWORD:
        raise HTTPException(503, "Importer is not configured: set IMPORTER_PASSWORD in the .env file.")
    if password is not None and hmac.compare_digest(password, APP_PASSWORD):
        return
    if verify_session_token(password):
        return
    raise HTTPException(401, "Incorrect importer password or expired remembered session.")


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



def normalize_source_tags(*values) -> list[str]:
    """Normalize tag-like values returned by model providers."""
    found: list[str] = []
    seen: set[str] = set()

    def visit(value):
        if value is None or isinstance(value, (bool, int)):
            return
        if isinstance(value, dict):
            for key in ("name", "label", "title", "value", "keyword", "term"):
                item = value.get(key)
                if isinstance(item, str):
                    visit(item)
                    return
            for key in ("tags", "keywords", "subjects", "subject", "categories", "terms"):
                if key in value:
                    visit(value[key])
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                visit(item)
            return
        if not isinstance(value, str):
            return
        name = re.sub(r"\s+", " ", value).strip().strip(" \t\r\n,;")
        if not name or name.isdigit():
            return
        name = name[:64].rstrip()
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            found.append(name)

    for value in values:
        visit(value)
    return found[:40]


def safe_folder_name(value: str) -> str:
    """Turn a source model title into one safe Bambuddy folder name."""
    invalid = '<>:"/\\|?*'
    name = "".join(" " if char in invalid or ord(char) < 32 else char for char in str(value or "Imported model"))
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name or name in {".", ".."}:
        name = "Imported model"
    return name[:120].rstrip(" .") or "Imported model"

async def bambuddy_request(method: str, path: str, **kwargs) -> httpx.Response:
    if not BAMBUDDY_API_KEY:
        raise HTTPException(503, "BAMBUDDY_API_KEY is missing from the importer .env file.")
    headers = kwargs.pop("headers", {})
    headers["X-API-Key"] = BAMBUDDY_API_KEY
    timeout = kwargs.pop("timeout", httpx.Timeout(90, connect=20))
    url = f"{BAMBUDDY_URL}/api/v1/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            return await client.request(method, url, headers=headers, **kwargs)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Could not connect to Bambuddy at {BAMBUDDY_URL}: {exc}") from exc


async def create_bambuddy_folder(name: str) -> int:
    response = await bambuddy_request(
        "POST",
        "/library/folders",
        json={"name": safe_folder_name(name)},
    )
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        raise HTTPException(response.status_code, f"Bambuddy could not create the model folder: {detail}")
    try:
        folder = response.json()
        folder_id = int(folder["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(502, "Bambuddy returned an invalid folder response.") from exc
    return folder_id


async def apply_bambuddy_tags(file_ids: list[int], source_tags: list[str]) -> list[str]:
    """Create missing source tags and assign them to the newly imported files."""
    tags = normalize_source_tags(source_tags)
    file_ids = sorted({int(file_id) for file_id in file_ids if file_id is not None})
    if not tags or not file_ids:
        return []

    listed = await bambuddy_request("GET", "/library/tags/")
    if listed.status_code >= 400:
        try:
            detail = listed.json().get("detail", listed.text)
        except Exception:
            detail = listed.text
        raise HTTPException(listed.status_code, f"Bambuddy could not list library tags: {detail}")
    try:
        existing = listed.json()
    except ValueError as exc:
        raise HTTPException(502, "Bambuddy returned an invalid tag list.") from exc
    if not isinstance(existing, list):
        raise HTTPException(502, "Bambuddy returned an unexpected tag list.")

    by_key = {
        str(item.get("name", "")).strip().casefold(): int(item["id"])
        for item in existing
        if isinstance(item, dict) and item.get("name") and item.get("id") is not None
    }
    tag_ids: list[int] = []
    applied_names: list[str] = []
    for name in tags:
        key = name.casefold()
        tag_id = by_key.get(key)
        if tag_id is None:
            created = await bambuddy_request("POST", "/library/tags/", json={"name": name})
            if created.status_code == 409:
                # Another import may have created this tag between GET and POST.
                refreshed = await bambuddy_request("GET", "/library/tags/")
                if refreshed.status_code >= 400:
                    continue
                try:
                    current_tags = refreshed.json()
                except ValueError:
                    continue
                tag_id = next(
                    (
                        int(item["id"])
                        for item in current_tags
                        if isinstance(item, dict)
                        and str(item.get("name", "")).strip().casefold() == key
                        and item.get("id") is not None
                    ),
                    None,
                )
            elif created.status_code < 400:
                try:
                    payload = created.json()
                    tag_id = int(payload["id"])
                except (ValueError, KeyError, TypeError):
                    tag_id = None
            else:
                continue
        if tag_id is not None:
            tag_ids.append(tag_id)
            applied_names.append(name)

    if not tag_ids:
        return []
    assigned = await bambuddy_request(
        "POST",
        "/library/tags/bulk-assign",
        json={"file_ids": file_ids, "tag_ids": sorted(set(tag_ids)), "action": "add"},
    )
    if assigned.status_code >= 400:
        try:
            detail = assigned.json().get("detail", assigned.text)
        except Exception:
            detail = assigned.text
        raise HTTPException(assigned.status_code, f"Bambuddy could not assign source tags: {detail}")
    return applied_names


async def bambuddy_upload(filename: str, payload: bytes, source_url: str, folder_id: int | None = None) -> dict:
    if not BAMBUDDY_API_KEY:
        raise HTTPException(503, "BAMBUDDY_API_KEY is missing from the importer .env file.")
    params = {"folder_id": folder_id} if folder_id is not None else None
    response = await bambuddy_request(
        "POST",
        "/library/files/",
        params=params,
        timeout=httpx.Timeout(180, connect=20),
        files={"file": (filename, payload, "application/octet-stream")},
    )
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
        "tags": normalize_source_tags(model.get("tags")),
        "paid_or_club": is_paid,
        "files": files,
    }


async def import_printables(url: str, file_id: str, file_type: str, filename: str, folder_id: int | None = None) -> dict:
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
    return await bambuddy_upload(file_name, payload, url, folder_id=folder_id)


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
    return {
        "source": "Thingiverse",
        "model_id": thing_id,
        "title": thing.get("name") or f"Thing {thing_id}",
        "url": url,
        "tags": normalize_source_tags(thing.get("tags"), thing.get("tags_string"), thing.get("category")),
        "files": options,
    }


async def import_thingiverse(url: str, file_id: str, filename: str, folder_id: int | None = None) -> dict:
    resolved = await resolve_thingiverse(url)
    selected = next((f for f in resolved["files"] if f["id"] == str(file_id)), None)
    if not selected:
        raise HTTPException(400, "The selected file does not belong to this Thingiverse model.")
    file_name = safe_filename(filename or selected["name"])
    payload = await download_public_file(selected["download_url"])
    if file_name.lower().endswith(".3mf") and not payload.startswith(b"PK\x03\x04"):
        raise HTTPException(400, "The downloaded .3mf was not a valid ZIP/3MF container.")
    return await bambuddy_upload(file_name, payload, url, folder_id=folder_id)




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


async def import_nasa_github(url: str, file_id: str, filename: str, folder_id: int | None = None) -> dict:
    resolved = await resolve_nasa_github(url)
    selected = next((f for f in resolved["files"] if f["id"] == file_id and f["kind"] == "nasa_github"), None)
    if not selected:
        raise HTTPException(400, "The selected file does not belong to this NASA model folder.")
    file_name = safe_filename(filename or selected["name"])
    payload = await download_public_file(selected["download_url"])
    if file_name.lower().endswith(".3mf") and not payload.startswith(b"PK\x03\x04"):
        raise HTTPException(400, "The downloaded .3mf was not a valid ZIP/3MF container.")
    return await bambuddy_upload(file_name, payload, url, folder_id=folder_id)


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
        "_fields": "id,slug,link,title,excerpt,content,featured_image_url,date,modified,tags",
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
    raw_nasa_tags = item.get("tags") or []
    nasa_tag_names = []
    numeric_tag_ids = [str(value) for value in raw_nasa_tags if str(value).isdigit()]
    if numeric_tag_ids:
        try:
            tag_rows = await external_json(
                NASA_TAGS_API,
                params={"include": ",".join(numeric_tag_ids[:100]), "per_page": 100, "_fields": "id,name"},
            )
            if isinstance(tag_rows, list):
                nasa_tag_names = [row.get("name") for row in tag_rows if isinstance(row, dict)]
        except HTTPException:
            nasa_tag_names = []
    else:
        nasa_tag_names = raw_nasa_tags
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
        "tags": normalize_source_tags(nasa_tag_names),
        "files": files,
    }


async def import_nasa(url: str, file_id: str, filename: str, folder_id: int | None = None) -> dict:
    resolved = await resolve_nasa(url)
    selected = next((f for f in resolved["files"] if f["id"] == file_id and f["kind"] == "nasa"), None)
    if not selected:
        raise HTTPException(400, "The selected file does not belong to this NASA model page.")
    file_name = safe_filename(filename or selected["name"])
    payload = await download_public_file(selected["id"])
    if file_name.lower().endswith(".3mf") and not payload.startswith(b"PK\x03\x04"):
        raise HTTPException(400, "The downloaded .3mf was not a valid ZIP/3MF container.")
    return await bambuddy_upload(file_name, payload, url, folder_id=folder_id)


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
        "tags": normalize_source_tags(meta.get("tags"), meta.get("keywords"), meta.get("subjects"), meta.get("subject")),
        "files": files,
    }


async def import_nih(url: str, file_id: str, filename: str, folder_id: int | None = None) -> dict:
    resolved = await resolve_nih(url)
    selected = next((item for item in resolved["files"] if item["id"] == file_id and item["kind"] == "nih"), None)
    if not selected:
        raise HTTPException(400, "The selected file does not belong to this NIH 3D entry.")
    file_name = safe_filename(filename or selected["name"])
    payload = await download_public_file(selected["id"])
    if file_name.lower().endswith(".3mf") and not payload.startswith(b"PK\x03\x04"):
        raise HTTPException(400, "The downloaded .3mf was not a valid ZIP/3MF container.")
    return await bambuddy_upload(file_name, payload, url, folder_id=folder_id)


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
        "tags": normalize_source_tags(
            [row.get("tags") for row in rows],
            [row.get("categories") for row in rows],
            [(row.get("content") or {}).get("tags") for row in rows],
        ),
        "files": files,
    }


async def import_smithsonian(url: str, file_id: str, filename: str, folder_id: int | None = None) -> dict:
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
                    imported.append(await bambuddy_upload(mesh_name, mesh_data, url, folder_id=folder_id))
        except zipfile.BadZipFile as exc:
            raise HTTPException(400, "The Smithsonian model archive is not a valid ZIP file.") from exc
        return {
            "imported_files": imported,
            "filenames": [item.get("filename") for item in imported if item.get("filename")],
            "source_url": url,
        }
    mesh_name = safe_filename(filename or selected["name"])
    return await bambuddy_upload(mesh_name, payload, url, folder_id=folder_id)



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
        "tags": normalize_source_tags(meta.get("subject"), meta.get("keywords"), meta.get("tags")),
    }


async def import_archive(url: str, file_id: str, filename: str, folder_id: int | None = None) -> dict:
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
                    imported.append(await bambuddy_upload(mesh_name, mesh_data, url, folder_id=folder_id))
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
    return await bambuddy_upload(mesh_name, payload, url, folder_id=folder_id)


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
            "prop": "imageinfo|categories",
            "iiprop": "url|extmetadata",
            "cllimit": 50,
            "clshow": "!hidden",
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
        "tags": normalize_source_tags([
            str(category.get("title", "")).removeprefix("Category:")
            for category in (page.get("categories") or [])
            if isinstance(category, dict)
        ]),
        "files": [{
            "id": direct_url,
            "kind": "wikimedia",
            "name": safe_filename(filename),
            "size": imageinfo.get("size"),
            "note": note,
        }],
    }


async def import_wikimedia(url: str, file_id: str, filename: str, folder_id: int | None = None) -> dict:
    resolved = await resolve_wikimedia(url)
    selected = next((f for f in resolved["files"] if f["id"] == file_id and f["kind"] == "wikimedia"), None)
    if not selected:
        raise HTTPException(400, "The selected file does not belong to this Wikimedia Commons page.")
    file_name = safe_filename(filename or selected["name"])
    payload = await download_public_file(selected["id"])
    if file_name.lower().endswith(".3mf") and not payload.startswith(b"PK\x03\x04"):
        raise HTTPException(400, "The downloaded .3mf was not a valid ZIP/3MF container.")
    return await bambuddy_upload(file_name, payload, url, folder_id=folder_id)




async def resolve_model_source(url: str) -> dict:
    """Resolve one supported model page while retaining server-side download URLs."""
    if parse_printables_id(url):
        return await resolve_printables(url)
    if parse_thingiverse_id(url):
        return await resolve_thingiverse(url)
    if parse_nasa_github_folder(url):
        return await resolve_nasa_github(url)
    if parse_nasa_slug(url):
        return await resolve_nasa(url)
    if parse_nih_id(url):
        return await resolve_nih(url)
    if parse_smithsonian_id(url):
        return await resolve_smithsonian(url)
    if parse_archive_id(url):
        return await resolve_archive(url)
    if parse_wikimedia_title(url):
        return await resolve_wikimedia(url)
    parsed = urlparse(url)
    suffix = PurePosixPath(parsed.path).suffix.lower()
    if suffix in ALLOWED_EXTENSIONS:
        return {
            "source": "Direct file URL",
            "title": PurePosixPath(parsed.path).name,
            "url": url,
            "tags": [],
            "files": [{"id": "direct", "kind": "direct", "name": PurePosixPath(parsed.path).name, "size": None}],
        }
    raise HTTPException(
        400,
        "Supported inputs: public Printables, NASA 3D Resources, NIH 3D, Smithsonian 3D, Wikimedia Commons and Internet Archive model pages; Thingiverse pages with an optional App Token; or direct HTTPS model-file URLs.",
    )


def public_model_response(resolved: dict) -> dict:
    """Remove server-only download URLs and archive internals before returning to the browser."""
    response = dict(resolved)
    response["files"] = []
    for item in resolved.get("files") or []:
        public_item = dict(item)
        public_item.pop("download_url", None)
        public_item.pop("archive", None)
        response["files"].append(public_item)
    return response


async def import_resolved_file(url: str, selected: dict, folder_id: int | None) -> dict:
    file_id = str(selected["id"])
    filename = safe_filename(selected["name"])
    kind = str(selected.get("kind") or "")
    if parse_printables_id(url):
        return await import_printables(url, file_id, kind, filename, folder_id=folder_id)
    if parse_thingiverse_id(url):
        return await import_thingiverse(url, file_id, filename, folder_id=folder_id)
    if parse_nasa_github_folder(url):
        return await import_nasa_github(url, file_id, filename, folder_id=folder_id)
    if parse_nasa_slug(url):
        return await import_nasa(url, file_id, filename, folder_id=folder_id)
    if parse_nih_id(url):
        return await import_nih(url, file_id, filename, folder_id=folder_id)
    if parse_smithsonian_id(url):
        return await import_smithsonian(url, file_id, filename, folder_id=folder_id)
    if parse_archive_id(url):
        return await import_archive(url, file_id, filename, folder_id=folder_id)
    if parse_wikimedia_title(url):
        return await import_wikimedia(url, file_id, filename, folder_id=folder_id)
    raise HTTPException(400, "This URL is not a supported model page. Use the direct-file import field for direct downloads.")


def uploaded_file_ids(results: list[dict]) -> list[int]:
    found = []
    for result in results:
        nested = result.get("imported_files")
        items = nested if isinstance(nested, list) else [result]
        for item in items:
            if isinstance(item, dict) and item.get("id") is not None:
                try:
                    found.append(int(item["id"]))
                except (TypeError, ValueError):
                    continue
    return sorted(set(found))



def needs_model_folder(url: str, selected_files: list[dict]) -> bool:
    if len(selected_files) > 1:
        return True
    return (
        len(selected_files) == 1
        and str(selected_files[0].get("name", "")).lower().endswith(".zip")
        and bool(parse_smithsonian_id(url) or parse_archive_id(url))
    )


async def run_model_import(url: str, selections: list[ImportSelection]) -> dict:
    resolved = await resolve_model_source(url)
    if resolved.get("source") == "Direct file URL":
        raise HTTPException(400, "Use the direct-file import field for direct HTTPS file URLs.")

    selected_files = []
    seen = set()
    for selection in selections:
        file_type = selection.file_type
        match = next(
            (
                item for item in (resolved.get("files") or [])
                if str(item.get("id")) == str(selection.file_id)
                and (file_type is None or str(item.get("kind")) == str(file_type))
            ),
            None,
        )
        if match is None:
            raise HTTPException(400, f"The selected file {selection.filename!r} does not belong to this model page.")
        key = (str(match["id"]), str(match.get("kind") or ""))
        if key not in seen:
            seen.add(key)
            selected_files.append(match)
    if not selected_files:
        raise HTTPException(400, "Select at least one file to import.")

    # All selected files from the same URL stay together; supported source ZIPs
    # also get a folder when one archive expands into multiple model meshes.
    folder_id = None
    folder_name = None
    if needs_model_folder(url, selected_files):
        folder_name = safe_folder_name(str(resolved.get("title") or "Imported model"))
        folder_id = await create_bambuddy_folder(folder_name)

    imported_results = []
    flattened_files = []
    failures = []
    for selected in selected_files:
        try:
            result = await import_resolved_file(url, selected, folder_id)
            imported_results.append(result)
            nested = result.get("imported_files")
            if isinstance(nested, list):
                flattened_files.extend(item for item in nested if isinstance(item, dict))
            else:
                flattened_files.append(result)
        except HTTPException as exc:
            failures.append({
                "filename": selected.get("name"),
                "status": exc.status_code,
                "error": str(exc.detail),
            })
        except Exception as exc:
            failures.append({
                "filename": selected.get("name"),
                "status": 500,
                "error": f"Unexpected import error: {exc.__class__.__name__}",
            })

    file_ids = uploaded_file_ids(imported_results)
    tags_assigned = []
    tag_warning = None
    if file_ids and resolved.get("tags"):
        try:
            tags_assigned = await apply_bambuddy_tags(file_ids, resolved.get("tags") or [])
        except HTTPException as exc:
            tag_warning = str(exc.detail)

    return {
        "source": resolved.get("source"),
        "title": resolved.get("title"),
        "source_url": url,
        "folder": {"id": folder_id, "name": folder_name} if folder_id is not None else None,
        "files_imported": len(flattened_files),
        "imported_files": flattened_files,
        "failed_files": failures,
        "tags_assigned": tags_assigned,
        "tag_warning": tag_warning,
    }


@app.post("/api/extension/v1/session")
@app.post("/api/session")
async def api_session(x_importer_password: str | None = Header(default=None)) -> dict:
    check_password(x_importer_password)
    return {
        "token": create_session_token(),
        "expires_in": SESSION_TOKEN_TTL_SECONDS,
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/api/extension/v1/info")
async def extension_api_info(x_importer_password: str | None = Header(default=None)) -> dict:
    check_password(x_importer_password)
    return {
        "api_name": EXTENSION_API_CONTRACT["api_name"],
        "api_major": EXTENSION_API_CONTRACT["api_major"],
        "contract_revision": EXTENSION_API_CONTRACT["contract_revision"],
        "supported_api_majors": EXTENSION_API_CONTRACT["supported_api_majors"],
        "features": EXTENSION_API_CONTRACT["features"],
        "sources": EXTENSION_API_CONTRACT["sources"],
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return HTML


@app.post("/api/extension/v1/resolve")
@app.post("/api/resolve")
async def api_resolve(body: ResolveRequest, x_importer_password: str | None = Header(default=None)) -> dict:
    check_password(x_importer_password)
    resolved = await resolve_model_source(body.url)
    return public_model_response(resolved)


@app.post("/api/import")
async def api_import(body: ImportRequest, x_importer_password: str | None = Header(default=None)) -> dict:
    check_password(x_importer_password)
    if not body.file_id or not body.filename:
        raise HTTPException(400, "Missing selected file details.")
    return await run_model_import(
        body.url,
        [ImportSelection(file_id=body.file_id, file_type=body.file_type, filename=body.filename)],
    )


@app.post("/api/extension/v1/import-batch")
@app.post("/api/import-batch")
async def api_import_batch(body: BatchImportRequest, x_importer_password: str | None = Header(default=None)) -> dict:
    check_password(x_importer_password)
    return await run_model_import(body.url, body.files)


@app.post("/api/extension/v1/import-direct")
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
.row{display:flex;gap:9px}.row input{flex:1}button{border:0;border-radius:9px;padding:12px 16px;background:var(--accent);color:#052e16;font-weight:700;font:inherit;cursor:pointer;white-space:nowrap}button:disabled{opacity:.5;cursor:wait}.secondary{background:#374151;color:var(--text);border:1px solid #4b5563}
.remember-option{display:flex;align-items:center;gap:9px;margin-top:13px;font-weight:500}.remember-option input{width:18px;min-width:18px;height:18px;margin:0;accent-color:var(--accent)}
.login-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:0 2px;margin:0 0 12px}.login-toolbar button{padding:8px 11px;font-size:13px}
.file{display:flex;align-items:center;justify-content:flex-start;gap:12px;border-top:1px solid var(--line);padding:13px 0}.file:first-child{border-top:0}.file input[type=checkbox]{width:21px;height:21px;flex:0 0 auto;margin:0;accent-color:var(--accent)}.file-info{flex:1;min-width:0}.file small{display:block;color:var(--muted);margin-top:3px;overflow-wrap:anywhere}
.file-actions{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:14px 0}.selection-count{color:var(--muted);font-size:13px;margin-left:10px}.tag-list{display:flex;gap:7px;flex-wrap:wrap;margin:12px 0}.tag-chip{font-size:12px;padding:4px 9px;border:1px solid #4b5563;background:#111827;border-radius:999px;color:#d1d5db}
.status{white-space:pre-wrap;padding:12px;border-radius:9px;background:#111827;color:#d1d5db;margin-top:12px;display:none}.status.error{color:#fecaca;border:1px solid #7f1d1d}.hint{font-size:13px}.hidden{display:none!important}.tag{color:#86efac;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.06em}
@media(max-width:560px){.row{flex-direction:column}.row button{width:100%}.file-actions{align-items:stretch;flex-direction:column}.file-actions button{width:100%}.selection-count{margin-left:0}.file{align-items:flex-start}}
</style></head><body><main>
<div class="head"><div class="mark">⬇</div><div><h1>Bambuddy Model Importer</h1><div class="hint" style="color:var(--muted)">Fetch a public model directly into your Bambuddy library</div></div></div>
<section class="panel" id="loginPanel">
 <label for="password">Importer password</label>
 <div class="row"><input id="password" type="password" autocomplete="current-password" placeholder="Password configured in .env"><button id="unlock">Unlock</button></div>
 <label class="remember-option"><input id="rememberLogin" type="checkbox" checked> Remember this browser on this device</label>
 <div class="hint" style="color:var(--muted);margin-top:8px">When enabled, a signed session token is saved in this browser for up to one year. The importer password and Bambuddy API key are not stored in browser storage.</div>
 <div id="loginStatus" class="status"></div>
</section>
<section id="mainPanel" class="hidden">
 <div class="login-toolbar"><span class="hint">Importer session active on this device</span><button id="forgetLogin" class="secondary" type="button">Forget saved login</button></div>
 <div class="panel"><label for="modelUrl">Model page URL</label><div class="row"><input id="modelUrl" type="url" placeholder="https://www.printables.com/model/..."><button id="resolve">Find files</button></div><p class="hint">No source-site account is needed for public/free Printables, NASA 3D Resources, NIH 3D, Smithsonian print-ready STL files, Wikimedia Commons and archived Thingiverse models on Internet Archive. Select several files below to group them in one Bambuddy subfolder. Source tags are imported automatically when available. Thingiverse's live API still requires its own App Token.</p><div id="resolveStatus" class="status"></div></div>
 <div id="modelPanel" class="panel hidden">
  <div class="tag" id="source"></div>
  <h2 id="modelTitle" style="font-size:19px;margin:7px 0"></h2>
  <div id="sourceTags" class="tag-list hidden"></div>
  <div class="file-actions">
   <div><button id="selectAll" class="secondary" type="button">Select all</button> <span id="selectionCount" class="selection-count">0 selected</span></div>
   <button id="importSelected" type="button" disabled>Import selected files</button>
  </div>
  <p class="hint">Selecting multiple files creates one subfolder in Bambuddy. Source tags are added to every imported file.</p>
  <div id="fileList"></div>
 </div>
 <div class="panel"><label for="directUrl">Direct model-file URL</label><div class="row"><input id="directUrl" type="url" placeholder="https://example.com/model.3mf"><button id="directImport" class="secondary">Import file</button></div><p class="hint">This must be a direct file-download URL, not a model's web page.</p><div id="directStatus" class="status"></div></div>
</section>
<p class="hint">After import, open Bambuddy → File Manager, select the new file, then use the OrcaSlicer sidecar to slice it.</p>
</main>
<script>
let importerPassword='';let currentModel=null;
const $=id=>document.getElementById(id);
function showStatus(id,message,error=false){const el=$(id);el.textContent=message;el.classList.remove('hidden');el.style.display='block';el.classList.toggle('error',error)}
async function api(path,body){const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Importer-Password':importerPassword},body:JSON.stringify(body)});let data={};try{data=await response.json()}catch{}if(!response.ok)throw new Error(data.detail||('HTTP '+response.status));return data}
function selectedFiles(){return [...document.querySelectorAll('.file-check:checked')].map(box=>box._modelFile)}
function updateSelection(){const count=selectedFiles().length;$('selectionCount').textContent=count+' selected';$('importSelected').disabled=count===0}
const REMEMBERED_SESSION_KEY='bambuddy-importer-session-v1';
function showImporter(){ $('loginPanel').classList.add('hidden');$('mainPanel').classList.remove('hidden') }
function clearRememberedSession(){try{localStorage.removeItem(REMEMBERED_SESSION_KEY)}catch{}}
async function restoreRememberedSession(){
 let savedToken='';
 try{savedToken=localStorage.getItem(REMEMBERED_SESSION_KEY)||''}catch{return}
 if(!savedToken)return;
 importerPassword=savedToken;
 try{
  const session=await api('/api/session',{});
  importerPassword=session.token;
  try{localStorage.setItem(REMEMBERED_SESSION_KEY,session.token)}catch{}
  showImporter();
 }catch(e){
  importerPassword='';
  clearRememberedSession();
  showStatus('loginStatus','The remembered login expired or was invalid. Enter the importer password again.',true);
 }
}
$('unlock').onclick=async()=>{
 const enteredPassword=$('password').value;
 if(!enteredPassword){showStatus('loginStatus','Enter the importer password.',true);return}
 $('unlock').disabled=true;
 importerPassword=enteredPassword;
 try{
  if($('rememberLogin').checked){
   const session=await api('/api/session',{});
   importerPassword=session.token;
   try{localStorage.setItem(REMEMBERED_SESSION_KEY,session.token)}
   catch{showStatus('loginStatus','Browser storage is unavailable, so this login will last only until the importer tab is reloaded.',true)}
  }else{
   clearRememberedSession();
  }
  showImporter();
 }catch(e){
  importerPassword='';
  showStatus('loginStatus',e.message,true);
 }finally{$('unlock').disabled=false}
};
$('password').addEventListener('keydown',e=>{if(e.key==='Enter')$('unlock').click()});
$('forgetLogin').onclick=()=>{
 clearRememberedSession();
 importerPassword='';
 $('password').value='';
 $('mainPanel').classList.add('hidden');
 $('loginPanel').classList.remove('hidden');
 showStatus('loginStatus','Saved login removed from this browser.',false);
};
restoreRememberedSession();
$('selectAll').onclick=()=>{const boxes=[...document.querySelectorAll('.file-check')];const all=boxes.length>0&&boxes.every(box=>box.checked);boxes.forEach(box=>box.checked=!all);$('selectAll').textContent=all?'Select all':'Clear selection';updateSelection()};
$('resolve').onclick=async()=>{const url=$('modelUrl').value.trim();if(!url)return showStatus('resolveStatus','Paste a model URL first.',true);$('resolve').disabled=true;$('modelPanel').classList.add('hidden');try{currentModel=await api('/api/resolve',{url:url});$('source').textContent=currentModel.source;$('modelTitle').textContent=currentModel.title;$('fileList').replaceChildren();$('sourceTags').replaceChildren();const sourceTags=currentModel.tags||[];if(sourceTags.length){const heading=document.createElement('strong');heading.textContent='Source tags:';$('sourceTags').append(heading);sourceTags.forEach(name=>{const chip=document.createElement('span');chip.className='tag-chip';chip.textContent=name;$('sourceTags').append(chip)});$('sourceTags').classList.remove('hidden')}else{$('sourceTags').classList.add('hidden')}for(const file of currentModel.files){const row=document.createElement('div');row.className='file';const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.className='file-check';checkbox._modelFile=file;checkbox.addEventListener('change',updateSelection);const info=document.createElement('div');info.className='file-info';const title=document.createElement('strong');title.textContent=file.name;const sub=document.createElement('small');const size=file.size?' • '+(Number(file.size)/1048576).toFixed(2)+' MB':'';sub.textContent=(file.kind||'file')+size+(file.note?' • '+file.note:'');info.append(title,sub);row.append(checkbox,info);$('fileList').append(row)}$('selectAll').textContent='Select all';updateSelection();$('modelPanel').classList.remove('hidden');showStatus('resolveStatus','Found '+currentModel.files.length+' supported file(s). Select the parts to import together.')}catch(e){showStatus('resolveStatus',e.message,true)}finally{$('resolve').disabled=false}};
$('importSelected').onclick=async()=>{const selected=selectedFiles();if(!selected.length)return showStatus('resolveStatus','Select at least one file.',true);$('importSelected').disabled=true;$('importSelected').textContent='Importing…';try{const result=await api('/api/import-batch',{url:currentModel.url,files:selected.map(file=>({file_id:file.id,file_type:file.kind,filename:file.name}))});let message='Imported '+(result.files_imported||0)+' file(s) into Bambuddy.';if(result.folder)message+='\nFolder: '+result.folder.name;if(result.tags_assigned&&result.tags_assigned.length)message+='\nBambuddy tags: '+result.tags_assigned.join(', ');if(result.tag_warning)message+='\nTag warning: '+result.tag_warning;if(result.failed_files&&result.failed_files.length)message+='\nFailed: '+result.failed_files.map(item=>item.filename+': '+item.error).join('; ');showStatus('resolveStatus',message,result.files_imported===0);$('importSelected').textContent='Import selected files';updateSelection();const first=result.imported_files&&result.imported_files[0]&&result.imported_files[0].id;if(first)window.parent.postMessage({type:'bambuddy:navigate',path:'/files?highlight='+encodeURIComponent(first)},'*')}catch(e){showStatus('resolveStatus',e.message,true);$('importSelected').textContent='Retry import';updateSelection()}};
$('directImport').onclick=async()=>{const url=$('directUrl').value.trim();if(!url)return showStatus('directStatus','Paste a direct HTTPS file URL first.',true);$('directImport').disabled=true;try{const result=await api('/api/import-direct',{url});showStatus('directStatus',`Imported ${result.filename||'file'} into Bambuddy. Open File Manager to slice it.`)}catch(e){showStatus('directStatus',e.message,true)}finally{$('directImport').disabled=false}};
</script></body></html>'''
