"""Self-check for the connection pool. No DB, no test framework:

    cd Backend && python test_pool.py

Swaps pymysql.connect for a fake so every connection is countable. What matters is that
the pool actually saves handshakes (the whole reason it exists), that a connection the
server closed while idle does not surface as a request failure, and that a failed write
is never replayed.
"""

import queue

import pymysql

import main


class FakeConn:
    """Counts what was run on it. `dead` makes the next statement raise the error a
    server-side close produces."""

    def __init__(self, dead=False):
        self.dead = dead
        self.closed = False
        self.commits = 0
        self.pings = 0
        self.statements = []

    def cursor(self):
        return FakeCursor(self)

    def ping(self, reconnect=False):
        self.pings += 1
        if self.dead:
            raise pymysql.err.OperationalError(2006, "MySQL server has gone away")

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.lastrowid = 7

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, args=None):
        if self.conn.dead:
            raise pymysql.err.OperationalError(2013, "Lost connection to MySQL server")
        self.conn.statements.append((sql, args))
        return 1

    def fetchall(self):
        return [{"ok": 1}]


def install(*conns):
    """Hand out the given connections in order; fail loudly if more are asked for than
    the test allowed — an unexpected extra connect is exactly the regression here."""
    made = list(conns)
    handed = []

    def fake_connect(**config):
        assert made, "opened more connections than the test allowed"
        conn = made.pop(0)
        handed.append(conn)
        return conn

    pymysql.connect = fake_connect
    main._POOL = queue.LifoQueue()
    return handed


def check_second_call_reuses_the_connection():
    # The point of the whole pool: one handshake, two queries.
    conns = install(FakeConn())
    main.query("SELECT 1")
    main.query("SELECT 2")
    assert len(conns) == 1, conns
    assert len(conns[0].statements) == 2, conns[0].statements
    assert not conns[0].closed


def check_stale_pooled_connection_is_retried():
    # A connection idle past wait_timeout is closed server-side with no notice. The
    # request must still succeed, on a fresh connection, rather than 500.
    stale = FakeConn(dead=True)
    replacement = FakeConn()
    install(replacement)
    main._POOL.put(stale)
    rows = main.query("SELECT 2")
    assert rows == [{"ok": 1}], rows
    assert stale.closed, "the dead connection must not go back in the pool"
    assert len(replacement.statements) == 1, replacement.statements


def check_new_connection_failure_is_not_retried():
    # A brand-new connection failing is a real error (bad credentials, DB down), not a
    # stale socket. Retrying it would only double the wait before the same failure.
    install(FakeConn(dead=True))
    try:
        main.query("SELECT 1")
    except pymysql.err.OperationalError:
        return
    raise AssertionError("expected the error to propagate")


def check_write_pings_before_reusing():
    # A write is never replayed after a mid-flight failure (it may already have been
    # applied), so a pooled connection is verified up front instead.
    first = FakeConn()
    install(first)
    main.insert("INSERT INTO t VALUES (1)")
    assert first.pings == 0, "a fresh connection needs no ping"
    assert first.commits == 1
    main.update("UPDATE t SET a = 1")
    assert first.pings == 1, "the reused connection must be pinged"
    assert first.commits == 2


def check_failed_write_is_discarded_not_pooled():
    broken = FakeConn()
    install(broken)
    broken.dead = True
    try:
        main.insert("INSERT INTO t VALUES (1)")
    except pymysql.err.OperationalError:
        pass
    assert broken.closed, "a connection that failed mid-write must not be reused"
    assert main._POOL.qsize() == 0


def check_pool_is_capped():
    main.POOL_MAX = 2
    main._POOL = queue.LifoQueue()
    for _ in range(5):
        main._POOL.put(FakeConn())
    extra = FakeConn()
    main._release(main._POOL, extra)
    assert extra.closed, "past the cap, a returned connection is closed rather than kept"
    main.POOL_MAX = 10


if __name__ == "__main__":
    real_connect = pymysql.connect
    try:
        check_second_call_reuses_the_connection()
        check_stale_pooled_connection_is_retried()
        check_new_connection_failure_is_not_retried()
        check_write_pings_before_reusing()
        check_failed_write_is_discarded_not_pooled()
        check_pool_is_capped()
    finally:
        pymysql.connect = real_connect
    print("ok")
