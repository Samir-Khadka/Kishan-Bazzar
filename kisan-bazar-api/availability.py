"""
The lot stock invariant — ONE module for "never sell more than pooled".

Two request paths turn lot stock into orders: a buyer placing a direct
order (POST /v1/orders) and staff accepting a bid/offer (POST
/v1/offers/<id>/decide). They used to each carry a private copy of the
availability math and lock discipline — and the copies diverged: the
offer path checked the offer's status *outside* its transaction, so
two racing accepts could convert one offer into two orders. One
module, one implementation, one place for the race tests to aim at.

Rules of the module:
  - The check (SUM of existing orders vs pooled_kg) and the insert
    happen inside the caller's BEGIN IMMEDIATE write transaction.
    SQLite serializes writers, so check+insert under the lock is
    atomic against every other order path that also comes through
    here — which is now all of them.
  - Failures raise StockError carrying the HTTP status and message
    the API has always returned for that case, so route handlers
    translate it mechanically and the API contract doesn't drift.
"""
import repositories


class StockError(Exception):
    """A stock/availability rejection with its API status + message."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def total_ordered_kg(conn, lot_id):
    row = conn.execute(
        "SELECT COALESCE(SUM(quantity_kg), 0) AS total FROM orders "
        "WHERE lot_id=?",
        (lot_id,),
    ).fetchone()
    return row["total"]


def remaining_kg(conn, lot):
    return (lot["pooled_kg"] or 0) - total_ordered_kg(conn, lot["id"])


def insert_order(conn, lot, quantity_kg, buyer_name, buyer_phone, *,
                 unit_rate=None, insufficient_status=400):
    """Insert an order against `lot` if enough stock remains.

    THE CALLER MUST HOLD THE WRITE LOCK (BEGIN IMMEDIATE) — the
    availability re-read and the insert are only atomic inside it.
    `unit_rate` defaults to the lot's confirmed rate; offer acceptance
    passes the agreed (bid/counter) rate instead. Raises StockError
    when the remaining stock can't cover the order.
    """
    already = total_ordered_kg(conn, lot["id"])
    pooled = lot["pooled_kg"] or 0
    if already + quantity_kg > pooled:
        raise StockError(
            f"only {pooled - already} kg still available on this lot",
            insufficient_status)
    cur = conn.execute(
        "INSERT INTO orders (buyer_name, buyer_phone, lot_id, "
        "quantity_kg, unit_rate, status) VALUES (?,?,?,?,?,'placed')",
        (buyer_name, buyer_phone, lot["id"], quantity_kg,
         lot["rate_per_kg"] if unit_rate is None else unit_rate),
    )
    return cur.lastrowid


def place_order_locked(conn, *, lot_id, quantity_kg, buyer_name,
                       buyer_phone):
    """The direct-order path, whole: take the write lock, validate
    the lot, check stock, insert, commit. Returns (order_id, lot).

    Raises StockError (already rolled back) for every rejection the
    endpoint has always produced: 404 unknown lot, 409 lot not
    orderable, 400 quantity beyond the lot / beyond remaining stock.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        lot = repositories.get_lot(conn, lot_id)
        if not lot:
            raise StockError("lot not found", 404)
        if lot["status"] != "ready":
            raise StockError(
                f"lot is '{lot['status']}', not orderable", 409)
        if quantity_kg > (lot["pooled_kg"] or 0):
            raise StockError("quantity exceeds available lot size", 400)
        order_id = insert_order(
            conn, lot, quantity_kg, buyer_name, buyer_phone,
            insufficient_status=400)
        conn.commit()
        return order_id, lot
    except Exception:
        conn.rollback()
        raise
