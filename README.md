# Bambuddy Model Importer (Portainer + GitHub)

This package is prepared for Portainer's **Git repository Stack** deployment. Portainer pulls a prebuilt image from GitHub Container Registry (GHCR); it does not need to build the Dockerfile itself. The source repository contains a GitHub Actions workflow that builds and publishes the image whenever the app source changes.

The importer is a separate container. It does not replace, rebuild, or modify the existing Bambuddy container.

## What it imports

- **Printables:** public/free model files via the public GraphQL endpoint. It lists supported STL, 3MF, STEP, STP, OBJ and AMF files and lets you select one. Paid/Club-restricted files may not be anonymously downloadable.
- **Thingiverse:** optional official API support. A Thingiverse App Token is required; without one, Thingiverse's official API will not provide file downloads.
- **Other sources:** direct HTTPS URLs to a downloadable STL, 3MF, STEP, STP, OBJ or AMF file. A model-page URL from an unsupported host is not a direct download URL.

The importer fetches the selected file on the server and sends it to Bambuddy's library API. It does not send the file through your phone or browser. Slice the imported geometry with the OrcaSlicer sidecar already configured in Bambuddy.

## Part 1 - Put the source on GitHub

1. Create a **public** GitHub repository named `bambuddy-model-importer`.
2. Upload the *contents* of this directory to the repository root. The `.github/workflows/publish-image.yml` file must remain in that exact path. Make sure hidden files (`.github`, `.dockerignore`, `.gitignore`) are included.
3. Open the repository's **Actions** tab. The workflow named **Build and publish importer image** should run on the first push. Wait until it finishes successfully.
4. Open your GitHub profile or organisation's **Packages** page, select `bambuddy-model-importer`, then open **Package settings** and change package visibility to **Public**. The repository can be public while the container package is private, so check this separately. A public GHCR package can be pulled by Portainer without registry credentials.

The published image name is:

`ghcr.io/YOUR_GITHUB_USERNAME/bambuddy-model-importer:latest`

Replace `YOUR_GITHUB_USERNAME` with the lowercase GitHub account or organisation name.

## Part 2 - Create a Bambuddy API key

In Bambuddy, open **Settings → API Keys** and create a key with **Manage Library** permission. Do not grant printer-control or other permissions. Copy the key; it is entered into Portainer and is not committed to GitHub.

## Part 3 - Deploy in Portainer

1. Open Portainer and select the Docker environment that runs Bambuddy.
2. Open **Stacks → Add stack**.
3. Name it `bambuddy-model-importer`.
4. Choose **Git Repository** as the build method.
5. Repository URL: `https://github.com/YOUR_GITHUB_USERNAME/bambuddy-model-importer`
6. Reference: `refs/heads/main`
7. Compose path: `compose.yaml`
8. In **Environment variables**, add the values below. Do not put secrets into the Git repository.

| Variable | Value |
|---|---|
| `IMPORTER_IMAGE` | `ghcr.io/YOUR_GITHUB_USERNAME/bambuddy-model-importer:latest` |
| `BAMBUDDY_URL` | `http://host.docker.internal:8756` |
| `BAMBUDDY_API_KEY` | The API key created in Part 2 |
| `IMPORTER_PASSWORD` | A long, unique password for the importer page |
| `THINGIVERSE_TOKEN` | Leave blank for Printables-only, account-free imports |
| `MAX_DOWNLOAD_MB` | `512` |

`BAMBUDDY_URL` above assumes the existing Bambuddy container uses `network_mode: host` and listens on port `8756`, as in the supplied setup. `host.docker.internal` is mapped to the Docker host by `extra_hosts` in the stack. Do not change the Bambuddy stack to deploy this importer.

9. Click **Deploy the stack**. Portainer pulls the published image from GHCR.

## Part 4 - Open it inside Bambuddy

In Bambuddy, open **Settings → External Links → Add Link** and enter:

- Name: `Model Importer`
- URL: `http://YOUR-DOCKER-HOST:8097`
- Open in New Tab: off

Use the host address that your phone can reach over your VPN. The importer page asks for the `IMPORTER_PASSWORD` set in Portainer. Paste a public/free Printables model URL, select a file, and import it. The file should then appear in Bambuddy's File Manager.

Keep port 8097 reachable only from your trusted LAN/VPN. Do not forward it directly to the public internet.

## Updating the importer

When you push a source-code change to GitHub, Actions rebuilds and publishes the `latest` image. After the workflow completes, open the stack in Portainer and choose **Pull and redeploy** (wording can vary by Portainer version). The Compose file sets `pull_policy: always`, so the current GHCR image is fetched during redeployment.

## OrcaSlicer sidecar reference

`orca-slicer-api.compose.yml` is retained as a separate reference only. Do not deploy a second Orca sidecar if yours is already running. It includes the required `network_mode: gojira_default` and `dns: 8.8.8.8` settings.

## Important limitations

- Printables' GraphQL API is not a stable public developer contract and may change.
- Thingiverse's official API requires a Thingiverse App Token. It cannot be honestly described as account-free.
- Only public/free models that the source exposes anonymously can be fetched without logging in.
