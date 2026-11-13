"""抓取与解析：从网页里找出图片、视频、音频、文件、链接、正文、表格。"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse, unquote
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

EXT = {
    "image": {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "avif", "ico", "tif", "tiff"},
    "video": {"mp4", "webm", "mkv", "mov", "avi", "flv", "m4v", "m3u8", "mpd", "ts", "wmv"},
    "audio": {"mp3", "m4a", "flac", "wav", "ogg", "aac", "opus", "wma"},
    "file": {
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "txt", "json", "xml",
        "zip", "rar", "7z", "tar", "gz", "epub", "mobi", "apk", "exe", "dmg", "torrent",
    },
}
EXT_TO_TYPE = {e: t for t, exts in EXT.items() for e in exts}
MEDIA_RE = re.compile(
    r"""(?:https?:)?//[^\s"'<>\\()]+?\.(?:mp4|m3u8|webm|mov|flv|mpd|mp3|m4a|flac|wav|ogg|aac)(?:\?[^\s"'<>\\()]*)?""",
    re.I,
)
CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.I)
EMBED_HOSTS = ("youtube.com", "youtu.be", "bilibili.com", "vimeo.com", "dailymotion.com", "youku.com", "v.qq.com", "ixigua.com")
ALL_TYPES = ["image", "video", "audio", "file", "link", "text", "table"]
MAX_PAGES_LIMIT = 300
MAX_HTML_BYTES = 8 * 1024 * 1024


def ext_of(url: str) -> str:
    path = urlparse(url).path
    name = path.rsplit("/", 1)[-1]
    if "." in name:
        return name.rsplit(".", 1)[-1].lower()[:8]
    return ""


def name_of(url: str) -> str:
    path = unquote(urlparse(url).path)
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return name or urlparse(url).netloc


def type_by_ext(url: str) -> str | None:
    return EXT_TO_TYPE.get(ext_of(url))


def type_by_content_type(ct: str) -> str | None:
    ct = (ct or "").split(";")[0].strip().lower()
    if ct.startswith("image/"):
        return "image"
    if ct.startswith("video/") or ct in ("application/vnd.apple.mpegurl", "application/x-mpegurl", "application/dash+xml"):
        return "video"
    if ct.startswith("audio/"):
        return "audio"
    if ct in ("application/pdf", "application/zip", "application/octet-stream", "text/csv") or "officedocument" in ct:
        return "file"
    return None


def clean(text: str | None, limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit]


def pick_srcset(srcset: str) -> list[str]:
    """srcset 里取最大的一张。"""
    best, best_w = None, -1.0
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        w = 0.0
        if len(bits) > 1:
            m = re.match(r"([\d.]+)[wx]", bits[1])
            if m:
                w = float(m.group(1))
        if w > best_w:
            best, best_w = bits[0], w
    return [best] if best else []


@dataclass
class Job:
    id: str
    params: dict
    status: str = "running"  # running / done / stopped / error
    message: str = ""
    items: list = field(default_factory=list)
    pages: list = field(default_factory=list)
    seen: set = field(default_factory=set)
    queued: int = 0
    stop: bool = False
    started: float = field(default_factory=time.time)
    finished: float | None = None

    def add(self, type_: str, url: str, page: str, note: str = "", data=None, kind: str = ""):
        if type_ not in self.params["types"] and type_ != "custom":
            return
        key = (type_, url) if type_ not in ("text", "table", "custom") else (type_, url, len(self.items))
        if key in self.seen:
            return
        self.seen.add(key)
        self.items.append({
            "id": len(self.items),
            "type": type_,
            "url": url,
            "page": page,
            "name": name_of(url) if type_ not in ("text", "table", "custom") else "",
            "ext": ext_of(url),
            "note": note,
            "kind": kind,
            "data": data,
        })

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for it in self.items:
            counts[it["type"]] = counts.get(it["type"], 0) + 1
        return {
            "id": self.id,
            "status": self.status,
            "message": self.message,
            "pages": self.pages,
            "queued": self.queued,
            "counts": counts,
            "total": len(self.items),
            "elapsed": round((self.finished or time.time()) - self.started, 1),
            "params": self.params,
        }


JOBS: dict[str, Job] = {}


def new_job(params: dict) -> Job:
    job = Job(id=uuid.uuid4().hex[:10], params=params)
    JOBS[job.id] = job
    # 只保留最近 20 个任务
    for old in list(JOBS)[:-20]:
        JOBS.pop(old, None)
    return job


def extract(job: Job, page_url: str, html_bytes: bytes) -> list[str]:
    """解析一个页面，把资源记进 job，返回页面里的链接（供继续爬）。"""
    soup = BeautifulSoup(html_bytes, "lxml")
    base_tag = soup.find("base", href=True)
    base = urljoin(page_url, base_tag["href"]) if base_tag else page_url

    def absu(u: str | None) -> str | None:
        if not u:
            return None
        u = u.strip()
        if not u or u.startswith(("data:", "javascript:", "mailto:", "tel:", "blob:", "#", "about:")):
            return None
        full = urldefrag(urljoin(base, u))[0]
        return full if full.startswith(("http://", "https://")) else None

    def add(type_, u, note="", kind=""):
        full = absu(u)
        if full:
            job.add(type_, full, page_url, clean(note), kind=kind)

    title = clean(soup.title.string if soup.title else "", 300)

    # —— 图片 ——
    for img in soup.find_all("img"):
        note = img.get("alt") or img.get("title") or ""
        cands = []
        for attr in ("data-original", "data-src", "data-lazy-src", "data-actualsrc", "data-lazy", "data-url", "src"):
            if img.get(attr):
                cands.append(img[attr])
                break
        for attr in ("data-srcset", "srcset"):
            if img.get(attr):
                cands += pick_srcset(img[attr])
                break
        for c in cands:
            add("image", c, note)
    for src in soup.select("picture source[srcset]"):
        for c in pick_srcset(src["srcset"]):
            add("image", c)
    for meta in soup.find_all("meta"):
        prop = (meta.get("property") or meta.get("name") or "").lower()
        content = meta.get("content")
        if prop in ("og:image", "og:image:url", "twitter:image", "twitter:image:src"):
            add("image", content, "封面图")
        elif prop in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"):
            add("video", content, title or "页面声明的视频")
        elif prop in ("og:audio", "og:audio:url"):
            add("audio", content, "页面声明的音频")
    for el in soup.find_all(style=True):
        for u in CSS_URL_RE.findall(el["style"]):
            add("image", u, "背景图")

    # —— 视频 / 音频 ——
    for tag, type_ in (("video", "video"), ("audio", "audio")):
        for el in soup.find_all(tag):
            add(type_, el.get("src"), el.get("title") or title)
            for s in el.find_all("source"):
                add(type_, s.get("src"), el.get("title") or title)
            if tag == "video" and el.get("poster"):
                add("image", el["poster"], "视频封面")
    for el in soup.find_all(["iframe", "embed"]):
        full = absu(el.get("src") or el.get("data-src"))
        if full and any(h in urlparse(full).netloc for h in EMBED_HOSTS):
            job.add("video", full, page_url, clean(el.get("title") or "嵌入的播放器"), kind="embed")

    # 藏在脚本 / JSON 里的媒体地址
    text = html_bytes.decode(soup.original_encoding or "utf-8", errors="replace").replace("\\/", "/")
    text = text.replace("\\u002F", "/").replace("\\u0026", "&").replace("&amp;", "&")
    for m in MEDIA_RE.findall(text):
        t = type_by_ext(absu(m) or "")
        if t in ("video", "audio"):
            add(t, m, title)

    # —— 链接（顺带按后缀归类） ——
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        full = absu(a["href"])
        if not full:
            continue
        note = a.get_text(" ", strip=True) or a.get("title") or ""
        t = type_by_ext(full)
        if t:
            job.add(t, full, page_url, clean(note))
        else:
            links.append(full)
            same = urlparse(full).netloc == urlparse(page_url).netloc
            job.add("link", full, page_url, clean(note), kind="站内" if same else "站外")

    # —— 表格 ——
    if "table" in job.params["types"]:
        for i, table in enumerate(soup.find_all("table")):
            rows = []
            for tr in table.find_all("tr"):
                cells = [clean(c.get_text(" ", strip=True), 500) for c in tr.find_all(["th", "td"])]
                if any(cells):
                    rows.append(cells)
            if len(rows) >= 2:
                cap = table.find("caption")
                job.add("table", page_url, page_url, clean(cap.get_text() if cap else f"{title} · 表格 {i + 1}"),
                        data={"rows": rows[:2000]})

    # —— 自定义选择器 ——
    sel = (job.params.get("selector") or "").strip()
    if sel:
        try:
            for el in soup.select(sel)[:2000]:
                link = absu(el.get("href") or el.get("src"))
                job.add("custom", link or page_url, page_url, "",
                        data={"text": clean(el.get_text(" ", strip=True), 2000), "link": link or "", "tag": el.name})
        except Exception as e:  # 选择器写错
            job.message = f"选择器无效：{e}"

    # —— 正文 ——（放最后，因为要删节点）
    if "text" in job.params["types"]:
        desc = ""
        m = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
        if m:
            desc = clean(m.get("content"), 500)
        headings = [
            {"level": int(h.name[1]), "text": clean(h.get_text(" ", strip=True), 200)}
            for h in soup.find_all(["h1", "h2", "h3"])
        ]
        headings = [h for h in headings if h["text"]][:200]
        for junk in soup(["script", "style", "noscript", "template", "svg", "nav", "footer", "header", "aside", "form"]):
            junk.decompose()
        root = soup.find("article") or soup.find("main") or soup.body or soup
        paras = []
        for p in root.find_all(["p", "li", "blockquote", "pre", "h1", "h2", "h3", "h4"]):
            t = clean(p.get_text(" ", strip=True), 3000)
            if len(t) >= 2 and (not paras or paras[-1] != t):
                paras.append(t)
        body = "\n".join(paras)
        if len(body) < 200:  # 没有规整段落的页面，退回整页文字
            body = re.sub(r"\n\s*\n+", "\n", root.get_text("\n", strip=True))
        job.add("text", page_url, page_url, title,
                data={"title": title, "description": desc, "headings": headings, "text": body[:60000], "chars": len(body)})

    return links, title


class Robots:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.cache: dict[str, RobotFileParser | None] = {}

    async def allowed(self, url: str) -> bool:
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self.cache:
            rp = None
            try:
                r = await self.client.get(origin + "/robots.txt", timeout=8)
                if r.status_code == 200 and "<html" not in r.text[:300].lower():
                    rp = RobotFileParser()
                    rp.parse(r.text.splitlines())
            except Exception:
                rp = None
            self.cache[origin] = rp
        rp = self.cache[origin]
        return True if rp is None else rp.can_fetch("*", url)


async def render_with_browser(url: str) -> bytes:
    """用无头浏览器打开页面（给靠 JS 出内容的网站用），自动往下滚几屏。"""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = None
        last_err = None
        for kwargs in ({}, {"channel": "msedge"}, {"channel": "chrome"}):
            try:
                browser = await p.chromium.launch(headless=True, **kwargs)
                break
            except Exception as e:
                last_err = e
        if browser is None:
            raise RuntimeError(f"没有可用的浏览器：{last_err}")
        try:
            page = await browser.new_page(user_agent=UA, viewport={"width": 1366, "height": 900})
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            for _ in range(6):
                await page.mouse.wheel(0, 2400)
                await page.wait_for_timeout(450)
            return (await page.content()).encode("utf-8")
        finally:
            await browser.close()


async def crawl(job: Job):
    p = job.params
    start = p["url"]
    start_host = urlparse(start).netloc
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    visited = {start}
    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20) as client:
            robots = Robots(client)
            while queue and not job.stop and len(job.pages) < p["max_pages"]:
                url, depth = queue.popleft()
                job.queued = len(queue)
                rec = {"url": url, "depth": depth, "status": "抓取中", "title": "", "found": 0}
                job.pages.append(rec)
                before = len(job.items)
                try:
                    if p["robots"] and not await robots.allowed(url):
                        rec["status"] = "robots.txt 不允许，已跳过"
                        continue
                    if p["render"]:
                        body = await render_with_browser(url)
                        links, title = extract(job, url, body)
                    else:
                        async with client.stream("GET", url) as r:
                            ct = r.headers.get("content-type", "")
                            final = str(r.url)
                            if r.status_code >= 400:
                                hint = {401: "需要登录", 403: "网站拒绝了自动访问", 404: "页面不存在", 429: "访问太频繁"}.get(r.status_code)
                                rec["status"] = f"HTTP {r.status_code}" + (f"（{hint}）" if hint else "")
                                continue
                            direct = type_by_content_type(ct)
                            if direct and "html" not in ct:
                                job.add(direct, final, url, "直接链接")
                                rec["status"] = "完成"
                                rec["found"] = len(job.items) - before
                                continue
                            chunks, size = [], 0
                            async for chunk in r.aiter_bytes():
                                chunks.append(chunk)
                                size += len(chunk)
                                if size > MAX_HTML_BYTES:
                                    break
                            body = b"".join(chunks)
                        links, title = extract(job, final, body)
                    rec["title"] = title
                    rec["status"] = "完成"
                    rec["found"] = len(job.items) - before
                    if depth < p["depth"]:
                        for link in links:
                            if link in visited:
                                continue
                            if p["same_site"] and urlparse(link).netloc != start_host:
                                continue
                            visited.add(link)
                            queue.append((link, depth + 1))
                        job.queued = len(queue)
                except Exception as e:
                    rec["status"] = f"失败：{type(e).__name__} {str(e)[:120]}"
                finally:
                    if queue and not job.stop:
                        await asyncio.sleep(p["delay"])
        job.status = "stopped" if job.stop else "done"
    except Exception as e:
        job.status = "error"
        job.message = f"{type(e).__name__}: {e}"
    finally:
        job.queued = 0
        job.finished = time.time()
