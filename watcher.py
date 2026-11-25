#!/usr/bin/env python3
"""
Horde watch for WoW Forever (EU PvP).

Polls public sources (Blizzard forums, Blizzard staff posts, Wowhead news) and
sends a Telegram message when someone reports that Horde character creation
has opened, or when Blizzard staff post about the faction lock.

It never touches the game client or a Battle.net account.

Environment:
  TELEGRAM_BOT_TOKEN   token from @BotFather
  TELEGRAM_CHAT_ID     one chat id, or several separated by commas

Flags:
  --dry-run   print alerts instead of sending them, do not save state
  --test      send a test message with the status of each source, then exit
"""
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ----------------------------------------------------------------- settings
EU = "https://eu.forums.blizzard.com/en/wow"
US = "https://us.forums.blizzard.com/en/wow"
WOWHEAD_RSS = "https://www.wowhead.com/news/rss/all"

FOREVER_CATEGORIES = {359, 360}   # EU forum: WoW: Forever + General Discussion
PLAYER_MAX_AGE = timedelta(hours=3)    # older player reports are stale
STAFF_MAX_AGE = timedelta(hours=48)
POST_PAGES = 3                    # pages of newest forum posts read per run
MAX_ALERTS_PER_RUN = 5
HEARTBEAT_HOUR_UTC = 8            # silent daily "still watching"; None = off
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
UA = "Mozilla/5.0 (compatible; horde-watch/1.0; personal forum notifier)"

# ------------------------------------------------------------------ helpers


def now_utc():
    return datetime.now(timezone.utc)


def parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(str(value))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fetch(url, accept="application/json"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def fetch_json(url):
    return json.loads(fetch(url).decode("utf-8", "replace"))


QUOTE_HTML = re.compile(r"<aside\b.*?</aside>|<blockquote\b.*?</blockquote>", re.S | re.I)
QUOTE_BB = re.compile(r"\[quote\b.*?\[/quote\]", re.S | re.I)
TAG = re.compile(r"<[^>]+>")


def clean(text):
    """Drop quoted replies and markup so only the author's own words remain."""
    if not text:
        return ""
    text = QUOTE_HTML.sub(" ", text)
    text = QUOTE_BB.sub(" ", text)
    text = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", "\n", text, flags=re.I)
    text = TAG.sub(" ", text)
    text = html.unescape(text)
    text = text.replace("’", "'").replace("‘", "'")
    return re.sub(r"[ \t]+", " ", text).strip()


# ----------------------------------------------------------------- matching
OPENWORD = r"(?:open|opened|unlocked|available|unrestricted)(?!\s+(?:world|beta|pvp|to\s+debate))"
COPULA = r"(?:is|are|'s|just|now|finally|got|has|have|been|again|currently|creation)"
FILLER = r"(?:(?:now|finally|just|been|back|currently|actually)\s+)*"
GAP = r"(?:(?!alliance|lock|clos|block|restrict)[^.!?\n]){0,50}?"

POSITIVE = [
    # "Horde is open", "Horde creation on EU PvP just opened"
    re.compile(r"\bhorde\b" + GAP + r"\b" + COPULA + r"\s+" + FILLER + OPENWORD + r"\b"),
    # "it is now open for Horde"
    re.compile(r"\b" + COPULA + r"\s+" + FILLER + OPENWORD + r"\b[^.!?\n]{0,25}\b(?:for|to|on)\s+(?:the\s+)?horde\b"),
    # "HORDE OPEN", "horde eu pvp unlocked"
    re.compile(r"\bhorde\s+(?:(?:eu|pvp|side|creation|faction)\s+){0,3}" + OPENWORD + r"\b"),
    # "I can now create a Horde character", "managed to make my horde"
    re.compile(r"\b(?:can|could|able\s+to|managed\s+to|got\s+to)\s+(?:now\s+|finally\s+|just\s+)?"
               r"(?:create|make|made|roll|rolled)\b[^.!?\n]{0,30}\bhorde\b"),
    # "just created my orc"
    re.compile(r"\b(?:just|finally)\s+(?:created|made|rolled)\b[^.!?\n]{0,30}"
               r"\b(?:horde|orc|troll|tauren|undead|forsaken)\b"),
    # "Horde is no longer locked", "horde isn't locked anymore"
    re.compile(r"\bhorde\b[^.!?\n]{0,40}\b(?:no\s+longer|not|isn't|isnt)\s+(?:faction\s+)?(?:locked|restricted|blocked)\b"),
]
# These only count when the thread itself is about the Horde lock.
CONTEXT_POSITIVE = [
    re.compile(r"\b(?:it's|its|it\s+is|is|they're)\s+" + FILLER + r"(?:open|unlocked|available|up)\b"
               r"[^.!?\n]{0,15}\b(?:now|again|atm|rn|currently|guys|go)\b"),
    re.compile(r"\b(?:open|unlocked|opened)\s+(?:now|again|right\s+now|atm|rn)\b"),
    re.compile(r"\b(?:now|just)\s+(?:open|unlocked|opened)\b"),
    re.compile(r"\b(?:lock|restriction|block)\s+(?:is\s+|has\s+been\s+|was\s+just\s+|got\s+|is\s+now\s+|has\s+just\s+been\s+)?"
               r"(?:lifted|removed|gone|off|over|disabled)\b"),
]
NO_LONGER_LOCKED = POSITIVE[-1]
OPEN_FOR_HORDE = POSITIVE[1]
CREATION_WORD = re.compile(r"creat|lock|pvp|faction|character|realm|ruleset|server")

# Words that turn a sentence into a wish, question, condition or denial.
SOFT_NEG = re.compile(
    r"\b(?:not|never|cannot|cant|unable|when|whenever|if|once|until|unless|hope|hoping|wish|"
    r"waiting|wait|should|would|will|gonna|going\s+to|want|please|pls|maybe|why|how|anyone|"
    r"any\s+news|eta|supposed|soon|rumou?rs?|imagine|expect|expected|whether|claims?|allegedly|"
    r"apparently|heard|someone\s+said)\b|n't\b")
HARD_NEG = re.compile(r"\b(?:not|cannot|cant|unable|still|nope|nvm|locked\s+again|closed\s+again)\b|n't\b|\?")
PAST = re.compile(
    r"\b(?:yesterday|last\s+(?:night|week)|days?\s+ago|hours?\s+ago|earlier\s+today|this\s+morning|"
    r"back\s+then|at\s+launch|name\s+reservation|closed\s+again|locked\s+again|again\s+locked|"
    r"was\s+(?:open|unlocked|available)|were\s+(?:open|unlocked|available)|used\s+to)\b")
OTHER_RULESET = re.compile(r"\b(?:normal|pve|rp|roleplay|roleplaying|hardcore)\b")
OTHER_REGION_CS = re.compile(r"\b(?:NA|US)\b")
OTHER_REGION = re.compile(r"\b(?:north\s+america|americas?|american|oceanic)\b")
EU_HINT = re.compile(r"\b(?:eu|europe|european)\b")
SENTENCE = re.compile(r"[^.!?\n]+[.!?]*")


def _sentence_ok(sentence_lc, sentence_orig, match, allow_not):
    """Reject wishes, questions, denials, old news and other rulesets/regions."""
    head = sentence_lc[:match.end()]
    tail = sentence_lc[match.end():]
    if allow_not:
        head = head[:match.start()]
    if SOFT_NEG.search(head) or HARD_NEG.search(tail):
        return False
    if PAST.search(sentence_lc):
        return False
    if OTHER_RULESET.search(sentence_lc):
        return False
    other_region = OTHER_REGION_CS.search(sentence_orig) or OTHER_REGION.search(sentence_lc)
    if other_region and not EU_HINT.search(sentence_lc):
        return False
    return True


def open_signal(title, text, first_post):
    """Return the sentence that reports an opening, or None."""
    title_lc = (title or "").lower()
    about_horde_lock = "horde" in title_lc
    body = text or ""
    if re.search(r"\b(?:locked|closed)\s+again\b", body.lower()):
        return None
    parts = []
    if first_post and title:
        parts.append(title)
    parts.extend(SENTENCE.findall(body))
    for sentence in parts:
        s_lc = sentence.lower().strip()
        if not s_lc:
            continue
        patterns = list(POSITIVE)
        if about_horde_lock or "horde" in body.lower():
            patterns += CONTEXT_POSITIVE
        for pat in patterns:
            m = pat.search(s_lc)
            if not m:
                continue
            if pat in CONTEXT_POSITIVE and not (about_horde_lock or "horde" in s_lc):
                continue
            # "X is open for Horde" only counts when X is about character creation.
            if pat is OPEN_FOR_HORDE and not (about_horde_lock or CREATION_WORD.search(s_lc)):
                continue
            if _sentence_ok(s_lc, sentence, m, allow_not=(pat is NO_LONGER_LOCKED)):
                return sentence.strip()
    return None


STAFF_TOPIC = re.compile(r"\b(?:faction|horde|alliance)\b")
STAFF_LOCK = re.compile(r"character\s+creation|\block(?:ed|s)?\b|restrict|balance|unavailable|imbalance")
STAFF_GAME = re.compile(r"\bforever\b|\bpvp\b")


def staff_signal(title, text):
    blob = ((title or "") + " \n " + (text or "")).lower()
    return bool(STAFF_TOPIC.search(blob) and STAFF_LOCK.search(blob) and STAFF_GAME.search(blob))


NEWS_LOCK = re.compile(r"faction\s+lock|character\s+creation|horde")
NEWS_CHANGE = re.compile(r"lift|reopen|re-open|unlock|\bopen(?:s|ed)?\b|eas(?:e|es|ed|ing)\b|relax|remov|available again")


def news_signal(title, text):
    t = (title or "").lower()
    blob = t + " \n " + (text or "").lower()
    if "forever" not in blob and "pvp" not in blob:
        return False
    return bool(NEWS_LOCK.search(t) and NEWS_CHANGE.search(t))


# ------------------------------------------------------------------ sources


def _post_item(base, tag, p, topic=None):
    topic = topic or {}
    topic_id = p.get("topic_id") or topic.get("id")
    slug = p.get("topic_slug") or topic.get("slug") or "topic"
    number = p.get("post_number") or 1
    staff = bool(p.get("staff") or p.get("admin")
                 or "blizz" in str(p.get("primary_group_name") or "").lower())
    return {
        "id": f"{tag}-{p.get('id')}",
        "kind": "staff" if staff else "player",
        "title": p.get("topic_title") or topic.get("title") or "",
        "text": clean(p.get("raw") or p.get("cooked") or p.get("excerpt") or p.get("blurb") or ""),
        "author": p.get("username") or (p.get("user") or {}).get("username") or "?",
        "created": parse_time(p.get("created_at")),
        "url": f"{base}/t/{slug}/{topic_id}/{number}",
        "first_post": number == 1,
        "category": p.get("category_id") or topic.get("category_id"),
    }


def src_forum_posts(base=EU, tag="eu"):
    """Newest posts across the forum, a few pages back."""
    items, before = [], None
    cutoff = now_utc() - timedelta(minutes=90)
    for _ in range(POST_PAGES):
        url = f"{base}/posts.json" + (f"?before={before}" if before else "")
        posts = fetch_json(url).get("latest_posts") or []
        if not posts:
            break
        items += [_post_item(base, tag, p) for p in posts]
        ids = [p.get("id") for p in posts if isinstance(p.get("id"), int)]
        oldest = min((i["created"] for i in items if i["created"]), default=None)
        if not ids or (oldest and oldest < cutoff):
            break
        before = min(ids)
    return items


LOOSE = re.compile(r"open|unlock|lift|creat|availab|made|rolled")


def src_forum_search(base=EU, tag="eu"):
    """Forum search for recent posts that mention Horde, newest first."""
    day = (now_utc() - timedelta(days=1)).strftime("%Y-%m-%d")
    q = urllib.parse.quote(f"horde after:{day} order:latest")
    data = fetch_json(f"{base}/search.json?q={q}")
    topics = {t.get("id"): t for t in (data.get("topics") or [])}
    items, fetched = [], 0
    for p in data.get("posts") or []:
        item = _post_item(base, tag, p, topics.get(p.get("topic_id")))
        created = item["created"]
        fresh = created and now_utc() - created <= PLAYER_MAX_AGE
        # The search blurb is cut short, so read the full post for candidates.
        if fresh and fetched < 8 and LOOSE.search(item["text"].lower() + item["title"].lower()):
            try:
                full = fetch_json(f"{base}/posts/{p.get('id')}.json")
                fetched += 1
                full_item = _post_item(base, tag, full, topics.get(p.get("topic_id")))
                full_item["title"] = full_item["title"] or item["title"]
                full_item["category"] = full_item["category"] or item["category"]
                item = full_item
            except Exception:
                pass
        items.append(item)
    return items


def src_blue(base, tag):
    """Posts by Blizzard staff (the forum's blue tracker)."""
    data = fetch_json(f"{base}/groups/blizzard-tracker/posts.json")
    posts = data if isinstance(data, list) else (data.get("posts") or [])
    host = base.split("/en/")[0]
    items = []
    for p in posts:
        topic = p.get("topic") or {}
        item = _post_item(base, tag, p, topic)
        item["kind"] = "staff"
        item["title"] = p.get("title") or topic.get("title") or item["title"]
        if p.get("url"):
            item["url"] = host + p["url"] if p["url"].startswith("/") else p["url"]
        items.append(item)
    return items


def src_wowhead():
    root = ET.fromstring(fetch(WOWHEAD_RSS, accept="application/rss+xml, application/xml"))
    items = []
    for node in root.iter("item"):
        link = (node.findtext("link") or "").strip()
        items.append({
            "id": "wh-" + (node.findtext("guid") or link),
            "kind": "news",
            "title": (node.findtext("title") or "").strip(),
            "text": clean(node.findtext("description") or ""),
            "author": "Wowhead",
            "created": parse_time(node.findtext("pubDate")),
            "url": link,
            "first_post": True,
            "category": None,
        })
    return items


SOURCES = [
    # (name, function, required for health)
    ("EU forum posts", lambda: src_forum_posts(EU, "eu"), True),
    ("EU forum search", lambda: src_forum_search(EU, "eu"), True),
    ("EU Blizzard staff posts", lambda: src_blue(EU, "eu"), True),
    ("US Blizzard staff posts", lambda: src_blue(US, "us"), False),
    ("Wowhead news", src_wowhead, False),
]


def collect():
    items, status = {}, []
    for name, fn, required in SOURCES:
        try:
            got = fn()
            for item in got:
                if item["id"] not in items or len(item["text"]) > len(items[item["id"]]["text"]):
                    items[item["id"]] = item
            status.append((name, True, f"{len(got)} items", required))
        except urllib.error.HTTPError as e:
            status.append((name, False, f"HTTP {e.code}", required))
        except Exception as e:
            status.append((name, False, type(e).__name__, required))
    return list(items.values()), status


# ----------------------------------------------------------------- alerting


def relevant_place(item):
    """Player posts must be in the Forever forums or mention Forever/PvP."""
    if item["category"] in FOREVER_CATEGORIES:
        return True
    blob = (item["title"] + " " + item["text"]).lower()
    return "forever" in blob or "pvp" in blob


def classify(item, since):
    created = item["created"]
    if not created or created < since:
        return None
    age = now_utc() - created
    if item["kind"] == "staff":
        if age <= STAFF_MAX_AGE and staff_signal(item["title"], item["text"]):
            return "staff", item["text"][:300]
        return None
    if item["kind"] == "news":
        if age <= STAFF_MAX_AGE and news_signal(item["title"], item["text"]):
            return "news", item["text"][:300]
        return None
    if age > PLAYER_MAX_AGE or not relevant_place(item):
        return None
    sentence = open_signal(item["title"], item["text"], item["first_post"])
    return ("player", sentence[:300]) if sentence else None


def age_text(created):
    minutes = int((now_utc() - created).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 90:
        return f"{minutes} min ago"
    return f"{minutes // 60} h ago"


def format_alert(item, label, snippet):
    e = html.escape
    head = {
        "player": "🟢 <b>Possible Horde opening (EU PvP)</b>",
        "staff": "🔵 <b>Blizzard post about the faction lock</b>",
        "news": "📰 <b>News about the faction lock</b>",
    }[label]
    lines = [head]
    if item["title"]:
        lines.append(e(item["title"][:150]))
    if snippet:
        lines.append(f"<i>“{e(snippet)}”</i>")
    lines.append(f"{e(item['author'])}, {age_text(item['created'])}")
    lines.append(e(item["url"]))
    if label == "player":
        lines.append("Player report, not confirmed. Try creating the character now.")
    return "\n".join(lines)


def send_telegram(text, silent=False, dry_run=False):
    if dry_run:
        print("--- would send ---\n" + text + "\n")
        return True
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chats = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    if not token or not chats:
        print("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing.")
        return False
    ok = True
    for chat in chats:
        body = urllib.parse.urlencode({
            "chat_id": chat, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": "true",
            "disable_notification": "true" if silent else "false",
        }).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
        try:
            urllib.request.urlopen(req, timeout=20).read()
        except urllib.error.HTTPError as e:
            ok = False   # never print the URL: it contains the token
            print(f"Telegram refused the message (HTTP {e.code}). Check token and chat id.")
        except Exception as e:
            ok = False
            print(f"Telegram could not be reached ({type(e).__name__}).")
    return ok


def status_text(status):
    return "\n".join(f"{'✅' if ok else '❌'} {html.escape(name)}: {html.escape(detail)}"
                     for name, ok, detail, _ in status)


# -------------------------------------------------------------------- state


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
        f.write("\n")


# --------------------------------------------------------------------- main


def main(argv):
    dry_run = "--dry-run" in argv
    state = load_state()
    before = json.dumps(state, sort_keys=True)
    now = now_utc()

    items, status = collect()
    for name, ok, detail, _ in status:
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")
    required_ok = any(ok for _, ok, _, required in status if required)

    if "--test" in argv:
        sent = send_telegram("🧪 <b>Horde watcher test</b>\n" + status_text(status), dry_run=dry_run)
        return 0 if sent and required_ok else 1

    first_run = "since" not in state
    if first_run:
        state["since"] = now.isoformat()
        head = ("✅ <b>Horde watcher is live.</b> Checking every 5 minutes."
                if required_ok else
                "⚠️ <b>Horde watcher started, but it cannot reach the forum yet.</b>")
        send_telegram(head + "\n" + status_text(status), dry_run=dry_run)
    since = parse_time(state["since"]) or now
    alerted = state.get("alerted", [])

    hits = []
    for item in items:
        if item["id"] in alerted:
            continue
        result = classify(item, since)
        if result:
            hits.append((item, *result))
    hits.sort(key=lambda h: h[0]["created"], reverse=True)
    print(f"{len(items)} items checked, {len(hits)} new alert(s)")

    for item, label, snippet in hits[:MAX_ALERTS_PER_RUN]:
        if send_telegram(format_alert(item, label, snippet), dry_run=dry_run):
            alerted.append(item["id"])
    extra = hits[MAX_ALERTS_PER_RUN:]
    if extra:
        send_telegram(f"…and {len(extra)} more matching posts in the forum.", dry_run=dry_run)
        alerted += [h[0]["id"] for h in extra]
    state["alerted"] = alerted[-500:]

    # Health: warn once when the forum has been unreachable three runs in a row.
    streak = 0 if required_ok else state.get("fail_streak", 0) + 1
    if streak == 3:
        send_telegram("⚠️ <b>Horde watcher cannot reach the forum.</b>\n" + status_text(status),
                      dry_run=dry_run)
    if streak or state.get("fail_streak"):
        state["fail_streak"] = streak

    today = now.strftime("%Y-%m-%d")
    if (HEARTBEAT_HOUR_UTC is not None and not first_run and now.hour >= HEARTBEAT_HOUR_UTC
            and state.get("heartbeat") != today):
        send_telegram("👀 Still watching, nothing to report.\n" + status_text(status),
                      silent=True, dry_run=dry_run)
        state["heartbeat"] = today
    elif first_run:
        state["heartbeat"] = today

    if not dry_run and json.dumps(state, sort_keys=True) != before:
        save_state(state)
    return 0 if required_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
