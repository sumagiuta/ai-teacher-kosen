import os
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import google.generativeai as genai
from dotenv import load_dotenv
from models import db, User, Assignment, Report, LessonLog
import base64

# .envファイル読み込み
load_dotenv()

app = Flask(__name__)
app.secret_key = 'kosen_pbl_secret_key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///kosen.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# DB初期化
db.init_app(app)

# APIキー設定
try:
    genai.configure(api_key=os.environ["GOOGLE_API_KEY"])
except KeyError:
    print("エラー: GOOGLE_API_KEYが設定されていません。")

# モデル設定
model_pro = genai.GenerativeModel('gemini-3-pro-preview')
model_flash = genai.GenerativeModel('gemini-3-flash-preview')

# --- DB構築 ---
with app.app_context():
    db.create_all()
    if not User.query.filter_by(username='sensei').first():
        db.session.add(User(username='sensei', role='teacher'))
        db.session.add(User(username='gakusei', role='student'))
        db.session.commit()

# --- ルート処理 ---

@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = User.query.get(session['user_id'])
    if user.role == 'teacher':
        return redirect(url_for('teacher_dashboard'))
    else:
        return redirect(url_for('student_dashboard'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        user = User.query.filter_by(username=username).first()
        if user:
            session['user_id'] = user.id
            session['role'] = user.role
            return redirect(url_for('index'))
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# --- 先生用 ---
@app.route('/teacher')
def teacher_dashboard():
    if session.get('role') != 'teacher': return redirect(url_for('index'))
    assignments = Assignment.query.order_by(Assignment.created_at.desc()).all()
    return render_template('teacher_dashboard.html', assignments=assignments)

@app.route('/create_assignment', methods=['POST'])
def create_assignment():
    if session.get('role') != 'teacher': return redirect(url_for('index'))
    title = request.form['title']
    description = request.form['description']
    new_assignment = Assignment(title=title, description=description, created_by=session['user_id'])
    db.session.add(new_assignment)
    db.session.commit()
    return redirect(url_for('teacher_dashboard'))

# --- 生徒用 ---
@app.route('/student')
def student_dashboard():
    if session.get('role') != 'student': return redirect(url_for('index'))
    assignments = Assignment.query.order_by(Assignment.created_at.desc()).all()
    return render_template('student_dashboard.html', assignments=assignments)

# --- 自由学習ツール ---
@app.route('/tools')
def tools_page():
    if session.get('role') != 'student': return redirect(url_for('index'))
    return render_template('free_tools.html')

# --- 授業 (スライド) 機能 ---
@app.route('/lesson_page/<int:assignment_id>')
def lesson_page(assignment_id):
    if session.get('role') != 'student': return redirect(url_for('index'))
    assignment = Assignment.query.get_or_404(assignment_id)
    
    lesson_log = LessonLog.query.filter_by(
        assignment_id=assignment_id, 
        student_id=session['user_id']
    ).first()
    
    saved_slides = lesson_log.slides_content if lesson_log else None
    
    return render_template('lesson.html', assignment=assignment, saved_slides=saved_slides)

@app.route('/api/generate_lesson', methods=['POST'])
def generate_lesson_api():
    data = request.json
    assignment_id = data.get('assignment_id')
    user_id = session.get('user_id')
    
    existing_log = LessonLog.query.filter_by(assignment_id=assignment_id, student_id=user_id).first()
    if existing_log:
        return jsonify({"slides": existing_log.slides_content})

    assignment = Assignment.query.get(assignment_id)
    
    prompt = f"""
    あなたは高専（高等専門学校）の専門科目の教員です。
    以下のテーマについて、高専生向けの講義スライドをMarkdown形式で**5枚**作成してください。

    テーマ: {assignment.title}
    詳細指示: {assignment.description}

    【要件】
    1. Markdown形式で記述してください。
    2. スライド区切りは `---SLIDE_BREAK---` のみ。
    3. 数式は LaTeX形式 ($$ ... $$) を使用。
    4. 専門的かつ論理的に。
    """

    try:
        response = model_pro.generate_content(prompt)
        text = response.text if response.parts else "生成失敗"
        
        new_log = LessonLog(assignment_id=assignment_id, student_id=user_id, slides_content=text)
        db.session.add(new_log)
        db.session.commit()
        
        return jsonify({"slides": text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/ask_teacher', methods=['POST'])
def ask_teacher_api():
    data = request.json
    prompt = f"""
    あなたは高専の教員です。
    スライド内容: {data.get('context', '')}
    質問: {data.get('question')}
    論理的に数式を用いて解説してください。
    """
    try:
        response = model_flash.generate_content(prompt)
        return jsonify({"answer": response.text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- 課題レポート機能 ---
@app.route('/report_page/<int:assignment_id>')
def report_page(assignment_id):
    if session.get('role') != 'student': return redirect(url_for('index'))
    assignment = Assignment.query.get_or_404(assignment_id)
    return render_template('report.html', assignment=assignment)

@app.route('/api/check_report', methods=['POST'])
def check_report_api():
    data = request.json
    prompt = f"""
    高専の教員として以下のレポートを採点してください。
    課題: {data.get('assignment_title', '自由課題')}
    レポート:
    {data.get('content')}
    
    評価項目: 論理性, 専門性, 工学的視点
    出力: 評価, 良かった点, 改善点, 修正案
    """
    try:
        response = model_pro.generate_content(prompt)
        return jsonify({"feedback": response.text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- 汎用ツールAPI (レポート添削・問題採点) ---
@app.route('/api/general_grading', methods=['POST'])
def general_grading_api():
    data = request.json
    mode = data.get('mode') # 'report' or 'problem'
    
    contents = []
    system_prompt = "あなたは高専の教員です。"
    
    # Base64画像デコード用ヘルパー
    def decode_image(base64_string):
        if not base64_string: return None
        header, encoded = base64_string.split(",", 1)
        image_bytes = base64.b64decode(encoded)
        mime_type = header.split(";")[0].split(":")[1]
        return {"mime_type": mime_type, "data": image_bytes}

    if mode == 'report':
        system_prompt += "提出されたあらゆる分野のレポートに対し、専門的な視点（論理構成、参考文献の扱い、技術的正確さ）で添削を行ってください。"
        contents.append(system_prompt)
        contents.append(f"レポート本文:\n{data.get('text_content')}")
        
    elif mode == 'problem':
        system_prompt += "学生が解いた問題を採点してください。途中式の論理展開も確認し、間違っている場合はどこで間違えたかを指摘してください。"
        contents.append(system_prompt)

        # 1. 模範解答（テキスト）
        model_answer_text = data.get('model_answer', '')
        if model_answer_text:
            contents.append(f"【模範解答 (テキスト)】\n{model_answer_text}")

        # 2. 模範解答（画像）
        model_answer_img = data.get('model_answer_image')
        if model_answer_img:
            contents.append("【模範解答 (画像)】")
            contents.append(decode_image(model_answer_img))

        contents.append("\n上記「模範解答」を正解基準として、以下の「学生の解答」を採点してください。\n")

        # 3. 補足テキスト（問題文など）
        text_content = data.get('text_content', '')
        if text_content:
            contents.append(f"【補足情報/問題文】\n{text_content}")

        # 4. 学生の解答（画像）
        images = data.get('images', [])
        if images:
            contents.append("【学生の解答 (画像)】")
            for img_str in images:
                decoded = decode_image(img_str)
                if decoded: contents.append(decoded)

    try:
        response = model_pro.generate_content(contents)
        return jsonify({"result": response.text})
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)