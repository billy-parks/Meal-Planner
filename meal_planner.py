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

POSTAL_CODE = "K7C3T3"  # Carleton Place, Ontario

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


def check_flipp_sales(items: list[str]) -> dict[str, list[tuple[str, str]]]:
    """
    Fetch current flyers from Flipp for Carleton Place and return shopping list
    items that are on sale at Walmart, FreshCo, or Your Independent Grocer.

    Returns {original_item_text: [(store_display_name, price_string), ...]}
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
    target_flyers: list[tuple[int, str]] = []
    for flyer in flyers:
        merchant = (flyer.get("merchant") or "").lower()
        display = next((d for kw, d in FLIPP_STORE_MAP.items() if kw in merchant), None)
        if display and "Groceries" in flyer.get("categories", []):
            target_flyers.append((flyer["id"], display))

    if not target_flyers:
        return {}

    # Step 2 — fetch all items from each target store's flyer
    sale_catalog: list[tuple[str, str, str]] = []  # (item_name_lower, store, price_str)
    for flyer_id, store_display in target_flyers:
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
                if name and price_str:
                    sale_catalog.append((name.lower(), store_display, price_str))
        except Exception:
            continue

    # Step 3 — match shopping list items against the sale catalog
    sales: dict[str, list] = {}
    for item in items:
        term = _simplify_for_search(item).lower()
        if not term or len(term) < 3:
            continue
        for sale_name, store_display, price_str in sale_catalog:
            if term in sale_name or sale_name in term:
                if item not in sales:
                    sales[item] = []
                if not any(s == store_display for s, _ in sales[item]):
                    sales[item].append((store_display, price_str))

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
                    tags = ", ".join(f"{store} ({price})" for store, price in store_sales)
                    line = line.rstrip() + f" [ON SALE: {tags}]"
                    break

        result.append(line)

    return "\n".join(result)


def format_html_email(meal_plan_text: str) -> str:
    """Wrap the plain-text meal plan in a clean HTML email."""
    lines = meal_plan_text.split("\n")
    html_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            html_lines.append("<br>")
        elif stripped.startswith("WEEK OF"):
            html_lines.append(f'<h1 style="color:#2d6a4f;font-family:Georgia,serif;">'
                              f'🍽️ {stripped}</h1>')
        elif stripped.startswith("---") and stripped.endswith("---"):
            section = stripped.strip("-").strip()
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
            item_text = stripped[1:].strip()
            sale_match = re.search(r"\[ON SALE: (.*?)\]$", item_text)
            if sale_match:
                clean = item_text[:sale_match.start()].strip()
                badge = (
                    f'<span style="background:#e63946;color:white;font-size:11px;'
                    f'padding:1px 7px;border-radius:10px;font-weight:bold;'
                    f'margin-left:6px;">🏷️ ON SALE: {sale_match.group(1)}</span>'
                )
                html_lines.append(f'<li style="margin:2px 0;">{clean}{badge}</li>')
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
    html = format_html_email(meal_plan)
    send_email(html, meal_plan)


if __name__ == "__main__":
    main()
