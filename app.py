from datetime import timedelta
from flask import flash


from flask import Flask, render_template, request, redirect, jsonify
from flask_login import (
    LoginManager, login_user, logout_user,
    login_required, current_user
)
from flask_bcrypt import Bcrypt
from flask_jwt_extended import (
    JWTManager, create_access_token,
    jwt_required, get_jwt_identity
)
import os

from data import db
from model import User, Todo

# ---------------- CONFIG ----------------

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key")

app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

app.config["JWT_SECRET_KEY"] = os.getenv(
    "JWT_SECRET_KEY", "jwt-super-secret"
)
app.config["JWT_ACCESS_TOKEN_EXPIRES"]=timedelta(minutes=30)

# ---------------- EXTENSIONS ----------------

db.init_app(app)
bcrypt = Bcrypt(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

jwt = JWTManager(app)
@jwt.expired_token_loader
def expired_token_callback(jwt_header, jwt_payload):
    return jsonify({"error": "Token expired"}), 401

@jwt.invalid_token_loader
def invalid_token_callback(error):
    return jsonify({"error": "Invalid token"}), 401

@jwt.unauthorized_loader
def missing_token_callback(error):
    return jsonify({"error": "Token missing"}), 401


    

# ---------------- LOGIN ----------------

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# =====================================================
# ==================== API (JWT) ======================
# =====================================================

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    user = User.query.filter_by(
        username=data.get("username")
    ).first()

    if not user or not bcrypt.check_password_hash(
        user.password, data.get("password")
    ):
        return jsonify({"error": "Invalid credentials"}), 401

    access_token = create_access_token(identity=user.id)
    return jsonify({"access_token": access_token}), 200


@app.route("/api/todos", methods=["GET"])
@jwt_required()
def get_todos():
    user_id = get_jwt_identity()

    page = request.args.get("page", 1, type=int)
    limit = request.args.get("limit", 5, type=int)

    pagination = Todo.query.filter_by(
        user_id=user_id
    ).order_by(
        Todo.date_c.desc()
    ).paginate(
        page=page,
        per_page=limit,
        error_out=False
    )

    return jsonify({
        "page": page,
        "limit": limit,
        "total": pagination.total,
        "todos": [
            {
                "id": t.id,
                "title": t.title,
                "desc": t.desc,
                "completed": t.completed,
                "created_at": t.date_c.strftime("%Y-%m-%d")
            }
            for t in pagination.items
        ]
    }), 200


@app.route("/api/todos", methods=["POST"])
@jwt_required()
def create_todo_api():
    user_id = get_jwt_identity()
    data = request.get_json()

    if not data or not data.get("title"):
        return jsonify({"error": "Invalid JSON"}), 400

    todo = Todo(
        title=data.get("title"),
        desc=data.get("desc"),
        user_id=user_id
    )

    db.session.add(todo)
    db.session.commit()

    return jsonify({"message": "Todo created"}), 201


# =====================================================
# ================= WEB (Flask-Login) =================
# =====================================================

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username=request.form["username"]
        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            flash("Username already exists", "error")
            return redirect("/signup")


        password = bcrypt.generate_password_hash(
            request.form["password"]
        ).decode("utf-8")

        user = User(
            username=username,
            password=password
        )
        db.session.add(user)
        db.session.commit()
        return redirect("/login")

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        user = User.query.filter_by(username=username).first()
        if not user:
            flash("Username does not exist. Please sign up.", "error")
            return redirect("/signup")
        if not bcrypt.check_password_hash(user.password, password):
            flash("Incorrect password. Please try again.", "error")
            return redirect("/login")
        login_user(user)
        flash("Logged in successfully!","Success")
        return redirect("/")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/login")


@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "POST":
        todo = Todo(
            title=request.form["title"],
            desc=request.form["desc"],
            user_id=current_user.id
        )
        db.session.add(todo)
        db.session.commit()
        return redirect("/")

    page = request.args.get("page", 1, type=int)
    search = request.args.get("search", "")

    todos = Todo.query.filter(
        Todo.user_id == current_user.id,
        Todo.title.contains(search)
    ).order_by(
        Todo.date_c.desc()
    ).paginate(page=page, per_page=5)

    return render_template("index.html", todos=todos, search=search)


@app.route("/delete/<int:id>")
@login_required
def delete(id):
    todo = Todo.query.filter_by(
        id=id,
        user_id=current_user.id
    ).first_or_404()

    db.session.delete(todo)
    db.session.commit()
    return redirect("/")


@app.route("/update/<int:id>", methods=["GET", "POST"])
@login_required
def update(id):
    todo = Todo.query.filter_by(
        id=id,
        user_id=current_user.id
    ).first_or_404()

    if request.method == "POST":
        todo.title = request.form["title"]
        todo.desc = request.form["desc"]
        db.session.commit()
        return redirect("/")

    return render_template("update.html", todo=todo)


@app.route("/toggle/<int:id>")
@login_required
def toggle(id):
    todo = Todo.query.filter_by(
        id=id,
        user_id=current_user.id
    ).first_or_404()

    todo.completed = not todo.completed
    db.session.commit()
    return redirect("/")

