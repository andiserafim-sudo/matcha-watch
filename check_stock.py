#!/usr/bin/env python3
"""
Multi-product stock checker (Marukyu Koyamaen + Fujiedaen).
When any watched product is in stock:
  - sends a push notification via ntfy.sh (no account or secrets needed)
  - exposes outputs (in_stock, subject, details) to GitHub Actions for the email step
  - exits with code 1 so the workflow also "fails" (GitHub backup notification)
"""

import html as htmllib
import json
import os
import re
import time
import sys

import requests

# ----------------- PRODUCTS -----------------
# type "woocommerce": parses per-variation stock JSON (Marukyu Koyamaen)
# type "ocnk": Japanese shops on ocnk.net, detects the out-of-stock text
PRODUCTS = [
    # --- Marukyu Koyamaen (English shop): alert on 40g and 100g only ---
    {
        "name": "Matcha Isuzu (五十鈴) from Marukyu Koyamaen English shop",
        "url": "https://www.marukyu-koyamaen.co.jp/english/shop/products/1191040c1",
        "buy_url": "https://www.marukyu-koyamaen.co.jp/english/shop/products/1191040c1",
        "type": "motoan",
        "watch_grams": ["040", "100"],
    },
    {
        "name": "Matcha Wako (和光) from Marukyu Koyamaen English shop",
        "url": "https://www.marukyu-koyamaen.co.jp/english/shop/products/1161020c1",
        "buy_url": "https://www.marukyu-koyamaen.co.jp/english/shop/products/1161020c1",
        "type": "motoan",
        "watch_grams": ["040", "100"],
    },
    {
        "name": "Matcha Kinrin (金輪) from Marukyu Koyamaen English shop",
        "url": "https://www.marukyu-koyamaen.co.jp/english/shop/products/1151020c1",
        "buy_url": "https://www.marukyu-koyamaen.co.jp/english/shop/products/1151020c1",
        "type": "motoan",
        "watch_grams": ["040", "100"],
    },
    # --- Marukyu Koyamaen via Sazen Tea (backup channel, ships to France) ---
    {
        "name": "Matcha Kinrin (金輪) from Marukyu Koyamaen, at Sazen Tea",
        "url": "https://www.sazentea.com/en/products/p155-matcha-kinrin.html",
        "buy_url": "https://www.sazentea.com/en/products/p155-matcha-kinrin.html",
        "type": "sazen",
    },
    {
        "name": "Matcha Wako (和光) from Marukyu Koyamaen, at Sazen Tea",
        "url": "https://www.sazentea.com/en/products/p156-matcha-wako.html",
        "buy_url": "https://www.sazentea.com/en/products/p156-matcha-wako.html",
        "type": "sazen",
    },
    {
        "name": "Matcha Isuzu (五十鈴) from Marukyu Koyamaen, at Sazen Tea",
        "url": "https://www.sazentea.com/en/products/p159-matcha-isuzu.html",
        "buy_url": "https://www.sazentea.com/en/products/p159-matcha-isuzu.html",
        "type": "sazen",
    },
    # --- Yamamasa Koyamaen via Sazen Tea (ships to France, DHL/FedEx) ---
    {
        "name": "Matcha Ogurayama (小倉山) from Yamamasa Koyamaen, at Sazen Tea",
        "url": "https://www.sazentea.com/en/products/p823-matcha-ogurayama.html",
        "buy_url": "https://www.sazentea.com/en/products/p823-matcha-ogurayama.html",
        "type": "sazen",
    },
    {
        "name": "Matcha Shikibu no Mukashi (式部の昔) from Yamamasa Koyamaen, at Sazen Tea",
        "url": "https://www.sazentea.com/en/products/p822-matcha-shikibu-no-mukashi.html",
        "buy_url": "https://www.sazentea.com/en/products/p822-matcha-shikibu-no-mukashi.html",
        "type": "sazen",
    },
    {
        "name": "Matcha Samidori (さみどり) from Yamamasa Koyamaen, at Sazen Tea",
        "url": "https://www.sazentea.com/en/products/p825-matcha-samidori.html",
        "buy_url": "https://www.sazentea.com/en/products/p825-matcha-samidori.html",
        "type": "sazen",
    },
    # --- Horii Shichimeien (official shop) ---
    {
        "name": "Matcha Todou no Mukashi (都昔) from Horii Shichimeien",
        "url": "https://horiishichimeien.com/en/products/matcha-todounomukashi.js",
        "buy_url": "https://horiishichimeien.com/en/products/matcha-todounomukashi",
        "type": "shopify",
        "watch_variants": [],
    },
    {
        "name": "Matcha Uji Mukashi (宇治昔) from Horii Shichimeien",
        "url": "https://horiishichimeien.com/en/products/matcha-ujimukashi.js",
        "buy_url": "https://horiishichimeien.com/en/products/matcha-ujimukashi",
        "type": "shopify",
        "watch_variants": [],
    },
]

# ntfy.sh push notifications: no account needed. The topic name is the only
# password, so it lives in the NTFY_TOPIC repository secret, never in this
# file. This repo can therefore be public. Subscribe to that same topic in
# the ntfy app on your phone.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# Email alert via your own mailbox (SMTP). Set these as GitHub secrets:
#   MAIL_USER = address the alert is sent FROM (e.g. your Gmail)
#   MAIL_PASS = Gmail App Password (or SMTP password of that mailbox)
# Optional: MAIL_SERVER (default smtp.gmail.com), MAIL_PORT (default 465)
ALERT_TO = os.environ.get("ALERT_TO") or os.environ.get("MAIL_USER", "")
MAIL_USER = os.environ.get("MAIL_USER", "")
MAIL_PASS = os.environ.get("MAIL_PASS", "")
MAIL_SERVER = os.environ.get("MAIL_SERVER") or "smtp.gmail.com"
MAIL_PORT = int(os.environ.get("MAIL_PORT") or "465")

# Set FORCE_TEST=1 to send a fake in-stock alert through the real
# notification pipeline (used by the "test" checkbox in GitHub Actions).
TEST_MODE = os.environ.get("FORCE_TEST", "") == "1"

# Set FORCE_CANARY=1 to run the REAL check but treat ANY variant as watched
# (including sizes normally filtered out, like the 30g can). If any product
# page really has stock, a real alert fires. Used by the "canary" checkbox.
CANARY_MODE = os.environ.get("FORCE_CANARY", "") == "1"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9,fr;q=0.8,ja;q=0.7",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# friendly shop names for alert subjects, keyed by host
SHOP_NAMES = {
    "sazentea.com": "Sazen Tea",
    "marukyu-koyamaen.co.jp": "Marukyu Koyamaen",
    "horiishichimeien.com": "Horii Shichimeien",
    "yamamasa-koyamaen.com": "Yamamasa Koyamaen",
}

SIZE_LABELS = {
    "1161020C1": "20g can",
    "1161040C1": "40g can",
    "1161100C1": "100g can",
    "1161100C6": "100g bag",
    "1161200C1": "200g can",
    "1191040C1": "40g can",
    "1191100C1": "100g can",
    "1F43100C6": "100g bag",
    "1191200C1": "200g can",
    "1171020C1": "20g can",
    "1171040C1": "40g can",
    "1171100C1": "100g can",
    "1171200C1": "200g can",
    "1151020C1": "20g can",
    "1151040C1": "40g can",
    "1151100C1": "100g can",
    "1151200C1": "200g can",
    "1191040C1": "40g can",
    "1191100C1": "100g can",
    "1F43100C6": "100g bag",
    "1191200C1": "200g can",
    "11A1040C1": "40g can",
    "11A1100C1": "100g can",
    "1F23100C6": "100g bag",
    "1F23200C1": "200g can",
}


def send_push(title: str, message: str) -> None:
    if not NTFY_TOPIC:
        print("[warn] NTFY_TOPIC secret not set, skipping push notification.")
        return
    """Push notification via ntfy.sh (no account needed).

    Published as JSON, not via HTTP headers: headers can only carry latin-1,
    so a title containing Japanese characters (e.g. 金輪) would raise an
    encoding error and the push would never be sent.
    """
    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": 5,
        "tags": ["tea"],
    }
    try:
        resp = requests.post(
            "https://ntfy.sh/",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=20,
        )
        print(f"[info] ntfy push response: {resp.status_code}")
        if resp.status_code == 200:
            return
        print(f"[warn] ntfy error body: {resp.text[:300]}")
    except Exception as e:
        print(f"[warn] ntfy JSON publish failed: {e}")

    # Fallback: plain publish with an ASCII-safe title
    try:
        safe_title = title.encode("ascii", "replace").decode("ascii")
        resp = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": safe_title, "Priority": "high", "Tags": "tea"},
            timeout=20,
        )
        print(f"[info] ntfy fallback response: {resp.status_code}")
    except Exception as e:
        print(f"[warn] ntfy fallback failed: {e}")


def check_woocommerce(page: str, product: dict):
    """Returns (status, detail_text). status: 'in_stock' | 'oos' | 'changed'."""
    m = re.search(r'data-product_variations="([^"]+)"', page)
    if m:
        try:
            variations = json.loads(htmllib.unescape(m.group(1)))
        except json.JSONDecodeError:
            variations = []
        watch = [] if CANARY_MODE else [s.upper() for s in product.get("watch_skus", [])]
        watch_g = [] if CANARY_MODE else product.get("watch_grams", [])
        avail = []
        keys = []
        for v in variations:
            sku = str(v.get("sku", "")).upper()
            label = " ".join(str(x) for x in (v.get("attributes") or {}).values())
            grams_ok = (not watch_g) or (sku[4:7] in watch_g) or any(
                re.search(r"(?<!\d)" + g.lstrip("0") + r"\s*g", label) for g in watch_g)
            if v.get("is_in_stock") and (not watch or sku in watch) and grams_ok:
                label = SIZE_LABELS.get(sku, sku)
                price = v.get("display_price", "?")
                avail.append(f"{label} (SKU {sku}, ¥{price})")
                keys.append(sku)
        if avail:
            return "in_stock", "Available sizes: " + ", ".join(avail), keys
        return "oos", "", []
    low = page.lower()
    oos_markers = ["out of stock and unavailable", "品切れ", "在庫切れ", "売り切れ", "入荷待ち"]
    if any(m in low or m in page for m in oos_markers):
        return "oos", "", []
    return "changed", "", []


def check_marukyu(page: str, product: dict):
    """Try the structured variations JSON first (English shop often has it),
    then fall back to per-SKU block parsing (Motoan template)."""
    # 1. structured per-variant JSON, when the shop exposes it
    if 'data-product_variations="' in page:
        status, detail, keys = check_woocommerce(page, product)
        if status == "in_stock":
            return status, detail, keys

    # 2. the English shop prints this exact sentence only when the WHOLE
    #    product is unavailable. Do not use generic markers here: on the
    #    Japanese shop 在庫切れ appears next to individual sold-out sizes.
    if "out of stock and unavailable" in page.lower():
        return "oos", "", []

    # 3. otherwise judge each size block on its own
    return check_motoan(page, product)


def check_motoan(page: str, product: dict):
    """Marukyu Koyamaen shop, Japanese (Motoan) or English. The page lists each size as a
    separate block: SKU, set name, price, then either 在庫切れ (sold out) or a
    quantity selector. We must judge EACH SKU on its own: the page always
    contains 在庫切れ somewhere as long as at least one size is sold out.
    Tag-agnostic on purpose, so a template tweak doesn't break it."""
    positions = [(m.start(), m.group(0)) for m in
                 re.finditer(r"\b1[0-9A-Z]{8}\b", page)]
    # keep only the first occurrence of each SKU (they repeat in scripts/links)
    seen, blocks = set(), []
    for pos, sku in positions:
        if sku not in seen:
            seen.add(sku)
            blocks.append((pos, sku))
    if not blocks:
        return "changed", "", []

    watch_g = [] if CANARY_MODE else [int(g) for g in product.get("watch_grams", [])]
    avail, keys = [], []
    for i, (pos, sku) in enumerate(blocks):
        end = blocks[i + 1][0] if i + 1 < len(blocks) else len(page)
        chunk = htmllib.unescape(page[pos:min(end, pos + 2500)])
        text = re.sub(r"<[^>]+>", " ", chunk)
        low = text.lower()
        if "在庫切れ" in text or "売り切れ" in text or "out of stock" in low \
           or "sold out" in low:
            continue  # this size is sold out
        gm = re.search(r"(\d+)\s*g\s*([缶袋])?", text)
        grams = int(gm.group(1)) if gm else None
        label = f"{gm.group(1)}g{gm.group(2) or ''}" if gm else SIZE_LABELS.get(sku, sku)
        if watch_g and grams not in watch_g:
            continue
        pm = re.search(r"[¥￥]\s*([\d,]+)", text)
        price = f", ¥{pm.group(1)}" if pm else ""
        avail.append(f"{label} (SKU {sku}{price})")
        keys.append(sku)

    if avail:
        return "in_stock", "Available sizes: " + ", ".join(avail), keys
    return "oos", "", []


def check_sazen(page: str, product: dict):
    """Sazen Tea product pages. One size per page.

    Everything is judged inside the product info block only (the text above
    "SHIPPING DETAILS"), because further down the page customer reviews often
    contain phrases like "out of stock" and other products are listed with
    their own prices.

    Sazen uses two different sold-out wordings depending on the maker:
      - "This product is unavailable at the moment" (Yamamasa items)
      - "We apologize, but this item is currently out of stock" (Marukyu items)
    """
    text = re.sub(r"<[^>]+>", " ", htmllib.unescape(page))
    cut = re.search(r"SHIPPING DETAILS", text, re.I)
    info = text[:cut.start()] if cut else text
    low = info.lower()

    oos_markers = [
        "unavailable at the moment",
        "this item is currently out of stock",
        "currently out of stock",
        "sold out",
    ]
    if any(m in low for m in oos_markers):
        return "oos", "", []

    if "add to cart" in low or "add-to-cart" in low:
        wm = re.search(r"Net weight:\s*([\d.]+\s*(?:g|kg))", info, re.I)
        size = wm.group(1).replace(" ", "") if wm else "available"
        bm = re.search(r"Best before:\s*([A-Z]{3}\s*/\s*\d{4})", info, re.I)
        best = f", best before {bm.group(1)}" if bm else ""
        pm = re.search(r"Unit price:\s*\$\s?([\d,]+\.\d{2})", info, re.I)
        price = f", ${pm.group(1)}" if pm else ""
        return "in_stock", f"Buyable at Sazen: {size}{price}{best}", ["item"]

    return "changed", "", []


def check_ocnk(page: str, product: dict):
    """Fujiedaen / ocnk.net shops. 欠品 = out of stock, カートに入れる = add-to-cart button."""
    if "欠品しております" in page or "欠品して" in page:
        return "oos", "", []
    if "カートに入れる" in page or 'name="quantity"' in page:
        return "in_stock", "Add-to-cart button is back on the page.", ["item"]
    return "changed", "", []


def check_shopify(page: str, product: dict):
    """Official Shopify shops: the product .js endpoint returns clean JSON
    with an 'available' boolean per variant."""
    try:
        data = json.loads(page)
    except json.JSONDecodeError:
        return "changed", "", []
    # log every variant with price and availability, so the run log doubles
    # as a price list even when everything is sold out
    for v in data.get("variants", []):
        pr = v.get("price")
        pr = f"¥{pr // 100:,}" if isinstance(pr, int) else "?"
        mark = "IN STOCK" if v.get("available") else "sold out"
        print(f"[price]   {v.get('title','?')}: {pr} ({mark})")

    watch = [] if CANARY_MODE else product.get("watch_variants", [])
    avail, keys = [], []
    for v in data.get("variants", []):
        title = str(v.get("title", ""))
        if v.get("available") and (not watch or any(w in title for w in watch)):
            price = v.get("price")
            price_str = f", ¥{price // 100:,}" if isinstance(price, int) else ""
            avail.append(f"{title}{price_str}")
            keys.append(str(v.get("id", title)))
    if avail:
        return "in_stock", "Available sizes: " + ", ".join(avail), keys
    if data.get("variants"):
        return "oos", "", []
    return "changed", "", []


def check_rakuten(page: str, product: dict):
    """Rakuten Ichiba item pages. Layer 1: JSON-LD offers.availability.
    Layer 2: Japanese/English sold-out or add-to-cart text markers."""
    # Layer 1: structured data
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for d in items:
            if not isinstance(d, dict):
                continue
            offers = d.get("offers")
            if isinstance(offers, dict):
                offers = [offers]
            if isinstance(offers, list):
                avail = " ".join(str(o.get("availability", "")) for o in offers if isinstance(o, dict))
                if "InStock" in avail:
                    return "in_stock", "Rakuten reports InStock", ["item"]
                if "OutOfStock" in avail or "SoldOut" in avail:
                    return "oos", "", []
    # Layer 2: text markers
    low = page.lower()
    if "sold out" in low or "売り切れ" in page or "在庫なし" in page or "再入荷" in page:
        return "oos", "", []
    if "かごに追加" in page or "買い物かごに入れる" in page or "add to cart" in low:
        return "in_stock", "Add-to-cart button present", ["item"]
    return "changed", "", []


CHECKERS = {
    "woocommerce": check_woocommerce,
    "motoan": check_marukyu,
    "ocnk": check_ocnk,
    "shopify": check_shopify,
    "sazen": check_sazen,
    "rakuten": check_rakuten,
}


STATE_FILE = os.environ.get("STATE_FILE") or "state.json"


def load_state() -> dict:
    """Remembers which sizes were already reported as in stock, so a size that
    simply STAYS in stock doesn't re-alert every hour."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        print(f"[info] state saved ({sum(len(v) for v in state.values())} items in stock)")
    except Exception as e:
        print(f"[warn] could not save state: {e}")


def send_email(subject: str, body: str) -> bool:
    """Send the alert email via SMTP (SSL). Returns True on success."""
    if not MAIL_USER or not MAIL_PASS:
        print("[warn] MAIL_USER / MAIL_PASS not set, skipping email.")
        return False
    import smtplib
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Matcha Watcher <{MAIL_USER}>"
    msg["To"] = ALERT_TO
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL(MAIL_SERVER, MAIL_PORT, timeout=30) as s:
            s.login(MAIL_USER, MAIL_PASS)
            s.send_message(msg)
        print(f"[info] email sent to {ALERT_TO} via {MAIL_SERVER}")
        return True
    except Exception as e:
        print(f"[warn] email failed: {e}")
        return False


def set_github_outputs(in_stock: bool, subject: str, details: str) -> None:
    out_path = os.environ.get("GITHUB_OUTPUT")
    if not out_path:
        return
    with open(out_path, "a", encoding="utf-8") as f:
        f.write(f"in_stock={'true' if in_stock else 'false'}\n")
        f.write(f"subject={subject}\n")
        f.write("details<<EOD\n")
        f.write(details + "\n")
        f.write("EOD\n")


def main() -> int:
    if TEST_MODE:
        subject = "Matcha - TEST: alert system works"
        details = (
            "This is a TEST alert sent through the real notification pipeline.\n"
            "If you received this as a phone push AND as an email, everything works.\n\n"
            "Products being monitored:\n"
            + "\n".join(f"- {p['name']}\n  {p['buy_url']}" for p in PRODUCTS)
        )
        print("[TEST] Sending test alert...")
        send_push(subject, details)
        ok = send_email(subject, "You can buy it now.\n\n" + details)
        set_github_outputs(True, subject, details)
        return 0 if ok else 1

    if CANARY_MODE:
        print("[CANARY] Real check, all size filters disabled for this run.")

    old_state = {} if CANARY_MODE else load_state()
    new_state = {}

    alerts = []      # products with NEWLY available sizes
    warnings = []    # pages changed / errors, need manual review

    for i, p in enumerate(PRODUCTS):
        if i:
            time.sleep(3)   # be a polite visitor, not a hammering bot
        print(f"[info] Checking: {p['name']}")
        try:
            r = requests.get(p["url"], headers=HEADERS, timeout=30)
            print(f"[info]   HTTP {r.status_code}, {len(r.text)} bytes")
            if r.status_code != 200:
                new_state[p["url"]] = ["__http_error__"]
                if "__http_error__" not in set(old_state.get(p["url"], [])):
                    warnings.append((p, f"HTTP {r.status_code}, could not check"))
                continue
            status, detail, keys = CHECKERS[p["type"]](r.text, p)
        except Exception as e:
            new_state[p["url"]] = ["__http_error__"]
            if "__http_error__" not in set(old_state.get(p["url"], [])):
                warnings.append((p, f"error: {e}"))
            continue

        pkey = p["url"]
        already = set(old_state.get(pkey, []))

        if status == "in_stock":
            new_state[pkey] = keys
            fresh = [k for k in keys if k not in already]
            if fresh or CANARY_MODE:
                print(f"[ALERT]   IN STOCK (new). {detail}")
                alerts.append((p, detail))
            else:
                print(f"[info]   In stock, but already reported earlier. {detail}")
        elif status == "oos":
            print("[info]   Out of stock.")
        else:
            print("[warn]   Page structure changed, needs manual review.")
            # show what the runner actually received, so we can tell a real
            # template change from a bot-block page served with HTTP 200
            snippet = re.sub(r"<[^>]+>", " ", r.text[:4000])
            snippet = re.sub(r"\s+", " ", snippet).strip()
            print(f"[debug]   first 300 chars seen: {snippet[:300]}")
            # remember it, so a permanently changed page doesn't alert hourly
            new_state[pkey] = ["__page_changed__"]
            if "__page_changed__" not in already:
                warnings.append((p, "page structure changed, may be back in stock"))

    if not CANARY_MODE:
        save_state(new_state)

    if not alerts and not warnings:
        set_github_outputs(False, "", "")
        return 0

    # Build subject and body
    # Subject prefixes make the two cases unmistakable at a glance:
    #   "Matcha - STOCK AVAILABLE" = something is actually buyable
    #   "Matcha - BLOCKED"         = a shop could not be read (block, error,
    #                                 or template change), nothing to buy
    if alerts:
        names = " / ".join(a[0]["name"].split(" from ")[0] for a in alerts)
        subject = f"Matcha - STOCK AVAILABLE: {names}"
        if warnings:
            subject += f" (+{len(warnings)} shop(s) unreadable)"
    else:
        shops = []
        for w, _ in warnings:
            host = re.sub(r"^https?://(www\.)?([^/]+).*", r"\2", w["url"])
            shop = SHOP_NAMES.get(host, host)
            if shop not in shops:
                shops.append(shop)
        subject = f"Matcha - BLOCKED: could not read {' / '.join(shops)}"

    lines = []
    for p, detail in alerts:
        lines.append(f"IN STOCK: {p['name']}")
        if detail:
            lines.append(f"  {detail}")
        lines.append(f"  Buy: {p['buy_url']}")
        lines.append("")
    for p, why in warnings:
        lines.append(f"CHECK MANUALLY: {p['name']} ({why})")
        lines.append(f"  {p['buy_url']}")
        lines.append("")
    details = "\n".join(lines).strip()

    lead = ("You can buy it now." if alerts else
            "Nothing to buy. One or more shops could not be read: this is a "
            "block, an error, or a page change, not a restock.")
    print(f"[ALERT] Subject: {subject}\n{details}")
    send_push(subject, f"{lead}\n\n{details}")
    ok = send_email(subject, f"{lead}\n\n{details}")
    set_github_outputs(True, subject, details)
    # exit 1 (red run + GitHub backup email) ONLY if our own email failed
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
