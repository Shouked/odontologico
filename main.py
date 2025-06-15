# main.py  –  API Odonto-Sorriso

from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from databases import Database
from sqlalchemy import MetaData, Table, Column, String, Text, DateTime, Date, func
from sqlalchemy.dialects.postgresql import UUID
import uuid, json, httpx, os
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta, date
from openai import AsyncOpenAI

# ─────────────────── Configuração básica ─────────────────── #
load_dotenv()

app = FastAPI(title="API Consultório Odontológico")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL não definida.")

database = Database(DATABASE_URL)
metadata = MetaData()

# ─────────────────── Tabelas ─────────────────── #
pacientes = Table(
    "pacientes", metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
    Column("nome", String(255), nullable=False),
    Column("telefone", String(255), unique=True, nullable=False),
    Column("data_nascimento", Date, nullable=True),
)

agendamentos = Table(
    "agendamentos", metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
    Column("paciente_id", UUID(as_uuid=True), nullable=False),
    Column("data_hora", DateTime(timezone=True), nullable=False),
    Column("procedimento", String(255), nullable=False),
    Column("status", String(50), nullable=False, default="Agendado"),
)

historico_conversas = Table(
    "historico_conversas", metadata,
    Column("telefone", String(255), primary_key=True),
    Column("historico", Text, nullable=False),
    Column("last_updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("snoozed_until", DateTime(timezone=True), nullable=True),
)

# ─────────────────── Eventos FastAPI ─────────────────── #
@app.on_event("startup")
async def startup() -> None:
    await database.connect()


@app.on_event("shutdown")
async def shutdown() -> None:
    await database.disconnect()

# ─────────────────── Modelos Pydantic ─────────────────── #
class MensagemChat(BaseModel):
    telefone_usuario: str
    mensagem: str
    historico: Optional[List[dict]] = None

# ─────────────────── Ferramentas de Negócio ─────────────────── #
async def consultar_horarios_disponiveis(dia: str) -> str:
    try:
        data_cons = datetime.strptime(dia, "%Y-%m-%d").date()
    except ValueError:
        return "Formato de data inválido. Use AAAA-MM-DD."

    if data_cons < date.today():
        return "Não é possível agendar em datas passadas."

    base = [f"{h:02d}:00" for h in range(9, 18) if h != 12]
    rows = await database.fetch_all(
        agendamentos.select().where(func.date(agendamentos.c.data_hora) == data_cons)
    )
    ocupados = {r["data_hora"].strftime("%H:%M") for r in rows}
    disponiveis = [h for h in base if h not in ocupados]

    if not disponiveis:
        return f"Sem horários disponíveis em {data_cons:%d/%m/%Y}. Escolha outra data."
    return f"Horários disponíveis em {data_cons:%d/%m/%Y}: {', '.join(disponiveis)}."


async def agendar_consulta(telefone: str, data_hora_str: str, procedimento: str) -> str:
    paciente = await database.fetch_one(
        pacientes.select().where(pacientes.c.telefone == telefone)
    )
    if not paciente:
        return "Paciente não encontrado. Cadastre-se antes."

    try:
        data_hora = datetime.fromisoformat(data_hora_str).astimezone(timezone.utc)
    except ValueError:
        return "Formato de data-hora inválido. Use AAAA-MM-DDTHH:MM:SS."

    await database.execute(
        agendamentos.insert().values(
            id=uuid.uuid4(),
            paciente_id=paciente["id"],
            data_hora=data_hora,
            procedimento=procedimento,
            status="Agendado",
        )
    )
    return f"Agendamento confirmado para {data_hora:%d/%m/%Y às %H:%M} – {procedimento}."


async def cadastrar_paciente(
    telefone: str, nome: str, data_nasc_str: Optional[str]
) -> str:
    if await database.fetch_one(
        pacientes.select().where(pacientes.c.telefone == telefone)
    ):
        return "Você já possui cadastro."

    data_nasc = None
    if data_nasc_str:
        for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                data_nasc = datetime.strptime(data_nasc_str, fmt).date()
                break
            except ValueError:
                continue
        if not data_nasc:
            return "Data de nascimento inválida. Use DD/MM/AAAA."

    await database.execute(
        pacientes.insert().values(
            id=uuid.uuid4(),
            nome=nome,
            telefone=telefone,
            data_nascimento=data_nasc,
        )
    )
    return f"Cadastro realizado, {nome.split()[0]}! Como posso ajudar agora?"

# ─────────────────── IA / Transcrição / Download ─────────────────── #
async def chamar_ia(msgs: List[dict]) -> Dict[str, Any]:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
        "Content-Type": "application/json",
        "Referer": os.getenv("PUBLIC_URL", ""),
    }
    body = {"model": "openai/gpt-4o", "messages": msgs}

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return json.loads(content)
    except Exception as e:
        print("Erro IA:", e)
        return {"action": "responder", "data": {"texto": "Desculpe, erro na IA."}}


async def transcrever_audio(audio_bytes: bytes) -> Optional[str]:
    try:
        client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        file = ("audio.ogg", audio_bytes, "audio/ogg")
        tr = await client.audio.transcriptions.create(model="whisper-1", file=file)
        return tr.text
    except Exception as e:
        print("Erro transcrição:", e)
        return None


async def baixar_audio_bytes(url: str) -> Optional[bytes]:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                url, headers={"Client-Token": os.getenv("CLIENT_TOKEN")}
            )
            resp.raise_for_status()
            return resp.content
    except Exception as e:
        print("Erro download áudio:", e)
        return None

# ─────────────────── Prompt da Sofia ─────────────────── #
PROMPT_SOFIA = f"""
### Papel e Objetivo
Você é **Sofia**, assistente virtual da clínica *Odonto-Sorriso*.
Ajude pacientes a tirar dúvidas, cadastrar-se e agendar consultas.

### Informações da Clínica
- Horário: Seg-Sex 09:00-18:00 (fechado 12-13h)
- Procedimentos: Limpeza, Clareamento Dental, Restauração, Tratamento de Canal
- Duração padrão: 1 h.
- Data atual: {date.today():%d/%m/%Y}

### Regras
1. Use sempre o primeiro nome do paciente.
2. Peça **uma** informação por vez.
3. Use `consultar_horarios_disponiveis` para saber horários.
4. Antes de `agendar_consulta`, tenha procedimento+data+hora.
5. Se paciente não existir, colete nome completo + data de nascimento e chame `cadastrar_paciente`.

### Ferramentas (responda em JSON)
```json
{{"action":"responder","data":{{"texto":"..."}}}}
{{"action":"cadastrar_paciente","data":{{"nome":"Nome","data_nascimento":"DD/MM/AAAA"}}}}
{{"action":"consultar_horarios_disponiveis","data":{{"dia":"AAAA-MM-DD"}}}}
{{"action":"agendar_consulta","data":{{"procedimento":"...","data_hora":"AAAA-MM-DDTHH:MM:SS"}}}}
"""

@app.get("/")
async def root() -> Dict[str, str]:
return {"message": "API Odonto-Sorriso viva!"}

@app.head("/")
async def head_root() -> Response:
return Response(status_code=200)

@app.post("/chat")
async def chat(dados: MensagemChat) -> Dict[str, str]:
mensagens = [{"role": "system", "content": PROMPT_SOFIA}]
mensagens += dados.historico or []
mensagens.append({"role": "user", "content": dados.mensagem})

ia = await chamar_ia(mensagens)
action, data = ia.get("action"), ia.get("data", {})

if action == "responder":
    return {"reply": data.get("texto", "")}

if action == "cadastrar_paciente":
    return {
        "reply": await cadastrar_paciente(
            dados.telefone_usuario, data.get("nome"), data.get("data_nascimento")
        )
    }

if action == "consultar_horarios_disponiveis":
    return {"reply": await consultar_horarios_disponiveis(data.get("dia", ""))}

if action == "agendar_consulta":
    return {
        "reply": await agendar_consulta(
            dados.telefone_usuario,
            data.get("data_hora", ""),
            data.get("procedimento", ""),
        )
    }

return {"reply": "Desculpe, não entendi. Pode reformular?"}

@app.post("/whatsapp")
async def whatsapp_webhook(request: Request) -> Dict[str, str]:
try:
payload = await request.json()
except Exception:
raise HTTPException(status_code=400, detail="JSON inválido.")

telefone = payload.get("phone")
if not telefone:
    raise HTTPException(status_code=400, detail="Campo phone ausente.")

if payload.get("fromMe"):
    return {"status": "ok", "msg": "modo manual"}

texto = payload.get("text", {}).get("message")
audio_url = payload.get("audio", {}).get("audioUrl")
conteudo = texto or ""
if audio_url:
    bytes_audio = await baixar_audio_bytes(audio_url)
    conteudo = (
        await transcrever_audio(bytes_audio) if bytes_audio else "[Falha no áudio]"
    )

if not conteudo:
    return {"status": "ok", "msg": "sem conteúdo"}

row = await database.fetch_one(
    historico_conversas.select().where(historico_conversas.c.telefone == telefone)
)
hist = (
    json.loads(row["historico"])
    if row and datetime.now(timezone.utc) - row["last_updated_at"] < timedelta(hours=24)
    else []
)

async with httpx.AsyncClient() as client:
    public = os.getenv("PUBLIC_URL", "").rstrip("/")
    resp = await client.post(
        f"{public}/chat",
        json={
            "telefone_usuario": telefone,
            "mensagem": conteudo,
            "historico": hist,
        },
        timeout=120,
    )
    reply = resp.json().get("reply", "Falha no bot.")

    novo_hist = hist + [
        {"role": "user", "content": conteudo},
        {"role": "assistant", "content": reply},
    ]
    hist_json = json.dumps(novo_hist[-20:])

    if row:
        await database.execute(
            historico_conversas.update()
            .where(historico_conversas.c.telefone == telefone)
            .values(historico=hist_json, last_updated_at=func.now())
        )
    else:
        await database.execute(
            historico_conversas.insert().values(
                telefone=telefone, historico=hist_json, last_updated_at=func.now()
            )
        )

    await client.post(
        f"https://api.z-api.io/instances/{os.getenv('INSTANCE_ID')}/token/{os.getenv('TOKEN')}/send-text",
        headers={"Client-Token": os.getenv("CLIENT_TOKEN")},
        json={"phone": telefone, "message": reply},
        timeout=30,
    )

return {"status": "ok"}
