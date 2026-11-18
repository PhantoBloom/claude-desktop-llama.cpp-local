# Claude Local Proxy (Universal Windows)

Run a local, Anthropic-compatible AI endpoint backed by **llama.cpp + Vulkan**
on any Windows PC with an NVIDIA, AMD, or Intel GPU. Claude Desktop (Chat mode
and Claude Code Local Sessions) then talks to `http://127.0.0.1:8080` as if it
were Anthropic's API, but everything runs on your own GPU.

On first launch it downloads the llama.cpp Vulkan binaries and the Qwen3-8B
model automatically. No Python, no Ollama, no CUDA/ROCm toolkits required.

---

## Requirements

| Item | Minimum | Recommended |
|---|---|---|
| OS | Windows 10 22H2 / Windows 11, 64-bit | Windows 11 |
| GPU | Vulkan 1.1+ capable discrete or integrated GPU | 8 GB+ VRAM discrete GPU |
| GPU driver | Current vendor driver (ships Vulkan runtime) | Latest |
| RAM | 16 GB | 16 GB+ |
| Disk | ~6 GB free on the drive containing `%LOCALAPPDATA%` | 10 GB+ |
| Network | Only needed on first run (downloads) | Any |

**CPU-only machines are not supported.** The Vulkan backend requires a GPU.

Tested GPUs: NVIDIA GeForce (GTX 900 and newer), AMD Radeon (Polaris and newer),
Intel Arc / Iris Xe. Older cards may work but are untested.

---

## Quick start

1. Double-click **`ClaudeLocalProxy.exe`**.
2. Nothing appears for a moment. On first run it downloads:
   - llama.cpp Vulkan binaries (~50 MB) from GitHub
   - Qwen3-8B Q4_K_M model (~4.7 GB) from HuggingFace
3. A **tray icon** appears. Hover it to see progress; the tooltip reads
   `ready on :8080` when the proxy is live.
4. In **Claude Desktop**, set the API base URL to `http://127.0.0.1:8080`.
   (Settings → Developer → Custom API endpoint, or your usual proxy config.)
5. Start chatting.

The first launch takes **5-15 minutes** depending on download speed. Every
later launch takes about 15-40 seconds (loading the model into VRAM).

---

## What gets installed where

Everything lives in one folder, so uninstalling is just deleting it:

```
%LOCALAPPDATA%\ClaudeLocalProxy\
├── config.json          # settings (auto-created on first run)
├── proxy.log            # runtime log
├── llama.cpp\           # downloaded llama-server.exe + DLLs
├── models\              # downloaded .gguf model files
├── downloads\           # temporary partial downloads
└── certs\               # optional TLS certs for HTTPS listeners
```

To find it: press `Win+R`, type `%LOCALAPPDATA%\ClaudeLocalProxy`, press Enter.

---

## Configuration (`config.json`)

Close the app, edit `config.json`, relaunch. The default file looks like this:

```json
{
  "model_repo": "Qwen/Qwen3-8B-GGUF",
  "model_file": "Qwen3-8B-Q4_K_M.gguf",
  "context_size": 16384,
  "threads": 0,
  "device": null,
  "proxy_port": 8080,
  "llama_port": 8081,
  "extra_llama_args": [],
  "skip_llama_download": false,
  "skip_model_download": false,
  "custom_llama_server_exe": null,
  "custom_model_path": null
}
```

| Key | Meaning |
|---|---|
| `model_repo` / `model_file` | HuggingFace repo and GGUF filename to download on first run |
| `context_size` | Model context window in tokens. 16384 is the default; raise to 32768 if you have VRAM headroom |
| `threads` | CPU threads for llama-server. `0` = let llama.cpp pick (recommended) |
| `device` | Vulkan device to use, e.g. `"Vulkan0"`. `null` = let llama.cpp pick the first Vulkan device. Check `proxy.log` for the available list |
| `proxy_port` | Port Claude Desktop connects to. Default 8080 |
| `llama_port` | Internal llama-server port. Default 8081 |
| `extra_llama_args` | Extra arguments passed verbatim to `llama-server.exe` |
| `skip_llama_download` | Set `true` if you already put `llama-server.exe` somewhere and set `custom_llama_server_exe` |
| `skip_model_download` | Set `true` if you already have a `.gguf` and set `custom_model_path` |
| `custom_llama_server_exe` | Absolute path to your own `llama-server.exe` (overrides download) |
| `custom_model_path` | Absolute path to your own `.gguf` (overrides download) |

### Changing the model

Any GGUF from HuggingFace works. For example, a smaller model for a 6 GB card:

```json
{
  "model_repo": "Qwen/Qwen3-8B-GGUF",
  "model_file": "Qwen3-8B-Q4_K_S.gguf"
}
```

Or a different model family entirely:

```json
{
  "model_repo": "bartowski/Qwen2.5-7B-Instruct-GGUF",
  "model_file": "Qwen2.5-7B-Instruct-Q4_K_M.gguf"
}
```

Delete the old file from `models\` to reclaim disk space.

### Selecting a specific GPU

On laptops or desktops with both an integrated GPU and a discrete GPU, llama.cpp
may pick the iGPU. The log (`proxy.log`) prints the device list at startup:

```
[Vulkan] Available devices:
  Vulkan0: Intel(R) UHD Graphics (VRAM: 4096 MiB)
  Vulkan1: NVIDIA GeForce RTX 4060 (VRAM: 8188 MiB)
```

Set `"device": "Vulkan1"` to pin the discrete GPU.

---

## Tray menu

| Item | Action |
|---|---|
| **Open Log** | Open `proxy.log` (useful when something goes wrong) |
| **Open Log Folder** | Open `%LOCALAPPDATA%\ClaudeLocalProxy` |
| **Open Models Folder** | Open the `models\` folder |
| **Open Config** | Open `config.json` |
| **Quit** | Stop the proxy and llama-server cleanly |

Closing the tray icon with **Quit** is the correct way to stop it. Killing the
process from Task Manager can leave `llama-server.exe` running; if that happens,
the next launch will refuse to start and tell you port 8081 is in use.

---

## Troubleshooting

### "Vulkan runtime is not installed"

Your GPU driver is missing or too old. Install the current driver:

- **NVIDIA**: <https://www.nvidia.com/Download/index.aspx>
- **AMD**: <https://www.amd.com/en/support>
- **Intel**: <https://www.intel.com/content/www/us/en/download-center/home.html>

Alternatively, run in an elevated PowerShell:

```powershell
winget install --id KhronosGroup.VulkanRT -e
```

### "Port 8080 is already in use" / "Port 8081 is already in use"

Another copy of the proxy (or a leftover `llama-server.exe`) is running.

```powershell
Get-Process llama-server, ClaudeLocalProxy -ErrorAction SilentlyContinue | Stop-Process -Force
```

Then relaunch. Or change `proxy_port` / `llama_port` in `config.json`.

### The tray icon never reaches "ready"

Open **Open Log** and look at the last lines. Common causes:

- `llama-server.exe exited unexpectedly` → your GPU may not support the
  required Vulkan features. Update the driver first.
- `Timed out waiting for llama-server` → the model is very large for your VRAM.
  Try `Qwen3-8B-Q4_K_S.gguf` or a smaller model.
- `No Windows Vulkan asset found` → GitHub API rate limit. Wait an hour, or
  set `custom_llama_server_exe` to a manually downloaded `llama-server.exe`.

### It runs but responses are extremely slow

- Check `proxy.log` for the selected Vulkan device. If it picked an integrated
  GPU, set `"device": "Vulkan1"` (or whichever index is your discrete card).
- Qwen3-8B Q4_K_M needs ~6 GB VRAM for full offload. On a 4 GB card it will
  partially run on CPU and be slow. Use a smaller quant or model.
- Make sure no other game or rendering app is using the GPU.

### SmartScreen says "Windows protected your PC"

The exe is unsigned. Click **More info → Run anyway**. If you would rather not,
run from source instead: install Python 3.11+ and run
`python run_claude_local_universal.py`.

### Claude Desktop says it cannot connect

Make sure the tray tooltip says `ready on :8080`, then check that Claude
Desktop's API base URL is exactly `http://127.0.0.1:8080` (no trailing slash,
`http` not `https`).

### I want to use my own model file

Put the `.gguf` anywhere and set in `config.json`:

```json
{
  "skip_model_download": true,
  "custom_model_path": "D:\\Models\\my-model.gguf"
}
```

### I want to use my own llama-server.exe

```json
{
  "skip_llama_download": true,
  "custom_llama_server_exe": "C:\\llama.cpp\\llama-server.exe"
}
```

Make sure it is a **Vulkan** build (`bin-win-vulkan-x64`), not a CPU, CUDA, or
SYCL build.

---

## Uninstall

1. Right-click the tray icon → **Quit**.
2. Delete `%LOCALAPPDATA%\ClaudeLocalProxy`.
3. Delete the exe.

That is everything. No registry entries, no services, no scheduled tasks.

---

## Privacy and network

- The proxy listens only on `127.0.0.1`. Nothing on your LAN can reach it.
- Model downloads come from `github.com` (llama.cpp binaries) and
  `huggingface.co` (model weights).
- When you ask the model to search the web, the proxy issues a DuckDuckGo /
  Google News RSS request and a direct fetch of any URL you paste. All other
  inference is fully local.
- Your prompts are never sent anywhere except the local llama-server on
  `127.0.0.1:8081`.

---

## Source and rebuilding

The script is a single self-contained Python file. To rebuild the exe:

```powershell
python -m pip install pyinstaller pystray pillow ddgs pypdf
python -m PyInstaller --noconfirm --clean --onedir --windowed ^
    --name ClaudeLocalProxy ^
    --hidden-import "pystray._win32" ^
    --collect-submodules "pystray" ^
    --collect-submodules "ddgs" ^
    --exclude-module "tkinter" ^
    run_claude_local_universal.py
```

The result is `dist\ClaudeLocalProxy\ClaudeLocalProxy.exe`. Ship the whole
`ClaudeLocalProxy` folder, not just the exe.
