# Conectar a conta do Instagram (Meta)

Fontes: documentação oficial da Instagram Platform, consultada em out/2026 (Graph API v26.0).

## Requisitos

- Conta do Instagram **profissional** (Empresa ou Criador de conteúdo).
- Um app em https://developers.facebook.com, com o produto **Instagram** adicionado.
- Para o caso deste projeto, usando a configuração **Instagram API with Unified Login** e token de usuário do sistema (SUAT), configure `META_LOGIN_TYPE=unified`:
  - usa `graph.facebook.com`;
  - exige que a conta profissional do Instagram esteja atribuída como ativo à configuração empresarial, com as permissões de publicação;
  - não use o mecanismo de renovação de tokens long-lived neste modo.
- Os modos anteriores continuam disponíveis: `instagram` usa `graph.instagram.com`; `facebook` usa `graph.facebook.com` e segue o fluxo tradicional de Facebook Login.

## Passo a passo (Unified Login / SUAT)

1. No painel do app, abra a configuração de **Instagram API with Unified Login**.
2. Configure os ativos empresariais e adicione a conta profissional do Instagram do Doova.
3. Conceda, no mínimo, `instagram_basic` e `instagram_content_publish`. Não habilite permissões de anúncios, mensagens ou comentários se o projeto não usar esses recursos.
4. Gere o token de usuário do sistema (SUAT) com acesso à conta do Instagram.
5. No Render, defina `META_LOGIN_TYPE=unified` e coloque o token em `META_ACCESS_TOKEN`.
6. Defina `META_IG_USER_ID` com o ID da conta profissional do Instagram. Não use o ID da Página do Facebook.
7. Mantenha `TOKEN_ENCRYPTION_KEY` configurada para proteger tokens que sejam armazenados pelo sistema. O SUAT não é renovado pelo endpoint de renovação do projeto; se for revogado/substituído, atualize `META_ACCESS_TOKEN` no Render.
8. Valide a conexão pelo endpoint `GET /api/integrations/meta/check` antes de desligar `DRY_RUN`.

Para os fluxos antigos, consulte as instruções correspondentes no painel da Meta. Os nomes de permissões e os tipos de token não devem ser misturados entre os diferentes métodos de login.

## Regras da API que o sistema respeita

| Regra | Como o sistema trata |
|---|---|
| Fluxo de publicação | `POST /<IG_ID>/media` (image_url, caption, alt_text) → `GET /<contêiner>?fields=status_code` até `FINISHED` → `POST /<IG_ID>/media_publish` |
| Somente **JPEG** | gerado automaticamente |
| Imagem em URL pública HTTPS | servida em `PUBLIC_BASE_URL/media/<token>.jpg` |
| Limite de **100 publicações por API em 24 h móveis** | consultado em `content_publishing_limit`; o limite próprio (`MAX_POSTS_PER_DAY`, padrão 2) é bem menor |
| Contêineres expiram em 24 h (`EXPIRED`) | tratados como falha com nova tentativa |
| Insights podem atrasar até 48 h | métricas ausentes são registradas como indisponíveis |
| `impressions` descontinuada para mídias após 02/07/2024 | o sistema usa `views` |

## Renovação do token

- No modo Unified Login, o SUAT não passa pelo mecanismo de renovação de tokens long-lived implementado para os outros modos.
- Se o SUAT for revogado ou precisar ser substituído, gere outro no painel da Meta e atualize `META_ACCESS_TOKEN` no Render.
- `POST /api/integrations/meta/refresh-token` retorna conflito (`409`) no modo Unified Login para evitar uma tentativa incompatível.
- Nos modos `instagram` e `facebook`, permanecem disponíveis os mecanismos anteriores de renovação de tokens long-lived.
- Para conferir o estado do token: `GET /api/integrations/meta/check` (somente leitura).
- Se o token expirar, as publicações falham com erro de autenticação e não são repetidas indefinidamente: viram `failed` e aparecem no histórico. Gere um token novo e reagende.

## Validação antes de desligar a simulação

1. Com `DRY_RUN=true`, rode `GET /api/integrations/meta/check`. Ele deve mostrar o `username` da conta e o uso do limite.
2. Abra no navegador `PUBLIC_BASE_URL/media/<token>.jpg` de uma arte criada. O caminho aparece no evento "SIMULAÇÃO" do histórico. A imagem precisa abrir sem login.
3. Crie uma publicação de teste e aprove-a. Mude para `DRY_RUN=false` e use `publish-now` **uma única vez**.
4. Confira no Instagram e em `GET /api/posts/{id}`: estado `published`, com `ig_media_id` e `permalink`.
5. Depois de 48 h, rode `POST /api/metrics/collect` e confira `GET /api/metrics/posts/{id}`.

Até concluir esse roteiro com a conta real, considere a publicação **não validada**.