import base64
import json
import unittest

from panel.formats import parse_node, render_subscription
from panel.qr import make_qr, qr_svg


CLASH_TEMPLATE = "proxies:\n{{PROXIES}}\nproxy-groups:\n  - name: test\n    type: select\n    proxies:\n{{PROXY_NAMES}}\n"
SINGBOX_TEMPLATE = '{"outbounds":[\n{{OUTBOUNDS}}\n],"route":{"final":{{FIRST_TAG}}}}'


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {
                "name": "东京 VLESS",
                "uri": "vless://11111111-1111-1111-1111-111111111111@example.com:443?security=tls&type=ws&host=edge.example.com&path=%2Fws#Tokyo",
            },
            {
                "name": "SS",
                "uri": "ss://YWVzLTI1Ni1nY206cGFzc3dvcmQ@example.net:8388#SS-Node",
            },
        ]

    def test_parse_vless(self):
        node = parse_node(self.rows[0]["uri"], self.rows[0]["name"])
        self.assertEqual(node["type"], "vless")
        self.assertEqual(node["server"], "example.com")
        self.assertEqual(node["query"]["path"], "/ws")

    def test_base64_output(self):
        body, content_type = render_subscription("base64", "", self.rows)
        decoded = base64.b64decode(body).decode()
        self.assertIn("vless://", decoded)
        self.assertIn("ss://", decoded)
        self.assertIn("text/plain", content_type)

    def test_clash_output(self):
        body, _ = render_subscription("clash", CLASH_TEMPLATE, self.rows)
        text = body.decode()
        self.assertIn('type: "vless"', text)
        self.assertIn('type: "ss"', text)
        self.assertIn('name: "东京 VLESS"', text)

    def test_singbox_output_is_json(self):
        body, _ = render_subscription("singbox", SINGBOX_TEMPLATE, self.rows)
        data = json.loads(body)
        self.assertEqual(data["route"]["final"], "东京 VLESS")
        self.assertEqual(len(data["outbounds"]), 3)

    def test_qr_svg_is_square_and_self_contained(self):
        matrix = make_qr("https://sub.example.com/s/token/auto")
        self.assertEqual(len(matrix), len(matrix[0]))
        self.assertGreaterEqual(len(matrix), 21)
        svg = qr_svg("https://sub.example.com/s/token/auto").decode()
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("<path", svg)
        self.assertNotIn("http://www.google", svg)


if __name__ == "__main__":
    unittest.main()
