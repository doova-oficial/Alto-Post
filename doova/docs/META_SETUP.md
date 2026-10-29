# Conectar a conta do Instagram (Meta)

Fontes: documentação oficial da Instagram Platform, consultada em out/2026 (Graph API v26.0).

## Requisitos

- Conta do Instagram **profissional** (Empresa ou Criador de conteúdo).
- Um app em https://developers.facebook.com, com o produto **Instagram** adicionado.
- O fluxo recomendado é o **Instagram API with Instagram Login** (`META_LOGIN_TYPE=instagram`):
  - usa `graph.instagram.com`;
  - não exige Página do Facebook.
- Alternativa: o fluxo com Facebook Login (`META_LOGIN_TYPE=facebook`), que exige a conta vinculada a uma Página.

## Passo a passo (Instagram Login)

1. No painel do app, abra **Instagram → API setup with Instagram login**.
2. Adicione a conta do Doova e gere o token de acesso. Permissões necessárias:
   - `instagram_business_basic`;
   - `instagram_business_content_publish`.
3. O token gerado no painel é de **longa duração (60 dias)**. Coloque-o em `META_ACCESS_TOKEN`.
4. Descubra o ID da conta:
   ```bash
   curl "https://graph.instagram.com/v26.0/me?fields=user_id,username&access_token=SEU_TOKEN"
   ```
   Use o valor de `user_id` em `META_IG_USER_ID`.
5. Preencha `META_APP_ID` e `META_APP_SECRET` (painel do app → Configurações → Básico).
6. Enquanto o app estiver em modo de desenvolvimento, apenas contas com função no app (administrador, desenvolvedor ou testador) podem ser usadas. Para uso permanente, siga as exigências de revisão de app da Meta para as permissões acima.

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

- Tokens de longa duração valem 60 dias. Podem ser renovados quando tiverem pelo menos 24 h e antes de expirar:
  ```
  GET https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token=...
  ```
- **Renovação automática:** com `TOKEN_ENCRYPTION_KEY` definida, o sistema:
  - renova o token toda segunda-feira (tarefa `token_refresh`);
  - guarda o novo token **criptografado** no banco;
  - passa a usá-lo no lugar de `META_ACCESS_TOKEN`.
- Para renovar manualmente: `POST /api/integrations/meta/refresh-token`.
- Sem chave de criptografia: renove pelo painel da Meta e atualize `META_ACCESS_TOKEN` antes de 60 dias.
- Para conferir o estado do token: `GET /api/integrations/meta/check` (somente leitura).
- Se o token expirar, as publicações falham com erro de autenticação e não são repetidas indefinidamente: viram `failed` e aparecem no histórico. Gere um token novo e reagende.

## Validação antes de desligar a simulação

1. Com `DRY_RUN=true`, rode `GET /api/integrations/meta/check`. Ele deve mostrar o `username` da conta e o uso do limite.
2. Abra no navegador `PUBLIC_BASE_URL/media/<token>.jpg` de uma arte criada. O caminho aparece no evento "SIMULAÇÃO" do histórico. A imagem precisa abrir sem login.
3. Crie uma publicação de teste e aprove-a. Mude para `DRY_RUN=false` e use `publish-now` **uma única vez**.
4. Confira no Instagram e em `GET /api/posts/{id}`: estado `published`, com `ig_media_id` e `permalink`.
5. Depois de 48 h, rode `POST /api/metrics/collect` e confira `GET /api/metrics/posts/{id}`.

Até concluir esse roteiro com a conta real, considere a publicação **não validada**.