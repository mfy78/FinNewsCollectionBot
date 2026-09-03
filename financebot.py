# 福生无量天尊
from openai import OpenAI
import bleach
import feedparser
import markdown
from newspaper import Article
from datetime import datetime
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path
import smtplib
import ssl
import time
import pytz
import os

SHANGHAI_TZ = pytz.timezone("Asia/Shanghai")
REPORTS_DIR = Path(__file__).resolve().parent / "reports"

# RSS源地址列表
rss_feeds = {
    "💲 华尔街见闻":{
        "华尔街见闻":"https://dedicated.wallstreetcn.com/rss.xml",      
    },
    "💻 36氪":{
        "36氪":"https://36kr.com/feed",   
        },
    "🇨🇳 中国经济": {
        "香港經濟日報":"https://www.hket.com/rss/china",
        "东方财富":"http://rss.eastmoney.com/rss_partener.xml",
        "百度股票焦点":"http://news.baidu.com/n?cmd=1&class=stock&tn=rss&sub=0",
        "中新网":"https://www.chinanews.com.cn/rss/finance.xml",
        "国家统计局-最新发布":"https://www.stats.gov.cn/sj/zxfb/rss.xml",
    },
      "🇺🇸 美国经济": {
        "华尔街日报 - 经济":"https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness",
        "华尔街日报 - 市场":"https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
        "MarketWatch美股": "https://www.marketwatch.com/rss/topstories",
        "ZeroHedge华尔街新闻": "https://feeds.feedburner.com/zerohedge/feed",
        "ETF Trends": "https://www.etftrends.com/feed/",
    },
    "🌍 世界经济": {
        "华尔街日报 - 经济":"https://feeds.content.dowjones.io/public/rss/socialeconomyfeed",
        "BBC全球经济": "http://feeds.bbci.co.uk/news/business/rss.xml",
    },
}

# 获取北京时间
def now_in_shanghai():
    return datetime.now(SHANGHAI_TZ)


def required_env(name):
    value = os.getenv(name)
    if not value:
        raise ValueError(f"环境变量 {name} 未设置")
    return value

# 爬取网页正文 (用于 AI 分析，但不展示)
def fetch_article_text(url):
    try:
        print(f"📰 正在爬取文章内容: {url}")
        article = Article(url)
        article.download()
        article.parse()
        text = article.text[:1500]  # 限制长度，防止超出 API 输入限制
        if not text:
            print(f"⚠️ 文章内容为空: {url}")
        return text
    except Exception as e:
        print(f"❌ 文章爬取失败: {url}，错误: {e}")
        return "（未能获取文章正文）"

# 添加 User-Agent 头
def fetch_feed_with_headers(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    return feedparser.parse(url, request_headers=headers)


# 自动重试获取 RSS
def fetch_feed_with_retry(url, retries=3, delay=5):
    for i in range(retries):
        try:
            feed = fetch_feed_with_headers(url)
            if feed and hasattr(feed, 'entries') and len(feed.entries) > 0:
                return feed
        except Exception as e:
            print(f"⚠️ 第 {i+1} 次请求 {url} 失败: {e}")
            time.sleep(delay)
    print(f"❌ 跳过 {url}, 尝试 {retries} 次后仍失败。")
    return None

# 获取RSS内容（爬取正文但不展示）
def fetch_rss_articles(rss_feeds, max_articles=10):
    news_data = {}
    analysis_text = ""  # 用于AI分析的正文内容

    for category, sources in rss_feeds.items():
        category_content = ""
        for source, url in sources.items():
            print(f"📡 正在获取 {source} 的 RSS 源: {url}")
            feed = fetch_feed_with_retry(url)
            if not feed:
                print(f"⚠️ 无法获取 {source} 的 RSS 数据")
                continue
            print(f"✅ {source} RSS 获取成功，共 {len(feed.entries)} 条新闻")

            articles = []  # 每个source都需要重新初始化列表
            for entry in feed.entries[:max_articles]:
                title = entry.get('title', '无标题')
                link = entry.get('link', '') or entry.get('guid', '')
                if not link:
                    print(f"⚠️ {source} 的新闻 '{title}' 没有链接，跳过")
                    continue

                # 爬取正文用于分析（不展示）
                article_text = fetch_article_text(link)
                analysis_text += f"【{title}】\n{article_text}\n\n"

                print(f"🔹 {source} - {title} 获取成功")
                articles.append(f"- [{title}]({link})")

            if articles:
                category_content += f"### {source}\n" + "\n".join(articles) + "\n\n"

        news_data[category] = category_content

    return news_data, analysis_text

# AI 生成内容摘要（基于爬取的正文）
def summarize(client, text):
    if not text.strip():
        raise ValueError("未抓取到可用的新闻正文，无法生成摘要")

    completion = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": """
             你是一名专业的财经新闻分析师，请根据以下新闻内容，按照以下步骤完成任务：
             1. 提取新闻中涉及的主要行业和主题，找出近1天涨幅最高的3个行业或主题，以及近3天涨幅较高且此前2周表现平淡的3个行业/主题。（如新闻未提供具体涨幅，请结合描述和市场情绪推测热点）
             2. 针对每个热点，输出：
                - 催化剂：分析近期上涨的可能原因（政策、数据、事件、情绪等）。
                - 复盘：梳理过去3个月该行业/主题的核心逻辑、关键动态与阶段性走势。
                - 展望：判断该热点是短期炒作还是有持续行情潜力。
             3. 将以上分析整合为一篇1500字以内的财经热点摘要，逻辑清晰、重点突出，适合专业投资者阅读。
             """},
            {"role": "user", "content": text}
        ]
    )
    return completion.choices[0].message.content.strip()


def render_html(markdown_content, title):
    rendered = markdown.markdown(
        markdown_content,
        extensions=["extra", "sane_lists"],
        output_format="html5",
    )
    safe_html = bleach.clean(
        rendered,
        tags={
            "a", "blockquote", "br", "code", "em", "h1", "h2", "h3",
            "h4", "hr", "li", "ol", "p", "pre", "strong", "table",
            "tbody", "td", "th", "thead", "tr", "ul",
        },
        attributes={"a": ["href", "title"]},
        protocols={"http", "https", "mailto"},
        strip=True,
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ margin: 0; background: #f3f4f6; color: #1f2937; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.7; }}
    main {{ width: min(760px, calc(100% - 32px)); margin: 24px auto; padding: 28px; box-sizing: border-box; background: #ffffff; border-top: 4px solid #0f766e; }}
    h1 {{ margin: 0 0 20px; color: #111827; font-size: 26px; }}
    h2 {{ margin-top: 30px; padding-bottom: 8px; border-bottom: 1px solid #d1d5db; color: #0f766e; font-size: 20px; }}
    h3 {{ margin-top: 22px; color: #374151; font-size: 17px; }}
    a {{ color: #0369a1; text-decoration: none; }}
    li {{ margin: 8px 0; }}
    blockquote {{ margin: 16px 0; padding: 8px 16px; border-left: 3px solid #b45309; color: #4b5563; background: #fffbeb; }}
    pre, code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; background: #f3f4f6; }}
    pre {{ padding: 12px; overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 8px; border: 1px solid #d1d5db; text-align: left; }}
    @media (max-width: 600px) {{ main {{ width: 100%; margin: 0; padding: 20px 16px; }} h1 {{ font-size: 23px; }} }}
  </style>
</head>
<body>
  <main>
    {safe_html}
  </main>
</body>
</html>
"""


def save_report(markdown_content, html_content, run_time):
    report_dir = REPORTS_DIR / run_time.strftime("%Y") / run_time.strftime("%m")
    report_dir.mkdir(parents=True, exist_ok=True)
    base_name = run_time.strftime("%Y-%m-%d-%H%M%S")
    markdown_path = report_dir / f"{base_name}.md"
    html_path = report_dir / f"{base_name}.html"
    markdown_path.write_text(markdown_content, encoding="utf-8")
    html_path.write_text(html_content, encoding="utf-8")
    print(f"✅ Markdown 报告已保存: {markdown_path.relative_to(REPORTS_DIR.parent)}")
    print(f"✅ HTML 报告已保存: {html_path.relative_to(REPORTS_DIR.parent)}")
    return markdown_path, html_path


def parse_recipients(raw_recipients):
    """Parse, validate, and de-duplicate one or more recipient addresses."""
    normalized = raw_recipients.replace(";", ",").replace("\n", ",")
    parsed = getaddresses([normalized])
    recipients = []
    invalid = []
    for _, address in parsed:
        address = address.strip()
        if not address:
            continue
        if "@" not in address or address.startswith("@") or address.endswith("@"):
            invalid.append(address)
            continue
        if address not in recipients:
            recipients.append(address)

    if invalid:
        raise ValueError(
            "环境变量 MAIL_TO 中存在无效邮箱地址: " + ", ".join(invalid)
        )
    if not recipients:
        raise ValueError("环境变量 MAIL_TO 中没有有效的收件地址")
    return recipients


def send_email(subject, html_content):
    smtp_host = required_env("SMTP_HOST")
    smtp_security = (os.getenv("SMTP_SECURITY") or "ssl").strip().lower()
    default_port = 465 if smtp_security == "ssl" else 587
    smtp_port = int(os.getenv("SMTP_PORT") or str(default_port))
    smtp_username = required_env("SMTP_USERNAME")
    smtp_password = required_env("SMTP_PASSWORD")
    mail_from = (os.getenv("MAIL_FROM") or smtp_username).strip()
    recipients = parse_recipients(required_env("MAIL_TO"))

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = mail_from
    message["To"] = ", ".join(recipients)
    message.set_content(html_content, subtype="html", charset="utf-8")

    context = ssl.create_default_context()
    if smtp_security == "ssl":
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=30) as smtp:
            smtp.login(smtp_username, smtp_password)
            smtp.send_message(message)
    elif smtp_security == "starttls":
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(smtp_username, smtp_password)
            smtp.send_message(message)
    else:
        raise ValueError("SMTP_SECURITY 仅支持 ssl 或 starttls")

    print(f"✅ 邮件已发送至 {len(recipients)} 个收件地址")


if __name__ == "__main__":
    run_time = now_in_shanghai()
    report_time = run_time.strftime("%Y-%m-%d %H:%M")
    email_subject = f"财经新闻摘要 | {report_time}"
    openai_client = OpenAI(
        api_key=required_env("OPENAI_API_KEY"),
        base_url="https://api.deepseek.com/v1",
    )

    # 每个网站获取最多 5 篇文章
    articles_data, analysis_text = fetch_rss_articles(rss_feeds, max_articles=5)
    
    # AI生成摘要
    summary = summarize(openai_client, analysis_text)

    # 生成 Markdown 报告和可直接作为邮件正文的 HTML 页面
    final_summary = f"# {report_time} 财经新闻摘要\n\n## 今日分析总结\n\n{summary}\n\n---\n\n"
    for category, content in articles_data.items():
        if content.strip():
            final_summary += f"## {category}\n{content}\n\n"

    html_report = render_html(final_summary, email_subject)
    save_report(final_summary, html_report, run_time)
    send_email(email_subject, html_report)
