# Pendências e Próximos Passos — CareVoice AI (`carevoice-ai`)

Documento técnico consolidando as implementações realizadas, o estado validado em testes/análise estática, as evoluções opcionais restantes e o que depende de credenciais, ambiente externo ou decisões jurídicas/clínicas fornecidas por você.

---

## Implementações Concluídas nesta Rodada

1. **Motor de estruturação clínica via LLM (`carevoice/structure/llm.py`, `carevoice/structure/pseudonymizer.py`, `carevoice/structure/dates.py`):**
   - Cliente HTTP assíncrono provider-independent (`UrllibLLMTransport`) suportando **Ollama local** (`/api/chat`) por padrão e **provedor compatível com OpenAI** (`/chat/completions`) bloqueado até opt-in explícito (`CAREVOICE_CLOUD_PROCESSING_ENABLED=true`).
   - Pseudonimização reversível de nomes de pacientes, apelidos e responsáveis (`[PACIENTE]`, `[RESPONSAVEL_1]`, ...) antes de enviar o texto ao LLM quando `CAREVOICE_PSEUDONYMIZE_BEFORE_LLM=true`, com restauração após validação do JSON.
   - Filtragem de marcações `#interno` para impedir vazamento no `parent_summary` (`RF15`).
   - Resolução determinística de datas relativas em pt-BR (`"ontem"`, `"anteontem"`, `"sexta-feira passada"`, `"dd/mm/aaaa"`) no fuso horário do profissional (`RF10`).
   - Retry corretivo automático (até 2 tentativas) enviando erros de schema Pydantic ou de fidelidade (`verify_draft_fidelity`) ao modelo (`RF12`, `RF13`).
2. **Transcrição em nuvem opt-in e inspeção real de áudio (`carevoice/transcribe/cloud.py`, `carevoice/audio/limits.py`):**
   - `CloudOpenAITranscriber` (`/v1/audio/transcriptions`) protegido por `cloud_processing_enabled=True`.
   - `inspect_audio_stream()` via PyAV (`av` + `numpy`) validando stream de áudio real, duração efetiva e rejeitando áudios silenciosos/sem fala (`verify_media=True`, `RF06`).
3. **Domínio clínico, PDF, cotas e auditoria (`carevoice/domain/records.py`, `carevoice/documents/pdf.py`, `carevoice/jobs/queue.py`, `carevoice/jobs/worker.py`):**
   - Selo `"Retificação da evolução de dd/mm/aaaa"` (+ motivo) no PDF quando `version > 1` (`RF18`).
   - Relatório clínico periódico em PDF (`build_periodic_report_pdf` / `render_periodic_report` — `RF23`), tendência de metas (`goal_progress` — `RF21`), busca textual sobre sessões criptografadas (`search_sessions` — `RF20`), descarte com guarda de retenção mínima (`discard_session`, `minimum_retention_days` — `RF27`), ciclo de lembrete/arquivamento (`24h` / `7 dias` — `RF16`), exportação `.zip` do prontuário (`export_patient_bundle` — `RF25`) e listagem de auditoria (`list_audit_events` — `RF26`).
   - Cota diária de jobs por profissional (`daily_limit=100` — `RF29`) em `JobQueue`.
   - Integração opcional do `ClinicalStructurer` e seleção `local`/`cloud` no `ProcessingWorker`.
4. **Bot do Telegram e Infraestrutura (`carevoice/bot/router.py`, `carevoice/bot/telegram_api.py`, `Dockerfile`, `docker-compose.yml`):**
   - Comandos `/start`, `/ajuda`, `/privacidade`, `/status`, `/entrar`, `/convite`, `/paciente novo`, `/pacientes`, `/consentimento`, `/revogar_consentimento`, `/metas`, `/meta nova`, `/sessao`, `/historico`, `/buscar`, `/progresso`, `/relatorio`, `/exportar` e `/auditoria`.
   - Fluxo de envio por texto/áudio com desambiguação por botões inline e revisão via botões (`[✅ Aprovar] [✏️ Editar] [🔄 Refazer] [🗑️ Descartar]`).
   - Entrypoint `carevoice-bot` em `pyproject.toml`, `Dockerfile` (usuário não-root) e `docker-compose.yml` (PostgreSQL 16 + `migrate` + `worker` + `bot`).

---

## 1. O que ainda falta implementar (Evoluções Opcionais / Backlog)

### 1.1 Gráficos visuais no `/progresso` e relatórios (`RF21`, `RF23`)
- **Situação atual:** `goal_progress()` calcula a série temporal das medidas aprovadas e retorna o resumo de tendência em texto (incluído no `/progresso` e no PDF periódico).
- **O que falta:** Caso deseje gráfico PNG/SVG embutido no Telegram e no PDF periódico, adicionar geração de gráfico (ex.: `matplotlib` ou desenho vetorial ReportLab).

### 1.2 Templates personalizados pelo administrador (`RF11`)
- **Situação atual:** O sistema suporta estritamente os templates `SOAP`, `ABC` e `FREE`.
- **O que falta:** Tabela e fluxo administrativo para definir templates customizados além dos 3 padrões.

### 1.3 Senha/criptografia adicional no ZIP do `/exportar` (`RF25`) e áudio via PyAV no Telegram
- **Situação atual:** `export_patient_bundle()` gera arquivo `.zip` em memória (com `manifest.json` + PDFs aprovados) entregue somente ao profissional autenticado no chat; o `TelegramBotRunner` recebe áudio com `verify_media=False` por padrão no `JobQueue`.
- **O que falta:** Caso exija ZIP protegido por senha no nível do arquivo (ex.: AES-ZIP via `pyzipper`), adicionar esse wrapper; e habilitar `verify_media=True` no `JobQueue` quando os áudios reais forem contêineres OGG/WAV válidos.

### 1.4 Rotina automatizada de backup criptografado + lembretes agendados (`RF16`, `RF28`)
- **Situação atual:** Existem os métodos `list_pending_drafts_for_reminder(older_than_hours=24)` e `archive_stale_drafts(older_than_days=7)` no serviço, mas ainda não há agendador disparando mensagens proativas no chat do Telegram nem script de `pg_dump` criptografado com teste de restauração.

### 1.5 Webhook HTTPS vs Long-Polling no Telegram (`RNF09`)
- **Situação atual:** `carevoice-bot` usa long-polling (`getUpdates`) via HTTPS de saída, dispensando exposição de porta pública.
- **O que falta:** Se desejar operar por webhook em produção com TLS reverso, expor endpoint HTTP/ASGI com verificação de `X-Telegram-Bot-Api-Secret-Token`.

---

## 2. O que depende de você (Credenciais, Infraestrutura e Decisões)

| Item | O que precisa ser definido / fornecido | Impacto direto |
|---|---|---|
| **1. Segredos no `.env`** | Definir `CAREVOICE_TELEGRAM_BOT_TOKEN` (`@BotFather`), `CAREVOICE_ADMINISTRATOR_CHAT_ID`, e gerar `CAREVOICE_FIELD_ENCRYPTION_KEY` (chave Fernet). | Necessário para subir `carevoice-bot` e `carevoice-worker`. |
| **2. Provedor de IA (Ollama ou Nuvem)** | Subir Ollama local (`ollama pull qwen2.5:7b-instruct` em `CAREVOICE_OLLAMA_BASE_URL`) ou habilitar nuvem (`CAREVOICE_CLOUD_PROCESSING_ENABLED=true`, `CAREVOICE_LLM_PROVIDER=cloud`, `CAREVOICE_CLOUD_LLM_API_KEY`). | Necessário para estruturação real automática no worker. |
| **3. Banco PostgreSQL + Docker** | Subir Docker (`docker compose up -d --build`) ou fornecer `CAREVOICE_DATABASE_URL` PostgreSQL real para validar migrações `alembic upgrade head` fora do SQLite. | Obrigatório para `CAREVOICE_ENVIRONMENT=production`. |
| **4. Corpus de 50 áudios clínicos (`RNF02`/`RNF03`)** | Executar avaliação empírica de WER (`<= 10%`), latência (`<= 30s`) e taxa de falsos positivos/omissões com `faster-whisper` (`medium` pt-BR) em hardware alvo. | Validação quantitativa antes de liberar piloto ampliado. |
| **5. Governança LGPD / Jurídica (`RNF15`)** | Conduzir RIPD, modelagem de ameaças (STRIDE), contratos com operadores (se usar nuvem), validação com conselhos profissionais e decisão sobre manter `CAREVOICE_REAL_DATA_ENABLED=false` até conclusão. | Pré-requisito para sair do piloto com dados fictícios. |
| **6. Sincronização Git (`ahead/behind`)** | Decidir se deseja consolidar os commits e arquivos novos na branch `main` e enviar (`push`) para `origin/main` (`https://github.com/PauloHFA/CareVoice.git`). | Mantém o repositório remoto atualizado com toda a implementação. |

---

## 3. Verificação Executada

```bash
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m ruff format --check carevoice tests
.venv/Scripts/python.exe -m mypy carevoice
.venv/Scripts/python.exe -m pytest --cov=carevoice --cov-report=term-missing -q
.venv/Scripts/python.exe -m pip_audit --skip-editable
```
