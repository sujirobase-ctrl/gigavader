"""Central configuration: paths, colors, enums and host matcher table.

Everything tunable lives here so the rest of the package stays declarative.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

# --- Paths -------------------------------------------------------------------

URLS_FILE = "URLs.txt"
SESSION_LOG = "session.log"
DOWNLOAD_FOLDER = "Downloads"
STATE_FILENAME = ".fpd_state.json"

# --- HTTP --------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 30
CHUNK_SIZE = 64 * 1024
MAX_RETRIES = 6
RETRY_BACKOFF = 2.0
# Exponential backoff base (seconds) used *only* for HTTP 429 rate-limiting.
# 5 ** attempt -> 5s, 25s, 60s(capped). Longer than RETRY_BACKOFF because a 429
# means the CDN is throttling us by IP and hammering it again immediately just
# extends the ban window (seen on SimpCity's c3wi-b.cdn.cr).
RATE_429_BACKOFF = 5.0
MAX_WORKERS = 3
# Seconds to wait before starting each file. CDNs such as filester/bunkr
# rate-limit rapid or concurrent requests (HTTP 429); a modest delay keeps a
# parallel run under the limit without making single-file downloads painful.
INTER_FILE_DELAY = 3.0

# --- Parallel chunked download ----------------------------------------------
# Adapted from Lysagxra/BunkrDownloader's design: large files are split into
# small byte-range work units so a dropped connection only retries the failed
# unit (not the whole file), and chunks resume across runs. Constants below
# tune when chunking kicks in and how units are sized.
MIN_PARALLEL_SIZE = 5 * 1024 * 1024        # only chunk files >= 5 MB
MIN_WORK_UNIT_SIZE = 4 * 1024 * 1024       # smallest byte-range unit
MAX_WORK_UNIT_SIZE = 64 * 1024 * 1024      # largest byte-range unit
UNITS_PER_CONNECTION = 4                    # more units than threads -> work-stealing
CHUNK_MAX_RETRIES = 5                       # internal retries per chunk
CHUNK_BASE_DELAY = 2.0                      # backoff base for chunk retries
LARGE_FILE_CHUNK_SIZE = 1024 * 1024         # read buffer for chunk writes

# --- UI ----------------------------------------------------------------------

REFRESH_PER_SECOND = 10
MAX_LOG_ROWS = 4
OVERALL_BUFFER_SIZE = 3

PROGRESS_MANAGER_COLORS = {
    "overall_border_color": "bright_blue",
    "task_border_color": "medium_purple",
    "progress_color": "light_cyan3",
}

LOG_COLORS = {
    "event": "light_cyan3",
    "details": "grey85",
    "timestamp": "grey58",
    "border": "grey35",
}

PROGRESS_COLUMNS_SEPARATOR = "[grey58]•"

VERSION = "2.0.0"


def get_version_string() -> str:
    """Return the footer version string shown under the live view."""
    return f"ForumPostDownloader CLI v{VERSION}"


# --- Result taxonomy ---------------------------------------------------------


class TaskResult(IntEnum):
    """Terminal state of a single media task."""

    COMPLETED = 0
    SKIPPED = 1
    FAILED = 2


class SkipReason(IntEnum):
    """Why a task was skipped."""

    ALREADY_DOWNLOADED = 0
    IGNORED_EXTENSION = 1
    NOT_INCLUDED = 2
    DRY_RUN = 3


class FailReason(IntEnum):
    """Why a task failed."""

    HTTP_ERROR = 0
    UNRESOLVED = 1
    WRITE_ERROR = 2
    NETWORK_ERROR = 3


TASK_REASON_MAPPING = {
    TaskResult.SKIPPED: SkipReason,
    TaskResult.FAILED: FailReason,
}


@dataclass
class ProgressConfig:
    """Bundle of progress-bar presentation settings."""

    task_name: str
    item_description: str
    color: str = PROGRESS_MANAGER_COLORS["progress_color"]
    panel_width: int = 40
    overall_buffer: deque = field(
        default_factory=lambda: deque(maxlen=OVERALL_BUFFER_SIZE),
    )


# --- Filename sanitising -----------------------------------------------------

INVALID_PATH_CHARS = r'<>:"/\|?*'
INVALID_CHAR_SUBSTITUTE = "-"
MAX_FILENAME_LENGTH = 180

# NOTE: .webp / .avif are intentionally excluded — the user wants only
# traditional raster formats (jpg/jpeg/png and friends), never webp.
IMAGE_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".jpe", ".svg", ".tif", ".tiff",
    ".jif", ".bmp",
)
VIDEO_EXTENSIONS = (
    ".mpeg", ".avchd", ".webm", ".mpv", ".swf", ".avi", ".m4p", ".wmv", ".mp2",
    ".m4v", ".qt", ".mpe", ".mp4", ".flv", ".mov", ".mpg", ".mkv", ".ts",
)
AUDIO_EXTENSIONS = (
    ".mp3", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".flac", ".opus", ".wma",
    ".aiff", ".aif", ".caf", ".weba",
)
ARCHIVE_EXTENSIONS = (".zip", ".rar", ".7z", ".tar", ".gz")

CONTENT_TYPE_EXTENSIONS = {
    "video/mp4": "mp4",
    "video/webm": "webm",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "application/zip": "zip",
    "application/x-7z-compressed": "7z",
    "application/x-rar": "rar",
    "application/vnd.rar": "rar",
}


# --- Host matcher table ------------------------------------------------------
# Ported from ForumPostDownloader src/main.js `hosts`.
# Each entry: (name, category, single_regex, album_regex | None)

HOSTS: list[tuple[str, str, str, str | None]] = [
    (
        "simpcity",
        "Attachments",
        r"simpcity\.(su|cr|is|cz|hk|rs|ax)/(attachments|data/video)/",
        None,
    ),
    ("coomer", "Profiles", r"coomer\.(st|party|su)/[\w.\-]+/user", None),
    ("coomer", "image", r"(\w+\.)?coomer\.(st|party|su)/(data|thumbnail)", None),
    (
        "jpg.church",
        "image",
        r"(simp\d+\.)?(cuckcapital\.cr|jpe?g\d?\.(church|fish|fishing|pet|su|cr|homes))/(?!(img/|a/|album/))",
        r"jpe?g\d?\.(church|fish|fishing|pet|su|cr|homes)/(a|album)/[\w.\-]+",
    ),
    ("kemono", "direct link", r".{2,6}\.kemono\.(cr|party|su)/data/", None),
    ("postimg.cc", "image", r"i\.?(postimg|pixxxels)\.cc/(.{8})", None),
    (
        "ibb.co",
        "image",
        r"([a-z]\d*\.)?ibb\.co/[\w.\-]{7}",
        r"ibb\.co/album/[\w.\-]+",
    ),
    ("imagevenue.com", "image", r"imagevenue\.com/(.{8})", None),
    ("img.kiwi", "image", r"img\.kiwi/images/", r"img\.kiwi/album/"),
    ("imgbox.com", "image", r"(thumbs|images)(\d+)?\.imgbox\.com/", r"imgbox\.com/g/"),
    ("imgur.com", "image", r"\w+\.imgur\.(com|io)", None),
    ("onlyfans.com", "image", r"public\.onlyfans\.com/files", None),
    ("reddit.com", "image", r"(\w+)?\.redd\.it", None),
    ("twitter.com", "image", r"([\w.\-]+)?twimg\.com/", None),
    ("pixl.li", "image", r"([a-z]\d*\.)pixl\.(li|is)/((img|image)/)?", r"pixl\.(li|is)/album/"),
    ("pixhost.to", "image", r"(t|img)\d*\.pixhost\.to/", r"pixhost\.to/gallery/"),
    ("imagebam.com", "image", r"imagebam\.com/(view|gallery)", None),
    ("saint.to", "video", r"(saint\d?\.(to|su|pk)/embed/|([\w.\-]+\.)?saint\d?\.(to|su|pk)/videos)", None),
    ("redgifs.com", "video", r"redgifs\.com/(ifr|watch)/[\w\-]+", None),
    (
        "bunkr",
        "",
        r"((stream|cdn\d*|i\d*|i-pizza|big-taco-1img)\.)?bunkr+(\.[a-z]{2,6})/(v/|f/|d/)?[\w.\-]+",
        r"bunkr+(\.[a-z]{2,6})/a/[\w\-]+",
    ),
    ("pixeldrain", "", r"(focus\.)?(pixeldrain\.(com|net)|pixeldra\.in)/[lu]/", None),
    ("gofile.io", "", r"gofile\.io/d/", None),
    ("erome.com", "", r"erome\.com/a/", None),
    ("cyberfile.me", "", r"cyberfile\.(su|me)/\w+/?", r"cyberfile\.(su|me)/folder/"),
    (
        "cyberdrop",
        "",
        r"(fs-\d+\.cyberdrop\.[a-z]{2,}/|cyberdrop\.[a-z]{2,}/(f|e)/)",
        r"cyberdrop\.[a-z]{2,}/a/",
    ),
    # filester.me — SimpCity file-host folders (e.g. Ikumi VODs / patreon audios)
    (
        "filester",
        "Files",
        r"filester\.(me|sh|si|gg)/d/[\w\-]+",
        r"filester\.(me|sh|si|gg)/f/[\w\-]+",
    ),
    # turbo.cr — SimpCity video host; real mp4 comes from /api/sign (see _resolve_turbo)
    (
        "turbo.cr",
        "Video",
        r"([\w\-]+\.)?turbo\.cr/(embed|v|d)/[\w\-]+",
        r"([\w\-]+\.)?turbo\.cr/a/[\w\-]+",
    ),
    # goonbox.cr — SimpCity image/album host (see _resolve_goodbox)
    ("goonbox.cr", "Images", r"goonbox\.cr/img/[\w\-]+", r"goonbox\.cr/a/[\w\-]+"),
    # box.com / boxcloud.com — cloud share links (see _resolve_box)
    ("box.com", "Files", None, r"box\.(com|cloud\.com)/s/[\w\-]+"),
    # pomf2.lain.la — pomf clone direct file host (see _resolve_pomf2)
    ("pomf2.lain.la", "Files", None, r"pomf2?\.lain\.la/(?:f|data|files)/[\w\-]+"),
    # SimpCity CDN mirrors — direct links already caught by the fallback, listed
    # here so they show up as a known host
    (
        "cuckcapital.cr",
        "Images",
        r"(?:simp\d+\.cuckcapital\.cr|(?:[\w-]+\.)?cdn\.cr|cuckcapital\.cr)/",
        None,
    ),
    # saint2 mirror
    ("saint2.cr", "Video", r"saint2?\.(to|su|pk|cr)/(?:embed|videos)/", None),
    # adult video hosts — direct links fall through to _resolve_direct
    ("pornhub.com", "Video", r"pornhub\.com/(?:view_video|embed|video)", None),
    ("spankbang.com", "Video", r"spankbang\.com/(?:video|embed)/", None),
    ("xvideos.com", "Video", r"xvideos\.com/(?:video|embed|#)", None),
    ("rule34.xxx", "Image", r"rule34\.xxx/index\.php\?page=post&s=view", None),
    ("noodlemagazine.com", "Video", r"noodlemagazine\.com/video/", None),
    ("pvvstream.pro", "Video", r"pvvstream\.pro/", None),
    # twitter/tumblr/reddit image mirrors
    ("pbs.twimg.com", "Image", r"pbs\.twimg\.com/", None),
    ("media.tumblr.com", "Image", r"media\.tumblr\.com/", None),
    ("nhentai-proxy", "Image", r"nhentai-proxy\.(herokuapp|net)/", None),
    ("imgvb.com", "Image", r"imgvb\.com/", None),
    ("jpeg.pet", "Image", r"jpe?g\d?\.(pet|church|fish|fishing|su|homes)/", None),
    # githubusercontent (e.g. banners/attachments referenced directly)
    ("githubusercontent.com", "Files", r"githubusercontent\.com/", None),
]

# Direct-link fallback: any absolute URL ending in a known media extension.
# NOTE: .webp / .avif intentionally excluded (user wants traditional formats only).
# A trailing lookahead on a URL terminator (quote / < / > / space / ? / end) stops
# ".avif" from partially matching the ".avi" alternative, while still allowing URLs
# embedded in HTML attributes like <img src="...">.
DIRECT_LINK_PATTERN = (
    r"https?://[^\s\"'<>]+?\."
    r"(?:jpg|jpeg|png|gif|bmp|mp4|webm|mkv|mov|m4v|avi|mp3|wav|ogg|m4a|flac|aac|opus|zip|rar|7z)"
    r"(?=[\"'<>?\s]|$)"
)


def download_root(custom_path: str | None = None) -> Path:
    """Return the root download directory, mirroring BunkrDownloader layout."""
    base = Path(custom_path) if custom_path else Path.cwd()
    return base / DOWNLOAD_FOLDER
