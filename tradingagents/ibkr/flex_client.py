"""IBKR Flex Web Service client — download and parse trade history."""

import time
import xml.etree.ElementTree as ET

import requests

_SEND_URL = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.SendRequest"
_GET_URL = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement"

# IBKR Flex error codes that mean "the report isn't ready yet — try again
# shortly". Safe to keep polling (GetStatement) or to retry (SendRequest).
# NB: 1019 ("Statement generation in progress") is the *normal* response while a
# report builds — it MUST be treated as transient, or the very first poll fails.
_TRANSIENT_CODES = {
    "1001",  # Statement could not be generated at this time
    "1004",  # Statement is incomplete at this time
    "1005",  # Settlement data is not ready
    "1006",  # FIFO P/L data is not ready
    "1007",  # MTM P/L data is not ready
    "1008",  # MTM and FIFO P/L data is not ready
    "1009",  # Statement is not ready at this time
    "1019",  # Statement generation in progress  ← the common one
    "1021",  # Statement could not be retrieved at this time
}
_RATE_LIMIT_CODE = "1018"  # Too many requests — back off harder, then retry

# Codes that won't fix themselves on retry — fail fast with an actionable hint.
_PERMANENT_CODES = {
    "1003": "Statement is not available — check the Flex query's date range / configuration.",
    "1010": "Legacy Flex Queries are no longer supported — recreate it as an Activity Flex Query.",
    "1011": "Flex service account is inactive — enable the Flex Web Service in IBKR Account Settings.",
    "1012": "Flex token has expired — generate a new one (IBKR → Settings → Flex Web Service) and update IBKR_FLEX_TOKEN.",
    "1013": "Request blocked by an IP restriction on the Flex token.",
    "1014": "Flex query is invalid — check IBKR_FLEX_QUERY_ID.",
    "1015": "Flex token is invalid — check IBKR_FLEX_TOKEN.",
    "1016": "Account is invalid for this query.",
    "1017": "Reference code is invalid or expired — retry the whole request.",
    "1020": "Invalid request or unable to validate request.",
}

_POLL_INTERVAL = 5         # base seconds between GetStatement polls
_MAX_POLL_SECONDS = 300    # give report generation up to 5 minutes
_SEND_RETRIES = 12         # max SendRequest attempts on transient errors
_SEND_RETRY_WAIT = 8       # base seconds between SendRequest retries
_RATE_LIMIT_WAIT = 30      # longer backoff when rate-limited (1018)


def _describe(code: str, msg: str, fallback: str) -> str:
    """Human-readable error string, with a fix hint for known permanent codes."""
    if code in _PERMANENT_CODES:
        return f"{_PERMANENT_CODES[code]} (IBKR code {code})"
    return f"{fallback} (code {code}): {msg}"


def download_flex_xml(token: str, query_id: str, progress=None, debug=None) -> str:
    """Request and download a Flex report. Returns the raw XML string.

    The date range is defined in the query itself (e.g. 'Last 365 Days' in the
    portal) — it cannot be overridden from this API call. Call this with the same
    token + query_id every time.

    `progress` is an optional callable(str) for human-readable status updates.
    `debug`    is an optional callable(str) that receives the raw IBKR responses
               (the actual XML/status), for troubleshooting.
    """
    def _say(message: str) -> None:
        if progress:
            try:
                progress(message)
            except Exception:
                pass

    def _dbg(message: str) -> None:
        if debug:
            try:
                debug(message)
            except Exception:
                pass

    # Step 1 — ask IBKR to generate the report (retry on transient/busy errors).
    ref_code = None
    last_error = None
    last_code = ""
    for attempt in range(1, _SEND_RETRIES + 1):
        try:
            r = requests.get(_SEND_URL, params={"t": token, "q": query_id, "v": 3}, timeout=30)
            r.raise_for_status()
        except requests.RequestException as e:
            last_error = f"Network error contacting IBKR Flex: {e}"
            _say(f"Network error (attempt {attempt}/{_SEND_RETRIES}); retrying in {_SEND_RETRY_WAIT}s…")
            time.sleep(_SEND_RETRY_WAIT)
            continue

        _dbg(f"SendRequest (attempt {attempt}) raw response:\n{r.text.strip()}")
        try:
            root = ET.fromstring(r.text)
        except ET.ParseError as e:
            raise RuntimeError(f"Unexpected Flex response: {r.text[:200]}") from e

        if root.findtext("Status") == "Success":
            ref_code = root.findtext("ReferenceCode")
            _dbg(f"SendRequest accepted; reference code = {ref_code}")
            break

        code = root.findtext("ErrorCode", "")
        msg = root.findtext("ErrorMessage", r.text)
        last_code = code
        if code in _PERMANENT_CODES:
            raise RuntimeError(_describe(code, msg, "Flex request rejected"))

        last_error = _describe(code, msg, "Flex SendRequest failed")
        if attempt < _SEND_RETRIES and (code in _TRANSIENT_CODES or code == _RATE_LIMIT_CODE):
            wait = _RATE_LIMIT_WAIT if code == _RATE_LIMIT_CODE else _SEND_RETRY_WAIT
            note = "rate-limited" if code == _RATE_LIMIT_CODE else "report not ready"
            _say(f"IBKR {note} (attempt {attempt}/{_SEND_RETRIES}); waiting {wait}s…")
            time.sleep(wait)
            continue
        raise RuntimeError(last_error)

    if not ref_code:
        hint = ""
        if last_code in _TRANSIENT_CODES or last_code == _RATE_LIMIT_CODE:
            hint = (
                "\nIBKR kept reporting the statement 'not ready', which usually means the "
                "query is too large or the service is busy. Try a Flex query with a shorter "
                "date range (e.g. Last 30 Days) — the range is set in the IBKR portal, not "
                "here — or download the XML manually and use --file."
            )
        raise RuntimeError((last_error or "Flex SendRequest failed after retries") + hint)

    # Step 2 — poll GetStatement until the XML report is ready.
    _say("Report requested; waiting for IBKR to generate it…")
    deadline = time.time() + _MAX_POLL_SECONDS
    poll = 0
    while time.time() < deadline:
        poll += 1
        time.sleep(_POLL_INTERVAL)
        try:
            r2 = requests.get(_GET_URL, params={"t": token, "q": ref_code, "v": 3}, timeout=60)
            r2.raise_for_status()
        except requests.RequestException as e:
            _say(f"Network hiccup while polling (poll {poll}); retrying… ({e})")
            continue

        if "<FlexQueryResponse" in r2.text:
            _dbg(f"GetStatement poll {poll}: report ready ({len(r2.text)} bytes)")
            return r2.text  # full XML report is ready

        try:
            root2 = ET.fromstring(r2.text)
        except ET.ParseError:
            continue  # non-XML response mid-generation — keep polling

        status = root2.findtext("Status", "")
        code2 = root2.findtext("ErrorCode", "")
        msg2 = root2.findtext("ErrorMessage", r2.text)
        _dbg(f"GetStatement poll {poll} → status={status!r} code={code2!r} msg={msg2!r}")
        # IBKR may issue a fresh reference code on each status check.
        ref_code = root2.findtext("ReferenceCode") or ref_code

        # Keep polling while it's still generating: explicit "In Progress",
        # any transient code (incl. 1019), or no error code at all.
        if status in ("Success", "In Progress") or not code2 or code2 in _TRANSIENT_CODES:
            _say(f"Report generating… (poll {poll})")
            continue
        if code2 == _RATE_LIMIT_CODE:
            _say(f"Rate-limited while polling; backing off {_RATE_LIMIT_WAIT}s…")
            time.sleep(_RATE_LIMIT_WAIT)
            continue
        # Anything else is a real failure.
        raise RuntimeError(_describe(code2, msg2, "Flex GetStatement failed"))

    raise RuntimeError(
        f"Flex report not ready after {_MAX_POLL_SECONDS}s of polling. IBKR can be "
        "slow for large date ranges — try again shortly, or download the XML "
        "manually and run: tradingagents import-ibkr --file <path>"
    )


def _parse_flex_date(raw: str) -> str:
    """Return YYYY-MM-DD from an IBKR Flex datetime, or "" if unparseable.

    Handles "YYYY-MM-DD;HHMMSS", "YYYYMMDD;HHMMSS", "YYYY-MM-DD", "YYYYMMDD".
    """
    if not raw:
        return ""
    head = str(raw).split(";")[0].split(" ")[0].strip().replace("-", "")
    if len(head) >= 8 and head[:8].isdigit():
        return f"{head[:4]}-{head[4:6]}-{head[6:8]}"
    return ""


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

        # Exit (close) date — "YYYY-MM-DD;HHMMSS" or "YYYYMMDD" in IBKR Flex.
        exit_date = _parse_flex_date(trade.get("tradeDate") or trade.get("dateTime", ""))

        # Entry (open) date of the closed lot — present when the Flex query
        # includes the "Open Date/Time" or "Holding Period Date/Time" field.
        entry_date = _parse_flex_date(
            trade.get("openDateTime") or trade.get("holdingPeriodDateTime", "")
        )

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
            "entry_date": entry_date,
            "exit_date": exit_date,
            "ibkr_trade_id": trade.get("tradeID", ""),
            "ibkr_exec_id": trade.get("ibExecID") or trade.get("execID", ""),
        })

    return trades
