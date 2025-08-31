from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from db import users_collection, clients_collection
from werkzeug.security import check_password_hash

login_bp = Blueprint('login', __name__, template_folder='templates')


def _status(entity, default="active"):
    return (entity or {}).get("status", default).strip().lower()


def _is_active(entity):
    return _status(entity) == "active"


@login_bp.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        # Clear previous session early
        session.clear()

        # === Admin / Assistant Login ===
        user = users_collection.find_one({"username": username})
        if user and check_password_hash(user.get("password", "") or "", password):

            # Blocked/Inactive gate for ALL staff roles
            if not _is_active(user):
                s = _status(user)
                if s == "blocked":
                    flash("Your account is blocked. Contact an administrator.", "danger")
                else:
                    flash("Your account is inactive. Contact an administrator.", "warning")
                return redirect(url_for('login.login'))

            # Set session
            session['username'] = username
            session['role'] = user.get('role', 'assistant')
            session['name'] = user.get("name", "User")

            role = session['role']
            if role == 'admin':
                return redirect(url_for('admin_dashboard.dashboard'))
            elif role == 'assistant':
                return redirect(url_for('assistant_dashboard.dashboard'))
            else:
                flash("Unauthorized role.", "warning")
                session.clear()
                return redirect(url_for('login.login'))

        # === Registered Client Login (client_id + phone as password) ===
        client = clients_collection.find_one({"client_id": username})
        if client and (client.get("phone") or "") == password:
            # Enforce status
            s = _status(client)
            if s != "active":
                if s == "blocked":
                    flash("Your client account is blocked. Please contact support.", "danger")
                else:
                    flash("Your client account is inactive. Please contact support.", "warning")
                return redirect(url_for('login.login'))

            # Set session for active client
            session['role'] = 'client'
            session['client_id'] = str(client['_id'])
            session['client_code'] = client.get('client_id')
            session['client_name'] = client.get('name')
            return redirect(url_for('client_dashboard.dashboard'))

        # === External Client Login (name + phone) ===
        # Only allow if explicitly status == "external" and not blocked/inactive
        external = clients_collection.find_one({
            "name": {"$regex": f"^{username}$", "$options": "i"},
            "phone": password
        })
        if external:
            s = _status(external)
            if s == "blocked":
                flash("Your external account is blocked. Please contact support.", "danger")
                return redirect(url_for('login.login'))
            if s == "inactive":
                flash("Your external account is inactive. Please contact support.", "warning")
                return redirect(url_for('login.login'))

            if s == "external":
                session['role'] = 'external'
                session['external_id'] = str(external['_id'])
                session['external_name'] = external.get('name')
                session['external_phone'] = external.get('phone')
                return redirect(url_for('external.external_dashboard'))

        # If no match at all
        flash("Invalid credentials", "danger")
        return redirect(url_for('login.login'))

    # GET
    return render_template('login.html')
