from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import io
import os
import re
import pandas as pd
from playwright.sync_api import sync_playwright
from pypdf import PdfReader
import requests

CSV_FILE = "scratchcards.csv"
CATALOG_URL = "https://www.national-lottery.co.uk/scratchcards/all-scratchcards"


def parse_pdf_procedures(pdf_url):
  """Downloads procedures PDF in-memory to extract Game Name, Game ID, Total Printed, and Release Date."""
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
      )
  }
  release_date = None
  total_printed = None
  pdf_game_name = None
  pdf_game_id = None

  try:
    # 1. HTTP Last-Modified Header
    head_resp = requests.head(pdf_url, headers=headers, timeout=10)
    if "Last-Modified" in head_resp.headers:
      dt = parsedate_to_datetime(head_resp.headers["Last-Modified"])
      release_date = dt.strftime("%Y-%m-%d")

    # 2. Download and Read PDF Stream
    get_resp = requests.get(pdf_url, headers=headers, timeout=15)
    get_resp.raise_for_status()

    pdf_stream = io.BytesIO(get_resp.content)
    reader = PdfReader(pdf_stream)

    if not release_date and reader.metadata:
      raw_meta = reader.metadata.get("/CreationDate") or reader.metadata.get(
          "/ModDate"
      )
      if raw_meta:
        match = re.search(r"D:(\d{4})(\d{2})(\d{2})", str(raw_meta))
        if match:
          release_date = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"

    full_text = ""
    for page in reader.pages:
      full_text += page.extract_text() or ""

    # Extract Game Name
    name_m = re.search(
        r"Game\s+Name:\s*[“\"״]([^”\"״\n\r]+)[”\"״]", full_text, re.IGNORECASE
    )
    if not name_m:
      name_m = re.search(r"Game\s+Name:\s*([^\n\r]+)", full_text, re.IGNORECASE)
    if name_m:
      pdf_game_name = name_m.group(1).strip(" “\"״\t")

    # Extract Game ID
    num_m = re.search(
        r"Game\s+Number:\s*[“\"״]?Game\s*(\d+)[”\"״]?",
        full_text,
        re.IGNORECASE,
    )
    if num_m:
      pdf_game_id = num_m.group(1).strip()

    # Extract Total Printed
    print_m = re.search(
        r"([\d,]+)\s+Scratchcards\s+in\s+the\s+initial\s+print\s+run",
        full_text,
        re.IGNORECASE,
    )
    if print_m:
      total_printed = int(print_m.group(1).replace(",", ""))

  except Exception as e:
    print(f"    [!] PDF parse error for {pdf_url}: {e}")

  return pdf_game_name, pdf_game_id, total_printed, release_date


def calculate_stats(
    printed,
    jackpots_init,
    jackpots_left,
    release_date_str,
    expected_lifecycle=240,
):
  """Computes estimated cards left, current odds, and advantage ratio."""
  if not printed or not jackpots_init or jackpots_init <= 0:
    return "N/A", "N/A", "N/A"

  days_elapsed = None
  if release_date_str and release_date_str != "N/A":
    try:
      rel_date = datetime.strptime(release_date_str, "%Y-%m-%d")
      days_elapsed = max(
          0, (datetime.now(timezone.utc).replace(tzinfo=None) - rel_date).days
      )
    except Exception:
      pass

  if days_elapsed is not None:
    time_decay = max(0.05, 1.0 - (days_elapsed / expected_lifecycle))
    jackpot_ratio = jackpots_left / jackpots_init
    blended = (0.6 * time_decay) + (0.4 * jackpot_ratio)
    est_remaining = int(printed * blended)
  else:
    claimed = jackpots_init - jackpots_left
    est_remaining = int(printed * (1.0 - (claimed / (jackpots_init + 1))))

  est_remaining = max(1000, min(printed, est_remaining))

  if jackpots_left > 0:
    current_odds = round(est_remaining / jackpots_left, 0)
    base_odds = printed / jackpots_init
    advantage_ratio = round(base_odds / current_odds, 3)
  else:
    current_odds = "No Top Prizes"
    advantage_ratio = 0.0

  return est_remaining, current_odds, advantage_ratio


def run_scraper():
  print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting Scraper...")

  cached_games = {}
  if os.path.exists(CSV_FILE):
    try:
      prev_df = pd.read_csv(CSV_FILE)
      for _, row in prev_df.iterrows():
        url = str(row.get("procedures_url"))
        if url and pd.notna(row.get("total_printed")):
          cached_games[url] = row.to_dict()
      print(f"Loaded {len(cached_games)} cached games.")
    except Exception as e:
      print(f"Cache load skipped: {e}")

  scraped_records = []

  with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox"],
    )
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
    )
    page = context.new_page()

    print(f"Opening {CATALOG_URL}...")
    page.goto(CATALOG_URL, wait_until="domcontentloaded", timeout=60000)

    # 1. Accept Cookie Banner
    try:
      cookie_btn = page.locator(
          "#onetrust-accept-btn-handler, button:has-text('Accept All Cookies')"
      )
      if cookie_btn.count() > 0:
        cookie_btn.first.click(timeout=5000)
    except Exception:
      pass

    # 2. Wait for Scratchcards
    try:
      page.wait_for_selector(
          "text=top prizes left, text=Read procedures", timeout=30000
      )
    except Exception as e:
      print(f"Timeout waiting for elements: {e}")

    # 3. Locate all procedure links
    proc_links = page.locator(
        "a:has-text('Read procedures'), a:has-text('procedures')"
    ).all()
    print(f"Found {len(proc_links)} scratchcard links.")

    for link in proc_links:
      try:
        proc_href = link.get_attribute("href") or ""
        if not proc_href or "game-procedures-welsh" in proc_href:
          continue
        if not proc_href.startswith("http"):
          proc_href = "https://www.national-lottery.co.uk" + proc_href

        # Select the nearest enclosing card tile container [1]
        card = link.locator("xpath=ancestor::*[contains(., 'to play')][1]")
        if card.count() == 0:
          card = link.locator(
              "xpath=ancestor::*[contains(., 'top prizes left')][1]"
          )

        card_text = card.inner_text()

        # Extract Card Title from the tile header
        lines = [line.strip() for line in card_text.split("\n") if line.strip()]
        first_line = lines[0] if lines else "Unknown"
        game_name = re.sub(r"\s*\[\d+\].*$", "", first_line).strip()

        # Extract Game ID (e.g. [1501])
        id_match = (
            re.search(r"\[(\d{4})\]", card_text)
            or re.search(r"GM(\d{4})", card_text)
            or re.search(r"game-(\d{4})", proc_href)
        )
        game_id = id_match.group(1) if id_match else "UNKNOWN"

        # Extract Price (£)
        price_match = re.search(
            r"£(\d+(?:\.\d{2})?)\s+to\s+play", card_text, re.IGNORECASE
        )
        price = float(price_match.group(1)) if price_match else 0.0

        # Extract Top Prize (£)
        jackpot_match = re.search(
            r"Win\s+up\s+to\s+£([\d,]+)", card_text, re.IGNORECASE
        )
        top_prize = (
            int(jackpot_match.group(1).replace(",", ""))
            if jackpot_match
            else 0
        )

        # Extract Jackpots Left vs Initial
        prizes_match = re.search(
            r"(\d+)\s*/\s*(\d+)\s+top\s+prizes\s+left",
            card_text,
            re.IGNORECASE,
        )
        jackpots_left = int(prizes_match.group(1)) if prizes_match else 0
        jackpots_init = int(prizes_match.group(2)) if prizes_match else 0

        # PDF lookup or download
        cached = cached_games.get(proc_href, {})
        total_printed = cached.get("total_printed")
        release_date = cached.get("release_date")

        if not total_printed or str(total_printed) == "nan":
          print(f"  --> Downloading Procedures PDF: {proc_href}")
          pdf_name, pdf_id, parsed_n, parsed_date = parse_pdf_procedures(
              proc_href
          )
          if game_name == "Unknown" and pdf_name:
            game_name = pdf_name
          if game_id == "UNKNOWN" and pdf_id:
            game_id = pdf_id
          total_printed = parsed_n
          release_date = (
              parsed_date
              or release_date
              or datetime.now(timezone.utc).strftime("%Y-%m-%d")
          )

        est_left, odds_now, edge_ratio = calculate_stats(
            total_printed, jackpots_init, jackpots_left, release_date
        )

        print(
            f"Parsed: {game_name:20} [ID: {game_id}] | £{price:4.2f} | Top:"
            f" £{top_prize:9,d} | Jackpots: {jackpots_left}/{jackpots_init} |"
            f" Ratio: {edge_ratio}"
        )

        scraped_records.append({
            "game_id": game_id,
            "game_name": game_name,
            "price": price,
            "top_prize": top_prize,
            "jackpots_left": jackpots_left,
            "jackpots_initial": jackpots_init,
            "total_printed": total_printed if total_printed else "N/A",
            "release_date": release_date if release_date else "N/A",
            "est_cards_left": est_left,
            "odds_1_in": odds_now,
            "advantage_ratio": edge_ratio,
            "procedures_url": proc_href,
            "last_updated": (
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            ),
        })

      except Exception as err:
        print(f"  [!] Error parsing card element: {err}")

    browser.close()

  if scraped_records:
    df = pd.DataFrame(scraped_records)
    df = df.drop_duplicates(subset=["procedures_url"])
    # Sort by Advantage Ratio descending
    df["sort_key"] = pd.to_numeric(df["advantage_ratio"], errors="coerce")
    df = df.sort_values(by="sort_key", ascending=False).drop(
        columns=["sort_key"]
    )
  else:
    df = pd.DataFrame(
        columns=[
            "game_id",
            "game_name",
            "price",
            "top_prize",
            "jackpots_left",
            "jackpots_initial",
            "total_printed",
            "release_date",
            "est_cards_left",
            "odds_1_in",
            "advantage_ratio",
            "procedures_url",
            "last_updated",
        ]
    )

  df.to_csv(CSV_FILE, index=False)
  print(f"\n[✓] Successfully saved {len(df)} distinct scratchcards to {CSV_FILE}")


if __name__ == "__main__":
  run_scraper()
