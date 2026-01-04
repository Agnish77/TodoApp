from flask import Flask,render_template, request, redirect
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from flask import jsonify
from flask_login import LoginManager
from model import User
from flask_bcrypt import Bcrypt
import os
from flask_login import login_user, logout_user, login_required, current_user
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
    "DATABASE_URL", "sqlite:///project.db"
)


login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

bcrypt = Bcrypt(app)
from data import db
from model import Todo

db.init_app(app)
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))
@app.route("/api/todos",methods=["GET"])
@login_required
def get_todos():
    todos = Todo.query.filter_by(user_id=current_user.id).all()

    return jsonify([
        {
            "id": todo.id,
            "title": todo.title,
            "desc": todo.desc,
            "completed": todo.completed,
            "created_at": todo.date_c.strftime("%Y-%m-%d")
        }
        for todo in todos
    ])
@app.route("/api/todos", methods=["POST"])
@login_required
def create_todo_api():
    data=request.get_json()
    todo=Todo(
        title=data["title"],
        desc=data["desc"],
        user_id=current_user.id

    )
    db.session.add(todo)
    db.session.commit()
    return jsonify({"message": "Todo created"}),201

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
            username = request.form["username"]
            password = bcrypt.generate_password_hash(
                request.form["password"]
            ).decode("utf-8")

            user = User(username=username, password=password)
            db.session.add(user)
            db.session.commit()

            return redirect("/login")

    return render_template("signup.html")
@app.route("/login",methods=["GET","POST"])
def login():
    if request.method=="POST":
        user=User.query.filter_by(username=request.form["username"]).first()
        if user and bcrypt.check_password_hash(user.password, request.form["password"]):
            login_user(user)
            return redirect("/")


    return render_template("login.html")


@app.route("/",methods=['GET','POST'])
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
        

    
    page=request.args.get("page",1,type=int)
    search=request.args.get("search","")
    query=Todo.query.filter(
        Todo.user_id==current_user.id,
        Todo.title.contains(search)


    )
    todos=query.order_by(Todo.date_c.desc()).paginate(
        page=page,
        per_page=5

    )
    return render_template('index.html',todos=todos,search=search)

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/login")

@app.route("/delete/<int:id>")
def delete(id):
    todo = Todo.query.get_or_404(id)
    db.session.delete(todo)
    db.session.commit()
    return redirect("/")

@app.route("/update/<int:id>", methods=["GET", "POST"])
def update(id):
    todo = Todo.query.get_or_404(id)

    if request.method == "POST":
        todo.title = request.form["title"]
        todo.desc = request.form["desc"]
        db.session.commit()
        return redirect("/")
 
    return render_template("update.html", todo=todo)


@app.route("/toggle/<int:id>")
def toggle(id):
    todo = Todo.query.get_or_404(id)
    todo.completed = not todo.completed
    db.session.commit()
    return redirect("/")

if __name__ == "__main__":
    app.run(debug=True)
