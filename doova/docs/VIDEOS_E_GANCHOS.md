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

No modo de renderização `server`, o processamento automático de `POST /api/trends/run` também tenta gerar um vídeo para
cada tendência aprovada. Se a API de vídeos estiver indisponível, a publicação com foto não é perdida: o relatório
retorna `video_error`. O comportamento do modo Android é diferente e está descrito abaixo.

### Worker Android

Para transferir o processamento para o celular, configure `VIDEO_RENDER_MODE=android` e um
`VIDEO_WORKER_TOKEN` exclusivo com pelo menos 32 caracteres. No Render, mantenha também
`VIDEO_AUTO_PUBLISH=true` para o tick publicar o Reel quando estiver pronto; deixe
`DRY_RUN=true` até validar o fluxo antes de habilitar a publicação real. O app Android
precisa da URL HTTPS do serviço e do mesmo token do worker; ele não usa credenciais da Meta,
Pexels ou Cloudinary.

Nesse modo, a publicação de tendência fica vinculada ao Reel: o publicador de fotos a ignora,
e o tick aguarda o upload do celular, a aprovação e o horário agendado. Se o celular estiver
offline ou a busca do vídeo falhar, a publicação é adiada em vez de publicar a foto sozinha.
Com `TREND_USE_GROQ=true` e Groq configurada, o servidor envia o termo em alta e até três
manchetes observadas à IA para gerar um roteiro curto: um gancho nos primeiros três segundos,
duas falas de contexto e a chamada do Doova. O prompt proíbe inventar fatos ou promessas; as
manchetes servem apenas como contexto. Se a Groq não estiver habilitada ou falhar, é usado o
roteiro padrão determinístico. O servidor envia as falas e seus tempos ao app, que renderiza
um vídeo-fonte vertical com áudio original e legendas simples; não reproduz as transições,
a narração TTS nem todas as variantes do renderizador FFmpeg.

Quando houver pelo menos três Reels publicados com métricas disponíveis há 24 horas ou mais,
o prompt da Groq também recebe exemplos dos ganchos com melhores e piores sinais de retenção
(razão entre tempo médio assistido e duração) e compartilhamento (compartilhamentos/alcance).
São associações observacionais para orientar novos testes, não prova de causalidade. A legenda
publicada inclui `#doova` e hashtags relacionadas ao tema, sem duplicar tags que já existam.

Em Android 15 ou superior, o sistema limita o uso contínuo de serviços foreground do tipo
`dataSync`; se atingir o limite diário, o usuário precisa abrir o app e iniciar o worker
novamente. A tarefa em andamento será retomada quando o lease expirar.
