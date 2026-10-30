# Experimentos de layout

## O que pode ser medido e o que não pode

- A API da Meta mede **distribuição e ações**: alcance, visualizações, curtidas, comentários, compartilhamentos, salvamentos, visitas ao perfil e seguidores.
- Ela **não mede atenção visual**. Não há dado sobre "para onde as pessoas olharam". Uma afirmação como "título no topo atrai mais o olhar" não pode ser comprovada com essas métricas.
- O que pode ser observado é, por exemplo: "publicações com título no topo tiveram maior taxa de interações por alcance, nestas condições".
- Cliques e conversões no app exigem um link rastreável (por exemplo, UTM na bio) e integração com a análise do app, que não fazem parte deste sistema.

## Variáveis testáveis (fatores)

| Fator | Níveis |
|---|---|
| `title_position` | top, center, bottom |
| `subtitle_position` | above_title, below_title |
| `title_size` | números inteiros |
| `text_amount` | curto, medio, longo |
| `info_quantity` | 0 a 4 itens |
| `primary_color`, `accent_color` | cores em hexadecimal |
| `background` | gradient, solid, light, dark |
| `shapes` | true, false |
| `template` | qualquer um dos 8 templates |
| `cta` | textos livres |
| `message_type` | requer `message_variants` no tema |

## Desenho

- **A/B:** um fator com dois níveis. **Fatorial:** combinações de fatores, até 8 por bloco.
- **Bloco = tema.** Dentro do bloco, tudo é igual exceto o fator testado: mesmos textos e, por padrão, a mesma legenda.
- A ordem é **aleatória dentro do bloco**, e todas as publicações saem **no mesmo horário do dia**, em dias diferentes.
- A comparação usa a métrica na **mesma idade** da publicação (`ANALYSIS_TARGET_AGE_HOURS`, padrão 72 h). Publicações mais novas ficam fora até atingirem essa idade.
- Os resultados são apresentados de duas formas: brutos e **ajustados por bloco** (descontando a média do tema).
- O teste de permutação só roda quando cada grupo tem ao menos `ANALYSIS_MIN_SAMPLES_PER_GROUP` publicações (padrão 5). Abaixo disso, o relatório mostra apenas médias descritivas e diz quantas publicações faltam.

## Exemplo: posição do título

```bash
curl -X POST $API/api/experiments -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{
  "name": "posicao_titulo_v1",
  "factors": {"title_position": ["top", "bottom"]},
  "theme_keys": ["convite_prestadores", "convite_contratantes", "beneficio_perto",
                 "dica_contratar", "dica_orcamento"],
  "template": "beneficios",
  "hypothesis": "Título no topo está associado a maior taxa de interações por alcance",
  "primary_metric": "engagement_rate"
}'
# depois:
#   POST /api/experiments/{id}/generate
#   aprovar cada post
#   POST /api/experiments/{id}/schedule  {"start_date": "2026-11-03", "slot": "19:00"}
#   GET  /api/experiments/{id}/results
```

## Taxas e denominadores

| Taxa | Cálculo |
|---|---|
| `engagement_rate` | `total_interactions` ÷ alcance (ou curtidas + comentários + compartilhamentos + salvamentos, se `total_interactions` faltar) |
| `like_rate`, `comment_rate`, `share_rate`, `save_rate` | métrica ÷ alcance |
| `profile_visit_rate` | visitas ao perfil ÷ alcance |
| `follow_rate` | novos seguidores ÷ alcance |
| `views_per_reach` | visualizações ÷ alcance |

Métricas indisponíveis ficam vazias, nunca como zero.

## Limitações

- Com um perfil novo, o alcance de cada publicação é pequeno e muito variável. Espere precisar de dezenas de publicações para detectar diferenças moderadas.
- O algoritmo do Instagram, o dia da semana, os acontecimentos locais e o crescimento de seguidores mudam ao longo do experimento. O ajuste por bloco e o horário fixo reduzem esse efeito, mas não o eliminam.
- Um resultado "significativo" deve ser replicado antes de virar padrão. Testar muitas dimensões ao mesmo tempo aumenta a chance de achar diferenças por acaso.