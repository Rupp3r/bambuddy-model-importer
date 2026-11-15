# Bambuddy Model Importer

A lightweight web application that downloads supported 3D model files on the server and uploads them directly to the Bambuddy library. The browser sends a model-page URL or direct download URL; the model file itself is fetched by the importer container, not by the phone or desktop browser.

The importer runs as a separate service. It does not replace or modify Bambuddy.

## Features

- Resolve public/free Printables model pages and list supported files.
- Optional Thingiverse imports through its official API.
- Import direct HTTPS links to supported model files.
- Upload selected files to Bambuddy through its library API.
- Password-protected web interface.
- Configurable maximum download size.

### Supported file formats

STL, 3MF, STEP, STP, OBJ and AMF.

## Supported sources and access requirements

| Source | Requirements |
|---|---|
| Printables | Public/free files exposed through the site's GraphQL API. Paid or Club-restricted files may require an account, purchase or membership and may not be downloadable anonymously. |
| Thingiverse | A Thingiverse App Token is required for the official API. |
| Direct file URL | A public HTTPS URL that directly downloads a supported model file. A model-page URL is not a direct file URL. |

Printables' GraphQL API is not a stable public developer contract and may change. File access remains subject to the source site's permissions and terms.

## How it works

1. Open the importer web interface.
2. Enter the importer password.
3. Paste a supported model-page URL or a direct HTTPS file URL.
4. Select a supported file when the source returns a file list.
5. The importer downloads the file server-side and uploads it to Bambuddy's library API.
6. Open the imported file in Bambuddy and use the slicer configured in that installation.

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
| `BAMBUDDY_API_KEY` | Bambuddy API key with **Manage Library** permission. |
| `IMPORTER_PASSWORD` | Long, unique password used to unlock the importer interface. |

### Optional environment variables

| Variable | Default | Description |
|---|---|---|
| `IMPORTER_IMAGE` | `ghcr.io/rupp3r/bambuddy-model-importer:latest` | Container image to deploy. Set this when using a fork or another registry. |
| `IMPORTER_PORT` | `8097` | Host port mapped to the importer web interface. |
| `THINGIVERSE_TOKEN` | Empty | Thingiverse App Token. |
| `MAX_DOWNLOAD_MB` | `512` | Maximum permitted download size in MiB. |

### Networking

The stack uses its own default Compose network and does not hard-code an existing network name. Set `BAMBUDDY_URL` to an address reachable from the importer container.

The Compose file includes a `host.docker.internal:host-gateway` mapping for Docker environments that support it. This can be used when Bambuddy's API is exposed on the Docker host. If Bambuddy runs in another container, connect the importer to a network that both containers can access, then use Bambuddy's service or container DNS name and API port in `BAMBUDDY_URL`. Configure any external network for your environment in Portainer; do not assume a network name from this example.

Open the web interface at `http://<docker-host>:<IMPORTER_PORT>` (default: `http://<docker-host>:8097`).

### GHCR image

GitHub Actions builds and publishes the container image to GitHub Container Registry when application source files are pushed to the `main` branch.

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

After a new image has been published, redeploy the stack in Portainer and ensure it pulls the latest image. For local Compose deployments:

```bash
docker compose pull
docker compose up -d
```

## Security

- Restrict access to trusted users and networks, such as a private LAN or VPN.
- Do not expose the importer directly to the public internet without an appropriate secure access layer.
- Use a dedicated Bambuddy API key with only the permissions needed to upload files to the library.
- Do not commit `.env` files, API keys, access tokens or passwords.
- The importer only accepts public HTTPS download URLs and rejects destinations that resolve to non-public IP addresses.

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

The health endpoint is available at `/health`.
