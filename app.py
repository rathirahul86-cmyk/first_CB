from flask import Flask, render_template, request, jsonify, redirect, url_for
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
import os, io, re

try:
    import pdfplumber
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False

app = Flask(__name__)
app.secret_key = "portfolio-local-key"

# ---------------------------------------------------------------------------
# Folder structure
# ---------------------------------------------------------------------------
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
BROKER_FOLDERS = {
    "Fidelity":       os.path.join(DATA_DIR, "fidelity"),
    "Schwab":         os.path.join(DATA_DIR, "schwab"),
    "Morgan Stanley": os.path.join(DATA_DIR, "morgan_stanley"),
}
for folder in BROKER_FOLDERS.values():
    os.makedirs(folder, exist_ok=True)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _clean(val):
    if val is None:
        return 0.0
    s = str(val).replace("$","").replace(",","").replace("%","").strip()
    negative = s.startswith("(") and s.endswith(")")
    s = s.replace("(","").replace(")","")
    try:
        return -float(s) if negative else float(s or 0)
    except ValueError:
        return 0.0

# Non-ticker tokens to ignore when scanning for symbols
_NON_TICKER = {
    "SIPC","SEC","NFS","FBS","IRA","ETF","ETN","INC","CL","STK","COM","LLC",
    "LTD","USD","EUR","NPV","CAP","ADR","NA","ISIN","SEDOL","CUSIP","ACT",
    "ALL","ANY","FOR","THE","AND","ARE","NOT","MAY","SPA","SPC","AMP",
}

# ---------------------------------------------------------------------------
# ── SCHWAB PDF PARSER ────────────────────────────────────────────────────────
# Format: SYMBOL  DESCRIPTION  QTY  PRICE  MKTVAL  COSTBASIS  GAIN  ...
# ---------------------------------------------------------------------------
# Regex: starts with a ticker, followed by compressed description, then numbers
_SCHWAB_ROW = re.compile(
    r'^([A-Z][A-Z0-9\.\-]{0,5})\s+'        # SYMBOL
    r'\S.*?\s+'                              # DESCRIPTION (no spaces, compressed)
    r'([\d,]+\.[\d]+)\s+'                   # QTY
    r'([\d,]+\.[\d]+)\s+'                   # PRICE
    r'([\d,]+\.[\d]+)\s+'                   # MARKET VALUE
    r'([\d,]+\.[\d]+)\s+'                   # COST BASIS
    r'(\(?[\d,]+\.[\d]+\)?|N/A)'            # GAIN/LOSS
)
# Fixed income / bond row: CUSIP  DESCRIPTION  [COUPON]  MM/DD/YY  QTY  PRICE  MV  CB  ...
_SCHWAB_BOND = re.compile(
    r'^\S{9}\s+'                             # CUSIP (9 chars)
    r'(\S+)\s+'                              # SHORT DESCRIPTION
    r'(?:[\d\.]+\s+)?'                       # COUPON (optional — may not appear in text)
    r'\d{2}/\d{2}/\d{2}\s+'                 # MATURITY DATE
    r'([\d,]+\.[\d]+)\s+'                    # QTY/PAR
    r'([\d,]+\.[\d]+)\s+'                    # PRICE
    r'([\d,]+\.[\d]+)\s+'                    # MARKET VALUE
    r'([\d,]+\.[\d]+)'                       # ADJ COST BASIS
)

# For cash/sweep lines: "BankSweep  CHARLESSCHWAB  ...  20825.65  ..."
_SCHWAB_CASH = re.compile(
    r'BankSweep\s+\S+\s+[\d,]+\.[\d]+\s+([\d,]+\.[\d]+)'
)

def parse_schwab_pdf(path, account_label):
    holdings = []
    acct_num = re.search(r'_(\d{3})\.PDF$', path, re.IGNORECASE)
    acct_suffix = f" ({acct_num.group(1)})" if acct_num else ""
    label = account_label + acct_suffix

    with pdfplumber.open(path) as pdf:
        in_positions = False
        current_type = "Stock"
        cash_val = 0.0

        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                line = line.strip()

                # Section headers
                if "Positions - Equities" in line:
                    in_positions = True; current_type = "Stock"; continue
                if "Positions - Exchange Traded Funds" in line:
                    in_positions = True; current_type = "ETF"; continue
                if "Positions - Fixed Income" in line:
                    in_positions = True; current_type = "Bond"; continue
                if "Positions - Options" in line or "Transactions" in line:
                    in_positions = False; continue
                if line.startswith("Total") or line.startswith("Estimated"):
                    continue

                # Cash sweep
                m = _SCHWAB_CASH.search(line)
                if m:
                    cash_val = _clean(m.group(1)); continue

                if not in_positions:
                    continue

                m = _SCHWAB_ROW.match(line)
                if m:
                    symbol, qty, price, mv, cb, gl = m.groups()
                    if symbol in _NON_TICKER:
                        continue
                    desc_match = re.match(r'[A-Z][A-Z0-9\.\-]{0,5}\s+(\S+)', line)
                    desc_text = desc_match.group(1) if desc_match else symbol
                    holdings.append({
                        "account":      label,
                        "symbol":       symbol,
                        "description":  desc_text,
                        "quantity":     _clean(qty),
                        "last_price":   _clean(price),
                        "market_value": _clean(mv),
                        "cost_basis":   _clean(cb),
                        "type":         current_type,
                    })
                    continue

                # Bond / Fixed Income row (CUSIP format)
                if current_type == "Bond":
                    mb = _SCHWAB_BOND.match(line)
                    if mb:
                        desc, qty, price, mv, cb = mb.groups()
                        holdings.append({
                            "account":      label,
                            "symbol":       "BOND",
                            "description":  desc,
                            "quantity":     _clean(qty),
                            "last_price":   _clean(price),
                            "market_value": _clean(mv),
                            "cost_basis":   _clean(cb),
                            "type":         "Bond",
                        })

        if cash_val > 0:
            holdings.append({
                "account": label, "symbol": "CASH",
                "description": "Cash & Sweep", "quantity": 1,
                "last_price": cash_val, "market_value": cash_val,
                "cost_basis": cash_val, "type": "Cash",
            })

    return holdings


# ---------------------------------------------------------------------------
# ── FIDELITY PDF PARSER ──────────────────────────────────────────────────────
# Two sub-formats:
#   A) DESC (SYM) QTY PRICE MKTVAL COST GAIN  [symbol on same line, nums after]
#   B) DESC QTY PRICE MKTVAL COST GAIN\n(SYM) [symbol on following line]
# ---------------------------------------------------------------------------
_FID_SYM_INLINE = re.compile(
    r'^(.+?)\(([A-Z][A-Z0-9\.]{0,5})\)\s+'  # DESCRIPTION (SYMBOL)
    r'([\d,]+\.[\d]+)\s+'                    # QTY
    r'\$?([\d,]+\.[\d]+)\s+'                 # PRICE
    r'\$?([\d,]+\.[\d]+)\s+'                 # MARKET VALUE
    r'\$?([\d,]+\.[\d]+|not applicable)'     # COST BASIS
)
# Match a (SYMBOL) appearing ANYWHERE in a line (end of continuation line, or alone)
_FID_SYM_ANY    = re.compile(r'\(([A-Z][A-Z0-9\.]{0,5})\)')
_FID_NUMS_LINE  = re.compile(
    r'([\d,]+\.[\d]+)\s+'                    # QTY
    r'\$?([\d,]+\.[\d]+)\s+'                 # PRICE
    r'\$?([\d,]+\.[\d]+)\s+'                 # MARKET VALUE
    r'\$?([\d,]+\.[\d]+|not applicable)'     # COST BASIS
)

# Account type labels for Fidelity
def _fidelity_account_name(text):
    if "ROLLOVER IRA" in text:
        owner = re.search(r'([A-Z]+ [A-Z]+)\s*[-–]\s*ROLLOVER IRA', text)
        return f"Fidelity IRA ({owner.group(1).title() if owner else 'Rollover'})"
    if "BROKERAGELINK" in text or "NON-PROTOTYPE" in text:
        company = re.search(r'(FACEBOOK|META|GOOGLE|AMAZON|APPLE|MICROSOFT)', text)
        return f"Fidelity 401k ({company.group(1).title() if company else 'BrokerageLink'})"
    if "INDIVIDUAL" in text:
        return "Fidelity Individual"
    return "Fidelity"

def _fidelity_asset_type(section):
    s = section.lower()
    if "mutual fund" in s or "fzrox" in s or "vtsax" in s or "swppx" in s:
        return "Mutual Fund"
    if "exchange traded" in s or "etp" in s:
        return "ETF"
    if "stock" in s or "common" in s:
        return "Stock"
    if "core account" in s or "money market" in s or "fdrxx" in s or "spaxx" in s:
        return "Cash"
    return "Stock"

def parse_fidelity_pdf(path, account_label):
    holdings = []

    with pdfplumber.open(path) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    # Detect account label from document text
    label = _fidelity_account_name(full_text)

    # Skip stock-plan-only statements (no individual positions)
    if ("Stock Plans" in full_text and
        "Common Stock" not in full_text and
        "Mutual Funds" not in full_text and
        "Exchange Traded" not in full_text):
        return []

    lines = full_text.splitlines()
    current_section = "Stock"

    for i, line in enumerate(lines):
        ls = line.strip()

        # Track section to determine asset type
        ls_low = ls.lower()
        if "mutual fund" in ls_low:
            current_section = "Mutual Fund"
        elif "exchange traded" in ls_low or "equity etp" in ls_low:
            current_section = "ETF"
        elif ls_low.startswith("stocks") or "common stock" in ls_low:
            current_section = "Stock"
        elif "core account" in ls_low or "money market" in ls_low:
            current_section = "Cash"

        # Skip header/footer lines
        if any(ls.startswith(skip) for skip in
               ("Total ", "Price", "Description", "All positions", "Copyright",
                "Schwab", "Fidelity", "Information About", "Lost or Stolen",
                "Additional", "Income Summary", "Account Summary", "Holdings\n")):
            continue

        # Case A: symbol on same line with numbers AFTER it
        m = _FID_SYM_INLINE.match(ls)
        if m:
            desc, symbol, qty, price, mv, cb = m.groups()
            if symbol not in _NON_TICKER and _clean(mv) > 0:
                holdings.append({
                    "account":      label,
                    "symbol":       symbol,
                    "description":  desc.strip()[:60],
                    "quantity":     _clean(qty),
                    "last_price":   _clean(price),
                    "market_value": _clean(mv),
                    "cost_basis":   _clean(cb),
                    "type":         current_section,
                })
            continue

        # Case B: (SYMBOL) appears anywhere in line but numbers are NOT after it
        # Covers: "(FZROX)" alone, "INDEX ADMIRAL (VTSAX)", "USD (VOO)", "DEPOSITARY RECEIPT (SPY)"
        m = _FID_SYM_ANY.search(ls)
        if m:
            symbol = m.group(1)
            if symbol in _NON_TICKER:
                continue
            # Confirm no >=3 numbers appear after the symbol on this line
            after_sym = ls[m.end():]
            nums_inline = re.findall(r'[\d,]+\.[\d]+', after_sym)
            if len(nums_inline) >= 3:
                continue  # already handled by Case A or is a totals line

            # Scan backwards (up to 4 lines) for the line with the numbers
            for j in range(i - 1, max(i - 5, -1), -1):
                prev = lines[j].strip()
                nm = _FID_NUMS_LINE.search(prev)
                if nm:
                    qty, price, mv, cb = nm.groups()
                    if _clean(mv) > 0:
                        desc = prev[:nm.start()].strip() or symbol
                        if len(desc) < 3 and j > 0:
                            desc = lines[j - 1].strip()
                        holdings.append({
                            "account":      label,
                            "symbol":       symbol,
                            "description":  desc[:60],
                            "quantity":     _clean(qty),
                            "last_price":   _clean(price),
                            "market_value": _clean(mv),
                            "cost_basis":   _clean(cb),
                            "type":         current_section,
                        })
                    break

    return holdings


# ---------------------------------------------------------------------------
# ── MORGAN STANLEY PDF PARSER ────────────────────────────────────────────────
# IBM ESPP / stock plan format
# ---------------------------------------------------------------------------
def parse_morgan_stanley_pdf(path, account_label):
    with pdfplumber.open(path) as pdf:
        text = pdf.pages[0].extract_text() or ""

    issuer_m  = re.search(r'Issuer Description:\s+(.+?)(?:\n|$)', text)
    issuer = (issuer_m.group(1).strip() if issuer_m else "").upper()

    # Each row shows two columns: Opening Value | Closing Value
    # We always want the LAST number on each line (= closing value)
    _nums = re.compile(r'[\d,]+\.[\d]{2,}')

    def last_val(pattern):
        m = re.search(pattern + r'\s+(.*?)(?:\n|$)', text)
        if not m:
            return 0.0
        nums = _nums.findall(m.group(1))
        return _clean(nums[-1]) if nums else 0.0

    qty   = last_val(r'Number of Shares')
    price = last_val(r'Share Price')
    mv    = last_val(r'Share Value')

    if mv == 0 and qty > 0 and price > 0:
        mv = round(qty * price, 2)

    if qty == 0 and mv == 0:
        return []

    # Map issuer name to ticker
    symbol = "IBM"
    if "MICROSOFT"  in issuer: symbol = "MSFT"
    elif "APPLE"    in issuer: symbol = "AAPL"
    elif "GOOGLE"   in issuer: symbol = "GOOGL"
    elif "AMAZON"   in issuer: symbol = "AMZN"
    elif "META"     in issuer: symbol = "META"

    return [{
        "account":      account_label,
        "symbol":       symbol,
        "description":  issuer_m.group(1).strip()[:60] if issuer_m else symbol,
        "quantity":     qty,
        "last_price":   price,
        "market_value": mv,
        "cost_basis":   0.0,   # Cost basis not in ESPP statements
        "type":         "Stock",
    }]


# ---------------------------------------------------------------------------
# ── FIDELITY NETBENEFITS PDF PARSER ─────────────────────────────────────────
# Format (page 2): fund rows with columns:
#   [fund name...] BEG_SHARES END_SHARES $BEG_PRICE $END_PRICE $BEG_MV $END_MV
# Fund names may span multiple lines before/within the data row.
# ---------------------------------------------------------------------------
_NB_ROW = re.compile(
    r'([\d,]+\.[\d]+)\s+'       # BEG_SHARES
    r'([\d,]+\.[\d]+)\s+'       # END_SHARES
    r'\$([\d,]+\.[\d]+)\s+'     # BEG_PRICE
    r'\$([\d,]+\.[\d]+)\s+'     # END_PRICE
    r'\$([\d,]+\.[\d]+)\s+'     # BEG_VALUE
    r'\$([\d,]+\.[\d]+)'        # END_VALUE
)
_NB_SKIP_KW = ("Shares as of", "Investment", "Tier ", "TIER ", "Account Totals",
               "Fidelity NetBenefits", "https://", "Page ")

def parse_fidelity_netbenefits(path):
    with pdfplumber.open(path) as pdf:
        page1 = pdf.pages[0].extract_text() or ""
        page2 = (pdf.pages[1].extract_text() or "") if len(pdf.pages) > 1 else ""

    # Detect account name from page 1
    p1up = page1.upper()
    if "RED HAT" in p1up:
        acct = "Fidelity 401k (Red Hat)"
    elif "MICROSOFT" in p1up:
        acct = "Fidelity 401k (Microsoft)"
    else:
        # Generic: try to extract plan/company name from top of page
        m = re.search(r'Retirement Savings Statement\s*\n(.+?)\n', page1)
        acct = m.group(1).strip()[:40] if m else "Fidelity 401k"

    holdings = []
    lines = page2.splitlines()

    for i, line in enumerate(lines):
        ls = line.strip()
        if any(kw in ls for kw in _NB_SKIP_KW):
            continue

        m = _NB_ROW.search(ls)
        if not m:
            continue

        beg_sh, end_sh, beg_px, end_px, beg_mv, end_mv = m.groups()
        qty   = _clean(end_sh)
        price = _clean(end_px)
        mv    = _clean(end_mv)

        if mv <= 0 or qty <= 0:
            continue

        # Description: text before numbers on this line + nearest valid preceding line
        prefix = ls[:m.start()].strip()
        prev_name = ""
        for j in range(i - 1, max(i - 6, -1), -1):
            pls = lines[j].strip()
            if not pls:
                continue
            if _NB_ROW.search(pls):
                break   # hit a previous data row — stop
            if "$" in pls or any(kw in pls for kw in _NB_SKIP_KW):
                continue
            prev_name = pls
            break

        desc = (prev_name + " " + prefix).strip() if prev_name else (prefix or "401k Fund")

        # Symbol: initials of alphabetic words, max 6 chars
        sym_words = [w for w in desc.split() if w[:1].isalpha()]
        symbol = "".join(w[0] for w in sym_words).upper()[:6] or "FUND"

        holdings.append({
            "account":      acct,
            "symbol":       symbol,
            "description":  desc[:60],
            "quantity":     qty,
            "last_price":   price,
            "market_value": mv,
            "cost_basis":   0.0,
            "type":         "Mutual Fund",
        })

    return holdings


# ---------------------------------------------------------------------------
# ── CSV PARSERS (kept for manual imports) ────────────────────────────────────
# ---------------------------------------------------------------------------
def _find_header_row(lines, keywords=("symbol", "ticker")):
    for i, line in enumerate(lines):
        if any(k in line.lower() for k in keywords):
            return i
    return 0

def parse_fidelity_csv(df, account_label):
    holdings = []
    for _, row in df.iterrows():
        sym = str(row.get("Symbol", "")).strip()
        if not sym or sym.lower() in ("symbol", "pending activity", "nan"):
            continue
        holdings.append({
            "account":      account_label or str(row.get("Account Name", "Fidelity")),
            "symbol":       sym,
            "description":  str(row.get("Description", sym)),
            "quantity":     _clean(row.get("Quantity", 0)),
            "last_price":   _clean(row.get("Last Price", 0)),
            "market_value": _clean(row.get("Current Value", 0)),
            "cost_basis":   _clean(row.get("Cost Basis Total", 0)),
            "type":         str(row.get("Type", "Stock")),
        })
    return holdings

def parse_schwab_csv(df, account_label):
    holdings = []
    for _, row in df.iterrows():
        sym = str(row.get("Symbol", "")).strip()
        if not sym or sym.lower() in ("symbol", "nan"):
            continue
        holdings.append({
            "account":      account_label or "Schwab",
            "symbol":       sym,
            "description":  str(row.get("Description", sym)),
            "quantity":     _clean(row.get("Quantity", 0)),
            "last_price":   _clean(row.get("Price", 0)),
            "market_value": _clean(row.get("Market Value", 0)),
            "cost_basis":   _clean(row.get("Cost Basis", 0)),
            "type":         str(row.get("Security Type", "Stock")),
        })
    return holdings

def detect_and_parse_csv(df, account_label):
    cols = [c.lower().strip() for c in df.columns]
    if "last price" in cols and "current value" in cols:
        return parse_fidelity_csv(df, account_label)
    if "security type" in cols:
        return parse_schwab_csv(df, account_label)
    return parse_schwab_csv(df, account_label)  # generic fallback

def load_csv_file(path, account_label):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        lines = f.read().splitlines()
    start = _find_header_row(lines)
    df = pd.read_csv(io.StringIO("\n".join(lines[start:])))
    return detect_and_parse_csv(df, account_label)

# ---------------------------------------------------------------------------
# ── FILE DISPATCHER ──────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------
def load_file(path, broker):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return load_csv_file(path, broker)
    if ext == ".pdf":
        if not PDF_SUPPORT:
            print(f"  [PDF] pdfplumber not installed — skipping {os.path.basename(path)}")
            return []
        b = broker.lower()
        if "schwab" in b:
            return parse_schwab_pdf(path, broker)
        elif "fidelity" in b:
            if "netbenefits" in os.path.basename(path).lower():
                return parse_fidelity_netbenefits(path)
            return parse_fidelity_pdf(path, broker)
        elif "morgan" in b:
            return parse_morgan_stanley_pdf(path, broker)
    return []

# ---------------------------------------------------------------------------
# ── SCAN DATA FOLDERS ────────────────────────────────────────────────────────
# For quarterly statements, only the MOST RECENT file per account is loaded
# to avoid double-counting positions across time periods.
# ---------------------------------------------------------------------------
def _extract_date_and_account(fname):
    """Return (date_str_for_sorting, account_id) from a filename."""
    # Schwab: "Brokerage Statement_2025-12-31_582.PDF" → date="2025-12-31", acct="582"
    m = re.search(r'(\d{4}-\d{2}-\d{2})_(\w+)\.', fname, re.IGNORECASE)
    if m:
        return m.group(1), m.group(2)

    # Morgan Stanley: "Quarterly Statement 12_31_2025.pdf" → date="2025-12-31", acct="ms"
    m = re.search(r'(\d{2})_(\d{2})_(\d{4})\.', fname, re.IGNORECASE)
    if m:
        return f"{m.group(3)}-{m.group(1)}-{m.group(2)}", "ms"

    # Fidelity annual statements (different account each file) — treat each as unique
    # "Statement12312025.pdf", "Statement12312025-2.pdf"
    # Extract date + file variant so they're all kept
    m = re.search(r'(\d{8})', fname)
    if m:
        d = m.group(1)  # e.g. "12312025"
        variant = re.search(r'-(\d+)\.', fname)
        acct_id = f"fid_{variant.group(1) if variant else '0'}"
        # Convert MMDDYYYY to YYYY-MM-DD
        try:
            date_str = f"{d[4:8]}-{d[0:2]}-{d[2:4]}"
        except Exception:
            date_str = d
        return date_str, acct_id

    return "0000-00-00", fname  # fallback: keep file

def _most_recent_files(folder):
    """Return only the most recent file per account within a broker folder."""
    files = [f for f in os.listdir(folder)
             if not f.startswith(".") and
             os.path.splitext(f)[1].lower() in (".csv", ".pdf")]

    # Group by account_id, keep the file with the latest date
    best = {}  # account_id → (date_str, fname)
    for f in files:
        date_str, acct_id = _extract_date_and_account(f)
        if acct_id not in best or date_str > best[acct_id][0]:
            best[acct_id] = (date_str, f)

    return sorted(v[1] for v in best.values())

def scan_data_folders():
    holdings = []
    for broker, folder in BROKER_FOLDERS.items():
        if not os.path.isdir(folder):
            continue
        files_to_load = _most_recent_files(folder)
        for fname in files_to_load:
            fpath = os.path.join(folder, fname)
            print(f"  Loading [{broker}] {fname}")
            try:
                result = load_file(fpath, broker)
                print(f"    → {len(result)} positions")
                holdings.extend(result)
            except Exception as e:
                print(f"    ERROR: {e}")
    return holdings

def active_holdings():
    data = scan_data_folders()
    return data if data else SAMPLE_HOLDINGS

# ---------------------------------------------------------------------------
# ── SUMMARY ──────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------
def calculate_summary(holdings):
    for h in holdings:
        h["gain_loss"]     = h["market_value"] - h["cost_basis"]
        h["gain_loss_pct"] = (h["gain_loss"] / h["cost_basis"] * 100) if h["cost_basis"] > 0 else 0

    total_value    = sum(h["market_value"] for h in holdings)
    total_cost     = sum(h["cost_basis"]   for h in holdings)
    total_gain     = total_value - total_cost
    total_gain_pct = (total_gain / total_cost * 100) if total_cost > 0 else 0

    accounts   = {}
    allocation = {}
    for h in holdings:
        accounts[h["account"]]  = accounts.get(h["account"], 0)  + h["market_value"]
        allocation[h["type"]]   = allocation.get(h["type"], 0)   + h["market_value"]

    file_count = sum(
        len([f for f in os.listdir(folder) if not f.startswith(".")])
        for folder in BROKER_FOLDERS.values()
        if os.path.isdir(folder)
    )

    return {
        "total_value":    round(total_value, 2),
        "total_cost":     round(total_cost, 2),
        "total_gain":     round(total_gain, 2),
        "total_gain_pct": round(total_gain_pct, 2),
        "num_positions":  len([h for h in holdings if h["symbol"] not in ("CASH",)]),
        "accounts":       {k: round(v, 2) for k, v in sorted(accounts.items(), key=lambda x: -x[1])},
        "allocation":     {k: round(v, 2) for k, v in sorted(allocation.items(), key=lambda x: -x[1])},
        "holdings":       sorted(holdings, key=lambda x: x["market_value"], reverse=True),
        "file_count":     file_count,
    }

# ---------------------------------------------------------------------------
# ── SAMPLE DATA (fallback) ───────────────────────────────────────────────────
# ---------------------------------------------------------------------------
SAMPLE_HOLDINGS = [
    {"account": "Fidelity",       "symbol": "AAPL",  "description": "Apple Inc",                    "quantity": 50,  "last_price": 227.52, "market_value": 11376.00, "cost_basis": 8500.00,  "type": "Stock"},
    {"account": "Fidelity",       "symbol": "MSFT",  "description": "Microsoft Corp",               "quantity": 30,  "last_price": 415.26, "market_value": 12457.80, "cost_basis": 9200.00,  "type": "Stock"},
    {"account": "Fidelity",       "symbol": "VOO",   "description": "Vanguard S&P 500 ETF",         "quantity": 100, "last_price": 501.18, "market_value": 50118.00, "cost_basis": 42000.00, "type": "ETF"},
    {"account": "Schwab",         "symbol": "VTI",   "description": "Vanguard Total Stock Mkt ETF", "quantity": 120, "last_price": 236.58, "market_value": 28389.60, "cost_basis": 22000.00, "type": "ETF"},
    {"account": "Schwab",         "symbol": "META",  "description": "Meta Platforms Inc",           "quantity": 100, "last_price": 660.09, "market_value": 66009.00, "cost_basis": 40000.00, "type": "Stock"},
    {"account": "Morgan Stanley", "symbol": "IBM",   "description": "Intl Business Machines",       "quantity": 98,  "last_price": 296.21, "market_value": 29029.00, "cost_basis": 0,        "type": "Stock"},
]

# ---------------------------------------------------------------------------
# ── ROUTES ───────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    holdings = active_holdings()
    summary  = calculate_summary([dict(h) for h in holdings])
    is_sample = summary["file_count"] == 0
    return render_template("index.html", summary=summary, is_sample=is_sample,
                           broker_folders=BROKER_FOLDERS)

@app.route("/upload", methods=["POST"])
def upload():
    files  = request.files.getlist("files")
    broker = request.form.get("broker", "Fidelity")
    folder = BROKER_FOLDERS.get(broker, BROKER_FOLDERS["Fidelity"])
    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext in (".csv", ".pdf"):
            f.save(os.path.join(folder, f.filename))
    return redirect(url_for("index"))

@app.route("/clear/<broker>")
def clear_broker(broker):
    folder = BROKER_FOLDERS.get(broker)
    if folder and os.path.isdir(folder):
        for f in os.listdir(folder):
            if not f.startswith("."):
                os.remove(os.path.join(folder, f))
    return redirect(url_for("index"))

@app.route("/api/sp500")
def sp500():
    try:
        end    = datetime.now()
        start  = end - timedelta(days=365)
        hist   = yf.Ticker("^GSPC").history(start=start, end=end)
        if hist.empty:
            return jsonify({"error": "No data"}), 500
        base    = hist["Close"].iloc[0]
        returns = ((hist["Close"] - base) / base * 100).round(2)
        return jsonify({
            "dates":  [d.strftime("%Y-%m-%d") for d in returns.index],
            "values": returns.tolist(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/files")
def list_files():
    return jsonify({
        broker: [f for f in os.listdir(folder) if not f.startswith(".")]
        for broker, folder in BROKER_FOLDERS.items()
        if os.path.isdir(folder)
    })

if __name__ == "__main__":
    print("\n  Portfolio Dashboard → http://localhost:5001")
    print(f"  Data folders:")
    for broker, folder in BROKER_FOLDERS.items():
        files = [f for f in os.listdir(folder) if not f.startswith(".")] if os.path.isdir(folder) else []
        print(f"    {broker:20s} → {len(files)} file(s)")
    print()
    app.run(debug=True, port=5001)
