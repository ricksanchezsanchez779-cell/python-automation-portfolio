#!/usr/bin/env python3
"""Price-table scraper.

Extracts a price table from any HTML page and writes a normalized CSV.
Configured per client: URL + CSS selectors, no code changes needed.

Examples
--------
Offline demo (bundled fixture, no network):

    python price_scraper.py --demo

Real target with pagination:

    python price_scraper.py \
        --url https://books.toscrape.com/ \
        --item-selector "article.product_pod" \
        --name-selector "h3 a" \
        --price-selector "p.price_color" \
        --next-selector "li.next a" \
        --max-pages 3 \
        --out books.csv

Design notes
------------
* Retries with exponential backoff + jitter on 429/5xx and timeouts.
* Respects robots.txt unless --ignore-robots is passed.
* Polite fixed delay between requests (--delay, default 1.0s).
* Price strings are parsed into a numeric value + ISO currency code, so the
  client can pivot/sum the output directly in Excel.
"""

from __future__ import annotations

import argparse
import csv
import logging
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

LOG = logging.getLogger("price_scraper")

USER_AGENT = "PriceTableBot/1.0 (portfolio demo; contact: your-email@example.com)"
TIMEOUT = 20

# --------------------------------------------------------------------------
# Price parsing
# --------------------------------------------------------------------------

CURRENCY_SYMBOLS = {
    "R$": "BRL",
    "$": "USD",
    "US$": "USD",
    "£": "GBP",
    "€": "EUR",
    "¥": "JPY",
}
CURRENCY_CODES = {"BRL", "USD", "GBP", "EUR", "JPY", "ARS", "MXN", "CLP"}

_NUM_RE = re.compile(r"[-+]?\d[\d\s.,']*")


def parse_price(raw: str) -> tuple[float | None, str | None]:
    """Turn 'R$ 1.234,56' / '$51.77' / '1 234,56 EUR' into (1234.56, 'BRL').

    Returns (None, None) when no number is present.
    """
    if not raw:
        return None, None

    text = raw.replace("\xa0", " ").strip()
    currency = None

    upper = text.upper()
    for code in CURRENCY_CODES:
        if re.search(rf"\b{code}\b", upper):
            currency = code
            break
    if currency is None:
        for symbol, code in CURRENCY_SYMBOLS.items():
            if symbol in text:
                currency = code
                break

    match = _NUM_RE.search(text)
    if not match:
        return None, currency

    number = match.group(0).replace(" ", "").replace("'", "")
    has_comma, has_dot = "," in number, "." in number

    if has_comma and has_dot:
        # The right-most separator is the decimal one.
        decimal = "," if number.rfind(",") > number.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        number = number.replace(thousands, "").replace(decimal, ".")
    elif has_comma:
        head, _, tail = number.rpartition(",")
        # "1,234" -> thousands, "51,77" -> decimal
        number = f"{head}.{tail}" if len(tail) == 2 else number.replace(",", "")
    elif has_dot:
        head, _, tail = number.rpartition(".")
        if len(tail) == 3 and head:
            number = number.replace(".", "")

    try:
        return float(number), currency
    except ValueError:
        return None, currency


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        }
    )
    return session


def robots_allowed(session: requests.Session, url: str) -> bool:
    """Check robots.txt for the target host. Fails open on network errors."""
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    parser = RobotFileParser()
    try:
        response = session.get(robots_url, timeout=TIMEOUT)
        if response.status_code >= 400:
            return True
        parser.parse(response.text.splitlines())
        return parser.can_fetch(USER_AGENT, url)
    except requests.RequestException:
        return True


def fetch(session: requests.Session, url: str, retries: int = 3) -> str:
    """GET with exponential backoff + jitter. Raises on final failure."""
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=TIMEOUT)
            if response.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {response.status_code}", response=response)
            response.raise_for_status()
            return response.text
        except (requests.RequestException, requests.HTTPError) as exc:
            last_error = exc
            if attempt == retries:
                break
            backoff = (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            LOG.warning("Attempt %d/%d failed (%s). Retrying in %.1fs", attempt, retries, exc, backoff)
            time.sleep(backoff)
    raise RuntimeError(f"Could not fetch {url}: {last_error}")


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


@dataclass
class Row:
    source_url: str
    name: str
    price_raw: str
    price_value: float | None
    currency: str | None
    in_stock: str
    scraped_at: str


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def extract_rows(
    html: str,
    page_url: str,
    item_selector: str,
    name_selector: str,
    price_selector: str,
    stock_selector: str | None,
) -> list[Row]:
    soup = BeautifulSoup(html, "lxml")
    rows: list[Row] = []
    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for item in soup.select(item_selector):
        name_node = item.select_one(name_selector)
        price_node = item.select_one(price_selector)
        if price_node is None:
            LOG.debug("Item without price, skipped: %s", clean(item.get_text())[:60])
            continue

        price_raw = clean(price_node.get_text())
        value, currency = parse_price(price_raw)
        stock_node = item.select_one(stock_selector) if stock_selector else None

        rows.append(
            Row(
                source_url=page_url,
                name=clean(name_node.get_text()) if name_node else "",
                price_raw=price_raw,
                price_value=value,
                currency=currency,
                in_stock=clean(stock_node.get_text()) if stock_node else "",
                scraped_at=scraped_at,
            )
        )
    return rows


def next_page_url(soup: BeautifulSoup, selector: str | None, base_url: str) -> str | None:
    if not selector:
        return None
    node = soup.select_one(selector)
    if node is None:
        return None
    href = node.get("href")
    return urljoin(base_url, href) if href else None


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

FIELDS = ["name", "price_raw", "price_value", "currency", "in_stock", "source_url", "scraped_at"]


def write_csv(rows: list[Row], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


# --------------------------------------------------------------------------
# Offline demo fixture
# --------------------------------------------------------------------------

DEMO_HTML = """
<html><body>
<div class="grid">
  <div class="card"><h3>Monitor 27" 4K</h3><span class="price">R$ 1.899,90</span><span class="stock">Em estoque</span></div>
  <div class="card"><h3>Teclado mecânico</h3><span class="price">R$ 349,00</span><span class="stock">Em estoque</span></div>
  <div class="card"><h3>Mouse sem fio</h3><span class="price">R$ 89,90</span><span class="stock">Últimas 3 unidades</span></div>
  <div class="card"><h3>Headset USB</h3><span class="price">R$ 259,50</span><span class="stock">Em estoque</span></div>
  <div class="card"><h3>Webcam HD</h3><span class="price">R$ 179,99</span><span class="stock">Esgotado</span></div>
</div>
</body></html>
"""


def run_demo(out_path: Path) -> int:
    rows = extract_rows(
        DEMO_HTML,
        page_url="fixture://demo",
        item_selector="div.card",
        name_selector="h3",
        price_selector="span.price",
        stock_selector="span.stock",
    )
    write_csv(rows, out_path)
    LOG.info("Demo: %d rows written to %s", len(rows), out_path)
    for row in rows:
        LOG.info("  %-22s %10.2f %s", row.name, row.price_value or 0, row.currency)
    return len(rows)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape a price table into a clean CSV.")
    parser.add_argument("--url", help="Start URL")
    parser.add_argument("--item-selector", default="", help="CSS selector for each product block")
    parser.add_argument("--name-selector", default="h3", help="CSS selector for the product name")
    parser.add_argument("--price-selector", default=".price", help="CSS selector for the price")
    parser.add_argument("--stock-selector", default=None, help="Optional CSS selector for availability")
    parser.add_argument("--next-selector", default=None, help="CSS selector for the 'next page' link")
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between requests")
    parser.add_argument("--out", default="prices.csv")
    parser.add_argument("--ignore-robots", action="store_true")
    parser.add_argument("--demo", action="store_true", help="Run the offline demo")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    out_path = Path(args.out)

    if args.demo:
        return 0 if run_demo(out_path) else 1

    if not args.url or not args.item_selector:
        LOG.error("--url and --item-selector are required (or use --demo)")
        return 2

    session = build_session()
    if not args.ignore_robots and not robots_allowed(session, args.url):
        LOG.error("robots.txt forbids scraping %s. Use --ignore-robots only with written permission.", args.url)
        return 3

    all_rows: list[Row] = []
    url: str | None = args.url
    page = 1

    while url and page <= args.max_pages:
        LOG.info("Page %d: %s", page, url)
        html = fetch(session, url)
        rows = extract_rows(
            html,
            page_url=url,
            item_selector=args.item_selector,
            name_selector=args.name_selector,
            price_selector=args.price_selector,
            stock_selector=args.stock_selector,
        )
        LOG.info("  %d items", len(rows))
        all_rows.extend(rows)

        url = next_page_url(BeautifulSoup(html, "lxml"), args.next_selector, url)
        page += 1
        if url and page <= args.max_pages:
            time.sleep(args.delay)

    if not all_rows:
        LOG.error("No rows extracted. Check your selectors.")
        return 4

    write_csv(all_rows, out_path)
    with_price = sum(1 for r in all_rows if r.price_value is not None)
    LOG.info("Done: %d rows (%d with parsed price) -> %s", len(all_rows), with_price, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
