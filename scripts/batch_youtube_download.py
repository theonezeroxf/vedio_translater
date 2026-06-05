#!/usr/bin/env python3
"""Batch helper for downloading YouTube videos through web parsing services.

The public pages at https://tubedown.cn/youtube and https://youtube.iiilab.com/
are designed for a browser workflow: paste one YouTube URL, parse it, then click
the returned download link.  This script adds a batch-friendly wrapper around
that workflow while keeping the network implementation configurable because
those sites do not publish a stable machine API and their form endpoints may
change.

Common usage:

    # Open a local queue page with copy/open buttons for every URL.
    python scripts/batch_youtube_download.py urls.txt --assistant-html queue.html

    # Try an HTTP endpoint directly.  If the provider changes its endpoint,
    # override --endpoint/--method/--url-field without editing the script.
    python scripts/batch_youtube_download.py urls.txt --provider tubedown --output downloads

Only download videos that you own, have permission to download, or that are
licensed for offline use.  Respect YouTube and provider terms of service.
"""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

PROVIDERS = {
    "tubedown": {
        "page": "https://tubedown.cn/youtube",
        "endpoint": "https://tubedown.cn/youtube",
        "method": "POST",
        "url_field": "url",
        "referer": "https://tubedown.cn/youtube",
    },
    "iiilab": {
        "page": "https://youtube.iiilab.com/",
        "endpoint": "https://youtube.iiilab.com/",
        "method": "POST",
        "url_field": "link",
        "referer": "https://youtube.iiilab.com/",
    },
}

YOUTUBE_RE = re.compile(
    r"^https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)"
)
MEDIA_URL_RE = re.compile(
    r"https?://[^\s'\"<>]+?(?:\.mp4|\.webm|\.m4a|\.mp3)(?:\?[^\s'\"<>]*)?",
    re.IGNORECASE,
)
FILENAME_SAFE_RE = re.compile(r"[^\w.()+\-=\u4e00-\u9fff]+", re.UNICODE)


@dataclass(frozen=True)
class ResolvedMedia:
    url: str
    label: str = "video"


def read_youtube_urls(paths_or_urls: Iterable[str]) -> list[str]:
    """Read YouTube URLs from arguments, files, or stdin."""

    raw_lines: list[str] = []
    values = list(paths_or_urls)
    if not values:
        raw_lines.extend(sys.stdin.read().splitlines())
    for value in values:
        candidate = Path(value)
        if candidate.exists() and candidate.is_file():
            raw_lines.extend(candidate.read_text(encoding="utf-8").splitlines())
        else:
            raw_lines.append(value)

    urls: list[str] = []
    seen: set[str] = set()
    for line in raw_lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        # Allow files containing notes before/after the link.
        found = re.findall(r"https?://[^\s,，]+", text)
        for url in found or [text]:
            url = url.strip().strip("'\"<>，,。")
            if not YOUTUBE_RE.match(url):
                continue
            if url not in seen:
                urls.append(url)
                seen.add(url)
    return urls


def sanitize_filename(name: str, default: str = "youtube_video") -> str:
    name = urllib.parse.unquote(name).split("?")[0].split("#")[0]
    name = Path(name).name or default
    name = FILENAME_SAFE_RE.sub("_", name).strip("._ ")
    return name or default


def iter_json_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_json_strings(item)


def extract_media_urls(text: str) -> list[ResolvedMedia]:
    """Extract direct media URLs from JSON or HTML returned by a provider."""

    urls: list[str] = []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if data is not None:
        for value in iter_json_strings(data):
            urls.extend(MEDIA_URL_RE.findall(value))
    urls.extend(MEDIA_URL_RE.findall(html.unescape(text)))

    resolved: list[ResolvedMedia] = []
    seen: set[str] = set()
    for url in urls:
        clean_url = url.replace("\\/", "/")
        if clean_url not in seen:
            resolved.append(ResolvedMedia(url=clean_url, label="video"))
            seen.add(clean_url)
    return resolved


def request_provider(
    video_url: str,
    *,
    endpoint: str,
    method: str,
    url_field: str,
    referer: str,
    timeout: float,
) -> str:
    payload = urllib.parse.urlencode({url_field: video_url}).encode("utf-8")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
        "Accept": "application/json,text/html,application/xhtml+xml,*/*;q=0.8",
        "Referer": referer,
    }
    http_method = method.upper()
    if http_method == "GET":
        joiner = "&" if "?" in endpoint else "?"
        request_url = f"{endpoint}{joiner}{payload.decode('utf-8')}"
        data = None
    else:
        request_url = endpoint
        data = payload
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    req = urllib.request.Request(request_url, data=data, headers=headers, method=http_method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return body.decode(charset, errors="replace")


def download_file(url: str, destination: Path, timeout: float, overwrite: bool) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        final_url = response.geturl()
        content_type = response.headers.get_content_type()
        name = sanitize_filename(urllib.parse.urlsplit(final_url).path)
        if "." not in name:
            ext = mimetypes.guess_extension(content_type) or ".mp4"
            name += ext
        target = destination / name
        if target.exists() and not overwrite:
            stem, suffix = target.stem, target.suffix
            index = 2
            while target.exists():
                target = destination / f"{stem}_{index}{suffix}"
                index += 1
        with target.open("wb") as fh:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
        return target


def build_assistant_html(urls: list[str], output: Path) -> None:
    cards = []
    for index, url in enumerate(urls, 1):
        escaped_url = html.escape(url, quote=True)
        buttons = []
        for provider in PROVIDERS.values():
            page = html.escape(provider["page"], quote=True)
            buttons.append(f'<a target="_blank" rel="noreferrer" href="{page}">打开解析站</a>')
        cards.append(
            f"""
            <section class="card">
              <h2>{index}. YouTube 链接</h2>
              <p><code>{escaped_url}</code></p>
              <button data-url="{escaped_url}">复制链接</button>
              {' '.join(buttons)}
            </section>
            """
        )
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>YouTube 批量下载队列</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; }}
.card {{ border: 1px solid #ddd; border-radius: 10px; padding: 1rem; margin: 1rem 0; }}
button, a {{ display: inline-block; margin: .25rem .5rem .25rem 0; padding: .5rem .75rem; }}
code {{ overflow-wrap: anywhere; }}
.notice {{ background: #fff7d6; border: 1px solid #f0d66b; padding: 1rem; border-radius: 10px; }}
</style>
</head>
<body>
<h1>YouTube 批量下载队列</h1>
<p class="notice">请只下载你拥有版权、已获授权或许可离线使用的视频，并遵守 YouTube 与解析站条款。</p>
<p>每条记录先点击“复制链接”，再打开 Tubedown 或 iiiLab 页面粘贴解析下载。</p>
{''.join(cards)}
<script>
document.querySelectorAll('button[data-url]').forEach((button) => {{
  button.addEventListener('click', async () => {{
    await navigator.clipboard.writeText(button.dataset.url);
    button.textContent = '已复制';
    setTimeout(() => button.textContent = '复制链接', 1500);
  }});
}});
</script>
</body>
</html>"""
    output.write_text(document, encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量处理 YouTube 链接并通过解析站下载视频。")
    parser.add_argument("inputs", nargs="*", help="YouTube URL、包含 URL 的文本文件；省略时从 stdin 读取。")
    parser.add_argument("--provider", choices=sorted(PROVIDERS), default="tubedown", help="默认解析服务。")
    parser.add_argument("--endpoint", help="覆盖解析 HTTP 端点；解析站接口变化时使用。")
    parser.add_argument("--method", choices=["GET", "POST"], help="解析 HTTP 方法。")
    parser.add_argument("--url-field", help="解析请求中 YouTube 链接对应的表单字段名。")
    parser.add_argument("--output", default="downloads/youtube", help="下载文件保存目录。")
    parser.add_argument("--timeout", type=float, default=60.0, help="单次 HTTP 请求超时时间（秒）。")
    parser.add_argument("--sleep", type=float, default=2.0, help="每条链接处理后的等待秒数，避免请求过快。")
    parser.add_argument("--overwrite", action="store_true", help="允许覆盖同名文件。")
    parser.add_argument("--dry-run", action="store_true", help="只解析并打印媒体链接，不下载。")
    parser.add_argument("--assistant-html", type=Path, help="生成可点击的批量处理 HTML 队列并退出。")
    parser.add_argument("--open", action="store_true", help="与 --assistant-html 一起使用时自动用浏览器打开。")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    urls = read_youtube_urls(args.inputs)
    if not urls:
        print("未找到有效的 YouTube 链接。", file=sys.stderr)
        return 2

    if args.assistant_html:
        build_assistant_html(urls, args.assistant_html)
        print(f"已生成批量处理页面：{args.assistant_html}")
        if args.open:
            webbrowser.open(args.assistant_html.resolve().as_uri())
        return 0

    provider = PROVIDERS[args.provider]
    endpoint = args.endpoint or provider["endpoint"]
    method = args.method or provider["method"]
    url_field = args.url_field or provider["url_field"]
    output_dir = Path(args.output)

    failures = 0
    for index, video_url in enumerate(urls, 1):
        print(f"[{index}/{len(urls)}] 解析：{video_url}")
        try:
            response_text = request_provider(
                video_url,
                endpoint=endpoint,
                method=method,
                url_field=url_field,
                referer=provider["referer"],
                timeout=args.timeout,
            )
            medias = extract_media_urls(response_text)
            if not medias:
                raise RuntimeError("解析响应中未找到直接媒体链接；请检查 --endpoint/--url-field，或改用 --assistant-html。")
            media = medias[0]
            print(f"  媒体链接：{media.url}")
            if not args.dry_run:
                target = download_file(media.url, output_dir, args.timeout, args.overwrite)
                print(f"  已保存：{target}")
        except (urllib.error.URLError, TimeoutError, RuntimeError, OSError) as exc:
            failures += 1
            print(f"  失败：{exc}", file=sys.stderr)
        if index < len(urls) and args.sleep > 0:
            time.sleep(args.sleep)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
