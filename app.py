from flask import Flask, render_template, request, redirect, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, login_user, logout_user,
    login_required, current_user
)
from flask_bcrypt import Bcrypt
from datetime import datetime
import os

from data import db
from model import User, Todo

# ---------------- CONFIG ----------------

basedir = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key")

app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL")

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

os.makedirs(os.path.join(basedir, "instance"), exist_ok=True)

# ---------------- EXTENSIONS ----------------

db.init_app(app)
bcrypt = Bcrypt(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

# ---------------- DB INIT (CRITICAL) ----------------



# ---------------- LOGIN ----------------

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ---------------- API ROUTES ----------------

@app.route("/api/todos", methods=["GET"])
@login_required
def get_todos():
    todos = Todo.query.filter_by(user_id=current_user.id).all()
    return jsonify([
        {
            "id": t.id,
            "title": t.title,
            "desc": t.desc,
            "completed": t.completed,
            "created_at": t.date_c.strftime("%Y-%m-%d")
        }
        for t in todos
    ])

@app.route("/api/todos", methods=["POST"])
@login_required
def create_todo_api():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    todo = Todo(
        title=data.get("title"),
        desc=data.get("desc"),
        user_id=current_user.id
    )
    db.session.add(todo)
    db.session.commit()
    return jsonify({"message": "Todo created"}), 201

# ---------------- AUTH ----------------

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        password = bcrypt.generate_password_hash(
            request.form["password"]
        ).decode("utf-8")

        user = User(
            username=request.form["username"],
            password=password
        )
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

        if user and bcrypt.check_password_hash(
            user.password, request.form["password"]
        ):
            login_user(user)
            return redirect("/")

    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/login")

# ---------------- WEB ROUTES ----------------

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
        id=id, user_id=current_user.id
    ).first_or_404()

    db.session.delete(todo)
    db.session.commit()
    return redirect("/")

@app.route("/update/<int:id>", methods=["GET", "POST"])
@login_required
def update(id):
    todo = Todo.query.filter_by(
        id=id, user_id=current_user.id
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
        id=id, user_id=current_user.id
    ).first_or_404()

    todo.completed = not todo.completed
    db.session.commit()
    return redirect("/")
