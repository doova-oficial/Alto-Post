# Configurar a Groq

1. Crie uma chave em https://console.groq.com/keys e coloque-a em `GROQ_API_KEY`.
2. `GROQ_MODEL` tem como padrão `openai/gpt-oss-120b`, modelo de produção na lista da Groq em out/2026.
   - `openai/gpt-oss-20b` é uma alternativa mais barata e rápida.
   - Não use `llama-3.3-70b-versatile` nem `llama-3.1-8b-instant`: foram desligados para os planos gratuito e de desenvolvimento em 16/08/2026.
3. Confira os modelos disponíveis na sua conta: `GET /api/integrations/groq/models`.

## Como a legenda é gerada

- **Endpoint:** `POST https://api.groq.com/openai/v1/chat/completions`.
- **Parâmetros:** `max_completion_tokens`, `reasoning_effort=low` e `include_reasoning=false`, para que só a legenda volte.
- **Prompt:** inclui as regras do brief e os fatos de `app/content/doova_context.yaml`. Sugere uma estrutura diferente a cada vez, para variar o texto.
- **Proteções:**
  - timeout de 30 s;
  - até 3 novas tentativas com espera exponencial, respeitando `retry-after` quando a resposta é 429;
  - intervalo mínimo de 2 s entre chamadas.
- **Validação posterior:**
  - remove emojis preservando acentos;
  - limita a 3 hashtags e normaliza a pontuação;
  - detecta: renda garantida, números de usuários, vagas não verificadas, superlativos, placeholders, introduções do modelo e textos incompletos.
- **Se a Groq falhar:** a publicação vai para `caption_failed` e o sistema tenta de novo em 5, 10, 20... minutos, até `CAPTION_MAX_ATTEMPTS`. Depois disso, ela vai para `needs_review`. **Nunca** é publicada com legenda vazia.
- **Emojis:** para liberar no futuro, use `CAPTION_ALLOW_EMOJIS=true`. O prompt passa a permitir no máximo um.