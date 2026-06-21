"""IBKR Flex Web Service client — download and parse trade history."""

import time
import xml.etree.ElementTree as ET

import requests

_SEND_URL = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.SendRequest"
_GET_URL = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement"

_POLL_INTERVAL = 3    # seconds between GetStatement polls
_MAX_ATTEMPTS = 40    # 120 seconds max wait for report generation

# Error codes IBKR returns on SendRequest when the server is temporarily busy.
# These are transient — retrying after a short wait usually succeeds.
_SEND_TRANSIENT_CODES = {"1001", "1003", "1004"}
_SEND_RETRIES = 10    # max SendRequest retries on transient errors
_SEND_RETRY_WAIT = 10 # seconds to wait between SendRequest retries

# Error codes IBKR can return during GetStatement polling that mean "still
# generating" rather than a real failure.  Safe to keep polling on these.
_GET_TRANSIENT_CODES = {"1010", "1011", "1012", "1003", "1004"}


def download_flex_xml(token: str, query_id: str) -> str:
    """Request and download a Flex report. Returns the raw XML string.

    The date range is defined in the query itself (set to 'Last 365 Days'
    in the portal). Call this with the same token + query_id every time.
    """
    # Step 1 — ask IBKR to generate the report (retry on transient errors)
    last_error = None
    for send_attempt in range(1, _SEND_RETRIES + 1):
        r = requests.get(_SEND_URL, params={"t": token, "q": query_id, "v": 3}, timeout=30)
        r.raise_for_status()

        try:
            root = ET.fromstring(r.text)
        except ET.ParseError as e:
            raise RuntimeError(f"Unexpected Flex response: {r.text[:200]}") from e

        status = root.findtext("Status")
        if status == "Success":
            break

        code = root.findtext("ErrorCode", "")
        msg = root.findtext("ErrorMessage", r.text)
        last_error = f"Flex SendRequest failed (code {code}): {msg}"

        if code in _SEND_TRANSIENT_CODES and send_attempt < _SEND_RETRIES:
            time.sleep(_SEND_RETRY_WAIT)
            continue

        raise RuntimeError(last_error)
    else:
        raise RuntimeError(last_error or "Flex SendRequest failed after retries")

    ref_code = root.findtext("ReferenceCode")

    # Step 2 — poll until the report is ready
    for attempt in range(_MAX_ATTEMPTS):
        time.sleep(_POLL_INTERVAL)
        r2 = requests.get(_GET_URL, params={"t": token, "q": ref_code, "v": 3}, timeout=60)
        r2.raise_for_status()

        if "<FlexQueryResponse" in r2.text:
            return r2.text  # full XML report is ready

        # Still generating — parse status response
        try:
            root2 = ET.fromstring(r2.text)
            s2 = root2.findtext("Status", "")
            code2 = root2.findtext("ErrorCode", "")
            # IBKR may issue a new reference code on each status check
            ref_code = root2.findtext("ReferenceCode") or ref_code
            if s2 in ("Success", "In Progress"):
                continue
            # Some IBKR versions return a non-Success status with a transient
            # error code (e.g. 1012 "Statement generation in progress") rather
            # than Status="In Progress". Keep polling on these too.
            if code2 in _GET_TRANSIENT_CODES:
                continue
            raise RuntimeError(f"Flex GetStatement failed: {root2.findtext('ErrorMessage', r2.text)}")
        except ET.ParseError:
            continue  # non-XML response during generation — keep polling

    raise RuntimeError(f"Flex report not ready after {_MAX_ATTEMPTS * _POLL_INTERVAL}s")


def parse_closing_trades(xml_str: str) -> list[dict]:
    """Parse Flex XML and return one dict per closing stock execution.

    Each dict has the fields needed to merge into trades.json.
    Opening trades are skipped — P&L is only available on close.
    """
    root = ET.fromstring(xml_str)
    trades = []

    for trade in root.iter("Trade"):
        # IBKR labels this "Asset Class" in the portal; XML attribute varies by version
        asset_cat = trade.get("assetCategory") or trade.get("assetClass", "")
        if asset_cat != "STK":
            continue
        if trade.get("openCloseIndicator", "") != "C":
            continue

        buy_sell = trade.get("buySell", "")
        qty = abs(float(trade.get("quantity") or 0))
        exit_price = abs(float(trade.get("tradePrice") or 0))
        pnl_gross = float(trade.get("fifoPnlRealized") or 0)
        commission = abs(float(trade.get("ibCommission") or 0))
        net_pnl = pnl_gross - commission

        # Reconstruct entry price from exit price and gross P&L
        # BUY direction (closed by SELL): pnl = (exit - entry) * qty
        # SHORT direction (closed by BUY): pnl = (entry - exit) * qty
        if buy_sell == "SELL":
            direction = "BUY"
            entry_price = (exit_price - pnl_gross / qty) if qty else exit_price
        else:
            direction = "SHORT"
            entry_price = (exit_price + pnl_gross / qty) if qty else exit_price

        cost_basis = entry_price * qty
        pnl_pct = (net_pnl / cost_basis * 100) if cost_basis else 0.0

        # DateTime field is "YYYY-MM-DD;HHMMSS" in IBKR Flex
        date_raw = trade.get("dateTime", "")
        exit_date = trade.get("tradeDate") or (date_raw.split(";")[0] if date_raw else "")

        outcome = "WIN" if net_pnl > 0 else ("LOSS" if net_pnl < 0 else "BREAK_EVEN")

        trades.append({
            "ticker": trade.get("symbol", ""),
            "description": trade.get("description", ""),
            "currency": trade.get("currencyPrimary", "USD"),
            "direction": direction,
            "shares": qty,
            "entry_price": round(entry_price, 4),
            "exit_price": round(exit_price, 4),
            "pnl": round(net_pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "outcome": outcome,
            "exit_date": exit_date,
            "ibkr_trade_id": trade.get("tradeID", ""),
            "ibkr_exec_id": trade.get("ibExecID") or trade.get("execID", ""),
        })

    return trades
