# Operação

## Pausar e retomar

- API: `POST /api/scheduler/pause` (com `{"reason": "..."}`) e `POST /api/scheduler/resume`.
- CLI: `python -m app.cli pause --reason "revisão"` e `python -m app.cli resume`.
- A pausa fica gravada no banco e vale para todas as instâncias, inclusive após reinício.
- Durante a pausa, nada é publicado, nem com `publish-now`.

## Modo automático

Veja [MODO_AUTOMATICO.md](MODO_AUTOMATICO.md): checagens automáticas, horário adaptativo, alertas e pausa automática.

## Logs

- Os logs vão para a saída padrão. No Render, veja a aba **Logs**.
- Cada mudança de estado de uma publicação gera uma linha de log e um registro em `post_events`. Consulte em `GET /api/posts/{id}` ou `GET /api/posts/history`.
- Os segredos são mascarados nos logs:
  - tokens `access_token=`, `Bearer`, `gsk_…`, `EAA…`, `IGAA…`;
  - os valores configurados das variáveis sensíveis.
- Estado das tarefas agendadas: `GET /api/scheduler` mostra a última execução e o resultado de cada uma.

## Limites de frequência

| Configuração | Padrão | Efeito |
|---|---|---|
| `MAX_POSTS_PER_DAY` | 2 | máximo de publicações em 24 h móveis |
| `MIN_MINUTES_BETWEEN_POSTS` | 180 | intervalo mínimo entre publicações |
| `PUBLISH_MAX_ATTEMPTS` | 3 | tentativas por publicação; depois vira `failed` |

O publicador faz no máximo uma publicação por ciclo.

## Backups

- **SQLite (uso local):** a tarefa `backup` roda diariamente às 03:30 e copia o banco para `BACKUP_DIR`, mantendo os `BACKUP_KEEP` mais recentes. A cópia é consistente mesmo com o banco em uso.
- **PostgreSQL (produção):**
  - use os backups automáticos do Render Postgres pago (diários, com recuperação pontual conforme o plano);
  - além disso, faça uma cópia externa periódica:
    ```bash
    pg_dump "$DATABASE_URL" --format=custom --file doova-$(date +%F).dump
    pg_restore --clean --no-owner --dbname "$DATABASE_URL" doova-AAAA-MM-DD.dump
    ```
- As artes ficam no banco (JPEG). Os PNG em `generated/` podem ser regenerados a partir de `variants.params`.

## Checklist de segurança

- `ADMIN_API_TOKEN` com 24 caracteres ou mais (gere com `python -m app.cli gen-token`). Sem ele, a API administrativa fica bloqueada (HTTP 503).
- Segredos somente em variáveis de ambiente. O `.env` está no `.gitignore` e no `.dockerignore`.
- Limites de requisições:
  - 60 por minuto em geral;
  - 10 por minuto em geração e publicação;
  - 10 tentativas de autenticação inválidas por minuto por IP.
- Em produção pública, considere `DOCS_ENABLED=false`.
- O limitador de requisições é em memória. Com várias instâncias, cada uma conta separadamente.

## Problemas comuns

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| `not_configured` no publicador | `DRY_RUN=false` sem credenciais ou sem `PUBLIC_BASE_URL` | preencher as variáveis |
| `failed` com `code=190` | token da Meta expirado | gerar token novo e reagendar |
| contêiner `ERROR` | a Meta não conseguiu baixar a imagem | conferir se `/media/<token>.jpg` abre publicamente |
| `caption_failed` | Groq indisponível ou limitada | aguardar; o sistema tenta de novo sozinho |
| `needs_review` | validação encontrou problema (no modo automático, depois de tentar outra legenda) | ver `review_notes` e `caption_issues`, editar e aprovar |
| publicação pausada sozinha | `AUTO_PAUSE_FAILURES` falhas definitivas em 24 h | ver `GET /api/alerts`, corrigir e `POST /api/scheduler/resume` |