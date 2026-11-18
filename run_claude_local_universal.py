r"""
Claude Local Proxy - Universal Windows build (Vulkan, any GPU vendor).

Self-bootstrapping: on first run it downloads the llama.cpp Vulkan binaries and
the Qwen3-8B model into %LOCALAPPDATA%\ClaudeLocalProxy, then starts a
Vulkan-backed llama-server plus an Anthropic-compatible HTTP proxy on :8080.

Requires a Vulkan-capable GPU (NVIDIA, AMD, or Intel). CPU-only is not supported.
"""

import ssl
import threading
import webbrowser
import tempfile
import base64
import html as html_lib
import re
import socket
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile

try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        DDGS = None

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

_IS_WINDOWS = sys.platform == "win32"
_NO_WINDOW_FLAGS = 0x08000000 if _IS_WINDOWS else 0
_IS_FROZEN = bool(getattr(sys, "frozen", False))
_LOG_FILE = None
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# --------------------------------------------------------------------------
# Paths and configuration
# --------------------------------------------------------------------------


def _appdata_dir():
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or tempfile.gettempdir()
    p = os.path.join(base, "ClaudeLocalProxy")
    try:
        os.makedirs(p, exist_ok=True)
    except Exception:
        p = tempfile.gettempdir()
    return p


APPDATA_DIR = _appdata_dir()
LLAMA_DIR = os.path.join(APPDATA_DIR, "llama.cpp")
MODELS_DIR = os.path.join(APPDATA_DIR, "models")
DOWNLOADS_DIR = os.path.join(APPDATA_DIR, "downloads")
CONFIG_PATH = os.path.join(APPDATA_DIR, "config.json")
CERTS_DIR = os.path.join(APPDATA_DIR, "certs")

DEFAULT_CONFIG = {
    "model_repo": "Qwen/Qwen3-8B-GGUF",
    "model_file": "Qwen3-8B-Q4_K_M.gguf",
    "context_size": 16384,
    "threads": 0,
    "device": None,
    "proxy_port": 8080,
    "llama_port": 8081,
    "extra_llama_args": [],
    "skip_llama_download": False,
    "skip_model_download": False,
    "custom_llama_server_exe": None,
    "custom_model_path": None,
}

# Reassigned from config at startup.
PROXY_PORT = 8080
TARGET_URL = "http://127.0.0.1:8081"
VISION_SIDECAR_URL = "http://127.0.0.1:8082/v1/chat/completions"


def _load_or_create_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            if isinstance(user_cfg, dict):
                cfg.update(user_cfg)
        except Exception as e:
            print(f"[Config] Could not parse {CONFIG_PATH}: {e} - using defaults", flush=True)
    else:
        try:
            os.makedirs(APPDATA_DIR, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
            print(f"[Config] Created default config at {CONFIG_PATH}", flush=True)
        except Exception as e:
            print(f"[Config] Could not write default config: {e}", flush=True)
    return cfg


# --------------------------------------------------------------------------
# Web search / fetch helpers (unchanged from the tested version)
# --------------------------------------------------------------------------

WEB_SEARCH_TOOL = {
    "name": "web_search",
    "description": "Search the live internet using DuckDuckGo for recent news, facts, or documentation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"}
        },
        "required": ["query"],
    },
}

_FILLER_PREFIXES = (
    "search the web for", "search the internet for", "search online for",
    "search for", "search up", "search it up", "search that up", "search this up",
    "search the web", "search online", "search the internet",
    "look up", "look it up", "look that up", "look this up", "look online", "lookup", "look for",
    "google it", "google that", "google this", "google for", "google",
    "find online", "find it online", "find out about", "find info on", "find info about",
    "find information on", "find information about", "find details on", "find details about",
    "the latest news about", "latest news about", "recent news about", "breaking news about",
    "what's the latest on", "what is the latest on", "what's the latest about", "what is the latest about",
    "current news about", "current news on",
    "give me detailed information about", "give me detailed info on", "give me info on",
    "detailed information about", "detailed information on", "detailed info about", "detailed info on",
    "information about", "information on", "info about", "info on",
    "details about", "details on", "detail about", "detail on",
    "tell me everything about", "tell me everything on", "tell me about", "tell me",
    "summarize the top", "check online", "check the web", "check the internet",
    "find on the web", "find on the internet", "find it on the web", "find it on the internet",
)

_LEADING_MARKERS = (
    "hey ", "hi ", "hello ", "ok ", "okay ", "so ", "well ", "now ", "then ",
    "please ", "can you ", "could you ", "would you ", "will you ", "do you ",
    "i need you to ", "i want you to ", "go ahead and ", "just ",
)


def _clean_search_query(query):
    if not query:
        return ""
    q = query.strip()
    changed = True
    while changed and q:
        changed = False
        low = q.lower()
        for lead in _LEADING_MARKERS:
            if low.startswith(lead):
                q = q[len(lead):].strip(" .?!,;:")
                changed = True
                break
        if changed:
            continue
        low = q.lower()
        for p in _FILLER_PREFIXES:
            if low.startswith(p):
                q = q[len(p):].strip(" .?!,;:")
                changed = True
                break
        if changed:
            continue
        for tail in (" please", " for me"):
            if q.lower().endswith(tail):
                q = q[:-len(tail)].strip(" .?!,;:")
                changed = True
                break
    return q or query.strip()


WANTS_WEB_PATTERN = re.compile(
    r"(?i)("
    r"\bsearch\s+(?:the\s+(?:web|internet)\s+for\s+|online\s+for\s+|for\s+|up\s+|it\s+up|that\s+up|this\s+up|the\s+web|online|the\s+internet|it|that|this)\b|"
    r"\bgoogle\s+(?:it|that|this|for\s+)?\b|"
    r"\blook\s+(?:it|that|this)?\s*up\b|"
    r"\blook\s+online\b|"
    r"\blookup\b|"
    r"\bfind\s+(?:it\s+|that\s+|this\s+)?(?:online|on\s+the\s+(?:web|internet))\b|"
    r"\bcheck\s+(?:online|the\s+web|the\s+internet)\b|"
    r"\b(?:latest|current|recent|breaking|today'?s)\s+news\b|"
    r"\bnews\s+(?:about|on|for)\b|"
    r"\bwhat'?s\s+the\s+latest\b|"
    r"\bweather\s+(?:in|for|today|forecast)\b|"
    r"\bstock\s+price\b"
    r")"
)


def _build_search_query(latest_u, all_msgs):
    q = _clean_search_query(latest_u)
    if len(q) >= 10:
        return q
    for m in reversed(all_msgs[:-1]):
        if m.get("role") != "user":
            continue
        c = m.get("content", "")
        prev_text = (
            c if isinstance(c, str)
            else " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
        )
        prev_clean = _clean_search_query(prev_text)
        if len(prev_clean) >= 10:
            return prev_clean
    return q


def run_host_web_search(query, max_results=6):
    q = _clean_search_query(query)
    if not q:
        return "No search query provided."
    print(f"[WebSearch] Searching host internet for: {q}", flush=True)
    results = []

    if DDGS is not None:
        try:
            with DDGS() as ddgs:
                for r in ddgs.news(q, max_results=max_results):
                    results.append(
                        f"- {r.get('title', '')} ({r.get('source', '')} - {r.get('date', '')}): {r.get('body', '')}"
                    )
                if not results:
                    for r in ddgs.text(q, max_results=max_results):
                        results.append(f"- {r.get('title', '')} ({r.get('href', '')}): {r.get('body', '')}")
        except Exception as e:
            print(f"[WebSearch] DDGS fallback triggered: {e}", flush=True)

    if not results:
        try:
            rss_url = (
                f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}"
                f"&hl=en-US&gl=US&ceid=US:en"
            )
            req = urllib.request.Request(rss_url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=8) as resp:
                xml_data = resp.read()
            root = ET.fromstring(xml_data)
            for item in root.findall(".//item")[:max_results]:
                title = item.findtext("title", "")
                pub_date = item.findtext("pubDate", "")
                source = item.findtext("source", "")
                results.append(f"- {title} ({source} - {pub_date})")
        except Exception as e:
            return f"Search error: {e}"

    return "\n".join(results) if results else "No search results found."


def fetch_webpage(url, max_chars=10000, timeout=15):
    print(f"[WebFetch] Fetching: {url}", flush=True)
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "") or ""
            raw = resp.read(3_000_000)

        charset = "utf-8"
        m = re.search(r"charset=([^;\s]+)", content_type, re.I)
        if m:
            charset = m.group(1).strip().strip("\"'")
        try:
            text = raw.decode(charset, errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")

        if "html" not in content_type.lower() and not text.lstrip().startswith("<"):
            if len(text) > max_chars:
                text = text[:max_chars] + f"\n[... truncated at {max_chars} chars ...]"
            return f"[{url}]\n{text}"

        title = url
        mt = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
        if mt:
            title = html_lib.unescape(mt.group(1).strip()) or url

        cleaned = re.sub(
            r"(?is)<(script|style|noscript|svg|iframe|template|head)[^>]*>.*?</\1>",
            " ", text,
        )
        cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
        cleaned = html_lib.unescape(cleaned)
        cleaned = re.sub(r"[ \t\r\f\v]+", " ", cleaned)
        cleaned = re.sub(r"\n\s*\n+", "\n\n", cleaned).strip()

        if not cleaned:
            return f"[{url}] (no readable text content found)"

        if len(cleaned) > max_chars:
            cleaned = cleaned[:max_chars] + f"\n[... truncated at {max_chars} chars ...]"
        return f"[Webpage: {title}] ({url})\n{cleaned}"
    except Exception as e:
        print(f"[WebFetch] Error fetching {url}: {e}", flush=True)
        return f"[WebFetch] Error fetching {url}: {e}"


# --------------------------------------------------------------------------
# Bootstrap: download llama.cpp Vulkan + model on first run
# --------------------------------------------------------------------------


def _download_with_resume(url, dest_path, label, icon=None, max_retries=3):
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    part_path = dest_path + ".part"

    for attempt in range(1, max_retries + 1):
        try:
            existing = os.path.getsize(part_path) if os.path.exists(part_path) else 0
            headers = {
                "User-Agent": _USER_AGENT,
                "Accept-Encoding": "identity",
            }
            if existing > 0:
                headers["Range"] = f"bytes={existing}-"
            print(f"[Download] {label}: attempt {attempt}, starting at {existing} bytes", flush=True)
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status == 206:
                    total = existing + int(resp.headers.get("Content-Length") or 0)
                    mode = "ab"
                else:
                    existing = 0
                    total = int(resp.headers.get("Content-Length") or 0)
                    mode = "wb"

                done = existing
                last_notify = 0.0
                with open(part_path, mode) as f:
                    while True:
                        buf = resp.read(1024 * 256)
                        if not buf:
                            break
                        f.write(buf)
                        done += len(buf)
                        now = time.time()
                        if total and now - last_notify > 1.0:
                            pct = int(done * 100 / total)
                            msg = (
                                f"{label}: {pct}% "
                                f"({done // (1024 * 1024)}/{total // (1024 * 1024)} MB)"
                            )
                            print(f"[Download] {msg}", flush=True)
                            if icon is not None:
                                try:
                                    icon.title = msg
                                except Exception:
                                    pass
                            last_notify = now

            size = os.path.getsize(part_path)
            if total and size < total:
                raise IOError(f"incomplete download ({size}/{total} bytes)")
            os.replace(part_path, dest_path)
            print(f"[Download] {label}: complete ({size} bytes)", flush=True)
            if icon is not None:
                try:
                    icon.title = f"{label}: ready"
                except Exception:
                    pass
            return dest_path
        except Exception as e:
            print(f"[Download] {label}: attempt {attempt} failed: {e}", flush=True)
            if attempt >= max_retries:
                raise
            time.sleep(2 * attempt)


def _latest_llama_vulkan_asset():
    api = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
    req = urllib.request.Request(
        api,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            rel = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise RuntimeError(
                "GitHub API rate limit reached while resolving the llama.cpp release. "
                "Wait an hour, or set 'custom_llama_server_exe' in config.json to an "
                "existing llama-server.exe path."
            )
        raise
    for asset in rel.get("assets", []):
        name = asset.get("name", "")
        if "bin-win-vulkan-x64" in name and name.endswith(".zip"):
            return asset["browser_download_url"], name
    raise RuntimeError("No Windows Vulkan asset found in the latest llama.cpp release.")


def _find_llama_server(root):
    if not os.path.isdir(root):
        return None
    for dirpath, _, files in os.walk(root):
        if "llama-server.exe" in files:
            return os.path.join(dirpath, "llama-server.exe")
    return None


def _ensure_llama_server(cfg, icon=None):
    custom = cfg.get("custom_llama_server_exe")
    if custom:
        if os.path.exists(custom):
            print(f"[Bootstrap] Using custom llama-server: {custom}", flush=True)
            return custom
        raise FileNotFoundError(f"custom_llama_server_exe not found: {custom}")

    existing = _find_llama_server(LLAMA_DIR)
    if existing:
        print(f"[Bootstrap] Found existing llama-server: {existing}", flush=True)
        return existing

    if cfg.get("skip_llama_download"):
        raise FileNotFoundError(
            "llama-server.exe not found and 'skip_llama_download' is true. "
            "Set 'custom_llama_server_exe' in config.json."
        )

    url, name = _latest_llama_vulkan_asset()
    zip_path = os.path.join(DOWNLOADS_DIR, name)
    print(f"[Bootstrap] Downloading llama.cpp Vulkan from {url}", flush=True)
    _download_with_resume(url, zip_path, "llama.cpp Vulkan", icon)

    os.makedirs(LLAMA_DIR, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(LLAMA_DIR)
    try:
        os.remove(zip_path)
    except Exception:
        pass

    exe = _find_llama_server(LLAMA_DIR)
    if not exe:
        raise RuntimeError("llama-server.exe missing from the downloaded archive.")
    print(f"[Bootstrap] Extracted llama-server to {exe}", flush=True)
    return exe


def _ensure_model(cfg, icon=None):
    custom = cfg.get("custom_model_path")
    if custom:
        if os.path.exists(custom):
            print(f"[Bootstrap] Using custom model: {custom}", flush=True)
            return custom
        raise FileNotFoundError(f"custom_model_path not found: {custom}")

    dest = os.path.join(MODELS_DIR, cfg["model_file"])
    if os.path.exists(dest) and os.path.getsize(dest) > 100 * 1024 * 1024:
        print(f"[Bootstrap] Found existing model: {dest}", flush=True)
        return dest

    if cfg.get("skip_model_download"):
        raise FileNotFoundError(
            "Model file not found and 'skip_model_download' is true. "
            "Set 'custom_model_path' in config.json."
        )

    url = (
        f"https://huggingface.co/{cfg['model_repo']}"
        f"/resolve/main/{cfg['model_file']}"
    )
    print(f"[Bootstrap] Downloading model from {url}", flush=True)
    _download_with_resume(url, dest, cfg["model_file"], icon)
    return dest


def _check_vulkan_runtime():
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    dll = os.path.join(sysroot, "System32", "vulkan-1.dll")
    if os.path.exists(dll):
        print(f"[Vulkan] Runtime present: {dll}", flush=True)
        return

    print("[Vulkan] vulkan-1.dll missing - attempting winget install", flush=True)
    try:
        result = subprocess.run(
            [
                "winget", "install", "--id", "KhronosGroup.VulkanRT", "-e",
                "--silent", "--accept-package-agreements", "--accept-source-agreements",
            ],
            capture_output=True, text=True, timeout=300,
            creationflags=_NO_WINDOW_FLAGS,
        )
        print(f"[Vulkan] winget exit={result.returncode}", flush=True)
        if result.stdout:
            print(result.stdout, flush=True)
        if result.stderr:
            print(result.stderr, flush=True)
    except Exception as e:
        print(f"[Vulkan] winget unavailable: {e}", flush=True)

    if not os.path.exists(dll):
        raise RuntimeError(
            "Vulkan runtime is not installed. Install your GPU driver "
            "(NVIDIA, AMD, or Intel - each ships the Vulkan runtime) or run "
            "'winget install KhronosGroup.VulkanRT', then relaunch."
        )


def _list_vulkan_devices(server_exe):
    try:
        result = subprocess.run(
            [server_exe, "--list-devices"],
            capture_output=True, text=True, timeout=30,
            creationflags=_NO_WINDOW_FLAGS,
        )
        out = (result.stdout or "") + (result.stderr or "")
        print("[Vulkan] Available devices:\n" + out, flush=True)
        return out
    except Exception as e:
        print(f"[Vulkan] --list-devices failed: {e}", flush=True)
        return ""


# --------------------------------------------------------------------------
# llama-server command construction
# --------------------------------------------------------------------------


def build_llama_cmd(cfg, model_path, server_exe):
    cmd = [
        server_exe,
        "-m", model_path,
        "--alias", "claude-sonnet-4-5-20250929",
        "--port", str(int(cfg["llama_port"])),
        "--jinja",
        "--reasoning-budget", "0",
        "-ngl", "999",
        "-c", str(int(cfg["context_size"])),
        "-fa", "on",
        "-ctk", "q4_0",
        "-ctv", "q4_0",
        "-np", "1",
        "-b", "1024",
        "-ub", "512",
        "--repeat-penalty", "1.12",
        "--repeat-last-n", "128",
    ]
    threads = int(cfg.get("threads") or 0)
    if threads > 0:
        cmd += ["-t", str(threads)]
    device = cfg.get("device")
    if device:
        cmd += ["-dev", str(device), "-sm", "none", "-mg", "0"]
    extra = cfg.get("extra_llama_args") or []
    if isinstance(extra, list):
        cmd += [str(x) for x in extra]
    return cmd


# --------------------------------------------------------------------------
# Anthropic proxy (unchanged logic, configurable ports)
# --------------------------------------------------------------------------


def compress_schema(schema):
    if isinstance(schema, dict):
        return {k: compress_schema(v) for k, v in schema.items() if k != "description"}
    if isinstance(schema, list):
        return [compress_schema(item) for item in schema]
    return schema


def describe_image_with_sidecar(b64_data, user_prompt="", media_type="image/png"):
    print("[VisionSidecar] Analyzing image with Qwen2.5-VL-3B (port 8082)...", flush=True)
    prompt = (
        "Provide a thorough, accurate description of this image, including all visible text (OCR), UI elements, "
        "charts/numbers, code, objects, colors, and layout details so another AI assistant can answer the user's request."
    )
    if user_prompt:
        prompt += f" User's question about this image: {user_prompt[:300]}"

    payload = {
        "model": "qwen2.5-vl-3b",
        "max_tokens": 600,
        "temperature": 0.1,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64_data}"}},
                ],
            }
        ],
    }
    try:
        req = urllib.request.Request(
            VISION_SIDECAR_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            res = json.loads(resp.read().decode("utf-8"))
        desc = res.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if desc:
            print(f"[VisionSidecar] Image described ({len(desc)} chars)", flush=True)
            return (
                f"\n[Visual Context - The user attached an image. Here is what is visible in the image]:\n{desc}\n"
                f"[Answer the user's question directly based on this visual content. Do NOT say you cannot view images.]\n"
            )
    except Exception as e:
        print(f"[VisionSidecar] Error calling port 8082: {e}", flush=True)
    return "\n[User attached an image, but the vision sidecar on port 8082 did not respond.]\n"


def extract_attachment_text(block):
    src_obj = block.get("source", {})
    media_type = src_obj.get("media_type", "")
    data_str = src_obj.get("data", "")
    title = block.get("title") or block.get("name") or "attached_file"
    if src_obj.get("type") == "text" and data_str:
        return f"\n[Attached File: {title}]\n{data_str}\n"
    if src_obj.get("type") == "base64" and data_str:
        try:
            raw_bytes = base64.b64decode(data_str)
            if "pdf" in media_type.lower():
                try:
                    import pypdf, io
                    reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
                    pdf_txt = "\n".join(page.extract_text() or "" for page in reader.pages)
                    return f"\n[Attached PDF: {title}]\n{pdf_txt[:30000]}\n"
                except Exception:
                    return f"\n[Attached PDF: {title} - install pypdf to extract PDF text]\n"
            decoded = raw_bytes.decode("utf-8", errors="replace")
            return f"\n[Attached File: {title}]\n{decoded[:30000]}\n"
        except Exception as e:
            return f"\n[Attached File: {title} (could not decode: {e})]\n"
    return ""


def strip_cowork_bloat(text):
    if not isinstance(text, str):
        return text
    for tag in ("system-reminder", "available-deferred-tools", "environment", "local-command-caveat", "context"):
        text = re.sub(rf"<{tag}>.*?</{tag}>", "", text, flags=re.DOTALL)
    return text.strip()


def render_visual_blocks(full_text):
    blocks = re.findall(r"```(html|svg|mermaid)\s*\n([\s\S]*?)```", full_text, flags=re.IGNORECASE)
    if not blocks:
        return
    lang, code = blocks[-1]
    lang = lang.lower().strip()
    code = code.strip()
    if lang == "html":
        html_doc = (
            code if "<html" in code.lower()
            else f"<!DOCTYPE html><html><head><meta charset='utf-8'><style>body{{margin:0;background:#0f172a;color:#f8fafc;display:flex;justify-content:center;align-items:center;min-height:100vh;font-family:system-ui}}</style></head><body>{code}</body></html>"
        )
    elif lang == "svg":
        html_doc = f"<!DOCTYPE html><html><head><meta charset='utf-8'><style>body{{margin:0;background:#0f172a;display:flex;justify-content:center;align-items:center;min-height:100vh}}</style></head><body>{code}</body></html>"
    else:
        html_doc = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<script type=\"module\">import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';"
            "mermaid.initialize({startOnLoad:true,theme:'dark'});</script>"
            "<style>body{margin:20px;background:#0f172a;color:#f8fafc;display:flex;justify-content:center;align-items:center;min-height:90vh;font-family:system-ui}</style>"
            f"</head><body><pre class=\"mermaid\">{code}</pre></body></html>"
        )

    out_path = os.path.join(tempfile.gettempdir(), "claude_visual_render.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    print(f"[Visual] Rendered {lang.upper()} graphic -> {out_path}", flush=True)
    webbrowser.open(f"file:///{out_path.replace(os.sep, '/')}")


def apply_visual_instructions(data, user_text):
    visual_keywords = (
        "graph", "chart", "plot", "diagram", "flowchart", "draw", "visualize",
        "visualization", "svg", "mermaid", "mindmap", "timeline", "architecture", "infographic"
    )
    if any(k in user_text.lower() for k in visual_keywords):
        vis_rules = (
            "\n\n[Visual Rendering Rules]: Output a single polished, production-grade visual block:\n"
            "- For data charts, line graphs, bar charts, pie/radar charts, or plots: output a complete <!DOCTYPE html> block inside ```html using Chart.js (https://cdn.jsdelivr.net). Style it like a modern dark-mode dashboard (#0b0f19 background, #111827 card with 16px rounded corners, subtle border #1f2937, title + subtitle, smooth curves/gradients, proper axis labels, legend, and interactive hover tooltips).\n"
            "- For flowcharts, sequence diagrams, state machines, or mindmaps: output a clean, well-labeled ```mermaid block with clear node names and subgraphs.\n"
            "- For custom vector art or diagrams: output a detailed, multi-layered ```svg block with viewBox, gradients, crisp typography, and balanced padding."
        )
        sys_ = data.get("system")
        if isinstance(sys_, list):
            sys_.append({"type": "text", "text": vis_rules})
        else:
            data["system"] = str(sys_ or "") + vis_rules


TOOL_INTENT_PATTERN = re.compile(
    r"(?i)\b("
    r"file|files|folder|directory|path|read|write|edit|save|delete|rename|move|list|ls|dir|"
    r"run|execute|command|terminal|powershell|bash|cmd|script|code|git|repo|install|"
    r"search|lookup|look up|google|web|internet|news|latest|today|current|weather|price|stock|"
    r"browse|browser|url|http|website|page|open|click|screenshot|"
    r"calendar|event|schedule|email|gmail|drive|doc|docs|sheet|notion|slack|discord|"
    r"memory|remember|note|notes|database|sql|query|mcp|plugin|tool|fetch|api"
    r")\b"
)


def message_needs_tools(messages):
    if not messages:
        return False
    last_msg = messages[-1]
    content = last_msg.get("content", "")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return True
        user_text = " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    else:
        user_text = str(content).strip()
    if user_text.startswith("/"):
        return True
    return bool(TOOL_INTENT_PATTERN.search(user_text))


def build_clean_system_prompt(raw_system):
    custom_parts = []
    if isinstance(raw_system, str):
        blocks = [raw_system]
    elif isinstance(raw_system, list):
        blocks = [b.get("text", "") for b in raw_system if isinstance(b, dict) and b.get("type") == "text"]
    else:
        blocks = []

    for text in blocks:
        cleaned = re.sub(r"<system-reminder>[\s\S]*?</system-reminder>", "", text).strip()
        if not cleaned:
            continue
        if not cleaned.startswith("You are Claude, created by Anthropic") and len(cleaned) < 4000:
            custom_parts.append(cleaned)

    base_directive = (
        "You are Claude, a helpful AI assistant, do not ever use emoji's under any circumstances. "
        "You are professional. When giving math equations use LaTeX ($...$ for inline with NO spaces touching the $ signs, e.g. $f(x)=x^2$, and $$...$$ on separate lines for display math). "
        "When told to create a file or build something, always use the file-creation tool (such as Write) to create the actual file on disk rather than outputting the file contents into the chat. Only call other tools or plugins when strictly required to fulfill the user's request.\n\n"
        "IMPORTANT - LIVE WEB ACCESS: You have real internet access through the proxy running behind you. "
        "When the user asks you to search, look something up, google, or find something online (or includes a URL), "
        "the proxy automatically performs a live search or fetches the page and injects the content into your context "
        "as messages starting with '[PROXY INJECTION - LIVE WEB SEARCH]' or '[PROXY INJECTION - LIVE WEBPAGE CONTENT]'. "
        "Those are REAL, CURRENT results fetched just now - treat them as authoritative live data. "
        "You DO have internet access and real-time information. Never claim your knowledge is limited to a training cutoff "
        "or that you cannot access the internet. If the injected results are unhelpful or empty, say so honestly and suggest "
        "an alternative phrasing, but do not deny having web access."
    )
    if custom_parts:
        return base_directive + "\n\nAdditional Instructions:\n" + "\n\n".join(custom_parts)
    return base_directive


def normalize_latex_formatting(text):
    if not text:
        return text
    text = re.sub(r'\\\[([\s\S]*?)\\\]', lambda m: f"\n$$\n{m.group(1).strip()}\n$$\n", text)
    text = re.sub(r'\\\(([\s\S]*?)\\\)', lambda m: f"${m.group(1).strip()}$", text)
    text = re.sub(r'(?<!\$)\$\s+([^$\n]+?)\s+\$(?!\$)', r'$\1$', text)
    text = re.sub(r'(?<!\$)\$\s+([^$\n]+?)\$(?!\$)', r'$\1$', text)
    text = re.sub(r'(?<!\$)\$([^$\n]+?)\s+\$(?!\$)', r'$\1$', text)
    return text


def stream_and_fix_latex(resp, wfile):
    line_buf = ""
    raw_buf = b""
    last_delta_evt = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": ""}}

    def emit_text(txt, template_evt):
        if not txt:
            return
        evt = json.loads(json.dumps(template_evt))
        evt["delta"]["text"] = txt
        out = f"event: content_block_delta\ndata: {json.dumps(evt)}\n\n".encode("utf-8")
        wfile.write(out)
        wfile.flush()

    while True:
        chunk = resp.read(512)
        if not chunk:
            break
        raw_buf += chunk.replace(b"\r\n", b"\n")
        while b"\n\n" in raw_buf:
            frame, raw_buf = raw_buf.split(b"\n\n", 1)
            if not frame.strip():
                continue
            frame_str = frame.decode("utf-8", errors="replace")
            data_line = None
            for fline in frame_str.splitlines():
                if fline.startswith("data: "):
                    data_line = fline[6:]
                    break
            if data_line and data_line.strip() != "[DONE]":
                try:
                    evt = json.loads(data_line)
                    etype = evt.get("type")
                    if etype == "content_block_delta" and evt.get("delta", {}).get("type") == "text_delta":
                        last_delta_evt = evt
                        line_buf += evt["delta"].get("text", "")
                        if "\n" in line_buf:
                            parts = line_buf.split("\n")
                            complete = "\n".join(parts[:-1]) + "\n"
                            line_buf = parts[-1]
                            emit_text(normalize_latex_formatting(complete), last_delta_evt)
                        continue
                    elif etype in ("content_block_start", "content_block_stop", "message_delta", "message_stop"):
                        if line_buf:
                            emit_text(normalize_latex_formatting(line_buf), last_delta_evt)
                            line_buf = ""
                except Exception:
                    pass
            wfile.write(frame + b"\n\n")
            wfile.flush()

    if line_buf:
        emit_text(normalize_latex_formatting(line_buf), last_delta_evt)
    if raw_buf:
        wfile.write(raw_buf)
        wfile.flush()


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[Proxy] {self.address_string()} - {format % args}", flush=True)

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        self._forward_request("GET")

    def do_POST(self):
        self._forward_request("POST")

    def _forward_request(self, method):
        parsed_path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else None

        if method == "POST" and parsed_path == "/v1/messages/count_tokens":
            est_toks = max(64, len(body) // 4) if body else 128
            encoded = json.dumps({"input_tokens": est_toks}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return

        if method == "POST" and parsed_path == "/v1/messages" and body:
            print(f"[Incoming] /v1/messages (body={len(body)} bytes)", flush=True)
            data = json.loads(body)
            messages = data.get("messages", [])
            last_msg = str(messages[-1].get("content", "")).lower() if messages else ""
            if len(last_msg) < 1200 and (
                ("compact" in last_msg and "conversation" in last_msg)
                or "title for this conversation" in last_msg
                or "generate a title" in last_msg
            ):
                fake_resp = {
                    "id": "msg_title",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Local Session"}],
                    "model": data.get("model", "claude-sonnet-4-5-20250929"),
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                }
                encoded = json.dumps(fake_resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
                return

            raw_system = data.get("system")
            raw_incoming_tools = list(data.get("tools") or [])

            for key in ("thinking", "context_management", "betas", "metadata"):
                data.pop(key, None)

            has_vision = False  # universal build does not bundle a vision projection
            for msg in data.get("messages", []):
                if isinstance(msg.get("content"), str):
                    msg["content"] = strip_cowork_bloat(msg["content"]) or "..."
                elif isinstance(msg.get("content"), list):
                    cleaned = []
                    for block in msg["content"]:
                        btype = block.get("type")
                        if btype == "text":
                            txt = strip_cowork_bloat(block.get("text", ""))
                            if txt:
                                cleaned.append({"type": "text", "text": txt})
                        elif btype == "document":
                            doc_txt = extract_attachment_text(block)
                            if doc_txt:
                                cleaned.append({"type": "text", "text": doc_txt})
                        elif btype == "image":
                            if has_vision:
                                cleaned.append(block)
                            else:
                                img_src = block.get("source", {})
                                b64_img = img_src.get("data", "")
                                if b64_img:
                                    u_hint = " ".join(
                                        b.get("text", "") for b in msg.get("content", [])
                                        if isinstance(b, dict) and b.get("type") == "text"
                                    )
                                    desc_txt = describe_image_with_sidecar(
                                        b64_img, u_hint, img_src.get("media_type", "image/png")
                                    )
                                    cleaned.append({"type": "text", "text": desc_txt})
                                else:
                                    cleaned.append({"type": "text", "text": "[User attached an unsupported image URL.]"})
                        elif btype in ("tool_use", "tool_result"):
                            cleaned.append(block)
                    msg["content"] = cleaned if cleaned else [{"type": "text", "text": "..."}]

            latest_u = ""
            for m in reversed(data.get("messages", [])):
                if m.get("role") == "user":
                    c = m.get("content", "")
                    latest_u = (
                        c if isinstance(c, str)
                        else " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
                    )
                    latest_u = latest_u.strip()
                    if latest_u:
                        break

            injected_parts = []

            if WANTS_WEB_PATTERN.search(latest_u):
                try:
                    from datetime import datetime
                    cur_year = str(datetime.now().year)
                    search_query = _build_search_query(latest_u, data["messages"])
                    search_query = re.sub(r"\b(2024|2025)\b", cur_year, search_query[:150])
                    if search_query:
                        search_res = run_host_web_search(search_query, max_results=6)
                        injected_parts.append(
                            "[PROXY INJECTION - LIVE WEB SEARCH]\n"
                            f"The proxy just ran a live web search for: {search_query}\n\n"
                            f"Results:\n{search_res}\n\n"
                            "You DID just search the web. Use these results to answer. "
                            "Do NOT claim you lack internet access or that your knowledge is limited to a training cutoff."
                        )
                except Exception as e:
                    print(f"[WebSearch] Skipped: {e}", flush=True)

            if latest_u:
                explicit_urls = []
                for raw_url in re.findall(r'https?://[^\s<>"\']+', latest_u):
                    cleaned_url = raw_url.rstrip(".,;:!?)]}\"'")
                    if cleaned_url and cleaned_url not in explicit_urls:
                        explicit_urls.append(cleaned_url)
                explicit_urls = [
                    u for u in explicit_urls[:2]
                    if not re.search(r"\.(png|jpe?g|gif|webp|svg|mp4|mp3|wav|pdf|zip)(\?|$)", u, re.I)
                ]
                if explicit_urls:
                    fetched_pages = [fetch_webpage(u) for u in explicit_urls]
                    injected_parts.append(
                        "[PROXY INJECTION - LIVE WEBPAGE CONTENT]\n"
                        "The proxy fetched these webpages live just now:\n\n"
                        + "\n\n---\n\n".join(fetched_pages)
                        + "\n\nYou DO have internet access. Use this content to answer."
                    )
                    print(f"[WebFetch] Fetched {len(explicit_urls)} page(s)", flush=True)

            if injected_parts:
                data["messages"].append({
                    "role": "user",
                    "content": "\n\n=====\n\n".join(injected_parts) + "\n\nAnswer the user's question using the above live content.",
                })

            clean_sys = build_clean_system_prompt(raw_system)
            data["system"] = [{"type": "text", "text": clean_sys}]
            apply_visual_instructions(data, latest_u)
            data["thinking"] = {"type": "disabled"}

            data["messages"] = [m for m in data.get("messages", []) if m.get("role") in ("user", "assistant")]

            if message_needs_tools(data.get("messages", [])):
                all_user_text = " ".join(
                    str(m.get("content", "")).lower() for m in data.get("messages", []) if m.get("role") == "user"
                )
                core_names = {
                    "skill", "agent", "task", "write", "edit", "multiedit", "read",
                    "bash", "glob", "grep", "ls", "todowrite", "todoread", "webfetch",
                }
                compacted_tools = []
                for tool in raw_incoming_tools:
                    if not isinstance(tool, dict):
                        continue
                    t_name = str(tool.get("name", ""))
                    t_low = t_name.lower()
                    if t_low == "toolsearch":
                        continue
                    parts = t_low.split("__")
                    server_prefix = parts[1] if t_low.startswith("mcp__") and len(parts) > 2 else ""
                    is_core = t_low in core_names or any(
                        c in t_low for c in ("write", "edit", "read", "bash", "skill", "file", "dir")
                    )
                    is_mentioned_mcp = bool(server_prefix and server_prefix in all_user_text) or any(
                        p in all_user_text for p in re.split(r"[_\-:]+", t_low) if len(p) > 3
                    )
                    if not (is_core or is_mentioned_mcp):
                        continue
                    raw_desc = tool.get("description") or ""
                    short_desc = next(iter(raw_desc.split(".")))[:80].strip()
                    compacted_tools.append({
                        "name": t_name,
                        "description": short_desc,
                        "input_schema": compress_schema(tool.get("input_schema", {})),
                    })
                data["tools"] = compacted_tools[:24]
                print(
                    f"[Router] Tools ENABLED ({len(data['tools'])}/{len(raw_incoming_tools)})",
                    flush=True,
                )
            else:
                data.pop("tools", None)
                data.pop("tool_choice", None)
                print("[Router] Fast Chat Mode (0 tools)", flush=True)

            if isinstance(data.get("tools"), list):
                data["tools"] = [t for t in data["tools"] if t.get("name") != "web_search"]
                if not data["tools"]:
                    data.pop("tools", None)
                    data.pop("tool_choice", None)

            sys_len = len(json.dumps(data.get("system", [])))
            tools_len = len(json.dumps(data.get("tools", [])))
            msgs_len = len(json.dumps(data.get("messages", [])))
            est_tokens = (sys_len + tools_len + msgs_len) // 4
            print(
                f"[Payload] ~{est_tokens} est tokens "
                f"(system={sys_len}c, tools={tools_len}c, msgs={msgs_len}c)",
                flush=True,
            )

            body = json.dumps(data).encode("utf-8")

        req = urllib.request.Request(
            f"{TARGET_URL}{self.path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method=method,
        )

        try:
            with urllib.request.urlopen(req) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in ("transfer-encoding", "content-length"):
                        self.send_header(k, v)
                self.end_headers()
                collected_text = []
                if "text/event-stream" in resp.headers.get("Content-Type", ""):
                    stream_and_fix_latex(resp, self.wfile)
                else:
                    resp_bytes = resp.read()
                    self.wfile.write(resp_bytes)
                    self.wfile.flush()
                    try:
                        result = json.loads(resp_bytes.decode("utf-8", errors="replace"))
                        for block in result.get("content", []):
                            if isinstance(block, dict) and block.get("type") == "text":
                                collected_text.append(block.get("text", ""))
                    except Exception:
                        pass
                if collected_text:
                    render_visual_blocks("".join(collected_text))
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass
        except urllib.error.HTTPError as e:
            err_body = e.read()
            print(f"[llama-server {e.code} error]: {err_body.decode('utf-8', errors='replace')}", flush=True)
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(err_body)


# --------------------------------------------------------------------------
# Server lifecycle
# --------------------------------------------------------------------------


def trim_process_dram(pid):
    try:
        import ctypes
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_QUERY_INFORMATION = 0x0400
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION, False, pid
        )
        if h:
            ctypes.windll.psapi.EmptyWorkingSet(h)
            ctypes.windll.kernel32.CloseHandle(h)
            print(f"[Memory] Trimmed idle startup pages for llama-server (PID {pid})", flush=True)
    except Exception:
        pass


def wait_for_llama_server(server_proc, port=8081, timeout=180):
    print(f"[Launcher] Waiting for llama-server on port {port}...", flush=True)
    deadline = time.time() + timeout
    health = f"http://127.0.0.1:{port}/health"
    while time.time() < deadline:
        if server_proc.poll() is not None:
            raise RuntimeError("llama-server.exe exited unexpectedly during startup.")
        try:
            with urllib.request.urlopen(health, timeout=1) as r:
                if r.status == 200:
                    print("[Launcher] llama-server is ready.", flush=True)
                    trim_process_dram(server_proc.pid)
                    return
        except Exception:
            pass
        time.sleep(0.5)
    raise TimeoutError("Timed out waiting for llama-server to become ready.")


def start_daemon_https(port, cert_file, key_file, label):
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), ProxyHandler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"[Proxy] {label}", flush=True)
        return server
    except Exception as e:
        print(f"[Proxy] Port {port} skipped: {e}", flush=True)
        return None


def _port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


class Application:
    def __init__(self, cfg, stop_event):
        self.cfg = cfg
        self.stop_event = stop_event
        self.server_proc = None
        self.httpd = None
        self.https_servers = []
        self.fatal_error = None
        self.llama_exe = None
        self.model_path = None

    def start(self, icon=None):
        global PROXY_PORT, TARGET_URL
        try:
            _check_vulkan_runtime()

            proxy_port = int(self.cfg["proxy_port"])
            llama_port = int(self.cfg["llama_port"])
            if _port_in_use(proxy_port):
                raise RuntimeError(
                    f"Port {proxy_port} is already in use. Another proxy may be running."
                )
            if _port_in_use(llama_port):
                raise RuntimeError(
                    f"Port {llama_port} is already in use. Another llama-server may be running."
                )

            self.llama_exe = _ensure_llama_server(self.cfg, icon)
            self.model_path = _ensure_model(self.cfg, icon)
            _list_vulkan_devices(self.llama_exe)

            PROXY_PORT = proxy_port
            TARGET_URL = f"http://127.0.0.1:{llama_port}"

            cmd = build_llama_cmd(self.cfg, self.model_path, self.llama_exe)
            print(f"[Launcher] {' '.join(cmd)}", flush=True)

            env = {k: v for k, v in os.environ.items() if not k.startswith("LLAMA_ARG_")}
            popen_kwargs = {"env": env}
            if _LOG_FILE is not None:
                popen_kwargs["stdout"] = _LOG_FILE
                popen_kwargs["stderr"] = subprocess.STDOUT
                popen_kwargs["creationflags"] = _NO_WINDOW_FLAGS

            self.server_proc = subprocess.Popen(cmd, **popen_kwargs)
            wait_for_llama_server(self.server_proc, port=llama_port)

            self.httpd = ThreadingHTTPServer(("127.0.0.1", proxy_port), ProxyHandler)
            threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
            print(
                f"[Proxy] LISTENING for Claude Desktop on http://127.0.0.1:{proxy_port}",
                flush=True,
            )

            cert_file = os.path.join(CERTS_DIR, "server.crt")
            key_file = os.path.join(CERTS_DIR, "server.key")
            if os.path.exists(cert_file) and os.path.exists(key_file):
                s8443 = start_daemon_https(8443, cert_file, key_file, "HTTPS on :8443")
                if s8443:
                    self.https_servers.append(s8443)
                s443 = start_daemon_https(443, cert_file, key_file, "HTTPS on :443")
                if s443:
                    self.https_servers.append(s443)
            else:
                print(
                    f"[Proxy] No TLS certs in {CERTS_DIR}; HTTPS listeners disabled.",
                    flush=True,
                )
            return True
        except Exception as e:
            self.fatal_error = e
            traceback.print_exc()
            return False

    def shutdown(self):
        for srv in self.https_servers:
            try:
                srv.shutdown()
            except Exception:
                pass
            try:
                srv.server_close()
            except Exception:
                pass
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
            except Exception:
                pass
            try:
                self.httpd.server_close()
            except Exception:
                pass
        if self.server_proc is not None and self.server_proc.poll() is None:
            print("[Launcher] Stopping llama-server...", flush=True)
            try:
                self.server_proc.terminate()
                self.server_proc.wait(timeout=5)
            except Exception:
                try:
                    self.server_proc.kill()
                except Exception:
                    pass
        if _LOG_FILE is not None:
            try:
                _LOG_FILE.flush()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Logging, tray, entry point
# --------------------------------------------------------------------------


def _setup_logging():
    global _LOG_FILE
    if not _IS_FROZEN:
        return None
    try:
        os.makedirs(APPDATA_DIR, exist_ok=True)
        log_path = os.path.join(APPDATA_DIR, "proxy.log")
        _LOG_FILE = open(log_path, "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = _LOG_FILE
        if sys.stderr is None:
            sys.stderr = _LOG_FILE
        return _LOG_FILE
    except Exception:
        return None


def _show_error(msg):
    print(f"[ERROR] {msg}", flush=True)
    if _IS_WINDOWS and _IS_FROZEN:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, str(msg), "Claude Local Proxy", 0x10)
        except Exception:
            pass


def _build_icon_image():
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, size - 3, size - 3], fill=(99, 102, 241, 255), outline=(255, 255, 255, 255), width=2)
    d.arc([16, 12, size - 16, size - 12], start=45, end=315, fill=(255, 255, 255, 255), width=5)
    return img


def _build_tray_icon(tooltip="Claude Local Proxy"):
    icon_img = _build_icon_image()
    log_path = os.path.join(APPDATA_DIR, "proxy.log")

    def on_quit(icon, item):
        print("[Tray] Quit requested", flush=True)
        icon.stop()

    def on_open_log(icon, item):
        try:
            if os.path.exists(log_path):
                os.startfile(log_path)
            else:
                os.startfile(APPDATA_DIR)
        except Exception:
            pass

    def on_open_folder(icon, item):
        try:
            os.startfile(APPDATA_DIR)
        except Exception:
            pass

    def on_open_models(icon, item):
        try:
            os.makedirs(MODELS_DIR, exist_ok=True)
            os.startfile(MODELS_DIR)
        except Exception:
            pass

    def on_open_config(icon, item):
        try:
            if os.path.exists(CONFIG_PATH):
                os.startfile(CONFIG_PATH)
            else:
                os.startfile(APPDATA_DIR)
        except Exception:
            pass

    menu = pystray.Menu(
        pystray.MenuItem("Claude Local Proxy", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open Log", on_open_log, default=True),
        pystray.MenuItem("Open Log Folder", on_open_folder),
        pystray.MenuItem("Open Models Folder", on_open_models),
        pystray.MenuItem("Open Config", on_open_config),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )
    return pystray.Icon("claude-local-proxy-universal", icon_img, tooltip, menu)


def main():
    _setup_logging()

    if _IS_FROZEN and not HAS_TRAY:
        _show_error(
            "Tray icon support is missing from this build. "
            "Rebuild with pystray and Pillow installed."
        )
        return

    cfg = _load_or_create_config()
    stop_event = threading.Event()
    app = Application(cfg, stop_event)

    if not HAS_TRAY:
        if not app.start(None):
            _show_error(f"Startup failed: {app.fatal_error}")
            return
        try:
            while not stop_event.wait(1.0):
                pass
        except KeyboardInterrupt:
            stop_event.set()
        app.shutdown()
        return

    icon = _build_tray_icon("Claude Local Proxy (starting)")

    def worker():
        ok = app.start(icon)
        if ok:
            try:
                icon.title = f"Claude Local Proxy (ready on :{cfg['proxy_port']})"
            except Exception:
                pass
        else:
            try:
                icon.title = "Claude Local Proxy (failed - see log)"
            except Exception:
                pass
            try:
                icon.stop()
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True).start()
    icon.run()

    stop_event.set()
    app.shutdown()
    if app.fatal_error:
        _show_error(f"Startup failed: {app.fatal_error}")


if __name__ == "__main__":
    main()