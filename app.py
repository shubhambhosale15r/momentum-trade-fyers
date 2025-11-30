import streamlit as st
import pandas as pd
import numpy as np
import time
import os
import tempfile
from datetime import datetime, timedelta
from fyers_apiv3 import fyersModel
from stocklist import STOCK_UNIVERSE
from stqdm import stqdm
import threading
from collections import deque
import traceback

# -------------------------
# Page & constants
# -------------------------
PAGE_TITLE = "Swing Trade"
PAGE_ICON = "📈"
LOADING_TEXT = "Analyzing Stocks..."
MAX_REQUESTS_PER_SECOND = 10
MAX_REQUESTS_PER_MINUTE = 190

st.set_page_config(
    page_title=PAGE_TITLE,
    page_icon=PAGE_ICON,
    layout="wide",
    menu_items=None,
)

# -------------------------
# Rate limiter
# -------------------------
class RateLimiter:
    def __init__(self):
        self.lock = threading.Lock()
        self.request_times = deque()
        self.minute_request_times = deque()

    def wait(self):
        with self.lock:
            now = time.time()
            # per-second
            while self.request_times and self.request_times[0] <= now - 1:
                self.request_times.popleft()
            if len(self.request_times) >= MAX_REQUESTS_PER_SECOND:
                oldest = self.request_times[0]
                wait_time = 1 - (now - oldest)
                if wait_time > 0:
                    time.sleep(wait_time)
                    now = time.time()
            # per-minute
            while self.minute_request_times and self.minute_request_times[0] <= now - 60:
                self.minute_request_times.popleft()
            if len(self.minute_request_times) >= MAX_REQUESTS_PER_MINUTE:
                oldest_min = self.minute_request_times[0]
                wait_time_min = 60 - (now - oldest_min)
                if wait_time_min > 0:
                    time.sleep(wait_time_min)
                    now = time.time()
                    while self.minute_request_times and self.minute_request_times[0] <= now - 60:
                        self.minute_request_times.popleft()
            self.request_times.append(now)
            self.minute_request_times.append(now)

fyers_rate_limiter = RateLimiter()

# -------------------------
# Session state defaults
# -------------------------
def initialize_session_state():
    if 'view_universe_rankings' not in st.session_state:
        st.session_state.view_universe_rankings = False
    if 'view_recommended_stocks' not in st.session_state:
        st.session_state.view_recommended_stocks = False
    if 'analyze_button_clicked' not in st.session_state:
        st.session_state.analyze_button_clicked = False
    if 'view_high_momentum_stocks' not in st.session_state:
        st.session_state.view_high_momentum_stocks = False
    if 'fyers_access_token' not in st.session_state:
        st.session_state.fyers_access_token = ""
    if 'apply_benchmark' not in st.session_state:
        st.session_state.apply_benchmark = True

initialize_session_state()

# -------------------------
# CSS + header
# -------------------------
def inject_custom_css():
    st.markdown(f"""
        <style>
            .loading-container {{
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                padding: 2rem;
            }}
            .spinner {{
                border: 4px solid rgba(0, 0, 0, 0.1);
                border-radius: 50%;
                border-top: 4px solid #3498db;
                width: 40px;
                height: 40px;
                animation: spin 1s linear infinite;
                margin-bottom: 1rem;
            }}
            @keyframes spin {{
                0% {{ transform: rotate(0deg); }}
                100% {{ transform: rotate(360deg); }}
            }}
        </style>
    """, unsafe_allow_html=True)

inject_custom_css()

def display_header():
    st.markdown(f"""
        <h1 style='text-align: center;'>{PAGE_ICON} {PAGE_TITLE}</h1>
        <div style="text-align: center; font-size: 1.1rem; color: #777;">
            Select a stock universe and click buttons to analyze momentum.<br>
        </div>
    """, unsafe_allow_html=True)

display_header()

# -------------------------
# Sidebar: token + controls
# -------------------------
def create_sidebar():
    with st.sidebar:
        token_input = st.text_input("Enter Fyers Access Token", type="password", value=st.session_state.fyers_access_token)
        if token_input:
            st.session_state.fyers_access_token = token_input

        universe_name = st.radio("Select Stock Universe", list(STOCK_UNIVERSE.keys()))
        st.info(f"Selected: {universe_name}")

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Analyze Stock Universe", use_container_width=True):
                st.session_state.view_universe_rankings = False
                st.session_state.view_recommended_stocks = False
                st.session_state.view_high_momentum_stocks = False
                st.session_state.analyze_button_clicked = True
            if st.button("Stock Universes Ranks", use_container_width=True):
                st.session_state.analyze_button_clicked = False
                st.session_state.view_recommended_stocks = False
                st.session_state.view_high_momentum_stocks = False
                st.session_state.view_universe_rankings = True
        with col2:
            if st.button("Recommended Stocks", use_container_width=True):
                st.session_state.analyze_button_clicked = False
                st.session_state.view_universe_rankings = False
                st.session_state.view_high_momentum_stocks = False
                st.session_state.view_recommended_stocks = True
            if st.button("High Momentum Stocks", use_container_width=True):
                st.session_state.analyze_button_clicked = False
                st.session_state.view_universe_rankings = False
                st.session_state.view_recommended_stocks = False
                st.session_state.view_high_momentum_stocks = True

    return universe_name

stock_universe_name = create_sidebar()

# -------------------------
# Fyers init
# -------------------------
def initialize_fyers():
    if st.session_state.fyers_access_token:
        temp_dir = tempfile.gettempdir()
        log_dir = os.path.join(temp_dir, "fyers_logs")
        os.makedirs(log_dir, exist_ok=True)
        fyers = fyersModel.FyersModel(
            client_id="0F5WWD1SBL-100",
            token=st.session_state.fyers_access_token,
            log_path=log_dir + os.sep
        )
        return fyers
    else:
        st.error("Please enter your Fyers Access Token in the sidebar.")
        return None

fyers = initialize_fyers()

# -------------------------
# Data download (cached)
# -------------------------
@st.cache_data(show_spinner=False)
def download_stock_data(ticker, start_date, end_date, retries=5):
    """
    Downloads daily OHLCV via Fyers. Returns DataFrame with columns: Date, Open, High, Low, Close, Volume
    Handles index vs equity symbol formatting.
    """
    if fyers is None:
        return pd.DataFrame()

    # -------- FIXED INDEX SYMBOL HANDLING --------
    # Indices typically have "-INDEX" and should NOT use "-EQ"
    # Accept also common index raw tickers
    index_like = False
    ticker_upper = str(ticker).upper()
    if "-INDEX" in ticker_upper or ticker_upper in ["NIFTY50", "NIFTY", "NIFTY_50", "NIFTY50-INDEX"]:
        index_like = True

    if index_like:
        symbol = f"NSE:{ticker_upper}"
    else:
        # Assume equity: append -EQ
        symbol = f"NSE:{ticker_upper}-EQ"
    # ---------------------------------------------

    all_data = []
    current_start = start_date
    total_chunks = 0
    successful_chunks = 0

    while current_start <= end_date:
        total_chunks += 1
        current_end = min(current_start + timedelta(days=89), end_date)

        for attempt in range(retries):
            try:
                fyers_rate_limiter.wait()
                data = {
                    "symbol": symbol,
                    "resolution": "D",
                    "date_format": "1",
                    "range_from": current_start.strftime("%Y-%m-%d"),
                    "range_to": current_end.strftime("%Y-%m-%d"),
                    "cont_flag": "1"
                }

                response = fyers.history(data)

                if response is None:
                    print(f"FYERS returned None for {symbol}")
                    time.sleep(0.5)
                    continue

                if response.get("s") == "error":
                    error_msg = response.get("message", "Unknown error")
                    print(f"API error for {ticker} ({symbol}): {error_msg}")

                    # Invalid symbol -> bail out
                    if "Invalid symbol" in error_msg or "Invalid Format" in error_msg:
                        return pd.DataFrame()

                    # Rate limit hint
                    if "request limit reached" in error_msg.lower():
                        time.sleep(0.3)
                        continue

                    break

                candles = response.get("candles", [])
                if not candles:
                    print(f"No candles returned for {ticker} ({symbol}) ({current_start} to {current_end})")
                    break

                df_chunk = pd.DataFrame(
                    candles,
                    columns=["timestamp", "Open", "High", "Low", "Close", "Volume"]
                )

                df_chunk["Date"] = pd.to_datetime(df_chunk["timestamp"], unit="s")
                all_data.append(df_chunk)
                successful_chunks += 1
                break

            except Exception as e:
                print(f"Attempt {attempt+1} failed for {ticker} ({symbol}): {e}")
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    print(f"All retries failed for {ticker} chunk {current_start} to {current_end}")
                    traceback.print_exc()
        current_start = current_end + timedelta(days=1)

    if not all_data:
        print(f"Failed to get any data for {ticker} ({symbol}). Success: {successful_chunks}/{total_chunks} chunks")
        return pd.DataFrame()

    full_df = pd.concat(all_data, ignore_index=True)
    full_df["Date"] = pd.to_datetime(full_df["timestamp"], unit="s")
    full_df.set_index("Date", inplace=True)
    full_df.sort_index(inplace=True)
    full_df = full_df[~full_df.index.duplicated(keep='first')]

    print(f"Downloaded {len(full_df)} records for {ticker} ({symbol})")
    return full_df[["Open", "High", "Low", "Close", "Volume"]].reset_index()

# -------------------------
# Return calculations
# -------------------------
def calculate_returns(df, period):
    try:
        df = df.dropna(subset=['Close']).copy()
        df.sort_index(inplace=True)
        if len(df) < period:
            return np.nan
        # period is number of trading days; use iloc positions
        return (df['Close'].iloc[-1] / df['Close'].iloc[-period]) - 1
    except Exception as e:
        print(f"Return calculation error: {e}")
        return np.nan

# -------------------------
# Index (NIFTY) benchmark functions (cached)
# -------------------------
@st.cache_data(show_spinner=False)
def load_index_data_cached():
    # Use raw ticker without NSE: prefix; download_stock_data will format it
    return download_stock_data(
        "NIFTY50-INDEX",
        datetime.today().date() - timedelta(days=400),
        datetime.today().date()
    )

def calculate_index_score(df):
    """Calculates: (1W + 1M + 3M) / SD(3M daily returns)"""
    if df is None or df.empty:
        return np.nan
    df = df.copy()
    df.set_index("Date", inplace=True)
    df["Daily Return"] = df["Close"].pct_change()

    r1 = calculate_returns(df, 5)    # 1 week ~ 5 trading days
    r2 = calculate_returns(df, 21)   # 1 month ~ 21 trading days
    r3 = calculate_returns(df, 63)   # 3 months ~ 63 trading days

    sd3 = df["Daily Return"].dropna().tail(63).std()

    if any(pd.isna([r1, r2, r3, sd3])) or sd3 == 0:
        return np.nan

    return (r1 + r2 + r3) / sd3

@st.cache_data(show_spinner=False)
def load_index_score_cached():
    df = load_index_data_cached()
    return calculate_index_score(df)

# -------------------------
# Per-symbol processing (uses same formula for stock score)
# -------------------------
def process_symbol_score(t, start, end):
    try:
        df = download_stock_data(t, start, end)
        if df.empty:
            print(f"No data available for {t}")
            return None

        df['Date'] = pd.to_datetime(df['Date'])
        df.set_index('Date', inplace=True)
        data_points = len(df)
        if data_points < 5:
            print(f"Insufficient data ({data_points}) for {t}")
            return None

        df['Daily Return'] = df['Close'].pct_change()
        # Use last 63 daily returns for sd
        last63_daily = df['Daily Return'].dropna().tail(63)
        sd3 = last63_daily.std() if len(last63_daily) > 0 else np.nan

        r1 = calculate_returns(df, 5) if data_points >= 5 else np.nan
        r2 = calculate_returns(df, 21) if data_points >= 21 else np.nan
        r3 = calculate_returns(df, 63) if data_points >= 63 else np.nan

        if any(pd.isna([r1, r2, r3, sd3])) or sd3 == 0:
            stock_score = np.nan
        else:
            stock_score = (r1 + r2 + r3) / sd3

        return {
            "Ticker": t,
            "Data Points": data_points,
            "Stock Score": stock_score,
            "3-Month Return (%)": r3 * 100 if pd.notna(r3) else np.nan,
            "1-Month Return (%)": r2 * 100 if pd.notna(r2) else np.nan,
            "1-Week Return (%)": r1 * 100 if pd.notna(r1) else np.nan,
            "3M SD (daily)": sd3,
            "Price": df['Close'].iloc[-1]
        }
    except Exception as e:
        print(f"Error processing {t}: {e}")
        traceback.print_exc()
        return None

# -------------------------
# Universe analysis (applies benchmark filter optionally)
# -------------------------
@st.cache_data(show_spinner=False)
def analyze_universe(name, symbols, index_score=np.nan, apply_benchmark=True):
    end = datetime.today().date()
    start = end - timedelta(days=400)
    rows = []
    try:
        progress_bar = st.progress(0)
    except Exception:
        progress_bar = None

    for i, symbol in enumerate(symbols):
        result = process_symbol_score(symbol, start, end)
        if result is not None:
            rows.append(result)
        if progress_bar:
            progress_bar.progress((i + 1) / len(symbols))
    if progress_bar:
        progress_bar.empty()

    if not rows:
        return pd.DataFrame(), np.nan, []

    df_res = pd.DataFrame(rows)
    df_res = df_res[df_res["Stock Score"].notna()].copy()

    rejected_stocks = []
    if apply_benchmark and not np.isnan(index_score):
        mask = df_res["Stock Score"] > index_score
        rejected_df = df_res[~mask]
        if not rejected_df.empty:
            rejected_stocks = rejected_df["Ticker"].tolist()
        df_res = df_res[mask].copy()

    avg_score = df_res["Stock Score"].mean() if not df_res.empty else np.nan
    return df_res, avg_score, rejected_stocks

# -------------------------
# Helper top functions
# -------------------------
def get_top_universes_by_score(index_score, apply_benchmark):
    data = []
    for name, syms in stqdm(STOCK_UNIVERSE.items(), desc="Processing Universes", leave=False):
        _, avg, _ = analyze_universe(name, syms, index_score, apply_benchmark)
        data.append({"Stock Universe": name, "Average Stock Score": avg})
    df = pd.DataFrame(data)
    df = df[df["Average Stock Score"].notna()]
    return df.sort_values("Average Stock Score", ascending=False)

def get_top_stocks_from_universe(name, symbols, index_score, apply_benchmark):
    df, _, _ = analyze_universe(name, symbols, index_score, apply_benchmark)
    return df.sort_values("Stock Score", ascending=False) if not df.empty else pd.DataFrame()

def get_top_stocks_overall(index_score, apply_benchmark):
    all_dfs = []
    for name, syms in stqdm(STOCK_UNIVERSE.items(), desc="Processing All Universes", leave=False):
        df, _, _ = analyze_universe(name, syms, index_score, apply_benchmark)
        if not df.empty:
            all_dfs.append(df)
    if not all_dfs:
        return pd.DataFrame()
    combined = pd.concat(all_dfs, ignore_index=True)
    combined.dropna(subset=["Stock Score"], inplace=True)
    combined.sort_values("Stock Score", ascending=False, inplace=True)
    unique = combined.drop_duplicates(subset=["Ticker"], keep="first")
    return unique.head(10)

# -------------------------
# Load index score & display controls
# -------------------------
index_score = np.nan
if fyers is not None:
    try:
        idx_df = load_index_data_cached()
        index_score = calculate_index_score(idx_df)
    except Exception as e:
        print("Failed to load index score:", e)
        index_score = np.nan

# Display index score and toggle
col_a, col_b = st.columns([2, 1])
with col_a:
    st.subheader("📊 Benchmark: NIFTY 50 Score")
    st.write("Score formula: (1W + 1M + 3M) / SD(3M daily returns)")
with col_b:
    st.metric("NIFTY 50 Score", f"{index_score:.4f}" if not np.isnan(index_score) else "N/A")

apply_benchmark_input = st.checkbox("Apply Benchmark Filtering (Stock Score > Index Score)", value=st.session_state.apply_benchmark)
st.session_state.apply_benchmark = apply_benchmark_input

# -------------------------
# Loading spinner helper
# -------------------------
def display_loading():
    st.markdown(f"""
        <div class='loading-container'>
            <div class="spinner"></div>
            <div>{LOADING_TEXT}</div>
        </div>
    """, unsafe_allow_html=True)

# -------------------------
# Main UI logic
# -------------------------
def main():
    # Analyze Stock Universe
    if st.session_state.analyze_button_clicked:
        st.subheader(f"Stock Score Analysis: {stock_universe_name}")
        loading_placeholder = st.empty()
        with loading_placeholder.container():
            display_loading()
            df, _, rejected = analyze_universe(stock_universe_name, STOCK_UNIVERSE[stock_universe_name], index_score, st.session_state.apply_benchmark)
        loading_placeholder.empty()

        if not df.empty:
            df = df.sort_values("Stock Score", ascending=False)
            st.dataframe(df.style.format({
                "Data Points": "{:.0f}",
                "3-Month Return (%)": "{:.2f}%",
                "1-Month Return (%)": "{:.2f}%",
                "1-Week Return (%)": "{:.2f}%",
                "3M SD (daily)": "{:.6f}",
                "Stock Score": "{:.6f}",
                "Price": "{:.2f}"
            }), use_container_width=True)
        else:
            st.warning("No data available for this universe (or no stocks passed the benchmark filter).")

        # Rejected log
        if st.session_state.apply_benchmark:
            with st.expander("📉 Stocks Rejected by Benchmark Filter"):
                if rejected:
                    st.write(pd.DataFrame({"Rejected Stocks": rejected}))
                else:
                    st.write("No rejections — either benchmark missing or all stocks passed the filter.")
        st.session_state.analyze_button_clicked = False

    # Universes rank
    if st.session_state.view_universe_rankings:
        st.subheader("Stock Universes Rankings by Average Stock Score")
        loading_placeholder = st.empty()
        with loading_placeholder.container():
            display_loading()
            top_unis = get_top_universes_by_score(index_score, st.session_state.apply_benchmark)
        loading_placeholder.empty()
        if not top_unis.empty:
            st.dataframe(top_unis.style.format({
                "Average Stock Score": "{:.6f}"
            }), use_container_width=True)
        else:
            st.warning("No data available for universes ranking.")
        st.session_state.view_universe_rankings = False

    # Recommended Stocks
    if st.session_state.view_recommended_stocks:
        st.subheader("Recommended Stocks (Top 5 from Top Universes)")
        loading_placeholder = st.empty()
        with loading_placeholder.container():
            display_loading()
            top_unis = get_top_universes_by_score(index_score, st.session_state.apply_benchmark)
        loading_placeholder.empty()
        if top_unis.empty:
            st.warning("No universe data available.")
        else:
            for index, row in top_unis.head(10).iterrows():
                st.markdown(f"### {row['Stock Universe']} (Avg Score: {row['Average Stock Score']:.6f})")
                universe_loading = st.empty()
                with universe_loading.container():
                    display_loading()
                    top5 = get_top_stocks_from_universe(row['Stock Universe'], STOCK_UNIVERSE[row['Stock Universe']], index_score, st.session_state.apply_benchmark)
                universe_loading.empty()
                if not top5.empty:
                    st.dataframe(top5.head(5).style.format({
                        "Data Points": "{:.0f}",
                        "3-Month Return (%)": "{:.2f}%",
                        "1-Month Return (%)": "{:.2f}%",
                        "1-Week Return (%)": "{:.2f}%",
                        "3M SD (daily)": "{:.6f}",
                        "Stock Score": "{:.6f}",
                        "Price": "{:.2f}"
                    }), use_container_width=True)
                else:
                    st.write(f"No stocks data for {row['Stock Universe']} (or none passed the benchmark).")
        st.session_state.view_recommended_stocks = False

    # Top overall stocks
    if st.session_state.view_high_momentum_stocks:
        st.subheader("Top 10 Stocks by Stock Score (Across All Universes)")
        loading_placeholder = st.empty()
        with loading_placeholder.container():
            display_loading()
            top_momentum = get_top_stocks_overall(index_score, st.session_state.apply_benchmark)
        loading_placeholder.empty()
        if not top_momentum.empty:
            st.dataframe(top_momentum.style.format({
                "Data Points": "{:.0f}",
                "3-Month Return (%)": "{:.2f}%",
                "1-Month Return (%)": "{:.2f}%",
                "1-Week Return (%)": "{:.2f}%",
                "3M SD (daily)": "{:.6f}",
                "Stock Score": "{:.6f}",
                "Price": "{:.2f}"
            }), use_container_width=True)
        else:
            st.warning("No high-score data available (or none passed the benchmark).")
        st.session_state.view_high_momentum_stocks = False

# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    if not any([
        st.session_state.analyze_button_clicked,
        st.session_state.view_universe_rankings,
        st.session_state.view_recommended_stocks,
        st.session_state.view_high_momentum_stocks
    ]):
        st.markdown(f"""      
            <div style="text-align: center; font-size: 1.1rem; color: #777;">
               Select an option from the sidebar to begin analysis.<br>
            </div>
        """, unsafe_allow_html=True)
    else:
        main()
