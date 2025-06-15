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
class MensagemChat(BaseModel):
    telefone_usuario: str
    mensagem: str
    historico: Optional[List[dict]] = None

# --- Funções de Negócio (Ferramentas da IA) ---

# **NOVO**: Ferramenta para buscar paciente pelo telefone
async def consultar_paciente_por_telefone(telefone: str) -> str:
    """Consulta se um paciente já existe no banco de dados usando o número de telefone."""
    query = pacientes.select().where(pacientes.c.telefone == telefone)
    paciente = await database.fetch_one(query)
    if paciente:
        # Retorna o nome do paciente para a IA usar na conversa
        return f"Paciente encontrado: {paciente['nome']}."
    return "Paciente não cadastrado."

# **ALTERADO**: Função agora busca o próximo dia útil com horários disponíveis
async def consultar_horarios_disponiveis(dia_preferencial_str: Optional[str] = None) -> str:
    """
    Verifica os horários disponíveis. Se um dia for fornecido, verifica nesse dia.
    Caso contrário, encontra o próximo dia útil com vagas a partir de hoje.
    """
    data_inicial = date.today()
    if dia_preferencial_str:
        try:
            data_inicial = datetime.strptime(dia_preferencial_str, "%Y-%m-%d").date()
            if data_inicial < date.today():
                return f"Não é possível agendar em datas passadas. A data mais próxima é hoje, {date.today().strftime('%d/%m/%Y')}."
        except ValueError:
            return "Formato de data inválido. Use AAAA-MM-DD."

    # Loop para encontrar o próximo dia com vagas
    for i in range(30): # Procura nos próximos 30 dias
        data_consulta = data_inicial + timedelta(days=i)
        
        # Pula finais de semana
        if data_consulta.weekday() >= 5: # 5 = Sábado, 6 = Domingo
            continue

        horarios_base = [f"{h:02d}:00" for h in range(9, 18) if h != 12]
        query = agendamentos.select().where(func.date(agendamentos.c.data_hora) == data_consulta)
        agendamentos_existentes = await database.fetch_all(query)
        horarios_ocupados = [a['data_hora'].strftime("%H:%M") for a in agendamentos_existentes]
        horarios_disponiveis = [h for h in horarios_base if h not in horarios_ocupados]

        if horarios_disponiveis:
            return f"Encontrei horários para o dia {data_consulta.strftime('%d/%m/%Y')}. Os horários disponíveis são: {', '.join(horarios_disponiveis)}."
    
    return "Não encontrei horários disponíveis nos próximos 30 dias. Por favor, entre em contato com a clínica."

async def agendar_consulta(telefone: str, data_hora_str: str, procedimento: str) -> str:
    # ... (código existente sem alterações)
    return ""

async def cadastrar_paciente(telefone: str, nome: str, data_nascimento_str: Optional[str]) -> str:
    # ... (código existente sem alterações)
    return ""

# --- Funções Auxiliares de IA e Mídia ---

async def chamar_ia(messages: List[dict]) -> dict:
    # ... (código existente sem alterações)
    return {}

async def transcrever_audio(audio_bytes: bytes) -> str | None:
    # ... (código existente sem alterações)
    return ""

async def baixar_audio_bytes(url: str) -> bytes | None:
    # ... (código existente sem alterações)
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

    # **PROMPT COMPLETAMENTE REESCRITO**
    prompt_sistema = """
### Papel e Objetivo
Você é a Sofia, a assistente virtual da clínica "Odonto-Sorriso". Sua missão é ser uma recepcionista eficiente, guiando o paciente de forma lógica e natural. A data de hoje é {current_date}.

### Informações da Clínica
- **Procedimentos:** Limpeza, Clareamento Dental, Restauração, Tratamento de Canal.
- **Horário:** Segunda a Sexta, 09:00-12:00 e 13:00-18:00.

### Fluxo Obrigatório de Conversa
Siga ESTE fluxo, sem pular etapas.

1.  **Boas-vindas e Identificação:**
    a. A conversa SEMPRE começa com você usando a ferramenta `consultar_paciente_por_telefone`.
    b. **Se o paciente for encontrado:** Cumprimente-o pelo nome (Ex: "Olá, Iago! Bem-vindo de volta à Odonto-Sorriso. Como posso ajudar?").
    c. **Se não for encontrado:** Inicie o cadastro (Ex: "Olá! Bem-vindo à Odonto-Sorriso. Para começarmos, qual seu nome completo?").

2.  **Fluxo de Agendamento (SÓ INICIE APÓS A IDENTIFICAÇÃO):**
    a. **Passo 1: Procedimento:** Primeiro, pergunte QUAL o procedimento desejado. (Ex: "Qual procedimento você gostaria de agendar?").
    b. **Passo 2: Consultar Vagas:** APÓS saber o procedimento, use a ferramenta `consultar_horarios_disponiveis` SEM passar um dia. Ela te retornará a data mais próxima com vagas.
    c. **Passo 3: Oferecer Horários:** Apresente os horários encontrados ao paciente (Ex: "Perfeito. Encontrei vagas para o dia X. Os horários são Y e Z. Algum deles funciona para você?").
    d. **Passo 4: Confirmar:** Se o paciente escolher um horário, confirme o agendamento usando a ferramenta `agendar_consulta`. Se ele pedir outro dia, use a ferramenta `consultar_horarios_disponiveis` novamente, mas desta vez com o dia que ele pediu.

### Definição das Ferramentas (Actions)
Responda SEMPRE em JSON, usando uma das actions abaixo.

1.  **Para responder ao usuário:**
    ```json
    {
      "action": "responder",
      "data": {
        "texto": "Sua resposta aqui."
      }
    }
    ```
2.  **Para consultar um paciente existente (PRIMEIRA AÇÃO DA CONVERSA):**
    ```json
    {
        "action": "consultar_paciente_por_telefone",
        "data": null
    }
    ```
3.  **Para cadastrar um novo paciente (use apenas quando tiver NOME e DATA DE NASCIMENTO):**
    ```json
    {
      "action": "cadastrar_paciente",
      "data": {
        "nome": "Nome Completo",
        "data_nascimento": "DD/MM/AAAA"
      }
    }
    ```
4.  **Para agendar uma consulta:**
    ```json
    {
      "action": "agendar_consulta",
      "data": {
        "procedimento": "Nome do Procedimento",
        "data_hora": "AAAA-MM-DDTHH:MM:SS"
      }
    }
    ```
5.  **Para verificar horários (se precisar de um dia específico, passe o parâmetro "dia"):**
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
    # Se o histórico estiver vazio, a primeira ação é consultar o paciente
    if not historico:
        messages.append({"role": "user", "content": "Início da conversa."})
    else:
        messages.extend(historico)
        messages.append({"role": "user", "content": dados.mensagem})

    resposta_ia = await chamar_ia(messages)

    action = resposta_ia.get("action")
    action_data = resposta_ia.get("data", {})
    
    # Roteador de Ações
    if action == "responder":
        return {"reply": action_data.get("texto", "Ocorreu um erro.")}
    
    elif action == "consultar_paciente_por_telefone":
        # Ferramenta especial que re-chama a IA para ela poder se apresentar
        resultado_ferramenta = await consultar_paciente_por_telefone(dados.telefone_usuario)
        messages.append({"role": "assistant", "content": json.dumps(resposta_ia)})
        messages.append({"role": "tool", "content": resultado_ferramenta})
        resposta_final = await chamar_ia(messages)
        return {"reply": resposta_final.get("data", {}).get("texto")}

    elif action == "cadastrar_paciente":
        resposta_ferramenta = await cadastrar_paciente(dados.telefone_usuario, action_data.get("nome"), action_data.get("data_nascimento"))
        return {"reply": resposta_ferramenta}

    elif action == "agendar_consulta":
        resposta_ferramenta = await agendar_consulta(dados.telefone_usuario, action_data.get("data_hora"), action_data.get("procedimento"))
        return {"reply": resposta_ferramenta}

    elif action == "consultar_horarios_disponiveis":
        resposta_ferramenta = await consultar_horarios_disponiveis(action_data.get("dia"))
        return {"reply": resposta_ferramenta}
    
    else:
        return {"reply": "Ação desconhecida."}

@app.post("/whatsapp")
async def receber_mensagem_zapi(request: Request):
    # O código deste endpoint permanece o mesmo da versão anterior, pois a lógica
    # principal foi centralizada no endpoint /chat.
    # ... (código existente sem alterações)
    return {"status": "ok"}
