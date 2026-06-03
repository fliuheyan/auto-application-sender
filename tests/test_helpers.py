import unittest

from app import detect_captcha, extract_emails, pick_best_email


class HelperTests(unittest.TestCase):
    def test_extract_emails(self):
        content = "Contact us at careers@example.com and hr@example.com"
        emails = extract_emails(content)
        self.assertIn("careers@example.com", emails)
        self.assertIn("hr@example.com", emails)

    def test_best_email_priority(self):
        selected = pick_best_email(["info@example.com", "careers@example.com"])
        self.assertEqual(selected, "careers@example.com")

    def test_detect_captcha(self):
        self.assertTrue(detect_captcha("Please verify you are human with captcha"))
        self.assertFalse(detect_captcha("Normal careers page"))


if __name__ == "__main__":
    unittest.main()
