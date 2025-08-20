from flask import Blueprint, render_template, request, jsonify, session, abort
from bson import ObjectId
from datetime import datetime
from db import db

cancel_bp = Blueprint("order_cancel", __name__, template_folder="templates")

orders_col        = db["orders"]
clients_col       = db["clients"]
payments_col      = db["payments"]            # client-side payments
s_bdc_payment_col = db["s_bdc_payment"]       # central BDC payments
bdc_col           = db["bdc"]

# ✅ Optional collections (existence-checked)
_existing = set(db.list_collection_names())
bdc_txn_col = db["bdc_transactions"] if "bdc_transactions" in _existing else None
tax_col     = db["tax_records"]       if "tax_records"       in _existing else None

def _oid(v):
    try:
        return v if isinstance(v, ObjectId) else ObjectId(str(v))
    except Exception:
        return None

def _role_ok():
    return "role" in session and session["role"] in ("admin", "assistant")

@cancel_bp.route("/orders/cancel", methods=["GET"])
def page_cancel_orders():
    if not _role_ok(): abort(403)
    recent_clients = list(clients_col.find({}, {"name":1}).sort("name", 1).limit(50))
    return render_template("partials/cancel_orders.html", clients=recent_clients)

@cancel_bp.route("/orders/cancel/client/<client_id>/recent", methods=["GET"])
def recent_orders_for_client(client_id):
    """Return the client's 5 most recent orders + per-order confirmed paid_total."""
    if not _role_ok(): abort(403)
    coid = _oid(client_id)
    if not coid:
        return jsonify({"success": False, "error": "Invalid client id"}), 400

    orders = list(
        orders_col.find(
            {"client_id": coid},
            {
                "product":1,"region":1,"quantity":1,"date":1,"status":1,
                "delivery_status":1,"total_debt":1,"npa_status":1,"tts_status":1
            }
        ).sort("date",-1).limit(5)
    )

    # ---- build paid_total map (confirmed only), handling ObjectId and string order_id ----
    paid_map = {}
    if orders:
        oid_list = [o["_id"] for o in orders]
        str_list = [str(x) for x in oid_list]

        # order_id stored as ObjectId
        for r in payments_col.aggregate([
            {"$match": {"order_id": {"$in": oid_list}, "status": "confirmed"}},
            {"$group": {"_id": "$order_id", "sum": {"$sum": "$amount"}}}
        ]):
            paid_map[str(r["_id"])] = float(r.get("sum") or 0.0)

        # order_id stored as string
        for r in payments_col.aggregate([
            {"$match": {"order_id": {"$in": str_list}, "status": "confirmed"}},
            {"$group": {"_id": "$order_id", "sum": {"$sum": "$amount"}}}
        ]):
            k = str(r["_id"])
            paid_map[k] = paid_map.get(k, 0.0) + float(r.get("sum") or 0.0)

    # ---- normalize output ----
    out = []
    for o in orders:
        k = str(o["_id"])
        out.append({
            "order_id": k,
            "product": o.get("product",""),
            "region": o.get("region",""),
            "quantity": float(o.get("quantity") or 0),
            "status": o.get("status",""),
            "delivery_status": o.get("delivery_status",""),
            "npa_status": o.get("npa_status",""),
            "tts_status": o.get("tts_status",""),
            "total_debt": float(o.get("total_debt") or 0),
            "paid_total": float(paid_map.get(k, 0.0)),   # ✅ added
            "date": (o.get("date") or datetime.utcnow()).isoformat()
        })
    return jsonify({"success": True, "orders": out})

@cancel_bp.route("/orders/cancel/impact/<order_id>", methods=["GET"])
def cancel_impact(order_id):
    """Return a dry-run of what will be affected + confirmed paid sum for the order."""
    if not _role_ok(): abort(403)
    oid = _oid(order_id)
    if not oid:
        return jsonify({"success": False, "error": "Invalid order id"}), 400

    order = orders_col.find_one({"_id": oid})
    if not order:
        return jsonify({"success": False, "error": "Order not found"}), 404

    oid_str = str(oid)

    # sum client payments (CONFIRMED ONLY)
    paid_cursor = payments_col.aggregate([
        {"$match": {"order_id": {"$in":[oid, oid_str]}, "status": "confirmed"}},
        {"$group": {"_id": None, "sum": {"$sum": "$amount"}, "count": {"$sum": 1}}}
    ])
    paid_doc = next(paid_cursor, None) or {"sum":0.0, "count":0}

    impact = {
        "order": {
            "status": order.get("status"),
            "delivery_status": order.get("delivery_status"),
            "npa_status": order.get("npa_status"),
            "tts_status": order.get("tts_status"),
            "total_debt": float(order.get("total_debt") or 0),
        },
        "payments": {
            "count": int(paid_doc["count"]),
            "sum": float(paid_doc["sum"] or 0.0),  # ✅ confirmed-only total paid
        },
        # ✅ match both ObjectId and string forms
        "s_bdc_payment": s_bdc_payment_col.count_documents({"order_id": {"$in":[oid, oid_str]}}),
        "bdc_payment_details_hits": bdc_col.count_documents(
            {"payment_details.order_id": {"$in":[oid, oid_str]}}
        ),
        "bdc_transactions": (bdc_txn_col.count_documents({"order_id":{"$in":[oid,oid_str]}})
                             if bdc_txn_col else 0),
        "tax_records": (tax_col.count_documents({"order_id":{"$in":[oid,oid_str]}})
                        if tax_col else 0),
        # Convenience booleans
        "has_confirmed_payments": bool(paid_doc["count"] > 0),
        "delivered": (str(order.get("delivery_status","")).lower() == "delivered")
    }
    return jsonify({"success": True, "impact": impact})

@cancel_bp.route("/orders/cancel/refund/<order_id>", methods=["POST"])
def refund_client_payments(order_id):
    """
    Clicking 'Refund' should:
      - set payments.feedback='refunded'
      - set payments.amount=0
      - set the ORDER's total_debt=0 as well
    """
    if not _role_ok(): abort(403)
    oid = _oid(order_id)
    if not oid:
        return jsonify({"success": False, "error": "Invalid order id"}), 400

    user = session.get("email") or session.get("user") or "system"
    now = datetime.utcnow()

    with db.client.start_session() as sess:
        with sess.start_transaction():
            res = payments_col.update_many(
                {"order_id": {"$in":[oid, str(oid)]}, "status": "confirmed"},
                {"$set": {"feedback": "refunded", "amount": 0.0,
                          "refunded_at": now, "refunded_by": user}},
                session=sess
            )
            # zero the order debt when refunding
            orders_col.update_one(
                {"_id": oid},
                {"$set": {"total_debt": 0.0}},
                session=sess
            )

    return jsonify({"success": True, "refunded_count": res.modified_count})

@cancel_bp.route("/orders/cancel/execute/<order_id>", methods=["POST"])
def execute_cancel(order_id):
    """Delete side-effects and mark order/NPA/TTS cancelled (with audit)."""
    if not _role_ok(): abort(403)
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    force  = bool(data.get("force"))
    if not reason:
        return jsonify({"success": False, "error": "Reason is required"}), 400

    oid = _oid(order_id)
    if not oid:
        return jsonify({"success": False, "error": "Invalid order id"}), 400

    order = orders_col.find_one({"_id": oid})
    if not order:
        return jsonify({"success": False, "error": "Order not found"}), 404

    delivered = str(order.get("delivery_status","")).lower() == "delivered"
    has_confirmed = payments_col.count_documents(
        {"order_id": {"$in":[oid, str(oid)]}, "status": "confirmed", "amount": {"$gt": 0}}
    ) > 0

    if delivered and not force:
        return jsonify({"success": False, "error": "Delivered orders require force to cancel"}), 409
    if has_confirmed and not force:
        return jsonify({"success": False, "error": "Confirmed payments exist. Refund or force to continue"}), 409

    user = session.get("email") or session.get("user") or "system"
    now = datetime.utcnow()
    oid_str = str(oid)

    with db.client.start_session() as s:
        with s.start_transaction():
            # Delete side-effects
            sbdc_del = s_bdc_payment_col.delete_many({"order_id": {"$in":[oid, oid_str]}}, session=s)
            bdc_pull = bdc_col.update_many(
                {"payment_details.order_id": {"$in":[oid, oid_str]}},
                {"$pull": {"payment_details": {"order_id": {"$in":[oid, oid_str]}}}},
                session=s
            )
            bdc_txn_del = None
            if bdc_txn_col:
                bdc_txn_del = bdc_txn_col.delete_many({"order_id": {"$in":[oid, oid_str]}}, session=s)
            tax_del = None
            if tax_col:
                tax_del = tax_col.delete_many({"order_id": {"$in":[oid, oid_str]}}, session=s)

            # Flip order to cancelled + zero debt + NPA/TTS cancelled
            orders_col.update_one(
                {"_id": oid},
                {"$set": {
                    "status": "cancelled",
                    "delivery_status": "cancelled",
                    "npa_status": "cancelled",
                    "tts_status": "cancelled",
                    "total_debt": 0.0,
                    "cancelled_at": now,
                    "cancelled_by": user,
                    "cancel_reason": reason
                }},
                session=s
            )

            # Optional: clear S‑Tax partial fields
            orders_col.update_one(
                {"_id": oid},
                {"$unset": {
                    "s_tax_paid_amount": "",
                    "s_tax_paid_at": "",
                    "s_tax_paid_by": "",
                    "s_tax_payment": "",
                    "s_tax_reference": ""
                }},
                session=s
            )

            # Audit
            db["cancellations"].insert_one({
                "order_id": oid,
                "reason": reason,
                "by_user": user,
                "at": now,
                "deleted": {
                    "s_bdc_payment_count": sbdc_del.deleted_count,
                    "bdc_payment_details_updates": bdc_pull.modified_count,
                    "bdc_transactions_deleted": (bdc_txn_del.deleted_count if bdc_txn_del else 0),
                    "tax_records_deleted": (tax_del.deleted_count if tax_del else 0),
                }
            }, session=s)

    return jsonify({"success": True, "message": "Order cancelled and postings removed"})
