"""Unit tests for the IBKR Flex client's error-code handling (no network)."""

import unittest

import pytest

from tradingagents.ibkr import flex_client as fc


class _FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


@pytest.mark.unit
class CodeClassificationTests(unittest.TestCase):
    def test_in_progress_code_is_transient(self):
        # 1019 "Statement generation in progress" is the normal polling response.
        self.assertIn("1019", fc._TRANSIENT_CODES)

    def test_expired_token_is_permanent_with_hint(self):
        self.assertIn("1012", fc._PERMANENT_CODES)
        self.assertNotIn("1012", fc._TRANSIENT_CODES)
        self.assertIn("token", fc._PERMANENT_CODES["1012"].lower())

    def test_describe_permanent_adds_hint(self):
        msg = fc._describe("1012", "Token has expired", "Flex failed")
        self.assertIn("IBKR code 1012", msg)
        self.assertIn("generate a new", msg.lower())


@pytest.mark.unit
class DownloadFlowTests(unittest.TestCase):
    def setUp(self):
        # No real sleeping during tests.
        self._orig_sleep = fc.time.sleep
        fc.time.sleep = lambda *_a, **_k: None

    def tearDown(self):
        fc.time.sleep = self._orig_sleep

    def test_polls_through_in_progress_to_success(self):
        send_ok = '<FlexStatementResponse><Status>Success</Status><ReferenceCode>REF1</ReferenceCode></FlexStatementResponse>'
        in_progress = '<FlexStatementResponse><Status>Warn</Status><ErrorCode>1019</ErrorCode><ErrorMessage>Statement generation in progress</ErrorMessage></FlexStatementResponse>'
        ready = '<FlexQueryResponse queryName="x"><FlexStatements></FlexStatements></FlexQueryResponse>'
        responses = [send_ok, in_progress, in_progress, ready]
        seq = iter(responses)
        orig = fc.requests.get
        fc.requests.get = lambda *a, **k: _FakeResp(next(seq))
        try:
            updates = []
            out = fc.download_flex_xml("tok", "q", progress=updates.append)
        finally:
            fc.requests.get = orig
        self.assertIn("<FlexQueryResponse", out)
        # It actually polled (saw "generating" updates) rather than failing on 1019.
        self.assertTrue(any("generating" in u for u in updates))

    def test_permanent_code_fails_fast_with_hint(self):
        send_expired = '<FlexStatementResponse><Status>Fail</Status><ErrorCode>1012</ErrorCode><ErrorMessage>Token has expired</ErrorMessage></FlexStatementResponse>'
        orig = fc.requests.get
        fc.requests.get = lambda *a, **k: _FakeResp(send_expired)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                fc.download_flex_xml("tok", "q")
        finally:
            fc.requests.get = orig
        self.assertIn("1012", str(ctx.exception))
        self.assertIn("token", str(ctx.exception).lower())


@pytest.mark.unit
class ParseDateAndTradesTests(unittest.TestCase):
    def test_parse_flex_date_formats(self):
        self.assertEqual(fc._parse_flex_date("2026-04-29;093000"), "2026-04-29")
        self.assertEqual(fc._parse_flex_date("20260429;093000"), "2026-04-29")
        self.assertEqual(fc._parse_flex_date("20260429"), "2026-04-29")
        self.assertEqual(fc._parse_flex_date("2026-04-29"), "2026-04-29")
        self.assertEqual(fc._parse_flex_date(""), "")
        self.assertEqual(fc._parse_flex_date("garbage"), "")

    def test_parse_closing_trades_extracts_entry_date(self):
        xml = (
            '<FlexQueryResponse><FlexStatements><FlexStatement><Trades>'
            '<Trade assetCategory="STK" openCloseIndicator="C" buySell="SELL" '
            'symbol="MU" quantity="100" tradePrice="120.0" fifoPnlRealized="500" '
            'ibCommission="-1.0" tradeDate="20260610" openDateTime="20260605;093000" '
            'currencyPrimary="USD" tradeID="T1" ibExecID="E1" />'
            '</Trades></FlexStatement></FlexStatements></FlexQueryResponse>'
        )
        trades = fc.parse_closing_trades(xml)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["ticker"], "MU")
        self.assertEqual(t["direction"], "BUY")          # closed by SELL
        self.assertEqual(t["entry_date"], "2026-06-05")  # from openDateTime
        self.assertEqual(t["exit_date"], "2026-06-10")


@pytest.mark.unit
class ScreenedWithinTests(unittest.TestCase):
    def _make_run(self, root, name, ticker):
        d = root / "earnings" / name / ticker
        d.mkdir(parents=True)
        return d

    def test_entry_within_window(self):
        from pathlib import Path
        import tempfile
        from cli.commands.ibkr import _screened_within
        root = Path(tempfile.mkdtemp())
        self._make_run(root, "earnings_2026-06-01_20260601_1", "MU")

        # screened 2026-06-01, bought 2026-06-05 → 4 days, within 7
        ok = _screened_within({"ticker": "MU", "entry_date": "2026-06-05",
                               "exit_date": "2026-06-12"}, 7, root)
        self.assertTrue(ok)

        # bought 2026-06-20 → 19 days after screen, outside 7
        no = _screened_within({"ticker": "MU", "entry_date": "2026-06-20",
                               "exit_date": "2026-06-25"}, 7, root)
        self.assertFalse(no)

    def test_exit_fallback_when_no_entry(self):
        from pathlib import Path
        import tempfile
        from cli.commands.ibkr import _screened_within
        root = Path(tempfile.mkdtemp())
        self._make_run(root, "earnings_2026-06-01_20260601_1", "MU")
        # no entry date; exit 2026-06-20 is 19d after screen → within 30d fallback
        ok = _screened_within({"ticker": "MU", "entry_date": "", "exit_date": "2026-06-20"}, 7, root)
        self.assertTrue(ok)

    def test_no_screen_rejects(self):
        from pathlib import Path
        import tempfile
        from cli.commands.ibkr import _screened_within
        root = Path(tempfile.mkdtemp())
        self.assertFalse(_screened_within({"ticker": "ZZZZ", "entry_date": "2026-06-05",
                                           "exit_date": "2026-06-12"}, 7, root))


if __name__ == "__main__":
    unittest.main()
