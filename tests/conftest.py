import pytest
from app import app
from data import db
from model import User
from flask_bcrypt import Bcrypt

bcrypt = Bcrypt()

@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["JWT_SECRET_KEY"] = "test-jwt-secret"

    with app.app_context():
        db.create_all()

        # create test user
        password = bcrypt.generate_password_hash("testpass").decode("utf-8")
        user = User(username="testuser", password=password)
        db.session.add(user)
        db.session.commit()

        yield app.test_client()

        db.session.remove()
        db.drop_all()
