# Assistente-Documentacao-Clinica-Audio (`carevoice`)

Base de implementação para assistência à documentação clínica por áudio e texto. O sistema organiza relatos de profissionais, preserva pseudonimização e verificação factual literal de evidências, exige revisão e aprovação humana antes de emitir evoluções finais em PDF e não diagnostica nem recomenda condutas.

> **Identificadores internos preservados:** o pacote Python (`carevoice`), a distribuição (`carevoice-ai`), os entrypoints (`carevoice-worker`, `carevoice-bot`), o prefixo de ambiente (`CAREVOICE_*`) e as migrações Alembic permanecem inalterados para manter compatibilidade de imports e contratos.

## Estado atual

O núcleo implementa configuração tipada, logging com redação de campos sensíveis, persistência SQLAlchemy/Alembic, criptografia de campos pessoais com digest HMAC, convites de uso único, revogação de profissionais, isolamento por profissional, cadastros de pacientes, consentimento e metas.

- **Fila e Worker (`carevoice/jobs/`):** payloads criptografados na fila, `SKIP LOCKED`, cota diária por profissional (`daily_limit=100`), verificação de consentimento antes e durante o processamento, limpeza automática de transcrições vencidas, transcrição local (`faster-whisper`) ou nuvem opt-in (`CloudOpenAITranscriber`) e inspeção real de áudio (`inspect_audio_stream` via PyAV).
- **Estruturação e Fidelidade (`carevoice/structure/`, `carevoice/fidelity/`):** cliente LLM assíncrono (`Ollama` local por padrão ou nuvem opt-in), pseudonimização reversível de nomes antes do LLM, filtro `#interno`, resolução determinística de datas em `pt-BR`, retry corretivo (até 2x) e verificação literal de citações/números/unidades para `SOAP`, `ABC` e `FREE`.
- **Registros, PDFs e Bot Telegram (`carevoice/domain/records.py`, `carevoice/documents/pdf.py`, `carevoice/bot/`):** versões imutáveis com SHA-256, selo de retificação (`version > 1`), histórico, busca em sessões criptografadas, progresso de metas, relatório periódico em PDF, exportação `.zip`, auditoria e bot Telegram (`carevoice-bot`).

**Limites importantes:** a verificação de fidelidade confirma correspondência textual e números, mas não prova equivalência semântica; a revisão profissional continua indispensável. `faster-whisper` e o pipeline LLM ainda precisam de avaliação empírica com conjunto representativo de áudios (`RNF02`/`RNF03`), e o modo de dados reais permanece desabilitado por padrão (`CAREVOICE_REAL_DATA_ENABLED=false`).

## Desenvolvimento local

Requer Python 3.11 ou superior.

```powershell
git clone https://github.com/PauloHFA/Assistente-Documentacao-Clinica-Audio.git
cd Assistente-Documentacao-Clinica-Audio
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,audio]"
Copy-Item .env.example .env
alembic upgrade head
pytest
ruff check .
mypy carevoice
```

O `.env.example` contém apenas configurações de desenvolvimento. Gere uma chave Fernet para o
ambiente local com:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Configure a chave gerada em `CAREVOICE_FIELD_ENCRYPTION_KEY` e um identificador administrativo em
`CAREVOICE_ADMINISTRATOR_CHAT_ID` usando um armazenamento local de segredos; não os adicione ao Git.
Com as dependências e o banco configurados, inicie o worker:

```powershell
carevoice-worker
```

O worker consulta a fila a cada segundo, remove transcrições vencidas e encerra com Ctrl+C. No
primeiro job de áudio, faster-whisper poderá baixar os pesos do modelo; faça esse download inicial
em ambiente controlado. A fila pode ser usada pelo código interno, mas ainda não existe uma
interface para criar jobs ou operar o fluxo de revisão.

O processamento local e o uso de dados fictícios são os padrões. A habilitação técnica de um
provedor em nuvem não substitui consentimento por paciente, base legal, contratos com operadores,
avaliação de transferência internacional ou revisão jurídica. O Telegram é destinado apenas a
testes com dados fictícios até que o canal seja avaliado para uso real.

## Segurança e privacidade

- Nunca registre áudio, transcrição, conteúdo clínico, nomes ou segredos em logs.
- Nunca coloque credenciais no código, em arquivos versionados ou em mensagens de erro.
- Configure segredos por ambiente seguro; `.env` é local e ignorado pelo Git.
- Os campos de identificação e contato são criptografados na aplicação. Defina
  `CAREVOICE_FIELD_ENCRYPTION_KEY` com uma chave Fernet gerada e armazenada fora do banco; perder
  ou trocar a chave sem um procedimento de rotação torna esses campos irrecuperáveis.
- A configuração inicial do esquema usa Alembic (`alembic upgrade head`). SQLite é apenas para
  desenvolvimento/testes; produção exige PostgreSQL.
- `CAREVOICE_ADMINISTRATOR_CHAT_ID` deve ser configurado para emitir convites. Nunca compartilhe
  convites; são tokens de uso único com validade padrão de 24 horas.
- O código oferece operações internas de cadastro e consentimento, mas ainda não há fluxo de
  interface para obter ou administrar essas autorizações. Dados reais permanecem desabilitados
  por padrão.
- Antes de qualquer uso real, concluir RIPD, modelagem de ameaças, revisão jurídica, política de
  retenção e avaliação do canal e dos provedores.

## Qualidade

O CI executa Ruff, mypy, testes com limite de cobertura do núcleo e auditoria de dependências.
Os critérios clínicos de WER e fidelidade exigem conjuntos de avaliação apropriados; não são
validados por estes testes de fundação.
