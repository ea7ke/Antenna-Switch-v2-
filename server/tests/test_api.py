"""Tests de la API HTTP/WebSocket.

Necesitan fastapi y httpx (``pip install -r requirements-dev.txt``); si no están
instalados se omiten, porque el resto de tests solo usa la biblioteca estándar.
"""

from __future__ import annotations

import dataclasses
import unittest

try:
    from fastapi.testclient import TestClient

    HAVE_FASTAPI = True
except Exception:  # ImportError, o httpx ausente
    HAVE_FASTAPI = False

from server.audit import MemoryAuditSink
from server.config import load_config
from server.hardware import SimulatedHardware
from server.tests.helpers import EXAMPLE_CONFIG

HEADERS = {"X-Antenna-Client": "web"}


@unittest.skipUnless(HAVE_FASTAPI, "requiere fastapi y httpx (requirements-dev.txt)")
class ApiTest(unittest.TestCase):
    def setUp(self):
        from server.main import create_app

        cfg = dataclasses.replace(
            load_config(EXAMPLE_CONFIG),
            backend="simulated",
            dead_time_ms=20,
            maneuver_timeout_ms=1000,
        )
        self.hw = SimulatedHardware(active_high=cfg.active_high)
        self.audit = MemoryAuditSink()
        self.client = TestClient(create_app(cfg, hardware=self.hw, audit=self.audit))
        self.client.__enter__()  # arranca el lifespan (startup del conmutador)
        self.addCleanup(self.client.__exit__, None, None, None)

    def switch(self, radio, antenna_id, headers=HEADERS):
        return self.client.post(
            "/api/switch", json={"radio": radio, "antenna_id": antenna_id}, headers=headers
        )

    def test_initial_state_is_safe(self):
        body = self.client.get("/api/state").json()
        self.assertIsNone(body["fault"])
        self.assertIsNone(body["radios"]["R1"]["antenna_id"])
        self.assertIsNone(body["radios"]["R2"]["antenna_id"])
        self.assertEqual(self.hw.closed_pins(), set())
        self.assertEqual(len(body["antennas"]), 6)

    def test_switch_and_collision(self):
        res = self.switch("R1", 1)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["radios"]["R1"]["antenna_id"], 1)
        self.assertEqual(self.hw.closed_pins(), {2})

        res = self.switch("R2", 1)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["detail"]["code"], "ANTENNA_IN_USE")
        self.assertEqual(self.hw.closed_pins(), {2})

    def test_unknown_antenna_is_404(self):
        res = self.switch("R1", 99)
        self.assertEqual(res.status_code, 404)

    def test_invalid_radio_is_rejected(self):
        res = self.switch("R3", 1)
        self.assertEqual(res.status_code, 422)
        self.assertEqual(self.hw.closed_pins(), set())

    def test_mutations_require_the_client_header(self):
        res = self.switch("R1", 1, headers={})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.hw.closed_pins(), set())
        res = self.client.post("/api/emergency/release-all")
        self.assertEqual(res.status_code, 403)

    def test_release_all(self):
        self.switch("R1", 1)
        self.switch("R2", 2)
        res = self.client.post("/api/emergency/release-all", headers=HEADERS)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.hw.closed_pins(), set())

    def test_fault_blocks_switching_with_503(self):
        self.hw.force(8, True)  # glitch externo
        res = self.switch("R1", 1)
        self.assertEqual(res.status_code, 503)
        res = self.switch("R2", 3)
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["detail"]["code"], "FAULT")
        self.assertFalse(self.client.get("/api/health").json()["ok"])
        res = self.client.post("/api/emergency/release-all", headers=HEADERS)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(self.client.get("/api/health").json()["ok"])

    def test_health(self):
        body = self.client.get("/api/health").json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["backend"], "simulated")

    def test_audit_records_the_requester(self):
        self.switch("R1", 1)
        self.assertTrue(self.audit.of("SWITCH_OK")[0]["actor"].startswith("ip:"))

    def test_websocket_pushes_state(self):
        with self.client.websocket_connect("/ws") as ws:
            first = ws.receive_json()
            self.assertEqual(first["type"], "state")
            self.assertIsNone(first["state"]["radios"]["R1"]["antenna_id"])
            self.switch("R1", 2)
            update = ws.receive_json()
            self.assertEqual(update["state"]["radios"]["R1"]["antenna_id"], 2)

    def test_web_ui_is_served(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Conmutador de antenas", res.text)


if __name__ == "__main__":
    unittest.main()
