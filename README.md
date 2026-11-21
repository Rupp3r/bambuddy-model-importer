# Bambuddy Model Importer

A lightweight web importer that downloads supported 3D model files on the server and uploads them directly to a Bambuddy library. The browser sends a model-page URL or direct download URL; the model file itself is fetched by the importer container, not by the phone or desktop browser.

The importer runs as a separate service. It does not replace or modify Bambuddy.

> **AI-created, personal-use project.** This project is made by AI. It was created for the maintainer's personal use and is shared publicly as-is. It is not a supported product, and there is no commitment to maintain it.

## Project status and expectations

- **AI-generated:** The project code and documentation are created by AI from human-provided direction. AI-generated software can contain mistakes, incomplete behaviour and unexpected bugs. Review it and use it at your own discretion.
- **Personal use:** This project was built to solve a personal need, not as a commercial product or a service with a support obligation.
- **Issues and recommendations:** Bug reports, questions, feature requests and recommendations are welcome, but there is no promise that they will receive a response, be investigated, fixed or implemented. Issues may remain open or be closed without action.
- **Updates may stop:** Development and releases may pause or stop completely at any time, without notice. There is no release schedule, roadmap or guarantee of ongoing compatibility.
- **Use at your own risk:** You are responsible for reviewing the code, securing your deployment and deciding whether the project is suitable for your setup. You may need to maintain or adapt your own fork if the project no longer receives updates.

## Features

- Import public models from several sources without creating accounts on those source sites.
- Resolve supported model pages and list downloadable model files.
- Select and import multiple files from the same model page in one operation.
- Automatically create a Bambuddy subfolder for multi-file model imports.
- Import source tags into Bambuddy's tag catalog and assign them to imported files when the source exposes tags.
- Import direct HTTPS links to supported model files.
- Upload selected files to Bambuddy through its library API.
- Password-protected web interface.
- Configurable maximum download size.
- Choose a Bambuddy File Manager folder, optionally create a title-named subfolder, and override the imported title.
- Set each imported file's External link to the original model page and add a `Source: <URL>` tag when that label fits Bambuddy's 64-character tag-name limit. The External link always retains the full URL.
- Keep a persistent, thumbnail-backed import history with File, Slice and Website actions. File and Website open in the same tab. Sidecar slicing opens a settings dialog for printer, process and filament presets, build plate, auto-arrange and auto-orient; the importer then tracks Bambuddy's slice job and reports failures or links to the completed file. Desktop mode opens the configured desktop slicer.
- Save and slice one unsliced STL or 3MF. Multi-file selections and G-code files are save-only.
- Resolve MakerWorld plates through Bambuddy's native Model Sources API. This requires the configured API key to have MakerWorld/cloud permissions available to its owner.

### Supported file formats

STL, 3MF, G-code, STEP, STP, OBJ and AMF.

## Sources that do not require an account

| Source | Accepted URL | What can be imported |
|---|---|---|
| [Printables](https://www.printables.com/) | Public/free model page | Public/free model files. Paid, Club-only or otherwise restricted files may not be downloadable anonymously. |
| [Shapekit](https://shapekit.cc/3d-models) | Public model page, e.g. `https://shapekit.cc/bird-feeder` | Public STL/3MF and ZIP downloads exposed by the page. ZIP imports are expanded into supported model files. |
| [NASA 3D Resources](https://science.nasa.gov/3d-resources/) | Folder or file page in the [official NASA 3D Resources GitHub repository](https://github.com/nasa/NASA-3D-Resources) | Supported printable files found in that repository folder. The GitHub API is used without a personal token. |
| [NIH 3D](https://3d.nih.gov/) | Public entry page, e.g. `https://3d.nih.gov/entries/3DPX-000914` | Published submission files in supported formats. Uses the public entry and download endpoints; no API key or NIH account is needed. Each model has its own licence. |
| [Smithsonian 3D](https://3d.si.edu/) | Public 3D object page | Public print-ready STL files exposed by the Smithsonian 3D API. Some objects do not have an STL download. Check the individual object's usage conditions before reuse. |
| [Wikimedia Commons](https://commons.wikimedia.org/) | File page for a supported 3D file | Public STL, 3MF, STEP, STP, OBJ or AMF files. The file's licence and creator information are shown when the API provides them. |
| [Internet Archive](https://archive.org/) | Archived Thingiverse item, e.g. `https://archive.org/details/thingiverse-123456` | Original model files in public Thingiverse archive snapshots. This is an archival copy, not a live or complete Thingiverse catalogue. The item's licence metadata is displayed when available. |
| Direct file URL | Public HTTPS URL that directly downloads a supported file | Supported files from any host that does not require a login to fetch the file. A model-page URL is not a direct file URL. |

These sources use public endpoints and do not require source-site accounts, cookies or personal API keys. Public access does not automatically mean unrestricted reuse. Review each model's licence, attribution and usage conditions before sharing or using it commercially.

Printables' GraphQL API and several other sites' undocumented/public endpoints can change. Provider failures are reported by the importer and do not affect the other sources.

### Optional account-gated source

| Source | Requirement |
|---|---|
| [Thingiverse](https://www.thingiverse.com/) live API | Requires a Thingiverse App Token. Obtaining one requires a Thingiverse developer account/app registration, so live Thingiverse API downloads are not part of the account-free integrations. Use the Internet Archive integration for available archived Thingiverse items. |

Sites where the model page is public but the actual download requires a login, personal access token or membership are not described as account-free sources and are not enabled as such.

### Bambuddy-connected source

| Source | Requirement |
|---|---|
| [MakerWorld](https://makerworld.com/) public `/models/{id}` pages | The importer uses Bambuddy's built-in Model Sources resolve/import API. The Bambuddy API key needs **Read Status**, **Manage Library** and **Allow cloud access**; the key owner also needs an active Bambu Cloud login stored in Bambuddy. No MakerWorld credentials are entered into the importer. |

## Browser extension

A Chrome/Chromium Manifest V3 extension is available from the [GitHub Releases](https://github.com/Rupp3r/bambuddy-model-importer/releases/latest) page.

It adds a toolbar shortcut to import models from the same sources supported by this importer: Printables, Shapekit, MakerWorld through Bambuddy, NASA 3D Resources, NIH 3D, Smithsonian 3D, Wikimedia Commons, Internet Archive Thingiverse snapshots, optional live Thingiverse API, and direct HTTPS model-file links. The extension reads the current tab URL, asks the importer to resolve the model, lets you select files, then asks the importer to download and upload them. Multiple selections use the importer's existing folder and source-tag handling.

### Extension setup

1. Download the latest extension ZIP from [Releases](https://github.com/Rupp3r/bambuddy-model-importer/releases/latest) and extract it. The ZIP contains a `bambuddy-model-importer/` folder; extract updates into the same parent folder and allow the files to be overwritten.
2. Open `chrome://extensions/` in Chrome or the equivalent extensions page in a Chromium browser.
3. Enable **Developer mode**, select **Load unpacked**, and choose the extracted extension folder.
4. Open extension **Settings**. Enter the base URL of the importer service and its `IMPORTER_PASSWORD`.
5. Allow the browser permission for that importer host, then click **Test connection and save**.
6. Open a supported model page and click the extension toolbar button.

The extension stores a signed session token in the browser profile, not the importer password or Bambuddy API key. The Bambuddy API key remains server-side in the importer configuration. Model files are downloaded by the importer container, not by the browser.

### Compatibility and releases

The importer and extension use the shared `extension/api-contract.json` contract and versioned `/api/extension/v1/*` endpoints. Additive backend changes and new model sources stay compatible with API v1. Breaking changes must use a new API major version and retain v1 while v1 clients are supported. The extension checks the API major version and required capabilities before it imports.

GitHub Actions checks extension changes whenever the importer is updated. It creates a new extension ZIP and GitHub Release only when files under `extension/` change. Importer-only updates do not rebuild the extension; the last compatible extension release remains available from the same Releases page. Bump `extension/manifest.json` version when changing the extension so each package has a unique release tag.

The extension requires CORS access from browser-extension origins on the importer service. This is enabled for Chrome/Chromium and Firefox extension origins; API calls still require the signed importer session token.

## How it works

1. Open the importer web interface.
2. Enter the importer password once on each browser/device. Keep **Remember this browser on this device** checked to automatically unlock on later visits.
3. Paste a supported model-page URL or a direct HTTPS file URL.
4. Select one or more supported files from the model page.
5. When multiple files are selected, the importer creates a Bambuddy subfolder named after the model and uploads the selected parts into it. Single-file imports stay in the library root. A single Smithsonian or Internet Archive ZIP that expands into several mesh files also gets a subfolder.
6. When the source provides tags or categories, the importer displays them before import, reuses matching Bambuddy tags or creates missing tags, then assigns those tags to the imported files. It also sets the file's External link to the model page and adds a Source URL tag where Bambuddy's tag-name limit allows it.
7. The importer downloads the selected files server-side and uploads them to Bambuddy's library API. You can choose a destination folder, create a title-named subfolder, and override the title.
8. Choose **Save** or **Save and slice** for one STL or unsliced 3MF. Multi-file selections and G-code are save-only. The Slice action follows Bambuddy's configured desktop slicer or queues the configured API sidecar.
9. Use the import history cards to open the saved file in File Manager, start slicing, or revisit the source website. File and Website links stay in the current tab. Sidecar slicing opens a settings dialog and tracks the background job to completion; desktop mode launches the configured desktop slicer.

If the importer is served from a public HTTPS address while Bambuddy is on a private LAN address, the browser may require **Local network** access for the importer site before it can open Bambuddy's File Manager. Allow that permission in the browser's site settings. Chrome's Local Network Access permission protects public-to-LAN requests; the importer prompts from a user click before handing off to a private address.

Slicing uses Bambuddy's configured slicer. The importer does not start a print.

## Deploy with Portainer

The repository includes a generic `compose.yaml` for a Portainer Git Repository stack. It does not assume a particular Docker network, DNS server, host filesystem layout, time zone or private host configuration.

1. Confirm that the importer container will be able to reach your Bambuddy API endpoint.
2. In Portainer, select the Docker environment where you want to deploy the importer.
3. Go to **Stacks → Add stack** and select **Git Repository**.
4. Repository URL: `https://github.com/<OWNER>/<REPOSITORY>`
5. Reference: `refs/heads/main`
6. Compose path: `compose.yaml`
7. Add the required environment variables below.
8. Deploy the stack.

### Required environment variables

| Variable | Description |
|---|---|
| `BAMBUDDY_URL` | Base URL of the Bambuddy API, reachable from the importer container. |
| `BAMBUDDY_API_KEY` | Bambuddy API key with **Read Status** and **Manage Library** permissions. For MakerWorld imports, also enable **Allow cloud access** and make sure the key owner has an active Bambu Cloud login in Bambuddy. |
| `IMPORTER_PASSWORD` | Long, unique password used to unlock the importer interface. |

### Optional environment variables

| Variable | Default | Description |
|---|---|---|
| `IMPORTER_IMAGE` | `ghcr.io/rupp3r/bambuddy-model-importer:latest` | Container image to deploy. Set this when using a fork or another registry. |
| `IMPORTER_PORT` | `8097` | Host port mapped to the importer web interface. |
| `THINGIVERSE_TOKEN` | Empty | Thingiverse App Token for users who already have an eligible developer token. |
| `MAX_DOWNLOAD_MB` | `512` | Maximum permitted download size in MiB. |

### Networking

The stack uses its own default Compose network and does not hard-code an existing network name. Set `BAMBUDDY_URL` to an address reachable from the importer container.

The Compose file includes a `host.docker.internal:host-gateway` mapping for Docker environments that support it. This can be used when Bambuddy's API is exposed on the Docker host. If Bambuddy runs in another container, connect the importer to a network that both containers can access, then use Bambuddy's service or container DNS name and API port in `BAMBUDDY_URL`. Configure any external network for your environment in Portainer.

The Compose file mounts the named `importer-history` volume at `/data`. Keep this volume when updating or recreating the container; deleting it removes the importer's saved history. Older browser-only history is not automatically migrated into the persistent history database.

Open the web interface at `http://<docker-host>:<IMPORTER_PORT>` (default: `http://<docker-host>:8097`).

### GHCR image

GitHub Actions builds and publishes the container image to GitHub Container Registry when application source files or tests are pushed to the `main` branch.

```text
ghcr.io/<owner-lowercase>/bambuddy-model-importer:latest
```

The workflow converts the repository owner's name to lowercase before creating the image tag. Repository visibility and GHCR package visibility are separate. Set the package to **Public** in GitHub Packages if the image should be pulled without registry credentials.

## Deploy with Docker Compose

1. Copy `.env.example` to `.env`.
2. Set `BAMBUDDY_URL`, `BAMBUDDY_API_KEY` and `IMPORTER_PASSWORD`.
3. Run:

```bash
docker compose up -d
```

## Updating

After a new image has been published, redeploy the stack in Portainer and ensure it pulls the latest image. For local Docker Compose deployments:

```bash
docker compose pull
docker compose up -d
```

## Security

- Restrict access to trusted users and networks, such as a private LAN or VPN.
- Do not expose the importer directly to the public internet without an appropriate secure access layer.
- Use a dedicated Bambuddy API key with **Read Status** and **Manage Library**. MakerWorld imports additionally require **Allow cloud access**, and the key owner must have an active Bambu Cloud login stored in Bambuddy.
- The optional **Remember this browser** setting stores a signed importer session token in that browser's local storage for up to one year. The importer password itself and the Bambuddy API key are not stored in the browser. Anyone with access to that browser profile can use the remembered importer session. Use **Forget saved login** on shared devices. Changing `IMPORTER_PASSWORD` invalidates existing remembered sessions.
- Do not commit `.env` files, API keys, access tokens or passwords.
- The importer only accepts public HTTPS download URLs and rejects destinations that resolve to non-public IP addresses.
- Model files and archives are untrusted input. Review imported models before printing them.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export BAMBUDDY_URL=http://<bambuddy-host>:<port>
export BAMBUDDY_API_KEY=<api-key>
export IMPORTER_PASSWORD=<local-password>

uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

Run the test suite with:

```bash
python -m unittest discover -s tests -v
```

The health endpoint is available at `/health`.

