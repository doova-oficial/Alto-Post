# Doova — Sistema de Automação e Marketing do Instagram

Sistema em Python para o perfil do **Doova**, um marketplace de trabalhos informais e serviços que começa por Iporá (GO). Ele faz quatro coisas:

- gera artes profissionais com Pillow, sem IA;
- escreve legendas com a API da Groq;
- publica pela API oficial do Instagram (Meta Graph API);
- mede os resultados e compara variações de layout com experimentos controlados.

Por padrão o sistema roda em **modo automático** (`REQUIRE_MANUAL_APPROVAL=false`): cria, confere, aprova, agenda no melhor horário e publica sozinho, e avisa você quando algo precisa de atenção. Ele começa em **modo de simulação** (`DRY_RUN=true`): nada vai ao Instagram até você trocar para `DRY_RUN=false`. Veja [docs/MODO_AUTOMATICO.md](docs/MODO_AUTOMATICO.md). Para voltar a revisar tudo antes, use `REQUIRE_MANUAL_APPROVAL=true`.

## Sumário
- [Visão geral](#visão-geral)
- [Rodar localmente](#rodar-localmente)
- [Rodar os testes](#rodar-os-testes)
- [Fluxo de uso](#fluxo-de-uso)
- [Endpoints](#endpoints)
- [Templates e configuração das artes](#templates-e-configuração-das-artes)
- [O que foi testado e o que depende de credenciais](#o-que-foi-testado-e-o-que-depende-de-credenciais)
- Documentação detalhada em `docs/`:
  - [ARQUITETURA](docs/ARQUITETURA.md)
  - [DEPLOY_RENDER](docs/DEPLOY_RENDER.md)
  - [META_SETUP](docs/META_SETUP.md)
  - [GROQ](docs/GROQ.md)
  - [OPERACAO](docs/OPERACAO.md)
  - [EXPERIMENTOS](docs/EXPERIMENTOS.md)

## Visão geral

```
app/
  main.py              FastAPI (lifespan, cabeçalhos de segurança, rotas)
  config.py            todas as configurações via variáveis de ambiente
  database.py          SQLAlchemy (SQLite local / PostgreSQL em produção)
  models.py            campanhas, experimentos, variantes, artes, legendas, posts, eventos, métricas, travas
  security.py          token administrativo + limite de requisições
  schemas.py           validação de entrada (Pydantic, campos extras proibidos)
  cli.py               comandos: init-db, tick, seed-launch, pause, report, gen-token...
  routes/              public (health, imagem p/ Meta), content, insights, system
  services/
    image_generator.py   8 templates, ajuste automático de texto, relatório de layout
    fonts.py             resolução de fontes multiplataforma + fallback
    text_layout.py       quebra de linha, hifenização, recusa de texto que não cabe
    template_config.py   leitura/validação de templates.yaml
    groq_service.py      legendas (timeout, tentativas, retry-after, intervalo mínimo)
    caption_validator.py remove emojis, limita hashtags, bloqueia promessas/números inventados
    meta_service.py      contêiner → status_code → media_publish, limites, insights, renovação de token
    content_service.py   criação de posts, estados, aprovação, agenda
    publisher.py         fila de publicação (travas, simulação, duplicidade, tentativas, verificação)
    scheduler.py         APScheduler + "tick" externo, ambos protegidos por travas no banco
    analytics_service.py coleta de métricas, taxas com denominador explícito, comparações, relatórios
    experiment_service.py A/B e fatorial com blocos por tema
    trends_service.py    sugestões: datas, histórico, FAQ, Google Trends (RSS)
    library.py, doova_context.py, token_store.py, backup.py, state.py
  templates/templates.yaml   marca, cores, tamanhos, posições e textos padrão dos 8 templates
  content/                   temas de lançamento, datas comemorativas, fatos permitidos sobre o Doova
  assets/fonts/              fonte Inter (licença OFL, incluída)
tests/                       97 testes com simuladores (nenhuma API real)
generated/                   artes geradas: <campanha>/<AAAA-MM-DD>/<id-do-post>/arte.png|arte.jpg|parametros.json
```

## Rodar localmente

Requisitos: Python 3.11 ou 3.12. Não é necessário instalar fontes.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env                 # Windows: copy .env.example .env
python -m app.cli gen-token          # cole o valor em ADMIN_API_TOKEN no .env
uvicorn app.main:app --reload
```

- Documentação interativa: http://127.0.0.1:8000/docs. Clique em **Authorize** e informe o token.
- Sem `GROQ_API_KEY`, crie publicações com legenda manual (`"caption": "..."`) ou com `generate_caption: false`.
- Para gerar uma amostra de cada template: `python -m app.cli render-samples` (a saída vai para `generated/amostras/`).
- Para criar as 12 publicações do plano de lançamento: `python -m app.cli seed-launch`. Use `--no-caption` para pular a Groq.

Com Docker:

```bash
docker build -t doova-marketing .
docker run --env-file .env -p 8000:8000 -v "$PWD/data:/app/data" doova-marketing
```

## Rodar os testes

```bash
pip install -r requirements-dev.txt
pytest
```

Os testes usam um banco SQLite temporário e simuladores HTTP da Groq e da Meta. Nenhuma API real é chamada.

## Fluxo de uso

1. **Criar.** `POST /api/posts` com `theme_key` (tema da biblioteca) ou com `template` + `params`. O sistema gera a arte e a legenda, depois valida as duas.
2. **Revisar.** `GET /api/posts/pending` lista o que precisa de atenção:
   - `pending_approval`: pronta, aguardando aprovação;
   - `needs_review`: a validação encontrou problemas;
   - `caption_failed`: a Groq está fora; o sistema tenta de novo sozinho.
3. **Aprovar ou rejeitar.** Use `POST /api/posts/{id}/approve` ou `.../reject`. Para trocar a legenda, use `PUT /api/posts/{id}/caption`; trocar a legenda exige nova aprovação.
4. **Agendar.** `POST /api/posts/{id}/schedule` com horário local. Outra opção é `AUTO_SCHEDULE=true`, que ocupa os horários de `POSTING_SLOTS`.
5. **Publicar.** O agendador publica no horário, uma publicação por ciclo, respeitando os limites. Para publicar na hora, use `POST /api/posts/{id}/publish-now`.
6. **Medir.** As métricas são coletadas a cada 6 h. Os resultados ficam em:
   - `GET /api/metrics/summary` (resumo);
   - `GET /api/metrics/compare?dimension=title_position` (comparação por dimensão);
   - `POST /api/metrics/reports` (CSV e JSON).
7. **Pausar** a qualquer momento com `POST /api/scheduler/pause` (ou `python -m app.cli pause`).

Exemplo:

```bash
TOKEN=...; API=http://127.0.0.1:8000
curl -s -X POST $API/api/posts -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"theme_key": "convite_prestadores", "campaign": "lancamento"}'
```

## Endpoints

| Função | Método e rota |
|---|---|
| Saúde do serviço | `GET /health` (público) |
| Imagem servida à Meta | `GET /media/{token}.jpg` (público, token aleatório de 43 caracteres) |
| Listar templates | `GET /api/templates` |
| Gerar arte avulsa | `POST /api/art` · `GET /api/art/{id}/image` |
| Gerar legenda | `POST /api/captions/preview` |
| Criar publicação | `POST /api/posts` |
| Listar pendentes | `GET /api/posts/pending` · `GET /api/posts?status=...` |
| Detalhes, legenda | `GET /api/posts/{id}` · `PUT /api/posts/{id}/caption` · `POST .../caption/regenerate` |
| Aprovar / rejeitar / cancelar | `POST /api/posts/{id}/approve` · `.../reject` · `.../cancel` |
| Agendar | `POST /api/posts/{id}/schedule` |
| Publicar agora | `POST /api/posts/{id}/publish-now` |
| Histórico | `GET /api/posts/history` |
| Métricas | `GET /api/metrics/summary` · `GET /api/metrics/posts/{id}` · `POST /api/metrics/collect` · `POST /api/metrics/reports` |
| Comparar | `GET /api/metrics/compare` · `GET /api/experiments/{id}/results` |
| Experimentos | `POST /api/experiments` · `POST .../{id}/generate` · `POST .../{id}/schedule` |
| Sugestões de conteúdo | `GET /api/suggestions` · `GET /api/library` |
| Oportunidades verificadas | `POST /api/opportunities` · `POST /api/opportunities/{id}/deactivate` |
| Pausar / retomar | `POST /api/scheduler/pause` · `POST /api/scheduler/resume` |
| Agendador | `GET /api/scheduler` · `POST /api/scheduler/tick` · `POST /api/scheduler/run/{job}` |
| Integrações | `GET /api/integrations/meta/check` · `POST /api/integrations/meta/refresh-token` · `GET /api/integrations/groq/models` |
| Configuração (sem segredos) | `GET /api/config` |

Todas as rotas `/api` exigem `Authorization: Bearer <ADMIN_API_TOKEN>` (ou o cabeçalho `X-Admin-Token`).

## Templates e configuração das artes

Os oito templates, com formato de 1080×1350 (retrato 4:5) e saída em PNG e JPEG:

| Template | Uso |
|---|---|
| `beneficios` | benefícios |
| `pergunta` | pergunta ao público |
| `educativo` | conteúdo educativo |
| `autonomos` | voltado a autônomos |
| `contratantes` | voltado a quem contrata |
| `funcionalidades` | explicação de funcionalidade |
| `aquisicao` | aquisição de usuários |
| `institucional` | institucional |

Edite `app/templates/templates.yaml` para mudar cores, tamanhos, posições, CTA e textos padrão sem tocar no código. Cada publicação também aceita parâmetros próprios em `params`:

- textos: `title`, `subtitle`, `body`, `items`, `cta`, `tag`;
- cores e fundo: `primary_color`, `accent_color`, `background`;
- posições: `title_position`, `subtitle_position`, `logo_position`, `text_align`;
- tamanhos e elementos: `title_size`, `shapes`, `item_style`, entre outros.

Regras de texto:

- O texto é quebrado e reduzido automaticamente até um tamanho mínimo legível.
- Se mesmo assim não couber, a arte **é recusada com erro**. Ela nunca é cortada.
- Cada arte registra um relatório de layout com:
  - posição de cada elemento;
  - escala usada;
  - contraste mínimo, verificado como ≥ 4,5:1 nos testes.

Para usar o logotipo real, coloque o arquivo em `app/assets/` e preencha `brand.logo_image` em `templates.yaml`. O sistema também lê o arroba do perfil em `brand.handle`.

## O que foi testado e o que depende de credenciais

**Testado automaticamente (97 testes passando):**
- geração das 8 artes, títulos longos, recusa de texto que não cabe, fallback de fonte, cores do Doova e contraste;
- remoção de emojis preservando acentos, limite de hashtags e bloqueio de promessas, números e vagas inventados;
- falhas da Groq: timeout, 429 com `retry-after`, 5xx, 401, resposta vazia ou truncada. Nunca resulta em legenda vazia;
- fila persistente após reinício, travas entre processos, pausa e modo de simulação (a Meta nunca é chamada);
- bloqueio de duplicidade, limites de frequência e recuperação de processos interrompidos;
- fluxo completo de publicação com a **Meta simulada**: contêiner → status → publicação, falha ambígua verificada antes de repetir, erros definitivos;
- cálculo de métricas, normalização por idade, testes estatísticos, experimentos e associação variante ↔ publicação;
- segurança de todas as rotas.

**Não testado com serviços reais (depende das suas credenciais):**
- publicação real no Instagram, coleta real de insights e renovação real do token;
- chamada real à Groq;
- formato atual do RSS do Google Trends (testado com amostra; ver limitações em `trends_service.py`).

Antes de desligar a simulação, siga o roteiro de validação em [docs/META_SETUP.md](docs/META_SETUP.md#validação-antes-de-desligar-a-simulação).

## Licenças

A fonte Inter (`app/assets/fonts`) é distribuída sob a SIL Open Font License 1.1 (ver `LICENSE-Inter.txt`).