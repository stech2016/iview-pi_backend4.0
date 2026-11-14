"""Two (or many) people send a command to the SAME PLC at the same moment: every command is accepted, they are written one after the other
(never at the same time on the one connection), in the order they arrived, and each one gets its own result."""
import importlib.util
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "plant_gateway" / "plant_gateway.py"


@pytest.fixture(scope="module")
def pg():
    spec = importlib.util.spec_from_file_location("pg_cmdconc", SRC)
    m = importlib.util.module_from_spec(spec)
    sys.modules["pg_cmdconc"] = m
    spec.loader.exec_module(m)
    return m


def _gateway(pg, monkeypatch):
    g = pg.Gateway.__new__(pg.Gateway)
    g.secret, g.cmd_topic = "s" * 32, "stech/co/gw/main/cmd"
    g.seen, g.cmdq, g.acks = pg.OrderedDict(), {}, []
    reg = {"id": 7, "metric_name": "setpoint", "writable": True, "register_type": "holding", "data_type": "int16", "scale_factor": 1.0,
           "write_min": 0.0, "write_max": 100.0}
    dev = {"id": 1, "name": "pump"}
    key = ("tcp", "10.0.0.5", 502)
    g.specs = {(1, 7): (dev, reg, key)}
    g.cmdq[key] = pg.queue.Queue()
    monkeypatch.setattr(g, "ack", lambda uid, stage, back=None, err="": g.acks.append((uid, stage)), raising=False)
    return g, key


def _payload(pg, g, uid, raw):
    exp = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    sig = pg.sign(g.secret, g.cmd_topic, ts, f"cmd|{uid}|1|7|{raw!r}|{exp}")
    return json.dumps({"uid": uid, "dev": 1, "reg": 7, "raw": raw, "exp": exp, "ts": ts, "sig": sig}).encode()


def test_commands_from_many_people_at_once_are_written_one_by_one(pg, monkeypatch):
    g, key = _gateway(pg, monkeypatch)
    start = threading.Barrier(8)

    def person(n):
        start.wait()                                     # all eight press "send" at the same moment
        g.handle_command(_payload(pg, g, f"cmd-{n}", float(10 + n)))
    threads = [threading.Thread(target=person, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(u for u, s in g.acks if s == "delivered") == sorted(f"cmd-{n}" for n in range(8))   # every command accepted, none lost

    spans, active, overlap = [], [0], [False]

    def execute(dev, reg, raw):                          # the PLC write; it must never run twice at the same time
        active[0] += 1
        overlap[0] = overlap[0] or active[0] > 1
        t0 = time.monotonic()
        time.sleep(0.02)
        spans.append((raw, t0, time.monotonic()))
        active[0] -= 1
        return raw
    g.run_commands(key, execute)                         # the one thread that owns the connection
    assert len(spans) == 8                               # all eight written
    assert not overlap[0]                                # never at the same time
    assert all(spans[i][2] <= spans[i + 1][1] for i in range(7))   # strictly one after the other
    assert sorted(u for u, s in g.acks if s == "written") == sorted(f"cmd-{n}" for n in range(8))   # each person gets their own result


def test_the_same_command_sent_twice_is_written_once(pg, monkeypatch):
    g, key = _gateway(pg, monkeypatch)
    p = _payload(pg, g, "cmd-dup", 42.0)
    g.handle_command(p)
    g.handle_command(p)                                  # a repeat (network resend) is ignored
    done = []
    g.run_commands(key, lambda d, r, raw: done.append(raw) or raw)
    assert done == [42.0]
