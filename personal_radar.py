"""Keep the personal RSS queue across Actions runs and deliver a single digest."""

import argparse
from datetime import datetime, timedelta, timezone
from html import escape
import json
import os
from pathlib import Path
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from pytz import timezone as get_timezone
import requests

from trendradar.core import load_config, load_frequency_words, matches_word_groups
from trendradar.crawler.rss.fetcher import RSSFeedConfig, RSSFetcher
from trendradar.utils.time import is_within_days


MAX_NEWS = 12
DEDUP_DAYS = 7
URGENT = "重大突发与公共安全"
RUMOUR = re.compile(r"^\s*(?:[【\[]\s*)?(?:网传|据传|传闻称|未经证实|unconfirmed\b|rumou?rs?\b)", re.I)
CLARIFICATION = re.compile(r"辟谣|澄清|否认|官方(?:核实|证实)|查无实据|\b(?:denie[ds]|denies|debunk\w*|fact.check\w*|clarif\w*|false claims?)\b", re.I)
TRACKING = {"fbclid", "gclid", "mc_cid", "mc_eid", "igshid"}


def url_key(url):
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None:
        return ""
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING
    ]
    return urlunsplit(("", parts.netloc.lower(), parts.path.rstrip("/") or "/", urlencode(sorted(query)), ""))


def dedup_keys(item):
    keys = {("url", url_key(item["url"]))}
    title = unicodedata.normalize("NFKC", item["title"]).casefold()
    title = "".join(character for character in title if character.isalnum())
    if len(title) >= 8 and item["source_id"] != "usgs":
        keys.add(("title", title))
    return keys


def classify(title, rules):
    if RUMOUR.search(title) and not CLARIFICATION.search(title):
        return None
    groups, filters, global_filters = rules
    if not matches_word_groups(title, groups, filters, global_filters):
        return None
    for group in groups:
        if matches_word_groups(title, [group], []):
            return group["display_name"]
    return None


def read_pending(state):
    path = state / "pending.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def write_pending(state, pending):
    state.mkdir(parents=True, exist_ok=True)
    path = state / "pending.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(pending, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def sent_entries(state, today, days=DEDUP_DAYS):
    for offset in range(days):
        path = state / "history" / f"{today - timedelta(days=offset)}.jsonl"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                yield json.loads(line)


def merge_pending(pending, incoming, sent):
    seen = {key for item in [*pending, *sent] for key in dedup_keys(item)}
    added = []
    for item in incoming:
        keys = dedup_keys(item)
        if keys.isdisjoint(seen):
            added.append(item)
            seen.update(keys)
    return [*pending, *added]


def collect(config, rules, today):
    rss = config["RSS"]
    if not rss["ENABLED"]:
        raise SystemExit("RSS 采集已关闭。")
    freshness = rss["FRESHNESS_FILTER"]
    feeds = [RSSFeedConfig(**entry) for entry in rss["FEEDS"]]
    fetcher = RSSFetcher(
        feeds=feeds,
        request_interval=rss["REQUEST_INTERVAL"],
        timeout=rss["TIMEOUT"],
        timezone=config["TIMEZONE"],
        freshness_enabled=freshness["ENABLED"],
        default_max_age_days=freshness["MAX_AGE_DAYS"],
    )
    try:
        data = fetcher.fetch_all()
    finally:
        fetcher.session.close()
    if len(data.failed_ids) == len(fetcher.feeds):
        raise SystemExit("所有 RSS 源均抓取失败，保留现有待发送队列。")

    items = []
    for feed in fetcher.feeds:
        for entry in data.items.get(feed.id, []):
            if not url_key(entry.url):
                continue
            if entry.published_at and freshness["ENABLED"]:
                max_age = feed.max_age_days if feed.max_age_days is not None else freshness["MAX_AGE_DAYS"]
                if max_age > 0 and not is_within_days(entry.published_at, max_age, config["TIMEZONE"]):
                    continue
            category = classify(entry.title, rules)
            if category:
                items.append({
                    "title": entry.title,
                    "url": entry.url,
                    "source_id": feed.id,
                    "source_name": feed.name,
                    "feed_time": entry.published_at or "",
                    "category": category,
                    "queued_on": today.isoformat(),
                })
    print(f"RSS 可用 {len(fetcher.feeds) - len(data.failed_ids)}/{len(fetcher.feeds)}，匹配 {len(items)} 条。")
    if data.failed_ids:
        print("RSS 失败来源：" + ", ".join(data.failed_ids))
    return items


def display_time(value, display_timezone):
    if not value:
        return ""
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        return ""
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(display_timezone).strftime("%m-%d %H:%M")


def create_digest(pending, config, groups):
    display_timezone = get_timezone(config["TIMEZONE"])
    source_order = {feed["id"]: index for index, feed in enumerate(config["RSS"]["FEEDS"])}
    category_order = {group["display_name"]: index for index, group in enumerate(groups)}
    ordered = sorted(pending, key=lambda item: (
        item["category"] != URGENT,
        item["queued_on"],
        category_order.get(item["category"], len(category_order)),
        source_order.get(item["source_id"], len(source_order)),
    ))

    header = "个人信息雷达 · 新增资讯"
    lines = [f"<b>{header}</b>"]
    # Telegram limits the text after decoding HTML. Count UTF-16 units conservatively.
    remaining = 3950 - len(header.encode("utf-16-le")) // 2
    selected = []
    for item in ordered[:MAX_NEWS]:
        title = item["title"]
        clock = display_time(item["feed_time"], display_timezone)
        detail = f"{item['category']} · {item['source_name']}"
        if clock:
            detail += f" · {clock}（源标记时间）"
        size = len(f"\n\n{title}\n{detail}".encode("utf-16-le")) // 2
        if size > remaining and selected:
            break
        if size > remaining:
            # Keep a usable link even if a publisher supplies a multi-page title.
            title = title.encode("utf-16-le")[: 2 * (remaining - 300)].decode("utf-16-le", errors="ignore") + "…"
            size = len(f"\n\n{title}\n{detail}".encode("utf-16-le")) // 2
        remaining -= size
        lines.append(f'<a href="{escape(item["url"], quote=True)}">{escape(title)}</a>\n{escape(detail)}')
        selected.append(item)

    deferred = len(ordered) - len(selected)
    if deferred:
        lines.append(f"另有 {deferred} 条留待后续推送。")
    return "\n\n".join(lines), selected


def telegram_call(token, method, data):
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=data,
            timeout=25,
            allow_redirects=False,
        )
        payload = response.json()
    except (requests.RequestException, ValueError):
        raise SystemExit(f"Telegram {method} 连接或响应异常。") from None
    if response.status_code != 200 or not isinstance(payload, dict) or not payload.get("ok"):
        raise SystemExit(f"Telegram {method} 失败（HTTP {response.status_code}）。检查 GitHub Secrets 和频道权限。")
    return payload["result"]


def send_message(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    is_channel_id = re.fullmatch(r"-1[0-9]{12}", chat_id) and (
        -1_997_852_516_352 <= int(chat_id) <= -1_000_000_000_001
    )
    if not token or not is_channel_id:
        raise SystemExit("请配置 Telegram Bot Token 和私人频道的数字 Chat ID。")
    chat = telegram_call(token, "getChat", {"chat_id": chat_id})
    if chat.get("type") != "channel" or chat.get("username") or chat.get("active_usernames"):
        raise SystemExit("目标必须为私人频道。")
    telegram_call(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    })


def deliver(state, config, rules, today):
    pending = read_pending(state)
    if not config["ENABLE_NOTIFICATION"] or not pending:
        print("推送已关闭或没有新增匹配内容。")
        return
    sent = {key for item in sent_entries(state, today) for key in dedup_keys(item)}
    pending = [item for item in pending if dedup_keys(item).isdisjoint(sent)]
    if not pending:
        write_pending(state, [])
        print("没有新增匹配内容。")
        return
    text, selected = create_digest(pending, config, rules[0])
    send_message(text)
    history = state / "history" / f"{today}.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    with history.open("a", encoding="utf-8") as file:
        for item in selected:
            file.write(json.dumps(item, ensure_ascii=False) + "\n")
    write_pending(state, [item for item in pending if item not in selected])
    print(f"Telegram 已确认接收 {len(selected)} 条。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["preview", "collect", "deliver", "test"])
    parser.add_argument("--state", type=Path, default=Path("radar-state"))
    args = parser.parse_args()
    config = load_config()
    rules = load_frequency_words()
    if not rules[0]:
        raise SystemExit("词组为空，已停止，避免推送整份 RSS。")
    if config["REPORT_MODE"] != "incremental" or config["FILTER"]["METHOD"] != "keyword":
        raise SystemExit("个人雷达需要 incremental 增量模式和 keyword 筛选。")
    today = datetime.now(get_timezone(config["TIMEZONE"])).date()

    if args.operation == "test":
        send_message("【部署测试】个人信息雷达已连接私人频道。这是一条测试消息。")
        print("Telegram 已确认接收测试消息。")
    elif args.operation == "deliver":
        deliver(args.state, config, rules, today)
    else:
        incoming = collect(config, rules, today)
        sent = list(sent_entries(args.state, today))
        pending = merge_pending(read_pending(args.state), incoming, sent)
        if args.operation == "collect":
            write_pending(args.state, pending)
        print(f"待发送队列共 {len(pending)} 条。")


if __name__ == "__main__":
    main()
