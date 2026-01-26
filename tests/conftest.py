import pytest
from app import app, db
from app.models import User

@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["JWT_SECRET_KEY"] = "test-jwt-secret"
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"

    with app.test_client() as client:
        with app.app_context():
            db.create_all()

            user = User(username="testuser")
            user.set_password("testpass")
            db.session.add(user)
            db.session.commit()

        yield client
