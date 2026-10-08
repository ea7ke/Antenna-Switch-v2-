from __future__ import annotations

import asyncio
import random
import unittest

from server.audit import MemoryAuditSink
from server.hardware import HardwareError, SimulatedHardware
from server.statemachine import AntennaSwitch, Radio, SwitchError
from server.tests.helpers import antennas, assert_physical_invariants, make_switch

R1, R2 = "R1", "R2"


def holder(state: dict, radio: str):
    return state["radios"][radio]["antenna_id"]


class StartupTest(unittest.IsolatedAsyncioTestCase):
    async def test_boot_leaves_safe_state_and_logs_what_it_found(self):
        hw = SimulatedHardware(initial_closed=[2, 8])  # estado peligroso heredado
        sw, hw, audit = await make_switch(hw=hw)
        self.assertEqual(hw.closed_pins(), set())
        self.assertEqual(audit.of("BOOT")[0]["found_closed"], [2, 8])
        state = sw.snapshot()
        self.assertIsNone(sw.fault)
        self.assertIsNone(holder(state, R1))
        self.assertIsNone(holder(state, R2))
        self.assertTrue(state["relays_ok"])

    async def test_boot_never_restores_a_previous_assignment(self):
        hw = SimulatedHardware(initial_closed=[2])  # R1 estaba en la antena 1
        sw, hw, _ = await make_switch(hw=hw)
        self.assertEqual(hw.closed_pins(), set())
        self.assertIsNone(holder(sw.snapshot(), R1))

    async def test_stuck_relay_at_boot_starts_in_fault(self):
        hw = SimulatedHardware()
        hw.stick_closed(2)
        sw, _, audit = await make_switch(hw=hw)
        self.assertEqual(sw.fault, "RELAY_MISMATCH")
        self.assertEqual(audit.of("RELAY_MISMATCH")[0]["phase"], "boot")
        with self.assertRaises(SwitchError) as cm:
            await sw.switch(R2, 5)
        self.assertEqual(cm.exception.code, "FAULT")

    async def test_hardware_unavailable_aborts_startup(self):
        class BadHw(SimulatedHardware):
            def setup(self, pins):
                raise HardwareError("sin pigpiod")

        audit = MemoryAuditSink()
        sw = AntennaSwitch(BadHw(), antennas(), audit=audit)
        with self.assertRaises(HardwareError):
            await sw.startup()
        self.assertEqual(audit.of("FAULT")[0]["code"], "HARDWARE_ERROR")

    async def test_switch_before_startup_is_refused(self):
        sw = AntennaSwitch(SimulatedHardware(), antennas(), audit=MemoryAuditSink())
        with self.assertRaises(RuntimeError):
            await sw.switch(R1, 1)

    def test_constructor_validation(self):
        hw = SimulatedHardware()
        with self.assertRaises(ValueError):
            AntennaSwitch(hw, [])
        a = antennas()
        dup = [a[0], type(a[1])(a[1].id, "x", a[0].pin_r1, 99)]  # pin repetido
        with self.assertRaises(ValueError):
            AntennaSwitch(hw, dup)
        with self.assertRaises(ValueError):
            AntennaSwitch(hw, a, dead_time_s=0.5, maneuver_timeout_s=0.5)


class SwitchingTest(unittest.IsolatedAsyncioTestCase):
    async def test_basic_switch(self):
        sw, hw, audit = await make_switch()
        state = await sw.switch(R1, 1)
        self.assertEqual(hw.closed_pins(), {2})
        self.assertEqual(holder(state, R1), 1)
        state = await sw.switch(R2, 3)
        self.assertEqual(hw.closed_pins(), {2, 10})
        self.assertEqual(state["radios"][R2]["antenna"], "20m Yagi")
        self.assertEqual(len(audit.of("SWITCH_OK")), 2)
        self.assertTrue(state["relays_ok"])
        assert_physical_invariants(self, hw)

    async def test_active_low_modules_end_to_end(self):
        sw, hw, _ = await make_switch(active_high=False)
        self.assertTrue(all(level == 1 for level in hw._levels.values()))  # reposo
        await sw.switch(R1, 1)
        self.assertEqual(hw._levels[2], 0)  # cerrado = nivel bajo
        self.assertEqual(hw.closed_pins(), {2})
        self.assertTrue(sw.snapshot()["relays_ok"])

    async def test_disconnect_with_none(self):
        sw, hw, _ = await make_switch()
        await sw.switch(R1, 1)
        state = await sw.switch(R1, None)
        self.assertEqual(hw.closed_pins(), set())
        self.assertIsNone(holder(state, R1))

    async def test_change_antenna_is_break_before_make_with_dead_time(self):
        sw, hw, _ = await make_switch(dead_time=0.05, timeout=1.0)
        await sw.switch(R1, 1)
        start = len(hw.events)
        await sw.switch(R1, 2)
        moves = hw.events[start:]
        self.assertEqual([(e.pin, e.closed) for e in moves], [(2, False), (3, True)])
        self.assertGreaterEqual(moves[1].t - moves[0].t, 0.045)
        assert_physical_invariants(self, hw)

    async def test_same_antenna_is_a_noop(self):
        sw, hw, audit = await make_switch()
        await sw.switch(R1, 1)
        events = len(hw.events)
        await sw.switch(R1, 1)
        self.assertEqual(len(hw.events), events)  # ningún relé se mueve
        self.assertEqual(len(audit.of("SWITCH_OK")), 1)

    async def test_unknown_antenna(self):
        sw, hw, audit = await make_switch()
        with self.assertRaises(SwitchError) as cm:
            await sw.switch(R1, 99)
        self.assertEqual((cm.exception.code, cm.exception.status), ("UNKNOWN_ANTENNA", 404))
        self.assertEqual(hw.closed_pins(), set())
        self.assertEqual(audit.of("SWITCH_DENIED")[0]["code"], "UNKNOWN_ANTENNA")

    async def test_maintenance(self):
        sw, hw, _ = await make_switch(maintenance={4: "rotor averiado"})
        with self.assertRaises(SwitchError) as cm:
            await sw.switch(R1, 4)
        self.assertEqual(cm.exception.code, "MAINTENANCE")
        self.assertIn("rotor averiado", cm.exception.message)
        entry = next(a for a in sw.snapshot()["antennas"] if a["id"] == 4)
        self.assertEqual(entry["maintenance"], "rotor averiado")
        await sw.set_maintenance(4, None)
        await sw.switch(R1, 4)
        self.assertEqual(hw.closed_pins(), {5})

    async def test_maintenance_does_not_disconnect_an_antenna_in_use(self):
        sw, hw, _ = await make_switch()
        await sw.switch(R1, 1)
        await sw.set_maintenance(1, "revisión")
        self.assertEqual(hw.closed_pins(), {2})
        with self.assertRaises(SwitchError):
            await sw.switch(R2, 1)

    async def test_set_maintenance_unknown_antenna(self):
        sw, _, _ = await make_switch()
        with self.assertRaises(SwitchError) as cm:
            await sw.set_maintenance(77, "x")
        self.assertEqual(cm.exception.status, 404)


class ExclusivityTest(unittest.IsolatedAsyncioTestCase):
    async def test_collision_is_blocked_and_audited(self):
        sw, hw, audit = await make_switch()
        await sw.switch(R1, 1)
        events = len(hw.events)
        with self.assertRaises(SwitchError) as cm:
            await sw.switch(R2, 1)
        self.assertEqual((cm.exception.code, cm.exception.status), ("ANTENNA_IN_USE", 409))
        self.assertEqual(len(hw.events), events)  # no se tocó ningún relé
        self.assertEqual(hw.closed_pins(), {2})
        blocked = audit.of("COLLISION_BLOCKED")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["radio"], R2)
        self.assertEqual(blocked[0]["ant_to"], 1)

    async def test_antenna_becomes_available_after_release(self):
        sw, hw, _ = await make_switch()
        await sw.switch(R1, 1)
        await sw.switch(R1, None)
        await sw.switch(R2, 1)
        self.assertEqual(hw.closed_pins(), {8})

    async def test_cannot_swap_antennas_directly(self):
        sw, hw, _ = await make_switch()
        await sw.switch(R1, 1)
        await sw.switch(R2, 2)
        with self.assertRaises(SwitchError):
            await sw.switch(R1, 2)
        with self.assertRaises(SwitchError):
            await sw.switch(R2, 1)
        self.assertEqual(hw.closed_pins(), {2, 9})

    async def test_simultaneous_requests_for_the_same_antenna(self):
        sw, hw, _ = await make_switch(dead_time=0.001)
        for antenna in range(1, 7):
            for _ in range(20):
                results = await asyncio.gather(
                    sw.switch(R1, antenna), sw.switch(R2, antenna), return_exceptions=True
                )
                ok = [r for r in results if isinstance(r, dict)]
                errors = [r for r in results if isinstance(r, SwitchError)]
                self.assertEqual(len(ok), 1, results)
                self.assertEqual(errors[0].code, "ANTENNA_IN_USE")
                await asyncio.gather(sw.switch(R1, None), sw.switch(R2, None))
        assert_physical_invariants(self, hw)

    async def test_randomised_concurrent_stress(self):
        sw, hw, _ = await make_switch(dead_time=0.001)
        rng = random.Random(1234)
        for _ in range(60):
            ops = [
                (rng.choice([R1, R2]), rng.choice([None, 1, 2, 3, 4, 5, 6]))
                for _ in range(8)
            ]
            results = await asyncio.gather(
                *(sw.switch(r, a) for r, a in ops), return_exceptions=True
            )
            for res in results:
                if isinstance(res, Exception):
                    self.assertIsInstance(res, SwitchError)
                    self.assertEqual(res.code, "ANTENNA_IN_USE")
            state = sw.snapshot()
            a1, a2 = holder(state, R1), holder(state, R2)
            self.assertTrue(a1 is None or a1 != a2)
            self.assertTrue(state["relays_ok"])
            self.assertIsNone(state["fault"])
        assert_physical_invariants(self, hw)

    async def test_physical_guard_blocks_even_if_validation_is_bypassed(self):
        sw, hw, audit = await make_switch()
        await sw.switch(R1, 1)
        sw._validate = lambda *args, **kwargs: None  # simula un bug futuro
        with self.assertRaises(SwitchError) as cm:
            await sw.switch(R2, 1)
        self.assertEqual((cm.exception.code, cm.exception.status), ("RELAY_MISMATCH", 503))
        self.assertEqual([e for e in hw.events if e.pin == 8 and e.closed], [])
        self.assertEqual(hw.closed_pins(), set())  # y todo queda abierto
        self.assertEqual(sw.fault, "RELAY_MISMATCH")


class FaultTest(unittest.IsolatedAsyncioTestCase):
    async def test_stuck_relay_after_make_opens_everything_and_faults(self):
        sw, hw, audit = await make_switch()
        await sw.switch(R1, 1)
        hw.stick_closed(2)  # el relé de la antena 1 se queda pegado
        with self.assertRaises(SwitchError) as cm:
            await sw.switch(R1, 2)
        self.assertEqual((cm.exception.code, cm.exception.status), ("RELAY_MISMATCH", 503))
        self.assertEqual(hw.closed_pins(), {2})  # solo el pegado; el nuevo se abrió
        self.assertFalse(hw.read_relay(3))
        self.assertEqual(sw.fault, "RELAY_MISMATCH")
        self.assertTrue(audit.of("RELAY_MISMATCH"))
        self.assertTrue(audit.of("FAULT"))

        with self.assertRaises(SwitchError) as cm:
            await sw.switch(R2, 5)
        self.assertEqual((cm.exception.code, cm.exception.status), ("FAULT", 503))

        with self.assertRaises(SwitchError) as cm:  # no se puede rearmar con un relé pegado
            await sw.release_all()
        self.assertEqual(cm.exception.code, "RELEASE_FAILED")
        self.assertEqual(sw.fault, "RELAY_MISMATCH")

        hw.unstick(2)  # reparado
        state = await sw.release_all()
        self.assertIsNone(state["fault"])
        self.assertTrue(audit.of("FAULT_CLEARED"))
        await sw.switch(R2, 5)
        self.assertEqual(hw.closed_pins(), {12})

    async def test_unexpected_external_change_is_caught_before_moving_anything(self):
        sw, hw, _ = await make_switch()
        hw.force(8, True)  # glitch: el relé de R2 en la antena 1 se cierra solo
        with self.assertRaises(SwitchError) as cm:
            await sw.switch(R1, 1)
        self.assertEqual(cm.exception.code, "RELAY_MISMATCH")
        self.assertEqual([e for e in hw.events if e.pin == 2 and e.closed], [])
        self.assertEqual(hw.closed_pins(), set())  # open_all también cerró el glitch

    async def test_gpio_write_failure(self):
        sw, hw, _ = await make_switch()
        hw.fail_writes(2)
        with self.assertLogs("antenna.switch", level="ERROR"):
            with self.assertRaises(SwitchError) as cm:
                await sw.switch(R1, 1)
        self.assertEqual((cm.exception.code, cm.exception.status), ("HARDWARE_ERROR", 503))
        self.assertEqual(sw.fault, "HARDWARE_ERROR")
        self.assertEqual(hw.closed_pins(), set())

    async def test_maneuver_timeout_goes_to_safe_state(self):
        sw, hw, _ = await make_switch(dead_time=0.01, timeout=0.3)
        await sw.switch(R1, 1)
        await sw.switch(R2, 3)
        sw._dead_time = 5  # simula un bucle de eventos atascado en mitad del cambio
        with self.assertRaises(SwitchError) as cm:
            await sw.switch(R1, 2)
        self.assertEqual((cm.exception.code, cm.exception.status), ("MANEUVER_TIMEOUT", 504))
        self.assertEqual(sw.fault, "MANEUVER_TIMEOUT")
        self.assertEqual(hw.closed_pins(), set())  # R1 y R2 sin antena: estado seguro
        state = sw.snapshot()
        self.assertIsNone(holder(state, R1))
        self.assertIsNone(holder(state, R2))
        assert_physical_invariants(self, hw)

    async def test_cancelled_caller_does_not_leave_a_half_done_maneuver(self):
        sw, hw, _ = await make_switch(dead_time=0.05, timeout=1.0)
        await sw.switch(R1, 1)
        task = asyncio.ensure_future(sw.switch(R1, 2))
        await asyncio.sleep(0.01)  # en pleno tiempo muerto
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.15)  # la maniobra protegida termina sola
        state = sw.snapshot()
        self.assertEqual(holder(state, R1), 2)
        self.assertTrue(state["relays_ok"])
        self.assertIsNone(state["fault"])
        self.assertEqual(hw.closed_pins(), {3})
        assert_physical_invariants(self, hw)


class EmergencyAndLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_release_all(self):
        sw, hw, audit = await make_switch()
        await sw.switch(R1, 1)
        await sw.switch(R2, 2)
        state = await sw.release_all(actor="admin", reason="prueba")
        self.assertEqual(hw.closed_pins(), set())
        self.assertIsNone(holder(state, R1))
        self.assertIsNone(holder(state, R2))
        ev = audit.of("EMERGENCY_RELEASE")[0]
        self.assertEqual((ev["actor"], ev["reason"], ev["result"]), ("admin", "prueba", "OK"))
        assert_physical_invariants(self, hw)

    async def test_shutdown_leaves_safe_state(self):
        sw, hw, _ = await make_switch()
        await sw.switch(R1, 1)
        await sw.shutdown()
        self.assertEqual(hw.closed_pins(), set())

    async def test_boot_id_changes_between_runs(self):
        first, _, _ = await make_switch()
        second, _, _ = await make_switch()
        self.assertTrue(first.snapshot()["boot"])
        self.assertNotEqual(first.snapshot()["boot"], second.snapshot()["boot"])

    async def test_subscribers_receive_state_changes(self):
        sw, _, _ = await make_switch()
        queue = sw.subscribe()
        await sw.switch(R1, 1)
        first = queue.get_nowait()
        self.assertEqual(holder(first, R1), 1)
        await sw.switch(R1, 2)
        second = queue.get_nowait()
        self.assertGreater(second["seq"], first["seq"])
        sw.unsubscribe(queue)
        await sw.switch(R1, 3)
        self.assertTrue(queue.empty())

    async def test_slow_subscriber_does_not_block_and_gets_latest(self):
        sw, _, _ = await make_switch(dead_time=0.001)
        queue = sw.subscribe()
        for i in range(20):
            await sw.switch(R1, 1 if i % 2 == 0 else None)
        self.assertLessEqual(queue.qsize(), 16)
        last = None
        while not queue.empty():
            last = queue.get_nowait()
        self.assertEqual(last["seq"], sw.snapshot()["seq"])


class FakeGuard:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.error: Exception | None = None
        self.state = "RX"
        self.delay = 0.0
        self.end_error: Exception | None = None

    async def begin_maneuver(self, radio):
        self.calls.append(("begin", radio.value))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.state

    async def end_maneuver(self, radio):
        self.calls.append(("end", radio.value))
        if self.end_error:
            raise self.end_error


class PttGuardTest(unittest.IsolatedAsyncioTestCase):
    async def test_guard_brackets_a_successful_maneuver(self):
        guard = FakeGuard()
        sw, hw, audit = await make_switch(ptt_guard=guard)
        await sw.switch(R1, 1)
        self.assertEqual(guard.calls, [("begin", R1), ("end", R1)])
        self.assertEqual(audit.of("SWITCH_OK")[0]["ptt_state"], "RX")

    async def test_tx_active_blocks_before_touching_relays(self):
        guard = FakeGuard()
        guard.error = SwitchError("TX_ACTIVE", "R1 está transmitiendo", 409)
        sw, hw, audit = await make_switch(ptt_guard=guard)
        events = len(hw.events)
        with self.assertRaises(SwitchError) as cm:
            await sw.switch(R1, 1)
        self.assertEqual(cm.exception.code, "TX_ACTIVE")
        self.assertEqual(len(hw.events), events)
        self.assertEqual(guard.calls, [("begin", R1)])  # no hay end: begin falló
        self.assertEqual(len(audit.of("TX_ACTIVE_BLOCKED")), 1)
        self.assertIsNone(sw.fault)

    async def test_guard_failure_means_ptt_unknown(self):
        guard = FakeGuard()
        guard.error = ConnectionError("agente caído")
        sw, hw, _ = await make_switch(ptt_guard=guard)
        events = len(hw.events)
        with self.assertRaises(SwitchError) as cm:
            await sw.switch(R1, 1)
        self.assertEqual((cm.exception.code, cm.exception.status), ("PTT_UNKNOWN", 428))
        self.assertEqual(len(hw.events), events)

    async def test_guard_timeout_means_ptt_unknown(self):
        guard = FakeGuard()
        guard.delay = 5
        sw, hw, _ = await make_switch(ptt_guard=guard, timeout=0.1)
        with self.assertRaises(SwitchError) as cm:
            await sw.switch(R1, 1)
        self.assertEqual(cm.exception.code, "PTT_UNKNOWN")
        self.assertEqual(hw.closed_pins(), set())

    async def test_tx_is_released_even_if_the_maneuver_faults(self):
        guard = FakeGuard()
        sw, hw, _ = await make_switch(ptt_guard=guard)
        hw.force(8, True)
        with self.assertRaises(SwitchError):
            await sw.switch(R1, 1)
        self.assertEqual(guard.calls, [("begin", R1), ("end", R1)])

    async def test_failure_releasing_the_guard_does_not_mask_success(self):
        guard = FakeGuard()
        guard.end_error = ConnectionError("agente caído")
        sw, hw, _ = await make_switch(ptt_guard=guard)
        with self.assertLogs("antenna.switch", level="ERROR"):
            await sw.switch(R1, 1)
        self.assertEqual(hw.closed_pins(), {2})


if __name__ == "__main__":
    unittest.main()
