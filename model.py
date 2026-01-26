from data import db
from datetime import datetime
from flask_login import UserMixin
from flask_bcrypt import Bcrypt

bcrypt = Bcrypt()

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password = bcrypt.generate_password_hash(password).decode("utf-8")

    def check_password(self, password):
        return bcrypt.check_password_hash(self.password, password)




class Todo(db.Model):
    
    id=db.Column(db.Integer,primary_key=True)
    title=db.Column(db.String(200),nullable=False)
    desc=db.Column(db.String(500),nullable=False)
    completed=db.Column(db.Boolean,default=False)
    date_c=db.Column(db.DateTime,default=datetime.utcnow)
    updated_at=db.Column(db.DateTime,onupdate=datetime.utcnow)
    user_id=db.Column(db.Integer,db.ForeignKey("user.id"))
      
    def __repr__(self)->str:
        return f"{self.id} - {self.title}"
