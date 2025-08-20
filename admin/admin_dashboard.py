from flask import Blueprint, render_template, session
from db import db

admin_dashboard_bp = Blueprint('admin_dashboard', __name__, template_folder='templates')

# Collections
clients_collection = db["clients"]
orders_collection = db["orders"]
payments_collection = db["payments"]
truck_payments_collection = db["truck_payments"]
tax_records_collection = db["tax_records"]     # <-- for OMC S-Tax paid
sbdc_collection = db["s_bdc_payment"]          # <-- for BDC bank payments

@admin_dashboard_bp.route('/dashboard')
def dashboard():
    # Count unapproved client orders
    unapproved_orders_count = orders_collection.count_documents({"status": "pending"})

    # Count overdue clients
    overdue_clients_count = clients_collection.count_documents({"status": "overdue"})

    # Count unconfirmed normal payments
    unconfirmed_payments_count = payments_collection.count_documents({"status": "pending"})

    # Count unconfirmed truck payments
    unconfirmed_truck_payments_count = truck_payments_collection.count_documents({"status": "pending"})

    # ✅ Truck debtors count
    pipeline = [
        {"$group": {"_id": "$client_id", "total_debt": {"$sum": "$total_debt"}, "total_paid": {"$sum": "$paid"}}},
        {"$project": {"amount_left": {"$subtract": ["$total_debt", "$total_paid"]}}},
        {"$match": {"amount_left": {"$gt": 0}}},
        {"$count": "truck_debtors_count"}
    ]
    agg_result = list(db["orders"].aggregate(pipeline))
    truck_debtors_count = agg_result[0]["truck_debtors_count"] if agg_result else 0

    # ---------- OMC unpaid S-Tax summary (JSON-safe aggregation) ----------
    omc_pipe = [
        {"$match": {
            "$or": [
                {"order_type": "s_tax"},
                {"order_type": "combo"},
                {"s_tax": {"$gt": 0}},
                {"s-tax": {"$gt": 0}},
            ]
        }},
        # choose s_tax if present else s-tax
        {"$addFields": {
            "rate_raw": {"$ifNull": ["$s_tax", {"$ifNull": ["$s-tax", 0]}]},
            "qty_raw": {"$ifNull": ["$quantity", 0]}
        }},
        {"$addFields": {
            "rate": {"$toDouble": "$rate_raw"},
            "qty": {"$toDouble": "$qty_raw"}
        }},
        {"$addFields": {"due": {"$round": [{"$multiply": ["$rate", "$qty"]}, 2]}}},
        # sum of S-Tax payments for this order_id (order_oid in tax_records)
        {"$lookup": {
            "from": "tax_records",
            "let": {"oid": "$_id"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$order_oid", "$$oid"]},
                            {"$regexMatch": {"input": "$type", "regex": r"^s[\s_-]*tax$", "options": "i"}}
                        ]
                    }
                }},
                {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
            ],
            "as": "tax"
        }},
        {"$addFields": {"paid": {"$ifNull": [{"$arrayElemAt": ["$tax.total", 0]}, 0]}}},
        {"$addFields": {"remain": {"$round": [{"$subtract": ["$due", {"$toDouble": "$paid"}]}, 2]}}},
        {"$match": {"remain": {"$gt": 0}}},
        {"$group": {"_id": "$omc", "outstanding": {"$sum": "$remain"}}},
        {"$project": {"_id": 0, "omc": {"$ifNull": ["$_id", "—"]}, "outstanding": {"$round": ["$outstanding", 2]}}},
        {"$sort": {"outstanding": -1}}
    ]
    omc_rows = list(orders_collection.aggregate(omc_pipe))
    omc_debtors_count = len(omc_rows)
    omc_outstanding_total = float(sum((row.get("outstanding") or 0) for row in omc_rows))

    # ---------- BDC unpaid bank-payment summary ----------
    bdc_pipe = [
        {"$match": {"payment_type": {"$regex": r"^(credit|from\s*account)$", "$options": "i"}}},
        {"$lookup": {"from": "orders", "localField": "order_id", "foreignField": "_id", "as": "ord"}},
        {"$addFields": {
            "bdc_id_eff": {"$ifNull": ["$bdc_id", {"$arrayElemAt": ["$ord.bdc_id", 0]}]},
            "amount_d": {"$toDouble": "$amount"},
            "paid_d": {"$toDouble": {"$ifNull": ["$bank_paid_total", 0]}}
        }},
        {"$addFields": {"remain": {"$subtract": ["$amount_d", "$paid_d"]}}},
        {"$match": {"bdc_id_eff": {"$ne": None}, "remain": {"$gt": 0}}},
        {"$group": {"_id": "$bdc_id_eff", "outstanding": {"$sum": "$remain"}}},
        {"$project": {"_id": 0, "outstanding": {"$round": ["$outstanding", 2]}}},
        {"$sort": {"outstanding": -1}}
    ]
    bdc_rows = list(sbdc_collection.aggregate(bdc_pipe))
    bdc_debtors_count = len(bdc_rows)
    bdc_outstanding_total = float(sum((row.get("outstanding") or 0) for row in bdc_rows))

    return render_template(
        'admin/admin_dashboard.html',
        unapproved_orders_count=unapproved_orders_count,
        overdue_clients_count=overdue_clients_count,
        unconfirmed_payments_count=unconfirmed_payments_count,
        unconfirmed_truck_payments_count=unconfirmed_truck_payments_count,
        truck_debtors_count=truck_debtors_count,
        # NEW: pass OMC/BDC debt summaries for tooltips/badges
        omc_debtors_count=omc_debtors_count,
        omc_outstanding_total=omc_outstanding_total,
        bdc_debtors_count=bdc_debtors_count,
        bdc_outstanding_total=bdc_outstanding_total
    )
