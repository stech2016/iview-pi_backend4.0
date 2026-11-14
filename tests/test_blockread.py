"""Grouped reading of the plant PC: values that lie together are fetched with one request. The promise: NOTHING is lost - every configured value
that a one-by-one read would return is returned, with the same number, also when the device refuses a group or limits the request size."""
import asyncio
import importlib.util
import random
import struct
import sys
import threading
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "plant_gateway" / "plant_gateway.py"


@pytest.fixture(scope="module")
def pg():
    spec = importlib.util.spec_from_file_location("pg_blockread", SRC)
    m = importlib.util.module_from_spec(spec)
    sys.modules["pg_blockread"] = m
    spec.loader.exec_module(m)
    return m


class FakePlc:
    """A small Modbus TCP device in a thread. Memory is random; `forbidden` addresses and `max_per_request` make it refuse like real PLCs do."""

    def __init__(self, seed=1, forbidden=(), max_per_request=2000):
        rnd = random.Random(seed)
        self.regs = [rnd.randrange(0, 65536) for _ in range(700)]
        self.bits = [rnd.randrange(0, 2) for _ in range(700)]
        self.forbidden, self.max, self.requests = set(forbidden), max_per_request, 0
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()
        threading.Thread(target=self._run, args=(ready,), daemon=True).start()
        ready.wait(5)

    def _run(self, ready):
        asyncio.set_event_loop(self.loop)
        server = self.loop.run_until_complete(asyncio.start_server(self._client, "127.0.0.1", 0))
        self.port = server.sockets[0].getsockname()[1]
        ready.set()
        self.loop.run_forever()

    async def _client(self, r, w):
        try:
            while True:
                tid, _, ln, uid = struct.unpack(">HHHB", await r.readexactly(7))
                body = await r.readexactly(ln - 1)
                fc = body[0]
                addr, cnt = struct.unpack(">HH", body[1:5])
                self.requests += 1
                if fc in (1, 2, 3, 4):
                    mem = self.bits if fc in (1, 2) else self.regs
                    if cnt > self.max:
                        resp = bytes([fc | 0x80, 3])                           # illegal data value
                    elif any(a in self.forbidden or a >= len(mem) for a in range(addr, addr + cnt)):
                        resp = bytes([fc | 0x80, 2])                           # illegal data address
                    elif fc in (1, 2):
                        data = bytearray((cnt + 7) // 8)
                        for i in range(cnt):
                            if mem[addr + i]:
                                data[i // 8] |= 1 << (i % 8)
                        resp = bytes([fc, len(data)]) + bytes(data)
                    else:
                        resp = bytes([fc, cnt * 2]) + b"".join(struct.pack(">H", mem[addr + i]) for i in range(cnt))
                else:
                    resp = bytes([fc | 0x80, 1])
                w.write(struct.pack(">HHHB", tid, 0, len(resp) + 1, uid) + resp)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass


def _device(pg, plc, regs):
    dev = {"id": 1, "name": "plc", "protocol": "Modbus TCP", "host": "127.0.0.1", "port": plc.port, "unit_id": 1, "timeout_ms": 1500,
           "word_order": "big", "byte_order": "big", "address_base": 0, "registers": regs}
    return dev


def _reg(i, rtype, addr, dt="float32", bit=None):
    return {"id": i, "metric_name": f"m{i}", "register_type": rtype, "register_address": addr, "data_type": dt, "bit_index": bit}


def _read(pg, plc, regs, merge=True):
    old = pg.BLOCK_GAP, pg.BLOCK_BIT_GAP
    if not merge:
        pg.BLOCK_GAP = pg.BLOCK_BIT_GAP = -1          # never share a request: the old one-by-one way
    try:
        dev = _device(pg, plc, regs)
        link = pg.ModbusLink([dev], {"unit_id": 1})
        try:
            return {r["id"]: repr(v) for r, v in link.read_device(dev)}, link   # repr: random memory contains NaN, which never equals itself
        finally:
            link.close()
    finally:
        pg.BLOCK_GAP, pg.BLOCK_BIT_GAP = old


def _random_layout(rnd):
    regs, used = [], set()
    for i in range(rnd.randrange(1, 60)):
        rtype = rnd.choice(["holding", "holding", "holding", "input", "coil", "discrete"])
        dt = "bool" if rtype in ("coil", "discrete") else rnd.choice(["float32", "float32", "uint16", "int16", "int32", "uint32", "float64"])
        n = 1 if rtype in ("coil", "discrete") else {"float32": 2, "uint16": 1, "int16": 1, "int32": 2, "uint32": 2, "float64": 4}[dt]
        addr = rnd.randrange(0, 300)
        if any((rtype, addr + k) in used for k in range(n)):
            continue
        used.update((rtype, addr + k) for k in range(n))
        bit = None
        regs.append(_reg(len(regs) + 1, rtype, addr, dt if rtype not in ("coil", "discrete") else "float32", bit))
    return regs


def test_grouped_reading_returns_exactly_what_one_by_one_reading_returns(pg):
    plc = FakePlc(seed=7)
    rnd = random.Random(2026)
    for _ in range(60):
        layout = _random_layout(rnd)
        if not layout:
            continue
        grouped, _ = _read(pg, plc, layout, merge=True)
        single, _ = _read(pg, plc, layout, merge=False)
        assert grouped == single and len(grouped) == len(layout)          # not one value missing, not one different


def test_two_hundred_values_need_only_a_handful_of_requests(pg):
    plc = FakePlc(seed=3)
    regs = [_reg(i + 1, "holding", i * 2) for i in range(200)]            # 200 float32 = 400 registers in a row
    before = plc.requests
    got, _ = _read(pg, plc, regs)
    assert len(got) == 200
    assert plc.requests - before <= 5                                       # was 200


def test_a_refused_address_inside_a_group_loses_nothing_else_and_is_not_retried_every_second(pg):
    plc = FakePlc(seed=4, forbidden={14, 15})                              # the PLC has a hole where sensor 8 lives
    regs = [_reg(i + 1, "holding", i * 2) for i in range(16)]
    dev = _device(pg, plc, regs)
    link = pg.ModbusLink([dev], {"unit_id": 1})
    first = {r["id"]: v for r, v in link.read_device(dev)}
    assert set(first) == set(range(1, 17)) - {8} and len(first) == 15       # all but the refused one
    n = plc.requests
    link.read_device(dev)
    assert plc.requests - n == 16                                           # second round: no useless group request again - 16 single reads (the refused one is still asked, as before)
    link.close()


def test_a_device_that_allows_only_small_requests_still_gives_every_value(pg):
    plc = FakePlc(seed=5, max_per_request=4)                               # refuses anything longer than 4 registers
    regs = [_reg(i + 1, "holding", i * 2) for i in range(30)]
    grouped, _ = _read(pg, plc, regs)
    single, _ = _read(pg, plc, regs, merge=False)
    assert len(grouped) == 30 and grouped == single


def test_far_apart_values_are_not_dragged_into_one_big_request(pg):
    plc = FakePlc(seed=6)
    regs = [_reg(1, "holding", 0), _reg(2, "holding", 400)]
    before = plc.requests
    got, _ = _read(pg, plc, regs)
    assert len(got) == 2 and plc.requests - before == 2


def test_a_silent_device_is_still_reported_as_silent(pg):
    plc = FakePlc(seed=8)
    regs = [_reg(i + 1, "holding", i * 2) for i in range(10)]
    dev = _device(pg, plc, regs)
    dev["port"] = 1                                                         # nobody listens here
    link = pg.ModbusLink([dev], {"unit_id": 1})
    with pytest.raises((pg.CommError, pg.LinkError)):
        link.read_device(dev)
