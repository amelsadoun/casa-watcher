#!/usr/bin/env python3
"""
CASA Paris-Saclay watcher
-------------------------
Scrapes https://casa.universite-paris-saclay.fr/fr/housing/list, detects NEW
housing offers (by their unique UUID) and pushes them to your phone with ntfy.

Usage
  python casa_watcher.py --dry-run     # parse page 1 and print what was found (no state, no push)
  python casa_watcher.py --test-ntfy   # send a test notification
  python casa_watcher.py               # normal run (what GitHub Actions executes every ~5 min)
  python casa_watcher.py --full        # force a scan of ALL pages

All configuration is done with environment variables (see README.md).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://casa.universite-paris-saclay.fr"


# --------------------------------------------------------------------------- #
# Configuration (environment variables)
# --------------------------------------------------------------------------- #
def _int(name: str, default: int | None = None) -> int | None:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


LIST_URL = os.getenv("CASA_URL", "").strip() or f"{BASE}/fr/housing/list"
NTFY_SERVER = (os.getenv("NTFY_SERVER", "").strip() or "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_TOKEN = os.getenv("NTFY_TOKEN", "").strip()          # only for protected/self-hosted servers
NTFY_PRIORITY = _int("NTFY_PRIORITY", 4)                   # 1..5
PAGES_PER_RUN = _int("PAGES_PER_RUN", 5)                   # pages scanned per run when sorted newest-first
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "").strip() or 0.7)
MAX_NOTIFS = _int("MAX_NOTIFS_PER_RUN", 15)                # anti-flood
FAIL_ALERT_AFTER = _int("FAIL_ALERT_AFTER", 6)             # consecutive failed runs before a "scraper down" push
AUTO_SORT = os.getenv("AUTO_SORT", "1").strip() != "0"     # try to sort "newest first" automatically
ASSUME_SORTED = os.getenv("ASSUME_SORTED", "0").strip() == "1"  # set to 1 if CASA_URL already sorts newest-first
SKIP_FULL = os.getenv("SKIP_FULL", "1").strip() != "0"     # ignore offers flagged "complet"
MAX_PRICE = _int("MAX_PRICE")                              # e.g. 700
MIN_SURFACE = _int("MIN_SURFACE")                          # e.g. 15
DEPARTMENTS = {d.strip() for d in os.getenv("DEPARTMENTS", "").split(",") if d.strip()}  # e.g. "91,78"
STATE_FILE = Path(os.getenv("STATE_FILE", "state/seen.json"))
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0 casa-watcher/1.0 (personal use)",
)

log = logging.getLogger("casa")
ID_RE = re.compile(r"/housing/detail/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)
MODES = ("chez l'habitant", "logement entier", "colocation", "contre service", "résidence", "logement partagé")


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Listing:
    id: str
    url: str
    title: str = ""
    price_text: str = ""
    price: int | None = None
    surface: int | None = None
    mode: str = ""
    city: str = ""
    postcode: str = ""
    updated: str = ""
    offered_by: str = ""
    summary: str = ""
    image: str = ""
    full: bool = False
    housing_aid: bool = False


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5"})
    return s


def fetch(session: requests.Session, url: str) -> str:
    log.debug("GET %s", url)
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return r.text


def page_url(base_url: str, page: int, extra: dict[str, str] | None = None) -> str:
    """Return base_url with ?page=N (and optional extra params) set, keeping any existing filters."""
    u = urlparse(base_url)
    drop = {"page", *(extra or {}).keys()}
    query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k not in drop]
    query += list((extra or {}).items())
    query.append(("page", str(page)))
    return urlunparse(u._replace(query=urlencode(query)))


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _id_of(href: str | None) -> str | None:
    m = ID_RE.search(href or "")
    return m.group(1).lower() if m else None


def _card_container(anchor):
    """Climb from a card link to the largest ancestor that still holds only ONE listing id."""
    node = anchor
    while node.parent is not None and node.parent.name not in ("body", "html", "[document]"):
        ids = {_id_of(a.get("href")) for a in node.parent.select('a[href*="/housing/detail/"]')}
        ids.discard(None)
        if len(ids) > 1:
            break
        node = node.parent
    return node


def _norm(s: str) -> str:
    return s.lower().replace("’", "'").strip()


def _parse_card(box, lid: str, url: str) -> Listing:
    lines = [l.strip() for l in box.get_text("\n", strip=True).split("\n") if l.strip()]
    text = "\n".join(lines)
    headings = [h.get_text(" ", strip=True) for h in box.find_all(["h1", "h2", "h3", "h4"])]

    price_text = next((h for h in headings if "€" in h), "") or next((l for l in lines if "€" in l), "")
    title = next((h for h in headings if "€" not in h and h), "")
    if not title:
        title = next((l for l in lines if re.search(r"\bm\s*(2|²)", l)), "")

    price = None
    m = re.search(r"(\d[\d\s\u202f\u00a0]*)(?:[.,]\d+)?\s*€", price_text)
    if m:
        digits = re.sub(r"\D", "", m.group(1))
        price = int(digits) if digits else None

    surface = None
    m = re.search(r"(\d+)(?:\s*-\s*(\d+))?\s*m\s*(?:2|²)", title)
    if m:
        surface = max(int(g) for g in m.groups() if g)

    mode = next((l for l in lines[:12] if len(l) < 40 and any(k in _norm(l) for k in MODES)), "")

    city = postcode = ""
    city_idx = None
    for i, l in enumerate(lines[:16]):
        m = re.fullmatch(r"(.+?)\s*\((\d{5})\)", l)
        if m:
            city, postcode, city_idx = m.group(1).strip().title(), m.group(2), i
            break

    summary_lines: list[str] = []
    if city_idx is not None:
        for l in lines[city_idx + 1:]:
            if l.lower().startswith(("offre saisie", "mis à jour")):
                break
            summary_lines.append(l)
    summary = re.sub(r"\s+", " ", " ".join(summary_lines)).strip()
    if len(summary) > 220:
        summary = summary[:217].rstrip() + "…"

    m = re.search(r"Mis à jour le\s*(\d{2}/\d{2}/\d{4})", text)
    updated = m.group(1) if m else ""
    m = re.search(r"Offre saisie par\s*(.+)", text)
    offered_by = m.group(1).strip() if m else ""

    full = any(re.fullmatch(r"(?i)\W*(complet|full|indisponible)\W*", l) for l in lines[:8])
    housing_aid = "aides au logement" in _norm(price_text)

    image = ""
    for img in box.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            image = urljoin(BASE, src)
            break

    return Listing(
        id=lid, url=url, title=title, price_text=price_text, price=price, surface=surface, mode=mode,
        city=city, postcode=postcode, updated=updated, offered_by=offered_by, summary=summary,
        image=image, full=full, housing_aid=housing_aid,
    )


def parse_cards(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    out: list[Listing] = []
    for a in soup.select('a[href*="/housing/detail/"]'):
        lid = _id_of(a.get("href"))
        if not lid or lid in seen:
            continue
        seen.add(lid)
        out.append(_parse_card(_card_container(a), lid, urljoin(BASE, a["href"])))
    return out


def total_pages(html: str) -> int:
    nums = [int(n) for n in re.findall(r"[?&]page=(\d+)", html)]
    return max(nums) if nums else 1


def detect_sort(html: str) -> tuple[str, str, str] | None:
    """Find the 'Trier par' <select> and return (param_name, value, label) for 'newest first'."""
    soup = BeautifulSoup(html, "lxml")
    for sel in soup.find_all("select"):
        label = ""
        if sel.get("id"):
            lab = soup.find("label", attrs={"for": sel["id"]})
            label = lab.get_text(" ", strip=True) if lab else ""
        key = f"{label} {sel.get('name', '')} {sel.get('id', '')}"
        if not re.search(r"trier|sort|order|\btri\b", key, re.I):
            continue
        options = [(o.get("value", ""), o.get_text(" ", strip=True)) for o in sel.find_all("option")]
        log.info("Sort options found: %s", options)
        for pattern in (r"r[ée]cent|nouveau|derni|newest|latest", r"mis[e]? [àa] jour|publi|updated"):
            for value, text in options:
                if value and re.search(pattern, text, re.I):
                    return sel.get("name") or "", value, text
    return None


# --------------------------------------------------------------------------- #
# Scan
# --------------------------------------------------------------------------- #
def scan(session: requests.Session, full: bool) -> dict[str, Listing]:
    html = fetch(session, page_url(LIST_URL, 1))
    cards = parse_cards(html)
    if not cards:
        raise RuntimeError("0 listing cards parsed on page 1 (layout changed, blocked, or site down)")
    total = total_pages(html)
    extra: dict[str, str] = {}
    sorted_ok = ASSUME_SORTED

    if AUTO_SORT and not ASSUME_SORTED:
        found = detect_sort(html)
        if found and found[0]:
            name, value, label = found
            try:
                html_sorted = fetch(session, page_url(LIST_URL, 1, {name: value}))
                if parse_cards(html_sorted):
                    extra, html, sorted_ok = {name: value}, html_sorted, True
                    log.info("Sorting newest-first with %s=%s (%s)", name, value, label)
            except requests.RequestException as e:  # fall back to unsorted
                log.warning("Sorted request failed (%s); falling back to unsorted", e)
        else:
            log.warning("No usable 'newest first' sort found")

    if full or not sorted_ok:
        pages = total
        if not full:
            log.warning("Not sorted newest-first -> scanning ALL %d pages every run. "
                        "Set CASA_URL (with sort) + ASSUME_SORTED=1 to scan less.", total)
    else:
        pages = min(total, PAGES_PER_RUN)

    results: dict[str, Listing] = {}
    for p in range(1, pages + 1):
        if p > 1:
            time.sleep(REQUEST_DELAY)
            html = fetch(session, page_url(LIST_URL, p, extra))
        page_cards = parse_cards(html)
        if not page_cards:
            break
        for c in page_cards:
            results.setdefault(c.id, c)
    log.info("Scanned %d page(s), %d unique listings", min(pages, total), len(results))
    return results


def _date_key(d: str) -> str:
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", d or "")
    return f"{m.group(3)}{m.group(2)}{m.group(1)}" if m else "00000000"


def wanted(l: Listing) -> bool:
    if MAX_PRICE and l.price and l.price > MAX_PRICE:
        return False
    if MIN_SURFACE and l.surface and l.surface < MIN_SURFACE:
        return False
    if DEPARTMENTS and l.postcode and l.postcode[:2] not in DEPARTMENTS:
        return False
    return True


# --------------------------------------------------------------------------- #
# ntfy
# --------------------------------------------------------------------------- #
def ntfy(title: str, message: str, *, click: str | None = None, tags: list[str] | None = None,
         priority: int | None = None, attach: str | None = None) -> None:
    if not NTFY_TOPIC:
        raise RuntimeError("NTFY_TOPIC is not set")
    payload: dict = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": priority or NTFY_PRIORITY,
        "tags": tags or [],
    }
    if click:
        payload["click"] = click
        payload["actions"] = [{"action": "view", "label": "Ouvrir l'annonce", "url": click, "clear": True}]
    if attach:
        payload["attach"] = attach
    headers = {"Authorization": f"Bearer {NTFY_TOKEN}"} if NTFY_TOKEN else {}
    # JSON publishing => full UTF-8 support (accents, emojis) in title/message.
    r = requests.post(NTFY_SERVER, json=payload, headers=headers, timeout=20)
    r.raise_for_status()


def notify_listing(l: Listing) -> None:
    head = " · ".join(x for x in (l.price_text.replace("Aides au logement", "APL").strip(),
                                  f"{l.surface} m²" if l.surface else "", l.city) if x)
    lines = [l.title]
    where = " — ".join(x for x in (l.mode, f"{l.city} ({l.postcode})" if l.city else "") if x)
    if where:
        lines.append(where)
    if l.summary:
        lines.append(l.summary)
    meta = " · ".join(x for x in (f"MAJ {l.updated}" if l.updated else "", l.offered_by) if x)
    if meta:
        lines.append(meta)
    ntfy(f"Nouveau logement CASA — {head}", "\n".join(lines), click=l.url, tags=["house"], attach=l.image or None)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            data.setdefault("seen", {})
            data.setdefault("failures", 0)
            data.setdefault("initialized", bool(data["seen"]))
            return data
        except json.JSONDecodeError:
            log.error("State file is corrupted; starting fresh")
    return {"initialized": False, "seen": {}, "failures": 0}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Deterministic output => git only sees a diff when something really changed.
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="parse page 1 and print it; no state, no push")
    ap.add_argument("--test-ntfy", action="store_true", help="send a test notification and exit")
    ap.add_argument("--full", action="store_true", help="scan every page")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.test_ntfy:
        ntfy("CASA watcher", "Test OK ✅ — les notifications fonctionnent.", tags=["white_check_mark"])
        log.info("Test notification sent to %s/%s", NTFY_SERVER, NTFY_TOPIC)
        return 0

    session = make_session()

    if args.dry_run:
        html = fetch(session, page_url(LIST_URL, 1))
        cards = parse_cards(html)
        log.info("%d cards parsed on page 1, %d pages total", len(cards), total_pages(html))
        log.info("Sort detection: %s", detect_sort(html))
        for c in cards:
            print(f"- {c.price_text or '?':<28} | {c.title or '?':<34} | {c.mode:<16} | "
                  f"{c.city} {c.postcode} | maj {c.updated} | full={c.full} | wanted={wanted(c)}\n  {c.url}")
        return 0 if cards else 1

    state = load_state()
    first_run = not state["initialized"]
    try:
        listings = scan(session, full=args.full or first_run)
    except Exception as e:  # noqa: BLE001 - we want to alert on anything
        state["failures"] += 1
        log.error("Scan failed (%d in a row): %s", state["failures"], e)
        if state["failures"] == FAIL_ALERT_AFTER and NTFY_TOPIC:
            try:
                ntfy("CASA watcher en panne ⚠️", f"{state['failures']} échecs consécutifs.\n{e}", tags=["warning"], priority=3)
            except Exception as ne:  # noqa: BLE001
                log.error("Could not send failure alert: %s", ne)
        save_state(state)
        return 0  # don't spam GitHub failure e-mails; the ntfy alert covers it

    if state["failures"]:
        state["failures"] = 0

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    new = [l for l in listings.values() if l.id not in state["seen"] and not (SKIP_FULL and l.full)]
    log.info("%d new listing(s) (first_run=%s)", len(new), first_run)

    if first_run:
        for l in new:
            state["seen"][l.id] = today
        state["initialized"] = True
        save_state(state)
        if NTFY_TOPIC:
            ntfy("CASA watcher activé ✅", f"{len(new)} annonces enregistrées comme base. "
                 "Tu seras notifié à chaque nouvelle annonce.", tags=["white_check_mark"], priority=2)
        return 0

    sent = 0
    for l in sorted(new, key=lambda x: _date_key(x.updated)):  # oldest -> newest, so the newest lands on top
        state["seen"][l.id] = today
        if not wanted(l):
            continue
        if sent >= MAX_NOTIFS:
            continue
        try:
            notify_listing(l)
            sent += 1
            time.sleep(1)  # be nice to ntfy rate limits
        except Exception as e:  # noqa: BLE001
            log.error("ntfy failed for %s: %s", l.id, e)
            del state["seen"][l.id]  # retry on next run
    skipped = len([l for l in new if wanted(l)]) - sent
    if skipped > 0:
        ntfy("CASA — d'autres nouvelles annonces", f"+{skipped} nouvelles annonces non détaillées.", click=LIST_URL, priority=3)

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
