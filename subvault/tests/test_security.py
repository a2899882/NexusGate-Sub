import unittest

from panel.security import hash_password, random_token, verify_password


class SecurityTests(unittest.TestCase):
    def test_password_roundtrip(self):
        encoded = hash_password("a-long-test-password")
        self.assertTrue(verify_password("a-long-test-password", encoded))
        self.assertFalse(verify_password("wrong-password", encoded))

    def test_short_password_rejected(self):
        with self.assertRaises(ValueError):
            hash_password("short")

    def test_tokens_are_unique(self):
        self.assertNotEqual(random_token(), random_token())


if __name__ == "__main__":
    unittest.main()
