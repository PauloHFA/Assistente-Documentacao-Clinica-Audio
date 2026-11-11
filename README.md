# CareVoice AI

Base de implementação para uma ferramenta de apoio à documentação clínica. O sistema deverá
organizar relatos de profissionais, exigir revisão e aprovação antes de registrar uma evolução
como final e não deverá diagnosticar nem recomendar condutas.

## Estado atual

O núcleo implementa configuração tipada, logging com redação de campos sensíveis, persistência
SQLAlchemy, criptografia de campos pessoais, convites de uso único, revogação de profissionais,
isolamento por profissional, cadastros de pacientes, consentimento e metas. A fila persistente
armazena payloads criptografados, limita retries e verifica consentimento antes e durante o
processamento. O worker local processa jobs de texto e áudio; o áudio usa o adaptador faster-whisper.

O serviço de registros persiste transcrições criptografadas com prazo de retenção, cria versões
imutáveis de rascunhos estruturados, verifica evidências, exige aprovação humana para emitir PDF e
mantém histórico das versões aprovadas. A limpeza de transcrições vencidas é executada pelo worker.
Os schemas disponíveis são SOAP, ABC e livre.

**Limites importantes:** ainda não há bot, API ou interface de usuário para cadastrar/enfileirar
casos, apresentar transcrições, editar rascunhos ou aprová-los. Essas operações existem como
serviços internos e precisam ser integradas a uma interface. Não há integração com LLM nem geração
automática de rascunhos. A verificação de fidelidade confirma correspondência textual e números,
mas não prova equivalência semântica; a revisão profissional continua indispensável. O
faster-whisper ainda não foi avaliado com um conjunto representativo de áudios. O projeto não está
pronto para uso clínico e não deve receber dados reais de pacientes.

## Desenvolvimento local

Requer Python 3.11 ou superior.

```powershell
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
