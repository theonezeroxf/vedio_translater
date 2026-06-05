from pathlib import Path

from scripts.batch_youtube_download import (
    build_assistant_html,
    extract_media_urls,
    read_youtube_urls,
    sanitize_filename,
)


def test_read_youtube_urls_from_file_deduplicates_and_ignores_comments(tmp_path: Path):
    source = tmp_path / "urls.txt"
    source.write_text(
        "# ignored\n"
        "https://www.youtube.com/watch?v=abc123\n"
        "note https://youtu.be/xyz987, extra text\n"
        "https://www.youtube.com/watch?v=abc123\n"
        "https://example.com/not-youtube\n",
        encoding="utf-8",
    )

    assert read_youtube_urls([str(source)]) == [
        "https://www.youtube.com/watch?v=abc123",
        "https://youtu.be/xyz987",
    ]


def test_extract_media_urls_from_json_and_html_removes_duplicates():
    payload = r'''
    {
      "data": {"url": "https:\/\/cdn.example.com\/video.mp4?token=1"},
      "html": "<a href='https://cdn.example.com/video.mp4?token=1'>download</a>"
    }
    '''

    medias = extract_media_urls(payload)

    assert [media.url for media in medias] == ["https://cdn.example.com/video.mp4?token=1"]


def test_sanitize_filename_handles_url_encoded_names():
    assert sanitize_filename("/%E6%B5%8B%E8%AF%95 video.mp4?x=1") == "测试_video.mp4"


def test_build_assistant_html_contains_all_urls_and_provider_pages(tmp_path: Path):
    output = tmp_path / "queue.html"
    build_assistant_html(["https://youtu.be/abc123"], output)

    text = output.read_text(encoding="utf-8")

    assert "https://youtu.be/abc123" in text
    assert "https://tubedown.cn/youtube" in text
    assert "https://youtube.iiilab.com/" in text
