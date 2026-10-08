"""Current, dated headlines from RSS; no LLM/API key needed for retrieval.

World news comes with a section of headlines from the user's own country
(default: 3 from Bulgaria). Configure in .env:
    JARVIS_NEWS_COUNTRY=Bulgaria     # country for the local section
    JARVIS_NEWS_LOCAL_COUNT=3        # 0 turns the local section off
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode, urlparse
import xml.etree.ElementTree as ET

import requests

from core import settings

_WORLD = ("world", "world news", "top world news today", "news", "latest news")

# Countries with a native-language Google News edition: (hl, gl, ceid, local name used for search).
_EDITIONS = {"bulgaria": ("bg", "BG", "BG:bg", "България")}
_COUNTRY_ALIASES = {"bulgaria": "bulgaria", "българия": "bulgaria", "bulgarian": "bulgaria", "bg": "bulgaria",
                    "bulgarian news": "bulgaria", "news from bulgaria": "bulgaria", "новини от българия": "bulgaria"}


def _clean_title(title, source):
    """Google News appends " - Publisher" to every title; the publisher is reported separately."""
    head, sep, tail = title.rpartition(" - ")
    if sep and tail and source and (source.casefold().startswith(tail.casefold())
                                    or tail.casefold().startswith(source.casefold())):
        return head.strip()
    return title


def parse_feed(data, count=5, now=None):
    now = now or datetime.now(timezone.utc)
    articles, seen = [], set()
    for item in ET.fromstring(data).findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError, OverflowError):
            continue
        if not now - timedelta(days=3) <= published <= now + timedelta(minutes=15):
            continue
        if not title or urlparse(link).scheme not in ("http", "https"):
            continue
        source = (item.findtext("source") or "").strip()
        if not source:
            source = "BBC News" if "bbc.co" in urlparse(link).netloc else urlparse(link).netloc
        title = _clean_title(title, source)
        key = title.casefold()
        if key in seen:
            continue
        seen.add(key)
        articles.append({"title": title, "url": link, "source": source,
                         "published": published.astimezone(timezone.utc).isoformat()})
    articles.sort(key=lambda row: row["published"], reverse=True)
    return articles[:count]


def _country(topic):
    return _COUNTRY_ALIASES.get(" ".join(str(topic or "").casefold().split()))


def _feed_urls(topic):
    country = _country(topic)
    if country in _EDITIONS:
        hl, gl, ceid, local_name = _EDITIONS[country]
        edition = {"hl": hl, "gl": gl, "ceid": ceid}
        return ["https://news.google.com/rss?" + urlencode(edition),                       # top stories there
                f"https://news.google.com/rss/headlines/section/geo/{country.title()}?" + urlencode(edition),
                "https://news.google.com/rss/search?" + urlencode({"q": f"{local_name} when:1d", **edition})]
    query = "" if topic.lower() in _WORLD else topic
    urls = ["https://news.google.com/rss" + ("/search" if query else "") + "?" + urlencode({
        **({"q": query + " when:2d"} if query else {}), "hl": "en-US", "gl": "US", "ceid": "US:en"})]
    if not query:
        urls.append("https://feeds.bbci.co.uk/news/world/rss.xml")
    elif query.lower() in ("technology", "tech", "technology news"):
        urls.append("https://feeds.bbci.co.uk/news/technology/rss.xml")
    return urls


def fetch_headlines(topic="world", count=5):
    topic = str(topic or "world").strip()[:200]
    count = max(1, min(10, int(count)))
    failures = []
    for feed_url in _feed_urls(topic):
        try:
            with requests.get(feed_url, timeout=(3, 5), stream=True,
                              headers={"User-Agent": "MarkL-SchoolAssistant/1.0"}) as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_content(16384):
                    data.extend(chunk)
                    if len(data) > 2_000_000:
                        raise ValueError("News feed too large")
            articles = parse_feed(bytes(data), count)
            if articles:
                return articles
        except (requests.RequestException, ET.ParseError, ValueError) as exc:
            failures.append(type(exc).__name__)
    raise RuntimeError("No recent headlines available" + (f" ({', '.join(failures)})" if failures else ""))


def local_news_settings():
    """(country name, how many local headlines to add to world news)."""
    country = settings.get("JARVIS_NEWS_COUNTRY", "Bulgaria")
    return country, max(0, min(10, settings.get_int("JARVIS_NEWS_LOCAL_COUNT", 3)))


def _try(fetch, *args):
    try:
        return fetch(*args), None
    except (RuntimeError, ValueError, TypeError) as exc:
        return [], exc


def news_report(parameters=None, player=None):
    parameters = parameters or {}
    topic = str(parameters.get("topic") or "world")
    count = parameters.get("count", 5)
    country, local_count = local_news_settings()
    if parameters.get("local_count") is not None:
        local_count = max(0, min(10, int(parameters["local_count"])))
    with_local = topic.strip().lower() in _WORLD and local_count > 0

    if with_local:
        # Fetch both at once; ask for a few extra local ones in case they repeat a world story.
        with ThreadPoolExecutor(max_workers=2) as pool:
            world_job = pool.submit(_try, fetch_headlines, topic, count)
            local_job = pool.submit(_try, fetch_headlines, country, min(10, local_count + 3))
            (world, world_error), (local, local_error) = world_job.result(), local_job.result()
        world_titles = {a["title"].casefold() for a in world}
        local = [a for a in local if a["title"].casefold() not in world_titles][:local_count]
        sections = [("WORLD", world), (country.upper(), local)]
        errors = [e for e in (world_error, local_error) if e]
    else:
        articles, error = _try(fetch_headlines, topic, count)
        sections, errors = [(topic.upper(), articles)], [error] if error else []

    if not any(articles for _, articles in sections):
        if player:
            player.write_log(f"[News] {'; '.join(str(e) for e in errors)}")
        return "No news found: current feeds are unavailable."

    title = f"world + {country}" if with_local else topic
    lines = [f"Latest headlines — {title}",
             "Retrieved " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), ""]
    number = 0
    for name, articles in sections:
        if len(sections) > 1:
            lines.append(f"{name} ({len(articles)})" if articles else f"{name}: headlines unavailable right now")
        for article in articles:
            number += 1
            lines.extend([f"{number}. {article['title']}",
                          f"Source: {article['source']} | Published: {article['published']}", article["url"], ""])
        if len(sections) > 1:
            lines.append("")
    return "\n".join(lines).strip()
