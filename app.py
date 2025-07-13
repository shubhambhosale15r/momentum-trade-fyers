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
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from collections import deque

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

# Enhanced rate limiter implementation
class RateLimiter:
    def __init__(self):
        self.lock = threading.Lock()
        self.request_times = deque()
        self.minute_request_times = deque()
        
    def wait(self):
        with self.lock:
            now = time.time()
            # Handle per-second limit
            while self.request_times and self.request_times[0] <= now - 1:
                self.request_times.popleft()
            if len(self.request_times) >= MAX_REQUESTS_PER_SECOND:
                oldest = self.request_times[0]
                wait_time = 1 - (now - oldest)
                if wait_time > 0:
                    time.sleep(wait_time)
                    now = time.time()
            # Handle per-minute limit
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

initialize_session_state()

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
        <div style="text-align: center; font-size: 1.2rem; color: #c0c0c0;">
            Select a stock universe and click buttons to analyze momentum.<br>
        </div>
    """, unsafe_allow_html=True)

display_header()

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

@st.cache_data(show_spinner=False)
def download_stock_data(ticker, start_date, end_date, retries=5):
    if fyers is None:
        return pd.DataFrame()
    symbol = f"NSE:{ticker}-EQ"
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
                if response.get("s") == "error":
                    error_msg = response.get("message", "Unknown error")
                    print(f"API error for {ticker}: {error_msg}")
                    if "Invalid symbol" in error_msg:
                        return pd.DataFrame()
                    if "request limit reached" in error_msg:
                        time.sleep(0.2)
                        continue
                    break
                candles = response.get("candles", [])
                if not candles:
                    print(f"No candles returned for {ticker} ({current_start} to {current_end})")
                    break
                df_chunk = pd.DataFrame(candles, columns=["timestamp", "Open", "High", "Low", "Close", "Volume"])
                df_chunk["Date"] = pd.to_datetime(df_chunk["timestamp"], unit="s")
                all_data.append(df_chunk)
                successful_chunks += 1
                break
            except Exception as e:
                print(f"Attempt {attempt+1} failed for {ticker}: {e}")
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    print(f"All retries failed for {ticker} chunk {current_start} to {current_end}")
        else:
            print(f"No data for {ticker} in range {current_start} to {current_end}")
        current_start = current_end + timedelta(days=1)
    if not all_data:
        print(f"Failed to get any data for {ticker}. Success: {successful_chunks}/{total_chunks} chunks")
        return pd.DataFrame()
    full_df = pd.concat(all_data, ignore_index=True)
    full_df["Date"] = pd.to_datetime(full_df["timestamp"], unit="s")
    full_df.set_index("Date", inplace=True)
    full_df.sort_index(inplace=True)
    full_df = full_df[~full_df.index.duplicated(keep='first')]
    print(f"Downloaded {len(full_df)} records for {ticker}")
    return full_df[["Open", "High", "Low", "Close", "Volume"]].reset_index()

def calculate_returns(df, period):
    try:
        df = df.dropna(subset=['Close']).copy()
        df.sort_index(inplace=True)
        if len(df) < period:
            return np.nan
        return (df['Close'].iloc[-1] / df['Close'].iloc[-period]) - 1
    except Exception as e:
        print(f"Return calculation error: {e}")
        return np.nan

def process_symbol(t, start, end):
    try:
        df = download_stock_data(t, start, end)
        if df.empty:
            print(f"No data available for {t}")
            return None
        df['Date'] = pd.to_datetime(df['Date'])
        df.set_index('Date', inplace=True)
        data_points = len(df)
        min_required = max(63, 21, 5)
        if data_points < 5:
            print(f"Insufficient data ({data_points} points) for {t}")
            return None
        df['Daily Return'] = df['Close'].pct_change()
        valid_returns = df['Daily Return'].dropna()
        if len(valid_returns) < 5:
            vol = np.nan
        else:
            vol = valid_returns.std() * np.sqrt(63)
        r3 = calculate_returns(df, 63) if data_points >= 63 else np.nan
        r1 = calculate_returns(df, 21) if data_points >= 21 else np.nan
        r0 = calculate_returns(df, 5) if data_points >= 5 else np.nan
        if pd.notna(vol) and vol > 0:
            mom = (((r3 if pd.notna(r3) else 0)) +
                   ((r1 if pd.notna(r1) else 0)) +
                   ((r0 if pd.notna(r0) else 0))) / vol
        else:
            mom = np.nan
        return {
            "Ticker": t,
            "Data Points": data_points,
            "Momentum Score": mom,
            "3-Month Return (%)": r3 * 100 if pd.notna(r3) else np.nan,
            "1-Month Return (%)": r1 * 100 if pd.notna(r1) else np.nan,
            "1-Week Return (%)": r0 * 100 if pd.notna(r0) else np.nan,
            "Annualized Volatility": vol,
            "Price":df['Close'].iloc[-1]
        }
    except Exception as e:
        print(f"Error processing {t}: {str(e)}")
        import traceback
        traceback.print_exc()
        return None

@st.cache_data(show_spinner=False)
def analyze_universe(name, symbols):
    end = datetime.today().date()
    start = end - timedelta(days=400)
    rows = []
    progress_bar = st.progress(0)
    for i, symbol in enumerate(symbols):
        result = process_symbol(symbol, start, end)
        if result is not None:
            rows.append(result)
        progress_bar.progress((i + 1) / len(symbols))
    progress_bar.empty()
    if not rows:
        return pd.DataFrame(), np.nan
    df_res = pd.DataFrame(rows)
    df_res = df_res[df_res["Momentum Score"].notna()]
    avg_score = df_res["Momentum Score"].mean() if not df_res.empty else np.nan
    return df_res, avg_score

def get_top_universes_by_momentum():
    data = []
    for name, syms in stqdm(STOCK_UNIVERSE.items(), desc="Processing Universes", leave=False):
        _, avg = analyze_universe(name, syms)
        data.append({"Stock Universe": name, "Average Momentum Score": avg})
    df = pd.DataFrame(data)
    df = df[df["Average Momentum Score"].notna()]
    return df.sort_values("Average Momentum Score", ascending=False)

def get_top_stocks_from_universe(name, symbols):
    df, _ = analyze_universe(name, symbols)
    return df.sort_values("Momentum Score", ascending=False) if not df.empty else pd.DataFrame()

def get_top_momentum_stocks_overall():
    all_dfs = []
    for name, syms in stqdm(STOCK_UNIVERSE.items(), desc="Processing All Universes", leave=False):
        df, _ = analyze_universe(name, syms)
        if not df.empty:
            all_dfs.append(df)
    if not all_dfs:
        return pd.DataFrame()
    combined = pd.concat(all_dfs, ignore_index=True)
    combined.dropna(subset=["Momentum Score"], inplace=True)
    combined.sort_values("Momentum Score", ascending=False, inplace=True)
    unique = combined.drop_duplicates(subset=["Ticker"], keep="first")
    return unique.head(10)

def display_loading():
    st.markdown(f"""
        <div class='loading-container'>
            <div class="spinner"></div>
            <div>{LOADING_TEXT}</div>
        </div>
    """, unsafe_allow_html=True)

def normalize_column(df, score_column, normalized_column):
    if df.empty:
        df[normalized_column] = np.nan
        return df
    min_score = df[score_column].min()
    max_score = df[score_column].max()
    if pd.isna(min_score) or pd.isna(max_score) or max_score == min_score:
        df[normalized_column] = 0
    else:
        df[normalized_column] = 2 * ((df[score_column] - min_score) / (max_score - min_score)) - 1
    return df

def main():
    # Analyze Stock Universe section
    if st.session_state.analyze_button_clicked:
        st.subheader(f"Momentum Analysis: {stock_universe_name}")
        loading_placeholder = st.empty()
        with loading_placeholder.container():
            display_loading()
            df, _ = analyze_universe(stock_universe_name, STOCK_UNIVERSE[stock_universe_name])
        loading_placeholder.empty()
        if not df.empty:
            df = normalize_column(df, "Momentum Score", "Normalized Momentum Score")
            df = df.sort_values("Normalized Momentum Score", ascending=False)
            st.dataframe(df.style.format({
                "Data Points": "{:.0f}",
                "3-Month Return (%)": "{:.2f}%",
                "1-Month Return (%)": "{:.2f}%",
                "1-Week Return (%)": "{:.2f}%",
                "Annualized Volatility": "{:.4f}",
                # "Momentum Score": "{:.4f}",
                "Normalized Momentum Score": "{:.4f}"
            }), use_container_width=True)
        else:
            st.warning("No data available for this universe.")
        st.session_state.analyze_button_clicked = False
    # Stock Universes Ranks section
    if st.session_state.view_universe_rankings:
        st.subheader("Stock Universes Rankings by Average Momentum")
        loading_placeholder = st.empty()
        with loading_placeholder.container():
            display_loading()
            top_unis = get_top_universes_by_momentum()
        loading_placeholder.empty()
        if not top_unis.empty:
            top_unis = normalize_column(top_unis, "Average Momentum Score", "Normalized Average Momentum Score")
            top_unis = top_unis.sort_values("Normalized Average Momentum Score", ascending=False)
            st.dataframe(top_unis.style.format({
                # "Average Momentum Score": "{:.4f}",
                "Normalized Average Momentum Score": "{:.4f}"
            }), use_container_width=True)
        else:
            st.warning("No data available for universes ranking.")
        st.session_state.view_universe_rankings = False
    # Recommended Stocks section
    if st.session_state.view_recommended_stocks:
        st.subheader("Recommended Stocks (Top 5 from Top 3 Universes)")
        loading_placeholder = st.empty()
        with loading_placeholder.container():
            display_loading()
            top_unis = get_top_universes_by_momentum()
        loading_placeholder.empty()
        if top_unis.empty:
            st.warning("No universe data available.")
        else:
            for index, row in top_unis.iterrows():
                st.markdown(f"### {row['Stock Universe']} (Avg Score: {row['Average Momentum Score']:.4f})")
                universe_loading = st.empty()
                with universe_loading.container():
                    display_loading()
                    top5 = get_top_stocks_from_universe(row['Stock Universe'], STOCK_UNIVERSE[row['Stock Universe']])
                universe_loading.empty()
                if not top5.empty:
                    top5 = normalize_column(top5, "Momentum Score", "Normalized Momentum Score")
                    st.dataframe(top5.head(5).style.format({
                        "Data Points": "{:.0f}",
                        "3-Month Return (%)": "{:.2f}%",
                        "1-Month Return (%)": "{:.2f}%",
                        "1-Week Return (%)": "{:.2f}%",
                        "Annualized Volatility": "{:.4f}",
                        # "Momentum Score": "{:.4f}",
                        "Normalized Momentum Score": "{:.4f}"
                    }), use_container_width=True)
                else:
                    st.write(f"No stocks data for {row['Stock Universe']}")
        st.session_state.view_recommended_stocks = False
    # High Momentum Stocks section
    if st.session_state.view_high_momentum_stocks:
        st.subheader("Top 10 High Momentum Stocks (Across All Universes)")
        loading_placeholder = st.empty()
        with loading_placeholder.container():
            display_loading()
            top_momentum = get_top_momentum_stocks_overall()
        loading_placeholder.empty()
        if not top_momentum.empty:
            top_momentum = normalize_column(top_momentum, "Momentum Score", "Normalized Momentum Score")
            st.dataframe(top_momentum.style.format({
                "Data Points": "{:.0f}",
                "3-Month Return (%)": "{:.2f}%",
                "1-Month Return (%)": "{:.2f}%",
                "1-Week Return (%)": "{:.2f}%",
                "Annualized Volatility": "{:.4f}",
                # "Momentum Score": "{:.4f}",
                "Normalized Momentum Score": "{:.4f}"
            }), use_container_width=True)
        else:
            st.warning("No high momentum data available.")
        st.session_state.view_high_momentum_stocks = False

if __name__ == "__main__":
    if not any([
        st.session_state.analyze_button_clicked,
        st.session_state.view_universe_rankings,
        st.session_state.view_recommended_stocks,
        st.session_state.view_high_momentum_stocks
    ]):
        st.markdown(f"""      
            <div style="text-align: center; font-size: 1.2rem; color: #c0c0c0;">
               Select an option from the sidebar to begin analysis.<br>
            </div>
        """, unsafe_allow_html=True)
    else:
        main()
