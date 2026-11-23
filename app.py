import os
from datetime import datetime
from urllib.parse import quote_plus

from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, text
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def build_database_uri() -> str:
    """Resolve database URI from environment (PostgreSQL on Azure) or local SQLite fallback."""
    database_url = os.environ.get("DATABASE_URL", "").strip()

    if not database_url and os.environ.get("PGHOST"):
        pg_user = quote_plus(os.environ.get("PGUSER", ""))
        pg_password = quote_plus(os.environ.get("PGPASSWORD", ""))
        pg_host = os.environ["PGHOST"].strip()
        pg_port = os.environ.get("PGPORT", "5432").strip()
        pg_db = os.environ.get("PGDATABASE", "postgres").strip()
        pg_ssl = os.environ.get("PGSSLMODE", "require").strip()
        database_url = (
            f"postgresql+psycopg://{pg_user}:{pg_password}@{pg_host}:{pg_port}/{pg_db}"
            f"?sslmode={pg_ssl}"
        )

    if not database_url:
        sqlite_path = os.path.join(BASE_DIR, "finance_tracker.db")
        return f"sqlite:///{sqlite_path}"

    # Normalize standard postgres URLs to SQLAlchemy + psycopg (v3) driver
    if database_url.startswith("postgres://"):
        database_url = "postgresql+psycopg://" + database_url[len("postgres://") :]
    elif database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url[len("postgresql://") :]

    # Azure Database for PostgreSQL enforces SSL by default
    if "postgres.database.azure.com" in database_url and "sslmode=" not in database_url:
        separator = "&" if "?" in database_url else "?"
        database_url = f"{database_url}{separator}sslmode=require"

    return database_url


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")

database_uri = build_database_uri()
app.config["SQLALCHEMY_DATABASE_URI"] = database_uri
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
if database_uri.startswith("postgresql"):
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }

db = SQLAlchemy(app)


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    email = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(50), nullable=False)
    password = db.Column(db.String(255), nullable=False)


class Transaction(db.Model):
    __tablename__ = "transactions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    amount = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(100), nullable=False)
    date = db.Column(db.String(20), nullable=False)
    description = db.Column(db.Text)
    payment_method = db.Column(db.String(50), nullable=False)

    def to_tuple(self):
        return (
            self.id,
            self.user_id,
            self.amount,
            self.category,
            self.date,
            self.description or "",
            self.payment_method,
        )


def init_db():
    with app.app_context():
        db.create_all()


init_db()


@app.route("/health")
def health():
    try:
        db.session.execute(text("SELECT 1"))
        backend = "postgresql" if database_uri.startswith("postgresql") else "sqlite"
        return jsonify({"status": "ok", "database": backend}), 200
    except Exception as exc:
        return jsonify({"status": "error", "detail": str(exc)}), 500


@app.route("/")
def index():
    if "user_id" in session and "username" in session:
        user_id = session["user_id"]
        username = session["username"]

        user_transactions = Transaction.query.filter_by(user_id=user_id).all()

        total_amount = sum(t.amount for t in user_transactions)
        total_upi = sum(t.amount for t in user_transactions if t.payment_method == "UPI")
        total_cash = sum(t.amount for t in user_transactions if t.payment_method == "Cash")

        return render_template(
            "index.html",
            username=username,
            total_amount=total_amount,
            total_upi=total_upi,
            total_cash=total_cash,
        )
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            session["user_id"] = user.id
            session["username"] = user.username
            return redirect(url_for("index"))

        flash("Invalid username or password. Please try again.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        email = request.form["email"].strip()
        phone = request.form["phone"].strip()
        password = request.form["password"]

        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            flash("Username already exists. Please choose a different one.", "danger")
        else:
            new_user = User(
                username=username,
                email=email,
                phone=phone,
                password=generate_password_hash(password),
            )
            db.session.add(new_user)
            db.session.commit()
            flash("Registration successful! Please log in.", "success")
            return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/transactions")
def transactions():
    if "user_id" in session and "username" in session:
        user_id = session["user_id"]
        username = session["username"]

        rows = (
            Transaction.query.filter_by(user_id=user_id)
            .order_by(Transaction.date.desc(), Transaction.id.desc())
            .all()
        )
        transaction_tuples = [t.to_tuple() for t in rows]

        return render_template(
            "transaction.html", transactions=transaction_tuples, username=username
        )
    return redirect(url_for("login"))


@app.route("/add_transaction", methods=["POST"])
def add_transaction():
    if "user_id" in session and "username" in session:
        user_id = session["user_id"]
        date = request.form["date"]
        category = request.form["category"]
        try:
            amount = float(request.form["amount"])
        except ValueError:
            flash("Invalid amount value.", "danger")
            return redirect(url_for("transactions"))
        payment_method = request.form["payment_method"]
        description = request.form.get("notes", "")

        new_tx = Transaction(
            user_id=user_id,
            date=date,
            category=category,
            amount=amount,
            payment_method=payment_method,
            description=description,
        )
        db.session.add(new_tx)
        db.session.commit()

        return redirect(url_for("transactions"))
    return redirect(url_for("login"))


@app.route("/delete_transaction/<int:transaction_id>", methods=["POST"])
def delete_transaction(transaction_id):
    if "user_id" in session and "username" in session:
        user_id = session["user_id"]
        tx = Transaction.query.filter_by(id=transaction_id, user_id=user_id).first()
        if tx:
            db.session.delete(tx)
            db.session.commit()
            flash("Transaction deleted successfully.", "success")
    else:
        flash("You must be logged in to delete a transaction.", "danger")
    return redirect(url_for("transactions"))


@app.route("/daily_spending_data")
def daily_spending_data():
    if "user_id" in session:
        user_id = session["user_id"]

        data = (
            db.session.query(Transaction.date, func.sum(Transaction.amount))
            .filter(Transaction.user_id == user_id)
            .group_by(Transaction.date)
            .order_by(Transaction.date.asc())
            .all()
        )

        labels = [row[0] for row in data]
        amounts = [float(row[1]) for row in data]
        return jsonify({"labels": labels, "amounts": amounts})
    return redirect(url_for("login"))


@app.route("/monthly_spending_data")
def monthly_spending_data():
    if "user_id" in session:
        user_id = session["user_id"]

        month_expr = func.substr(Transaction.date, 1, 7)
        data = (
            db.session.query(month_expr.label("month"), func.sum(Transaction.amount))
            .filter(Transaction.user_id == user_id)
            .group_by(month_expr)
            .order_by(month_expr.asc())
            .all()
        )

        labels = []
        amounts = []
        for month_str, total in data:
            try:
                labels.append(datetime.strptime(month_str, "%Y-%m").strftime("%b %Y"))
            except (TypeError, ValueError):
                labels.append(str(month_str))
            amounts.append(float(total))

        return jsonify({"labels": labels, "amounts": amounts})
    return redirect(url_for("login"))


@app.route("/statistics")
def statistics():
    user_id = session.get("user_id")
    username = session.get("username")

    if user_id:
        total_expenses_result = (
            db.session.query(func.sum(Transaction.amount))
            .filter(Transaction.user_id == user_id)
            .scalar()
        )
        total_expenses = float(total_expenses_result or 0)

        expense_by_category_result = (
            db.session.query(Transaction.category, func.sum(Transaction.amount))
            .filter(Transaction.user_id == user_id)
            .group_by(Transaction.category)
            .all()
        )
        expense_by_category = {
            category: float(amount) for category, amount in expense_by_category_result
        }

        top_spending_categories_result = (
            db.session.query(Transaction.category, func.sum(Transaction.amount))
            .filter(Transaction.user_id == user_id)
            .group_by(Transaction.category)
            .order_by(func.sum(Transaction.amount).desc())
            .limit(5)
            .all()
        )
        top_spending_categories = {
            category: float(amount) for category, amount in top_spending_categories_result
        }

        return render_template(
            "statistics.html",
            username=username,
            total_expenses=total_expenses,
            expense_by_category=expense_by_category,
            top_spending_categories=top_spending_categories,
        )
    return redirect(url_for("login"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
