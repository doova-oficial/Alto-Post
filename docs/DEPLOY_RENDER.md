# Deploy no Render

## Limitações do plano gratuito (confirmadas na documentação do Render em out/2026)

- Serviços web gratuitos **hibernam após 15 minutos sem tráfego de entrada** e levam cerca de 1 minuto para acordar. Enquanto o serviço dorme, o agendador interno **não roda**, e as publicações não saem no horário.
- O sistema de arquivos é **efêmero**: tudo o que estiver em disco é perdido ao hibernar, reiniciar ou fazer deploy. Por isso o SQLite não serve em produção.
- Não há disco persistente no plano gratuito.
- O PostgreSQL gratuito **expira 30 dias após a criação**.
- Cron jobs são cobrados à parte (mínimo de US$ 1/mês por cron job).

## Opção recomendada: serviço sempre ligado

- Serviço web **Starter** (pago) + **PostgreSQL pago** (plano `basic-256mb` ou superior).
- O agendador interno roda continuamente.
- Os dados ficam no PostgreSQL, com backups do Render.

Passos:

1. Envie o projeto para um repositório no GitHub ou GitLab.
2. No Render: **New → Blueprint** e selecione o repositório. O `render.yaml` cria o serviço e o banco.
   - Confira no painel os nomes de plano vigentes.
   - O `ADMIN_API_TOKEN` é gerado automaticamente.
3. Preencha as variáveis marcadas como `sync: false`:
   - `PUBLIC_BASE_URL`: a URL `https://<nome>.onrender.com` exibida no painel;
   - `TOKEN_ENCRYPTION_KEY`: gere localmente com `python -m app.cli gen-key`;
   - `GROQ_API_KEY`;
   - `META_IG_USER_ID`, `META_ACCESS_TOKEN`, `META_APP_ID`, `META_APP_SECRET` (ver [META_SETUP.md](META_SETUP.md)).
4. Depois do deploy, confira `https://<nome>.onrender.com/health`.
5. Mantenha `DRY_RUN=true` até concluir a validação descrita em META_SETUP.md.

## Opção de baixo custo: serviço gratuito + gatilho externo

Útil para testes. Não é recomendada para operação contínua.

1. Use um PostgreSQL externo (qualquer provedor com plano permanente). O PostgreSQL gratuito do Render expira.
2. Configure `SCHEDULER_ENABLED=false`.
3. Agende chamadas periódicas, a cada 10 ou 15 minutos, por **uma** destas formas:
   - um **Cron Job do Render** (pago, a partir de US$ 1/mês) executando `python -m app.cli tick` com as mesmas variáveis de ambiente;
   - um serviço externo de cron chamando:
     ```
     curl -X POST https://<nome>.onrender.com/api/scheduler/tick -H "Authorization: Bearer <ADMIN_API_TOKEN>"
     ```
     A chamada acorda o serviço e executa as tarefas vencidas.
4. O tick é idempotente e protegido por travas no banco, então chamadas repetidas ou simultâneas não duplicam publicações.
5. Limitações:
   - o horário real de publicação pode atrasar até o intervalo do cron, mais cerca de 1 minuto para acordar;
   - arquivos PNG em disco somem quando o serviço hiberna, mas o JPEG usado na publicação fica no banco.

## Banco de dados

- `DATABASE_URL` aceita `postgres://`, `postgresql://` ou `postgresql+psycopg://`. O sistema normaliza o formato.
- As tabelas são criadas automaticamente na inicialização.