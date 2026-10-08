from __future__ import annotations

import importlib.util
import unittest

from server.hardware import HardwareError, PigpioHardware, SimulatedHardware

PINS = [2, 3, 8, 9]


class FakePi:
    """Imita lo mínimo del cliente pigpio."""

    def __init__(self, connected: bool = True, initial: dict[int, int] | None = None):
        self.connected = connected
        self.levels: dict[int, int] = dict(initial or {})
        self.writes: list[tuple[int, int]] = []
        self.stopped = False
        self.fail = False

    def write(self, pin: int, level: int) -> None:
        if self.fail:
            raise OSError("socket roto")
        self.levels[pin] = level
        self.writes.append((pin, level))

    def read(self, pin: int) -> int:
        if self.fail:
            raise OSError("socket roto")
        return self.levels.get(pin, 0)

    def stop(self) -> None:
        self.stopped = True


class PolarityTest(unittest.TestCase):
    def test_active_high(self):
        hw = SimulatedHardware(active_high=True)
        hw.setup(PINS)
        self.assertTrue(all(hw._levels[p] == 0 for p in PINS))  # abierto = nivel bajo
        hw.set_relay(2, True)
        self.assertEqual(hw._levels[2], 1)  # cerrado = nivel alto (como v1)
        self.assertTrue(hw.read_relay(2))
        self.assertFalse(hw.read_relay(3))

    def test_active_low(self):
        hw = SimulatedHardware(active_high=False)
        hw.setup(PINS)
        self.assertTrue(all(hw._levels[p] == 1 for p in PINS))  # abierto = nivel alto
        hw.set_relay(2, True)
        self.assertEqual(hw._levels[2], 0)  # cerrado = nivel bajo
        self.assertTrue(hw.read_relay(2))
        self.assertFalse(hw.read_relay(3))

    def test_setup_returns_state_found_before_taking_control(self):
        hw = SimulatedHardware(active_high=True, initial_closed=[2, 9])
        found = hw.setup(PINS)
        self.assertEqual({p for p, c in found.items() if c}, {2, 9})
        self.assertEqual(hw.closed_pins(), set())  # y los deja abiertos

    def test_unmanaged_pin_is_rejected(self):
        hw = SimulatedHardware()
        hw.setup(PINS)
        with self.assertRaises(HardwareError):
            hw.set_relay(20, True)
        with self.assertRaises(HardwareError):
            hw.read_relay(20)

    def test_events_are_logical(self):
        hw = SimulatedHardware(active_high=False)
        hw.setup([2])
        hw.set_relay(2, True)
        hw.set_relay(2, False)
        self.assertEqual([e.closed for e in hw.events], [False, True, False])


class PigpioTest(unittest.TestCase):
    def test_not_connected(self):
        with self.assertRaises(HardwareError):
            PigpioHardware(pi=FakePi(connected=False))

    def test_setup_writes_open_level_active_high(self):
        pi = FakePi()
        hw = PigpioHardware(active_high=True, pi=pi)
        hw.setup(PINS)
        self.assertEqual(pi.writes, [(p, 0) for p in PINS])

    def test_setup_writes_open_level_active_low(self):
        pi = FakePi()
        hw = PigpioHardware(active_high=False, pi=pi)
        hw.setup(PINS)
        self.assertEqual(pi.writes, [(p, 1) for p in PINS])

    def test_set_and_read(self):
        pi = FakePi()
        hw = PigpioHardware(active_high=True, pi=pi)
        hw.setup(PINS)
        hw.set_relay(8, True)
        self.assertEqual(pi.levels[8], 1)
        self.assertTrue(hw.read_relay(8))
        hw.set_relay(8, False)
        self.assertFalse(hw.read_relay(8))

    def test_io_errors_become_hardware_errors(self):
        pi = FakePi()
        hw = PigpioHardware(pi=pi)
        hw.setup(PINS)
        pi.fail = True
        with self.assertRaises(HardwareError):
            hw.set_relay(2, True)
        with self.assertRaises(HardwareError):
            hw.read_relay(2)

    def test_injected_pi_is_not_stopped_by_close(self):
        pi = FakePi()
        PigpioHardware(pi=pi).close()
        self.assertFalse(pi.stopped)  # no es nuestro: no lo cerramos

    @unittest.skipIf(
        importlib.util.find_spec("pigpio") is not None,
        "con pigpio instalado intentaría conectar con un pigpiod real",
    )
    def test_missing_pigpio_package(self):
        with self.assertRaises(HardwareError):
            PigpioHardware()


if __name__ == "__main__":
    unittest.main()
