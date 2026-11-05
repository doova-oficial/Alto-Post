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

Com `TREND_AUTO_ENABLED=true`, o agendador tenta completar até 10 Reels em cada janela móvel
de 24 horas: primeiro usa tendências aprovadas pelos filtros; depois, temas perenes de
`app/content/themes.yaml`. Reels perenes não são apresentados como notícias ou tendências.
Para publicar 10 por dia, configure também `MAX_POSTS_PER_DAY=10`, intervalos e horários
compatíveis, `VIDEO_AUTO_PUBLISH=true`, `DRY_RUN=false` e aprovação automática. No modo Android,
o worker precisa ficar conectado, autenticado e processar a fila; jobs pendentes não são Reels
renderizados nem publicações concluídas.
O endpoint `GET /api/config` mostra os limites efetivos e se a publicação automática e o token
do worker estão configurados. Os limites de publicação no blueprint são 10 por 24 h, com
intervalo de 90 minutos entre os horários.

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
um Reel em tela dividida: o vídeo original fica na metade superior e um clipe de pessoa
usando celular com expressão de surpresa/reação, buscado no banco de vídeos configurado,
fica na metade inferior. A reação é uma cena ilustrativa de banco, não uma reação real ao
conteúdo. O servidor solicita a narração ao ElevenLabs e envia ao app o áudio e o alinhamento
palavra a palavra. O worker remove o som dos dois clipes, usa a narração e destaca a palavra
falada em amarelo, com contorno preto e as demais palavras em branco. Assim, a legenda
acompanha o áudio efetivamente gerado. O modo Android requer `ELEVENLABS_API_KEY` e
`ELEVENLABS_VOICE_ID` no servidor, além do banco de vídeos já configurado. Se não encontrar
um clipe de reação ou o áudio falhar, a tarefa reporta erro em vez de gerar um Reel incompleto.
O app não reproduz as transições nem todas as variantes do renderizador FFmpeg.

Quando houver pelo menos três Reels publicados com métricas disponíveis há 7 dias ou mais,
o prompt da Groq também recebe exemplos dos ganchos com melhores e piores sinais de retenção
(razão entre tempo médio assistido e duração) e compartilhamento (compartilhamentos/alcance).
São associações observacionais para orientar novos testes, não prova de causalidade. A legenda
publicada inclui `#doova` e hashtags relacionadas ao tema, sem duplicar tags que já existam.

## Painel e características analisadas

Abra `/analytics` no endereço do serviço e informe o `ADMIN_API_TOKEN` do Render. O token
fica somente na sessão desta aba; os dados e os endpoints permanecem protegidos pela
autenticação administrativa. O painel mostra métricas recentes por Reel e permite comparar
grupos na idade-alvo configurada (`ANALYSIS_TARGET_AGE_HOURS`, padrão 7 dias; snapshots
com pelo menos 80% desse período podem entrar na comparação).

As dimensões comparáveis incluem gancho, tema, faixa de duração, variante criativa, transição,
quantidade e composição de segmentos, presença de cards gerados, renderizador (Servidor ou
Android/Media3), dia/horário e quantidade de Reels publicados no dia. As métricas incluem
views, alcance, interações, compartilhamentos, visitas ao perfil e retenção aproximada
(tempo médio assistido dividido pela duração). O painel separa a última coleta das amostras
que já atingiram a idade-alvo e marca campos indisponíveis como ausentes, não como zero.

Em Android 15 ou superior, o sistema limita o uso contínuo de serviços foreground do tipo
`dataSync`; se atingir o limite diário, o usuário precisa abrir o app e iniciar o worker
novamente. A tarefa em andamento será retomada quando o lease expirar.
