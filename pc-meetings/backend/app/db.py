"""MySQL access for the party database.

Route handlers are plain ``def``, so FastAPI runs them in its threadpool and a
blocking driver is the right shape. Three things about that threadpool and this
link shape everything below — RDS is reached from outside its VPC here, so a
round trip costs ~200ms and opening a connection ~900ms:

* **Connections are pooled, not kept per thread.** FastAPI hands each request
  whichever worker thread is idle, so a connection parked on a thread is one the
  next request usually cannot see: every arrival on a not-yet-used thread paid
  the ~900ms handshake again, for the life of the process. Borrowing from a
  shared pool costs nothing after the first few requests, whatever thread serves
  them.
* **Reads do not ping first.** Pinging before every statement paid the round trip
  twice for every figure on screen — ~3s of the meetings list on its own. A
  connection RDS dropped while idle announces itself on the next statement
  instead, and a SELECT is safe to run again, so the read path drops it and
  retries once. Writes still ping: replaying one is not safe, and there are two
  writes in the whole service.
* **Independent read groups run at once** (`parallel`), so a page needing two
  multi-second answers waits for the slower one rather than for the sum.
"""

from __future__ import annotations

import queue
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Callable

import pymysql

from . import config

# Enough for the parallel read groups plus the requests around them. A borrow
# beyond this opens its own connection and closes it on the way back rather than
# queueing, so a burst is slow, never stuck.
POOL_SIZE = 8

_idle: queue.LifoQueue = queue.LifoQueue(maxsize=POOL_SIZE)

# Independent read groups run here (see `parallel`).
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pcm-db")

# The connection is gone, rather than the query being wrong: worth one retry.
_DEAD = (pymysql.err.OperationalError, pymysql.err.InterfaceError)


def _connect() -> pymysql.connections.Connection:
    return pymysql.connect(
        **config.DB,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=config.DB_TIMEOUT_SECONDS,
        read_timeout=config.DB_TIMEOUT_SECONDS,
        autocommit=True,
    )


def _close(c: pymysql.connections.Connection) -> None:
    try:
        c.close()
    except Exception:  # noqa: BLE001 - already dead; closing is best effort
        pass


def _release(c: pymysql.connections.Connection) -> None:
    try:
        _idle.put_nowait(c)
    except queue.Full:
        _close(c)


@contextmanager
def borrow(ping: bool = False):
    """Lend out a live connection, taking it back unless it died in use.

    `ping` is for writes only — it is a round trip, which is exactly what the
    read path is not willing to spend.
    """
    try:
        c = _idle.get_nowait()
    except queue.Empty:
        c = _connect()
    else:
        if ping:
            c.ping(reconnect=True)

    try:
        yield c
    except _DEAD:
        _close(c)
        raise
    except Exception:
        # Not a connection failure (a SQL error, say). The connection is still
        # good and goes back, or the pool would drain one bad query at a time.
        _release(c)
        raise
    else:
        _release(c)


def rows(sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    for last in (False, True):
        try:
            with borrow() as c, c.cursor() as cur:
                cur.execute(sql, args)
                return list(cur.fetchall())
        except _DEAD:
            # An idle connection RDS closed behind our back looks exactly like
            # this on first use. The dead one has already been dropped; run the
            # query once more on a fresh one, and let a second failure through —
            # that one is real and belongs to the caller.
            if last:
                raise
    raise AssertionError("unreachable")


def one(sql: str, args: tuple = ()) -> dict[str, Any] | None:
    found = rows(sql, args)
    return found[0] if found else None


def scalar(sql: str, args: tuple = ()) -> Any:
    row = one(sql, args)
    return next(iter(row.values())) if row else None


def execute(sql: str, args: tuple = ()) -> int:
    # Pinged, unlike the read path: a write that reached the server before the
    # connection died must not be replayed, so liveness is checked up front.
    with borrow(ping=True) as c, c.cursor() as cur:
        cur.execute(sql, args)
        return cur.rowcount


def parallel(*fns: Callable[[], Any]) -> list[Any]:
    """Run independent read groups concurrently, each borrowing its own connection.

    A meetings-list load is two groups that do not need each other's answers —
    the counted figures and the not-scheduled roster diff — and each is seconds
    of database work, so in sequence the page waited for their sum. Only ever
    safe for reads that share no state: the caller sees the same list of results
    it would have got calling them in order, and the first exception raised comes
    back out of `.result()` exactly as it would have.
    """
    return [f.result() for f in [_POOL.submit(fn) for fn in fns]]


def placeholders(values) -> str:
    """``IN`` list for a known-length sequence — never interpolate the values."""
    return ", ".join(["%s"] * len(values))
