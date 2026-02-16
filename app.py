import os
import uuid
import secrets
import json
import io
import requests
from dotenv import load_dotenv
from datetime import datetime
from PIL import Image

from flask import Flask, render_template, request, jsonify, url_for, redirect, Response, stream_with_context
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin, LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.utils import secure_filename

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from authlib.integrations.flask_client import OAuth

load_dotenv()

app = Flask(__name__)

is_prod = os.environ.get("isProd", "false").lower() == "true"
if is_prod:
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get("DB_URL")
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///pedagogico.db'

app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024
app.config['SECRET_KEY'] = os.environ.get("SECRET_KEY")
app.config['GOOGLE_CLIENT_ID'] = os.environ.get("GOOGLE_CLIENT_ID")
app.config['GOOGLE_CLIENT_SECRET'] = os.environ.get("GOOGLE_CLIENT_SECRET")

db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'index'

oauth = OAuth(app)
google = oauth.register(
    name='google',
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={
        'scope': 'openid email profile https://www.googleapis.com/auth/drive.file'
    }
)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def get_google_credentials(user):
    token_data = json.loads(user.tokens)
    return Credentials(
        token=token_data['access_token'],
        refresh_token=token_data.get('refresh_token'),
        token_uri='https://oauth2.googleapis.com/token',
        client_id=app.config['GOOGLE_CLIENT_ID'],
        client_secret=app.config['GOOGLE_CLIENT_SECRET'],
        scopes=['https://www.googleapis.com/auth/drive.file']
    )


def get_drive_service(user):
    creds = get_google_credentials(user)
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


def upload_to_drive(user, file_obj, filename, mime_type):
    service = get_drive_service(user)

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

    return file_id, f"/cdn/{file_id}"


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    google_id = db.Column(db.String(100), unique=True, nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    avatar = db.Column(db.String(200))
    tokens = db.Column(db.Text)
    rooms_owned = db.relationship('Room', backref='owner', lazy=True)


class Room(db.Model):
    hash_id = db.Column(db.String(36), primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    institution = db.Column(db.String(100), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    posts = db.relationship('Post', backref='room', lazy=True, cascade="all, delete")


class RoomMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    room_hash = db.Column(db.String(36), db.ForeignKey('room.hash_id'), nullable=False)
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)


class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    room_hash = db.Column(db.String(36), db.ForeignKey('room.hash_id'), nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    author = db.relationship('User', backref=db.backref('user_posts', lazy=True))
    image_url = db.Column(db.String(500), nullable=False)
    drive_file_id = db.Column(db.String(100), nullable=True)
    caption = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


with app.app_context():
    db.create_all()


@app.route('/login')
def login():
    redirect_uri = url_for('auth_callback', _external=True)
    prompt = 'consent' if request.args.get('force_consent') else None
    return google.authorize_redirect(redirect_uri, access_type='offline', prompt=prompt)


@app.route('/auth/callback')
def auth_callback():
    try:
        token = google.authorize_access_token()
    except Exception:
        return redirect(url_for('index', error='auth_failed'))

    DRIVE_SCOPE = 'https://www.googleapis.com/auth/drive.file'
    scopes_concedidos = token.get('scope', '').split()

    if DRIVE_SCOPE not in scopes_concedidos:
        return redirect(url_for('login', error='drive_required', force_consent=True))

    user_info = token.get('userinfo')
    if not user_info:
        user_info = google.get('https://www.googleapis.com/oauth2/v1/userinfo').json()

    google_id = str(user_info.get('sub') or user_info.get('id'))

    user = User.query.filter_by(google_id=google_id).first()

    if not user:
        user = User(
            google_id=google_id,
            email=user_info.get('email'),
            name=user_info.get('name'),
            avatar=user_info.get('picture'),
            tokens=json.dumps(token)
        )
        db.session.add(user)
    else:
        user.tokens = json.dumps(token)
        user.avatar = user_info.get('picture')

    db.session.commit()
    login_user(user, remember=True)
    return redirect(url_for('profile'))


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))


@app.route('/cdn/<file_id>')
def cdn_proxy(file_id):
    if not file_id:
        return "Arquivo não encontrado", 404

    post = Post.query.filter_by(drive_file_id=file_id).first()
    if not post:
        return "Post não encontrado", 404

    author = post.author
    creds = get_google_credentials(author)

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleRequest())
            token_data = json.loads(author.tokens)
            token_data['access_token'] = creds.token
            author.tokens = json.dumps(token_data)
            db.session.commit()
        except Exception:
            return Response("Token do autor expirado", status=403)

    api_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    headers = {'Authorization': f'Bearer {creds.token}'}

    req = requests.get(api_url, headers=headers, stream=True)

    if req.status_code != 200:
        return Response("Erro ao carregar imagem", status=req.status_code)

    forward_headers = {
        'Content-Type': req.headers.get('Content-Type'),
        'Cache-Control': 'public, max-age=31536000'
    }

    return Response(
        stream_with_context(req.iter_content(chunk_size=4096)),
        headers=forward_headers,
        status=200
    )


@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('profile'))
    return render_template('login.html')


@app.route('/profile')
@login_required
def profile():
    return render_template('profile.html', user=current_user)


@app.route('/join/<room_hash>')
@login_required
def join_room(room_hash):
    room = db.session.get(Room, room_hash)
    if not room:
        return "Sala não encontrada", 404

    member = RoomMember.query.filter_by(user_id=current_user.id, room_hash=room_hash).first()
    if not member:
        member = RoomMember(user_id=current_user.id, room_hash=room_hash)
        db.session.add(member)
        db.session.commit()

    posts = Post.query.filter_by(room_hash=room_hash).order_by(Post.created_at.desc()).all()
    return render_template('feed.html', room=room, posts=posts, user=current_user)


@app.route('/api/create_room', methods=['POST'])
@login_required
def create_room():
    data = request.json
    room_hash = secrets.token_urlsafe(6)
    new_room = Room(
        hash_id=room_hash,
        owner_id=current_user.id,
        institution=data.get('institution'),
        name=data.get('room_name')
    )
    db.session.add(new_room)
    db.session.commit()
    return jsonify({'redirect_url': url_for('join_room', room_hash=room_hash, _external=True)})


@app.route('/api/post/<room_hash>', methods=['POST'])
@login_required
def add_post(room_hash):
    file = request.files.get('photo')
    caption = request.form.get('caption')

    if not file:
        return jsonify({'error': 'Dados incompletos'}), 400

    processed_file, mime_type = compress_image_if_needed(file)

    filename = secure_filename(f"{uuid.uuid4().hex[:8]}_{file.filename}")
    if mime_type == 'image/jpeg' and not filename.lower().endswith(('.jpg', '.jpeg')):
        filename = f"{filename}.jpg"

    file_id, image_url = upload_to_drive(current_user, processed_file, filename, mime_type)

    new_post = Post(
        room_hash=room_hash,
        author_id=current_user.id,
        image_url=image_url,
        drive_file_id=file_id,
        caption=caption
    )
    db.session.add(new_post)
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/updates/<room_hash>')
@login_required
def check_updates(room_hash):
    last_id = request.args.get('since', 0, type=int)
    new_posts = Post.query.filter(Post.room_hash == room_hash, Post.id > last_id).order_by(Post.created_at.asc()).all()

    data = []
    room = db.session.get(Room, room_hash)
    for post in new_posts:
        can_delete = (current_user.id == post.author_id or current_user.id == room.owner_id)
        img_url = url_for('cdn_proxy', file_id=post.drive_file_id, _external=True)
        data.append({
            'id': post.id,
            'author_name': post.author.name,
            'author_avatar': post.author.avatar,
            'author_initial': post.author.name[0].upper(),
            'image_url': img_url,
            'caption': post.caption,
            'can_delete': can_delete
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


if __name__ == '__main__':
    app.run(debug=True, port=5000)