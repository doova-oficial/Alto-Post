# Vídeos, ganchos e legendas

O fluxo de tendências agora pode criar um reel vertical a partir do mesmo tema que já é usado pela busca de fotos.
Por exemplo, uma tendência com `chuva` é classificada como `chuva`, usa a busca genérica `telhado conserto` no Pixabay
e não envia a manchete original para o banco de mídia.

## Configuração

Configure `PIXABAY_API_KEY` e as três variáveis do Cloudinary: `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY` e
`CLOUDINARY_API_SECRET`. O Dockerfile instala o `ffmpeg`, que converte o vídeo para 9:16, corta a duração configurada
e queima o texto em quatro blocos de legenda. O MP4 é enviado ao Cloudinary; o PostgreSQL guarda somente a URL,
metadados e o roteiro. Vídeos antigos que ainda tenham `mp4_bytes` continuam sendo servidos.

## Endpoints

- `POST /api/videos/trend` com `{"trend_term":"chuva em Goiânia", "render":true}` busca o vídeo, cria o gancho e renderiza o MP4.
- `GET /api/videos/{id}` retorna o roteiro SRT, o gancho e a origem.
- `GET /api/videos/{id}/file` serve o MP4 pronto.

O processamento automático de `POST /api/trends/run` também tenta gerar um vídeo para cada tendência aprovada. Se a
API de vídeos estiver indisponível, a publicação com foto não é perdida: o relatório retorna `video_error`.
