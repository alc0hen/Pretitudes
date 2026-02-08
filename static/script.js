// --- Gerenciamento de Identidade Local ---

const Storage = {
    getUUID: () => {
        let uuid = localStorage.getItem('user_uuid');
        if (!uuid) {
            uuid = crypto.randomUUID();
            localStorage.setItem('user_uuid', uuid);
        }
        return uuid;
    },
    setName: (name, institution) => {
        localStorage.setItem('user_name', name);
        localStorage.setItem('user_institution', institution);
    },
    getName: () => localStorage.getItem('user_name'),
    getInstitution: () => localStorage.getItem('user_institution')
};

// --- Lógica da Página Host ---

async function createRoom() {
    const hostName = document.getElementById('hostName').value;
    const institution = document.getElementById('institution').value;
    const roomName = document.getElementById('roomName').value;

    if (!hostName || !institution || !roomName) return alert("Preencha tudo!");

    // Salva identidade do Host
    Storage.getUUID(); // Garante que tem UUID
    Storage.setName(hostName, institution);

    const response = await fetch('/api/create_room', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            host_uuid: Storage.getUUID(),
            institution: institution,
            room_name: roomName
        })
    });

    const data = await response.json();
    if (data.redirect_url) window.location.href = data.redirect_url;
}

// --- Lógica do Feed ---

document.addEventListener("DOMContentLoaded", () => {
    // Só roda se estiver na página de feed
    const roomData = document.getElementById('roomData');
    if (!roomData) return;

    const roomInstitution = roomData.dataset.institution;
    const hostUUID = roomData.dataset.hostUuid;

    // 1. Verifica Identidade do Aluno
    let userName = Storage.getName();
    if (!userName) {
        userName = prompt(`Bem-vindo à sala do ${roomInstitution}.\nQual seu nome?`);
        if (userName) {
            Storage.setName(userName, roomInstitution);
        } else {
            // Se cancelar, define como Anônimo pra não quebrar
            Storage.setName("Anônimo", roomInstitution);
        }
    }

    // 2. Verifica se é Host para mostrar botões de delete
    const myUUID = Storage.getUUID();
    if (myUUID === hostUUID) {
        document.querySelectorAll('.btn-delete').forEach(btn => btn.style.display = 'block');
    }
});

// --- Funções de Postagem ---

function openModal() { document.getElementById('postModal').style.display = 'block'; }
function closeModal() { document.getElementById('postModal').style.display = 'none'; }

async function submitPost() {
    const roomHash = document.getElementById('roomData').dataset.hash;
    const fileInput = document.getElementById('fileInput');
    const caption = document.getElementById('captionInput').value;
    const fullName = `${Storage.getName()} - ${document.getElementById('roomData').dataset.institution}`;

    if (fileInput.files.length === 0) return alert("Selecione uma foto!");

    const formData = new FormData();
    formData.append('photo', fileInput.files[0]);
    formData.append('caption', caption);
    formData.append('user_name', fullName);

    const response = await fetch(`/api/post/${roomHash}`, {
        method: 'POST',
        body: formData
    });

    if (response.ok) location.reload();
    else alert("Erro ao postar");
}

async function deletePost(postId) {
    if(!confirm("Apagar este post?")) return;
    
    const response = await fetch(`/api/delete/${postId}`, {
        method: 'DELETE',
        headers: { 'X-Host-UUID': Storage.getUUID() }
    });

    if (response.ok) {
        document.getElementById(`post-${postId}`).remove();
    } else {
        alert("Apenas o host pode apagar posts.");
    }
}
