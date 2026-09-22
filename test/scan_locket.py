import argparse
import os
import re
import sys
import time
from typing import Optional, Set, Tuple

# Dam bao terminal Windows in tieng Viet khong bi loi cp1252 / UnicodeEncodeError
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

try:
    import requests
except ModuleNotFoundError as exc:
    requests = None
    REQUESTS_IMPORT_ERROR = exc
else:
    REQUESTS_IMPORT_ERROR = None

try:
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By
except ModuleNotFoundError as exc:
    uc = None
    By = None
    BROWSER_IMPORT_ERROR = exc
else:
    BROWSER_IMPORT_ERROR = None

try:
    # pyrefly: ignore [missing-import]
    from apify_client import ApifyClient
except ModuleNotFoundError:
    ApifyClient = None

# Apify API Token dự phòng
DEFAULT_APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "apify_api_mctAGlyMeyCpqOcshfAfCkM5xX2bs10hKFRh")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.tiktok.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
}

BANNER_WIDTH = 65
DEFAULT_MAX_SCROLLS = 70
DEFAULT_INITIAL_WAIT = 5.0
DEFAULT_REPLY_WAIT = 0.7
DEFAULT_SCROLL_WAIT = 1.0
STALE_SCROLL_LIMIT = 5
SCROLL_STEP = 1800

TIKTOK_VIDEO_ID_RE = re.compile(r"/video/(\d+)")
LONG_ID_RE = re.compile(r"(\d{15,22})")
LOCKET_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:locket\.cam/|locket\.camera/invites/)(?!links/)([A-Za-z0-9_.-]{3,30})",
    re.IGNORECASE,
)
LOCKET_DYNAMIC_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:locket\.camera|locket\.cam)/links/([A-Za-z0-9_-]{5,50})",
    re.IGNORECASE,
)
PREFIX_USERNAME_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])"
    r"(?:locket\s*của\s*mình|id\s*locket|add\s*locket|acc\s*locket|locketcam|locket|nick|acc|id|lk)"
    r"(?=\s|[:=\-@])\s*[:=\-]?\s*@?([A-Za-z0-9_.-]{3,30})(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)
LEADING_MARK_RE = re.compile(r"^[.@]+")
DATEISH_RE = re.compile(r"\d{1,4}[-/_]\d{1,2}([-/_]\d{1,4})?")
SPECIAL_ONLY_RE = re.compile(r"[^a-zA-Z0-9]+")
VALID_USERNAME_RE = re.compile(r"[a-zA-Z0-9_.-]+")

STATIC_EXTENSIONS = (".jpg", ".png", ".svg", ".css", ".js", ".html", ".php")
IGNORED_USERNAMES = {
    "locket",
    "cam",
    "camera",
    "link",
    "tiktok",
    "video",
    "follow",
    "share",
    "reply",
    "comment",
    "trending",
    "foryou",
    "fyp",
    "xyzbca",
    "explore",
    "music",
    "null",
    "undefined",
    "true",
    "false",
    "http",
    "https",
    "www",
    "com",
    "vn",
    "net",
    "org",
    "app",
    "view",
    "replies",
    "more",
    "user",
    "profile",
}

COMMENT_COUNT_SCRIPT = """
    const directCount = document.querySelector('[data-e2e="comment-count"]') ||
        document.querySelector('strong[data-e2e="comment-count"]') ||
        document.querySelector('[class*="CommentCount"]');

    if (directCount) {
        return (directCount.innerText || "").trim();
    }

    const headings = document.querySelectorAll('h4, p, span, strong, button');
    for (const heading of headings) {
        const text = (heading.innerText || "").trim();
        const match = text.match(/(?:comments?|bình\\s*luận)\\s*\\(?([0-9.,KMBkmb]+)\\)?/i) ||
            text.match(/([0-9.,KMBkmb]+)\\s*(?:comments?|bình\\s*luận)/i);
        if (match) {
            return match[1].trim();
        }
    }

    return "";
"""

CLICK_REPLIES_SCRIPT = """
    const replyTextRe = /^(view\\s*\\d*\\s*repl(?:y|ies)|xem\\s*\\d*\\s*câu\\s*trả\\s*lời|view replies|view earlier replies)$/i;
    const candidates = new Set();

    document
        .querySelectorAll('[data-e2e="comment-reply-expand"], [data-e2e="view-more-replies"], [class*="ReplyAction"], button, [role="button"]')
        .forEach((el) => {
            const t = (el.innerText || "").trim();
            if (replyTextRe.test(t)) candidates.add(el);
        });

    let clicked = 0;
    for (const el of candidates) {
        try {
            if (el.offsetParent !== null) {
                el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
                el.click();
                clicked++;
            }
        } catch (e) {}
    }

    return clicked;
"""

SCROLL_COMMENTS_SCRIPT = """
    window.scrollBy(0, arguments[0]);

    const panel = document.querySelector('[class*="DivCommentListContainer"]') ||
        document.querySelector('[data-e2e="comment-list"]');
    let panelHeight = 0;
    let panelScrollTop = 0;

    if (panel) {
        panel.scrollTop += arguments[0];
        panelHeight = panel.scrollHeight;
        panelScrollTop = panel.scrollTop + panel.clientHeight;
    }

    let items = document.querySelectorAll(
        '[data-e2e="comment-level-1"], [data-e2e="comment-level-2"], [class*="DivCommentItemContainer"], [class*="CommentItemWrapper"]'
    );
    if (!items || items.length === 0) {
        items = document.querySelectorAll(
            'div[data-pressable-container="true"], article, div[data-testid*="post"]'
        );
    }

    const bodyHeight = document.documentElement.scrollHeight || document.body.scrollHeight;
    const windowBottom = window.innerHeight + window.scrollY;
    const texts = Array.from(document.querySelectorAll('p, span, div, h2, h3')).slice(-25);
    
    const endOfComments = texts.some((el) => {
        const text = (el.innerText || "").trim().toLowerCase();
        return text.includes('no more comments') ||
            text.includes('không còn bình luận nào') ||
            text.includes('hết bình luận');
    });

    const blocked = texts.some((el) => {
        const text = (el.innerText || "").trim().toLowerCase();
        return text.includes("this content isn't available to everyone") ||
            text.includes("content isn't available") ||
            text.includes("nội dung này không hiển thị với tất cả mọi người") ||
            text.includes("it can't be seen by certain audiences");
    });

    return {
        commentCount: items.length,
        panelHeight,
        panelAtBottom: panel ? (panelScrollTop >= panelHeight - 50) : false,
        windowAtBottom: windowBottom >= bodyHeight - 100,
        bodyHeight,
        endOfComments,
        blocked
    };
"""

COLLECT_COMMENTS_SCRIPT = """
    let items = document.querySelectorAll('[class*="CommentItemWrapper"]');
    if (items.length === 0) {
        items = document.querySelectorAll('[data-e2e="comment-level-1"], [data-e2e="comment-level-2"]');
    }
    if (items.length === 0) {
        items = document.querySelectorAll('article, div[data-pressable-container="true"], div[data-testid*="post"]');
    }

    const seen = new Set();
    const comments = [];
    for (const item of items) {
        const text = (item.innerText || '').trim();
        const links = Array.from(item.querySelectorAll('a[href]')).map(a => a.href).filter(Boolean);
        const combined = [text, ...links].join(' ');
        if (!combined || seen.has(combined)) continue;
        seen.add(combined);
        comments.push({ index: comments.length + 1, text: combined });
    }
    return comments;
"""


def resolve_tiktok_url(url: str) -> str:
    """Tự động phân giải link rút gọn (vt.tiktok.com, vm.tiktok.com, tiktok.com/t/...) thành link video gốc."""
    clean_url = url.strip()
    if any(domain in clean_url.lower() for domain in ["vt.tiktok.com", "vm.tiktok.com", "/t/"]):
        if requests:
            try:
                resp = requests.get(clean_url, headers=DEFAULT_HEADERS, allow_redirects=True, stream=True, timeout=8)
                return resp.url
            except Exception:
                pass
    return clean_url


def extract_target_id(url: str) -> str:
    """Trích xuất ID từ link TikTok hoặc Threads."""
    if "threads." in url.lower():
        m = re.search(r'/(?:share|post)/([A-Za-z0-9_-]+)', url)
        if m:
            return f"threads_{m.group(1)}"
    match = TIKTOK_VIDEO_ID_RE.search(url) or LONG_ID_RE.search(url)
    return match.group(1) if match else "unknown"


def clean_username(username: str) -> Optional[str]:
    """Làm sạch và kiểm tra tính hợp lệ của username Locket."""
    if not username:
        return None

    username = LEADING_MARK_RE.sub("", username.strip().rstrip(".,:;!?)/\\\"'"))
    username_lower = username.lower()

    if len(username) < 3 or len(username) > 30:
        return None

    if username_lower.endswith(STATIC_EXTENSIONS):
        return None

    if username_lower in IGNORED_USERNAMES or username.startswith(".") or username.endswith("."):
        return None

    if DATEISH_RE.fullmatch(username):
        return None

    if username.isdigit() or SPECIAL_ONLY_RE.fullmatch(username):
        return None

    if not VALID_USERNAME_RE.fullmatch(username):
        return None

    return username


def normalize_locket_link(raw_username: str) -> Optional[str]:
    username = clean_username(raw_username)
    if not username:
        return None
    return f"https://locket.cam/{username}"


def normalize_dynamic_link(raw_token: str) -> Optional[str]:
    if not raw_token:
        return None
    token = raw_token.strip().rstrip(".,:;!?)/\\\"'")
    token_lower = token.lower()
    if len(token) < 5 or len(token) > 50:
        return None
    if token_lower in IGNORED_USERNAMES:
        return None
    return f"https://locket.camera/links/{token}"


def add_locket_link(locket_links: set, raw_username: str, source_label: str) -> bool:
    full_link = normalize_locket_link(raw_username)
    if not full_link or full_link in locket_links:
        return False

    locket_links.add(full_link)
    print(f" [+] [{source_label}] {full_link}")
    return True


def add_dynamic_link(locket_links: set, raw_token: str, source_label: str) -> bool:
    full_link = normalize_dynamic_link(raw_token)
    if not full_link or full_link in locket_links:
        return False

    locket_links.add(full_link)
    print(f" [+] [{source_label}] {full_link}")
    return True


def extract_locket_links_from_text(text: str, source_label: str, locket_links: set) -> int:
    found = 0

    for raw_username in LOCKET_URL_RE.findall(text):
        if add_locket_link(locket_links, raw_username, f"{source_label} Link"):
            found += 1

    for raw_token in LOCKET_DYNAMIC_URL_RE.findall(text):
        if add_dynamic_link(locket_links, raw_token, f"{source_label} Dynamic Link"):
            found += 1

    for raw_username in PREFIX_USERNAME_RE.findall(text):
        if add_locket_link(locket_links, raw_username, f"{source_label} Username"):
            found += 1

    return found


def write_links(output_file: str, locket_links: set) -> None:
    sorted_links = sorted(locket_links)
    with open(output_file, "w", encoding="utf-8") as file:
        file.write("\n".join(sorted_links))
        file.write("\n")


# ==============================================================================
# 1. PHƯƠNG ÁN TỐI ƯU: DIRECT INTERNAL API (SIÊU TỐC - 100% FREE - KHÔNG BROWSER)
# ==============================================================================
def scan_tiktok_comments_direct_api(
    tiktok_url: str,
    max_comments: int = 1000,
    expand_replies: bool = True,
    delay: float = 0.15,
    save_file: bool = False,
) -> Set[str]:
    """
    Quét trực tiếp qua REST API nội bộ của TikTok:
    - Miễn phí 100%, không cần Apify token hay browser.
    - Tốc độ siêu tốc (~2-4 giây cho 1,000 bình luận).
    - Hỗ trợ cả comment chính và replies lồng nhau.
    """
    if not requests:
        raise RuntimeError("Thư viện 'requests' chưa được cài đặt. Hãy chạy: pip install requests")

    print("\n" + "=" * BANNER_WIDTH)
    print("   QUÉT LINK LOCKET QUA DIRECT TIKTOK API (SIÊU TỐC - 100% FREE)")
    print("=" * BANNER_WIDTH)

    # Phân giải link rút gọn nếu có (vt.tiktok.com,...)
    resolved_url = resolve_tiktok_url(tiktok_url)
    target_id = extract_target_id(resolved_url)
    if not target_id or target_id == "unknown" or target_id.startswith("threads_"):
        m = LONG_ID_RE.search(resolved_url)
        if m:
            target_id = m.group(1)
        else:
            print(f"[-] Không thể trích xuất ID video TikTok từ link: {tiktok_url}")
            return set()

    print(f"[*] URL Video       : {resolved_url}")
    print(f"[*] Video ID        : {target_id}")
    print(f"[*] Giới hạn quét   : {max_comments if max_comments > 0 else 'Toàn bộ (Không giới hạn)'}")
    print(f"[*] Quét Replies    : {'BẬT (Quét cả câu trả lời)' if expand_replies else 'TẮT'}")
    print("-" * BANNER_WIDTH)

    start_time = time.time()
    locket_links = set()
    output_file = f"locket_scanned_{target_id}.txt"

    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)

    cursor = 0
    total_scanned = 0
    batch_index = 0

    print("[*] Đang kết nối TikTok Internal API...")

    while True:
        batch_index += 1
        count_to_fetch = 50
        if max_comments > 0:
            remaining = max_comments - total_scanned
            if remaining <= 0:
                break
            count_to_fetch = min(50, remaining)

        api_url = (
            f"https://www.tiktok.com/api/comment/list/"
            f"?aid=1988&aweme_id={target_id}&count={count_to_fetch}&cursor={cursor}"
        )

        try:
            resp = session.get(api_url, timeout=10)
            if resp.status_code != 200:
                print(f"[-] TikTok API trả về mã lỗi HTTP {resp.status_code}")
                break
            data = resp.json()
        except Exception as err:
            print(f"[-] Lỗi kết nối TikTok API: {err}")
            break

        status_code = data.get("status_code", 0)
        if status_code != 0 and status_code is not None:
            msg = data.get("status_msg") or f"Code {status_code}"
            print(f"[-] TikTok API thông báo: {msg}")
            break

        comments = data.get("comments") or []
        if not comments:
            break

        for c in comments:
            total_scanned += 1
            c_text = (c.get("text") or "").strip()
            cid = c.get("cid") or ""
            if c_text:
                extract_locket_links_from_text(c_text, f"Cmt #{cid}", locket_links)

            # Quét replies nếu được bật
            reply_count = int(c.get("reply_comment_total") or 0)
            if expand_replies and reply_count > 0:
                rep_cursor = 0
                while True:
                    rep_url = (
                        f"https://www.tiktok.com/api/comment/list/reply/"
                        f"?aid=1988&comment_id={cid}&item_id={target_id}&count=50&cursor={rep_cursor}"
                    )
                    try:
                        rep_resp = session.get(rep_url, timeout=8)
                        if rep_resp.status_code != 200:
                            break
                        rep_data = rep_resp.json()
                        rep_comments = rep_data.get("comments") or []
                        if not rep_comments:
                            break
                        for r in rep_comments:
                            total_scanned += 1
                            r_text = (r.get("text") or "").strip()
                            rcid = r.get("cid") or ""
                            if r_text:
                                extract_locket_links_from_text(r_text, f"Reply #{rcid}", locket_links)

                        if not rep_data.get("has_more"):
                            break
                        rep_cursor = rep_data.get("cursor", rep_cursor + len(rep_comments))
                        if delay > 0:
                            time.sleep(delay / 2)
                    except Exception:
                        break

        total_api_reported = data.get("total")
        total_str = f" / {total_api_reported}" if total_api_reported else ""
        print(f" -> Đang quét... {total_scanned}{total_str} bình luận | Tìm thấy: {len(locket_links)} link Locket")

        if not data.get("has_more"):
            print("[*] Đã tải hết danh sách bình luận (has_more = 0).")
            break

        cursor = data.get("cursor", cursor + len(comments))
        if delay > 0:
            time.sleep(delay)

    elapsed = time.time() - start_time
    print("\n" + "=" * BANNER_WIDTH)
    print(f"HOÀN TẤT TRONG {elapsed:.2f} GIÂY!")
    print(f"[*] Tổng số bình luận đã duyệt : {total_scanned}")
    print(f"[*] Tổng link Locket hợp lệ    : {len(locket_links)}")
    print("=" * BANNER_WIDTH)

    if locket_links:
        if save_file:
            write_links(output_file, locket_links)
            print(f"[OK] Đã cập nhật đầy đủ vào file: {os.path.abspath(output_file)}")
        else:
            # Tự động dọn dẹp file cũ nếu có
            if os.path.exists(output_file):
                try:
                    os.remove(output_file)
                except Exception:
                    pass
    else:
        print("[!] Không tìm thấy link Locket nào trong các bình luận đã duyệt.")

    return locket_links


# ==============================================================================
# 2. PHƯƠNG ÁN DỰ PHÒNG: APIFY CLOUD SCRAPER
# ==============================================================================
def scan_tiktok_comments_apify(
    tiktok_url: str,
    api_token: str = DEFAULT_APIFY_TOKEN,
    max_comments: int = 500,
    actor_id: str = "clockworks/tiktok-comments-scraper",
):
    """Sử dụng Apify Cloud Actor để cào comment TikTok (phương án dự phòng)."""
    if not ApifyClient:
        print("[-] Thư viện apify-client chưa được cài đặt. Hãy chạy: pip install apify-client")
        return

    client = ApifyClient(api_token)
    print("\n" + "=" * BANNER_WIDTH)
    print("   QUÉT LINK LOCKET QUA APIFY CLOUD SCRAPER")
    print("=" * BANNER_WIDTH)
    print(f"[*] URL Video       : {tiktok_url}")
    print(f"[*] Giới hạn cmt    : {max_comments if max_comments > 0 else 'Toàn bộ'}")
    print("-" * BANNER_WIDTH)

    locket_links = set()
    output_file = f"locket_scanned_{extract_target_id(tiktok_url)}.txt"

    clean_url = tiktok_url.strip()
    run_input = {
        "postURLs": [clean_url],
        "commentsPerPost": max_comments if max_comments > 0 else 500,
        "maxRepliesPerComment": 50,
    }

    try:
        print("[*] Đang gửi yêu cầu lên Apify Cloud...")
        run = client.actor(actor_id).call(run_input=run_input)
        if not run:
            print("[-] Không nhận được kết quả từ Apify.")
            return

        dataset_id = run.get("defaultDatasetId")
        dataset_items = client.dataset(dataset_id).list_items().items
        print(f"[*] Lấy về thành công {len(dataset_items)} dòng dữ liệu từ Apify.")

        for item in dataset_items:
            text = (item.get("text") or item.get("comment") or item.get("comment_text") or "").strip()
            if not text:
                continue
            comment_id = item.get("id") or item.get("cid") or item.get("index") or ""
            extract_locket_links_from_text(text, f"Apify #{comment_id}", locket_links)

        print("\n" + "=" * BANNER_WIDTH)
        print(f"HOÀN TẤT! Đã quét được: {len(locket_links)} link Locket.")
        print("=" * BANNER_WIDTH)

        if locket_links:
            write_links(output_file, locket_links)
            print(f"[OK] Đã cập nhật vào file: {os.path.abspath(output_file)}")
    except Exception as exc:
        print(f"[-] Có lỗi khi gọi Apify: {exc}")


# ==============================================================================
# 3. PHƯƠNG ÁN DỰ PHÒNG: SELENIUM CHROME HEADLESS (DÙNG CHO THREADS HOẶC KHI CẦN)
# ==============================================================================
def build_chrome_options(user_data_dir: Optional[str] = None, headless: bool = True):
    if not uc:
        raise RuntimeError("undetected_chromedriver chưa được cài đặt!")
    options = uc.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
    else:
        options.add_argument("--start-maximized")
    options.add_argument("--log-level=3")
    if user_data_dir:
        options.add_argument(f"--user-data-dir={user_data_dir}")
    return options


def click_comments_tab(driver) -> bool:
    try:
        tabs = driver.find_elements(By.XPATH, "//*[contains(text(), 'Comments') or contains(text(), 'Bình luận')]")
        for tab in tabs:
            if tab.is_displayed():
                tab.click()
                return True
    except Exception:
        return False
    return False


def read_total_comment_count(driver) -> str:
    try:
        return (driver.execute_script(COMMENT_COUNT_SCRIPT) or "").strip()
    except Exception:
        return ""


def expand_visible_replies(driver) -> int:
    try:
        return int(driver.execute_script(CLICK_REPLIES_SCRIPT) or 0)
    except Exception:
        return 0


def scroll_comments(driver) -> dict:
    try:
        return driver.execute_script(SCROLL_COMMENTS_SCRIPT, SCROLL_STEP) or {}
    except Exception:
        return {}


def collect_comments(driver) -> list:
    try:
        return driver.execute_script(COLLECT_COMMENTS_SCRIPT) or []
    except Exception:
        return []


def scan_tiktok_comments(
    target_url: str,
    max_scrolls: int = DEFAULT_MAX_SCROLLS,
    expand_replies: bool = True,
    initial_wait: float = DEFAULT_INITIAL_WAIT,
    reply_wait: float = DEFAULT_REPLY_WAIT,
    scroll_wait: float = DEFAULT_SCROLL_WAIT,
    headless: bool = True,
):
    """Phương án quét bằng trình duyệt ngầm Chrome (thích hợp cho Threads hoặc khi web bị chặn API)."""
    if not uc:
        print("[-] undetected_chromedriver chưa được cài đặt. Hãy chạy: pip install undetected-chromedriver")
        return

    print("\n" + "=" * BANNER_WIDTH)
    print("   TOOL QUÉT LINK LOCKET QUA CHROME BROWSER (TIKTOK / THREADS)")
    print("=" * BANNER_WIDTH)
    print(f"[*] URL             : {target_url}")
    print(f"[*] Số lần cuộn     : {max_scrolls} lần")
    print(f"[*] Chế độ chạy     : {'Chạy ngầm (Headless)' if headless else 'Mở cửa sổ Chrome'}")
    print("-" * BANNER_WIDTH)

    driver = None
    locket_links = set()
    output_file = f"locket_scanned_{extract_target_id(target_url)}.txt"

    try:
        print("[*] Đang khởi động trình duyệt ngầm...")
        driver = uc.Chrome(options=build_chrome_options(headless=headless), headless=headless)
        driver.get(target_url)

        print(f"[!] Chờ trang tải trong {initial_wait:g} giây...")
        time.sleep(initial_wait)

        if click_comments_tab(driver):
            time.sleep(1.5)

        total_comment_info = read_total_comment_count(driver)
        if total_comment_info:
            print(f"[*] Nhận diện có khoảng: {total_comment_info} bình luận!")

        no_new_content_count = 0
        last_comment_count = 0
        last_scroll_height = 0

        for scroll_index in range(1, max_scrolls + 1):
            clicked_count = expand_visible_replies(driver) if expand_replies else 0
            if clicked_count:
                time.sleep(reply_wait)

            scroll_info = scroll_comments(driver)
            time.sleep(scroll_wait)

            current_comments = int(scroll_info.get("commentCount") or 0)
            current_height = int(scroll_info.get("panelHeight") or scroll_info.get("bodyHeight") or 0)
            end_detected = bool(scroll_info.get("endOfComments"))
            is_blocked = bool(scroll_info.get("blocked"))

            if is_blocked:
                print("\n[!] ⚠️ Nội dung bị giới hạn hoặc yêu cầu đăng nhập!")
                break

            if end_detected:
                print(f" [*] Đã tải hết bình luận ({current_comments} bình luận trong DOM).")
                break

            unchanged = current_comments > 0 and current_comments == last_comment_count and current_height == last_scroll_height
            if unchanged and clicked_count == 0:
                no_new_content_count += 1
                if no_new_content_count >= STALE_SCROLL_LIMIT:
                    print(f" [*] Đã tải xong toàn bộ ({current_comments} bình luận trong DOM).")
                    break
            else:
                no_new_content_count = 0

            last_comment_count = current_comments
            last_scroll_height = current_height

            if scroll_index % 5 == 0 or scroll_index == max_scrolls:
                print(f" -> Đang tải... {current_comments} bình luận (lần cuộn: {scroll_index}/{max_scrolls})")

        print("\n[*] Đang bóc tách dữ liệu bình luận từ trang...")
        all_comments_data = collect_comments(driver)
        for comment in all_comments_data:
            comment_text = (comment.get("text") or "").strip()
            if not comment_text:
                continue
            comment_index = comment.get("index") or 0
            extract_locket_links_from_text(comment_text, f"Cmt #{comment_index}", locket_links)

        page_source = driver.page_source
        for raw_username in LOCKET_URL_RE.findall(page_source):
            add_locket_link(locket_links, raw_username, "Mã nguồn Link")
        for raw_token in LOCKET_DYNAMIC_URL_RE.findall(page_source):
            add_dynamic_link(locket_links, raw_token, "Mã nguồn Dynamic Link")

        print("\n" + "=" * BANNER_WIDTH)
        print(f"HOÀN TẤT! Đã quét được tổng cộng: {len(locket_links)} link Locket.")
        print("=" * BANNER_WIDTH)

        if locket_links:
            write_links(output_file, locket_links)
            print(f"[OK] Đã cập nhật đầy đủ vào file: {os.path.abspath(output_file)}")
        else:
            print("[!] Không tìm thấy link Locket nào.")

    except Exception as exc:
        print(f"[-] Có lỗi xảy ra: {exc}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


# ==============================================================================
# HÀM TIỆN ÍCH DÀNH CHO BÊN NGOÀI (BOT TELEGRAM / SCRIPT KHÁC)
# ==============================================================================
def scrape_comments_auto(target_url: str, max_comments: int = 1000, save_file: bool = False) -> Tuple[list, Optional[str]]:
    """
    Hàm gọi tự động, tối ưu hóa theo nền tảng:
    - Nếu là TikTok: Dùng Direct API (Siêu tốc, 100% Free).
    - Nếu là Threads: Dùng Selenium Chrome Headless.
    - save_file: Mặc định False để không tạo file rác locket_scanned_*.txt trên máy.
    """
    clean_url = target_url.strip()
    is_threads = "threads." in clean_url.lower()

    if not is_threads:
        # TikTok: Gọi Direct API
        try:
            links = scan_tiktok_comments_direct_api(
                clean_url,
                max_comments=max_comments,
                expand_replies=True,
                save_file=save_file
            )
            return sorted(list(links)), None
        except Exception as e:
            return None, f"Lỗi quét TikTok qua API: {str(e)}"
    else:
        # Threads: Dùng Chrome headless
        if not uc:
            return None, "undetected_chromedriver chưa cài đặt để quét Threads!"
        driver = None
        locket_links = set()
        try:
            options = build_chrome_options(headless=True)
            driver = uc.Chrome(options=options, headless=True)
            driver.get(clean_url)
            time.sleep(4)

            for _ in range(12):
                expand_visible_replies(driver)
                scroll_comments(driver)
                time.sleep(1)

            comments_data = collect_comments(driver)
            for comment in comments_data:
                c_text = (comment.get("text") or "").strip()
                if c_text:
                    extract_locket_links_from_text(c_text, "Threads", locket_links)

            page_source = driver.page_source
            for raw_u in LOCKET_URL_RE.findall(page_source):
                add_locket_link(locket_links, raw_u, "Mã nguồn Threads")
            for raw_token in LOCKET_DYNAMIC_URL_RE.findall(page_source):
                add_dynamic_link(locket_links, raw_token, "Mã nguồn Dynamic Link Threads")

            return sorted(list(locket_links)), None
        except Exception as e:
            return None, f"Lỗi cào Threads: {str(e)}"
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Tool quét link Locket từ bình luận TikTok / Threads (Mặc định: Direct API Siêu Tốc).")
    parser.add_argument("url", nargs="?", help="Đường link video TikTok hoặc bài viết Threads cần quét.")
    parser.add_argument("--max-comments", type=int, default=1000, help="Số lượng comment tối đa muốn quét (mặc định: 1000, 0 = không giới hạn).")
    parser.add_argument("--no-replies", action="store_true", help="Không quét câu trả lời (replies).")
    parser.add_argument("--save", action="store_true", help="Lưu kết quả ra file locket_scanned_*.txt (mặc định tự xóa/không lưu).")

    # Các chế độ phụ / dự phòng
    parser.add_argument("--apify", action="store_true", help="Dùng Apify Cloud thay vì Direct API.")
    parser.add_argument("--apify-token", default=DEFAULT_APIFY_TOKEN, help="Token Apify (nếu dùng --apify).")
    parser.add_argument("--chrome", action="store_true", help="Dùng trình duyệt Chrome trên máy thay vì Direct API.")
    parser.add_argument("--head", action="store_true", help="Mở cửa sổ Chrome hiển thị (khi dùng --chrome).")
    parser.add_argument("--max-scrolls", type=int, default=DEFAULT_MAX_SCROLLS, help="Số lần cuộn tối đa (khi dùng --chrome).")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    target_url = args.url.strip() if args.url else input("Nhập đường link video TikTok / Threads cần quét: ").strip()

    if not target_url:
        print("[-] Vui lòng nhập link video TikTok hoặc bài viết Threads hợp lệ!")
        return 1

    is_threads = "threads." in target_url.lower()

    # 1. Nếu là link Threads hoặc người dùng yêu cầu dùng Chrome
    if is_threads or args.chrome:
        scan_tiktok_comments(
            target_url,
            max_scrolls=args.max_scrolls,
            expand_replies=not args.no_replies,
            headless=not args.head,
        )
        return 0

    # 2. Nếu người dùng chỉ định dùng Apify Cloud
    if args.apify:
        scan_tiktok_comments_apify(
            tiktok_url=target_url,
            api_token=args.apify_token,
            max_comments=args.max_comments,
        )
        return 0

    # 3. MẶC ĐỊNH (TỐI ƯU NHẤT): Direct Internal API (Mặc định không lưu file rác nếu không có cờ --save)
    scan_tiktok_comments_direct_api(
        tiktok_url=target_url,
        max_comments=args.max_comments,
        expand_replies=not args.no_replies,
        save_file=args.save,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
