from flask import Blueprint, render_template, jsonify, request
from bson import ObjectId
from datetime import datetime
from db import db

# Collections
payments_col = db["payments"]
clients_col  = db["clients"]

payments_bp = Blueprint("payments_bp", __name__)

PAGE_SIZE = 10

def _to_oid(val):
    if isinstance(val, ObjectId):
        return val
    try:
        return ObjectId(val)
    except Exception:
        return None

def _fmt_date(dt):
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M")
    try:
        # try parsing ISO strings, if any
        return datetime.fromisoformat(str(dt)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(dt) if dt else "N/A"

@payments_bp.route("/payments")
def view_payments():
    # Query params
    q = (request.args.get("q") or "").strip()
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except Exception:
        page = 1

    # Filter (server-side search)
    filt = {}
    if q:
        # look up client ids that match name/phone/client_id
        client_sub = {"$or": [
            {"name":     {"$regex": q, "$options": "i"}},
            {"phone":    {"$regex": q, "$options": "i"}},
            {"client_id":{"$regex": q, "$options": "i"}},
        ]}
        match_ids = [c["_id"] for c in clients_col.find(client_sub, {"_id": 1})]
        if match_ids:
            filt["client_id"] = {"$in": match_ids}
        else:
            # keep a filter that won't match anything to avoid full-scan return
            filt["client_id"] = {"$in": []}

    total_count = payments_col.count_documents(filt)

    total_pages = max((total_count + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    if page > total_pages:
        page = total_pages

    skip = (page - 1) * PAGE_SIZE

    # Pull current page items
    cur = payments_col.find(
        filt,
        {
            "client_id": 1,
            "amount": 1,
            "bank_name": 1,
            "status": 1,
            "account_last4": 1,
            "proof_url": 1,
            "date": 1
        }
    ).sort("date", -1).skip(skip).limit(PAGE_SIZE)

    payment_docs = list(cur)

    # Batch-load clients referenced on THIS page
    client_ids = list({ _to_oid(p.get("client_id")) for p in payment_docs if _to_oid(p.get("client_id")) })
    client_map = {}
    if client_ids:
        for c in clients_col.find({"_id": {"$in": client_ids}}, {"name":1, "client_id":1, "phone":1}):
            client_map[str(c["_id"])] = c

    # Build view models
    payments = []
    for p in payment_docs:
        cid = _to_oid(p.get("client_id"))
        c   = client_map.get(str(cid)) if cid else None
        payments.append({
            "_id":            str(p["_id"]),
            "client_name":    (c.get("name") if c else "Unknown") or "Unknown",
            "client_id_str":  (c.get("client_id") if c else "Unknown") or "Unknown",
            "phone":          (c.get("phone") if c else "Unknown") or "Unknown",
            "amount":         float(p.get("amount") or 0),
            "bank_name":      p.get("bank_name") or "-",
            "account_last4":  p.get("account_last4") or "",
            "status":         p.get("status") or "pending",
            "proof_url":      p.get("proof_url") or "#",
            "date_str":       _fmt_date(p.get("date")),
        })

    # Pagination window (compact: current ±2)
    start_idx = (page - 1) * PAGE_SIZE + 1 if total_count else 0
    end_idx   = min(page * PAGE_SIZE, total_count)

    win_start = max(1, page - 2)
    win_end   = min(total_pages, page + 2)
    page_window = list(range(win_start, win_end + 1))
    show_first_ellipsis = win_start > 2
    show_last_ellipsis  = win_end < total_pages - 1

    return render_template(
        "partials/payments.html",
        payments=payments,
        page=page,
        total_pages=total_pages,
        total_count=total_count,
        start_idx=start_idx,
        end_idx=end_idx,
        q=q,
        page_window=page_window,
        show_first_ellipsis=show_first_ellipsis,
        show_last_ellipsis=show_last_ellipsis
    )

@payments_bp.route("/confirm_payment/<payment_id>", methods=["POST"])
def confirm_payment(payment_id):
    try:
        feedback = (request.form.get("feedback") or "").strip()
        update_fields = {
            "status": "confirmed",
            "confirmed_at": datetime.utcnow()
        }
        if feedback:
            update_fields["feedback"] = feedback

        result = payments_col.update_one(
            {"_id": ObjectId(payment_id)},
            {"$set": update_fields}
        )

        if result.modified_count == 1:
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "No matching payment found."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
