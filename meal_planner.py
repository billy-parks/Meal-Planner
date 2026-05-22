import anthropic
import smtplib
import os
import re
import random
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta

# ── Configuration (set these as Railway environment variables) ──────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GMAIL_ADDRESS     = os.environ["GMAIL_ADDRESS"]       # your Gmail address
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"] # Gmail App Password (not your login password)
RECIPIENT_EMAIL   = os.environ.get("RECIPIENT_EMAIL", GMAIL_ADDRESS)

POSTAL_CODE = "K7C3T3"   # Carleton Place, Ontario
CITY_SLUG   = "carleton-place-on"

FLIPP_BASE = "https://flyers-ng.flippback.com/api/flipp"

# Merchant name fragments (lowercase) → display names used in the email
FLIPP_STORE_MAP = {
    "walmart":            "Walmart",
    "freshco":            "FreshCo",
    "independent grocer": "Your Independent Grocer",
    "your independent":   "Your Independent Grocer",
}

FAMILY_PROFILE = """
- 3 people: 2 adults and one 8-year-old picky eater
- The child dislikes: strong spices, mixed textures, visible onions/mushrooms,
  most seafood, and pizza
- The child likes: pasta, hot dogs, tacos, chicken tenders, mild flavours, 
  anything with cheese, grilled cheese, simple sides like corn, brocolli or plain rice
- Adults enjoy a wider variety but prefer simple weeknight meals, including BBQ recipes
- Budget-conscious: no expensive specialty ingredients
- Mix of quick meals (under 30 min) and one or two weekend-style meals
"""

# ────────────────────────────────────────────────────────────────────────────

def generate_meal_plan() -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    next_monday = datetime.now() + timedelta(days=(7 - datetime.now().weekday()))
    week_label  = next_monday.strftime("%B %d, %Y")

    prompt = f"""
You are a friendly family meal planner. Generate a 7-day dinner meal plan 
for the week of {week_label} for the following family:

{FAMILY_PROFILE}

Format your response EXACTLY like this (use plain text, no markdown):

WEEK OF {week_label}
===================

MONDAY: [Meal Name]
TUESDAY: [Meal Name]
WEDNESDAY: [Meal Name]
THURSDAY: [Meal Name]
FRIDAY: [Meal Name]
SATURDAY: [Meal Name]
SUNDAY: [Meal Name]

---RECIPES---

[For each day, provide:]
[DAY]: [Meal Name]
Prep time: X min | Cook time: X min | Kid-friendly: Yes/Mostly/With modification
Ingredients (serves 3):
- item
- item
Instructions:
1. Step
2. Step
Kid tip: [one sentence on how to make it work for a picky eater]

---SHOPPING LIST---

Produce:
- item

Meat & Seafood:
- item

Dairy & Eggs:
- item

Pantry & Dry Goods:
- item

Frozen:
- item

Other:
- item

---MEAL PREP TIPS---
[2-3 sentences of weekend prep tips to make the week easier]
"""

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text


def _extract_shopping_items(meal_plan_text: str) -> list[str]:
    """Return every bullet from the ---SHOPPING LIST--- section."""
    items, in_section = [], False
    for line in meal_plan_text.split("\n"):
        stripped = line.strip()
        if stripped == "---SHOPPING LIST---":
            in_section = True
            continue
        if in_section:
            if stripped.startswith("---"):
                break
            if stripped.startswith("-"):
                items.append(stripped[1:].strip())
    return items


def _simplify_for_search(item: str) -> str:
    """Strip quantities/units and return first 1-3 meaningful words."""
    item = re.sub(r"\(.*?\)", "", item)
    item = re.sub(
        r"\b\d+(\.\d+)?\s*(g|kg|lb|lbs|oz|ml|L|litre|cup|cups|tsp|tbsp|"
        r"bunch|cloves?|cans?|pkg|packages?|slices?|heads?)\b",
        "", item, flags=re.IGNORECASE,
    )
    item = re.sub(r"^\d+\s*", "", item.strip())
    words = item.split()[:3]
    return " ".join(words).strip()


def _slugify(text: str) -> str:
    """Convert a string to a URL-safe slug (lowercase, hyphens, no repeated hyphens)."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return slug.strip("-")


def _flipp_item_url(item_id: int, merchant: str, flyer_name: str) -> str:
    """Build a working flipp.com item URL for the given item."""
    return (
        f"https://flipp.com/en-ca/{CITY_SLUG}/item/"
        f"{item_id}-{_slugify(merchant)}-{_slugify(flyer_name)}"
        f"?postal_code={POSTAL_CODE}"
    )


def _items_match(search_term: str, sale_name: str) -> bool:
    """
    True when every word in search_term appears as a whole word in sale_name.
    Uses word boundaries so 'corn' does not match 'popcorn'.
    """
    words = [w for w in re.split(r"\W+", search_term) if len(w) >= 3]
    if not words:
        return False
    return all(re.search(r"\b" + re.escape(w) + r"\b", sale_name, re.IGNORECASE) for w in words)


def check_flipp_sales(items: list[str]) -> dict[str, list[tuple]]:
    """
    Fetch current flyers from Flipp for Carleton Place and return shopping list
    items that are on sale at Walmart, FreshCo, or Your Independent Grocer.

    Returns {original_item_text: [(store, price, image_url, valid_to_str, flyer_url), ...]}
    Returns an empty dict (rather than raising) if Flipp cannot be reached.
    """
    sid = "".join(str(random.randint(0, 9)) for _ in range(16))
    params = {"locale": "en", "postal_code": POSTAL_CODE, "sid": sid}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    # Step 1 — discover flyers for our postal code
    resp = requests.get(f"{FLIPP_BASE}/data", params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    flyers = data.get("flyers") or []
    # Store (flyer_id, display_name, raw_merchant_name, flyer_name)
    target_flyers: list[tuple[int, str, str, str]] = []
    for flyer in flyers:
        merchant = (flyer.get("merchant") or "").lower()
        display = next((d for kw, d in FLIPP_STORE_MAP.items() if kw in merchant), None)
        if display and "Groceries" in flyer.get("categories", []):
            target_flyers.append((
                flyer["id"],
                display,
                flyer.get("merchant") or display,
                flyer.get("name") or "",
            ))

    if not target_flyers:
        return {}

    # Step 2 — fetch all items from each target store's flyer
    # catalog entry: (name_lower, store, price_str, image_url, valid_to_str, item_url)
    sale_catalog: list[tuple] = []
    for flyer_id, store_display, merchant_name, flyer_name in target_flyers:
        try:
            r = requests.get(
                f"{FLIPP_BASE}/flyers/{flyer_id}/flyer_items",
                params=params, headers=headers, timeout=15,
            )
            if r.status_code != 200:
                continue
            for fi in r.json():
                name = (fi.get("name") or "").strip()
                price = fi.get("price") or fi.get("current_price")
                try:
                    price_str = f"${float(price):.2f}" if price else ""
                except (ValueError, TypeError):
                    price_str = str(price)
                if not name or not price_str:
                    continue
                image_url = fi.get("cutout_image_url") or ""
                valid_to_str = ""
                raw_date = fi.get("valid_to") or ""
                if raw_date:
                    try:
                        dt = datetime.fromisoformat(raw_date)
                        valid_to_str = dt.strftime("%b") + " " + str(dt.day)
                    except (ValueError, AttributeError):
                        valid_to_str = raw_date[:10]
                item_url = _flipp_item_url(fi["id"], merchant_name, flyer_name)
                sale_catalog.append((name.lower(), store_display, price_str, image_url, valid_to_str, item_url))
        except Exception:
            continue

    # Step 3 — match shopping list items against the sale catalog
    sales: dict[str, list] = {}
    for item in items:
        term = _simplify_for_search(item).lower()
        if not term or len(term) < 3:
            continue
        for name_lower, store_display, price_str, image_url, valid_to_str, flyer_url in sale_catalog:
            if _items_match(term, name_lower):
                if item not in sales:
                    sales[item] = []
                if not any(s == store_display for s, *_ in sales[item]):
                    sales[item].append((store_display, price_str, image_url, valid_to_str, flyer_url))

    return sales


def annotate_shopping_list(meal_plan_text: str, sales: dict[str, list]) -> str:
    """Append [ON SALE: ...] markers to matching lines in the shopping list."""
    if not sales:
        return meal_plan_text

    lines = meal_plan_text.split("\n")
    in_section = False
    result = []

    for line in lines:
        stripped = line.strip()
        if stripped == "---SHOPPING LIST---":
            in_section = True
        elif in_section and stripped.startswith("---"):
            in_section = False

        if in_section and stripped.startswith("-"):
            item_text = stripped[1:].strip()
            item_lower = item_text.lower()
            for sale_item, store_sales in sales.items():
                if sale_item.lower() in item_lower or item_lower in sale_item.lower():
                    tags = ", ".join(f"{t[0]} ({t[1]})" for t in store_sales)
                    line = line.rstrip() + f" [ON SALE: {tags}]"
                    break

        result.append(line)

    return "\n".join(result)


def _sale_card_html(store_sales: list[tuple]) -> str:
    """Render one sale card per store as a compact HTML block."""
    cards = []
    for store, price, image_url, valid_to_str, flyer_url in store_sales:
        end_line = f'<span style="color:#888;font-size:10px;">Sale ends {valid_to_str}</span>' if valid_to_str else ""
        img_tag = (
            f'<img src="{image_url}" width="44" height="44" '
            f'style="object-fit:contain;border-radius:4px;margin-right:8px;vertical-align:middle;" />'
            if image_url else ""
        )
        cards.append(
            f'<div style="display:inline-flex;align-items:center;background:#fff5f5;'
            f'border:1px solid #e63946;border-radius:8px;padding:4px 10px 4px 6px;'
            f'margin:3px 4px 0 0;font-size:12px;vertical-align:middle;">'
            f'{img_tag}'
            f'<span>'
            f'<strong style="color:#e63946;">&#x1F3F7;&#xFE0F; ON SALE</strong> &nbsp;'
            f'<a href="{flyer_url}" style="color:#1b2e22;text-decoration:none;font-weight:bold;">'
            f'{store} &mdash; {price}</a><br>'
            f'{end_line}'
            f'</span>'
            f'</div>'
        )
    return "".join(cards)


def format_html_email(meal_plan_text: str, sales: dict | None = None) -> str:
    """Wrap the plain-text meal plan in a clean HTML email."""
    lines = meal_plan_text.split("\n")
    html_lines = []
    in_shopping = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            html_lines.append("<br>")
        elif stripped.startswith("WEEK OF"):
            html_lines.append(f'<h1 style="color:#2d6a4f;font-family:Georgia,serif;">'
                              f'🍽️ {stripped}</h1>')
        elif stripped.startswith("---") and stripped.endswith("---"):
            section = stripped.strip("-").strip()
            in_shopping = (section == "SHOPPING LIST")
            html_lines.append(f'<h2 style="color:#1b4332;border-bottom:2px solid #95d5b2;'
                              f'padding-bottom:4px;font-family:Georgia,serif;">{section}</h2>')
        elif any(stripped.startswith(day + ":") for day in
                 ["MONDAY","TUESDAY","WEDNESDAY","THURSDAY","FRIDAY","SATURDAY","SUNDAY"]):
            html_lines.append(f'<p style="font-size:16px;font-weight:bold;color:#40916c;">'
                              f'{stripped}</p>')
        elif stripped.startswith("Kid tip:"):
            html_lines.append(f'<p style="background:#d8f3dc;padding:6px 10px;border-radius:6px;'
                              f'font-size:14px;">🧒 {stripped}</p>')
        elif stripped.startswith("-"):
            # Strip the legacy text annotation if present
            item_text = re.sub(r"\s*\[ON SALE:.*?\]$", "", stripped[1:].strip())
            sale_html = ""
            if in_shopping and sales:
                item_lower = item_text.lower()
                for sale_item, store_sales in sales.items():
                    if sale_item.lower() in item_lower or item_lower in sale_item.lower():
                        sale_html = _sale_card_html(store_sales)
                        break
            if sale_html:
                html_lines.append(
                    f'<li style="margin:6px 0;">'
                    f'<span style="font-weight:500;">{item_text}</span><br>'
                    f'{sale_html}'
                    f'</li>'
                )
            else:
                html_lines.append(f'<li style="margin:2px 0;">{item_text}</li>')
        elif stripped[0].isdigit() and ". " in stripped:
            html_lines.append(f'<li style="margin:3px 0;">{stripped}</li>')
        else:
            html_lines.append(f'<p style="margin:4px 0;">{stripped}</p>')

    body = "\n".join(html_lines)

    return f"""
    <html><body style="font-family:Arial,sans-serif;max-width:680px;margin:auto;
                       padding:20px;color:#1b2e22;background:#f0faf4;">
      <div style="background:white;border-radius:12px;padding:30px;
                  box-shadow:0 2px 12px rgba(0,0,0,0.08);">
        <p style="color:#52b788;font-size:14px;">
          Your weekly meal plan is ready! Reply to this email with any changes 
          and I'll update it for you.
        </p>
        {body}
        <hr style="margin-top:30px;border:none;border-top:1px solid #d8f3dc;">
        <p style="font-size:12px;color:#888;">
          Generated by your family meal planner · Powered by Claude AI
        </p>
      </div>
    </body></html>
    """


import resend

def send_email(html_content: str, plain_text: str):
    resend.api_key = os.environ["RESEND_API_KEY"]
    next_monday = datetime.now() + timedelta(days=(7 - datetime.now().weekday()))
    subject = f"🍽️ Meal Plan for the Week of {next_monday.strftime('%B %d')}"

    resend.Emails.send({
        "from": "Meal Planner <meals@parksmealplans.shop>",
        "to": [e.strip() for e in os.environ["RECIPIENT_EMAILS"].split(",")],
        "subject": subject,
        "html": html_content,
    })
    print(f"✅ Meal plan sent!")

def main():
    print("🥗 Generating meal plan...")
    meal_plan = generate_meal_plan()

    print("🏷️  Checking local flyers (Walmart, FreshCo, Your Independent Grocer)...")
    shopping_items = _extract_shopping_items(meal_plan)
    sales: dict = {}
    try:
        sales = check_flipp_sales(shopping_items)
        if sales:
            print(f"   Found sales on {len(sales)} item(s).")
            meal_plan = annotate_shopping_list(meal_plan, sales)
        else:
            print("   No matching sales found this week.")
    except Exception as exc:
        print(f"   Sale check skipped: {exc}")

    print("📧 Sending email...")
    html = format_html_email(meal_plan, sales=sales or None)
    send_email(html, meal_plan)


if __name__ == "__main__":
    main()
