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
        "name": "Matcha Isuzu from Marukyu Koyamaen",
        "url": "https://www.marukyu-koyamaen.co.jp/english/shop/products/1191040c1",
        "buy_url": "https://www.marukyu-koyamaen.co.jp/english/shop/products/1191040c1?currency=EUR",
        "type": "woocommerce",
        # SKUs to watch, [] = any size. 1191040C1=40g can, 1191100C1=100g can,
        # 1F43100C6=100g bag, 1191200C1=200g can
        "watch_skus": [],
    },
    {
        "name": "Matcha Aoarashi from Marukyu Koyamaen",
        "url": "https://www.marukyu-koyamaen.co.jp/english/shop/products/11a1040c1",
        "buy_url": "https://www.marukyu-koyamaen.co.jp/english/shop/products/11a1040c1?currency=EUR",
        "type": "woocommerce",
        # 11A1040C1=40g can, 11A1100C1=100g can, 1F23100C6=100g bag, 1F23200C1=200g can
        "watch_skus": [],
    },
    {
        "name": "Matcha Shikibu no Mukashi 150g (Yamamasa Koyamaen) from Fujiedaen",
        "url": "https://fujiedaen.ocnk.net/product/954",
        "buy_url": "https://fujiedaen.ocnk.net/product/954",
        "type": "ocnk",
    },
    {
        "name": "Matcha Ogurayama 150g (Yamamasa Koyamaen) from Fujiedaen",
        "url": "https://fujiedaen.ocnk.net/product/953",
        "buy_url": "https://fujiedaen.ocnk.net/product/953",
        "type": "ocnk",
    },
    {
        "name": "Matcha Shikibu no Mukashi from Yamamasa Koyamaen (official shop)",
        # Shopify JSON endpoint: exact per-variant availability
        "url": "https://yamamasa-koyamaen.com/products/matcha-shikibu.js",
        "buy_url": "https://yamamasa-koyamaen.com/collections/matcha/products/matcha-shikibu",
        "type": "shopify",
        # Only alert for sizes >30g. Substring match against variant titles
        # (30g缶, 150g缶, 300g缶, 100g袋). "30g" alone would also match 300g/130g
        # patterns, so we exclude it by listing only the wanted ones.
        "watch_variants": ["100g", "150g", "300g"],
    },
]

# ntfy.sh push notifications: no account, no secrets. The topic name is the
# only "password", keep it private. Subscribe to this exact topic in the
# ntfy app on your phone to receive alerts.
NTFY_TOPIC = "matcha-andi-zyiz2e"

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
        avail = []
        for v in variations:
            sku = str(v.get("sku", "")).upper()
            if v.get("is_in_stock") and (not watch or sku in watch):
                label = SIZE_LABELS.get(sku, sku)
                price = v.get("display_price", "?")
                avail.append(f"{label} (SKU {sku}, ¥{price})")
        if avail:
            return "in_stock", "Available sizes: " + ", ".join(avail)
        return "oos", ""
    if "out of stock and unavailable" in page.lower():
        return "oos", ""
    return "changed", ""


def check_ocnk(page: str, product: dict):
    """Fujiedaen / ocnk.net shops. 欠品 = out of stock, カートに入れる = add-to-cart button."""
    if "欠品しております" in page or "欠品して" in page:
        return "oos", ""
    if "カートに入れる" in page or 'name="quantity"' in page:
        return "in_stock", "Add-to-cart button is back on the page."
    return "changed", ""


def check_shopify(page: str, product: dict):
    """Official Shopify shops: the product .js endpoint returns clean JSON
    with an 'available' boolean per variant."""
    try:
        data = json.loads(page)
    except json.JSONDecodeError:
        return "changed", ""
    watch = [] if CANARY_MODE else product.get("watch_variants", [])
    avail = []
    for v in data.get("variants", []):
        title = str(v.get("title", ""))
        if v.get("available") and (not watch or any(w in title for w in watch)):
            price = v.get("price")
            price_str = f", ¥{price // 100:,}" if isinstance(price, int) else ""
            avail.append(f"{title}{price_str}")
    if avail:
        return "in_stock", "Available sizes: " + ", ".join(avail)
    if data.get("variants"):
        return "oos", ""
    return "changed", ""


CHECKERS = {"woocommerce": check_woocommerce, "ocnk": check_ocnk, "shopify": check_shopify}


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
        print(f"[TEST] Sending test alert...")
        send_push(subject, details)
        set_github_outputs(True, subject, details)
        return 1

    if CANARY_MODE:
        print("[CANARY] Real check, all size filters disabled for this run.")

    alerts = []      # products in stock
    warnings = []    # pages changed / errors, need manual review

    for p in PRODUCTS:
        print(f"[info] Checking: {p['name']}")
        try:
            r = requests.get(p["url"], headers=HEADERS, timeout=30)
            print(f"[info]   HTTP {r.status_code}, {len(r.text)} bytes")
            if r.status_code != 200:
                warnings.append((p, f"HTTP {r.status_code}, could not check"))
                continue
            status, detail = CHECKERS[p["type"]](r.text, p)
        except Exception as e:
            warnings.append((p, f"error: {e}"))
            continue

        if status == "in_stock":
            print(f"[ALERT]   IN STOCK. {detail}")
            alerts.append((p, detail))
        elif status == "oos":
            print("[info]   Out of stock.")
        else:
            print("[warn]   Page structure changed, needs manual review.")
            warnings.append((p, "page structure changed, may be back in stock"))

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
    set_github_outputs(True, subject, details)
    return 1


if __name__ == "__main__":
    sys.exit(main())
