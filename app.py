import os
import redis
import json
import threading
import re
from datetime import timedelta

from flask import Flask, render_template, request, redirect, jsonify, flash
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_socketio import SocketIO
from flasgger import Swagger

from rq import Queue

from data import db
from model import User, Todo


# =====================================================
# APP CONFIG
# =====================================================

app = Flask(__name__)

app.secret_key = os.getenv("SECRET_KEY", "dev-secret")

app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
    "DATABASE_URL",
    "sqlite:///todo.db"
)

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

swagger = Swagger(app)


# =====================================================
# JWT CONFIG
# =====================================================

app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY", "jwt-secret")
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(minutes=30)


# =====================================================
# REDIS CONFIG
# =====================================================

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

try:
    redis_client = redis.from_url(REDIS_URL)
    redis_client.ping()
    print("✅ Redis connected successfully")
except Exception as e:
    print(f"⚠️ Redis connection failed: {e}")
    print("⚠️ Running without Redis support")
    redis_client = None

task_queue = Queue(connection=redis_client) if redis_client else None


# =====================================================
# EXTENSIONS
# =====================================================

db.init_app(app)

bcrypt = Bcrypt(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "Please login to access this page"
login_manager.login_message_category = "info"

jwt = JWTManager(app)

limiter = Limiter(
    get_remote_address,
    app=app,
    storage_uri=REDIS_URL if redis_client else "memory://",
    default_limits=["200 per day", "50 per hour"]
)

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    message_queue=REDIS_URL if redis_client else None,
    async_mode="eventlet"
)


# =====================================================
# LOGIN MANAGER
# =====================================================

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# =====================================================
# REDIS EVENT SYSTEM
# =====================================================

def publish_event(event):
    """Publish event to Redis channel"""
    if redis_client:
        try:
            redis_client.publish("todo_events", event)
        except Exception as e:
            print(f"Failed to publish event: {e}")


def redis_listener():
    """Listen for Redis events"""
    if not redis_client:
        return
    
    try:
        pubsub = redis_client.pubsub()
        pubsub.subscribe("todo_events")
        
        for message in pubsub.listen():
            if message["type"] == "message":
                socketio.emit("todo_update")
    except Exception as e:
        print(f"Redis listener error: {e}")


def start_event_listener():
    """Start Redis event listener thread"""
    if redis_client:
        thread = threading.Thread(target=redis_listener)
        thread.daemon = True
        thread.start()
        print("✅ Event listener started")


# =====================================================
# CACHE SYSTEM
# =====================================================

def get_cached_todos(user_id):
    """Get cached todos for a user"""
    if not redis_client:
        return None
    
    cache_key = f"todos:{user_id}"
    
    try:
        cached = redis_client.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        print(f"Cache read error: {e}")
    
    return None


def set_cached_todos(user_id, todos_data):
    """Cache todos for a user"""
    if not redis_client:
        return
    
    cache_key = f"todos:{user_id}"
    try:
        redis_client.setex(cache_key, 60, json.dumps(todos_data))
    except Exception as e:
        print(f"Cache write error: {e}")


def invalidate_cache(user_id):
    """Invalidate cache for a user"""
    if redis_client:
        try:
            redis_client.delete(f"todos:{user_id}")
        except Exception as e:
            print(f"Cache invalidation error: {e}")


# =====================================================
# BACKGROUND JOB
# =====================================================

def log_event(event):
    """Background job to log events"""
    print(f"Background job: {event}")


# =====================================================
# PASSWORD VALIDATION HELPER
# =====================================================

def validate_password_complexity(password):
    """
    Validate password meets all requirements:
    - 6-8 characters length
    - At least 1 uppercase letter
    - At least 1 lowercase letter
    - At least 1 number
    - At least 1 special character (@$!%*?&)
    """
    if len(password) < 6 or len(password) > 8:
        return False, "Password must be 6-8 characters long"
    if not re.search(r'[A-Z]', password):
        return False, "Password must contain at least one uppercase letter"
    if not re.search(r'[a-z]', password):
        return False, "Password must contain at least one lowercase letter"
    if not re.search(r'[0-9]', password):
        return False, "Password must contain at least one number"
    if not re.search(r'[@$!%*?&]', password):
        return False, "Password must contain at least one special character (@$!%*?&)"
    return True, "Password valid"


# =====================================================
# API LOGIN
# =====================================================

@app.route("/api/login", methods=["POST"])
@limiter.limit("5 per minute")
def api_login():
    """
    User Login
    ---
    tags:
      - Authentication
    consumes:
      - application/json
    parameters:
      - in: body
        name: body
        schema:
          type: object
          properties:
            username:
              type: string
              example: user1
            password:
              type: string
              example: password123
    responses:
      200:
        description: JWT access token
      401:
        description: Invalid credentials
    """

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


# =====================================================
# GET TODOS API
# =====================================================

@app.route("/api/todos", methods=["GET"])
@jwt_required()
def get_todos():
    """
    Get Todos
    ---
    tags:
      - Todos
    parameters:
      - name: page
        in: query
        type: integer
        default: 1
      - name: limit
        in: query
        type: integer
        default: 10
    responses:
      200:
        description: List of todos
    """

    user_id = get_jwt_identity()

    page = int(request.args.get("page", 1))
    limit = int(request.args.get("limit", 10))

    query = Todo.query.filter_by(user_id=user_id)

    todos = query.paginate(page=page, per_page=limit)

    return jsonify({
        "page": page,
        "limit": limit,
        "total": todos.total,
        "data": [
            {
                "id": t.id,
                "title": t.title,
                "desc": t.desc,
                "completed": t.completed
            }
            for t in todos.items
        ]
    })


# =====================================================
# CREATE TODO API
# =====================================================

@app.route("/api/todos", methods=["POST"])
@jwt_required()
def create_todo_api():
    """
    Create Todo
    ---
    tags:
      - Todos
    consumes:
      - application/json
    parameters:
      - in: body
        name: body
        schema:
          type: object
          properties:
            title:
              type: string
              example: Buy milk
            desc:
              type: string
              example: from supermarket
    responses:
      200:
        description: Todo created
    """

    user_id = get_jwt_identity()

    data = request.get_json()
    
    if not data.get("title"):
        return jsonify({"error": "Title is required"}), 400

    todo = Todo(
        title=data.get("title"),
        desc=data.get("desc", ""),
        user_id=user_id
    )

    db.session.add(todo)
    db.session.commit()

    invalidate_cache(user_id)

    publish_event("todo_created")

    if task_queue:
        task_queue.enqueue(log_event, "todo created")

    return jsonify({"message": "Todo created", "todo": {
        "id": todo.id,
        "title": todo.title,
        "desc": todo.desc,
        "completed": todo.completed
    }}), 201


# =====================================================
# SIGNUP
# =====================================================

@app.route("/signup", methods=["GET", "POST"])
def signup():
    """User registration with password validation"""
    
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        
        # Validate username
        if not username:
            flash("Username is required", "danger")
            return redirect("/signup")
        
        if len(username) < 3 or len(username) > 20:
            flash("Username must be 3-20 characters long", "danger")
            return redirect("/signup")
        
        if not re.match(r'^[A-Za-z0-9]+$', username):
            flash("Username can only contain letters and numbers", "danger")
            return redirect("/signup")
        
        # Check if username exists
        if User.query.filter_by(username=username).first():
            flash("Username already exists. Please choose another one.", "danger")
            return redirect("/signup")
        
        # Validate password complexity
        is_valid, message = validate_password_complexity(password)
        if not is_valid:
            flash(message, "danger")
            return redirect("/signup")
        
        # Check if passwords match
        if password != confirm:
            flash("Passwords do not match", "danger")
            return redirect("/signup")
        
        # Create new user
        hashed_password = bcrypt.generate_password_hash(password).decode("utf-8")
        user = User(username=username, password=hashed_password)
        
        try:
            db.session.add(user)
            db.session.commit()
            flash("Account created successfully! Please login.", "success")
            return redirect("/login")
        except Exception as e:
            db.session.rollback()
            flash(f"Error creating account: {str(e)}", "danger")
            return redirect("/signup")
    
    return render_template("signup.html")


# =====================================================
# LOGIN
# =====================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    """User login"""
    
    if current_user.is_authenticated:
        return redirect("/")
    
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        
        if not username or not password:
            flash("Please enter both username and password", "danger")
            return redirect("/login")
        
        user = User.query.filter_by(username=username).first()
        
        if not user:
            flash("User not found. Please check your username or sign up.", "danger")
            return redirect("/login")
        
        if not bcrypt.check_password_hash(user.password, password):
            flash("Wrong password. Please try again.", "danger")
            return redirect("/login")
        
        login_user(user, remember=request.form.get("remember_me", False))
        
        flash(f"Welcome back, {user.username}!", "success")
        return redirect("/")
    
    return render_template("login.html")


# =====================================================
# LOGOUT
# =====================================================

@app.route("/logout")
@login_required
def logout():
    """User logout"""
    logout_user()
    flash("You have been logged out successfully.", "info")
    return redirect("/login")


# =====================================================
# DASHBOARD
# =====================================================

@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    """Main dashboard with todo list"""
    
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        desc = request.form.get("desc", "").strip()
        
        if not title:
            flash("Task title is required", "danger")
            return redirect("/")
        
        todo = Todo(
            title=title,
            desc=desc,
            user_id=current_user.id
        )
        
        try:
            db.session.add(todo)
            db.session.commit()
            
            invalidate_cache(current_user.id)
            publish_event("todo_created")
            
            if task_queue:
                task_queue.enqueue(log_event, f"todo created by user {current_user.id}")
            
            flash("Task added successfully!", "success")
        except Exception as e:
            db.session.rollback()
            flash(f"Error adding task: {str(e)}", "danger")
        
        return redirect("/")
    
    # GET request - display todos
    search = request.args.get("search", "").strip()
    
    query = Todo.query.filter_by(user_id=current_user.id)
    
    if search:
        query = query.filter(Todo.title.contains(search) | Todo.desc.contains(search))
        flash(f"Showing results for: {search}", "info")
    
    todos = query.order_by(Todo.date_c.desc()).all()
    
    return render_template("index.html", todos=todos)


# =====================================================
# UPDATE TODO (EDIT ROUTE)
# =====================================================

@app.route("/update/<int:id>", methods=["GET", "POST"])
@login_required
def update(id):
    """
    Update a todo item
    """
    todo = Todo.query.filter_by(
        id=id,
        user_id=current_user.id
    ).first_or_404()
    
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        desc = request.form.get("desc", "").strip()
        
        if not title:
            flash("Title is required!", "danger")
            return redirect(f"/update/{id}")
        
        todo.title = title
        todo.desc = desc
        
        try:
            db.session.commit()
            
            invalidate_cache(current_user.id)
            publish_event("todo_updated")
            
            flash("Task updated successfully!", "success")
            return redirect("/")
        except Exception as e:
            db.session.rollback()
            flash(f"Error updating task: {str(e)}", "danger")
            return redirect(f"/update/{id}")
    
    return render_template("update.html", todo=todo)


# =====================================================
# DELETE TODO
# =====================================================

@app.route("/delete/<int:id>")
@login_required
def delete(id):
    """Delete a todo item"""
    
    todo = Todo.query.filter_by(
        id=id,
        user_id=current_user.id
    ).first_or_404()
    
    try:
        db.session.delete(todo)
        db.session.commit()
        
        invalidate_cache(current_user.id)
        publish_event("todo_deleted")
        
        flash("Task deleted successfully!", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting task: {str(e)}", "danger")
    
    return redirect("/")


# =====================================================
# TOGGLE TODO COMPLETION
# =====================================================

@app.route("/toggle/<int:id>")
@login_required
def toggle(id):
    """Toggle todo completion status"""
    
    todo = Todo.query.filter_by(
        id=id,
        user_id=current_user.id
    ).first_or_404()
    
    todo.completed = not todo.completed
    
    try:
        db.session.commit()
        
        invalidate_cache(current_user.id)
        publish_event("todo_updated")
        
        status = "completed" if todo.completed else "marked as pending"
        flash(f"Task {status} successfully!", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error toggling task: {str(e)}", "danger")
    
    return redirect("/")


# =====================================================
# API DOCS PAGE
# =====================================================

@app.route("/api-docs")
def api_docs():
    """API documentation page"""
    return render_template("api_docs.html")


# =====================================================
# ERROR HANDLERS
# =====================================================

@app.errorhandler(404)
def not_found_error(error):
    """Handle 404 errors"""
    return render_template("404.html"), 404


@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors"""
    db.session.rollback()
    return render_template("500.html"), 500


@app.errorhandler(429)
def rate_limit_error(error):
    """Handle rate limit errors"""
    flash("Too many requests. Please try again later.", "danger")
    return redirect("/")


# =====================================================
# CONTEXT PROCESSOR
# =====================================================

@app.context_processor
def utility_processor():
    """Add utility functions to templates"""
    return {
        "now": datetime.now(),
        "app_name": "Todo SaaS"
    }


# =====================================================
# CREATE DATABASE TABLES
# =====================================================

with app.app_context():
    try:
        db.create_all()
        print("✅ Database tables created successfully")
    except Exception as e:
        print(f"⚠️ Database creation error: {e}")


# =====================================================
# START SERVER
# =====================================================

if __name__ == "__main__":
    from datetime import datetime
    
    start_event_listener()
    
    print("\n" + "="*50)
    print("🚀 Todo SaaS Application Started")
    print("="*50)
    print(f"📍 URL: http://localhost:5000")
    print(f"📝 Signup: http://localhost:5000/signup")
    print(f"🔐 Login: http://localhost:5000/login")
    print(f"📚 API Docs: http://localhost:5000/api-docs")
    print("="*50 + "\n")
    
    socketio.run(
        app,
        host="0.0.0.0",
        port=5000,
        debug=True,
        allow_unsafe_werkzeug=True
    )
