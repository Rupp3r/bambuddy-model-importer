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

### Supported file formats

STL, 3MF, STEP, STP, OBJ and AMF.

## Sources that do not require an account

| Source | Accepted URL | What can be imported |
|---|---|---|
| [Printables](https://www.printables.com/) | Public/free model page | Public/free model files. Paid, Club-only or otherwise restricted files may not be downloadable anonymously. |
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

## How it works

1. Open the importer web interface.
2. Enter the importer password once on each browser/device. Keep **Remember this browser on this device** checked to automatically unlock on later visits.
3. Paste a supported model-page URL or a direct HTTPS file URL.
4. Select one or more supported files from the model page.
5. When multiple files are selected, the importer creates a Bambuddy subfolder named after the model and uploads the selected parts into it. Single-file imports stay in the library root. A single Smithsonian or Internet Archive ZIP that expands into several mesh files also gets a subfolder.
6. When the source provides tags or categories, the importer displays them before import, reuses matching Bambuddy tags or creates missing tags, then assigns those tags to the imported files. Sources that do not expose tags are imported without source tags.
7. The importer downloads the selected files server-side and uploads them to Bambuddy's library API.
8. Open the imported files in Bambuddy and use the slicer configured in that installation.

Importing a model does not automatically slice or start a print. Slicing and printer control remain within Bambuddy and any separately configured services.

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
| `BAMBUDDY_API_KEY` | Bambuddy API key with **Manage Library** permission. This scope is used for uploads, folder creation and tag creation/assignment. |
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
- Use a dedicated Bambuddy API key with the **Manage Library** scope needed for file uploads, folder creation and tag assignment.
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
