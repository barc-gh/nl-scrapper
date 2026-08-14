import io
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import pandas as pd
from playwright.sync_api import sync_playwright
from pypdf import PdfReader
import requests

CSV_FILE = "scratchcards.csv"
CATALOG_URL = "https://www.national-lottery.co.uk/scratchcards/all-scratchcards"


def parse_pdf_procedures(pdf_url):
  """Downloads the Game Procedures PDF in memory and extracts:

  - Total cards printed (N)
  - PDF creation date or server Last-Modified date
  """
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
          " like Gecko) Chrome/120.0.0.0 Safari/537.36"
      )
  }

  release_date = None
  total_printed = None

  try:
    # 1. Check HTTP Last-Modified header
    head_resp = requests.head(pdf_url, headers=headers, timeout=10)
    if "Last-Modified" in head_resp.headers:
      dt = parsedate_to_datetime(head_resp.headers["Last-Modified"])
      release_date = dt.strftime("%Y-%m-%d")

    # 2. Download PDF content into memory
    get_resp = requests.get(pdf_url, headers=headers, timeout=15)
    get_resp.raise_for_status()

    pdf_stream = io.BytesIO(get_resp.content)
    reader = PdfReader(pdf_stream)

    # If HTTP header wasn't found, try embedded PDF CreationDate metadata
    if not release_date and reader.metadata:
      raw_meta_date = reader.metadata.get("/CreationDate") or reader.metadata.get(
          "/ModDate"
      )
      if raw_meta_date:
        match = re.search(r"D:(\d{4})(\d{2})(\d{2})", str(raw_meta_date))
        if match:
          release_date = (
              f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
          )

    # 3. Extract total printed from the text layer
    full_text = ""
    for page in reader.pages:
      full_text += page.extract_text() or ""

    # Regex search: "There are X,XXX,XXX Scratchcards in the initial print run"
    print_run_match = re.search(
        r"([\d,]+)\s+Scratchcards\s+in\s+the\s+initial\s+print\s+run",
        full_text,
        re.IGNORECASE,
    )
    if print_run_match:
      total_printed = int(print_run_match.group(1).replace(",", ""))

  except Exception as e:
    print(f"  [!] Error parsing PDF ({pdf_url}): {e}")

  return total_printed, release_date


def calculate_stats(
    printed,
    jackpots_init,
    jackpots_left,
    release_date_str,
    expected_lifecycle=240,
):
  """Computes estimated cards left, current odds, and advantage ratio."""
  if not printed or not jackpots_init or jackpots_init <= 0:
    return None, None, None

  # 1. Days elapsed calculation
  days_elapsed = None
  if release_date_str:
    try:
      rel_date = datetime.strptime(release_date_str, "%Y-%m-%d")
      days_elapsed = max(
          0, (datetime.now(timezone.utc).replace(tzinfo=None) - rel_date).days
      )
    except ValueError:
      pass

  # 2. Estimate cards remaining (Blended Depletion Model)
  if days_elapsed is not None:
    # Blend time decay (60%) and jackpot claims (40%)
    time_decay_factor = max(0.05, 1.0 - (days_elapsed / expected_lifecycle))
    jackpot_factor = jackpots_left / jackpots_init
    blended_factor = (0.6 * time_decay_factor) + (0.4 * jackpot_factor)
    est_remaining = int(printed * blended_factor)
  else:
    # Fallback to pure Maximum Likelihood Depletion
    claimed = jackpots_init - jackpots_left
    est_remaining = int(printed * (1.0 - (claimed / (jackpots_init + 1))))

  # Ensure realistic bounds
  est_remaining = max(1000, min(printed, est_remaining))

  # 3. Current 1-in-X Odds
  current_odds = (
      round(est_remaining / jackpots_left, 0) if jackpots_left > 0 else 0
  )

  # 4. Advantage Ratio (R)
  base_odds = printed / jackpots_init
  advantage_ratio = (
      round(base_odds / current_odds, 3)
      if current_odds and current_odds > 0
      else 0.0
  )

  return est_remaining, current_odds, advantage_ratio


def run_scraper():
  print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting Scraper...")

  # Load existing CSV cache to avoid re-downloading existing PDFs
  cached_games = {}
  if os.path.exists(CSV_FILE):
    try:
      prev_df = pd.read_csv(CSV_FILE)
      for _, row in prev_df.iterrows():
        cached_games[str(row.get("game_id"))] = row.to_dict()
      print(f"Loaded {len(cached_games)} cached games from {CSV_FILE}")
    except Exception as e:
      print(f"Could not load cache: {e}")

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
        )
    )
    page = context.new_page()

    print(f"Navigating to {CATALOG_URL}...")
    page.goto(CATALOG_URL, wait_until="networkidle", timeout=60000)

    # Select all scratchcard game cards on the catalog page
    cards = page.locator("article, .card--scratchcard, [class*='card']").all()
    print(f"Found {len(cards)} card elements on page.")

    for card in cards:
      card_text = card.inner_text()
      if (
          "top prizes left" not in card_text.lower()
          and "procedures" not in card_text.lower()
      ):
        continue

      # Extract Game Title & Game ID: e.g., "20X [1501]"
      title_match = re.search(r"^(.*?)\s*(?:\[(\d+)\])?\s*$", card_text.split("\n")[0])
      game_name = (
          title_match.group(1).strip() if title_match else card_text.split("\n")[0]
      )
      game_id_match = re.search(r"\[(\d{4})\]", card_text) or re.search(
          r"GM(\d{4})", card_text
      )
      game_id = game_id_match.group(1) if game_id_match else "UNKNOWN"

      # Extract Price (£)
      price_match = re.search(r"£(\d+(?:\.\d{2})?)\s+to play", card_text)
      price = float(price_match.group(1)) if price_match else 0.0

      # Extract Top Prize
      jackpot_match = re.search(r"Win up to £([\d,]+)", card_text)
      top_prize_str = (
          jackpot_match.group(1).replace(",", "") if jackpot_match else "0"
      )
      top_prize = int(top_prize_str)

      # Extract Remaining / Total Top Prizes (e.g. "3/4 top prizes left")
      prizes_left_match = re.search(
          r"(\d+)\s*/\s*(\d+)\s+top prizes left", card_text
      )
      jackpots_left = (
          int(prizes_left_match.group(1)) if prizes_left_match else 0
      )
      jackpots_init = (
          int(prizes_left_match.group(2)) if prizes_left_match else 0
      )

      # Extract Game Procedures PDF link
      proc_elem = card.locator("a:has-text('procedures'), a[href*='.pdf']")
      proc_href = (
          proc_elem.get_attribute("href") if proc_elem.count() > 0 else ""
      )
      if proc_href and not proc_href.startswith("http"):
        proc_href = "https://www.national-lottery.co.uk" + proc_href

      print(
          f"Processing: {game_name} [ID: {game_id}] | Jackpots:"
          f" {jackpots_left}/{jackpots_init}"
      )

      # Look up static values from cache or fetch from PDF
      cached = cached_games.get(str(game_id), {})
      total_printed = cached.get("total_printed")
      release_date = cached.get("release_date")

      if not total_printed and proc_href:
        print(f"  --> Downloading & parsing new PDF for Game {game_id}...")
        parsed_n, parsed_date = parse_pdf_procedures(proc_href)
        total_printed = parsed_n
        release_date = (
            parsed_date
            or release_date
            or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )

      # Statistical Computations
      est_left, odds_now, edge_ratio = calculate_stats(
          total_printed, jackpots_init, jackpots_left, release_date
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
          "est_cards_left": est_left if est_left else "N/A",
          "odds_1_in": odds_now if odds_now else "N/A",
          "advantage_ratio": edge_ratio if edge_ratio else "N/A",
          "procedures_url": proc_href,
          "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
      })

    browser.close()

  # Write back to CSV
  if scraped_records:
    df = pd.DataFrame(scraped_records)
    # Sort with highest advantage ratio first
    df.to_csv(CSV_FILE, index=False)
    print(f"\n[✓] Successfully written {len(df)} games to {CSV_FILE}")
  else:
    print("\n[!] No records were extracted. Check website structure.")


if __name__ == "__main__":
  run_scraper()
