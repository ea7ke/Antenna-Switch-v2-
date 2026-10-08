from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from server.config import ConfigError, load_config, parse_config
from server.tests.helpers import EXAMPLE_CONFIG, V1_MAP


def base() -> dict:
    return {
        "gpio": {"backend": "simulated"},
        "antenna": [
            {"id": 1, "name": "A", "gpio_r1": 2, "gpio_r2": 8},
            {"id": 2, "name": "B", "gpio_r1": 3, "gpio_r2": 9},
        ],
    }


class ExampleConfigTest(unittest.TestCase):
    def test_example_matches_v1_pin_map(self):
        cfg = load_config(EXAMPLE_CONFIG)
        got = [(a.id, a.name, a.gpio_r1, a.gpio_r2) for a in cfg.antennas]
        self.assertEqual(got, V1_MAP)

    def test_example_defaults(self):
        cfg = load_config(EXAMPLE_CONFIG)
        self.assertTrue(cfg.active_high)  # polaridad efectiva de v1
        self.assertEqual(cfg.dead_time_ms, 150)
        self.assertEqual(cfg.host, "127.0.0.1")  # sin auth => solo local
        self.assertEqual(len(set(cfg.pins)), 12)


class ValidationTest(unittest.TestCase):
    def assertRejected(self, data: dict, fragment: str = ""):
        with self.assertRaises(ConfigError) as ctx:
            parse_config(data)
        self.assertIn(fragment, str(ctx.exception))

    def test_minimal_ok(self):
        cfg = parse_config(base())
        self.assertEqual(len(cfg.antennas), 2)

    def test_duplicate_pin_across_radios(self):
        d = base()
        d["antenna"][1]["gpio_r2"] = 2  # comparte pin con gpio_r1 de la antena 1
        self.assertRejected(d, "repetidos")

    def test_duplicate_pin_same_radio(self):
        d = base()
        d["antenna"][1]["gpio_r1"] = 2
        self.assertRejected(d, "repetidos")

    def test_pin_out_of_range(self):
        for bad in (0, 1, 28, -3):
            d = copy.deepcopy(base())
            d["antenna"][0]["gpio_r1"] = bad
            self.assertRejected(d, "gpio_r1")

    def test_active_high_must_be_bool(self):
        d = base()
        d["gpio"]["active_high"] = "true"
        self.assertRejected(d, "active_high")
        d["gpio"]["active_high"] = 1
        self.assertRejected(d, "active_high")

    def test_typo_in_key_is_rejected(self):
        d = base()
        d["gpio"]["active_hig"] = False  # errata: no debe ignorarse en silencio
        self.assertRejected(d, "desconocidas")

    def test_unknown_section(self):
        d = base()
        d["gpoi"] = {}
        self.assertRejected(d, "desconocidas")

    def test_unknown_antenna_key(self):
        d = base()
        d["antenna"][0]["gpio_r3"] = 20
        self.assertRejected(d, "desconocidas")

    def test_missing_required_antenna_key(self):
        d = base()
        del d["antenna"][0]["gpio_r2"]
        self.assertRejected(d, "gpio_r2")

    def test_no_antennas(self):
        self.assertRejected({"gpio": {}}, "antenna")

    def test_duplicate_ids(self):
        d = base()
        d["antenna"][1]["id"] = 1
        self.assertRejected(d, "ids")

    def test_bad_backend(self):
        d = base()
        d["gpio"]["backend"] = "rpi"
        self.assertRejected(d, "backend")

    def test_timeout_must_exceed_dead_time(self):
        d = base()
        d["gpio"].update(dead_time_ms=500, maneuver_timeout_ms=600)
        self.assertRejected(d, "maneuver_timeout_ms")

    def test_port_range(self):
        d = base()
        d["server"] = {"port": 70000}
        self.assertRejected(d, "port")

    def test_radio_labels(self):
        d = base()
        d["radios"] = {"R1": "IC-7610"}
        cfg = parse_config(d)
        self.assertEqual(cfg.radio_labels["R1"], "IC-7610")
        self.assertEqual(cfg.radio_labels["R2"], "Radio 2")


class LoadTest(unittest.TestCase):
    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load_config("/no/existe/antenna.toml")

    def test_invalid_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.toml"
            p.write_text("esto = = no es toml", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(p)


if __name__ == "__main__":
    unittest.main()
