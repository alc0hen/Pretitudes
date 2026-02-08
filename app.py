import os
import uuid
import secrets
from flask import Flask, render_template, request, jsonify, url_for
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from datetime import datetime

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///pedagogico.db'
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['AVATAR_FOLDER'] = 'static/avatars'  # Pasta nova para avatares
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 
db = SQLAlchemy(app)

# --- Models ---
class Room(db.Model):
    hash_id = db.Column(db.String(36), primary_key=True)
    host_uuid = db.Column(db.String(36), nullable=False)
    institution = db.Column(db.String(100), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    posts = db.relationship('Post', backref='room', lazy=True, cascade="all, delete")

class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    room_hash = db.Column(db.String(36), db.ForeignKey('room.hash_id'), nullable=False)
    user_name = db.Column(db.String(100), nullable=False)
    user_avatar = db.Column(db.String(200), nullable=True) # Novo campo: Foto do perfil
    image_filename = db.Column(db.String(200), nullable=False)
    caption = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# --- Rotas ---
@app.route('/')
def index():
    return render_template('host.html')

@app.route('/host')
def host_view():
    return render_template('host.html')

@app.route('/join/<room_hash>')
def join_room(room_hash):
    room = Room.query.get_or_404(room_hash)
    posts = Post.query.filter_by(room_hash=room_hash).order_by(Post.created_at.desc()).all()
    return render_template('feed.html', room=room, posts=posts)

# --- API ---
@app.route('/api/create_room', methods=['POST'])
def create_room():
    data = request.json
    room_hash = secrets.token_urlsafe(6)
    new_room = Room(
        hash_id=room_hash,
        host_uuid=data.get('host_uuid'),
        institution=data.get('institution'),
        name=data.get('room_name')
    )
    db.session.add(new_room)
    db.session.commit()
    return jsonify({'redirect_url': url_for('join_room', room_hash=room_hash, _external=True)})

# Rota específica para subir o avatar do usuário
@app.route('/api/upload_avatar', methods=['POST'])
def upload_avatar():
    file = request.files.get('avatar')
    if not file:
        return jsonify({'error': 'Sem arquivo'}), 400
    
    filename = secure_filename(f"avatar_{uuid.uuid4().hex[:8]}_{file.filename}")
    file.save(os.path.join(app.config['AVATAR_FOLDER'], filename))
    
    return jsonify({'filename': filename})

@app.route('/api/post/<room_hash>', methods=['POST'])
def add_post(room_hash):
    file = request.files.get('photo')
    user_name = request.form.get('user_name')
    avatar_filename = request.form.get('user_avatar') # Recebe o nome do avatar já salvo
    caption = request.form.get('caption')

    if not file or not user_name:
        return jsonify({'error': 'Dados incompletos'}), 400

    filename = secure_filename(f"{uuid.uuid4().hex[:8]}_{file.filename}")
    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
    
    new_post = Post(
        room_hash=room_hash,
        user_name=user_name,
        user_avatar=avatar_filename, # Salva no banco
        image_filename=filename,
        caption=caption
    )
    db.session.add(new_post)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/delete/<int:post_id>', methods=['DELETE'])
def delete_post(post_id):
    post = Post.query.get_or_404(post_id)
    requester_uuid = request.headers.get('X-Host-UUID')
    if post.room.host_uuid == requester_uuid:
        db.session.delete(post)
        db.session.commit()
        return jsonify({'success': True})
    return jsonify({'error': 'Não autorizado'}), 403

if __name__ == '__main__':
    # Cria as pastas se não existirem
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(app.config['AVATAR_FOLDER'], exist_ok=True)
    
    with app.app_context():
        db.create_all()
    app.run(debug=True, port=5000, host='0.0.0.0')
