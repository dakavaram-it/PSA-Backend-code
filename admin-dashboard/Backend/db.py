# Backend/db.py — pooled MySQL access.
#
# The original design opened a new connection for *every* query, so each one
# paid ~1.5 s of pure setup before doing any work. Connections are pooled and
# reused instead, and endpoints share one connection for a whole request.
import contextlib
import queue
import threading
import time

import pymysql

from config import DB, IDLE_REVALIDATE_SECONDS, POOL_SIZE


def _close_quietly(conn):
    if conn is not None:
        with contextlib.suppress(Exception):
            conn.close()


class _Pool:
    """Fixed-capacity pool of connections, each initialised once with `setup`.

    take() blocks once POOL_SIZE connections are checked out, so a burst of
    requests can't open unbounded connections against the shared RDS box.
    """

    def __init__(self, setup, autocommit):
        self._setup = setup
        self._autocommit = autocommit
        self._idle = queue.LifoQueue()  # LIFO keeps a small working set hot
        self._slots = threading.BoundedSemaphore(POOL_SIZE)

    def _connect(self):
        conn = pymysql.connect(autocommit=self._autocommit, **DB)
        with conn.cursor() as cur:
            for stmt in self._setup:
                cur.execute(stmt)
        return conn

    def _checkout(self):
        while True:
            try:
                conn, released_at = self._idle.get_nowait()
            except queue.Empty:
                return self._connect()
            if time.monotonic() - released_at <= IDLE_REVALIDATE_SECONDS:
                return conn
            try:
                # reconnect=False on purpose: a reconnect would start a *new*
                # session without the SET SESSION pragmas below, silently
                # dropping the read-only guarantee. Discard and rebuild instead.
                conn.ping(reconnect=False)
                return conn
            except Exception:
                _close_quietly(conn)

    @contextlib.contextmanager
    def take(self):
        self._slots.acquire()
        conn = None
        try:
            conn = self._checkout()
            yield conn
        except (pymysql.Error, OSError):
            # Connection-level trouble: don't hand this one back to the pool.
            # Application errors (e.g. HTTPException) fall through and keep it.
            _close_quietly(conn)
            conn = None
            raise
        finally:
            if conn is not None:
                self._idle.put((conn, time.monotonic()))
            self._slots.release()


# autocommit=True is required, not cosmetic: with it off, every SELECT opens a
# REPEATABLE READ transaction that is never committed, and a pooled connection
# would then keep serving the same frozen snapshot for its whole lifetime —
# writes made elsewhere would appear to never land.
READ_POOL = _Pool(("SET SESSION TRANSACTION READ ONLY", "SET SESSION group_concat_max_len = 8192"),
                  autocommit=True)
# Writes manage their own transactions via run_write_tx, hence autocommit off.
WRITE_POOL = _Pool(("SET SESSION group_concat_max_len = 8192",), autocommit=False)


@contextlib.contextmanager
def read_cursor():
    """One pooled read-only cursor for a whole request — endpoints that issue
    several queries share it instead of taking a connection per query."""
    with READ_POOL.take() as conn, conn.cursor() as cur:
        yield cur


def run(sql, args=None, one=False, cur=None):
    """Read query. Pass `cur` to reuse a cursor already open for this request."""
    if cur is not None:
        cur.execute(sql, args)
        rows = cur.fetchall()
        return (rows[0] if rows else None) if one else rows
    with read_cursor() as c:
        return run(sql, args, one, cur=c)


def run_write_tx(fn):
    """Run fn(cursor) as one committed transaction; rolls back on error.

    Used where several statements must land atomically (create, cascading
    delete) — and also for the existence check and the post-write re-read each
    write endpoint does, so a whole write request costs one round trip's worth
    of connection setup instead of four or five.
    """
    with WRITE_POOL.take() as conn:
        try:
            with conn.cursor() as cur:
                result = fn(cur)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
