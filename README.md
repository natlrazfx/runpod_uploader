# RunPod Uploader GUI

A simple cross-platform GUI for uploading and downloading files between your computer and RunPod S3-compatible storage.

## What you need

- Python 3.10+ (Windows or macOS)
- A RunPod account with S3 storage enabled
- S3 credentials for your RunPod storage (Access Key, Secret Key, Bucket/Volume ID)

## Install

Create a virtual environment (optional but recommended) and install dependencies:

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS
source .venv/bin/activate

pip install PySide6 boto3 python-dotenv
```

## Configure credentials

Create a file named `.env` in the same folder as `runpod_uploader_gui.py`.

Minimal configuration:

```ini
RUNPOD_S3_ACCESS_KEY=your_access_key
RUNPOD_S3_SECRET_KEY=your_secret_key
RUNPOD_BUCKET=your_bucket_or_volume_id
RUNPOD_ENDPOINT=https://s3api-eu-cz-1.runpod.io
RUNPOD_REGION=eu-cz-1
LOCAL_ROOT=~/Downloads
```

Where to get these values:

- Access/Secret key: RunPod Console -> Storage -> S3 Credentials
- Bucket/Volume ID: your RunPod volume name or ID in the Console
- Endpoint/Region: shown in the RunPod Console for your storage region

Notes:

- Keep `.env` private. It contains your secrets.
- `LOCAL_ROOT` is the default folder shown on the left side. You can change it later in Settings.

## Run

From the project folder:

```bash
python runpod_uploader_gui.py
```

Quick launchers (same name as the script):

- macOS: double-click `runpod_uploader_gui.command` (make it executable once with `chmod +x runpod_uploader_gui.command`)
- Windows: double-click `runpod_uploader_gui.bat`

On first launch, the Settings dialog opens if credentials are missing.

## How to use the app

Layout:

- Left panel: your local files
- Right panel: RunPod storage

Common actions:

1) Upload
- Select one or more files on the left.
- Click **Upload**.
- If a name conflict exists, choose Replace, Make copy, Rename, or Skip.

2) Download
- Select one or more files (FILE) on the right.
- Click **Download**.
- Files download into the currently selected local folder.

3) Rename or Delete
- Select a single FILE on the right, then **Rename**.
- Select one or more items, then **Delete**.

4) Navigate folders
- Double-click a folder in the right panel to enter.
- Double-click `..` to go up.

5) Settings
- Use **Settings** to edit credentials or change the default local folder.

Windows-only:

- A **Drive** selector appears in the toolbar to switch between drives.

## Optional environment settings

You usually do not need these, but they are available for large uploads:

```ini
RUNPOD_PART_SIZE_MB=64
RUNPOD_MAX_CONCURRENCY=4
RUNPOD_UPLOAD_USE_THREADS=1
RUNPOD_READ_TIMEOUT=7200
RUNPOD_CONNECT_TIMEOUT=30
RUNPOD_UPLOAD_FALLBACK_BUMP=0
```

## Troubleshooting

- "No config": open **Settings** and fill in S3 credentials.
- Listing empty: verify `RUNPOD_BUCKET`, `RUNPOD_ENDPOINT`, and `RUNPOD_REGION`.
- Upload errors: check keys, bucket, and network access; try smaller files first.

## Security

- Do not share your `.env` file.
- If a key is exposed, rotate it in the RunPod Console.


## Don@tes
**If any of this turns out to be useful for you - I’m glad.  
And if you feel like supporting it:  
☕ 1–2 coffees are more than enough ☺️**  

[Click to Buy me a Coffee](buymeacoffee.com/natlrazfx)**
