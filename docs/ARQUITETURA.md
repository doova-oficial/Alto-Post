# Arquitetura e decisões técnicas

## Componentes

| Componente | Responsável por |
|---|---|
| FastAPI | API administrativa e duas rotas públicas: `/health` e `/media/{token}.jpg` |
| Banco (SQLAlchemy) | fonte da verdade para fila, estados, histórico, métricas, travas e pausa |
| Pillow | geração das artes, de forma determinística, sem IA |
| Groq | somente o texto da legenda, sempre revalidado localmente |
| Meta Graph API | publicação e insights, pela API oficial (sem automação de navegador) |
| APScheduler ou tick externo | publicação, novas tentativas de legenda, métricas, renovação de token e backup |

## Rastreabilidade

```
Campaign ─┐
Experiment ─► Variant (parâmetros + valores dos fatores + hash) ─► Artwork (PNG/JPEG, sha256, token público)
                     │
Post ────────────────┴─ Caption (texto final, texto bruto, modelo, versão do prompt, problemas)
  ├─ PostEvent (cada mudança de estado, com mensagem)
  ├─ ig_container_id, ig_media_id (único), permalink
  └─ MetricSnapshot (cada coleta: idade em horas, métricas, métricas indisponíveis)
```

Cada arte tem um ID único (`art_…`) e pertence a uma variante (`var_…`), que guarda os parâmetros completos usados. Com isso, toda publicação pode ser reproduzida e comparada.

## Estados de uma publicação

| Estado | Significado |
|---|---|
| `draft` | arte gerada, legenda em preparo |
| `caption_failed` | Groq indisponível; nova tentativa automática com espera crescente |
| `needs_review` | validação encontrou problemas; precisa de ação humana |
| `pending_approval` | pronta; aguardando aprovação manual |
| `approved` | aprovada, sem horário |
| `scheduled` | na fila |
| `publishing` | reservada por um processo (trava atômica) |
| `processing` | contêiner criado; aguardando `FINISHED` |
| `verifying` | resultado incerto; o sistema confere na conta antes de qualquer nova tentativa |
| `published` | publicada (com `ig_media_id`) |
| `simulated` | percorreu o fluxo em modo de simulação; nada foi enviado |
| `failed` | falha definitiva após as tentativas |
| `rejected`, `cancelled` | rejeitada ou cancelada |
| `duplicate_blocked` | conteúdo idêntico a outro já aprovado ou publicado |

## Decisões

1. **Imagem servida pelo próprio serviço.** A Meta exige uma URL pública para a imagem, em JPEG. Os bytes do JPEG ficam no banco e são servidos em `/media/<token>.jpg`, com um token aleatório de 256 bits. Assim a publicação funciona mesmo em plataformas sem disco persistente, sem depender de serviço de armazenamento externo.
2. **Banco como fila e trava.** Reservar uma publicação é um `UPDATE … WHERE status='scheduled'` atômico. As tarefas usam `job_locks` com prazo de expiração. Isso evita publicação dupla após reinícios, deploys com duas instâncias, ou cron externo e agendador rodando juntos.
3. **Falhas ambíguas não são repetidas às cegas.** Se `media_publish` sofre timeout ou erro 5xx, a publicação pode ter acontecido. O sistema então:
   - consulta as mídias recentes da conta e o `status_code` do contêiner;
   - só publica de novo se tiver certeza de que a publicação não aconteceu.
4. **Simulação idêntica ao fluxo real.** O modo de simulação passa pelas mesmas validações, limites e travas; só a chamada à Meta é substituída.
5. **Validação dupla da legenda.** A legenda é validada ao ser gerada e de novo imediatamente antes de publicar. Uma legenda alterada no banco ou que viole as regras não sai.
6. **Fatos permitidos centralizados.** `app/content/doova_context.yaml` é a única fonte de fatos que a IA pode usar. A lista de funcionalidades começa vazia de propósito: só inclua o que já existir no app.
7. **Oportunidades reais separadas das institucionais.** Uma vaga só pode ser anunciada se vinculada a uma `VerifiedOpportunity`, que registra fonte, quem verificou e data de expiração. Sem esse vínculo, textos como "vagas abertas" mandam a publicação para revisão.
8. **Estatística conservadora.** O sistema:
   - compara métricas na mesma idade da publicação (72 h por padrão);
   - usa teste de permutação só quando cada grupo tem amostra mínima;
   - aplica ajuste por bloco (tema);
   - escreve as conclusões sempre como associação, nunca como causa.
9. **Fontes incluídas.** A fonte Inter vem junto do projeto, então a renderização é idêntica em Windows, Linux e Docker. Se as fontes faltarem, o fallback (`ImageFont.load_default(size)`) mantém o sistema funcionando e registra um aviso de revisão.
10. **Modelo da Groq.** O padrão é `openai/gpt-oss-120b`, modelo de produção. Os modelos Llama 3.x foram desligados para os planos gratuito e de desenvolvimento em 16/08/2026. O modelo é configurável e pode ser verificado em `GET /api/integrations/groq/models`.