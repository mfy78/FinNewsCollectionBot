from calendar import timegm
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
from urllib.parse import urldefrag, urljoin

import requests
import feedparser
from pypdf import PdfReader


NEWS_WINDOW_HOURS = 24
REQUEST_TIMEOUT = 30
ARTICLE_TEXT_LIMIT = 6000
SENT_ARTICLES_PATH = Path(__file__).resolve().parent / "reports" / "sent_articles.json"
CNINFO_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_CATEGORIES = (
    "category_yjyg_szsh;category_yjkb_szsh;category_ndbg_szsh;"
    "category_bndbg_szsh;category_sjdbg_szsh;category_hg_szsh"
)
OFFICIAL_FEEDS = {
    "美联储货币政策": "https://www.federalreserve.gov/feeds/press_monetary.xml",
    "美联储官员讲话": "https://www.federalreserve.gov/feeds/speeches.xml",
    "欧洲央行新闻发布": "https://www.ecb.europa.eu/rss/press.html",
}
BLS_FEEDS = {
    "BLS 消费者价格指数": "https://www.bls.gov/feed/cpi.rss",
    "BLS 生产者价格指数": "https://www.bls.gov/feed/ppi.rss",
    "BLS 就业报告": "https://www.bls.gov/feed/empsit.rss",
    "BLS 职位空缺": "https://www.bls.gov/feed/jolts.rss",
}


def fetch_official_feed(url):
    response = requests.get(
        url, headers={"User-Agent": "Mozilla/5.0"}, timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    feed = feedparser.parse(response.content)
    if feed.bozo:
        raise ValueError(f"官方 RSS 内容无法解析: {url}") from feed.bozo_exception
    if not feed.version:
        raise ValueError(f"官方 RSS 返回了无法识别的内容: {url}")
    return feed


def load_sent_articles(path=SENT_ARTICLES_PATH):
    if not path.exists():
        return {}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state["version"] != 1:
        raise ValueError("发送记录版本无法识别")
    sent = state["sent"]
    if not isinstance(sent, dict):
        raise ValueError("发送记录 sent 必须为对象")
    return sent


def save_sent_articles(sent, links, run_time, path=SENT_ARTICLES_PATH):
    updated = {**sent, **dict.fromkeys(links, run_time.isoformat())}
    path.parent.mkdir(parents=True, exist_ok=True)
    pending_path = path.with_suffix(".pending")
    pending_path.write_text(
        json.dumps({"version": 1, "sent": updated}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    pending_path.replace(path)


def entry_published_at(entry):
    # feedparser 将 RSS 和 Atom 的日期转换为 UTC。
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    return datetime.fromtimestamp(timegm(parsed), tz=timezone.utc)


def select_recent_articles(entries, sent_links, run_time, max_articles):
    if max_articles < 1:
        raise ValueError("每个来源的文章数量必须大于零")
    cutoff = run_time - timedelta(hours=NEWS_WINDOW_HOURS)
    candidates = []
    for entry in entries:
        link = urldefrag(entry.get("link", ""))[0]
        published_at = entry["published_at"]
        if not link or link in sent_links or published_at is None:
            continue
        if cutoff <= published_at <= run_time:
            candidates.append({**entry, "link": link})
    candidates.sort(key=lambda entry: entry["published_at"], reverse=True)
    selected = []
    selected_links = set()
    for entry in candidates:
        if entry["link"] not in selected_links:
            selected.append(entry)
            selected_links.add(entry["link"])
        if len(selected) == max_articles:
            break
    return selected


def fetch_cninfo_announcements(run_time):
    start_date = run_time - timedelta(hours=NEWS_WINDOW_HOURS)
    response = requests.post(
        CNINFO_QUERY_URL,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.cninfo.com.cn/"},
        data={
            "pageNum": 1, "pageSize": 100, "column": "szse",
            "tabName": "fulltext", "plate": "", "stock": "",
            "searchkey": "", "secid": "", "category": CNINFO_CATEGORIES,
            "trade": "", "seDate": f"{start_date:%Y-%m-%d}~{run_time:%Y-%m-%d}",
            "sortName": "time", "sortType": "desc", "isHLtitle": "false",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    # 无结果时接口使用 null；缺少 announcements 字段则直接报错。
    announcements = data["announcements"]
    if announcements is None:
        announcements = []
    entries = []
    for announcement in announcements:
        if announcement["adjunctType"].upper() != "PDF":
            continue
        entries.append({
            "title": (
                f'{announcement["secCode"]} {announcement["secName"]} '
                f'{announcement["announcementTitle"]}'
            ),
            "link": urljoin("https://static.cninfo.com.cn/", announcement["adjunctUrl"]),
            "published_at": datetime.fromtimestamp(
                announcement["announcementTime"] / 1000, tz=timezone.utc,
            ),
        })
    return entries


def fetch_pdf_text(url):
    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    reader = PdfReader(BytesIO(response.content))
    parts = []
    length = 0
    for page in reader.pages:
        text = page.extract_text()
        parts.append(text)
        length += len(text)
        if length >= ARTICLE_TEXT_LIMIT:
            break
    text = "\n".join(parts).strip()[:ARTICLE_TEXT_LIMIT]
    if not text:
        raise ValueError(f"公告 PDF 没有可提取的文本: {url}")
    return text


def collect_source_articles(entries, source, sent_links, run_time, max_articles, fetch_text):
    articles = []
    analysis_parts = []
    for entry in select_recent_articles(entries, sent_links, run_time, max_articles):
        try:
            text = fetch_text(entry["link"])
        except Exception as error:
            print(f'⚠️ 跳过文章: {entry["link"]}，错误: {error}')
            continue
        if not text or text == "（未能获取文章正文）":
            print(f'⚠️ 跳过正文为空的文章: {entry["link"]}')
            continue
        published_at = entry["published_at"].astimezone(run_time.tzinfo)
        date_text = published_at.strftime("%Y-%m-%d %H:%M")
        articles.append(f'- [{entry["title"]}]({entry["link"]})（{date_text}）')
        analysis_parts.append(
            f'【{source} | {entry["title"]} | {date_text}】\n'
            f'来源：{entry["link"]}\n{text}\n\n'
        )
        sent_links.add(entry["link"])
    return articles, "".join(analysis_parts)
