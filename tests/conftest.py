import pytest
from app import app
from data import db
from flask_bcrypt import Bcrypt

bcrypt = Bcrypt()
from model import User

@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["JWT_SECRET_KEY"] = "test-jwt-secret"
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"

    with app.test_client() as client:
        with app.app_context():
            db.drop_all()
            db.create_all()

            hashed = bcrypt.generate_password_hash("testpass").decode("utf-8")
            user = User(username="testuser", password=hashed)
            db.session.add(user)
            db.session.commit()

        yield client

        with app.app_context():
            db.session.remove()
            db.drop_all()

