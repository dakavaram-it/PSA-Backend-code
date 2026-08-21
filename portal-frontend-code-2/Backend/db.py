# Backend/db.py — pooled MySQL access. Copied from ../../portal-dashboard/Backend/db.py.
#
# WRITE_POOL/run_write_tx come along unchanged rather than being stripped: Dashboard 2
# is read-only today, and keeping this file byte-identical to the one it was copied
# from is what lets a fix there be applied here by copying it again.
#
# Two pools, same reasoning as ../admin-dashboard/Backend/db.py: autocommit=True on the read
# pool (otherwise a pooled connection would keep serving one frozen REPEATABLE
# READ snapshot for its whole lifetime), conn.ping(reconnect=False) (a
# reconnect would start a session without the READ ONLY pragma below), and the
# BoundedSemaphore per pool (caps concurrent connections against the shared
# RDS box). Writes manage their own transactions via run_write_tx, hence
# autocommit off on that pool.
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
    def __init__(self, setup, autocommit):
        self._setup = setup
        self._autocommit = autocommit
        self._idle = queue.LifoQueue()
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
            _close_quietly(conn)
            conn = None
            raise
        finally:
            if conn is not None:
                self._idle.put((conn, time.monotonic()))
            self._slots.release()


READ_POOL = _Pool(("SET SESSION TRANSACTION READ ONLY",), autocommit=True)
WRITE_POOL = _Pool((), autocommit=False)


@contextlib.contextmanager
def read_cursor():
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

    Used for the existence check and the post-write re-read together, so a
    whole write request costs one round trip's worth of connection setup.
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
