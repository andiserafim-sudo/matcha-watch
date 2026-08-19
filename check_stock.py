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
import sys

import requests

# ----------------- PRODUCTS -----------------
# type "woocommerce": parses per-variation stock JSON (Marukyu Koyamaen)
# type "ocnk": Japanese shops on ocnk.net, detects the out-of-stock text
PRODUCTS = [
    {
        "name": "Matcha Kinrin (金輪) from Marukyu Koyamaen Motoan shop (Japan)",
        "url": "https://www.marukyu-koyamaen.co.jp/motoan-shop/products/1151020c1/",
        "buy_url": "https://www.marukyu-koyamaen.co.jp/motoan-shop/products/1151020c1/",
        "type": "woocommerce",
        # Only alert for 40g and 100g. Marukyu SKUs encode grams in chars 5-7:
        # 1151020C1 = 20g can, 1151040C1 = 40g can, 1151100C1 = 100g can,
        # 1Fxx100C6 = 100g bag. We match the grams field, plus the variant
        # label (e.g. "40g", "100g") as a backup.
        "watch_grams": ["040", "100"],
    },
    {
        "name": "Matcha Wako (和光) from Marukyu Koyamaen Motoan shop (Japan)",
        "url": "https://www.marukyu-koyamaen.co.jp/motoan-shop/products/1161020c1/",
        "buy_url": "https://www.marukyu-koyamaen.co.jp/motoan-shop/products/1161020c1/",
        "type": "woocommerce",
        # 1161020C1=20g can, 1161040C1=40g can, 1161100C1=100g can,
        # 1161100C6=100g bag, 1161200C1=200g can. Alert only on 40g / 100g.
        "watch_grams": ["040", "100"],
    },
    {
        "name": "Matcha Shikibu no Mukashi from Yamamasa Koyamaen (official shop)",
        # Shopify JSON endpoint: exact per-variant availability
        "url": "https://yamamasa-koyamaen.com/products/matcha-shikibu.js",
        "buy_url": "https://yamamasa-koyamaen.com/products/matcha-shikibu?variant=42106992033960",
        "type": "shopify",
        # Alert on ANY size: 30g can, 100g bag, 150g can, 300g can
        "watch_variants": [],
    },
    {
        "name": "Matcha Samidori (さみどり) from Yamamasa Koyamaen (official shop)",
        "url": "https://yamamasa-koyamaen.com/products/matcha-samidori.js",
        "buy_url": "https://yamamasa-koyamaen.com/products/matcha-samidori",
        "type": "shopify",
        # Alert on ANY size: 30g can, 100g bag, 150g can, 300g can
        "watch_variants": [],
    },
]

# ntfy.sh push notifications: no account, no secrets. The topic name is the
# only "password", keep it private. Subscribe to this exact topic in the
# ntfy app on your phone to receive alerts.
NTFY_TOPIC = "matcha-andi-zyiz2e"

# Email alert via your own mailbox (SMTP). Set these as GitHub secrets:
#   MAIL_USER = address the alert is sent FROM (e.g. your Gmail)
#   MAIL_PASS = Gmail App Password (or SMTP password of that mailbox)
# Optional: MAIL_SERVER (default smtp.gmail.com), MAIL_PORT (default 465)
ALERT_TO = "andi@serafim.fr"
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
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,ja;q=0.8",
}

SIZE_LABELS = {
    "1161020C1": "20g can",
    "1161040C1": "40g can",
    "1161100C1": "100g can",
    "1161100C6": "100g bag",
    "1161200C1": "200g can",
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
    """Push notification via ntfy.sh (no account needed). The email alert is
    handled separately by the workflow, via a GitHub issue."""
    try:
        resp = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "high", "Tags": "tea"},
            timeout=20,
        )
        print(f"[info] ntfy push response: {resp.status_code}")
        if resp.status_code != 200:
            print(f"[warn] ntfy error body: {resp.text[:300]}")
    except Exception as e:
        print(f"[warn] ntfy send failed: {e}")


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
    "ocnk": check_ocnk,
    "shopify": check_shopify,
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
        subject = "TEST: matcha stock alert system works"
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

    for p in PRODUCTS:
        print(f"[info] Checking: {p['name']}")
        try:
            r = requests.get(p["url"], headers=HEADERS, timeout=30)
            print(f"[info]   HTTP {r.status_code}, {len(r.text)} bytes")
            if r.status_code != 200:
                warnings.append((p, f"HTTP {r.status_code}, could not check"))
                continue
            status, detail, keys = CHECKERS[p["type"]](r.text, p)
        except Exception as e:
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
    if alerts:
        if len(alerts) == 1:
            subject = f"{alerts[0][0]['name']} is available"
        else:
            subject = "Matcha in stock: " + " / ".join(a[0]["name"] for a in alerts)
    else:
        subject = "Matcha stock checker: page changed, please verify"

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

    print(f"[ALERT] Subject: {subject}\n{details}")
    send_push(subject, f"You can buy it now.\n\n{details}")
    ok = send_email(subject, f"You can buy it now.\n\n{details}")
    set_github_outputs(True, subject, details)
    # exit 1 (red run + GitHub backup email) ONLY if our own email failed
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
