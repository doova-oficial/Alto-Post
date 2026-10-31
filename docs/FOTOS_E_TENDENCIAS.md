# Fotos de bancos gratuitos e assuntos do momento

Este fluxo transforma um assunto em alta no Google Trends em uma publicação com foto, sem ninguém precisar escolher a imagem:

1. **Assunto em alta.** O sistema lê o Google Trends (Goiás primeiro, depois Brasil).
2. **Triagem.** Assuntos sensíveis são descartados. Isso vale para o termo e para as manchetes ligadas a ele: tragédia, morte, acidente, crime, polícia, política, eleição, religião, saúde, apostas e conteúdo adulto. A lista fica em `SENSITIVE`, em `app/services/trend_content.py`. Para acrescentar palavras sem mexer no código, use `TREND_BLOCKLIST_EXTRA`.
3. **Palavra que representa o assunto.** O assunto só segue se combinar com um tema de serviço de `TREND_THEMES`. Cada tema tem palavras-chave e um termo genérico de busca de foto. Exemplos:

   | Assunto em alta | Tema | Busca no Openverse | Busca no Pixabay |
   |---|---|---|---|
   | "onda de calor" | calor | air conditioner repair | ar condicionado manutenção |
   | "chuva em Goiânia" | chuva | roof repair | telhado conserto |
   | "Enem 2026" | estudos | student studying tutor | estudante estudando |
   | "Dia das Mães" | festas | party food buffet | festa salgados |

   Com a Groq configurada, ela pode encaixar em um tema da lista um assunto que as palavras não pegaram, como "Halloween" → festas. Ela também pode vetar o assunto. Ela nunca cria temas novos, e a busca nunca usa nomes de pessoas ou marcas.
4. **Foto.** A busca é feita no Openverse e, se houver chave, no Pixabay. Se o termo completo não trouxer nada, o sistema tenta uma versão mais curta ("air conditioner repair" → "air conditioner"). A foto é baixada e convertida para JPEG. Ela é guardada com autor, licença e página de origem, e não se repete por `IMAGE_REUSE_DAYS`.
5. **Publicação.** A publicação usa um dos modelos com foto (`foto_destaque` ou `foto_fundo`, alternando para comparar os dois). O texto da arte é a frase do tema. A legenda vem da Groq e termina com o crédito completo da foto.
6. **Revisão.** No modo automático (padrão), as checagens automáticas substituem a revisão: a foto é recusada se o título ou as tags indicarem pessoas ou marcas, e o assunto é triado de novo, com as manchetes mais recentes, logo antes de publicar (se ganhou um lado sensível, a publicação é cancelada). Com `REQUIRE_MANUAL_APPROVAL=true`, a publicação fica em `needs_review`. Em ambos os casos ela expira em `TREND_POST_TTL_HOURS` (48 h) se não for publicada. O publicador também se recusa a publicar se:
   - o crédito tiver sido apagado da legenda;
   - o assunto tiver expirado.

## Licenças aceitas

| Banco | Licenças aceitas | Crédito |
|---|---|---|
| Openverse | CC0, Marca de Domínio Público, CC BY | Na arte: "Foto: autor (CC BY 2.0)". Na legenda: título, autor, licença com link, origem e "Recortada e com texto adicionado". |
| Pixabay | Licença do Pixabay | "Imagem ilustrativa: autor / Pixabay." |

Licenças "não comercial" (NC), "sem derivações" (ND) e "compartilha igual" (SA) são recusadas.

A foto é **ilustrativa**. A legenda é instruída a nunca dizer que ela mostra usuários, prestadores ou trabalhos feitos pelo Doova. A conferência automática de pessoas e marcas usa o título e as tags da foto, então não é perfeita: confira de vez em quando o que saiu e, se aparecer algo inadequado, acrescente a palavra em `FLAG_WORDS` (`app/services/image_search.py`).

## Regras dos bancos (out/2026)

- **Openverse:** funciona sem chave, com limite de requisições. Raspar o acervo é proibido. O acervo está quase todo em inglês, por isso as buscas usam termos em inglês.
- **Pixabay:** a chave é gratuita e obrigatória (`PIXABAY_API_KEY`). O limite é de 100 requisições por minuto. Toda busca fica em cache por 24 h, o que é exigido pelo Pixabay. Não é permitido usar o link da imagem diretamente, por isso a foto é sempre baixada.

## Como usar

| O que fazer | Como |
|---|---|
| Ver o que o sistema faria agora, sem criar nada | `GET /api/trends/evaluate` |
| Rodar manualmente | `POST /api/trends/run` ou `python -m app.cli trends-run --force` |
| Rodar sozinho | `TREND_AUTO_ENABLED=true`. Roda a cada `TREND_AUTO_INTERVAL_HOURS`, com no máximo `TREND_AUTO_MAX_PER_DAY` publicações por dia. |
| Buscar fotos à mão | `GET /api/photos/search?q=roof%20repair&provider=openverse` |
| Importar uma foto escolhida | `POST /api/photos/import` com `{"provider": "openverse", "provider_id": "..."}` |
| Procurar e importar a primeira foto adequada | `POST /api/photos/find` com `{"query_en": "...", "query_pt": "..."}` |
| Usar a foto em qualquer arte | `POST /api/art` ou `POST /api/posts` com `params.photo_id` e um template com foto |
| Ajustar o recorte da foto | `params.photo_focus_y` (0 = topo, 1 = base) |
| Comparar resultados | `GET /api/metrics/compare?dimension=has_photo`, `photo_provider`, `content_kind` ou `template` |

## Acrescentar temas

Edite `TREND_THEMES` em `app/services/trend_content.py`. Cada tema precisa de:

- `label`;
- `audience`;
- `keywords` (sem acento);
- `query_en`;
- `query_pt`;
- `title` (frase genérica sobre a necessidade do serviço, sem afirmar fatos sobre a notícia).
