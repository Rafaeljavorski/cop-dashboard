# Dashboard COP Telegram PostgreSQL

## Como subir no Railway

Crie um novo serviço no mesmo projeto, apontando para o mesmo GitHub.

Start Command:
```bash
gunicorn dashboard_app:app --bind 0.0.0.0:$PORT
```

Variável obrigatória:
```text
DATABASE_URL
```

Use a mesma DATABASE_URL do serviço do bot.

Depois o Railway vai gerar uma URL pública para o dashboard.
