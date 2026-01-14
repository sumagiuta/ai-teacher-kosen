from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

# ユーザー
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    role = db.Column(db.String(10), nullable=False) # 'teacher' or 'student'
    google_credentials = db.Column(db.Text, nullable=True)

# 課題（先生が作成） または 自主学習（生徒が作成）
class Assignment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    # created_by が先生なら「課題」、生徒なら「自主学習」と判断する
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # リレーション
    creator = db.relationship('User', backref='assignments')

# レポート（自由学習ツール用として残す）
class Report(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    content = db.Column(db.Text, nullable=False)
    feedback = db.Column(db.Text)
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)

# 授業スライドの保存用
class LessonLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey('assignment.id'))
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    slides_content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# ★新規追加: 確認テストの記録用
class QuizLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey('assignment.id'))
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    
    questions = db.Column(db.Text, nullable=False)      # AIが作った問題文(JSON文字列)
    student_answers = db.Column(db.Text, nullable=True) # 生徒の回答(JSON文字列)
    grading_result = db.Column(db.Text, nullable=True)  # AIの採点結果
    score = db.Column(db.Integer, default=0)            # 点数（100点満点など）
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # リレーション
    student = db.relationship('User', backref='quiz_logs')
    assignment = db.relationship('Assignment', backref='quiz_logs')

# 自由学習ツール・画像採点の履歴
class GradingLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    mode = db.Column(db.String(20)) 
    input_text = db.Column(db.Text, nullable=True)
    input_image = db.Column(db.Text, nullable=True)
    feedback_content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    student = db.relationship('User', backref='grading_logs', lazy=True)