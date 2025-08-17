import streamlit as st
import pandas as pd
import numpy as np
import time
import os
import tempfile
from datetime import datetime, timedelta
from fyers_apiv3 import fyersModel
from stocklist import STOCK_UNIVERSE
import threading
from collections import deque
import warnings

# Suppress Streamlit's threading warnings
warnings.filterwarnings("ignore", category=UserWarning, message=".*missing ScriptRunContext.*")

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
    if 'current_universe' not in st.session_state:
        st.session_state.current_universe = None

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
            .progress-container {{
                margin: 1rem 0;
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
        st.session_state.current_universe = universe_name
        st.info(f"Selected: {universe_name}")
    
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Analyze Stock Universe", use_container_width=True, key="analyze_btn"):
                st.session_state.view_universe_rankings = False
                st.session_state.view_recommended_stocks = False
                st.session_state.view_high_momentum_stocks = False
                st.session_state.analyze_button_clicked = True
            
            if st.button("Stock Universes Ranks", use_container_width=True, key="ranks_btn"):
                st.session_state.analyze_button_clicked = False
                st.session_state.view_recommended_stocks = False
                st.session_state.view_high_momentum_stocks = False
                st.session_state.view_universe_rankings = True
        
        with col2:
            if st.button("Recommended Stocks", use_container_width=True, key="recommended_btn"):
                st.session_state.analyze_button_clicked = False
                st.session_state.view_universe_rankings = False
                st.session_state.view_high_momentum_stocks = False
                st.session_state.view_recommended_stocks = True
            
            if st.button("High Momentum Stocks", use_container_width=True, key="momentum_btn"):
                st.session_state.analyze_button_clicked = False
                st.session_state.view_universe_rankings = False
                st.session_state.view_recommended_stocks = False
                st.session_state.view_high_momentum_stocks = True

create_sidebar()

def initialize_fyers():
    if st.session_state.fyers_access_token:
        try:
            temp_dir = tempfile.gettempdir()
            log_dir = os.path.join(temp_dir, "fyers_logs")
            os.makedirs(log_dir, exist_ok=True)
            fyers = fyersModel.FyersModel(
                client_id="0F5WWD1SBL-100",
                token=st.session_state.fyers_access_token,
                log_path=log_dir + os.sep
            )
            return fyers
        except Exception as e:
            st.error(f"Failed to initialize Fyers: {str(e)}")
            return None
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
    
    while current_start <= end_date:
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
                    if "Invalid symbol" in error_msg:
                        return pd.DataFrame()
                    if "request limit reached" in error_msg:
                        time.sleep(0.2)
                        continue
                    break
                
                candles = response.get("candles", [])
                if not candles:
                    break
                
                df_chunk = pd.DataFrame(candles, columns=["timestamp", "Open", "High", "Low", "Close", "Volume"])
                df_chunk["Date"] = pd.to_datetime(df_chunk["timestamp"], unit="s")
                all_data.append(df_chunk)
                break
                
            except Exception as e:
                if attempt == retries - 1:
                    break
                time.sleep(2 ** attempt)
        
        current_start = current_end + timedelta(days=1)
    
    if not all_data:
        return pd.DataFrame()
    
    full_df = pd.concat(all_data, ignore_index=True)
    full_df["Date"] = pd.to_datetime(full_df["timestamp"], unit="s")
    full_df.set_index("Date", inplace=True)
    full_df.sort_index(inplace=True)
    full_df = full_df[~full_df.index.duplicated(keep='first')]
    return full_df[["Open", "High", "Low", "Close", "Volume"]].reset_index()

def calculate_returns(df, period):
    try:
        df = df.dropna(subset=['Close']).copy()
        df.sort_index(inplace=True)
        if len(df) < period:
            return np.nan
        return (df['Close'].iloc[-1] / df['Close'].iloc[-period]) - 1
    except Exception as e:
        return np.nan

def process_symbol(t, start, end):
    try:
        df = download_stock_data(t, start, end)
        if df.empty or len(df) < 63:
            return None

        # ====== PRICE MOMENTUM CALCULATION ======
        df['Daily Return'] = df['Close'].pct_change()
        valid_returns = df['Daily Return'].dropna()
        
        # Require at least 21 days for volatility to be meaningful
        vol = valid_returns.std() * np.sqrt(63) if len(valid_returns) >= 21 else np.nan
        
        # Calculate returns (annualized)
        r3 = calculate_returns(df, 63)
        r1 = calculate_returns(df, 21)
        r0 = calculate_returns(df, 5)
        
        # Filter: Require at least 3 positive returns
        if not (r3 > 0 and r1 > 0 and r0 > 0):
            return None

        # ====== PVT CALCULATION (FIXED) ======
        pvt = (df['Close'].pct_change() * df['Volume']).fillna(0).cumsum()
        pvt_5 = pvt.iloc[-1] - pvt.iloc[-5] if len(pvt) >= 5 else np.nan
        pvt_21 = pvt.iloc[-1] - pvt.iloc[-21] if len(pvt) >= 21 else np.nan
        pvt_63 = pvt.iloc[-1] - pvt.iloc[-63] if len(pvt) >= 63 else np.nan
        
        # Filter: Reject if PVT is not rising in at least 3 timeframes
        if not (pvt_5 > 0 and pvt_21 > 0 and pvt_63 > 0) :
            return None

        # ====== NORMALIZATION IMPROVEMENTS ======
        # Use MEDIAN volume (resistant to outliers) and cap extremes
        median_volume = df['Volume'].tail(63).median()
        
        pvt_5_norm = np.log1p(pvt_5 / median_volume)   if median_volume > 0 else 0  # Log scale for better distribution
        pvt_21_norm = np.log1p(pvt_21 / median_volume) if median_volume > 0 else 0  # Log scale for better distribution
        pvt_63_norm = np.log1p(pvt_63 / median_volume) if median_volume > 0 else 0  # Log scale for better distribution

        # pvt_5_norm = min(pvt_5 / median_volume, 5) if median_volume > 0 else 0  # Cap at 5x
        # pvt_21_norm = min(pvt_21 / median_volume, 10) if median_volume > 0 else 0  # Cap at 10x
        # pvt_63_norm = min(pvt_63 / median_volume, 15) if median_volume > 0 else 0  # Cap at 15x

        # ====== MOMENTUM SCORING ======
        # Price momentum (volatility-adjusted only if returns are positive)
        if r3 > 0 and r1 > 0 and r0 > 0:
            price_mom = (0.5*r0 + 0.3*r1 + 0.2*r3) / vol if vol > 0 else 0
        else:
            price_mom = (r3 + r1 + r0)  # No vol scaling for negative returns
        
        # PVT momentum (averaged and capped)
        pvt_mom = (0.6 * pvt_5_norm + 0.3 * pvt_21_norm + 0.1 * pvt_63_norm)

        # Final score (70% price momentum, 30% PVT)
        final_mom = (0.5 * price_mom) + (0.5 * pvt_mom)

        # ====== LIQUIDITY FILTERS ======
        avg_volume = df['Volume'].tail(63).mean()
        # if avg_volume < 100000 or df['Close'].iloc[-1] < 10:  # Min 100K shares and $10 price
            # return None

        return {
            "Ticker": t,
            "Final Score": final_mom,
            "Price Momentum": price_mom,
            "PVT Momentum": pvt_mom,
            "PVT 5D (Norm)": pvt_5_norm,
            "PVT 21D (Norm)": pvt_21_norm,
            "PVT 63D (Norm)": pvt_63_norm,
            "3M Return (%)": r3 * 100,
            "1M Return (%)": r1 * 100,
            "1W Return (%)": r0 * 100,
            "Avg Volume (L)": f"{avg_volume/100000:.1f}",
            "Annualized Vol": vol
        }
    except Exception as e:
        return None
def analyze_universe(name, symbols):
    end = datetime.today().date()
    start = end - timedelta(days=400)
    rows = []
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    for i, symbol in enumerate(symbols):
        status_text.text(f"Processing {symbol} ({i+1}/{len(symbols)})")
        progress_bar.progress((i + 1) / len(symbols))
        
        result = process_symbol(symbol, start, end)
        if result is not None:
            rows.append(result)
    
    progress_bar.empty()
    status_text.empty()
    
    if not rows:
        return pd.DataFrame(), np.nan
    
    df_res = pd.DataFrame(rows)
    df_res = df_res[df_res["Final Score"].notna()]
    avg_score = df_res["Final Score"].mean() if not df_res.empty else np.nan
    return df_res, avg_score

def get_top_universes_by_momentum():
    data = []
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    universes = list(STOCK_UNIVERSE.items())
    for i, (name, syms) in enumerate(universes):
        status_text.text(f"Processing {name} ({i+1}/{len(universes)})")
        progress_bar.progress((i + 1) / len(universes))
        
        _, avg = analyze_universe(name, syms)
        data.append({"Stock Universe": name, "Average Momentum Score": avg})
    
    progress_bar.empty()
    status_text.empty()
    
    df = pd.DataFrame(data)
    df = df[df["Average Momentum Score"].notna()]
    return df.sort_values("Average Momentum Score", ascending=False)

def get_top_stocks_from_universe(name, symbols):
    df, _ = analyze_universe(name, symbols)
    return df.sort_values("Final Score", ascending=False) if not df.empty else pd.DataFrame()

def get_top_momentum_stocks_overall():
    all_dfs = []
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    universes = list(STOCK_UNIVERSE.items())
    for i, (name, syms) in enumerate(universes):
        status_text.text(f"Processing {name} ({i+1}/{len(universes)})")
        progress_bar.progress((i + 1) / len(universes))
        
        df, _ = analyze_universe(name, syms)
        if not df.empty:
            all_dfs.append(df)
    
    progress_bar.empty()
    status_text.empty()
    
    if not all_dfs:
        return pd.DataFrame()
    
    combined = pd.concat(all_dfs, ignore_index=True)
    combined.dropna(subset=["Final Score"], inplace=True)
    combined.sort_values("Final Score", ascending=False, inplace=True)
    unique = combined.drop_duplicates(subset=["Ticker"], keep="first")
    return unique.head(20)

def display_analysis_results():
    if st.session_state.analyze_button_clicked and st.session_state.current_universe:
        st.subheader(f"Momentum Analysis: {st.session_state.current_universe}")
        df, _ = analyze_universe(st.session_state.current_universe, STOCK_UNIVERSE[st.session_state.current_universe])
        
        if not df.empty:
            df = df.sort_values("Final Score", ascending=False)
            st.dataframe(df.style.format({
                "Final Score": "{:.4f}",
                "Price Momentum": "{:.4f}",
                "PVT Momentum": "{:.4f}",
                "PVT 5D (Norm)": "{:.2f}",
                "PVT 21D (Norm)": "{:.2f}",
                "PVT 63D (Norm)": "{:.2f}",
                "3M Return (%)": "{:.2f}%",
                "1M Return (%)": "{:.2f}%",
                "1W Return (%)": "{:.2f}%",
                "Annualized Vol": "{:.4f}"
            }), use_container_width=True)
        else:
            st.warning("No data available for this universe.")
        
        st.session_state.analyze_button_clicked = False

    elif st.session_state.view_universe_rankings:
        st.subheader("Stock Universes Rankings by Average Momentum")
        top_unis = get_top_universes_by_momentum()
        
        if not top_unis.empty:
            st.dataframe(top_unis.style.format({
                "Average Momentum Score": "{:.4f}"
            }), use_container_width=True)
        else:
            st.warning("No data available for universes ranking.")
        
        st.session_state.view_universe_rankings = False

    elif st.session_state.view_recommended_stocks:
        st.subheader("Recommended Stocks (Top 5 from Top 3 Universes)")
        top_unis = get_top_universes_by_momentum()
        
        if top_unis.empty:
            st.warning("No universe data available.")
        else:
            for index, row in top_unis.iterrows():
                st.markdown(f"### {row['Stock Universe']} (Avg Score: {row['Average Momentum Score']:.4f})")
                top5 = get_top_stocks_from_universe(row['Stock Universe'], STOCK_UNIVERSE[row['Stock Universe']])
                
                if not top5.empty:
                    st.dataframe(top5.head(5).style.format({
                        "Final Score": "{:.4f}",
                        "Price Momentum": "{:.4f}",
                        "PVT Momentum": "{:.4f}",
                        "PVT 5D (Norm)": "{:.2f}",
                        "PVT 21D (Norm)": "{:.2f}",
                        "PVT 63D (Norm)": "{:.2f}",
                        "3M Return (%)": "{:.2f}%",
                        "1M Return (%)": "{:.2f}%",
                        "1W Return (%)": "{:.2f}%",
                        "Annualized Vol": "{:.4f}"
                    }), use_container_width=True)
                else:
                    st.write(f"No stocks data for {row['Stock Universe']}")
        
        st.session_state.view_recommended_stocks = False

    elif st.session_state.view_high_momentum_stocks:
        st.subheader("Top 10 High Momentum Stocks (Across All Universes)")
        top_momentum = get_top_momentum_stocks_overall()
        
        if not top_momentum.empty:
            st.dataframe(top_momentum.style.format({
                "Final Score": "{:.4f}",
                "Price Momentum": "{:.4f}",
                "PVT Momentum": "{:.4f}",
                "PVT 5D (Norm)": "{:.2f}",
                "PVT 21D (Norm)": "{:.2f}",
                "PVT 63D (Norm)": "{:.2f}",
                "3M Return (%)": "{:.2f}%",
                "1M Return (%)": "{:.2f}%",
                "1W Return (%)": "{:.2f}%",
                "Annualized Vol": "{:.4f}"
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
        display_analysis_results()
