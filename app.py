import os
import json
import base64
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
import google.generativeai as genai
from dotenv import load_dotenv
from models import db, User, Assignment, Report, LessonLog, GradingLog, QuizLog

# Google連携用ライブラリ
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

load_dotenv()

app = Flask(__name__)
app.secret_key = 'kosen_pbl_secret_key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///kosen.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

db.init_app(app)

try:
    genai.configure(api_key=os.environ["GOOGLE_API_KEY"])
except KeyError:
    print("エラー: GOOGLE_API_KEYが設定されていません。")

model_pro = genai.GenerativeModel('gemini-3-pro-preview')
model_flash = genai.GenerativeModel('gemini-3-flash-preview')

CLIENT_SECRETS_FILE = "client_secret.json"
SCOPES = [
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
    'https://www.googleapis.com/auth/classroom.courses.readonly',
    'https://www.googleapis.com/auth/classroom.coursework.me.readonly',
    'https://www.googleapis.com/auth/calendar'
]

with app.app_context():
    db.create_all()
    if not User.query.filter_by(username='sensei').first():
        db.session.add(User(username='sensei', role='teacher'))
        db.session.add(User(username='gakusei', role='student'))
        db.session.commit()

# --- ルート処理 ---

@app.route('/')
def index():
    if 'user_id' not in session: return redirect(url_for('login'))
    user = User.query.get(session['user_id'])
    if not user:
        session.clear()
        return redirect(url_for('login'))
    
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

@app.route('/google_login')
def google_login():
    if not os.path.exists(CLIENT_SECRETS_FILE):
        return "エラー: client_secret.json が見つかりません。", 500
    flow = Flow.from_client_secrets_file(CLIENT_SECRETS_FILE, scopes=SCOPES, redirect_uri=url_for('oauth2callback', _external=True))
    authorization_url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true', prompt='consent')
    session['state'] = state
    return redirect(authorization_url)

@app.route('/oauth2callback')
def oauth2callback():
    state = session.get('state')
    if not state: return redirect(url_for('login'))
    try:
        flow = Flow.from_client_secrets_file(CLIENT_SECRETS_FILE, scopes=SCOPES, state=state, redirect_uri=url_for('oauth2callback', _external=True))
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        user_info = build('oauth2', 'v2', credentials=creds).userinfo().get().execute()
        email = user_info.get('email')
        user = User.query.filter_by(username=email).first()
        if not user:
            user = User(username=email, role='student')
            db.session.add(user)
        user.google_credentials = creds.to_json()
        db.session.commit()
        session['user_id'] = user.id
        session['role'] = user.role
        return redirect(url_for('index'))
    except Exception as e:
        return f"認証エラー: {str(e)}", 500

@app.route('/sync_calendar')
def sync_calendar():
    if 'user_id' not in session: return redirect(url_for('login'))
    user = User.query.get(session['user_id'])
    if not user.google_credentials: return "Google連携されていません。"
    try:
        creds = Credentials.from_authorized_user_info(json.loads(user.google_credentials), SCOPES)
        if creds.expired and creds.refresh_token: creds.refresh(Request())
        
        classroom = build('classroom', 'v1', credentials=creds)
        calendar = build('calendar', 'v3', credentials=creds)
        courses = classroom.courses().list(studentId='me', courseStates=['ACTIVE']).execute().get('courses', [])
        
        sync_count = 0
        cutoff = date.today() - timedelta(days=7)
        
        for c in courses:
            works = classroom.courses().courseWork().list(courseId=c['id']).execute().get('courseWork', [])
            for w in works:
                if 'dueDate' in w:
                    d = w['dueDate']
                    if not (d.get('year') and d.get('month') and d.get('day')): continue
                    if date(d['year'], d['month'], d['day']) < cutoff: continue
                    
                    dt = f"{d['year']}-{d['month']:02d}-{d['day']:02d}"
                    body = {
                        'summary': f"【課題】{w['title']} ({c['name']})",
                        'description': f"リンク: {w['alternateLink']}\n{w.get('description','')}",
                        'start': {'date': dt}, 'end': {'date': dt}
                    }
                    try:
                        calendar.events().insert(calendarId='primary', body=body).execute()
                        sync_count += 1
                    except: pass
        flash(f"{sync_count}件同期しました", "success")
        return redirect(url_for('student_dashboard'))
    except Exception as e: return f"同期エラー: {e}", 500

@app.route('/teacher')
def teacher_dashboard():
    if session.get('role') != 'teacher': return redirect(url_for('index'))
    my_assignments = Assignment.query.filter_by(created_by=session['user_id']).order_by(Assignment.created_at.desc()).all()
    logs = GradingLog.query.order_by(GradingLog.created_at.desc()).limit(20).all()
    return render_template('teacher_dashboard.html', assignments=my_assignments, logs=logs)

@app.route('/create_assignment', methods=['POST'])
def create_assignment():
    if session.get('role') != 'teacher': return redirect(url_for('index'))
    title = request.form['title']
    description = request.form['description']
    new_assignment = Assignment(title=title, description=description, created_by=session['user_id'])
    db.session.add(new_assignment)
    db.session.commit()
    return redirect(url_for('teacher_dashboard'))

@app.route('/student')
def student_dashboard():
    if session.get('role') != 'student': return redirect(url_for('index'))
    user = User.query.get(session['user_id'])
    all_assignments = Assignment.query.order_by(Assignment.created_at.desc()).all()
    teacher_assignments = [a for a in all_assignments if a.creator.role == 'teacher']
    my_lessons = [a for a in all_assignments if a.created_by == user.id]
    is_google_linked = True if user.google_credentials else False
    return render_template('student_dashboard.html', teacher_assignments=teacher_assignments, my_lessons=my_lessons, is_google_linked=is_google_linked)

@app.route('/create_self_study', methods=['POST'])
def create_self_study():
    if session.get('role') != 'student': return redirect(url_for('index'))
    title = request.form['title']
    description = request.form['description']
    new_lesson = Assignment(title=title, description=description, created_by=session['user_id'])
    db.session.add(new_lesson)
    db.session.commit()
    return redirect(url_for('student_dashboard'))

@app.route('/lesson_page/<int:assignment_id>')
def lesson_page(assignment_id):
    assignment = Assignment.query.get_or_404(assignment_id)
    if session.get('role') == 'student':
        if assignment.creator.role != 'teacher' and assignment.created_by != session['user_id']:
            return redirect(url_for('student_dashboard'))
    lesson_log = LessonLog.query.filter_by(assignment_id=assignment_id, student_id=session['user_id']).first()
    saved_slides = lesson_log.slides_content if lesson_log else None
    return render_template('lesson.html', assignment=assignment, saved_slides=saved_slides)

@app.route('/api/generate_lesson', methods=['POST'])
def generate_lesson_api():
    data = request.json
    assignment_id = data.get('assignment_id')
    user_id = session.get('user_id')
    existing = LessonLog.query.filter_by(assignment_id=assignment_id, student_id=user_id).first()
    if existing: return jsonify({"slides": existing.slides_content})
    assignment = Assignment.query.get(assignment_id)
    prompt = f"""
    あなたは高専の教員です。以下のテーマについて、高専生向けの講義スライドをMarkdown形式で**5枚**作成してください。
    テーマ: {assignment.title}
    詳細指示: {assignment.description}
    要件: Markdown形式, 区切りは `---SLIDE_BREAK---`, 数式はLaTeX形式($$ ... $$)
    """
    try:
        res = model_pro.generate_content(prompt)
        text = res.text
        db.session.add(LessonLog(assignment_id=assignment_id, student_id=user_id, slides_content=text))
        db.session.commit()
        return jsonify({"slides": text})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/ask_teacher', methods=['POST'])
def ask_teacher_api():
    data = request.json
    prompt = f"高専の教員として回答して。\n文脈: {data.get('context','')}\n質問: {data.get('question')}\n数式を用いて解説して。"
    try:
        return jsonify({"answer": model_flash.generate_content(prompt).text})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/quiz_page/<int:assignment_id>')
def quiz_page(assignment_id):
    if session.get('role') != 'student': return redirect(url_for('index'))
    assignment = Assignment.query.get_or_404(assignment_id)
    lesson_log = LessonLog.query.filter_by(assignment_id=assignment_id, student_id=session['user_id']).first()
    slides_content = lesson_log.slides_content if lesson_log else "（授業スライドがまだ生成されていません）"
    return render_template('quiz.html', assignment=assignment, slides_content=slides_content)

@app.route('/api/generate_quiz', methods=['POST'])
def generate_quiz_api():
    data = request.json
    assignment_id = data.get('assignment_id')
    user_id = session['user_id']

    existing_quiz = QuizLog.query.filter_by(assignment_id=assignment_id, student_id=user_id).first()
    if existing_quiz:
        return jsonify({
            "quiz_id": existing_quiz.id, 
            "questions": json.loads(existing_quiz.questions),
            "student_answers": json.loads(existing_quiz.student_answers) if existing_quiz.student_answers else None,
            "grading_result": existing_quiz.grading_result
        })

    lesson_log = LessonLog.query.filter_by(assignment_id=assignment_id, student_id=user_id).first()
    if not lesson_log: return jsonify({"error": "先に授業を受けてください"}), 400
    
    prompt = f"""
    以下の講義スライドに基づいて、学生の理解度を確認するための**記述式問題を3問**作成してください。
    【スライド内容】{lesson_log.slides_content}
    【出力】純粋なJSON配列: [{{"q_id": 1, "question": "..."}}, ...]
    """
    try:
        res = model_pro.generate_content(prompt)
        text = res.text.replace('```json','').replace('```','').strip()
        new_quiz = QuizLog(assignment_id=assignment_id, student_id=user_id, questions=text)
        db.session.add(new_quiz)
        db.session.commit()
        return jsonify({"quiz_id": new_quiz.id, "questions": json.loads(text)})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/grade_quiz', methods=['POST'])
def grade_quiz_api():
    data = request.json
    quiz_log = QuizLog.query.get(data.get('quiz_id'))
    answers = data.get('answers')
    questions = json.loads(quiz_log.questions)
    
    prompt = "高専の教員として回答を採点・解説してください。\n\n"
    for q in questions:
        prompt += f"問{q['q_id']}: {q['question']}\n回答: {answers.get(str(q['q_id']), '未回答')}\n\n"
    
    try:
        res = model_pro.generate_content(prompt)
        quiz_log.student_answers = json.dumps(answers)
        quiz_log.grading_result = res.text
        db.session.commit()
        
        db.session.add(GradingLog(student_id=session['user_id'], mode='quiz', input_text=f"確認テスト: {quiz_log.assignment.title}", feedback_content=res.text))
        db.session.commit()
        return jsonify({"result": res.text})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/tools')
def tools_page(): return render_template('free_tools.html')

# --- ★修正: 模範解答（複数）と、解答なし時の対応 ---
@app.route('/api/general_grading', methods=['POST'])
def general_grading_api():
    data = request.json
    mode = data.get('mode')
    user_id = session.get('user_id')
    
    contents = []
    system_prompt = "あなたは高専の教員です。提出された画像やテキストを見て採点を行ってください。"
    system_prompt += "【重要】画像の中に**複数の問題**が含まれている場合は、問1, 問2...のように**問題を区別して**それぞれ採点・解説してください。"
    
    def decode_image(b64): 
        try: return {"mime_type": "image/jpeg", "data": base64.b64decode(b64.split(",",1)[1] if "," in b64 else b64)}
        except: return None

    log_input_text = ""
    log_input_image = None

    if mode == 'report':
        contents = [system_prompt, f"レポート本文:\n{data.get('text_content', '')}"]
        log_input_text = data.get('text_content', '')
        
    elif mode == 'problem':
        contents.append(system_prompt)
        
        # 模範解答テキスト
        model_answer_text = data.get('model_answer', '')
        if model_answer_text: contents.append(f"【模範解答 (テキスト)】\n{model_answer_text}")
        
        # ★模範解答画像 (複数対応)
        model_answer_imgs = data.get('model_answer_images', [])
        if model_answer_imgs:
            contents.append("【模範解答 (画像一覧)】")
            for i, img_str in enumerate(model_answer_imgs):
                decoded = decode_image(img_str)
                if decoded: 
                    contents.append(f"--- 模範解答画像 {i+1} ---")
                    contents.append(decoded)

        # ★模範解答が一切ない場合のメッセージ
        if not model_answer_text and not model_answer_imgs:
            contents.append("※模範解答は提供されていません。あなたの専門知識に基づいて、正誤判定と解説を行ってください。")
        else:
            contents.append("\n上記「模範解答」を正解基準として採点してください。\n")
        
        # テキスト情報
        text_content = data.get('text_content', '')
        if text_content: 
            contents.append(f"【補足情報・テキスト解答】\n{text_content}")
            log_input_text = text_content
        
        # 学生の解答画像 (複数)
        images = data.get('images', [])
        if images:
            contents.append("【学生の解答 (画像一覧)】")
            log_input_image = images[0]
            for i, img_str in enumerate(images):
                decoded = decode_image(img_str)
                if decoded: 
                    contents.append(f"--- 学生画像 {i+1} ---")
                    contents.append(decoded)

    try:
        response = model_pro.generate_content(contents)
        result_text = response.text
        db.session.add(GradingLog(
            student_id=user_id, mode=mode, input_text=log_input_text, 
            input_image=log_input_image, feedback_content=result_text
        ))
        db.session.commit()
        return jsonify({"result": result_text})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/switch_role')
def switch_role():
    if 'user_id' not in session: return redirect(url_for('login'))
    cur = User.query.get(session['user_id'])
    target_name = 'gakusei' if cur.role == 'teacher' else 'sensei'
    target = User.query.filter_by(username=target_name).first()
    if not target:
        target = User(username=target_name, role='student' if target_name=='gakusei' else 'teacher')
        db.session.add(target)
        db.session.commit()
    session['user_id'] = target.id
    session['role'] = target.role
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True)