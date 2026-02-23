import os
import uuid
import secrets
import string
import json
import io
import requests
from dotenv import load_dotenv
from datetime import datetime
from PIL import Image

from flask import Flask, render_template, request, jsonify, url_for, redirect, Response, stream_with_context, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin, LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from authlib.integrations.flask_client import OAuth

load_dotenv()

app = Flask(__name__)

is_prod = os.environ.get("isProd", "false").lower() == "true"
app.config['IS_PROD'] = is_prod

if is_prod:
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get("DB_URL")
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///pedagogico.db'

app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024
app.config['SECRET_KEY'] = os.environ.get("SECRET_KEY") or secrets.token_hex(16)
app.config['GOOGLE_CLIENT_ID'] = os.environ.get("GOOGLE_CLIENT_ID")
app.config['GOOGLE_CLIENT_SECRET'] = os.environ.get("GOOGLE_CLIENT_SECRET")

db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'host_login'

oauth = OAuth(app)
google = oauth.register(
    name='google',
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={
        'scope': 'openid email profile https://www.googleapis.com/auth/drive.file'
    },
    authorize_params={
        'access_type': 'offline',
        'prompt': 'consent'
    }
)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    rooms_owned = db.relationship('Room', backref='owner', lazy=True)

    google_id = db.Column(db.String(100), unique=True, nullable=True)
    email = db.Column(db.String(100), unique=True, nullable=True)
    name = db.Column(db.String(100), nullable=True)
    avatar = db.Column(db.String(200), nullable=True)
    tokens = db.Column(db.Text, nullable=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class StorageAccount(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(100), unique=True, nullable=False)
    google_id = db.Column(db.String(100), unique=True, nullable=False)
    tokens = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, default=True)


class Room(db.Model):
    hash_id = db.Column(db.String(36), primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    institution = db.Column(db.String(100), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    code = db.Column(db.String(6), unique=True, nullable=True)
    posts = db.relationship('Post', backref='room', lazy=True, cascade="all, delete")


class RoomMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    room_hash = db.Column(db.String(36), db.ForeignKey('room.hash_id'), nullable=False)
    guest_name = db.Column(db.String(100), nullable=True)
    avatar = db.Column(db.String(200), nullable=True)
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)


class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    room_hash = db.Column(db.String(36), db.ForeignKey('room.hash_id'), nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    author = db.relationship('User', backref=db.backref('user_posts', lazy=True))
    guest_name = db.Column(db.String(100), nullable=True)
    image_url = db.Column(db.String(500), nullable=False)
    drive_file_id = db.Column(db.String(100), nullable=True)
    storage_account_id = db.Column(db.Integer, db.ForeignKey('storage_account.id'), nullable=True)
    storage_account = db.relationship('StorageAccount')
    caption = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    likes = db.relationship('PostLike', backref='post', lazy='dynamic', cascade="all, delete-orphan")


class PostLike(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    guest_id = db.Column(db.String(36), nullable=True)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def get_google_credentials(account_obj):
    if not account_obj or not account_obj.tokens:
        return None

    token_data = json.loads(account_obj.tokens)
    return Credentials(
        token=token_data['access_token'],
        refresh_token=token_data.get('refresh_token'),
        token_uri='https://oauth2.googleapis.com/token',
        client_id=app.config['GOOGLE_CLIENT_ID'],
        client_secret=app.config['GOOGLE_CLIENT_SECRET'],
        scopes=['https://www.googleapis.com/auth/drive.file']
    )


def get_drive_service(account_obj):
    creds = get_google_credentials(account_obj)
    return build('drive', 'v3', credentials=creds)


def compress_image_if_needed(file_storage):
    file_storage.seek(0, os.SEEK_END)
    size = file_storage.tell()
    file_storage.seek(0)

    if size <= 5 * 1024 * 1024:
        return file_storage, file_storage.content_type

    img = Image.open(file_storage)
    output = io.BytesIO()

    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    img.thumbnail((1920, 1920))
    img.save(output, format="JPEG", quality=70, optimize=True)
    output.seek(0)

    return output, 'image/jpeg'


def upload_to_drive(file_obj, filename, mime_type):
    account = StorageAccount.query.filter_by(is_active=True).first()
    if not account:
        if not app.config.get('IS_PROD'):
            mock_id = f"mock_{uuid.uuid4().hex}"
            return mock_id, f"/cdn/{mock_id}", None
        raise Exception("Nenhuma conta de armazenamento configurada.")

    service = get_drive_service(account)

    query = "mimeType='application/vnd.google-apps.folder' and name='pretitudes' and trashed=false"
    results = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    items = results.get('files', [])

    if not items:
        file_metadata = {'name': 'pretitudes', 'mimeType': 'application/vnd.google-apps.folder'}
        folder = service.files().create(body=file_metadata, fields='id').execute()
        folder_id = folder.get('id')
    else:
        folder_id = items[0]['id']

    file_metadata = {'name': filename, 'parents': [folder_id]}
    media = MediaIoBaseUpload(file_obj, mimetype=mime_type, resumable=True)
    file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id'
    ).execute()

    file_id = file.get('id')

    try:
        permission = {'type': 'anyone', 'role': 'reader', 'allowFileDiscovery': False}
        service.permissions().create(fileId=file_id, body=permission).execute()
    except Exception:
        pass

    return file_id, f"/cdn/{file_id}", account.id


def generate_room_code():
    return ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))


@app.route('/')
def index():
    return render_template('enter_code.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('profile'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        if not username or not password:
            return render_template('register.html', error="Preencha todos os campos")

        if User.query.filter_by(username=username).first():
            return render_template('register.html', error="Usuário já existe")

        new_user = User(username=username)
        new_user.set_password(password)

        if User.query.filter_by(is_admin=True).first() is None:
            new_user.is_admin = True

        db.session.add(new_user)
        db.session.commit()

        login_user(new_user)
        return redirect(url_for('setup_profile'))

    return render_template('register.html')


@app.route('/setup_profile', methods=['GET', 'POST'])
@login_required
def setup_profile():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        file = request.files.get('avatar')

        if not name:
            return render_template('setup_profile.html', error="Nome é obrigatório")

        current_user.name = name

        if file:
            processed_file, mime_type = compress_image_if_needed(file)
            filename = secure_filename(f"avatar_{current_user.id}_{uuid.uuid4().hex[:8]}")
            if mime_type == 'image/jpeg' and not filename.lower().endswith(('.jpg', '.jpeg')):
                filename = f"{filename}.jpg"

            try:
                file_id, image_url, _ = upload_to_drive(processed_file, filename, mime_type)
                current_user.avatar = url_for('cdn_proxy', file_id=file_id)
            except Exception as e:
                return render_template('setup_profile.html', error=str(e))

        db.session.commit()
        return redirect(url_for('profile'))

    return render_template('setup_profile.html')


@app.route('/host', methods=['GET', 'POST'])
def host_login():
    if current_user.is_authenticated:
        return redirect(url_for('profile'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user, remember=True)
            return redirect(url_for('profile'))

        return render_template('login_simple.html', error="Credenciais inválidas")

    return render_template('login_simple.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))


@app.route('/admin/storage')
@login_required
def admin_storage():
    if not current_user.is_admin:
         return redirect(url_for('profile'))
    accounts = StorageAccount.query.all()
    return render_template('admin_storage.html', accounts=accounts)


@app.route('/admin/connect_google')
@login_required
def admin_connect_google():
    if not current_user.is_admin:
         return redirect(url_for('profile'))
    redirect_uri = url_for('admin_auth_callback', _external=True)
    return google.authorize_redirect(redirect_uri)


@app.route('/admin/callback')
@login_required
def admin_auth_callback():
    if not current_user.is_admin:
        return redirect(url_for('profile'))

    token = google.authorize_access_token()

    if 'refresh_token' not in token:
        print("❌ ERRO NO CALLBACK: Refresh Token ausente.")
        error_html = """
        <!DOCTYPE html>
        <html lang="pt-BR">
        <head>
            <meta charset="UTF-8">
            <title>Erro de Permissão</title>
            <meta http-equiv="refresh" content="6;url=/admin/storage">
            <style>
                body { 
                    background-color: #ffcccc; 
                    display: flex; 
                    flex-direction: column; 
                    justify-content: center; 
                    align-items: center; 
                    height: 100vh; 
                    margin: 0; 
                    font-family: Arial, sans-serif; 
                }
                h1 { 
                    font-size: 5rem; 
                    color: #cc0000; 
                    text-align: center; 
                    margin: 0 20px;
                    text-transform: uppercase;
                }
                p { 
                    font-size: 2rem; 
                    color: #333; 
                    text-align: center;
                    margin-top: 20px;
                }
                .dica {
                    font-size: 1.2rem;
                    color: #666;
                    margin-top: 40px;
                    background: #fff;
                    padding: 15px;
                    border-radius: 8px;
                }
            </style>
        </head>
        <body>
            <h1>TOKEN MESTRE NÃO CAPTURADO</h1>
            <p>O Google se recusou a enviar a chave permanente.<br>Voltando em 5 segundos...</p>
            <div class="dica">
                <strong>DICA:</strong> Acesse myaccount.google.com/connections, remova o app e tente de novo.
            </div>
        </body>
        </html>
        """
        return error_html
    user_info = google.get('https://www.googleapis.com/oauth2/v1/userinfo').json()

    email = user_info['email']
    google_id = user_info['id']

    account = StorageAccount.query.filter_by(google_id=google_id).first()
    if not account:
        account = StorageAccount(email=email, google_id=google_id)
        db.session.add(account)

    account.tokens = json.dumps(token)
    account.is_active = True
    db.session.commit()

    return redirect(url_for('admin_storage'))


@app.route('/join_code', methods=['POST'])
def join_by_code():
    code = request.form.get('code', '').upper().strip()
    room = Room.query.filter_by(code=code).first()

    if not room:
        return render_template('enter_code.html', error="Sala não encontrada")

    return redirect(url_for('join_room', room_hash=room.hash_id))


@app.route('/room/<room_hash>/auth', methods=['GET', 'POST'])
def guest_login(room_hash):
    room = db.session.get(Room, room_hash)
    if not room:
        return "Sala não encontrada", 404

    if request.method == 'POST':
        guest_name = request.form.get('guest_name', '').strip()
        file = request.files.get('avatar')

        if not guest_name:
            return render_template('guest_login.html', room=room, error="Nome é obrigatório")

        session[f'guest_room_{room_hash}'] = True
        session[f'guest_name_{room_hash}'] = guest_name
        if 'guest_id' not in session:
            session['guest_id'] = str(uuid.uuid4())

        member = RoomMember.query.filter_by(room_hash=room_hash, guest_name=guest_name).first()
        if not member:
            member = RoomMember(room_hash=room_hash, guest_name=guest_name)
            db.session.add(member)

        if file:
            processed_file, mime_type = compress_image_if_needed(file)
            filename = secure_filename(f"guest_{uuid.uuid4().hex[:8]}")
            if mime_type == 'image/jpeg' and not filename.lower().endswith(('.jpg', '.jpeg')):
                filename = f"{filename}.jpg"

            try:
                file_id, image_url, _ = upload_to_drive(processed_file, filename, mime_type)
                member.avatar = url_for('cdn_proxy', file_id=file_id)
            except Exception as e:
                return render_template('guest_login.html', room=room, error=str(e))

        db.session.commit()

        return redirect(url_for('join_room', room_hash=room_hash))

    return render_template('guest_login.html', room=room)


@app.route('/profile')
@login_required
def profile():
    return render_template('profile.html', user=current_user)


@app.route('/join/<room_hash>')
def join_room(room_hash):
    room = db.session.get(Room, room_hash)
    if not room:
        return "Sala não encontrada", 404

    is_guest = False
    if current_user.is_authenticated:
        member = RoomMember.query.filter_by(user_id=current_user.id, room_hash=room_hash).first()
        if not member:
            member = RoomMember(user_id=current_user.id, room_hash=room_hash)
            db.session.add(member)
            db.session.commit()
    else:
        if not session.get(f'guest_room_{room_hash}'):
            return redirect(url_for('guest_login', room_hash=room_hash))
        is_guest = True

    posts = Post.query.filter_by(room_hash=room_hash).order_by(Post.created_at.desc()).all()

    guest_avatars = {}
    members = RoomMember.query.filter_by(room_hash=room_hash).all()
    for m in members:
        if m.guest_name:
            guest_avatars[m.guest_name] = m.avatar

    current_user_id = current_user.id if current_user.is_authenticated else None
    guest_id = session.get('guest_id')

    for p in posts:
        if current_user_id:
            p.liked_by_me = p.likes.filter_by(user_id=current_user_id).first() is not None
        elif guest_id:
            p.liked_by_me = p.likes.filter_by(guest_id=guest_id).first() is not None
        else:
            p.liked_by_me = False

    return render_template('feed.html', room=room, posts=posts, user=current_user, is_guest=is_guest, guest_avatars=guest_avatars)


@app.route('/api/profile/update', methods=['POST'])
@login_required
def update_profile():
    name = request.form.get('name')
    file = request.files.get('avatar')

    if name:
        current_user.name = name.strip()

    if file:
        processed_file, mime_type = compress_image_if_needed(file)
        filename = secure_filename(f"avatar_{current_user.id}_{uuid.uuid4().hex[:8]}")

        try:
             file_id, image_url, _ = upload_to_drive(processed_file, filename, mime_type)
             current_user.avatar = url_for('cdn_proxy', file_id=file_id)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/create_room', methods=['POST'])
@login_required
def create_room():
    data = request.json
    room_hash = secrets.token_urlsafe(6)

    room_code = generate_room_code()
    while Room.query.filter_by(code=room_code).first():
        room_code = generate_room_code()

    new_room = Room(
        hash_id=room_hash,
        owner_id=current_user.id,
        institution=data.get('institution'),
        name=data.get('room_name'),
        code=room_code
    )
    db.session.add(new_room)
    db.session.commit()
    return jsonify({'redirect_url': url_for('join_room', room_hash=room_hash, _external=True)})


@app.route('/api/post/<room_hash>', methods=['POST'])
def add_post(room_hash):
    if not current_user.is_authenticated and not session.get(f'guest_room_{room_hash}'):
        return jsonify({'error': 'Não autorizado'}), 403

    file = request.files.get('photo')
    caption = request.form.get('caption')

    if not file:
        return jsonify({'error': 'Dados incompletos'}), 400

    processed_file, mime_type = compress_image_if_needed(file)

    filename = secure_filename(f"{uuid.uuid4().hex[:8]}_{file.filename}")
    if mime_type == 'image/jpeg' and not filename.lower().endswith(('.jpg', '.jpeg')):
        filename = f"{filename}.jpg"

    try:
        file_id, image_url, storage_account_id = upload_to_drive(processed_file, filename, mime_type)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    author_id = current_user.id if current_user.is_authenticated else None
    guest_name = None

    if not current_user.is_authenticated:
        guest_name = session.get(f'guest_name_{room_hash}', 'Convidado')

    new_post = Post(
        room_hash=room_hash,
        author_id=author_id,
        guest_name=guest_name,
        image_url=image_url,
        drive_file_id=file_id,
        storage_account_id=storage_account_id,
        caption=caption
    )
    db.session.add(new_post)
    db.session.commit()
    return jsonify({'success': True})


@app.route('/cdn/<file_id>')
def cdn_proxy(file_id):
    if not file_id:
        return "Arquivo não encontrado", 404

    if file_id.startswith("mock_"):
        return redirect(url_for('static', filename='1.webp'))

    post = Post.query.filter_by(drive_file_id=file_id).first()

    account = None
    if post:
        if post.storage_account_id:
            account = db.session.get(StorageAccount, post.storage_account_id)
        elif post.author:
            account = post.author
    else:
        user = User.query.filter(User.avatar.like(f'%{file_id}')).first()
        if user:

            account = StorageAccount.query.filter_by(is_active=True).first()
        else:
            member = RoomMember.query.filter(RoomMember.avatar.like(f'%{file_id}')).first()
            if member:
                 account = StorageAccount.query.filter_by(is_active=True).first()

    if not account:
        return "Arquivo não encontrado", 404

    creds = get_google_credentials(account)
    if not creds:
        return Response("Erro nas credenciais de acesso", status=500)

    def refresh_and_update(creds, account):
        try:
            creds.refresh(GoogleRequest())
            token_data = json.loads(account.tokens)
            token_data['access_token'] = creds.token
            if creds.expiry:
                token_data['expiry'] = creds.expiry.isoformat()
            account.tokens = json.dumps(token_data)
            db.session.commit()
            return True
        except Exception as e:
            print(f"Erro ao renovar token: {e}")
            return False

    if creds.expired and creds.refresh_token:
        if not refresh_and_update(creds, account):
            return Response("Token expirado e falha na renovação", status=403)

    api_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"

    headers = {'Authorization': f'Bearer {creds.token}'}
    req = requests.get(api_url, headers=headers, stream=True)

    if req.status_code == 401 and creds.refresh_token:
        print(f"Token inválido (401) para {file_id}. Tentando renovar...")
        if refresh_and_update(creds, account):
            headers = {'Authorization': f'Bearer {creds.token}'}
            req = requests.get(api_url, headers=headers, stream=True)
        else:
            return Response("Sessão expirada. Faça login novamente no painel admin.", status=403)

    if req.status_code != 200:
        return Response(f"Erro ao carregar imagem: {req.status_code}", status=req.status_code)

    forward_headers = {
        'Content-Type': req.headers.get('Content-Type'),
        'Cache-Control': 'public, max-age=31536000'
    }

    return Response(
        stream_with_context(req.iter_content(chunk_size=4096)),
        headers=forward_headers,
        status=200
    )


@app.route('/api/like/<int:post_id>', methods=['POST'])
def toggle_like(post_id):
    post = db.session.get(Post, post_id)
    if not post: return jsonify({'error': 'Not found'}), 404

    user_id = current_user.id if current_user.is_authenticated else None
    guest_id = session.get('guest_id')

    if not user_id and not guest_id:
        guest_id = str(uuid.uuid4())
        session['guest_id'] = guest_id

    query = PostLike.query.filter_by(post_id=post_id)
    if user_id:
        existing = query.filter_by(user_id=user_id).first()
    else:
        existing = query.filter_by(guest_id=guest_id).first()

    liked = False
    if existing:
        db.session.delete(existing)
    else:
        new_like = PostLike(post_id=post_id, user_id=user_id, guest_id=guest_id)
        db.session.add(new_like)
        liked = True

    db.session.commit()
    return jsonify({'liked': liked, 'count': post.likes.count()})


@app.route('/api/updates/<room_hash>')
def check_updates(room_hash):
    if not current_user.is_authenticated and not session.get(f'guest_room_{room_hash}'):
        return jsonify({'error': 'Não autorizado'}), 403

    last_id = request.args.get('since', 0, type=int)
    new_posts = Post.query.filter(Post.room_hash == room_hash, Post.id > last_id).order_by(Post.created_at.asc()).all()

    data = []
    room = db.session.get(Room, room_hash)

    current_user_id = current_user.id if current_user.is_authenticated else None
    guest_id = session.get('guest_id')

    for post in new_posts:
        can_delete = False
        if current_user.is_authenticated:
            can_delete = (current_user.id == post.author_id or current_user.id == room.owner_id)

        if post.author:
            author_name = post.author.name or post.author.username
            author_avatar = post.author.avatar
            author_initial = author_name[0].upper()
        else:
            author_name = post.guest_name or "Convidado"
            member = RoomMember.query.filter_by(room_hash=room_hash, guest_name=author_name).first()
            author_avatar = member.avatar if member else None
            author_initial = author_name[0].upper()

        liked_by_me = False
        if current_user_id:
            liked_by_me = post.likes.filter_by(user_id=current_user_id).first() is not None
        elif guest_id:
            liked_by_me = post.likes.filter_by(guest_id=guest_id).first() is not None

        img_url = url_for('cdn_proxy', file_id=post.drive_file_id, _external=True)
        data.append({
            'id': post.id,
            'author_name': author_name,
            'author_avatar': author_avatar,
            'author_initial': author_initial,
            'image_url': img_url,
            'caption': post.caption,
            'can_delete': can_delete,
            'likes_count': post.likes.count(),
            'liked_by_me': liked_by_me
        })
    return jsonify(data)


@app.route('/api/delete/<int:post_id>', methods=['DELETE'])
@login_required
def delete_post(post_id):
    post = db.session.get(Post, post_id)
    if not post:
        return jsonify({'error': 'Post não encontrado'}), 404

    if post.author_id == current_user.id or post.room.owner_id == current_user.id:
        db.session.delete(post)
        db.session.commit()
        return jsonify({'success': True})
    return jsonify({'error': 'Não autorizado'}), 403


with app.app_context():
    db.create_all()


if __name__ == '__main__':
    app.run(debug=True, port=5000)