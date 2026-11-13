"""我的爬虫 · 本地网页服务。运行：python app.py"""
from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import crawler

ROOT = Path(__file__).parent
DOWNLOADS = ROOT / "downloads"
DOWNLOADS.mkdir(exist_ok=True)
HOST, PORT = "127.0.0.1", 8447
TYPE_DIR = {"image": "图片", "video": "视频", "audio": "音频", "file": "文件"}

app = FastAPI(title="我的爬虫")


# ───────────── 抓取 ─────────────
class ScanReq(BaseModel):
    url: str
    types: list[str] = Field(default_factory=lambda: list(crawler.ALL_TYPES))
    depth: int = 0
    max_pages: int = 20
    same_site: bool = True
    robots: bool = True
    render: bool = False
    delay: float = 0.5
    selector: str = ""


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise HTTPException(400, "请输入网址")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    if not urlparse(url).netloc:
        raise HTTPException(400, "网址格式不对")
    return url


@app.post("/api/scan")
async def scan(req: ScanReq):
    params = req.model_dump()
    params["url"] = normalize_url(req.url)
    params["types"] = [t for t in req.types if t in crawler.ALL_TYPES] or list(crawler.ALL_TYPES)
    params["depth"] = max(0, min(req.depth, 3))
    params["max_pages"] = max(1, min(req.max_pages, crawler.MAX_PAGES_LIMIT))
    params["delay"] = max(0.2, min(req.delay, 10))
    job = crawler.new_job(params)
    asyncio.create_task(crawler.crawl(job))
    return {"id": job.id}


def get_job(job_id: str) -> crawler.Job:
    job = crawler.JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在（服务重启后任务会清空）")
    return job


@app.get("/api/jobs/{job_id}")
async def job_state(job_id: str, offset: int = 0):
    job = get_job(job_id)
    out = job.summary()
    out["items"] = job.items[offset:]
    return out


@app.post("/api/jobs/{job_id}/stop")
async def job_stop(job_id: str):
    get_job(job_id).stop = True
    return {"ok": True}


# ───────────── 缩略图代理（带上来源页，绕开防盗链造成的裂图） ─────────────
_proxy_client: httpx.AsyncClient | None = None


@app.get("/api/proxy")
async def proxy(url: str, ref: str = ""):
    global _proxy_client
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400)
    if _proxy_client is None:
        _proxy_client = httpx.AsyncClient(headers={"User-Agent": crawler.UA}, follow_redirects=True, timeout=20)
    try:
        r = await _proxy_client.get(url, headers={"Referer": ref} if ref else {})
    except Exception:
        raise HTTPException(502)
    ct = r.headers.get("content-type", "application/octet-stream")
    if r.status_code >= 400 or not ct.startswith(("image/", "audio/", "video/")):
        raise HTTPException(404)
    return Response(r.content, media_type=ct, headers={"Cache-Control": "max-age=3600"})


# ───────────── 下载 ─────────────
class DlItem(BaseModel):
    url: str
    type: str = "file"
    page: str = ""
    kind: str = ""


class DlReq(BaseModel):
    items: list[DlItem]


TASKS: dict[str, dict] = {}
_sem = asyncio.Semaphore(3)


def find_ytdlp() -> list[str] | None:
    """优先用当前 Python 里装的 yt-dlp（pip install -U yt-dlp 升级的就是它），其次才找 PATH 里的。"""
    import importlib.util
    import sys
    if importlib.util.find_spec("yt_dlp"):
        return [sys.executable, "-m", "yt_dlp"]
    exe = shutil.which("yt-dlp")
    return [exe] if exe else None


YTDLP = find_ytdlp()
NODE = shutil.which("node")


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    return name[:120] or "未命名"


def unique_path(folder: Path, name: str) -> Path:
    path = folder / name
    stem, suffix = path.stem, path.suffix
    i = 2
    while path.exists():
        path = folder / f"{stem} ({i}){suffix}"
        i += 1
    return path


def dest_folder(item: DlItem) -> Path:
    host = safe_name(urlparse(item.page or item.url).netloc or "其他")
    folder = DOWNLOADS / host / TYPE_DIR.get(item.type, "文件")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def needs_ytdlp(item: DlItem) -> bool:
    if item.kind in ("embed", "page") or crawler.ext_of(item.url) in ("m3u8", "mpd"):
        return True
    # 视频网站的播放页 / 嵌入页不是文件本身，要靠 yt-dlp 解析出真正的视频
    host = urlparse(item.url).netloc
    return item.type in ("video", "audio") and any(h in host for h in crawler.EMBED_HOSTS)


def run_ytdlp(task: dict, item: DlItem):
    """流媒体（m3u8）和视频网站页面交给 yt-dlp 处理。"""
    if not YTDLP:
        task.update(status="error", error="没装 yt-dlp，无法下载流媒体（pip install yt-dlp）")
        return
    folder = dest_folder(item)
    cmd = [*YTDLP, "--no-playlist", "--newline", "--no-colors", "--windows-filenames",
           "-P", str(folder), "-o", "%(title).80s [%(id)s].%(ext)s", "--print", "after_move:filepath"]
    cmd += ["-S", "ext:mp4:m4a"]  # 优先 mp4，播放器兼容性最好
    if NODE:
        cmd += ["--js-runtimes", f"node:{NODE}"]  # YouTube 需要一个 JS 运行时才能拿全清晰度
    if item.type == "audio":
        cmd += ["-x"]
    if item.page and item.page != item.url:
        cmd += ["--referer", item.page]
    cmd.append(item.url)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        task["proc"] = proc
        last = ""
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            last = line
            m = re.search(r"\[download\]\s+([\d.]+)%", line)
            if m:
                task["percent"] = float(m.group(1))
            elif os.path.isabs(line) and os.path.exists(line):
                task["path"] = line
                task["name"] = os.path.basename(line)
                task["size"] = os.path.getsize(line)
        proc.wait()
        if task["status"] == "canceled":
            return
        if proc.returncode == 0:
            task.update(status="done", percent=100)
        else:
            task.update(status="error", error=last[-200:] or "yt-dlp 失败")
    except Exception as e:
        task.update(status="error", error=str(e)[:200])
    finally:
        task.pop("proc", None)


class NeedYtdlp(Exception):
    pass


async def run_download(task: dict, item: DlItem):
    async with _sem:
        if task["status"] == "canceled":
            return
        task["status"] = "running"
        if needs_ytdlp(item):
            await asyncio.to_thread(run_ytdlp, task, item)
            return
        path = None
        try:
            headers = {"User-Agent": crawler.UA}
            if item.page:
                headers["Referer"] = item.page
            async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=httpx.Timeout(30, read=60)) as c:
                async with c.stream("GET", item.url) as r:
                    r.raise_for_status()
                    ct = r.headers.get("content-type", "").split(";")[0].strip()
                    if "html" in ct and item.type in ("video", "audio") and YTDLP:
                        raise NeedYtdlp  # 地址指向的是播放页，换 yt-dlp 解析
                    elif "html" in ct and item.type != "file":
                        raise RuntimeError("拿到的是网页而不是文件（可能需要登录或有防盗链）")
                    name = None
                    cd = r.headers.get("content-disposition", "")
                    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd, re.I)
                    if m:
                        from urllib.parse import unquote
                        name = unquote(m.group(1))
                    name = safe_name(name or crawler.name_of(str(r.url)))
                    if "." not in name[-6:]:
                        name += mimetypes.guess_extension(ct) or ""
                    path = unique_path(dest_folder(item), name)
                    task["name"] = path.name
                    task["total"] = int(r.headers.get("content-length") or 0)
                    with open(path, "wb") as f:
                        async for chunk in r.aiter_bytes(64 * 1024):
                            if task["status"] == "canceled":
                                raise asyncio.CancelledError
                            f.write(chunk)
                            task["size"] += len(chunk)
                            if task["total"]:
                                task["percent"] = round(task["size"] * 100 / task["total"], 1)
            task.update(status="done", percent=100, path=str(path))
        except NeedYtdlp:
            await asyncio.to_thread(run_ytdlp, task, item)
        except asyncio.CancelledError:
            if path and path.exists():
                path.unlink(missing_ok=True)
        except Exception as e:
            if path and path.exists():
                path.unlink(missing_ok=True)
            msg = f"HTTP {e.response.status_code}" if isinstance(e, httpx.HTTPStatusError) else (str(e) or type(e).__name__)
            task.update(status="error", error=msg[:200])


@app.post("/api/download")
async def download(req: DlReq):
    ids = []
    for item in req.items[:2000]:
        if not item.url.startswith(("http://", "https://")):
            continue
        tid = uuid.uuid4().hex[:10]
        TASKS[tid] = {"id": tid, "url": item.url, "type": item.type, "name": crawler.name_of(item.url),
                      "status": "queued", "percent": 0, "size": 0, "total": 0, "error": "", "path": "",
                      "created": time.time()}
        asyncio.create_task(run_download(TASKS[tid], item))
        ids.append(tid)
    return {"ids": ids}


@app.get("/api/downloads")
async def downloads():
    tasks = [{k: v for k, v in t.items() if k != "proc"} for t in TASKS.values()]
    return {"tasks": tasks[-500:], "folder": str(DOWNLOADS)}


@app.post("/api/downloads/cancel")
async def cancel_all():
    for t in TASKS.values():
        if t["status"] in ("queued", "running"):
            t["status"] = "canceled"
            proc = t.get("proc")
            if proc:
                proc.terminate()
    return {"ok": True}


@app.post("/api/downloads/clear")
async def clear_finished():
    for tid in [k for k, t in TASKS.items() if t["status"] in ("done", "error", "canceled")]:
        TASKS.pop(tid, None)
    return {"ok": True}


class OpenReq(BaseModel):
    path: str = ""


@app.post("/api/open")
async def open_folder(req: OpenReq):
    """在资源管理器里打开下载文件夹（只允许 downloads 目录内）。"""
    target = Path(req.path).resolve() if req.path else DOWNLOADS
    if DOWNLOADS.resolve() not in [target, *target.parents]:
        raise HTTPException(400, "只能打开下载目录")
    if target.is_file():
        subprocess.Popen(["explorer", "/select,", str(target)])
    elif target.exists():
        os.startfile(target)  # noqa: S606
    return {"ok": True}


@app.get("/api/info")
async def info():
    try:
        import playwright  # noqa: F401
        has_browser = True
    except ImportError:
        has_browser = False
    return {"ytdlp": bool(YTDLP), "ytdlp_cmd": YTDLP, "node": NODE, "render": has_browser, "folder": str(DOWNLOADS)}


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}"
    print(f"\n  我的爬虫已启动：{url}\n  下载目录：{DOWNLOADS}\n  按 Ctrl+C 退出\n")
    if "--no-browser" not in __import__("sys").argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
