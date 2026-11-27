import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("API_ID", "1"); os.environ.setdefault("API_HASH", "x")
from plugins import social


class PricingRules(unittest.TestCase):
    def test_telegram_server3_is_lzt_not_hero(self):
        self.assertEqual(social.HERO_SERVERS["tg"], ())
        self.assertNotIn(("tg", 3), social.RISKY)

    def test_telegram_server3_has_profit(self):
        sell, cost, _ = social.gz_offer("tg", 3, 1.0)
        self.assertGreaterEqual(sell - cost, 10)

    def test_provider_routing(self):
        self.assertEqual(social.provider_for("wa", 1), "hero")          # Hero SMS: physical + cheap, sorted by price
        for code, srv in (("wa", 2), ("tc", 3), ("ig", 2), ("fb", 2), ("go", 2), ("ds", 2)):
            self.assertEqual(social.provider_for(code, srv), "tempora")  # TemporaSMS everywhere else

    def test_every_new_panel_route_keeps_a_profit(self):
        for code, srv in (("wa", 2), ("tc", 3), ("ig", 2), ("zz", 2)):
            sell, cost, _ = social.gz_offer(code, srv, 0.05)
            self.assertGreaterEqual(sell - cost, 4, (code, srv))

    def test_whatsapp_server1_virtual_rows_keep_old_cheap_margin(self):
        phys = social.gz_offer("wa", 1, 0.5, phys=10)[0]
        virt = social.gz_offer("wa", 1, 0.5, phys=0)[0]
        self.assertLess(virt, phys)

    def test_server_titles(self):
        self.assertIn("Fast & Smart", social.server_title("wa", 2))
        self.assertNotIn("Cheap", social.server_title("wa", 2))
        self.assertEqual(social.server_short(social.server_title("wa", 2)), "2 · Fast & Smart Service")

    def test_all_codes_accept_any_panel_app(self):
        self.assertIn("ds", social.ALL_CODES)
        self.assertEqual(social.ALL_CODES["ig"][1], "Instagram")
        self.assertTrue(social.ALL_CODES["zzz"][1])      # unknown app never raises


if __name__ == "__main__":
    unittest.main(verbosity=2)
