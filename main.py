from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from databases import Database
from sqlalchemy import create_engine, MetaData, Table, Column, String, Text, DateTime, Date, func
from sqlalchemy.dialects.postgresql import UUID
import uuid, json, httpx, os
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta, date
from openai import AsyncOpenAI

# Carrega as variáveis de ambiente do arquivo .env
load_dotenv()

# Inicialização do FastAPI
app = FastAPI(title="API Consultório Odontológico Avançada")

# Configuração do CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Configuração do Banco de Dados ---
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL não foi definida nas variáveis de ambiente.")

database = Database(DATABASE_URL)
metadata = MetaData()

# Definição das tabelas
pacientes = Table(
    "pacientes",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
    Column("nome", String(255), nullable=False),
    Column("telefone", String(255), unique=True, nullable=False),
    Column("data_nascimento", Date, nullable=True),
)
agendamentos = Table(
    "agendamentos",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
    Column("paciente_id", UUID(as_uuid=True), nullable=False),
    Column("data_hora", DateTime(timezone=True), nullable=False),
    Column("procedimento", String(255), nullable=False),
    Column("status", String(50), nullable=False, default='Agendado'),
)
historico_conversas = Table(
    "historico_conversas",
    metadata,
    Column("telefone", String(255), primary_key=True),
    Column("historico", Text, nullable=False),
    Column("last_updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("snoozed_until", DateTime(timezone=True), nullable=True)
)

# --- Ciclo de Vida da Aplicação ---
@app.on_event("startup")
async def startup():
    await database.connect()

@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()

# --- Modelos Pydantic ---
class ChatRequest(BaseModel):
    telefone_usuario: str
    mensagem: str
    historico: Optional[List[dict]] = []

# --- Funções de Ferramenta da IA ---

async def consultar_paciente_por_telefone(telefone: str) -> str:
    query = pacientes.select().where(pacientes.c.telefone == telefone)
    paciente = await database.fetch_one(query)
    return f"Paciente encontrado: {paciente['nome']}." if paciente else "Paciente não cadastrado."

async def cadastrar_paciente(telefone: str, nome: str, data_nascimento_str: Optional[str]) -> str:
    existente = await database.fetch_one(pacientes.select().where(pacientes.c.telefone == telefone))
    if existente: return "Você já possui um cadastro conosco."
    data_nascimento = None
    if data_nascimento_str:
        try:
            data_nascimento = datetime.strptime(data_nascimento_str, "%d/%m/%Y").date()
        except ValueError:
            try: data_nascimento = datetime.strptime(data_nascimento_str, "%Y-%m-%d").date()
            except ValueError: return "Formato de data inválido. Peça para o usuário fornecer no formato DD/MM/AAAA."
    novo_paciente = {"id": uuid.uuid4(), "nome": nome, "telefone": telefone, "data_nascimento": data_nascimento}
    query = pacientes.insert().values(**novo_paciente)
    await database.execute(query)
    return f"Ótimo, {nome.split(' ')[0]}! Seu cadastro foi realizado. Agora já podemos agendar sua consulta. Qual procedimento você gostaria?"

async def consultar_horarios_disponiveis(dia_preferencial_str: Optional[str] = None) -> str:
    data_inicial = date.today()
    if dia_preferencial_str:
        try:
            data_inicial = datetime.strptime(dia_preferencial_str, "%Y-%m-%d").date()
            if data_inicial < date.today(): return f"Não é possível agendar em datas passadas."
        except ValueError: return "Formato de data inválido. Use AAAA-MM-DD."
    for i in range(30):
        data_consulta = data_inicial + timedelta(days=i)
        if data_consulta.weekday() >= 5: continue
        horarios_base = [f"{h:02d}:00" for h in range(9, 18) if h != 12]
        query = agendamentos.select().where(func.date(agendamentos.c.data_hora) == data_consulta)
        agendamentos_existentes = await database.fetch_all(query)
        horarios_ocupados = [a['data_hora'].astimezone(timezone(timedelta(hours=-3))).strftime("%H:%M") for a in agendamentos_existentes]
        horarios_disponiveis = [h for h in horarios_base if h not in horarios_ocupados]
        if horarios_disponiveis:
            return f"Encontrei horários para o dia {data_consulta.strftime('%d/%m/%Y')}: {', '.join(horarios_disponiveis)}."
    return "Não encontrei horários disponíveis nos próximos 30 dias."

# --- Funções Auxiliares de IA e Mídia ---

async def chamar_ia(messages: List[dict]) -> dict:
    # Código da função permanece o mesmo...
    return {}

async def transcrever_audio(audio_bytes: bytes) -> str | None:
    # Código da função permanece o mesmo...
    return ""

async def baixar_audio_bytes(url: str) -> bytes | None:
    # Código da função permanece o mesmo...
    return b""

# --- Lógica Central do Chat (Refatorada) ---
async def processar_chat_logic(dados: ChatRequest):
    historico = dados.historico or []

    prompt_sistema = """
### Papel e Objetivo
Você é a Sofia, a recepcionista virtual da clínica "Odonto-Sorriso". Sua missão é ser proativa, eficiente e seguir o fluxo de conversa à risca. A data de hoje é {current_date}.

### Informações da Clínica
- **Procedimentos:** Limpeza, Clareamento Dental, Restauração, Tratamento de Canal.
- **Horário:** Segunda a Sexta, 09:00-12:00 e 13:00-18:00.

### Fluxo Obrigatório de Conversa
1.  **Início da Conversa:** Você receberá o status do paciente ("Paciente encontrado: [Nome]" ou "Paciente não cadastrado") como um resultado de ferramenta. Use essa informação para saudar o paciente de forma personalizada e iniciar a conversa.
2.  **Agendamento:** Se o paciente quiser agendar, SEMPRE pergunte o procedimento ANTES de consultar horários. Depois, use a ferramenta `consultar_horarios_disponiveis` para encontrar o próximo dia com vagas.
3.  **Ferramentas:** Use as ferramentas disponíveis para interagir com o sistema. Responda sempre em JSON.

### Definição das Ferramentas (Actions)
1.  **responder:** `{"action": "responder", "data": {"texto": "Sua resposta aqui."}}`
2.  **cadastrar_paciente:** `{"action": "cadastrar_paciente", "data": {"nome": "Nome Completo", "data_nascimento": "DD/MM/AAAA"}}`
3.  **agendar_consulta:** `{"action": "agendar_consulta", "data": {"procedimento": "Nome", "data_hora": "AAAA-MM-DDTHH:MM:SS"}}`
4.  **consultar_horarios_disponiveis:** `{"action": "consultar_horarios_disponiveis", "data": {"dia": "AAAA-MM-DD"}}`
""".replace("{current_date}", date.today().isoformat())

    messages = [{"role": "system", "content": prompt_sistema}]
    
    if not historico:
        resultado_consulta = await consultar_paciente_por_telefone(dados.telefone_usuario)
        messages.append({"role": "user", "content": dados.mensagem})
        messages.append({"role": "tool", "name": "consultar_paciente_por_telefone", "content": resultado_consulta})
    else:
        messages.extend(historico)
        messages.append({"role": "user", "content": dados.mensagem})

    resposta_ia = await chamar_ia(messages)
    action = resposta_ia.get("action")
    action_data = resposta_ia.get("data", {})
    
    # Roteador de Ações
    if action == "responder":
        return {"reply": action_data.get("texto")}
    elif action == "cadastrar_paciente":
        return {"reply": await cadastrar_paciente(dados.telefone_usuario, action_data.get("nome"), action_data.get("data_nascimento"))}
    elif action == "agendar_consulta":
        return {"reply": await agendar_consulta(dados.telefone_usuario, action_data.get("data_hora"), action_data.get("procedimento"))}
    elif action == "consultar_horarios_disponiveis":
        return {"reply": await consultar_horarios_disponiveis(action_data.get("dia"))}
    else:
        return {"reply": "Não entendi a ação. Por favor, reformule."}

# --- Endpoints da API ---

@app.get("/")
async def root():
    return {"message": "API do Consultório Odonto-Sorriso no ar!"}

@app.head("/")
async def head_root():
    return Response(status_code=200)

@app.post("/chat")
async def chat_endpoint(dados: ChatRequest):
    """Endpoint HTTP que expõe a lógica do chat."""
    return await processar_chat_logic(dados)

@app.post("/whatsapp")
async def receber_mensagem_zapi(request: Request):
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Payload JSON inválido.")

    numero_contato = payload.get("phone")
    if not numero_contato:
        raise HTTPException(status_code=400, detail="O campo 'phone' é obrigatório.")

    if payload.get("fromMe"):
        # Lógica do modo manual...
        return {"status": "ok", "message": "Modo manual ativado."}

    conteudo_processar = None
    texto_da_mensagem = payload.get("text", {}).get("message")
    audio_url = payload.get("audio", {}).get("audioUrl")

    if texto_da_mensagem:
        conteudo_processar = texto_da_mensagem
    elif audio_url:
        audio_bytes = await baixar_audio_bytes(audio_url)
        if audio_bytes:
            conteudo_processar = await transcrever_audio(audio_bytes) or "[Erro na transcrição]"
        else:
            conteudo_processar = "[Erro no download do áudio]"

    if not conteudo_processar:
        return {"status": "ok", "message": "Ignorando mensagem sem conteúdo."}

    try:
        query_select = historico_conversas.select().where(historico_conversas.c.telefone == numero_contato)
        resultado = await database.fetch_one(query_select)
        historico_recuperado = []
        if resultado:
             if resultado["snoozed_until"] and resultado["snoozed_until"] > datetime.now(timezone.utc):
                 return {"status": "ok", "message": "Conversa em modo manual."}
             if datetime.now(timezone.utc) - resultado["last_updated_at"] < timedelta(hours=24):
                 historico_recuperado = json.loads(resultado["historico"])
        
        # **CORREÇÃO**: Chama a lógica do chat diretamente, sem HTTP
        dados_chat = ChatRequest(telefone_usuario=numero_contato, mensagem=conteudo_processar, historico=historico_recuperado)
        dados_resposta = await processar_chat_logic(dados_chat)
        mensagem_resposta = dados_resposta.get("reply", "Não consegui gerar uma resposta.")

        historico_atualizado = historico_recuperado + [
            {"role": "user", "content": conteudo_processar},
            {"role": "assistant", "content": mensagem_resposta}
        ]
        historico_str = json.dumps(historico_atualizado[-20:])
        if resultado:
            query_db = historico_conversas.update().where(historico_conversas.c.telefone == numero_contato).values(historico=historico_str, last_updated_at=func.now(), snoozed_until=None)
        else:
            query_db = historico_conversas.insert().values(telefone=numero_contato, historico=historico_str, last_updated_at=func.now(), snoozed_until=None)
        await database.execute(query_db)

        # Envia a resposta final para o usuário
        async with httpx.AsyncClient() as client:
            instance_id, token, client_token = os.getenv("INSTANCE_ID"), os.getenv("TOKEN"), os.getenv("CLIENT_TOKEN")
            zapi_headers = {"Client-Token": client_token}
            await client.post(
                f"https://api.z-api.io/instances/{instance_id}/token/{token}/send-text",
                json={"phone": numero_contato, "message": mensagem_resposta},
                headers=zapi_headers, 
                timeout=30.0
            )
    except Exception as e:
        print(f"!!! Erro no Webhook /whatsapp: {e} !!!")

    return {"status": "ok"}
