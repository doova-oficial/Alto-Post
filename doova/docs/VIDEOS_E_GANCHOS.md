# Vídeos, ganchos e legendas

O fluxo de tendências agora pode criar um reel vertical a partir do mesmo tema que já é usado pela busca de fotos.
Por exemplo, uma tendência com `chuva` é classificada como `chuva`, usa a busca genérica `telhado conserto` no Pixabay
e não envia a manchete original para o banco de mídia.

## Configuração

Configure `PEXELS_API_KEY` (Pixabay só é fallback se `VIDEO_PIXABAY_FALLBACK_ENABLED=true`), as credenciais do ElevenLabs (`ELEVENLABS_API_KEY` e
`ELEVENLABS_VOICE_ID`) e as três variáveis do Cloudinary: `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY` e
`CLOUDINARY_API_SECRET`. O Dockerfile instala o `ffmpeg`, que converte o vídeo para 9:16, intercala até três
placeholders com cortes secos, corta a duração configurada e queima o texto em quatro blocos de legenda sem borda preta.
O MP4 é enviado ao Cloudinary; o PostgreSQL guarda somente a URL,
metadados e o roteiro. Vídeos antigos que ainda tenham `mp4_bytes` continuam sendo servidos.

No ElevenLabs, `ELEVENLABS_VOICE_ID` deve apontar para uma voz própria disponível em `My Voices`.
Vozes da Voice Library não ficam disponíveis pela API para contas gratuitas.

O roteiro enviado ao ElevenLabs permanece limpo, sem instruções de tom entre colchetes.
Além dos placeholders do Pexels, `VIDEO_GENERATED_IMAGE_COUNT` cria cards verticais locais
com o tema e as falas do roteiro. Eles são intercalados no vídeo com transições `xfade` e
ficam registrados em `segments` no retorno da API e em `metadata_json`, com início, fim,
tipo de mídia e transição. Isso permite comparar depois o desempenho de cada trecho;
retenção segundo a segundo ainda depende de uma fonte de métricas de Reels que forneça esse dado.

Para instâncias com 512 MB, o perfil recomendado mantém 1080x1920, dois vídeos de stock,
um card gerado, downloads de até 12 MB e uma thread do FFmpeg. Esses valores já estão
no `render.yaml`; aumente-os somente se houver memória disponível.

## Endpoints

- `POST /api/videos/trend` com `{"trend_term":"chuva em Goiânia", "render":true}` busca o vídeo, cria o gancho e renderiza o MP4.
- Se o mesmo vídeo de stock já tiver sido usado, envie `"force":true` para re-renderizar o teste; sem isso o fluxo continua idempotente e devolve o vídeo existente.
- `GET /api/videos/{id}` retorna o roteiro SRT, o gancho e a origem.
- `GET /api/videos/{id}/file` serve o MP4 pronto.

O processamento automático de `POST /api/trends/run` também tenta gerar um vídeo para cada tendência aprovada. Se a
API de vídeos estiver indisponível, a publicação com foto não é perdida: o relatório retorna `video_error`.
