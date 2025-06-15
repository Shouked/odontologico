# main.py
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List
from databases import Database
from sqlalchemy import create_engine, MetaData, Table, Column, String, Text, DateTime, Date, func, ForeignKey # <<< MUDANÇA: Importado ForeignKey
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

# Define o fuso horário de São Paulo para consistência
SAO_PAULO_TZ = pytz.timezone("America/Sao_Paulo")

# Inicialização do FastAPI
app = FastAPI(title="API Consultório Odontológico Avançada")

# Configuração do CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Em produção, restrinja para os domínios necessários
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Configuração do Banco de Dados ---
database = Database(DATABASE_URL)
metadata = MetaData()

# Definição das tabelas
pacientes = Table(
    "pacientes",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
    Column("nome", String(255), nullable=False),
    Column("telefone", String(255), unique=True, nullable=False, index=True), # <<< MUDANÇA: Adicionado index para performance
    Column("data_nascimento", Date, nullable=True),
)

agendamentos = Table(
    "agendamentos",
    metadata,
    # <<< MUDANÇA: Adicionada a ForeignKey para garantir integridade dos dados
    Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
    Column("paciente_id", UUID(as_uuid=True), ForeignKey("pacientes.id"), nullable=False),
    Column("data_hora", DateTime(timezone=True), nullable=False, index=True), # <<< MUDANÇA: Adicionado index para performance
    Column("procedimento", String(255), nullable=False),
    Column("status", String(50), nullable=False, default='Agendado'),
)

historico_conversas = Table(
    "historico_conversas",
    metadata,
    Column("telefone", String(255), primary_key=True, index=True),
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
    # <<< MUDANÇA: Usando Field(default_factory=list) que é a forma mais segura para padrões mutáveis
    historico: List[dict] = Field(default_factory=list)

# --- Funções de Ferramenta da IA (Lógica Revisada) ---

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

# <<< MUDANÇA: Função completamente reescrita para ser mais robusta e eficiente
async def consultar_horarios_disponiveis(dia_preferencial_str: Optional[str] = None) -> str:
    """
    Consulta horários disponíveis com tratamento de timezone robusto e consulta ao banco otimizada.
    """
    # Define a data de início da busca no fuso horário de São Paulo
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

    # Loop para buscar por um dia com horários nos próximos 30 dias
    for i in range(30):
        data_consulta = data_inicial + timedelta(days=i)
        
        # Pula fins de semana (Sábado = 5, Domingo = 6)
        if data_consulta.weekday() >= 5:
            continue
            
        # Horários de funcionamento padrão da clínica
        horarios_base = [f"{h:02d}:00" for h in range(9, 18) if h != 12] # 09:00, 10:00, 11:00, 13:00 ... 17:00

        # Define o início e o fim do dia em UTC para a consulta
        inicio_dia_utc = SAO_PAULO_TZ.localize(datetime.combine(data_consulta, datetime.min.time())).astimezone(timezone.utc)
        fim_dia_utc = SAO_PAULO_TZ.localize(datetime.combine(data_consulta, datetime.max.time())).astimezone(timezone.utc)

        # Consulta otimizada usando um range de data/hora
        query = agendamentos.select().where(
            (agendamentos.c.data_hora >= inicio_dia_utc) &
            (agendamentos.c.data_hora <= fim_dia_utc) &
            (agendamentos.c.status != 'Cancelado')
        )
        agendamentos_existentes = await database.fetch_all(query)
        
        # Converte os horários ocupados para o fuso horário de São Paulo para comparação
        horarios_ocupados = {a['data_hora'].astimezone(SAO_PAULO_TZ).strftime("%H:%M") for a in agendamentos_existentes}
        
        horarios_disponiveis = [h for h in horarios_base if h not in horarios_ocupados]
        
        if horarios_disponiveis:
            return f"Encontrei horários para o dia {data_consulta.strftime('%d/%m/%Y')}: {', '.join(horarios_disponiveis)}."
            
    return "Não encontrei horários disponíveis nos próximos 30 dias. Por favor, entre em contato para verificarmos outras possibilidades."

async def agendar_consulta(telefone: str, data_hora_str: str, procedimento: str) -> str:
    paciente_query = pacientes.select().where(pacientes.c.telefone == telefone)
    paciente = await database.fetch_one(paciente_query)
    if not paciente: return "Não encontrei seu cadastro. Por favor, informe seu nome completo para começarmos."
    try:
        # A IA deve fornecer o horário no formato 'AAAA-MM-DDTHH:MM:SS'
        # Convertemos para um objeto datetime ciente do fuso horário de São Paulo e depois para UTC para salvar
        dt_obj_local = SAO_PAULO_TZ.localize(datetime.fromisoformat(data_hora_str))
        data_hora_utc = dt_obj_local.astimezone(timezone.utc)
    except (ValueError, pytz.exceptions.AmbiguousTimeError, pytz.exceptions.NonExistentTimeError) as e:
        print(f"Erro de conversão de data: {e}")
        return "Formato de data e hora inválido ou ambíguo. Use AAAA-MM-DDTHH:MM:SS."
    
    novo_agendamento = {"id": uuid.uuid4(), "paciente_id": paciente["id"], "data_hora": data_hora_utc, "procedimento": procedimento, "status": "Agendado"}
    query = agendamentos.insert().values(**novo_agendamento)
    await database.execute(query)
    return f"Perfeito, {paciente['nome'].split(' ')[0]}! Seu agendamento para {procedimento} no dia {dt_obj_local.strftime('%d/%m/%Y às %H:%M')} foi confirmado."

# --- Funções Auxiliares de IA e Mídia ---

# <<< MUDANÇA: Adicionado tratamento de erro para o JSON retornado pela IA
async def chamar_ia(messages: List[dict]) -> dict:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
        "Content-Type": "application/json",
    }
    body = {
        "model": "openai/gpt-4o",
        "messages": messages,
        "response_format": {"type": "json_object"}
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            content_str = data["choices"][0]["message"]["content"]
            
            # Tenta converter o conteúdo da string para um dicionário Python
            try:
                return json.loads(content_str)
            except json.JSONDecodeError:
                print(f"Erro de decodificação JSON da IA. Resposta recebida: {content_str}")
                return {"action": "responder", "data": {"texto": "Desculpe, tive um problema para processar a resposta. Pode tentar de novo?"}}

    except httpx.HTTPStatusError as e:
        print(f"Erro de status HTTP na IA: {e.response.status_code} - {e.response.text}")
    except Exception as e:
        print(f"Erro genérico na IA: {e}")
    
    # Resposta padrão em caso de qualquer falha na comunicação ou processamento
    return {"action": "responder", "data": {"texto": "Desculpe, estou com dificuldades técnicas no momento. Tente novamente mais tarde."}}


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
async def processar_chat_logic(dados: ChatRequest):
    historico = dados.historico
    prompt_sistema = """
### Papel e Objetivo
Você é a Sofia, a recepcionista virtual da clínica "Odonto-Sorriso". Sua missão é ser proativa, eficiente e seguir o fluxo de conversa à risca. A data de hoje é {current_date}. Use sempre o timezone 'America/Sao_Paulo' como referência.

### Informações da Clínica
- **Procedimentos:** Limpeza, Clareamento Dental, Restauração, Tratamento de Canal.
- **Horário:** Segunda a Sexta, 09:00-12:00 e 13:00-18:00.

### Fluxo Obrigatório de Conversa
1.  **Início da Conversa:** Você receberá o status do paciente ("Paciente encontrado: [Nome]" ou "Paciente não cadastrado") como um resultado de ferramenta. Use essa informação para saudar o paciente de forma personalizada e iniciar a conversa.
2.  **Agendamento:** Se o paciente quiser agendar, SEMPRE pergunte o procedimento ANTES de consultar horários. Depois, use a ferramenta `consultar_horarios_disponiveis` para encontrar o próximo dia com vagas.
3.  **Ferramentas:** Use as ferramentas disponíveis para interagir com o sistema. Responda sempre em JSON. Para `agendar_consulta`, o campo `data_hora` deve ser no formato `AAAA-MM-DDTHH:MM:SS`.

### Definição das Ferramentas (Actions)
1.  **responder:** `{"action": "responder", "data": {"texto": "Sua resposta aqui."}}`
2.  **cadastrar_paciente:** `{"action": "cadastrar_paciente", "data": {"nome": "Nome Completo", "data_nascimento": "DD/MM/AAAA"}}`
3.  **agendar_consulta:** `{"action": "agendar_consulta", "data": {"procedimento": "Nome do Procedimento", "data_hora": "AAAA-MM-DDTHH:MM:SS"}}`
4.  **consultar_horarios_disponiveis:** `{"action": "consultar_horarios_disponiveis", "data": {"dia": "AAAA-MM-DD"}}` (opcional)
""".replace("{current_date}", datetime.now(SAO_PAULO_TZ).date().isoformat())

    messages = [{"role": "system", "content": prompt_sistema}]
    
    # <<< MUDANÇA: Lógica de histórico simplificada e mais segura
    # Adiciona o histórico recuperado do banco de dados
    if historico:
        messages.extend(historico)
    
    # Adiciona a mensagem atual do usuário
    messages.append({"role": "user", "content": dados.mensagem})

    # Se a conversa está começando (sem histórico), consulta o status do paciente
    if not historico:
        resultado_consulta = await consultar_paciente_por_telefone(dados.telefone_usuario)
        # <<< MUDANÇA: O resultado da ferramenta é adicionado no formato correto para a IA entender.
        tool_call_id = str(uuid.uuid4()) # Gera um ID para o 'tool_call'
        messages.insert(1, {"role": "assistant", "content": None, "tool_calls": [{"id": tool_call_id, "type": "function", "function": {"name": "consultar_paciente_por_telefone", "arguments": f'{{"telefone": "{dados.telefone_usuario}"}}'}}]})
        messages.insert(2, {"role": "tool", "tool_call_id": tool_call_id, "name": "consultar_paciente_por_telefone", "content": resultado_consulta})

    resposta_ia = await chamar_ia(messages)
    action = resposta_ia.get("action")
    action_data = resposta_ia.get("data", {})
    
    # Armazena a decisão da IA (a chamada de action) no histórico
    historico_para_salvar = list(messages) # Copia as mensagens
    historico_para_salvar.append({"role": "assistant", "content": json.dumps(resposta_ia)})

    # Roteador de Ações
    try:
        if action == "responder":
            resultado_final = {"reply": action_data.get("texto", "Não consegui processar sua solicitação."), "history": historico_para_salvar}
        elif action == "cadastrar_paciente":
            resultado_final = {"reply": await cadastrar_paciente(dados.telefone_usuario, action_data.get("nome"), action_data.get("data_nascimento")), "history": historico_para_salvar}
        elif action == "agendar_consulta":
            resultado_final = {"reply": await agendar_consulta(dados.telefone_usuario, action_data.get("data_hora"), action_data.get("procedimento")), "history": historico_para_salvar}
        elif action == "consultar_horarios_disponiveis":
            resultado_final = {"reply": await consultar_horarios_disponiveis(action_data.get("dia")), "history": historico_para_salvar}
        else:
            resultado_final = {"reply": "Não entendi a ação solicitada. Por favor, reformule.", "history": historico_para_salvar}
    except Exception as e:
        print(f"Erro ao executar a ação '{action}': {e}")
        resultado_final = {"reply": "Ocorreu um erro interno ao processar sua solicitação. Tente novamente.", "history": historico_para_salvar}

    return resultado_final

# --- Endpoints da API ---
@app.get("/")
async def root():
    return {"message": "API do Consultório Odonto-Sorriso no ar!"}

@app.head("/")
async def head_root():
    """Endpoint para Uptime Robot"""
    return Response(status_code=200)

@app.post("/chat")
async def chat_endpoint(dados: ChatRequest):
    """Endpoint de teste que expõe a lógica do chat."""
    resultado = await processar_chat_logic(dados)
    return {"reply": resultado.get("reply")}

@app.post("/whatsapp")
async def receber_mensagem_zapi(request: Request):
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Payload JSON inválido.")

    # Validação mínima do payload da Z-API
    numero_contato = payload.get("phone")
    if not numero_contato:
        print("Webhook recebido sem o campo 'phone'. Ignorando.")
        return Response(status_code=200) # Responde OK para não receber retentativas

    # Lógica de "snooze" para intervenção manual
    if payload.get("fromMe"):
        select_query = historico_conversas.select().where(historico_conversas.c.telefone == numero_contato)
        if await database.fetch_one(select_query):
            snooze_time = datetime.now(timezone.utc) + timedelta(minutes=30)
            update_query = historico_conversas.update().where(historico_conversas.c.telefone == numero_contato).values(snoozed_until=snooze_time)
            await database.execute(update_query)
            print(f"Modo manual ativado para {numero_contato} por 30 min.")
        return Response(status_code=200)

    # Processamento de texto e áudio
    conteudo_processar = None
    if payload.get("text", {}).get("message"):
        conteudo_processar = payload["text"]["message"]
    elif payload.get("audio", {}).get("audioUrl"):
        audio_bytes = await baixar_audio_bytes(payload["audio"]["audioUrl"])
        if audio_bytes:
            conteudo_processar = await transcrever_audio(audio_bytes) or "[Erro na transcrição]"
        else:
            conteudo_processar = "[Erro no download do áudio]"
    
    if not conteudo_processar:
        return Response(status_code=200)

    try:
        # Recupera histórico do banco
        query_select = historico_conversas.select().where(historico_conversas.c.telefone == numero_contato)
        resultado_db = await database.fetch_one(query_select)
        historico_recuperado = []

        if resultado_db:
            if resultado_db["snoozed_until"] and resultado_db["snoozed_until"] > datetime.now(timezone.utc):
                print(f"Conversa com {numero_contato} em modo manual (snoozed). Ignorando.")
                return Response(status_code=200)
            # Reseta o histórico se a última interação foi há mais de 6 horas (ajuste conforme necessário)
            if datetime.now(timezone.utc) - resultado_db["last_updated_at"] < timedelta(hours=6):
                historico_recuperado = json.loads(resultado_db["historico"])
        
        # Chama a lógica central
        dados_chat = ChatRequest(telefone_usuario=numero_contato, mensagem=conteudo_processar, historico=historico_recuperado)
        dados_resposta = await processar_chat_logic(dados_chat)
        mensagem_resposta = dados_resposta.get("reply", "Não consegui gerar uma resposta.")
        
        # <<< MUDANÇA: O histórico a ser salvo vem da `processar_chat_logic`
        historico_para_salvar = dados_resposta.get("history", [])
        # Adiciona a resposta final da ferramenta (o texto que o usuário vê) para completar o ciclo
        historico_para_salvar.append({"role": "assistant", "content": mensagem_resposta})
        historico_str = json.dumps(historico_para_salvar[-20:]) # Salva as últimas 20 trocas

        # Salva o histórico atualizado no banco
        if resultado_db:
            query_db = historico_conversas.update().where(historico_conversas.c.telefone == numero_contato).values(historico=historico_str, last_updated_at=func.now(), snoozed_until=None)
        else:
            query_db = historico_conversas.insert().values(telefone=numero_contato, historico=historico_str)
        await database.execute(query_db)

        # Envia a resposta para o usuário via Z-API
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
        print(f"!!! Erro Crítico no Webhook /whatsapp: {e} !!!")
        # Mesmo com erro, respondemos 200 para a Z-API não ficar reenviando a mesma mensagem
    
    return Response(status_code=200)
