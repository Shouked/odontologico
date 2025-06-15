# main.py
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List
from databases import Database
from sqlalchemy import (
    create_engine, MetaData, Table, Column, String, Text,
    DateTime, Date, func, ForeignKey
)
from sqlalchemy.dialects.postgresql import UUID
import uuid, json, httpx, os, pytz
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta, date
from openai import AsyncOpenAI

# ─────────────────── Configuração básica ─────────────────── #
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL não foi definida nas variáveis de ambiente.")

SAO_PAULO_TZ = pytz.timezone("America/Sao_Paulo")

# ─────────────────── Inicialização FastAPI ─────────────────── #
app = FastAPI(title="API Consultório Odontológico Avançada")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────── Configuração do Banco ─────────────────── #
database = Database(DATABASE_URL)
metadata = MetaData()

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

# ─────────────────── Ciclo de vida ─────────────────── #
@app.on_event("startup")
async def startup():
    await database.connect()

@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()

# ─────────────────── Modelos ─────────────────── #
class ChatRequest(BaseModel):
    telefone_usuario: str
    mensagem: str
    historico: List[dict] = Field(default_factory=list)

# ─────────────────── Funções de negócio ─────────────────── #
async def consultar_paciente_por_telefone(telefone: str) -> str:
    query = pacientes.select().where(pacientes.c.telefone == telefone)
    paciente = await database.fetch_one(query)
    return f"Paciente encontrado: {paciente['nome']}." if paciente else "Paciente não cadastrado."

async def cadastrar_paciente(telefone: str, nome: str, data_nascimento_str: Optional[str]) -> str:
    existente = await database.fetch_one(pacientes.select().where(pacientes.c.telefone == telefone))
    if existente:
        return "Você já possui um cadastro conosco."
    data_nascimento = None
    if data_nascimento_str:
        try:
            data_nascimento = datetime.strptime(data_nascimento_str, "%d/%m/%Y").date()
        except ValueError:
            try:
                data_nascimento = datetime.strptime(data_nascimento_str, "%Y-%m-%d").date()
            except ValueError:
                return "Formato de data inválido. Peça para o usuário fornecer no formato DD/MM/AAAA."
    novo_paciente = {
        "id": uuid.uuid4(),
        "nome": nome,
        "telefone": telefone,
        "data_nascimento": data_nascimento
    }
    await database.execute(pacientes.insert().values(**novo_paciente))
    primeiro_nome = nome.split(' ')[0]
    return f"Ótimo, {primeiro_nome}! Seu cadastro foi realizado. Agora já podemos agendar sua consulta. Qual procedimento você gostaria?"

async def consultar_horarios_disponiveis(dia_preferencial_str: Optional[str] = None) -> str:
    hoje_sp = datetime.now(SAO_PAULO_TZ).date()
    data_inicial = hoje_sp
    if dia_preferencial_str:
        try:
            data_preferencial = datetime.strptime(dia_preferencial_str, "%Y-%m-%d").date()
            if data_preferencial < hoje_sp:
                return f"Não é possível agendar em datas passadas. A data de hoje é {hoje_sp.strftime('%d/%m/%Y')}."
            data_inicial = data_preferencial
        except ValueError:
            return "Formato de data inválido. Use AAAA-MM-DD."
    for i in range(30):
        data_consulta = data_inicial + timedelta(days=i)
        if data_consulta.weekday() >= 5:
            continue  # pula finais de semana
        horarios_base = [f"{h:02d}:00" for h in range(9, 18) if h != 12]
        inicio_dia_utc = SAO_PAULO_TZ.localize(datetime.combine(data_consulta, datetime.min.time())).astimezone(timezone.utc)
        fim_dia_utc = SAO_PAULO_TZ.localize(datetime.combine(data_consulta, datetime.max.time())).astimezone(timezone.utc)
        query = agendamentos.select().where(
            (agendamentos.c.data_hora >= inicio_dia_utc) &
            (agendamentos.c.data_hora <= fim_dia_utc) &
            (agendamentos.c.status != 'Cancelado')
        )
        ag_existentes = await database.fetch_all(query)
        ocupados = {a['data_hora'].astimezone(SAO_PAULO_TZ).strftime("%H:%M") for a in ag_existentes}
        livres = [h for h in horarios_base if h not in ocupados]
        if livres:
            return f"Encontrei horários para o dia {data_consulta.strftime('%d/%m/%Y')}: {', '.join(livres)}."
    return "Não encontrei horários disponíveis nos próximos 30 dias."

async def agendar_consulta(telefone: str, data_hora_str: str, procedimento: str) -> str:
    paciente = await database.fetch_one(pacientes.select().where(pacientes.c.telefone == telefone))
    if not paciente:
        return "Não encontrei seu cadastro. Por favor, informe seu nome completo para começarmos."
    try:
        dt_local = SAO_PAULO_TZ.localize(datetime.fromisoformat(data_hora_str))
        dt_utc = dt_local.astimezone(timezone.utc)
    except Exception:
        return "Formato de data e hora inválido ou ambíguo. Use AAAA-MM-DDTHH:MM:SS."
    novo_agendamento = {
        "id": uuid.uuid4(),
        "paciente_id": paciente["id"],
        "data_hora": dt_utc,
        "procedimento": procedimento,
        "status": "Agendado"
    }
    await database.execute(agendamentos.insert().values(**novo_agendamento))
    primeiro_nome = paciente['nome'].split(' ')[0]
    return f"Perfeito, {primeiro_nome}! Seu agendamento para {procedimento} no dia {dt_local.strftime('%d/%m/%Y às %H:%M')} foi confirmado."

async def consultar_meus_agendamentos_por_telefone(telefone: str) -> str:
    paciente = await database.fetch_one(pacientes.select().where(pacientes.c.telefone == telefone))
    if not paciente:
        return "Não encontrei seu cadastro. Para que eu possa verificar seus agendamentos, você precisa estar cadastrado. Gostaria de se cadastrar?"
    ag_query = agendamentos.select().where(
        (agendamentos.c.paciente_id == paciente["id"]) &
        (agendamentos.c.data_hora >= func.now()) &
        (agendamentos.c.status == 'Agendado')
    ).order_by(agendamentos.c.data_hora)
    futuros = await database.fetch_all(ag_query)
    if not futuros:
        primeiro_nome = paciente['nome'].split(' ')[0]
        return f"Olá, {primeiro_nome}! Verifiquei aqui e você não possui agendamentos futuros conosco."
    linhas = []
    for ag in futuros:
        dt_local = ag['data_hora'].astimezone(SAO_PAULO_TZ)
        linhas.append(f"- {ag['procedimento']} no dia {dt_local.strftime('%d/%m/%Y às %H:%M')}")
    primeiro_nome = paciente['nome'].split(' ')[0]
    return f"Olá, {primeiro_nome}! Encontrei os seguintes agendamentos no seu nome:\n" + "\n".join(linhas)

# ─────────────────── Funções auxiliares de IA ─────────────────── #
async def chamar_ia(messages: List[dict]) -> dict:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
        "Content-Type": "application/json"
    }
    body = {
        "model": "openai/gpt-4o",
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            content_str = resp.json()["choices"][0]["message"]["content"]
            return json.loads(content_str)
    except (httpx.HTTPStatusError, json.JSONDecodeError, KeyError) as e:
        print(f"Erro na IA: {e}")
        return {
            "action": "responder",
            "data": {"texto": "Desculpe, estou com dificuldades técnicas. Tente novamente em instantes."},
        }

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
        async with httpx.AsyncClient(timeout=30) as client:
            headers = {"Client-Token": os.getenv("CLIENT_TOKEN")}
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            return r.content
    except Exception as e:
        print(f"Erro ao baixar áudio: {e}")
        return None

# ─────────────────── Lógica de chat ─────────────────── #
async def processar_chat_logic(dados: ChatRequest) -> str:
    prompt_sistema = f"""
### Papel e Objetivo
Você é a Sofia, a recepcionista virtual da clínica "Odonto-Sorriso". Sua missão é ser proativa, eficiente e humana.
A data de hoje é {datetime.now(SAO_PAULO_TZ).date().isoformat()}. O fuso horário de referência é 'America/Sao_Paulo'.

### Regras Críticas de Comportamento
1. **Análise de Histórico OBRIGATÓRIA:** Antes de cada resposta, analise todo o histórico da conversa para entender o contexto. Não pergunte informações que já foram dadas.
2. **Início da Conversa:** A primeira mensagem do usuário virá com um status. Use esse status para uma saudação calorosa e personalizada.
3. **Fluxo de Agendamento:** Se o paciente quiser agendar, SEMPRE pergunte o procedimento ANTES de consultar horários.
4. **Formato de Resposta:** Responda SEMPRE em JSON, usando uma das actions definidas.

### Informações da Clínica
- Procedimentos: Limpeza, Clareamento Dental, Restauração, Tratamento de Canal.
- Horário: Segunda a Sexta, 09:00-12:00 e 13:00-18:00.

### Definição das Ferramentas (Actions)
- responder: {"action": "responder", "data": {"texto": "..."}}
- cadastrar_paciente: {"action": "cadastrar_paciente", "data": {"nome": "Nome", "data_nascimento": "DD/MM/AAAA"}}
- agendar_consulta: {"action": "agendar_consulta", "data": {"procedimento": "X", "data_hora": "AAAA-MM-DDTHH:MM:SS"}}
- consultar_horarios_disponiveis: {"action": "consultar_horarios_disponiveis", "data": {"dia": "AAAA-MM-DD"}}
- consultar_meus_agendamentos: {"action": "consultar_meus_agendamentos", "data": {}}
"""
    messages = [
        {"role": "system", "content": prompt_sistema},
        *dados.historico,
        {"role": "user", "content": dados.mensagem},
    ]
    resposta_ia = await chamar_ia(messages)
    action = resposta_ia.get("action")
    data = resposta_ia.get("data", {})
    try:
        if action == "responder":
            return data.get("texto", "Não consegui processar sua solicitação.")
        if action == "cadastrar_paciente":
            return await cadastrar_paciente(
                dados.telefone_usuario,
                data.get("nome"),
                data.get("data_nascimento"),
            )
        if action == "agendar_consulta":
            return await agendar_consulta(
                dados.telefone_usuario,
                data.get("data_hora"),
                data.get("procedimento"),
            )
        if action == "consultar_horarios_disponiveis":
            return await consultar_horarios_disponiveis(data.get("dia"))
        if action == "consultarMeusAgendamentos" or action == "consultar_meus_agendamentos":
            return await consultar_meus_agendamentos_por_telefone(dados.telefone_usuario)
        print(f"Ação desconhecida: {action}")
        return "Não entendi o que preciso fazer. Pode reformular, por favor?"
    except Exception as e:
        print(f"Erro na ação '{action}': {e}")
        return "Ocorreu um erro interno ao processar sua solicitação."

# ─────────────────── Endpoints ─────────────────── #
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

    numero = payload.get("phone")
    if not numero:
        return Response(status_code=200)

    # Ignora mensagens enviadas por nós mesmos
    if payload.get("fromMe"):
        snooze = datetime.now(timezone.utc) + timedelta(minutes=30)
        query = """
            INSERT INTO historico_conversas (telefone, historico, snoozed_until)
            VALUES (:telefone, :historico, :snoozed)
            ON CONFLICT (telefone) DO UPDATE
            SET snoozed_until = :snoozed;
        """
        await database.execute(query, {"telefone": numero, "historico": "[]", "snoozed": snooze})
        return Response(status_code=200)

    conteudo = None
    if payload.get("text", {}).get("message"):
        conteudo = payload["text"]["message"]
    elif payload.get("audio", {}).get("audioUrl"):
        audio_bytes = await baixar_audio_bytes(payload["audio"]["audioUrl"])
        conteudo = await transcrever_audio(audio_bytes) if audio_bytes else None

    if not conteudo:
        return Response(status_code=200)

    try:
        # Recupera histórico se não estiver em snooze
        registro = await database.fetch_one(
            historico_conversas.select().where(historico_conversas.c.telefone == numero)
        )
        historico_recuperado = []
        if registro:
            if registro["snoozed_until"] and registro["snoozed_until"] > datetime.now(timezone.utc):
                return Response(status_code=200)
            if datetime.now(timezone.utc) - registro["last_updated_at"] < timedelta(hours=6):
                historico_recuperado = json.loads(registro["historico"])

        mensagem_para_ia = conteudo
        if not historico_recuperado:
            status_paciente = await consultar_paciente_por_telefone(numero)
            mensagem_para_ia = f"Status do paciente: {status_paciente}. Mensagem: {conteudo}"

        dados_chat = ChatRequest(
            telefone_usuario=numero,
            mensagem=mensagem_para_ia,
            historico=historico_recuperado,
        )
        resposta = await processar_chat_logic(dados_chat)

        historico_atualizado = historico_recuperado + [
            {"role": "user", "content": conteudo},
            {"role": "assistant", "content": resposta},
        ]
        historico_str = json.dumps(historico_atualizado[-20:], ensure_ascii=False)

        upsert = """
            INSERT INTO historico_conversas (telefone, historico, snoozed_until, last_updated_at)
            VALUES (:tel, :hist, NULL, NOW())
            ON CONFLICT (telefone) DO UPDATE
            SET historico = :hist,
                snoozed_until = NULL,
                last_updated_at = NOW();
        """
        await database.execute(upsert, {"tel": numero, "hist": historico_str})

        # ───── Envia resposta para WhatsApp (Z-API) ───── #
        instance_id = os.getenv("INSTANCE_ID")
        token = os.getenv("TOKEN")
        client_token = os.getenv("CLIENT_TOKEN")
        if not all([instance_id, token, client_token]):
            print("Variáveis de ambiente da Z-API ausentes.")
            return Response(status_code=200)

        zapi_url = f"https://api.z-api.io/instances/{instance_id}/token/{token}/send-text"
        payload_zapi = {"phone": numero, "message": resposta}
        headers = {
            "Client-Token": client_token,
            "Content-Type": "application/json; charset=utf-8",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(zapi_url, json=payload_zapi, headers=headers)

    except Exception as e:
        print(f"Erro crítico no webhook /whatsapp para {numero}: {e}")

    return Response(status_code=200)