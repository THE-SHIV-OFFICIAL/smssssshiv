"""Offline tests: TemporaSMS parsing/routing, sales logger (real code in spoiler, forwarded to buyer,
posted only after the code was delivered). Run in a disposable DATA_DIR (see AGENTS.md)."""
import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.update(API_ID="1", API_HASH="x", BOT_TOKEN="1:x", ADMIN_ID="1", OWNER_ID="1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import database as d
from utils import states
from plugins import social, numflow, salelog, temporasms as tp


class ParserTests(unittest.TestCase):
    def test_price_shapes(self):
        a = {"22": {"wa": {"cost": 0.12, "count": 50}}, "6": {"wa": {"cost": 0.05, "count": 0}}, "0": {"wa": {"cost": "0.3", "count": "7"}}}
        self.assertEqual(tp.parse_prices(a, "wa"), [("22", 0.12, 50), ("0", 0.3, 7)])        # sold-out dropped, cheap first
        b = {"wa": {"22": {"cost": 0.12, "count": 50}}}
        self.assertEqual(tp.parse_prices(b, "wa"), [("22", 0.12, 50)])
        c = {"22": {"wa": {"0.2": 4, "0.12": 6, "0.5": 0}}}
        self.assertEqual(tp.parse_prices(c, "wa"), [("22", 0.12, 10)])

    def test_status_normalisation(self):
        n = tp.normalize_status
        self.assertEqual(n("STATUS_OK:123456"), "STATUS_OK:123456")
        self.assertEqual(n("STATUS_OK:Your WhatsApp code: 123-456"), "STATUS_OK:123456")
        self.assertEqual(n("STATUS_OK:Telegram code: 55221. Do not share"), "STATUS_OK:55221")
        self.assertEqual(n('{"status":"ok","code":"4455"}'), "STATUS_OK:4455")
        self.assertEqual(n('{"status":"waiting"}'), "STATUS_WAIT_CODE")
        self.assertEqual(n("STATUS_CANCEL"), "STATUS_CANCEL")

    def test_services(self):
        self.assertEqual(tp._parse_services('{"services":[{"code":"ig","name":"Instagram"}]}'), [("ig", "Instagram")])
        self.assertEqual(tp._parse_services('{"ig":"Instagram"}'), [("ig", "Instagram")])


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        states.user_locks.clear()
        numflow._unlocked.clear(); numflow._pollers.clear(); numflow._locks.clear()
        for t in ("number_orders", "delivery_thanks"):
            try: d.cur.execute(f"DELETE FROM {t}")
            except Exception: pass
        self.uid = 7001
        d.ensure_user(self.uid)
        d.cur.execute("UPDATE users SET balance=1000, terms_accepted=1 WHERE user_id=?", (self.uid,)); d.db.commit()
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=42)), edit_message=AsyncMock(), send_file=AsyncMock())
        self.ev = SimpleNamespace(sender_id=self.uid, message_id=5, edit=AsyncMock(), answer=AsyncMock(), respond=AsyncMock())
        self.logs, self.thanks = [], []

        async def fake_post(title, body, buyer_id=None): self.logs.append((title, body, buyer_id))
        async def fake_thanks(b, u, k): self.thanks.append((u, k, len(self.logs)))
        self.fake_post, self.fake_thanks = fake_post, fake_thanks
        self.rows = [("22", "India", "IN", 0.04, 10, 0)]

    def _patches(self, status):
        return (patch.object(social, "gz_rows", new=AsyncMock(return_value=self.rows)),
                patch("plugins.temporasms.get_number", new=AsyncMock(return_value={"activationId": "A9", "phoneNumber": "9112345678901", "activationCost": 0.04})),
                patch("plugins.temporasms.get_status", new=AsyncMock(return_value=status)),
                patch("plugins.temporasms.set_status", new=AsyncMock()), patch("plugins.profit.notify_profit", new=AsyncMock()),
                patch("plugins.salelog.post_sale", new=self.fake_post), patch("utils.delivery.send_delivery_thanks", new=self.fake_thanks),
                patch.object(asyncio, "sleep", new=AsyncMock()))

    async def _buy(self, code, server, status):
        ps = self._patches(status)
        for p in ps: p.start()
        try: await social.buy_number(self.bot, self.ev, code, server, "22")
        finally:
            for p in ps: p.stop()

    async def test_all_apps_otp_is_logged_with_real_code_and_forwarded(self):
        await self._buy("ig", 2, "STATUS_OK:445566")
        self.assertEqual(len(self.logs), 1)
        _, body, buyer = self.logs[0]
        self.assertIn("<tg-spoiler>445566</tg-spoiler>", body)
        self.assertNotIn("●●●●●", body)
        self.assertEqual(buyer, self.uid)                      # forwarded to the buyer as confirmation
        self.assertIn("2 · Fast & Smart Service", body)
        self.assertEqual(self.thanks[0][2], 1)                 # thank-you only after the log / code

    async def test_whatsapp_server2_and_change_tg_no_use_temporasms(self):
        for code, srv in (("wa", 2), ("tc", 3)):
            self.logs.clear()
            await self._buy(code, srv, "STATUS_OK:99887")
            o = numflow.load(d.cur.execute("SELECT id FROM number_orders ORDER BY id DESC LIMIT 1").fetchone()[0])
            self.assertEqual(o["meta"]["prov"], "tempora")
            self.assertIn("99887", self.logs[-1][1])

    async def test_cancelled_order_sends_no_log_and_no_thanks(self):
        before = d.cur.execute("SELECT balance FROM users WHERE user_id=?", (self.uid,)).fetchone()[0]
        await self._buy("wa", 2, "STATUS_CANCEL")
        self.assertEqual((self.logs, self.thanks), ([], []))
        self.assertEqual(d.cur.execute("SELECT balance FROM users WHERE user_id=?", (self.uid,)).fetchone()[0], before)

    async def test_tglion_log_only_after_code_and_only_once(self):
        import plugins.tglion as tgl
        cs = {"US": {"name": "United States", "qty": 5, "price": 0.5}}
        with patch.object(tgl, "tl_countries", new=AsyncMock(return_value=cs)), \
             patch.object(tgl, "tl_buy", new=AsyncMock(return_value={"Number": "+18452000111", "price": 0.5})), \
             patch("plugins.profit.notify_profit", new=AsyncMock()), patch("plugins.salelog.post_sale", new=self.fake_post), \
             patch.object(social, "send_delivery_thanks", new=self.fake_thanks):
            await social.tl_buy_flow(self.ev, "US")
            self.assertEqual(self.logs, [])                      # purchase alone is NOT logged (no code yet)
            with patch.object(tgl, "tl_code", new=AsyncMock(return_value={"code": "73190", "pass": None})):
                await social.tl_get_code(self.ev, "+18452000111")
                await social.tl_get_code(self.ev, "+18452000111")
        self.assertEqual(len(self.logs), 1)
        self.assertIn("<tg-spoiler>73190</tg-spoiler>", self.logs[0][1])
        self.assertIn("United States", self.logs[0][1])
        self.assertEqual(self.logs[0][2], self.uid)

    async def test_post_sale_forwards_to_buyer_or_falls_back_to_copy(self):
        fb = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=555)), forward_messages=AsyncMock(),
                             get_me=AsyncMock(return_value=SimpleNamespace(username="Betaotp_robot")))
        with patch.object(salelog, "bot", fb):
            salelog._ME["u"] = None
            await salelog.post_sale("t", "b", buyer_id=self.uid)
            self.assertEqual(fb.forward_messages.await_args.args[:2], (self.uid, 555))
            fb.forward_messages.side_effect = Exception("protected chat")
            fb.send_message.reset_mock()
            await salelog.post_sale("t", "b", buyer_id=self.uid)
            self.assertTrue(any(c.args[0] == self.uid for c in fb.send_message.await_args_list))


if __name__ == "__main__":
    unittest.main(verbosity=2)
