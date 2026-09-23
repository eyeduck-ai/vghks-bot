import unittest
from datetime import timedelta

from opd_monitor.settings import Account, Settings, load_defaults, parse_range, today


class SettingsTests(unittest.TestCase):
    def test_blank_defaults_no_secret_in_public_or_repr(self):
        self.assertEqual(load_defaults().username, "")
        self.assertEqual(load_defaults().password, "")
        account = Account("a"*32,username="DOC1",password="synthetic-secret")
        self.assertNotIn("synthetic-secret", str(account))
        self.assertNotIn("password", account.persisted())
        self.assertTrue(account.public()["password_set"])
        self.assertEqual([c.name for c in load_defaults().categories],["手術","審查"])
        self.assertEqual([c.scope for c in load_defaults().categories], ["ap", "ap"])
        self.assertEqual(load_defaults().categories, Settings().categories)

    def test_password_retained_only_for_same_account(self):
        account = Account("a"*32,username="DOC1",password="synthetic")
        self.assertEqual(account.update({"password":""}).password,"synthetic")
        self.assertEqual(account.update({"username":"DOC2"}).password,"")
        self.assertEqual(account.update({"clear_password":True}).password,"")

    def test_invalid_numeric_category_and_config_values(self):
        for value in ({"read_timeout_seconds":float("nan")},{"min_delay_seconds":-1},{"max_attempts":1.5},{"max_attempts":True},{"allow_unverified_tls":"false"},{"categories":[]},{"marker":"#"},{"portal_base_url":"http://example.com"},{"categories":[{"name":"A","keywords":[""]}]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings().update(value)

    def test_date_default_and_single_day_overrides_stale_end(self):
        self.assertEqual(parse_range({}), (today(),today()))
        account = Account("a"*32).update({"start":"2025-01-01","end":"2025-01-05","mode":"single"})
        self.assertEqual(account.start,account.end)
        self.assertEqual(account.update({"mode":"range","end":"2025-02-01"}).end,"2025-02-01")
        for values in ({"start":"invalid"},{"start":"2026-02-30"},{"start":"2025-02-01","end":"2025-01-01"},{"end":(today()+timedelta(days=1)).isoformat()}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                parse_range(values)

    def test_future_date_is_allowed_only_for_registration_paths(self):
        future = today() + timedelta(days=30)
        values = {"start": future.isoformat(), "end": future.isoformat()}
        self.assertEqual(parse_range(values, allow_future=True), (future, future))
        self.assertEqual(Account("a" * 32).update(values).start, future.isoformat())
        with self.assertRaises(ValueError):
            parse_range(values)
        from opd_monitor.analysis import Analysis
        with self.assertRaisesRegex(ValueError, "今天"):
            Analysis.options({"modules": ["retina"], **values})
