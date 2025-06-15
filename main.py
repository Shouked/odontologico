# main.py
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List
from databases import Database
from sqlalchemy import create_engine, MetaData, Table, Column, String, Text, DateTime, Date, func, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
import uuid, json, httpx, os, pytz
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta, date
from openai import AsyncOpenAI

# Carrega as variáveis de ambiente do arquivo .env
load_dotenv()

# --- Configuração Essencial ---
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL não foi definida nas variáveis de ambiente.")

SAO_PAULO_TZ = pytz.timezone("America/Sao_Paulo")

# Inicialização do FastAPI
app = FastAPI(title="API Consultório Odontológico Avançada")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Configuração do Banco de Dados ---
database = Database(DATABASE_URL)
metadata = MetaData()

# Definição das tabelas
pacientes = Table(
    "pacientes", metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
    Column("nome", String(255), nullable=False),
    Column("telefone", String(255), unique=True, nullable=False, index=True),
    Column("data_nascimento", Date, nullable=True),
)

agendamentos = Table(
    "agendamentos", metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
    Column("paciente_id", UUID(as_uuid=True), ForeignKey("pacientes.id"), nullable=False),
    Column("data_hora", DateTime(timezone=True), nullable=False, index=True),
    Column("procedimento", String(255), nullable=False),
    Column("status", String(50), nullable=False, default='Agendado'),
)

historico_conversas = Table(
    "historico_conversas", metadata,
    Column("telefone", String(255), primary_key=True, index=True),
    Column("historico", Text, nullable=False),
    Column("last_updated_at", DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()),
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
    historico: List[dict] = Field(default_factory=list)

# --- Funções de Ferramenta da IA (Lógica das Ações) ---

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
    hoje_sp = datetime.now(SAO_PAULO_TZ).date()
    data_inicial = hoje_sp
    if dia_preferencial_str:
        try:
            data_preferencial = datetime.strptime(dia_preferencial_str, "%Y-%m-%d").date()
            if data_preferencial < hoje_sp: return f"Não é possível agendar em datas passadas. A data de hoje é {hoje_sp.strftime('%d/%m/%Y')}."
            data_inicial = data_preferencial
        except ValueError: return "Formato de data inválido. Use AAAA-MM-DD."
    for i in range(30):
        data_consulta = data_inicial + timedelta(days=i)
        if data_consulta.weekday() >= 5: continue
        horarios_base = [f"{h:02d}:00" for h in range(9, 18) if h != 12]
        inicio_dia_utc = SAO_PAULO_TZ.localize(datetime.combine(data_consulta, datetime.min.time())).astimezone(timezone.utc)
        fim_dia_utc = SAO_PAULO_TZ.localize(datetime.combine(data_consulta, datetime.max.time())).astimezone(timezone.utc)
        query = agendamentos.select().where(
            (agendamentos.c.data_hora >= inicio_dia_utc) &
            (agendamentos.c.data_hora <= fim_dia_utc) &
            (agendamentos.c.status != 'Cancelado')
        )
        agendamentos_existentes = await database.fetch_all(query)
        horarios_ocupados = {a['data_hora'].astimezone(SAO_PAULO_TZ).strftime("%H:%M") for a in agendamentos_existentes}
        horarios_disponiveis = [h for h in horarios_base if h not in horarios_ocupados]
        if horarios_disponiveis: return f"Encontrei horários para o dia {data_consulta.strftime('%d/%m/%Y')}: {', '.join(horarios_disponiveis)}."
    return "Não encontrei horários disponíveis nos próximos 30 dias."

async def agendar_consulta(telefone: str, data_hora_str: str, procedimento: str) -> str:
    paciente_query = pacientes.select().where(pacientes.c.telefone == telefone)
    paciente = await database.fetch_one(paciente_query)
    if not paciente: return "Não encontrei seu cadastro. Por favor, informe seu nome completo para começarmos."
    try:
        dt_obj_local = SAO_PAULO_TZ.localize(datetime.fromisoformat(data_hora_str))
        data_hora_utc = dt_obj_local.astimezone(timezone.utc)
    except Exception: return "Formato de data e hora inválido ou ambíguo. Use AAAA-MM-DDTHH:MM:SS."
    novo_agendamento = {"id": uuid.uuid4(), "paciente_id": paciente["id"], "data_hora": data_hora_utc, "procedimento": procedimento, "status": "Agendado"}
    query = agendamentos.insert().values(**novo_agendamento)
    await database.execute(query)
    return f"Perfeito, {paciente['nome'].split(' ')[0]}! Seu agendamento para {procedimento} no dia {dt_obj_local.strftime('%d/%m/%Y às %H:%M')} foi confirmado."

# <<< NOVO: Ferramenta para consultar agendamentos existentes do paciente >>>
async def consultar_meus_agendamentos_por_telefone(telefone: str) -> str:
    """
    Busca no banco de dados os agendamentos futuros para um determinado paciente.
    """
    # Passo 1: Encontrar o ID do paciente a partir do telefone
    paciente_query = pacientes.select().where(pacientes.c.telefone == telefone)
    paciente = await database.fetch_one(paciente_query)
    
    if not paciente:
        return "Não encontrei seu cadastro. Para que eu possa verificar seus agendamentos, você precisa estar cadastrado. Gostaria de se cadastrar?"

    # Passo 2: Buscar agendamentos futuros e ativos para esse paciente
    agendamentos_query = agendamentos.select().where(
        (agendamentos.c.paciente_id == paciente["id"]) &
        (agendamentos.c.data_hora >= func.now()) & # func.now() é ciente do fuso horário no PostgreSQL
        (agendamentos.c.status == 'Agendado')
    ).order_by(agendamentos.c.data_hora) # Ordena do mais próximo para o mais distante

    agendamentos_futuros = await database.fetch_all(agendamentos_query)

    if not agendamentos_futuros:
        return f"Olá, {paciente['nome'].split(' ')[0]}! Verifiquei aqui e você não possui agendamentos futuros conosco."

    # Passo 3: Formatar a resposta para o usuário
    lista_formatada = []
    for ag in agendamentos_futuros:
        data_hora_local = ag['data_hora'].astimezone(SAO_PAULO_TZ)
        item = f"- {ag['procedimento']} no dia {data_hora_local.strftime('%d/%m/%Y às %H:%M')}"
        lista_formatada.append(item)
    
    resposta = f"Olá, {paciente['nome'].split(' ')[0]}! Encontrei os seguintes agendamentos no seu nome:\n" + "\n".join(lista_formatada)
    return resposta


# --- Funções Auxiliares de IA e Mídia ---

async def chamar_ia(messages: List[dict]) -> dict:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
        "Content-Type": "application/json",
    }
    body = {"model": "openai/gpt-4o", "messages": messages, "response_format": {"type": "json_object"}}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
            content_str = response.json()["choices"][0]["message"]["content"]
            return json.loads(content_str)
    except (httpx.HTTPStatusError, json.JSONDecodeError, KeyError) as e:
        print(f"Erro na comunicação ou parse da IA: {e}")
        return {"action": "responder", "data": {"texto": "Desculpe, estou com dificuldades técnicas. Por favor, tente novamente em alguns instantes."}}


async def transcrever_audio(audio_bytes: bytes) -> str | None:
    try:
        client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        audio_file = ("audio.ogg", audio_bytes, "audio/ogg")
        transcription = await client.audio.transcriptions.create(model="whisper-1", file=audio_file)
        return transcription.text
    except Exception as e:
        print(f"Erro na transcrição: {e}")
        return None


async def baixar_audio_bytes(url: str) -> bytes | None:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            zapi_headers = {"Client-Token": os.getenv("CLIENT_TOKEN")}
            resposta = await client.get(url, headers=zapi_headers)
            resposta.raise_for_status()
            return resposta.content
    except Exception as e:
        print(f"Erro ao baixar áudio: {e}")
        return None

# --- Lógica Central do Chat (Refatorada) ---
async def processar_chat_logic(dados: ChatRequest) -> str:
    # <<< MUDANÇA: Adicionada a nova ferramenta ao prompt do sistema >>>
    prompt_sistema = """
### Papel e Objetivo
Você é a Sofia, a recepcionista virtual da clínica "Odonto-Sorriso". Sua missão é ser proativa, eficiente e humana.
A data de hoje é {current_date}. O fuso horário de referência é 'America/Sao_Paulo'.

### Regras Críticas de Comportamento
1.  **Análise de Histórico OBRIGATÓRIA:** Antes de cada resposta, você DEVE analisar todo o histórico da conversa para entender o contexto. Não pergunte informações que já foram dadas. Se o paciente já se identificou, use o nome dele.
2.  **Início da Conversa:** A primeira mensagem do usuário virá com um status (ex: 'Status do paciente: Paciente encontrado: João Silva. Mensagem: Olá'). Use esse status para uma saudação calorosa e personalizada.
3.  **Fluxo de Agendamento:** Se o paciente quiser agendar, SEMPRE pergunte o **procedimento** ANTES de consultar horários.
4.  **Formato de Resposta:** Responda SEMPRE em JSON, escolhendo uma das `actions` abaixo.

### Informações da Clínica
- **Procedimentos:** Limpeza, Clareamento Dental, Restauração, Tratamento de Canal.
- **Horário:** Segunda a Sexta, 09:00-12:00 e 13:00-18:00.

### Definição das Ferramentas (Actions)
- **responder:** `{"action": "responder", "data": {"texto": "Sua resposta simpática e contextualizada aqui."}}`
- **cadastrar_paciente:** `{"action": "cadastrar_paciente", "data": {"nome": "Nome Completo do Paciente", "data_nascimento": "DD/MM/AAAA"}}`
- **agendar_consulta:** `{"action": "agendar_consulta", "data": {"procedimento": "Nome do Procedimento", "data_hora": "AAAA-MM-DDTHH:MM:SS"}}`
- **consultar_horarios_disponiveis:** `{"action": "consultar_horarios_disponiveis", "data": {"dia": "AAAA-MM-DD"}}` (opcional)
- **consultar_meus_agendamentos:** `{"action": "consultar_meus_agendamentos", "data": {}}` (Use quando o paciente perguntar sobre suas consultas existentes)
""".replace("{current_date}", datetime.now(SAO_PAULO_TZ).date().isoformat())

    messages = [
        {"role": "system", "content": prompt_sistema},
        *dados.historico,
        {"role": "user", "content": dados.mensagem}
    ]

    resposta_ia = await chamar_ia(messages)
    action = resposta_ia.get("action")
    action_data = resposta_ia.get("data", {})

    # <<< MUDANÇA: Adicionado o roteamento para a nova ação >>>
    try:
        if action == "responder":
            return action_data.get("texto", "Não consegui processar sua solicitação.")
        elif action == "cadastrar_paciente":
            return await cadastrar_paciente(dados.telefone_usuario, action_data.get("nome"), action_data.get("data_nascimento"))
        elif action == "agendar_consulta":
            return await agendar_consulta(dados.telefone_usuario, action_data.get("data_hora"), action_data.get("procedimento"))
        elif action == "consultar_horarios_disponiveis":
            return await consultar_horarios_disponiveis(action_data.get("dia"))
        elif action == "consultar_meus_agendamentos":
            return await consultar_meus_agendamentos_por_telefone(dados.telefone_usuario)
        else:
            print(f"Ação desconhecida recebida da IA: {action}")
            return "Não entendi o que preciso fazer. Pode reformular, por favor?"
    except Exception as e:
        print(f"Erro ao executar a ação '{action}': {e}")
        return "Ocorreu um erro interno ao processar sua solicitação. A equipe já foi notificada."


# --- Endpoints da API ---
@app.get("/")
async def root():
    return {"message": "API do Consultório Odonto-Sorriso no ar!"}

@app.head("/")
async def head_root():
    return Response(status_code=200)

@app.post("/whatsapp")
async def receber_mensagem_zapi(request: Request):
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Payload JSON inválido.")

    numero_contato = payload.get("phone")
    if not numero_contato:
        return Response(status_code=200)

    if payload.get("fromMe"):
        snooze_time = datetime.now(timezone.utc) + timedelta(minutes=30)
        query = """
            INSERT INTO historico_conversas (telefone, historico, snoozed_until)
            VALUES (:telefone, :historico, :snoozed_until)
            ON CONFLICT (telefone) DO UPDATE SET snoozed_until = :snoozed_until;
        """
        await database.execute(query, values={"telefone": numero_contato, "historico": "[]", "snoozed_until": snooze_time})
        return Response(status_code=200)

    conteudo_processar = None
    if payload.get("text", {}).get("message"):
        conteudo_processar = payload["text"]["message"]
    elif payload.get("audio", {}).get("audioUrl"):
        audio_bytes = await baixar_audio_bytes(payload["audio"]["audioUrl"])
        conteudo_processar = (await transcrever_audio(audio_bytes) if audio_bytes else None) or "[Erro na transcrição/download do áudio]"
    
    if not conteudo_processar:
        return Response(status_code=200)

    try:
        query_select = historico_conversas.select().where(historico_conversas.c.telefone == numero_contato)
        resultado_db = await database.fetch_one(query_select)
        historico_recuperado = []

        if resultado_db:
            if resultado_db["snoozed_until"] and resultado_db["snoozed_until"] > datetime.now(timezone.utc):
                return Response(status_code=200)
            if datetime.now(timezone.utc) - resultado_db["last_updated_at"] < timedelta(hours=6):
                historico_recuperado = json.loads(resultado_db["historico"])
        
        mensagem_para_ia = conteudo_processar
        if not historico_recuperado:
            status_paciente = await consultar_paciente_por_telefone(numero_contato)
            mensagem_para_ia = f"Status do paciente: {status_paciente}. Mensagem: {conteudo_processar}"
        
        dados_chat = ChatRequest(telefone_usuario=numero_contato, mensagem=mensagem_para_ia, historico=historico_recuperado)
        mensagem_resposta = await processar_chat_logic(dados_chat)
        
        historico_atualizado = historico_recuperado + [
            {"role": "user", "content": conteudo_processar},
            {"role": "assistant", "content": mensagem_resposta}
        ]
        historico_str = json.dumps(historico_atualizado[-20:])

        upsert_query = """
            INSERT INTO historico_conversas (telefone, historico, snoozed_until, last_updated_at)
            VALUES (:telefone, :historico, NULL, NOW())
            ON CONFLICT (telefone) DO UPDATE 
            SET historico = :historico, snoozed_until = NULL, last_updated_at = NOW();
        """
        await database.execute(upsert_query, values={"telefone": numero_contato, "historico": historico_str})

        instance_id, token, client_token = os.getenv("INSTANCE_ID"), os.getenv("TOKEN"), os.getenv("CLIENT_TOKEN")
        zapi_url = f"https://api.z-api.io/instances/{instance_id}/token/{token}/send-text"
        async with httpx.AsyncClient() as client:
            await client.post(
                zapi_url,
                json={"phone": numero_contato, "message": mensagem_resposta},
                headers={"Client-Token": client_token}, 
                timeout=30.0
            )
    except Exception as e:
        print(f"!!! Erro Crítico no Webhook /whatsapp para {numero_contato}: {e} !!!")
    
    return Response(status_code=200)


