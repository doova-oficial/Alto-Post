# Modo automático

Com `REQUIRE_MANUAL_APPROVAL=false` (padrão), o sistema cria, confere, aprova, agenda e publica sem esperar ninguém. A revisão humana foi trocada por checagens automáticas, horário aprendido com os resultados e uma vigilância que avisa e pausa quando algo dá errado.

## O que acontece sozinho

| Etapa | Como | Tarefa |
|---|---|---|
| Legenda | gerada pela Groq e validada; se a validação recusar, pede outra explicando o motivo (até `CAPTION_MAX_ATTEMPTS`) | ao criar e `maintenance` |
| Rascunhos sem legenda | ganham legenda assim que houver `GROQ_API_KEY` (ex.: as 12 de lançamento) | `maintenance` (5 min) |
| Aprovação | automática quando legenda, arte e checagens estão ok | ao criar e `maintenance` |
| Agenda | próximo horário livre, escolhido pelo aprendizado de horários | `maintenance` |
| Publicação | contêiner → status → `media_publish`, com tentativas e deduplicação | `publish` (1 min) |
| Assuntos do momento | busca, triagem, foto e publicação (no máximo 1 por dia) | `trends_auto` (6 h) |
| Vigilância | alertas e pausa automática | `watchdog` (15 min) |

## Checagens automáticas (no lugar da revisão)

- **Legenda:** sem emojis, até 3 hashtags, sem promessas de renda ou clientes, sem vagas não verificadas, sem placeholders, tamanho e pontuação corretos.
- **Texto da arte:** as mesmas regras de conteúdo proibido.
- **Foto:** licença comercial, crédito na arte e na legenda, e recusa automática se o título ou as tags indicarem pessoas ou marcas (`PHOTO_AUTO_CHECK`).
- **Assunto do momento:** triado ao ser criado e de novo logo antes de publicar, com as manchetes mais recentes do Google Trends. Se ganhou um lado sensível, a publicação é cancelada. Expira em 48 h.
- **Antes de publicar:** imagem presente, legenda revalidada, conteúdo não duplicado, limites diários e intervalo mínimo.

O que não passar fica em `needs_review` e **não é publicado**; a vigilância avisa você.

## Horário adaptativo

`POSTING_STRATEGY=adaptive` (padrão) usa os horários de `POSTING_CANDIDATE_SLOTS` (07:30, 09:00, 11:30, 12:30, 15:00, 18:00, 19:30, 21:00).

1. **Aprendizado:** enquanto algum horário tiver menos de `TIMING_MIN_SAMPLES` (3) publicações medidas, as publicações são distribuídas em rodízio, começando pelos horários menos testados.
2. **Aproveitamento:** depois disso, as publicações vão para os horários com maior taxa de engajamento (interações / alcance, medida por volta de 72 h). Em `TIMING_EXPLORE_RATE` (20%) dos dias, um dos horários vai para teste, para perceber mudanças no público.

Assuntos do momento esfriam rápido (`TREND_PUBLISH_ASAP`): se o próximo horário estiver a mais de 3 h, a publicação sai em seguida, respeitando o intervalo mínimo e o limite diário. Entre 23h e 7h ela espera o próximo horário.

Somente publicações reais, com métricas da Meta, ensinam o sistema; em simulação ele só faz o rodízio. Com 8 horários e 3 medições por horário, o aprendizado precisa de cerca de 24 publicações reais (umas 2 semanas com 2 por dia). É uma heurística de otimização, não uma prova de que um horário causa mais engajamento.

Veja o que foi aprendido em `GET /api/timing`. Para horários fixos, use `POSTING_STRATEGY=fixed` e `POSTING_SLOTS`.

## Vigilância e alertas

Configure `ALERT_WEBHOOK_URL` para receber avisos no celular:

- **Discord:** URL de webhook do canal.
- **Slack:** URL de *Incoming Webhook*.
- **ntfy:** `https://ntfy.sh/um-topico-dificil-de-adivinhar` (app gratuito para Android e iOS).
- Qualquer outra URL recebe um JSON `{title, message}`.

Teste com `POST /api/alerts/test`. O histórico fica em `GET /api/alerts`.

| Situação | O que acontece |
|---|---|
| Publicação falhou de vez | aviso |
| `AUTO_PAUSE_FAILURES` (3) falhas definitivas em 24 h | **pausa toda a publicação** e avisa; retome com `POST /api/scheduler/resume` |
| Publicações barradas (`needs_review`) | aviso com o motivo |
| Fila cobre menos de `QUEUE_MIN_DAYS` (2) dias | aviso para criar mais conteúdo |
| Token da Meta expira em menos de 7 dias sem renovação automática | aviso |

Cada aviso igual é enviado no máximo uma vez a cada `ALERT_DEDUPE_HOURS` (12 h).

## Horário pontual exige servidor acordado

As tarefas rodam dentro do serviço web. No plano gratuito do Render, o serviço dorme após 15 minutos sem acesso, e nada roda enquanto ele dorme. Use um plano pago, ou chame `POST /api/scheduler/tick` a cada 5–10 minutos com um cron externo (veja [DEPLOY_RENDER.md](DEPLOY_RENDER.md)).

## Voltar para aprovação manual

Defina `REQUIRE_MANUAL_APPROVAL=true`. Tudo passa a ficar em `pending_approval` ou `needs_review` até você aprovar.