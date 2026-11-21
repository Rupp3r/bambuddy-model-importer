# Bambuddy Model Importer — Browser Extension

Chrome / Chromium toolbar extension for the [Bambuddy Model Importer](https://github.com/Rupp3r/bambuddy-model-importer).

It adds a one-click entry point from supported model pages. The extension reads the active tab URL, sends it to the self-hosted importer, displays the returned files and source tags, and submits selected files for import. It does not call model-site APIs, download model files, or connect to Bambu Cloud.

## Supported sources

The extension delegates source detection and downloads to the importer backend. It supports the same sources as the installed importer version:

- Printables (public/free files)
- Shapekit public model pages
- MakerWorld plates through Bambuddy's built-in Model Sources API; the Bambuddy key must have **Allow cloud access**, and its owner must have an active Bambu Cloud login stored in Bambuddy
- NASA 3D Resources
- NIH 3D
- Smithsonian 3D
- Wikimedia Commons
- Internet Archive Thingiverse snapshots
- Thingiverse live API when the importer has a token configured
- Direct HTTPS model-file URLs

The backend decides whether a URL and its files are supported. Updating the importer can add sources without changing the extension, provided the versioned extension API remains compatible.

## Install

1. Download the latest `bambuddy-model-importer-extension-vX.Y.Z.zip` from the repository's [Releases](https://github.com/Rupp3r/bambuddy-model-importer/releases).
2. Extract the ZIP. It contains a top-level `bambuddy-model-importer/` folder. For an update, extract into the same parent folder as the existing `bambuddy-model-importer` directory and allow the files to be overwritten.
3. Open `chrome://extensions` (or the equivalent extensions page in another Chromium browser).
4. Enable **Developer mode**.
5. Choose **Load unpacked** and select the extracted extension folder.
6. Click the extension icon and open **Settings**.

This is a manually installed extension package, not a Chrome Web Store listing. After extracting an update over the existing folder, return to `chrome://extensions` and reload the extension.

## Configure

1. Enter the base URL of the Bambuddy Model Importer service.
2. Enter the same `IMPORTER_PASSWORD` configured on the importer server.
3. Click **Test connection and save**.
4. Approve the browser permission to contact that importer hostname.

The extension exchanges the password for a signed one-year session token and stores the token in `chrome.storage.local`. The password itself is not saved. The Bambuddy API key remains on the importer server and is never entered in the extension.

To revoke extension sessions, change `IMPORTER_PASSWORD` in the importer deployment and restart/redeploy the importer. Use **Forget connection** to remove the saved token from one browser profile.

## Use

1. Open a supported model page.
2. Click the Bambuddy Model Importer toolbar icon.
3. Review the model title, file list and available source tags.
4. Select one or more files.
5. Click **Import selected files**.

When several files are selected, the importer creates a Bambuddy subfolder named after the model. Source tags and the source URL tag are applied by the importer when Bambuddy's tag-name limit allows it; the original model page is also stored in each file's External link field. Direct file URLs are imported through the same server-side downloader.

## Permissions and privacy

- `activeTab`: reads the current tab URL only after the user clicks the extension action.
- `storage`: stores the importer URL and signed importer session token locally in the browser profile.
- Optional host access: requested only for the configured importer hostname.
- No content scripts, page scraping, analytics, remote scripts or third-party trackers.
- The extension sends the current page URL and selected file identifiers to the configured importer. Model files are downloaded by the importer container.

## API compatibility

The extension and importer share `api-contract.json`. The extension calls versioned `/api/extension/v1/*` routes. Non-breaking backend additions remain compatible with v1. A breaking API change must use a new versioned route; the importer should retain v1 while v1 clients are supported.

GitHub Actions publishes an extension ZIP only when files under `extension/` change. An importer-only update builds the importer image but reuses the last extension release instead of rebuilding the extension.

## Project status

This is an AI-created, personal-use project. It is shared as-is. There is no guarantee of support, issue resolution, feature implementation, future updates or ongoing compatibility. Development may stop at any time without notice. See the main repository README.
