import unittest

from pocket import bonjour


class BonjourTest(unittest.TestCase):
    def test_macos_uses_dns_sd_with_public_txt_only(self):
        argv = bonjour.command(8430, "0.1.4", 2, which=lambda t: t == "dns-sd", host="mnemosyne")
        self.assertEqual(argv, ["dns-sd", "-R", "AINode Pocket on mnemosyne", "_ainode-pocket._tcp",
                                "local", "8430", "version=0.1.4", "devices=2"])

    def test_linux_uses_avahi(self):
        argv = bonjour.command(9000, "0.1.4", 0, which=lambda t: t == "avahi-publish-service", host="spark")
        self.assertEqual(argv[:4], ["avahi-publish-service", "AINode Pocket on spark", "_ainode-pocket._tcp", "9000"])

    def test_no_responder_means_no_advertisement(self):
        self.assertIsNone(bonjour.command(8430, "0.1.4", 1, which=lambda t: None))


if __name__ == "__main__":
    unittest.main()
