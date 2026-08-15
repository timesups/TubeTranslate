from pathlib import Path

import pytest

from backend.app.adapters import ytdlp
from backend.app.sources import SourceConfig


def _make_source(*, use_proxy: bool, cookie_dir: Path) -> SourceConfig:
    cookie_path = cookie_dir / "missing-cookie.txt"

    class _Source(SourceConfig):
        @property
        def cookie_path(self):
            return cookie_path

    return _Source(
        name="test",
        matches=lambda url: True,
        use_proxy=use_proxy,
        cookie_filename="missing-cookie.txt",
        asr_language="en",
        target_language="zh",
    )


def test_ytdlp_proxy_port_takes_priority(monkeypatch, tmp_path):
    monkeypatch.setenv("HTTP_PROXY", "http://env-proxy:8080")
    source = _make_source(use_proxy=True, cookie_dir=tmp_path)

    options = ytdlp._ydl_base(source, "7890")

    assert options["proxy"] == "http://127.0.0.1:7890"


def test_ytdlp_proxy_falls_back_to_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HTTP_PROXY", "http://env-proxy:8080")
    source = _make_source(use_proxy=True, cookie_dir=tmp_path)

    options = ytdlp._ydl_base(source, "")

    assert options["proxy"] == "http://env-proxy:8080"


def test_ytdlp_disables_proxy_when_source_opts_out(monkeypatch, tmp_path):
    monkeypatch.setenv("HTTP_PROXY", "http://env-proxy:8080")
    source = _make_source(use_proxy=False, cookie_dir=tmp_path)

    options = ytdlp._ydl_base(source, "7890")

    assert options["proxy"] == ""


def test_ytdlp_enables_node_js_runtime(tmp_path):
    source = _make_source(use_proxy=True, cookie_dir=tmp_path)

    options = ytdlp._ydl_base(source, "")

    assert options["js_runtimes"] == {"node": {}}


def test_ytdlp_format_candidates_require_min_720p():
    assert all("height>=720" in item or "height>=1080" in item for item in ytdlp.FORMAT_CANDIDATES)
    assert "best" not in ytdlp.FORMAT_CANDIDATES  # bare best would accept 360p
    assert () in ytdlp.YOUTUBE_PLAYER_CLIENTS
    assert ytdlp.MIN_VIDEO_HEIGHT == 720
    assert ytdlp.DOWNLOAD_ATTEMPTS == 5


def test_is_format_unavailable_strips_ansi():
    exc = RuntimeError(
        "\x1b[0;31mERROR:\x1b[0m [youtube] 4NbT_wAW9aQ: Requested format is not available. "
        "Use --list-formats for a list of available formats"
    )
    assert ytdlp._is_format_unavailable(exc) is True


def _youtube_source() -> SourceConfig:
    return SourceConfig(
        name="youtube",
        matches=lambda url: True,
        use_proxy=False,
        cookie_filename=None,
        asr_language="en",
        target_language="zh",
    )


def _patch_min_resolution(monkeypatch):
    monkeypatch.setattr(ytdlp, "_ensure_min_resolution", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ytdlp.time, "sleep", lambda *_args, **_kwargs: None)


def test_download_video_passes_only_the_canonical_url_to_both_ytdlp_sinks(
    monkeypatch, tmp_path
):
    extracted_urls: list[str] = []
    downloaded_urls: list[str] = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def extract_info(self, url, *, download):
            extracted_urls.append(url)
            assert download is False
            return {
                "id": "abcdefghijk",
                "uploader": "tester",
                "title": "canonical",
                "webpage_url": url,
                "thumbnail": "https://i.ytimg.com/vi/abcdefghijk/maxresdefault.jpg",
            }

        def sanitize_info(self, info):
            return info

        def download(self, urls):
            downloaded_urls.extend(urls)
            Path(self.options["outtmpl"]).write_bytes(b"video")

    monkeypatch.setattr(ytdlp.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    _patch_cover(monkeypatch)
    _patch_min_resolution(monkeypatch)

    session, _ = ytdlp.download_video(
        "HTTPS://WWW.YOUTUBE.COM:443/watch?v=abcdefghijk",
        tmp_path,
        _youtube_source(),
    )

    expected = "https://www.youtube.com/watch?v=abcdefghijk"
    assert extracted_urls == [expected]
    assert downloaded_urls == [expected]
    assert (session / "media" / "video_source.mp4").read_bytes() == b"video"
    assert (session / "media" / "cover_source.jpg").read_bytes() == b"\xff\xd8\xffcover"


class _FakeResponse:
    content = b"\xff\xd8\xffcover"

    def raise_for_status(self):
        return None


def _patch_cover(monkeypatch):
    monkeypatch.setattr(ytdlp.requests, "get", lambda *args, **kwargs: _FakeResponse())
    monkeypatch.setattr(
        ytdlp,
        "_save_cover_jpeg",
        lambda image_bytes, dest: dest.write_bytes(image_bytes),
    )


def test_download_video_does_not_reuse_leftover_file(monkeypatch, tmp_path):
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def extract_info(self, url, *, download):
            return {
                "id": "abcdefghijk",
                "uploader": "tester",
                "title": "leftover",
                "webpage_url": url,
                "thumbnail": "https://i.ytimg.com/vi/abcdefghijk/maxresdefault.jpg",
            }

        def sanitize_info(self, info):
            return info

        def download(self, urls):
            Path(self.options["outtmpl"]).write_bytes(b"fresh-video")

    monkeypatch.setattr(ytdlp.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    _patch_cover(monkeypatch)
    _patch_min_resolution(monkeypatch)

    session = tmp_path / "tester" / "leftover__abcdefghijk"
    leftover = session / "media" / "video_source.mp4"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b"corrupt-partial")

    result, _ = ytdlp.download_video(
        "https://www.youtube.com/watch?v=abcdefghijk",
        tmp_path,
        _youtube_source(),
    )
    assert (result / "media" / "video_source.mp4").read_bytes() == b"fresh-video"


def test_download_video_deletes_partial_and_raises_on_403(monkeypatch, tmp_path):
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def extract_info(self, url, *, download):
            return {
                "id": "abcdefghijk",
                "uploader": "tester",
                "title": "forbidden",
                "webpage_url": url,
                "thumbnail": "https://i.ytimg.com/vi/abcdefghijk/maxresdefault.jpg",
            }

        def sanitize_info(self, info):
            return info

        def download(self, urls):
            Path(self.options["outtmpl"]).write_bytes(b"partial")
            raise ytdlp.yt_dlp.utils.DownloadError("ERROR: unable to download video data: HTTP Error 403: Forbidden")

    monkeypatch.setattr(ytdlp.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    _patch_cover(monkeypatch)
    _patch_min_resolution(monkeypatch)

    with pytest.raises(RuntimeError, match="YouTube returned HTTP 403"):
        ytdlp.download_video(
            "https://www.youtube.com/watch?v=abcdefghijk",
            tmp_path,
            _youtube_source(),
        )

    leftover = tmp_path / "tester" / "forbidden__abcdefghijk" / "media" / "video_source.mp4"
    assert not leftover.exists()


def test_download_retries_403_with_next_player_client(monkeypatch, tmp_path):
    seen_clients: list[list[str]] = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def extract_info(self, url, *, download):
            return {
                "id": "abcdefghijk",
                "uploader": "tester",
                "title": "retry403",
                "webpage_url": url,
                "thumbnail": "https://i.ytimg.com/vi/abcdefghijk/maxresdefault.jpg",
            }

        def sanitize_info(self, info):
            return info

        def download(self, urls):
            clients = list(self.options.get("extractor_args", {}).get("youtube", {}).get("player_client") or [])
            seen_clients.append(clients)
            if clients == list(ytdlp.YOUTUBE_PLAYER_CLIENTS[0]):
                raise ytdlp.yt_dlp.utils.DownloadError("HTTP Error 403: Forbidden")
            Path(self.options["outtmpl"]).write_bytes(b"ok")

    monkeypatch.setattr(ytdlp.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    _patch_cover(monkeypatch)
    _patch_min_resolution(monkeypatch)

    session, _ = ytdlp.download_video(
        "https://www.youtube.com/watch?v=abcdefghijk",
        tmp_path,
        _youtube_source(),
    )
    assert (session / "media" / "video_source.mp4").read_bytes() == b"ok"
    assert seen_clients[0] == list(ytdlp.YOUTUBE_PLAYER_CLIENTS[0])
    assert seen_clients[-1] == list(ytdlp.YOUTUBE_PLAYER_CLIENTS[1])


def test_download_rejects_below_720p_and_retries_then_fails(monkeypatch, tmp_path):
    attempts = {"count": 0}

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def extract_info(self, url, *, download):
            return {
                "id": "abcdefghijk",
                "uploader": "tester",
                "title": "lowres",
                "webpage_url": url,
                "thumbnail": "https://i.ytimg.com/vi/abcdefghijk/maxresdefault.jpg",
            }

        def sanitize_info(self, info):
            return info

        def download(self, urls):
            attempts["count"] += 1
            Path(self.options["outtmpl"]).write_bytes(b"fake-360p")

    monkeypatch.setattr(ytdlp.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    _patch_cover(monkeypatch)
    monkeypatch.setattr(ytdlp, "_probe_video_height", lambda *_args, **_kwargs: 360)
    monkeypatch.setattr(ytdlp.time, "sleep", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="minimum 720p"):
        ytdlp.download_video(
            "https://www.youtube.com/watch?v=abcdefghijk",
            tmp_path,
            _youtube_source(),
        )

    # Each outer attempt tries multiple format/client candidates until resolution fails.
    assert attempts["count"] >= ytdlp.DOWNLOAD_ATTEMPTS
    leftover = tmp_path / "tester" / "lowres__abcdefghijk" / "media" / "video_source.mp4"
    assert not leftover.exists()


def test_download_accepts_720p_or_higher(monkeypatch, tmp_path):
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def extract_info(self, url, *, download):
            return {
                "id": "abcdefghijk",
                "uploader": "tester",
                "title": "hd720",
                "webpage_url": url,
                "thumbnail": "https://i.ytimg.com/vi/abcdefghijk/maxresdefault.jpg",
            }

        def sanitize_info(self, info):
            return info

        def download(self, urls):
            Path(self.options["outtmpl"]).write_bytes(b"fake-720p")

    monkeypatch.setattr(ytdlp.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    _patch_cover(monkeypatch)
    monkeypatch.setattr(ytdlp, "_probe_video_height", lambda *_args, **_kwargs: 720)

    session, _ = ytdlp.download_video(
        "https://www.youtube.com/watch?v=abcdefghijk",
        tmp_path,
        _youtube_source(),
    )
    assert (session / "media" / "video_source.mp4").read_bytes() == b"fake-720p"


def test_ydl_base_sets_youtube_player_clients(tmp_path):
    options = ytdlp._ydl_base(_youtube_source(), "")
    assert options["extractor_args"]["youtube"]["player_client"] == list(
        ytdlp.YOUTUBE_PLAYER_CLIENTS[0]
    )


def test_pick_thumbnail_url_prefers_largest():
    url = ytdlp._pick_thumbnail_url(
        {
            "thumbnail": "https://example.com/small.jpg",
            "thumbnails": [
                {"url": "https://example.com/a.jpg", "width": 120, "height": 90},
                {"url": "https://example.com/b.jpg", "width": 1280, "height": 720},
            ],
        }
    )
    assert url == "https://example.com/b.jpg"


def test_download_video_rejects_deceptive_url_before_cookie_or_ytdlp(
    monkeypatch, tmp_path
):
    calls: list[str] = []
    monkeypatch.setattr(ytdlp, "_ensure_cookie", lambda source: calls.append("cookie"))
    monkeypatch.setattr(
        ytdlp.yt_dlp,
        "YoutubeDL",
        lambda options: calls.append("ytdlp"),
    )

    with pytest.raises(ValueError):
        ytdlp.download_video(
            "https://youtube.com.evil.example/watch?v=abcdefghijk",
            tmp_path,
            _youtube_source(),
        )

    assert calls == []
