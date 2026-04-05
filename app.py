from datetime import timedelta
import os

from flask import Flask, render_template, request, redirect, jsonify, flash
from flask_login import (
    LoginManager, login_user, logout_user,
    login_required, current_user
)
from flask_bcrypt import Bcrypt
from flask_jwt_extended import (
    JWTManager, create_access_token,
    jwt_required, get_jwt_identity
)

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from flask_socketio import SocketIO, emit

from data import db
from model import User, Todo


# ---------------- APP ----------------

app = Flask(__name__)

app.secret_key = os.getenv("SECRET_KEY", "dev-secret")

app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
    "DATABASE_URL", "sqlite:///todo.db"
)

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False


# ---------------- JWT ----------------

app.config["JWT_SECRET_KEY"] = os.getenv(
    "JWT_SECRET_KEY",
    "jwt-secret"
)

app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(minutes=30)


# ---------------- EXTENSIONS ----------------

db.init_app(app)

bcrypt = Bcrypt(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

jwt = JWTManager(app)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"]
)

socketio = SocketIO(app, cors_allowed_origins="*")


# ---------------- LOGIN MANAGER ----------------

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# =====================================================
# ==================== API (JWT) ======================
# =====================================================

@app.route("/api/login", methods=["POST"])
@limiter.limit("5 per minute")
def api_login():

    data = request.get_json()

    user = User.query.filter_by(
        username=data.get("username")
    ).first()

    if not user or not bcrypt.check_password_hash(
        user.password,
        data.get("password")
    ):
        return jsonify({"error": "Invalid credentials"}), 401

    token = create_access_token(identity=user.id)

    return jsonify({"access_token": token})


@app.route("/api/todos", methods=["GET"])
@jwt_required()
def get_todos():

    user_id = get_jwt_identity()

    todos = Todo.query.filter_by(
        user_id=user_id
    ).all()

    return jsonify([
        {
            "id": t.id,
            "title": t.title,
            "desc": t.desc,
            "completed": t.completed
        }
        for t in todos
    ])


@app.route("/api/todos", methods=["POST"])
@jwt_required()
def create_todo_api():

    user_id = get_jwt_identity()

    data = request.get_json()

    todo = Todo(
        title=data.get("title"),
        desc=data.get("desc"),
        user_id=user_id
    )

    db.session.add(todo)
    db.session.commit()

    socketio.emit("todo_update")

    return jsonify({"message": "Todo created"})


# =====================================================
# ================= WEB ROUTES ========================
# =====================================================

@app.route("/signup", methods=["GET", "POST"])
def signup():

    if request.method == "POST":

        username = request.form["username"]

        if User.query.filter_by(username=username).first():
            flash("Username exists")
            return redirect("/signup")

        password = bcrypt.generate_password_hash(
            request.form["password"]
        ).decode("utf-8")

        user = User(username=username, password=password)

        db.session.add(user)
        db.session.commit()

        return redirect("/login")

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        user = User.query.filter_by(
            username=request.form["username"]
        ).first()

        if not user:
            flash("User not found")
            return redirect("/login")

        if not bcrypt.check_password_hash(
            user.password,
            request.form["password"]
        ):
            flash("Wrong password")
            return redirect("/login")

        login_user(user)

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

        socketio.emit("todo_update")

        return redirect("/")

    todos = Todo.query.filter_by(
        user_id=current_user.id
    ).order_by(Todo.date_c.desc()).all()

    return render_template("index.html", todos=todos)


@app.route("/delete/<int:id>")
@login_required
def delete(id):

    todo = Todo.query.filter_by(
        id=id,
        user_id=current_user.id
    ).first_or_404()

    db.session.delete(todo)
    db.session.commit()

    socketio.emit("todo_update")

    return redirect("/")


@app.route("/toggle/<int:id>")
@login_required
def toggle(id):

    todo = Todo.query.filter_by(
        id=id,
        user_id=current_user.id
    ).first_or_404()

    todo.completed = not todo.completed

    db.session.commit()

    socketio.emit("todo_update")

    return redirect("/")


# ---------------- RUN ----------------

if __name__ == "__main__":
    socketio.run(app)
