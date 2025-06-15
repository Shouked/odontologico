from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
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
app = FastAPI(title="API Consultório Odontológico")

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

# Definição das novas tabelas
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
    Column("paciente_id", UUID(as_uuid=True), nullable=False), # Foreign key logic managed by the app
    Column("data_hora", DateTime(timezone=True), nullable=False),
    Column("procedimento", String(255), nullable=False),
    Column("status", String(50), nullable=False, default='Agendado'), # e.g., Agendado, Realizado, Cancelado
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
class MensagemChat(BaseModel):
    telefone_usuario: str
    mensagem: str
    historico: Optional[List[dict]] = None

# --- Funções de Negócio (Ferramentas da IA) ---

async def consultar_horarios_disponiveis(dia: str) -> str:
    try:
        data_consulta = datetime.strptime(dia, "%Y-%m-%d").date()
    except ValueError:
        return "Formato de data inválido. Por favor, use AAAA-MM-DD."

    if data_consulta < date.today():
        return f"Não é possível agendar em datas passadas. Hoje é {date.today().strftime('%d/%m/%Y')}."

    horarios_base = [f"{h:02d}:00" for h in range(9, 18) if h != 12]
    
    query = agendamentos.select().where(func.date(agendamentos.c.data_hora) == data_consulta)
    agendamentos_existentes = await database.fetch_all(query)
    horarios_ocupados = [a['data_hora'].strftime("%H:%M") for a in agendamentos_existentes]

    horarios_disponiveis = [h for h in horarios_base if h not in horarios_ocupados]
    
    if not horarios_disponiveis:
        return f"Não temos horários disponíveis para o dia {data_consulta.strftime('%d/%m/%Y')}. Por favor, escolha outra data."
    
    return f"Horários disponíveis para {data_consulta.strftime('%d/%m/%Y')}: {', '.join(horarios_disponiveis)}."


async def agendar_consulta(telefone: str, data_hora_str: str, procedimento: str) -> str:
    paciente_query = pacientes.select().where(pacientes.c.telefone == telefone)
    paciente = await database.fetch_one(paciente_query)
    if not paciente:
        return "Paciente não encontrado. Por favor, cadastre-se antes de agendar."

    try:
        data_hora = datetime.fromisoformat(data_hora_str)
    except ValueError:
        return "Formato de data e hora inválido. Use AAAA-MM-DDTHH:MM:SS."

    novo_agendamento = {
        "id": uuid.uuid4(),
        "paciente_id": paciente["id"],
        "data_hora": data_hora,
        "procedimento": procedimento,
        "status": "Agendado"
    }
    query = agendamentos.insert().values(**novo_agendamento)
    await database.execute(query)
    return f"Agendamento confirmado para {procedimento} no dia {data_hora.strftime('%d/%m/%Y às %H:%M')}. Aguardamos você!"

async def cadastrar_paciente(telefone: str, nome: str, data_nascimento_str: Optional[str]) -> str:
    existente = await database.fetch_one(pacientes.select().where(pacientes.c.telefone == telefone))
    if existente:
        return "Você já possui um cadastro conosco."

    data_nascimento = None
    if data_nascimento_str:
        try:
            # **CORREÇÃO**: Tenta primeiro o formato brasileiro (DD/MM/YYYY)
            data_nascimento = datetime.strptime(data_nascimento_str, "%d/%m/%Y").date()
        except ValueError:
            try:
                # Se falhar, tenta o formato ISO (YYYY-MM-DD)
                data_nascimento = datetime.strptime(data_nascimento_str, "%Y-%m-%d").date()
            except ValueError:
                return "Formato de data de nascimento inválido. Por favor, peça ao usuário para fornecer no formato DD/MM/AAAA."

    novo_paciente = {
        "id": uuid.uuid4(),
        "nome": nome,
        "telefone": telefone,
        "data_nascimento": data_nascimento
    }
    query = pacientes.insert().values(**novo_paciente)
    await database.execute(query)
    return f"Cadastro de {nome} realizado com sucesso! Agora já podemos agendar sua consulta."

# --- Funções Auxiliares de IA e Mídia ---

async def chamar_ia(messages: List[dict]) -> dict: # Sempre retornará dict
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
        "Content-Type": "application/json",
        "Referer": os.getenv("PUBLIC_URL") or ""
    }
    body = {
        "model": "openai/gpt-4o",
        "messages": messages,
        "response_format": {"type": "json_object"} # Força a saída em JSON
    }
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            return json.loads(content) 
    except Exception as e:
        print(f"Erro na IA: {e}")
        return {"action": "responder", "data": {"texto": "Desculpe, ocorreu um erro de comunicação com a IA. Tente novamente."}}

async def transcrever_audio(audio_bytes: bytes) -> str | None:
    # Este bloco permanece o mesmo
    return ""

async def baixar_audio_bytes(url: str) -> bytes | None:
    # Este bloco permanece o mesmo
    return b""

# --- Endpoints da API ---

@app.get("/")
async def root():
    return {"message": "API do Consultório Odonto-Sorriso no ar!"}

@app.head("/")
async def head_root():
    return Response(status_code=200)

@app.post("/chat")
async def chat(dados: MensagemChat):
    historico = dados.historico or []

    prompt_sistema = """
### Papel e Objetivo
Você é a Sofia, a assistente virtual da clínica "Odonto-Sorriso". Seu objetivo é ser a recepcionista perfeita: tirar dúvidas, qualificar pacientes, verificar se já são cadastrados e agendar consultas. A data de hoje é {current_date}.

### Informações da Clínica
- **Horário de Funcionamento:** Segunda a Sexta, das 09:00 às 18:00. Não abrimos aos finais de semana.
- **Procedimentos:** Limpeza, Clareamento Dental, Restauração, Tratamento de Canal.
- **Duração dos Procedimentos:** Todos os procedimentos levam 1 hora e começam em horários cheios (09:00, 10:00, 11:00, 13:00, etc.).

### Regras de Conversa
1.  **Identificação:** Sempre verifique se o paciente já é cadastrado. Se não for, a primeira coisa a fazer é o cadastro.
2.  **Coleta de Dados:** NUNCA chame a ferramenta `cadastrar_paciente` sem ter o NOME e a DATA DE NASCIMENTO. Se faltar algum, peça ao usuário o que falta. Por exemplo, se tem o nome mas não a data, peça a data.
3.  **Clareza:** Peça UMA informação de cada vez.
4.  **Ferramentas (Actions):** Sua principal forma de interagir com o sistema é retornando um objeto JSON com uma "action" e os "data" necessários. NUNCA invente informações. Se precisar de algo, use uma ferramenta.

### Definição das Ferramentas (Actions)
Você DEVE responder usando um dos seguintes formatos JSON:

1.  **Para responder ao usuário:**
    ```json
    {
      "action": "responder",
      "data": {
        "texto": "Sua resposta aqui."
      }
    }
    ```

2.  **Para cadastrar um novo paciente (use apenas quando tiver NOME e DATA DE NASCIMENTO):**
    ```json
    {
      "action": "cadastrar_paciente",
      "data": {
        "nome": "Nome Completo do Paciente",
        "data_nascimento": "AAAA-MM-DD"
      }
    }
    ```

3.  **Para agendar uma consulta:**
    ```json
    {
      "action": "agendar_consulta",
      "data": {
        "procedimento": "Nome do Procedimento",
        "data_hora": "AAAA-MM-DDTHH:MM:SS"
      }
    }
    ```

4.  **Para verificar horários disponíveis:**
    ```json
    {
      "action": "consultar_horarios_disponiveis",
      "data": {
        "dia": "AAAA-MM-DD"
      }
    }
    ```
""".replace("{current_date}", date.today().isoformat())

    messages = [{"role": "system", "content": prompt_sistema}]
    messages.extend(historico)
    messages.append({"role": "user", "content": dados.mensagem})

    resposta_ia = await chamar_ia(messages)

    action = resposta_ia.get("action")
    action_data = resposta_ia.get("data", {})
    
    if action == "responder":
        return {"reply": action_data.get("texto", "Ocorreu um erro ao gerar a resposta.")}
    
    elif action == "cadastrar_paciente":
        nome = action_data.get("nome")
        data_nasc_str = action_data.get("data_nascimento")
        if not nome:
            resposta_ferramenta = "A IA tentou cadastrar sem um nome. Peça o nome ao usuário."
        else:
            resposta_ferramenta = await cadastrar_paciente(dados.telefone_usuario, nome, data_nasc_str)

    elif action == "agendar_consulta":
        resposta_ferramenta = await agendar_consulta(dados.telefone_usuario, action_data.get("data_hora"), action_data.get("procedimento"))
    elif action == "consultar_horarios_disponiveis":
        resposta_ferramenta = await consultar_horarios_disponiveis(action_data.get("dia"))
    else:
        resposta_ferramenta = "Ação desconhecida. Por favor, reformule seu pedido."
    
    messages.append({"role": "assistant", "content": json.dumps(resposta_ia)})
    messages.append({"role": "tool", "content": resposta_ferramenta})

    final_response_ia = await chamar_ia(messages)
    return {"reply": final_response_ia.get("data", {}).get("texto", "Não consegui processar a resposta final.")}


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
        return {"status": "ok", "message": "Modo manual ativado."}

    texto_da_mensagem = payload.get("text", {}).get("message")
    audio_url = payload.get("audio", {}).get("audioUrl")
    conteudo_processar = None

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
        
        async with httpx.AsyncClient() as client:
            public_url = os.getenv("PUBLIC_URL")
            resposta_chat = await client.post(
                 f"{public_url.rstrip('/')}/chat",
                 json={"telefone_usuario": numero_contato, "mensagem": conteudo_processar, "historico": historico_recuperado},
                 timeout=120.0
            )
            resposta_chat.raise_for_status()
            
            dados = resposta_chat.json()
            mensagem_resposta = dados.get("reply", "Não consegui gerar uma resposta.")

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
