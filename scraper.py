from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import io
import math
import os
import re
import pandas as pd
from playwright.sync_api import sync_playwright
from pypdf import PdfReader
import requests
import scipy.stats as stats

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

    # Extract Game Name from Clause 1 (e.g. Game Name: “20X”)
    name_m = re.search(
        r"Game\s+Name:\s*[“\"״]?([^”\"״\n\r]+)[”\"״]?", full_text, re.IGNORECASE
    )
    if name_m:
      pdf_game_name = name_m.group(1).strip(" “\"״\t")

    # Extract Game ID (e.g. Game Number: “Game 1501”)
    num_m = re.search(
        r"Game\s+Number:\s*[“\"״]?Game\s*(\d+)[”\"״]?",
        full_text,
        re.IGNORECASE,
    )
    if num_m:
      pdf_game_id = num_m.group(1).strip()

    # Extract Total Printed (e.g. 12,633,840 Scratchcards in the initial print run)
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


def calculate_bayesian_stats(
    total_printed,
    jackpots_init,
    jackpots_left,
    release_date_str,
    lambda_param=190,  # Weibull scale parameter (~6 months active lifecycle)
    beta_param=1.3,  # Front-loaded launch curve shape
):
  """Computes Bayesian posterior expectations and 80% credible intervals (Best/Worst case)."""
  if not total_printed or not jackpots_init or jackpots_init <= 0:
    return {
        "est_cards_left": "N/A",
        "odds_1_in": "N/A",
        "advantage_ratio": "N/A",
        "odds_best_case": "N/A",
        "odds_worst_case": "N/A",
        "ratio_best_case": "N/A",
        "ratio_worst_case": "N/A",
        "edge_verdict": "❓ Insufficient Data",
    }

  # 1. Days Elapsed
  days_elapsed = None
  if release_date_str and release_date_str != "N/A":
    try:
      rel_date = datetime.strptime(release_date_str, "%Y-%m-%d")
      days_elapsed = max(
          0, (datetime.now(timezone.utc).replace(tzinfo=None) - rel_date).days
      )
    except Exception:
      pass

  # 2. Prior Depletion Fraction from Weibull Sales Curve
  if days_elapsed is not None:
    prior_depletion = 1.0 - math.exp(
        -((days_elapsed / lambda_param) ** beta_param)
    )
    prior_depletion = min(0.96, max(0.02, prior_depletion))
  else:
    prior_depletion = 0.50

  # 3. Prior Beta Parameters
  prior_weight = 6.0  # Effective weight of the time prior
  alpha_0 = prior_depletion * prior_weight
  beta_0 = (1.0 - prior_depletion) * prior_weight

  # 4. Bayesian Conjugate Updating with Jackpots Claimed
  jackpots_claimed = jackpots_init - jackpots_left
  alpha_post = alpha_0 + jackpots_claimed
  beta_post = beta_0 + jackpots_left

  # Expected Mean Depletion
  post_mean_depletion = alpha_post / (alpha_post + beta_post)

  # 80% Credible Interval (10th percentile to 90th percentile)
  depletion_low = stats.beta.ppf(0.10, alpha_post, beta_post)  # Worst case
  depletion_high = stats.beta.ppf(0.90, alpha_post, beta_post)  # Best case

  depletion_low = max(0.01, min(0.98, depletion_low))
  depletion_high = max(0.02, min(0.99, depletion_high))

  # Remaining ticket estimates
  est_remaining = int(total_printed * (1.0 - post_mean_depletion))
  est_remaining = max(500, min(total_printed, est_remaining))

  cards_best_case = int(total_printed * (1.0 - depletion_high))
  cards_worst_case = int(total_printed * (1.0 - depletion_low))

  base_odds = total_printed / jackpots_init

  if jackpots_left > 0:
    odds_expected = round(est_remaining / jackpots_left, 0)
    ratio_expected = round(base_odds / odds_expected, 3)

    odds_best = round(cards_best_case / jackpots_left, 0)
    ratio_best = round(base_odds / odds_best, 3)

    odds_worst = round(cards_worst_case / jackpots_left, 0)
    ratio_worst = round(base_odds / odds_worst, 3)

    # Plain-English Verdict
    if ratio_expected >= 1.40 and ratio_worst >= 1.00:
      verdict = "🔥 Strong Edge (Confirmed)"
    elif ratio_expected >= 1.25:
      verdict = "🔥 High Edge"
    elif ratio_expected >= 1.05:
      verdict = "✅ Favourable"
    elif ratio_expected >= 0.90:
      verdict = "⚖️ Neutral"
    else:
      verdict = "⚠️ Depleted"
  else:
    odds_expected = "No Top Prizes"
    ratio_expected = 0.0
    odds_best = "No Top Prizes"
    ratio_best = 0.0
    odds_worst = "No Top Prizes"
    ratio_worst = 0.0
    verdict = "⛔ No Jackpots Left"

  return {
      "est_cards_left": est_remaining,
      "odds_1_in": odds_expected,
      "advantage_ratio": ratio_expected,
      "odds_best_case": odds_best,
      "odds_worst_case": odds_worst,
      "ratio_best_case": ratio_best,
      "ratio_worst_case": ratio_worst,
      "edge_verdict": verdict,
  }


def run_scraper():
  print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting Bayesian Scraper...")

  cached_games = {}
  if os.path.exists(CSV_FILE):
    try:
      prev_df = pd.read_csv(CSV_FILE)
      for _, row in prev_df.iterrows():
        url = str(row.get("procedures_url"))
        g_name = str(row.get("game_name"))
        g_id = str(row.get("game_id"))
        if (
            url
            and "to play" not in g_name.lower()
            and g_id != "UNKNOWN"
            and pd.notna(row.get("total_printed"))
        ):
          cached_games[url] = row.to_dict()
      print(f"Loaded {len(cached_games)} valid cached games.")
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

    # 1. Accept Cookies
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

        # Select the individual card container
        card = link.locator(
            "xpath=ancestor::*[.//h2 or .//h3 or .//h4 or"
            " contains(@class,'card')][1]"
        )
        if card.count() == 0:
          card = link.locator("xpath=ancestor::*[contains(., 'to play')][1]")

        card_text = card.inner_text()
        heading_elem = card.locator("h2, h3, h4").first
        html_title = (
            heading_elem.inner_text().strip() if heading_elem.count() > 0 else ""
        )

        # Price (£)
        price_match = re.search(
            r"£(\d+(?:\.\d{2})?)\s+to\s+play", card_text, re.IGNORECASE
        )
        price = float(price_match.group(1)) if price_match else 0.0

        # Top Prize (£)
        jackpot_match = re.search(
            r"Win\s+up\s+to\s+£([\d,]+)", card_text, re.IGNORECASE
        )
        top_prize = (
            int(jackpot_match.group(1).replace(",", ""))
            if jackpot_match
            else 0
        )

        # Jackpots Left / Initial
        prizes_match = re.search(
            r"(\d+)\s*/\s*(\d+)\s+top\s+prizes\s+left",
            card_text,
            re.IGNORECASE,
        )
        jackpots_left = int(prizes_match.group(1)) if prizes_match else 0
        jackpots_init = int(prizes_match.group(2)) if prizes_match else 0

        # Cache or Download PDF
        cached = cached_games.get(proc_href, {})
        game_name = cached.get("game_name")
        game_id = cached.get("game_id")
        total_printed = cached.get("total_printed")
        release_date = cached.get("release_date")

        is_invalid = (
            not game_name
            or "to play" in str(game_name).lower()
            or str(game_id) == "UNKNOWN"
            or not total_printed
            or str(total_printed) == "nan"
        )

        if is_invalid and proc_href:
          print(f"  --> Downloading & extracting PDF: {proc_href}")
          pdf_name, pdf_id, parsed_n, parsed_date = parse_pdf_procedures(
              proc_href
          )
          game_name = (
              pdf_name
              or re.sub(r"\s*\[\d+\].*$", "", html_title).strip()
              or "Unknown"
          )
          game_id = (
              pdf_id
              or (
                  re.search(r"\[(\d{4})\]", html_title).group(1)
                  if re.search(r"\[(\d{4})\]", html_title)
                  else "UNKNOWN"
              )
          )
          total_printed = parsed_n
          release_date = (
              parsed_date
              or release_date
              or datetime.now(timezone.utc).strftime("%Y-%m-%d")
          )

        # Compute Bayesian Statistics & Uncertainty Intervals
        bayes = calculate_bayesian_stats(
            total_printed, jackpots_init, jackpots_left, release_date
        )

        print(
            f"Parsed: {str(game_name):20} [ID: {str(game_id):4}] | £{price:4.2f}"
            f" | Jackpots: {jackpots_left}/{jackpots_init} | Ratio:"
            f" {bayes['advantage_ratio']} ({bayes['edge_verdict']})"
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
            "est_cards_left": bayes["est_cards_left"],
            "advantage_ratio": bayes["advantage_ratio"],
            "edge_verdict": bayes["edge_verdict"],
            "odds_1_in": bayes["odds_1_in"],
            "ratio_best_case": bayes["ratio_best_case"],
            "ratio_worst_case": bayes["ratio_worst_case"],
            "odds_best_case": bayes["odds_best_case"],
            "odds_worst_case": bayes["odds_worst_case"],
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
            "advantage_ratio",
            "edge_verdict",
            "odds_1_in",
            "ratio_best_case",
            "ratio_worst_case",
            "odds_best_case",
            "odds_worst_case",
            "procedures_url",
            "last_updated",
        ]
    )

  df.to_csv(CSV_FILE, index=False)
  print(f"\n[✓] Successfully saved {len(df)} distinct scratchcards to {CSV_FILE}")


if __name__ == "__main__":
  run_scraper()
